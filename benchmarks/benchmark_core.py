"""Reproducible CPU microbenchmarks; these are not accuracy benchmarks.

The baseline computes the SAME float64 border-median net integral, but converts
the whole frame separately for every ROI. The improved path converts only each
ROI and also returns metadata/quality checks. This is an explicitly constructed
naive implementation, not a timing claim about either upstream application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter

import numpy as np
import scipy
import skimage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workbench.analysis import _edge_background, analyze_wb


def timed(operation, repeats: int):
    timings = []
    value = None
    for _ in range(repeats):
        start = perf_counter()
        value = operation()
        timings.append((perf_counter() - start) * 1000)
    return value, {"repeat": repeats, "samples_ms": timings, "median_ms": float(np.median(timings)),
                   "p95_ms": float(np.percentile(timings, 95)), "min_ms": float(np.min(timings))}


def build_fixture():
    # Fixed integer arrays: byte-for-byte reproducible, no claimed experimental data.
    image = np.full((4096, 4096), 12000, dtype=np.uint16)
    rois = []
    for index in range(24):
        x = 100 + (index % 6) * 600
        y = 100 + (index // 6) * 800
        image[y + 15:y + 33, x + 24:x + 104] = 13001 + index
        rois.append({"id": f"r{index}", "asset_id": "raw", "x": x, "y": y, "width": 128, "height": 48,
                     "sample_id": f"sample{index}", "group": "Synthetic benchmark", "biological_replicate": f"bio{index}",
                     "technical_replicate": "1", "role": "target", "protein": "Benchmark"})
    return image, rois


def naive_full_frame_cast(image, rois):
    results = []
    for roi in rois:
        entire_float_frame = image.astype(np.float64)
        pixels = entire_float_frame[roi["y"]:roi["y"] + roi["height"], roi["x"]:roi["x"] + roi["width"]]
        background, _ = _edge_background(pixels, 0.1)
        results.append(float(np.sum(pixels - background, dtype=np.float64)))
    return results


def cpu_name():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def benchmark(repeats: int = 5):
    image, rois = build_fixture()
    original_digest = hashlib.sha256(image.tobytes()).hexdigest()
    params = {"rois": rois, "polarity": "bright", "background": "border"}
    optimized_call = lambda: analyze_wb({"raw": image}, params)
    _, cold = timed(optimized_call, 1)
    result, optimized = timed(optimized_call, repeats)
    baseline_result, baseline = timed(lambda: naive_full_frame_cast(image, rois), repeats)
    exact_match = baseline_result == [row["background_subtracted"] for row in result["rows"]]
    if not exact_match:
        raise AssertionError("Benchmark implementations must have identical numerical semantics.")
    rolling_image = np.full((256, 384), 2000, dtype=np.uint16)
    rolling_image[90:110, 100:230] = 8001
    rolling_roi = {**rois[0], "x": 0, "y": 0, "width": 384, "height": 256}
    rolling_result, rolling = timed(lambda: analyze_wb({"raw": rolling_image}, {
        "rois": [rolling_roi], "polarity": "bright", "background": "rolling_ball", "radius": 12,
    }), 1)
    report = {
        "benchmark_version": 1, "synthetic_data": True,
        "comparison_scope": "same float64 border-median net integral; naive full-frame-per-ROI conversion vs ROI-first conversion",
        "not_an_upstream_application_benchmark": True,
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "numpy": np.__version__, "scipy": scipy.__version__, "skimage": skimage.__version__,
                        "cpu": cpu_name(), "logical_cpu_count": os.cpu_count(),
                        "cpu_affinity_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None},
        "fixture": {"shape": list(image.shape), "dtype": str(image.dtype), "roi_count": len(rois),
                    "roi_shape": [48, 128], "sha256": original_digest},
        "optimized_cold": cold, "optimized_warm": optimized, "naive_warm": baseline,
        "median_speedup_for_this_case": baseline["median_ms"] / optimized["median_ms"],
        "identical_numerical_results": exact_match,
        "source_dtype_retained": all(row["source_dtype"] == "uint16" for row in result["rows"]),
        "source_bytes_unchanged": hashlib.sha256(image.tobytes()).hexdigest() == original_digest,
        "conversion_work": {"naive_float64_values_converted": int(image.size) * len(rois),
                            "optimized_float64_values_converted": sum(roi["width"] * roi["height"] for roi in rois)},
        "rolling_ball_case": {"shape": [256, 384], "radius_px": 12, **rolling,
                              "net_integral": rolling_result["rows"][0]["background_subtracted"],
                              "finite_result": bool(np.isfinite(rolling_result["rows"][0]["background_subtracted"]))},
        "limitations": ["Timings reflect this machine and synthetic workload only.",
                        "No inference about real-image detection accuracy or industry-wide speed is warranted.",
                        "The naive baseline is intentionally redundant and is not a measured upstream app."],
    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "benchmarks" / "results_core.json")
    arguments = parser.parse_args()
    if not 1 <= arguments.repeat <= 50:
        parser.error("--repeat must be between 1 and 50")
    result = benchmark(arguments.repeat)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
