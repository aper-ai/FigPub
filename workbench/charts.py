"""Source-linked statistical charts with portable, outlined text.

Labels are laid out with the actual bundled font, then converted to SVG paths.
Scientific values and provenance remain data, so charts can be regenerated;
outlined SVG labels are not advertised as independently editable text.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid
import warnings

import numpy as np

_RENDER_LOCK = threading.RLock()
_FONT_PATH = Path(__file__).resolve().parents[1] / "resources/fonts/DejaVuSans.ttf"
_CJK_COLLECTION = _FONT_PATH.with_name("NotoSansCJK-Regular.ttc")


def _font_cache_directory() -> Path:
    override = os.environ.get("FIGURE_WORKBENCH_CACHE")
    if override:
        return Path(override) / "fonts"
    data_directory = os.environ.get("FIGURE_WORKBENCH_DATA")
    if data_directory:
        return Path(data_directory) / "cache" / "fonts"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FigureWorkbench" / "Cache" / "fonts"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "FigureWorkbench" / "fonts"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "figure-workbench" / "fonts"


@lru_cache(maxsize=4)
def _font_codepoints(path_string: str, size: int, modification_ns: int) -> frozenset[int]:
    from fontTools.ttLib import TTFont
    font = TTFont(path_string, lazy=True)
    try:
        return frozenset(font.getBestCmap() or {})
    finally:
        font.close()


def _coverage(path: Path) -> frozenset[int]:
    stat = path.stat()
    return _font_codepoints(str(path), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=4)
def _cjk_face_cached(collection_string: str, size: int, modification_ns: int, cache_string: str) -> Path:
    """Extract SC face once to a stable, content-addressed application cache.

    Matplotlib/FreeType can keep a font file open. A persistent filename avoids
    deleting a temporary font underneath Windows font caches. It also avoids
    shipping another full-size copy of the existing TTC in the release archive.
    """
    from fontTools.ttLib import TTFont
    collection = Path(collection_string)
    with collection.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    directory = Path(cache_string)
    destination = directory / f"NotoSansCJK-SC-{digest[:20]}-face2.otf"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            # Reject damaged caches instead of silently using an OS fallback.
            if ord("中") in _coverage(destination):
                return destination
            raise ValueError("缓存的中文字体不完整，请删除字体缓存后重试。")
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        font = TTFont(collection, fontNumber=2, recalcTimestamp=False)
        try:
            font.save(temporary)
        finally:
            font.close()
        try:
            # Another process can finish the same cache file. Never replace an
            # already-open destination simply to install identical bytes.
            if destination.exists():
                temporary.unlink()
            else:
                try:
                    os.replace(temporary, destination)
                except OSError:
                    if not destination.is_file():
                        raise
                    temporary.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        if ord("中") not in _coverage(destination):
            raise ValueError("打包的中文字体缺少所需字形。")
        return destination
    except OSError as exc:
        raise ValueError("无法创建或读取图表中文字体缓存；请检查应用缓存目录的写入权限。") from exc


def _font_for_text(text: str) -> Path:
    # Newline/tab are layout controls, not glyphs. Every actual displayed
    # character must be supported by the selected bundled font.
    from fontTools.ttLib import TTLibError
    needed = {ord(character) for character in text if character not in "\n\r\t"}
    try:
        if needed <= _coverage(_FONT_PATH):
            return _FONT_PATH
        stat = _CJK_COLLECTION.stat()
        cjk = _cjk_face_cached(str(_CJK_COLLECTION), stat.st_size, stat.st_mtime_ns,
                               str(_font_cache_directory()))
        missing = needed - _coverage(cjk)
    except (OSError, TTLibError) as exc:
        raise ValueError("图表所需字体资源缺失或损坏，请恢复安装包 resources/fonts 中的字体。") from exc
    if missing:
        labels = " ".join(f"{chr(code)} (U+{code:04X})" for code in sorted(missing)[:12])
        raise ValueError("图表字体无法表示以下字符，请修改标签或安装支持的字体资源：" + labels)
    return cjk


def analysis_chart(result: dict, chart_type: str = "bar", width_mm: float = 80, height_mm: float = 60,
                   *, provenance: dict | None = None) -> bytes:
    if chart_type not in {"bar", "box", "scatter"}:
        raise ValueError("支持的图表为 bar、box、scatter")
    if not 30 <= float(width_mm) <= 300 or not 25 <= float(height_mm) <= 300:
        raise ValueError("图表物理尺寸超出 30–300 mm 范围")
    groups = []
    source_groups = result.get("groups", [])
    experiment_ids = {str(group.get("experiment_id") or "experiment-1") for group in source_groups}
    metrics = set()
    for group in source_groups:
        observations = np.asarray(group.get("values", []), dtype=float)
        observations = observations[np.isfinite(observations)]
        if not observations.size:
            continue
        target = group.get("target") or group.get("protein") or ""
        label = str(group.get("group", ""))
        if target:
            label = f"{target}\n{label}"
        if len(experiment_ids) > 1:
            label = f"{group.get('experiment_id') or 'experiment-1'}\n{label}"
        groups.append((label, observations))
        metrics.add(str(group.get("metric") or "value"))
    if len(metrics) > 1:
        raise ValueError("结果包含不同计量方式，不能共用一个数值轴。请补齐各实验的对照组后重新分析，或分别为一致计量方式的数据作图。")
    microscopic = result.get("kind") == "microscopy"
    if not groups and microscopic:
        observations = [obj.get("area_um2", obj.get("area_px")) for obj in result.get("objects", [])]
        values = np.asarray([value for value in observations if value is not None and np.isfinite(value)], dtype=float)
        if values.size:
            groups.append(("Objects", values))
    if not groups:
        raise ValueError("没有可作图的独立样本数据。请先完成分组与有效测量，不能从均值制造重复点。")
    if sum(len(values) for _, values in groups) > 100000:
        raise ValueError("观测点过多，请先按实验设计汇总为独立样本")
    labels = [f"{label}\nn={len(values)}" for label, values in groups]
    metric = next(iter(metrics)) if metrics else "area"
    y_label = {"fold_change": "Relative expression", "fold": "Relative intensity", "normalized": "Target / loading control",
               "net_intensity": "Integrated signal (a.u.)", "integrated_density": "Integrated signal (a.u.)",
               "background_subtracted": "Background-subtracted signal (a.u.)"}.get(metric, "Measured value")
    if microscopic:
        y_label = "Object area (µm²)" if any(obj.get("area_um2") is not None for obj in result.get("objects", [])) else "Object area (px²)"
    if microscopic:
        caption = "Objects within one image; not biological replicates"
    elif chart_type == "box":
        caption = "Dots: biological replicates · boxes: IQR; line: median; n=1: point only"
    else:
        caption = "Dots: biological replicates · mean ± SD; n=1: no SD"
    # All strings used by chart layout, including automatic scientific ticks.
    display_text = "\n".join(labels + [y_label, caption, "0123456789.,+−×e%"])
    description = json.dumps({
        "application": "Figure Workbench", "chart_type": chart_type,
        "width_mm": float(width_mm), "height_mm": float(height_mm), "kind": result.get("kind"),
        "text_mode": "glyph_paths", "label_editing": "regenerate_from_data",
        "plot_groups": [{"label": label, "values": values.tolist()} for label, values in groups],
        "y_label": y_label, "caption": caption, "provenance": provenance or {},
    }, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with _RENDER_LOCK:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
        from matplotlib import font_manager
        from matplotlib.text import Text
        font_path = _font_for_text(display_text)
        font_manager.fontManager.addfont(str(font_path))
        selected_font = font_manager.FontProperties(fname=str(font_path))
        palette = ["#16766d", "#d88e45", "#68819d", "#9b7b9f", "#8d9463", "#b36565"]
        with plt.rc_context({"svg.fonttype": "path", "font.family": selected_font.get_name(), "font.size": 8,
                             "svg.hashsalt": "figure-workbench-chart-v1", "text.usetex": False, "text.parse_math": False,
                             "axes.spines.top": False, "axes.spines.right": False,
                             "axes.linewidth": .65, "xtick.major.width": .65, "ytick.major.width": .65,
                             "axes.labelsize": 8, "xtick.labelsize": 7, "ytick.labelsize": 7}):
            figure, axes = plt.subplots(figsize=(float(width_mm) / 25.4, float(height_mm) / 25.4), layout="constrained")
            for index, (_, observations) in enumerate(groups):
                color = palette[index % len(palette)]
                mean = float(np.mean(observations))
                standard_deviation = float(np.std(observations, ddof=1)) if observations.size > 1 else None
                if chart_type == "bar":
                    axes.bar(index, mean, width=.58, color=color, alpha=.22, edgecolor=color, linewidth=.9)
                if chart_type == "box" and observations.size > 1:
                    axes.boxplot([observations], positions=[index], widths=.55, showfliers=False, patch_artist=True,
                                 boxprops={"facecolor": color, "alpha": .2, "edgecolor": color},
                                 medianprops={"color": color}, whiskerprops={"color": color}, capprops={"color": color})
                # Deterministic horizontal separation; it never changes measurement values.
                offsets = (np.arange(observations.size) % 9 - 4) * .028 if observations.size > 1 else np.zeros(1)
                axes.scatter(index + offsets, observations, s=13, color=color, edgecolors="white", linewidths=.35, zorder=3)
                if chart_type != "box":
                    axes.plot([index - .16, index + .16], [mean, mean], color=color, linewidth=1.2, zorder=4)
                    if standard_deviation is not None:
                        axes.errorbar(index, mean, yerr=standard_deviation, color=color, linewidth=.8, capsize=3, zorder=4)
            axes.set_xticks(range(len(groups)), labels)
            axes.set_ylabel(y_label)
            axes.margins(x=.16, y=.16)
            figure.text(.02, .005, caption, fontsize=5, color="#666666")
            # Instantiate current ticks before binding an explicit font file.
            axes.get_xticklabels()
            axes.get_yticklabels()
            for item in figure.findobj(match=Text):
                font_size = item.get_fontsize()
                item.set_fontproperties(selected_font)
                item.set_fontsize(font_size)
            buffer = BytesIO()
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings("error", message=r"Glyph .*missing from font")
                    figure.savefig(buffer, format="svg", metadata={"Date": None, "Creator": "Figure Workbench",
                                                                    "Description": description})
            except UserWarning as exc:
                raise ValueError("图表文字缺少字体字形，已停止输出以避免乱码。") from exc
            finally:
                plt.close(figure)
    svg = buffer.getvalue().decode("utf-8")
    svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg, flags=re.DOTALL)
    # SVG 1.1 xlink references are local; neither scripts nor external resources.
    return svg.encode("utf-8")
