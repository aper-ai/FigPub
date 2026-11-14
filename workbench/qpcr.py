"""Explicit-identity qPCR import and ΔΔCt analysis, without GUI dependencies.

New implementation for Figure Workbench (AGPL-3.0-only). It uses the standard
equal-efficiency ΔΔCt method; no replicates or significance values are invented.
Technical Ct repeats are averaged before biological-sample comparisons.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import islice
from io import StringIO
from math import isfinite, sqrt
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Mapping

import numpy as np

from .analysis import descriptive_statistics
from .statistics import compare_biological_groups


COLUMN_ALIASES: dict[str, set[str]] = {
    "sample": {"sample", "sample name", "samplename", "sample_name", "name", "样本", "样本名称", "sample_id"},
    "target": {"target", "target name", "target_name", "gene", "assay", "detector", "靶标", "基因"},
    "ct": {"ct", "cq", "cp", "c t", "ct value", "cq value", "threshold cycle", "ct值", "cq值"},
    "group": {"group", "condition", "treatment", "group name", "组别", "分组"},
    "biological_replicate": {"biological_replicate", "biological replicate", "biological", "bio", "生物重复"},
    "technical_replicate": {"technical_replicate", "technical replicate", "technical", "tech", "技术重复"},
    "well": {"well", "well position", "position", "孔", "孔位"},
    "plate_id": {"plate", "plate_id", "plate id", "板号"},
    "experiment_id": {"experiment_id", "experiment", "experiment id", "实验编号"},
    "included": {"included", "include", "use", "纳入", "是否纳入"},
}

MAX_QPCR_ROWS = 200_000
MAX_QPCR_COLUMNS = 512
MAX_QPCR_CELLS = 2_000_000


def _column_key(value: Any) -> str:
    return str(value).replace("\ufeff", "").strip().casefold()


def _canonical_header(cells: Iterable[Any]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    duplicates: set[str] = set()
    for index, value in enumerate(cells):
        name = _column_key(value)
        for canonical, aliases in COLUMN_ALIASES.items():
            if name in aliases:
                if canonical in mapping.values():
                    duplicates.add(canonical)
                mapping[index] = canonical
                break
    # Instrument metadata and unrelated workbook sheets are scanned before the
    # real header. Repeated words such as "Sample"/"Name" on those rows must not
    # prevent discovery of a later, valid Ct table. A genuine candidate header
    # still fails on ambiguity; we never silently choose one of duplicate columns.
    if duplicates and {"sample", "target", "ct"}.issubset(mapping.values()):
        names = "、".join(sorted(duplicates))
        raise ValueError(f"qPCR 表有多个 {names} 列，请保留一个或明确改名。")
    return mapping


def canonicalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in row.items():
        for canonical, aliases in COLUMN_ALIASES.items():
            if _column_key(key) in aliases:
                result[canonical] = value
                break
    return result


def choose_text_delimiter(text: str) -> str:
    """Choose comma/Tab/semicolon from evidence, including instrument preambles.

    A recognized qPCR header outranks generic sniffing. Without one, require a
    consistent, genuinely multicolumn sample; arbitrary prose is not a table.
    Only the first 64 KiB and 100 records are inspected for delimiter choice.
    Callers still apply their complete-file row/cell limits before accepting it.
    """
    sample = text[:65536]
    candidates = []
    for delimiter in ('\t', ',', ';'):
        try:
            rows = list(islice(csv.reader(sample.splitlines(), delimiter=delimiter), 100))
        except csv.Error:
            continue
        for index, cells in enumerate(rows):
            if len(cells) < 3:
                continue
            mapping = _canonical_header(cells)
            if {'sample', 'target', 'ct'}.issubset(mapping.values()):
                candidates.append((len(mapping), -index, delimiter))
                break
    if candidates:
        return max(candidates)[2]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=',\t;').delimiter
        rows = [row for row in islice(csv.reader(sample.splitlines(), delimiter=delimiter), 100)
                if any(str(value).strip() for value in row)]
    except csv.Error as exc:
        raise ValueError('无法识别 TXT/CSV 的多列分隔符；请提供有表头的逗号、Tab 或分号分隔表格。') from exc
    if len(rows) < 2 or len(rows[0]) < 2 or any(len(row) != len(rows[0]) for row in rows[1:]):
        raise ValueError('TXT/CSV 不是结构明确的多列表格；请使用一致的逗号、Tab 或分号分隔符。')
    return delimiter


def _bounded_qpcr_rows(rows: Iterable[Iterable[Any]]) -> Iterable[list[Any]]:
    """Apply import limits while reading, before retaining the whole table."""
    cells = 0
    for row_number, source_row in enumerate(rows, 1):
        if row_number > MAX_QPCR_ROWS:
            raise ValueError(f"qPCR 表格超过 {MAX_QPCR_ROWS} 行，请分批导入。")
        row = list(source_row)
        if len(row) > MAX_QPCR_COLUMNS:
            raise ValueError(f"qPCR 表格超过 {MAX_QPCR_COLUMNS} 列，请仅保留分析所需字段。")
        cells += len(row)
        if cells > MAX_QPCR_CELLS:
            raise ValueError(f"qPCR 表格超过 {MAX_QPCR_CELLS} 个单元格，请分批导入。")
        yield row


def _find_qpcr_header(rows: Iterable[Iterable[Any]]) -> tuple[int, dict[int, str]] | None:
    for index, cells in enumerate(islice(rows, 100)):
        mapping = _canonical_header(cells)
        if {"sample", "target", "ct"}.issubset(mapping.values()):
            return index, mapping
    return None


def read_qpcr_table(path: str | Path, sheet: str | int | None = None) -> dict[str, Any]:
    """Read a real CSV/TSV/XLSX table, finding an instrument preamble safely."""
    source = Path(path)
    suffix = source.suffix.lower()
    if source.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("qPCR 表格超过 100 MB，请分批导入。")
    sheet_names: list[str] = []
    if suffix in {".csv", ".tsv", ".txt"}:
        raw = source.read_bytes()
        try:
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeDecodeError:
            # Guessing BOM-less UTF-16 can silently reinterpret an even-length
            # GB18030 export as unrelated Unicode. UTF-16 requires its BOM.
            text = raw.decode("gb18030")
        delimiter = "\t" if suffix == ".tsv" else choose_text_delimiter(text)
        try:
            table = list(_bounded_qpcr_rows(csv.reader(StringIO(text), delimiter=delimiter, strict=True)))
        except csv.Error as exc:
            raise ValueError("qPCR 表格格式无效，请检查分隔符和字段引号。") from exc
        name = None
        header = _find_qpcr_header(table)
    elif suffix == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(source, read_only=True, data_only=True, keep_links=False)
        try:
            sheet_names = list(book.sheetnames)
            if isinstance(sheet, int):
                if not 0 <= sheet < len(book.sheetnames):
                    raise ValueError("工作表索引超出范围。")
                names = [book.sheetnames[sheet]]
            elif isinstance(sheet, str):
                if sheet not in book.sheetnames:
                    raise ValueError(f"工作表不存在：{sheet}")
                names = [sheet]
            else:
                names = book.sheetnames
            candidates = []
            for name in names:
                worksheet = book[name]
                # Inspect only candidate headers. Instrument workbooks often
                # contain large, unrelated metadata sheets; they must not be
                # materialized or make a valid Results table unreadable.
                max_column = min(worksheet.max_column or MAX_QPCR_COLUMNS + 1, MAX_QPCR_COLUMNS + 1)
                found = _find_qpcr_header(worksheet.iter_rows(values_only=True, max_row=100, max_col=max_column))
                if found is not None:
                    score = len(found[1]) + (20 if "result" in name.casefold() else 0)
                    candidates.append((score, name, found))
            if not candidates:
                raise ValueError("未找到 sample / target / ct（或对应仪器列名）的表头。")
            _, name, header = max(candidates, key=lambda item: item[0])
            table = list(_bounded_qpcr_rows(book[name].iter_rows(values_only=True)))
        finally:
            book.close()
    else:
        raise ValueError("qPCR 导入支持 CSV、TSV、TXT 和 XLSX；旧 XLS 请另存为 XLSX。")
    if header is None:
        raise ValueError("未找到 sample / target / ct（或对应仪器列名）的表头。")
    header_index, mapping = header
    header_width = len(table[header_index])
    rows: list[dict[str, Any]] = []
    for line_number, cells in enumerate(table[header_index + 1:], header_index + 2):
        if not any(value is not None and str(value).strip() for value in cells):
            continue
        if any(value is not None and str(value).strip() for value in cells[header_width:]):
            raise ValueError(f"第 {line_number} 行包含超出表头的非空数据，请检查分隔符和字段引号。")
        record = {canonical: cells[index] if index < len(cells) else None for index, canonical in mapping.items()}
        record["source_row"] = line_number
        rows.append(record)
    if not rows:
        raise ValueError("qPCR 表没有数据行。")
    return {"rows": rows, "columns": list(mapping.values()), "source": source.name,
            "header_row": header_index + 1, "sheet": name, "sheets": sheet_names, "warnings": []}


def included_flag(value: Any) -> bool:
    """CSV boolean strings must not silently turn excluded wells back on."""
    if value is None or value == "":
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "1.0", "true", "yes", "y", "是", "纳入"}:
        return True
    if text in {"0", "0.0", "false", "no", "n", "否", "排除"}:
        return False
    raise ValueError(f"无法识别 included 值：{text[:40]}；请使用 true/false 或 1/0。")


@dataclass(frozen=True, slots=True)
class CtObservation:
    source_row: int
    sample: str
    target: str
    ct: float | None
    group: str
    biological_replicate: str
    technical_replicate: str
    experiment_id: str
    well: str
    plate_id: str
    inferred_biological_identity: bool
    included: bool

    @property
    def sample_key(self) -> tuple[str, str, str, str]:
        return self.experiment_id, self.group, self.biological_replicate, self.sample

    @classmethod
    def parse(cls, record: Mapping[str, Any], index: int, group_mapping: Mapping[str, str]) -> "CtObservation":
        data = canonicalize_row(record)

        def text(name: str, default: str = "") -> str:
            value = data.get(name, default)
            return str(value).strip() if value is not None else ""

        sample, target = text("sample"), text("target")
        if not sample or not target:
            raise ValueError(f"qPCR 第 {index + 1} 条数据缺少 sample 或 target。")
        raw_ct = data.get("ct")
        try:
            # Excel Boolean cells and JSON true/false are not Ct measurements.
            # float(True) would otherwise contribute a spurious Ct of 1.0.
            ct = None if isinstance(raw_ct, (bool, np.bool_)) else float(raw_ct)
            if ct is not None and (not isfinite(ct) or ct < 0 or ct > 100):
                ct = None
        except (TypeError, ValueError):
            ct = None
        experiment = text("experiment_id") or "experiment-1"
        group = str(group_mapping.get(f"{experiment}::{sample}", group_mapping.get(sample, text("group")))).strip()
        if not group:
            raise ValueError(f"样本 {sample} 未指定组别。请填写 group 或提供 group_mapping，以明确样本分组。")
        biological = text("biological_replicate")
        return cls(
            int(data.get("source_row", index + 1)), sample, target, ct, group,
            biological or sample, text("technical_replicate"), experiment,
            text("well"), text("plate_id") or "plate-1", not bool(biological), included_flag(data.get("included", True)),
        )


def analyze_qpcr(rows: list[dict[str, Any]], params: dict[str, Any], *, cancel_event=None) -> dict[str, Any]:
    started = perf_counter()
    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('qPCR 分析已取消。')
    check_cancel()
    if not rows:
        raise ValueError("请先导入 qPCR Ct 数据。")
    if len(rows) > 200000:
        raise ValueError("一次最多分析 200000 条 Ct 记录。")
    if params.get("method", "delta_delta_ct") != "delta_delta_ct":
        raise ValueError("当前 qPCR 分析使用等扩增效率 ΔΔCt 方法。")
    references = params.get("reference_genes") or [params.get("reference_gene", "")]
    if not isinstance(references, (list, tuple)):
        raise ValueError("reference_genes 必须是内参名称列表。")
    references = [str(value).strip() for value in references if str(value).strip()]
    if not references:
        raise ValueError("请明确选择内参基因 reference_gene。")
    if len(set(references)) != len(references):
        raise ValueError("内参基因列表包含重复项。")
    control_group = str(params.get("control_group", "")).strip()
    if not control_group:
        raise ValueError("请明确选择对照组 control_group。")
    max_ct = params.get("max_ct")
    if max_ct is not None and (not isfinite(float(max_ct)) or float(max_ct) <= 0):
        raise ValueError("max_ct 必须是正的有限数值。")
    technical_sd_limit = float(params.get("technical_sd_limit", 0.5))
    if not isfinite(technical_sd_limit) or technical_sd_limit <= 0:
        raise ValueError("technical_sd_limit 必须为正数。")
    mapping = params.get("group_mapping") or {}
    if not isinstance(mapping, dict):
        raise ValueError("group_mapping 必须是样本身份到组别的映射。")
    excluded_rows = params.get("excluded_source_rows") or []
    if not isinstance(excluded_rows, list) or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in excluded_rows):
        raise ValueError("excluded_source_rows 必须是原表行号的正整数列表。")
    excluded_rows = set(excluded_rows)
    observations = []
    for index, row in enumerate(rows):
        if index % 256 == 0:
            check_cancel()
        observations.append(CtObservation.parse(row, index, mapping))
    observations = [replace(row, included=False) if row.source_row in excluded_rows else row for row in observations]
    missing_references = set(references) - {row.target for row in observations}
    if missing_references:
        raise ValueError(f"表内没有内参基因：{', '.join(sorted(missing_references))}")
    if control_group not in {row.group for row in observations}:
        raise ValueError(f"表内没有对照组：{control_group}")

    warnings: list[str] = []
    cleaned_rows: list[dict[str, Any]] = []
    duplicate_keys: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, observation in enumerate(observations):
        if index % 256 == 0:
            check_cancel()
        qc: list[str] = []
        included = observation.included and observation.ct is not None
        if observation.ct is None:
            qc.append("undetermined_or_invalid_ct")
        elif max_ct is not None and observation.ct > float(max_ct):
            qc.append("above_explicit_ct_cutoff")
            included = False
        elif observation.ct > 35:
            qc.append("high_ct_review_required")
        if not observation.included:
            qc.append("excluded_by_user")
        if observation.well:
            duplicate_keys[(observation.experiment_id, observation.plate_id, observation.well, observation.target)].append(index)
        elif observation.technical_replicate:
            duplicate_keys[(*observation.sample_key, observation.target, observation.technical_replicate)].append(index)
        cleaned_rows.append({
            "source_row": observation.source_row, "sample": observation.sample, "target": observation.target,
            "ct": observation.ct, "group": observation.group, "biological_replicate": observation.biological_replicate,
            "technical_replicate": observation.technical_replicate, "experiment_id": observation.experiment_id,
            "well": observation.well, "plate_id": observation.plate_id, "included": included, "qc_flags": qc,
        })
    for indices in duplicate_keys.values():
        # An explicit exclusion resolves a duplicated import/technical ID.
        # Keep all source rows, but do not let a deselected copy invalidate the
        # one record the user deliberately retained. Automatic Ct QC alone is
        # not enough to resolve an ambiguous duplicate identity.
        selected_indices = [index for index in indices if observations[index].included]
        if len(selected_indices) > 1:
            for index in selected_indices:
                cleaned_rows[index]["included"] = False
                cleaned_rows[index]["qc_flags"].append("duplicate_well_or_technical_id")

    technical: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for observation, cleaned in zip(observations, cleaned_rows):
        if cleaned["included"]:
            technical[(*observation.sample_key, observation.target)].append(cleaned)
    technical_rows: list[dict[str, Any]] = []
    by_sample: dict[tuple[str, str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for key, wells in technical.items():
        experiment_id, group, biological_id, sample, target = key
        stats = descriptive_statistics([well["ct"] for well in wells])
        record = {"experiment_id": experiment_id, "group": group, "biological_replicate": biological_id,
                  "sample": sample, "target": target, "mean_ct": stats["mean"], "sd_ct": stats["sd"],
                  "n_technical": stats["n"], "source_rows": [well["source_row"] for well in wells],
                  "qc_flags": ["technical_ct_sd_high"] if stats["sd"] is not None and stats["sd"] > technical_sd_limit else []}
        technical_rows.append(record)
        by_sample[(experiment_id, group, biological_id, sample)][target] = record
    sample_delta_rows: list[dict[str, Any]] = []
    for sample_key, targets in by_sample.items():
        if not all(reference in targets for reference in references):
            warnings.append(f"样本 {sample_key[3]} 缺少有效内参 Ct，未生成 ΔCt。")
            continue
        reference_ct = float(np.mean([targets[reference]["mean_ct"] for reference in references]))
        for target, technical_row in targets.items():
            if target in references:
                continue
            sample_delta_rows.append({**technical_row, "reference_ct": reference_ct,
                                      "reference_genes": references, "delta_ct": technical_row["mean_ct"] - reference_ct})
    if not sample_delta_rows:
        raise ValueError("没有可配对的目标基因和内参；请检查样本身份、孔重复和缺失 Ct。")
    biological: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in sample_delta_rows:
        biological[(row["experiment_id"], row["group"], row["biological_replicate"], row["target"])].append(row)
    biological_rows: list[dict[str, Any]] = []
    for (experiment_id, group, biological_id, target), entries in biological.items():
        biological_rows.append({
            "experiment_id": experiment_id, "group": group, "biological_replicate": biological_id, "target": target,
            "sample_ids": [row["sample"] for row in entries], "delta_ct": float(np.mean([row["delta_ct"] for row in entries])),
            "n_technical": sum(row["n_technical"] for row in entries), "n_samples": len(entries),
            "source_rows": [number for row in entries for number in row["source_rows"]],
            "reference_ct": float(np.mean([row["reference_ct"] for row in entries])),
            "mean_ct": float(np.mean([row["mean_ct"] for row in entries])),
        })
    baseline_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in biological_rows:
        if row["group"] == control_group:
            baseline_buckets[(row["experiment_id"], row["target"])].append(row["delta_ct"])
    baselines = {key: float(np.mean(values)) for key, values in baseline_buckets.items()}
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in biological_rows:
        baseline = baselines.get((row["experiment_id"], row["target"]))
        row.update({"control_mean_delta_ct": baseline, "delta_delta_ct": None, "fold_change": None,
                    "normalized": None, "value": None})
        if baseline is None:
            warnings.append(f"实验 {row['experiment_id']} 的 {row['target']} 缺少有效对照，未生成表达倍数。")
            continue
        delta_delta = row["delta_ct"] - baseline
        fold_change = float(2.0 ** (-delta_delta))
        row.update({"delta_delta_ct": delta_delta, "fold_change": fold_change, "normalized": fold_change, "value": fold_change})
        grouped[(row["experiment_id"], row["target"], row["group"])].append(row)
    groups: list[dict[str, Any]] = []
    for (experiment_id, target, group), entries in grouped.items():
        values = [row["fold_change"] for row in entries]
        stats = descriptive_statistics(values)
        delta_values = [row["delta_ct"] for row in entries]
        delta_stats = descriptive_statistics(delta_values)
        geometric_mean = float(2.0 ** (-(delta_stats["mean"] - baselines[(experiment_id, target)])))
        groups.append({"experiment_id": experiment_id, "target": target, "group": group, **stats,
                       "n_biological": stats["n"], "n_technical": sum(row["n_technical"] for row in entries),
                       "metric": "fold_change", "values": values, "delta_ct_values": delta_values,
                       "geometric_mean_fold": geometric_mean})
    if not groups:
        raise ValueError("没有任何目标基因可与对照组比较。")
    test = str(params.get("test", "none"))
    check_cancel()
    inference = compare_biological_groups(biological_rows, feature="target", value="delta_ct",
                                         control_group=control_group, test=test, basis="biological_delta_ct")
    warnings.extend(inference["warnings"])
    if any(observation.inferred_biological_identity for observation in observations):
        warnings.append("未填写 biological_replicate 的记录按 sample 合并；请确认每个 sample 确为独立生物样本。")
    excluded = sum(not row["included"] for row in cleaned_rows)
    if excluded:
        warnings.append(f"{excluded} 个孔因无效 Ct、用户排除、阈值或重复身份未用于均值；详见 source_rows。")
    if any("high_ct_review_required" in row["qc_flags"] for row in cleaned_rows):
        warnings.append("存在 Ct >35 的孔，已标记供复核。未设置 Ct 排除阈值时，这些孔仍纳入分析。")
    if any(row["qc_flags"] for row in technical_rows):
        warnings.append("部分技术重复 Ct 离散超过设置阈值，请复核扩增曲线及孔质量。")
    if any(group["n_biological"] < 2 for group in groups):
        warnings.append("部分组独立生物重复 n=1，SD/SEM 为空。")
    return {
        "kind": "qpcr", "rows": biological_rows, "groups": groups,
        "source_rows": cleaned_rows, "technical_rows": technical_rows, "sample_delta_rows": sample_delta_rows,
        "comparisons": inference["comparisons"], "omnibus": inference["omnibus"], "warnings": list(dict.fromkeys(warnings)),
        "method": {"name": "delta_delta_ct", "reference_genes": references, "control_group": control_group,
                   "technical_aggregation": "mean_ct_per_sample_before_delta_ct", "statistics_unit": "biological_replicate",
                   "amplification_efficiency_assumption": "equal_target_and_reference_efficiency_approximately_2",
                   "control_baseline": "mean_biological_delta_ct_within_experiment", "test": test,
                   "precision": "float64_without_intermediate_rounding"},
        "elapsed_ms": (perf_counter() - started) * 1000,
    }
