"""Standalone CPU inference worker for the separately packaged Cellpose 3 runtime.

Only this process imports Torch/Cellpose. No GUI or Figure Workbench package
imports are required, allowing the NumPy-2.0 model environment to stay isolated
from the application's numerical environment. The worker never downloads a
model and accepts only pinned official model bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from time import perf_counter


CELLPOSE_VERSION = "3.1.1.3"
MODEL_SPECS = {
    "cyto3": {"filename": "cyto3", "bytes": 26566255,
              "sha256": "2dc3087a8abd7da46d1ab0ddd5824639933cc3ff63b382af3fa1939a392db93c",
              "url": "https://www.cellpose.org/models/cyto3", "training_diameter_px": 30.0},
    "nuclei": {"filename": "nucleitorch_0", "bytes": 26563614,
               "sha256": "89ca45e4a45048d5010d29621466b1274b91b3dcb9714dce9f9e90a9a8671303",
               "url": "https://www.cellpose.org/models/nucleitorch_0", "training_diameter_px": 17.0},
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def configure_cpu_threads(threads: int = 4) -> None:
    """Select the bundled, TBB-independent backend before importing Numba."""
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 16:
        raise ValueError("CPU thread count must be an integer between 1 and 16.")
    # An inherited TBB choice must not override the component's portable backend.
    os.environ["NUMBA_THREADING_LAYER"] = "workqueue"
    os.environ["NUMBA_NUM_THREADS"] = str(threads)
    os.environ["OMP_NUM_THREADS"] = str(threads)
    os.environ["MKL_NUM_THREADS"] = str(threads)


def runtime_probe(threads: int = 4) -> dict:
    configure_cpu_threads(threads)
    from importlib.metadata import version
    import numpy as np
    import torch
    from cellpose import models  # noqa: F401 - verify actual compiled dependency imports.

    if version("cellpose") != CELLPOSE_VERSION:
        raise ValueError(f"This component requires Cellpose {CELLPOSE_VERSION}; found {version('cellpose')}.")
    if tuple(int(part) for part in np.__version__.split(".")[:2]) >= (2, 1):
        raise ValueError("Cellpose 3 requires an isolated NumPy < 2.1 environment.")
    return {"available": True, "cellpose_version": version("cellpose"),
            "torch_version": version("torch"), "numpy_version": np.__version__,
            "python_version": sys.version.split()[0], "device": "cpu",
            "numba_threading_layer_requested": "workqueue", "cpu_threads": threads,
            "dependencies": {name: version(name) for name in
                             ("scipy", "numba", "llvmlite", "opencv-python-headless", "imagecodecs", "fastremap")}}


def infer(input_path: Path, output_path: Path, metadata_path: Path,
          model_directory: Path, request: dict) -> None:
    from math import isfinite

    allowed = {"model", "diameter_px", "min_area", "flow_threshold", "cellprob_threshold", "niter", "threads", "normalize"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("Unsupported Cellpose request fields.")
    model_name = str(request.get("model", "cyto3"))
    if model_name not in MODEL_SPECS:
        raise ValueError("Only the pinned official cyto3 and nuclei models are accepted.")
    spec = MODEL_SPECS[model_name]
    weights = model_directory / spec["filename"]
    if not weights.is_file() or weights.stat().st_size != spec["bytes"] or file_sha256(weights) != spec["sha256"]:
        raise ValueError("The official local model is missing or its SHA-256 does not match the packaged model.")
    diameter = float(request.get("diameter_px", 30.0))
    flow_threshold = float(request.get("flow_threshold", .4))
    cellprob_threshold = float(request.get("cellprob_threshold", 0))
    minimum = int(request.get("min_area", 20))
    iterations = int(request.get("niter", 200))
    threads = int(request.get("threads", 4))
    normalize = request.get("normalize", True)
    if (not all(isfinite(value) for value in (diameter, flow_threshold, cellprob_threshold))
            or not 2 <= diameter <= 2000 or not 0 <= flow_threshold <= 10
            or not -20 <= cellprob_threshold <= 20 or not 1 <= minimum <= 16_000_000
            or not 1 <= iterations <= 1000 or not 1 <= threads <= 16 or not isinstance(normalize, bool)):
        raise ValueError("Cellpose parameters are outside supported bounds.")
    configure_cpu_threads(threads)
    import numpy as np
    # NPY is internal numeric transport, never a pickle/project loader.
    image = np.load(input_path, allow_pickle=False)
    if image.ndim != 2 or image.dtype.kind not in "buif" or image.size > 16_000_000 or not image.size:
        raise ValueError("Cellpose input must be a finite two-dimensional numeric ROI of at most 16 million pixels.")
    if not np.isfinite(image).all():
        raise ValueError("Cellpose input contains non-finite pixels.")
    if image.size * (spec["training_diameter_px"] / diameter) ** 2 > 32_000_000:
        raise ValueError("Requested diameter would enlarge the inference plane beyond the CPU memory limit; select a smaller ROI.")
    os.environ["CELLPOSE_LOCAL_MODELS_PATH"] = str(model_directory)
    status = runtime_probe(threads)
    import torch
    from cellpose import models

    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    started = perf_counter()
    model = models.CellposeModel(device=torch.device("cpu"), gpu=False, pretrained_model=str(weights))
    normalization = {"normalize": normalize, "percentile": [1.0, 99.0]}
    with torch.inference_mode():
        result = model.eval(image, diameter=diameter, channels=[0, 0], normalize=normalization,
                            flow_threshold=flow_threshold, cellprob_threshold=cellprob_threshold,
                            min_size=minimum, batch_size=8, niter=iterations, do_3D=False)
    labels = np.asarray(result[0])
    if labels.shape != image.shape or labels.dtype.kind not in "ui" or np.any(labels < 0):
        raise ValueError("Model output is not a native-size nonnegative instance label image.")
    if labels.max(initial=0) > image.size:
        raise ValueError("Model returned invalid instance IDs.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(output_path, labels.astype(np.int32), allow_pickle=False)
    import numba
    active_numba_threads = numba.get_num_threads()
    try:
        active_threading_layer = numba.threading_layer()
    except ValueError:
        active_threading_layer = "not_initialized"
    metadata = {**status, "model": model_name, "weights_filename": spec["filename"],
                "weights_sha256": spec["sha256"], "weights_source_url": spec["url"],
                "parameters": {**request, "normalization": normalization, "model_channels": [0, 0]},
                "native_output_shape": list(labels.shape), "inference_ms": (perf_counter() - started) * 1000,
                "numba_threading_layer": active_threading_layer,
                "numba_num_threads": active_numba_threads,
                "network_download_performed": False}
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Figure Workbench isolated Cellpose 3 CPU worker")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    try:
        if args.probe:
            if args.models_dir:
                os.environ["CELLPOSE_LOCAL_MODELS_PATH"] = str(args.models_dir)
            print(json.dumps(runtime_probe(), ensure_ascii=False, allow_nan=False))
        else:
            if any(value is None for value in (args.models_dir, args.input, args.output, args.metadata, args.request)):
                parser.error("inference requires --models-dir, --input, --output, --metadata and --request")
            if args.request.stat().st_size > 65536:
                raise ValueError("Cellpose request is too large.")
            request = json.loads(args.request.read_text(encoding="utf-8"))
            infer(args.input, args.output, args.metadata, args.models_dir, request)
        return 0
    except Exception as exc:
        print(json.dumps({"available": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
