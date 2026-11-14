"""Isolated, cancellable CPU Cellpose inference and original-pixel measurement.

Torch and Cellpose are optional, separately packaged dependencies. They are
never imported into the application process. Official model hashes are pinned;
project data cannot select an executable or an arbitrary model/pickle path.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from time import monotonic, perf_counter
from typing import Any

import numpy as np
from scipy import ndimage

from .cellpose_worker import CELLPOSE_VERSION, MODEL_SPECS, file_sha256
from .runtime_paths import bundle_root, default_data_dir
from .microscopy_measurements import _finite_number, _integer, analysis_region, resolve_channels


_STATUS_LOCK = threading.RLock()
_PROBE_CACHE: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()
_HASH_CACHE: OrderedDict[tuple[Any, ...], str] = OrderedDict()


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    interpreter: Path | None
    model_directory: Path
    worker: Path


def _runtime_layout() -> RuntimeLayout:
    root = bundle_root()
    override = os.environ.get("FIGURE_WORKBENCH_CELLPOSE_HOME")
    homes = [Path(override).expanduser().resolve()] if override else [root.parent / "cellpose", root / "cellpose"]
    selected = next((home for home in homes if home.is_dir()), homes[0])
    python_override = os.environ.get("FIGURE_WORKBENCH_CELLPOSE_PYTHON")
    if python_override:
        # Resolving a venv's python symlink would bypass its pyvenv.cfg and
        # silently launch the base interpreter without the model dependencies.
        interpreter = Path(python_override).expanduser().absolute()
    else:
        candidates = [selected / "runtime" / "python.exe", selected / "runtime" / "bin" / "python",
                      selected / "venv" / "bin" / "python", selected / "venv" / "Scripts" / "python.exe"]
        interpreter = next((candidate for candidate in candidates if candidate.is_file()), None)
        if interpreter is None and importlib.util.find_spec("cellpose") is not None and importlib.util.find_spec("torch") is not None:
            interpreter = Path(sys.executable)
    model_override = os.environ.get("FIGURE_WORKBENCH_CELLPOSE_MODELS")
    models = Path(model_override).expanduser().resolve() if model_override else selected / "models"
    return RuntimeLayout(interpreter, models, Path(__file__).with_name("cellpose_worker.py"))


def _child_environment(layout: RuntimeLayout) -> dict[str, str]:
    environment = os.environ.copy()
    # The application and inference component intentionally have different NumPy
    # ABIs. An inherited application PYTHONPATH must not cross that boundary.
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    # Both launch commands also pass -B: an isolated embedded interpreter can
    # ignore PYTHON* variables, while component files must remain unchanged.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["CELLPOSE_LOCAL_MODELS_PATH"] = str(layout.model_directory)
    cache = default_data_dir() / "cache" / "cellpose"
    cache.mkdir(parents=True, exist_ok=True)
    environment["NUMBA_CACHE_DIR"] = str(cache / "numba")
    return environment


def _file_identity(path: Path | None) -> tuple[Any, ...]:
    if path is None:
        return (None,)
    try:
        stat = path.stat()
        return (str(path.absolute()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return (str(path), None)


def _checked_hash(path: Path) -> str:
    key = _file_identity(path)
    with _STATUS_LOCK:
        if key in _HASH_CACHE:
            _HASH_CACHE.move_to_end(key)
            return _HASH_CACHE[key]
    digest = file_sha256(path)
    with _STATUS_LOCK:
        _HASH_CACHE[key] = digest
        while len(_HASH_CACHE) > 8:
            _HASH_CACHE.popitem(last=False)
    return digest


def _probe(layout: RuntimeLayout, refresh: bool) -> dict[str, Any]:
    if layout.interpreter is None or not layout.interpreter.is_file():
        return {"available": False, "status": "runtime_missing", "reason": "缺少独立 Cellpose CPU 运行环境。"}
    key = (*_file_identity(layout.interpreter), *_file_identity(layout.worker), str(layout.model_directory))
    with _STATUS_LOCK:
        cached = _PROBE_CACHE.get(key)
        if not refresh and cached and monotonic() - cached[0] < 300:
            return dict(cached[1])
    try:
        completed = subprocess.run([str(layout.interpreter), "-B", str(layout.worker), "--probe", "--models-dir", str(layout.model_directory)],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=45,
                                   env=_child_environment(layout),
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        lines = completed.stdout.decode("utf-8", errors="replace").strip().splitlines()
        result = json.loads(lines[-1]) if lines else {}
        if completed.returncode or not result.get("available"):
            detail = completed.stderr.decode("utf-8", errors="replace")[-1800:]
            result = {"available": False, "status": "runtime_incompatible", "reason": "Cellpose CPU 组件未通过实际依赖加载检查。", "detail": detail}
        else:
            result["status"] = "ready"
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        result = {"available": False, "status": "runtime_probe_failed", "reason": "Cellpose CPU 组件探测失败。", "detail": str(exc)[:1200]}
    with _STATUS_LOCK:
        _PROBE_CACHE[key] = (monotonic(), dict(result))
        while len(_PROBE_CACHE) > 4:
            _PROBE_CACHE.popitem(last=False)
    return result


def cellpose_status(*, probe: bool = True, refresh: bool = False) -> dict[str, Any]:
    """Report actual component/model readiness; no downloads or fake fallback.

    Dependency probing occurs in the isolated worker and is cached for five
    minutes. Missing components return immediately. Model hashes are cached by
    file identity and checked again in the worker before every inference.
    """
    layout = _runtime_layout()
    models = []
    for model_id, spec in MODEL_SPECS.items():
        path = layout.model_directory / spec["filename"]
        state = "missing"
        actual_hash = None
        try:
            if path.is_file():
                actual_hash = _checked_hash(path)
                state = "ready" if path.stat().st_size == spec["bytes"] and actual_hash == spec["sha256"] else "hash_mismatch"
        except OSError:
            state = "unreadable"
        models.append({"id": model_id, "name": "Cellpose cyto3" if model_id == "cyto3" else "Cellpose nuclei",
                       "filename": spec["filename"], "status": state, "weights_available": state == "ready",
                       "sha256": spec["sha256"], "actual_sha256": actual_hash, "source_url": spec["url"]})
    if probe:
        runtime = _probe(layout, refresh)
    else:
        key = (*_file_identity(layout.interpreter), *_file_identity(layout.worker), str(layout.model_directory))
        with _STATUS_LOCK:
            cached = _PROBE_CACHE.get(key)
            runtime = dict(cached[1]) if cached and monotonic() - cached[0] < 300 else None
        if runtime is None:
            runtime = {"available": False, "status": "not_probed", "reason": "尚未实际检查推理运行环境。"}
    for model in models:
        model["available"] = bool(model["weights_available"] and runtime.get("available"))
    available = any(model["available"] for model in models)
    reason = None if available else runtime.get("reason") or "Cellpose 离线模型缺失或校验不符，请完整解压配套 CPU 模型组件。"
    return {"available": available, "models": models, "runtime": runtime, "device": "cpu",
            "required_cellpose_version": CELLPOSE_VERSION, "reason": reason,
            "has_component": bool(layout.interpreter and layout.interpreter.is_file() and layout.worker.is_file()),
            "weights_available": any(model["weights_available"] for model in models),
            "probe_required": runtime.get("status") == "not_probed",
            "component_layout": "cellpose/runtime/python.exe + cellpose/models/", "automatic_downloads": False}


class CellposeCancelledError(RuntimeError):
    pass


def _run_worker(command: list[str], layout: RuntimeLayout, directory: Path, timeout: float,
                cancel_event: Any = None) -> str:
    if cancel_event is not None and cancel_event.is_set():
        raise CellposeCancelledError("Cellpose 分析已取消。")
    log_path = directory / "worker.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   env=_child_environment(layout),
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = monotonic() + timeout
        stopped_reason = None
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    stopped_reason = "cancelled"
                    break
                if monotonic() >= deadline:
                    stopped_reason = "timeout"
                    break
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if stopped_reason == "cancelled":
            raise CellposeCancelledError("Cellpose 分析已取消，推理进程已终止。")
        if stopped_reason == "timeout":
            raise ValueError("Cellpose CPU 分析超时且进程已终止；请选择较小 ROI 或调整直径。")
    detail = log_path.read_bytes()[-3000:].decode("utf-8", errors="replace")
    if process.returncode:
        raise ValueError("Cellpose 真实推理失败，未用其他算法替代。" + ("\n" + detail if detail else ""))
    return detail


def run_cellpose_labels(image: np.ndarray, params: Mapping[str, Any], *, cancel_event: Any = None) -> tuple[np.ndarray, dict[str, Any]]:
    """Infer native instance labels, using only the pinned official local model."""
    if cancel_event is not None and cancel_event.is_set():
        raise CellposeCancelledError("Cellpose 分析已取消。")
    model_id = str(params.get("model", "cyto3"))
    if model_id not in MODEL_SPECS:
        raise ValueError("Cellpose model 仅支持 cyto3 或 nuclei。")
    diameter = _finite_number(params.get("diameter_px", 17 if model_id == "nuclei" else 30), "diameter_px")
    flow = _finite_number(params.get("flow_threshold", .4), "flow_threshold")
    probability = _finite_number(params.get("cellprob_threshold", 0), "cellprob_threshold")
    timeout = _finite_number(params.get("timeout_s", 300), "timeout_s")
    if not 2 <= diameter <= 2000 or not 0 <= flow <= 10 or not -20 <= probability <= 20 or not 1 <= timeout <= 1800:
        raise ValueError("Cellpose 直径/阈值/运行时限超出支持范围。")
    normalize = params.get("normalize", True)
    if not isinstance(normalize, bool):
        raise ValueError("Cellpose normalize 必须为布尔值。")
    request = {"model": model_id, "diameter_px": diameter, "flow_threshold": flow,
               "cellprob_threshold": probability, "min_area": _integer(params.get("min_area", 20), "min_area", 1, 16_000_000),
               "niter": _integer(params.get("niter", 200), "niter", 1, 1000),
               "threads": _integer(params.get("threads", 4), "threads", 1, 16), "normalize": normalize}
    array = np.asarray(image)
    if array.ndim != 2 or array.dtype.kind not in "buif" or array.size > 16_000_000 or not array.size or not np.isfinite(array).all():
        raise ValueError("Cellpose 输入须为不超过 1600 万像素的有限数值二维 ROI。")
    spec = MODEL_SPECS[model_id]
    if array.size * (spec["training_diameter_px"] / diameter) ** 2 > 32_000_000:
        raise ValueError("所选直径会让内部重采样超出 CPU 内存上限，请缩小 ROI。")
    # The inference worker validates/imports the real dependencies itself. Do
    # not precede it with a separate blocking 45-second dependency probe: the
    # entire import/model-initialization phase must remain cancellable.
    status = cellpose_status(probe=False)
    model = next(item for item in status["models"] if item["id"] == model_id)
    if not status["has_component"] or not model["weights_available"]:
        raise ValueError("Cellpose 离线模型组件不可用：" + (status.get("reason") or model["status"]))
    layout = _runtime_layout()
    with tempfile.TemporaryDirectory(prefix="figure-cellpose-") as folder:
        directory = Path(folder)
        source, output, metadata_path, request_path = [directory / name for name in ("input.npy", "labels.npy", "result.json", "request.json")]
        np.save(source, array, allow_pickle=False)
        request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        command = [str(layout.interpreter), "-B", str(layout.worker), "--models-dir", str(layout.model_directory),
                   "--input", str(source), "--output", str(output), "--metadata", str(metadata_path), "--request", str(request_path)]
        _run_worker(command, layout, directory, timeout, cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise CellposeCancelledError("Cellpose 分析已取消。")
        if not output.is_file() or output.stat().st_size > array.size * 4 + 65536 or not metadata_path.is_file() or metadata_path.stat().st_size > 65536:
            raise ValueError("Cellpose 子进程没有返回有效、有限大小的标签和模型记录。")
        labels = np.load(output, allow_pickle=False)
        if labels.shape != array.shape or labels.dtype.kind not in "ui" or labels.min(initial=0) < 0 or labels.max(initial=0) > array.size:
            raise ValueError("Cellpose 标签尺寸或实例编号无效。")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("model") != model_id or metadata.get("weights_sha256") != spec["sha256"]:
            raise ValueError("Cellpose 返回的模型身份与请求不一致。")
        return labels.astype(np.int32, copy=True), metadata


def analyze_cellpose(image: np.ndarray | Mapping[str, np.ndarray], params: dict[str, Any],
                     *, cancel_event: Any = None) -> dict[str, Any]:
    from .microscopy import SpatialCalibration, _region_objects, _summary

    started = perf_counter()
    channels = resolve_channels(image, params, default_all=False)
    if len(channels) != 1:
        raise ValueError("当前 Cellpose 工作流需要明确选择一个目标通道；不会把多个通道自动平均成灰度。")
    channel = channels[0]
    region = analysis_region(params.get("roi"), channel.values.shape)
    raw = np.asarray(channel.values[region.bounds.slices], dtype=np.float64)
    finite = region.mask & np.isfinite(raw)
    if not np.any(finite):
        raise ValueError("Cellpose ROI 内没有有限原始像素。")
    prepared = channel.values[region.bounds.slices].copy()
    outside = ~region.mask | ~np.isfinite(raw)
    fill_value = float(np.median(raw[finite]))
    if np.any(outside):
        prepared[outside] = fill_value
    labels, model_record = run_cellpose_labels(prepared, params, cancel_event=cancel_event)
    labels[~finite] = 0
    sizes = np.bincount(labels.ravel())
    minimum = _integer(params.get("min_area", 20), "min_area", 1, 16_000_000)
    maximum = _integer(params["max_area"], "max_area", minimum, 16_000_000) if params.get("max_area") is not None else None
    allowed = sizes >= minimum
    allowed[0] = False
    if maximum is not None:
        allowed &= sizes <= maximum
    if params.get("exclude_border", False):
        edge = region.mask & ~ndimage.binary_erosion(region.mask, border_value=0)
        allowed[np.unique(labels[edge])] = False
    mapping = np.zeros(len(allowed), dtype=np.int32)
    mapping[allowed] = np.arange(1, np.count_nonzero(allowed) + 1)
    original_ids = np.flatnonzero(allowed)
    labels = mapping[labels]
    calibration = SpatialCalibration.parse(params)
    objects, count, warnings = _region_objects(labels, raw, region.bounds, calibration,
                                              bool(params.get("include_contours", True)),
                                              _integer(params.get("max_objects", 10000), "max_objects", 1, 100000), region.mask)
    for item in objects:
        item["model_instance_id"] = int(original_ids[item["id"] - 1])
        item["channel_id"] = channel.channel_id
        item["object_type"] = "cellpose_instance_candidate"
    if not calibration:
        warnings.append("未提供像素标定，仅报告原始 px / px²。")
    if np.any(outside & region.mask):
        warnings.append("ROI 内非有限像素仅在模型输入中以中位数填充，已从最终标签和强度测量排除。")
    warnings.append("Cellpose 返回模型实例候选，请对照原图复核；模型运行成功不代表对当前实验图像的准确率已验证。")
    foreground_area = int(np.count_nonzero(labels))
    summary = {**_summary(raw[finite]), "foreground_area_px": foreground_area,
               "foreground_fraction": foreground_area / int(np.count_nonzero(finite)),
               "object_type": "cellpose_instance_candidate", "count": count}
    if calibration:
        summary["foreground_area_um2"] = foreground_area * calibration.pixel_area_um2
    method = {"name": "cellpose", "measurement_basis": "original_pixels", "display_lut_used": False,
              "source_channel": channel.metadata(), "roi": region.geometry, "model_record": model_record,
              "input_outside_roi": "median_fill_for_detection_only", "fill_value": fill_value,
              "post_filter": {"min_area_px": minimum, "max_area_px": maximum,
                              "exclude_border": bool(params.get("exclude_border", False))},
              "instance_geometry": "native_source_pixel_grid"}
    if calibration:
        method["calibration"] = {"um_per_px_x": calibration.x_um_per_px, "um_per_px_y": calibration.y_um_per_px}
    result = {"kind": "microscopy", "count": count, "objects": objects, "rows": objects,
              "summary": summary, "warnings": warnings, "method": method,
              "elapsed_ms": (perf_counter() - started) * 1000}
    from .instances import attach_instance_mask
    return attach_instance_mask(result, labels, origin=(region.bounds.x, region.bounds.y),
                                source_shape=channel.values.shape)
