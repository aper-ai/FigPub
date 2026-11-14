"""Reproducible v0.3 workflow timings on bounded, synthetic native-pixel inputs.

Run from the project root:
    .venv/bin/python benchmarks/benchmark_workflows.py --repeats 5

Each case has one excluded warmup, then 3--5 measured calls. Fixture creation,
verification, hashing, JSON serialization, file/network I/O and browser rendering
are excluded. The workflow's own validation and result construction are included.
No upstream application is timed; these numbers do not establish comparative
speed, biological accuracy, Windows performance or concurrent server throughput.

FW_BENCH_THREADS controls process-local BLAS/OpenMP thread requests (default 1).
It does not reserve CPUs or prove every native library obeys that request.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter
from typing import Any, Callable


REQUESTED_THREADS = int(os.environ.get("FW_BENCH_THREADS", "1"))
if not 1 <= REQUESTED_THREADS <= 64:
    raise ValueError("FW_BENCH_THREADS must be an integer from 1 through 64.")
THREAD_VARIABLES = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
for variable in THREAD_VARIABLES:
    os.environ[variable] = str(REQUESTED_THREADS)

import numpy as np
import scipy
import skimage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workbench.fibers import analyze_fibers, recompute_fiber_result
from workbench.histology import DEFAULT_VECTORS, analyze_histology
from workbench.instances import decode_instance_mask, encode_instance_mask, review_instances
from workbench.wb_workflow import review_wb_exposures


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, allow_nan=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def array_digest(value: np.ndarray) -> str:
    return hashlib.sha256(memoryview(np.ascontiguousarray(value)).cast("B")).hexdigest()


def stable_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: stable_result(item) for key, item in value.items() if key != "elapsed_ms"}
    if isinstance(value, list):
        return [stable_result(item) for item in value]
    return value


def load_snapshot() -> dict[str, Any]:
    try:
        averages = [round(value, 5) for value in os.getloadavg()]
    except OSError:
        averages = None
    return {"utc": datetime.now(timezone.utc).isoformat(), "load_average_1_5_15_min": averages}


def read_optional(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def environment() -> dict[str, Any]:
    cpu = platform.processor()
    for line in (read_optional("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            cpu = line.split(":", 1)[1].strip()
            break
    return {"platform": platform.platform(), "python": platform.python_version(),
            "numpy": np.__version__, "scipy": scipy.__version__, "scikit_image": skimage.__version__,
            "cpu_model": cpu or "unknown", "logical_cpu_count": os.cpu_count(),
            "cpu_affinity_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "process_local_thread_requests": {key: os.environ[key] for key in THREAD_VARIABLES},
            "native_thread_pool_counts_independently_verified": False,
            "cgroup_cpu_max": read_optional("/sys/fs/cgroup/cpu.max"),
            "cgroup_cpuset_effective": read_optional("/sys/fs/cgroup/cpuset.cpus.effective"),
            "shared_host_exclusive_access": False,
            "concurrent_other_workloads": "Unknown: this process cannot enumerate other agents' isolated processes.",
            "load_average_scope": "Operating-system load average; it is not isolated CPU utilization or a count of competing benchmark jobs."}


@dataclass
class Case:
    name: str
    title: str
    operation: Callable[[], dict]
    verify: Callable[[dict], dict]
    arrays: dict[str, np.ndarray]
    protected: dict
    input_details: dict


def wb_case() -> Case:
    images, arrays, rois = {}, {}, []
    shape = (512, 768)
    exposures = (("weak", 2, .1), ("usable", 1600, 1.), ("clipped", 7000, 4.))
    for exposure_id, contrast, seconds in exposures:
        for blot in range(4):
            asset = f"synthetic-{blot}-{exposure_id}"
            raw = np.full(shape, 12000, np.uint16)
            raw[::2, ::2] += 2
            raw[1::2, 1::2] -= 2
            for lane in range(6):
                sample = blot * 6 + lane
                x = 35 + lane * 120
                for role, protein, y in (("target", "Target", 110), ("control", "ACTB", 310)):
                    signal = contrast if role == "target" or exposure_id == "weak" else int(contrast * 1.1)
                    raw[y + 12:y + 24, x + 15:x + 65] = 12000 - signal
                    rois.append({"id": f"{asset}-{lane}-{role}", "asset_id": asset,
                        "source": {"asset_id": asset, "series": 0, "frame": 0, "channel": 0},
                        "x": x, "y": y, "width": 80, "height": 36, "role": role, "protein": protein,
                        "experiment_id": "synthetic-experiment", "exposure_series_id": "same-cohort",
                        "sample_id": f"sample-{sample}", "group": "A" if sample < 12 else "B",
                        "biological_replicate": f"synthetic-bio-{sample}", "technical_replicate": "1",
                        "exposure_id": exposure_id, "exposure_seconds": seconds})
            if exposure_id == "clipped" and blot == 3:
                raw[123, 51] = 0
            images[(asset, 0, 0, 0)] = raw
            arrays[asset] = raw
    params = {"rois": rois, "polarity": "dark", "adc_bits": 16}

    def verify(result):
        require(len(result["rows"]) == 144, "All WB ROI measurements must be returned.")
        require(len(result["groups"]) == 1, "All samples belong to one declared exposure cohort.")
        group = result["groups"][0]
        require(group["recommended_pair_id"] == "usable", "Only the complete, usable exposure may be recommended.")
        included = [item for item in result["roi_decisions"] if item["included"]]
        require(len(included) == 48, "Exactly 24 target/control pairs must be selected.")
        candidates = {item["pair_id"]: item for item in group["candidates"]}
        require(not candidates["weak"]["eligible"] and not candidates["clipped"]["eligible"], "Weak/clipped candidates must fail.")
        require(any("saturated_pixels" in item["reasons"] for item in result["rows"]), "The original clipped pixel must be detected.")
        require(any("low_signal_to_noise" in item["reasons"] for item in result["rows"]), "Low SNR must be reported.")
        require(not result["method"]["automatic_selection_applied"], "Quality review must remain read-only.")
        return {"measured_rois": 144, "logical_measurements": group["logical_measurements"],
                "recommended_pair_id": group["recommended_pair_id"], "included_rois": len(included),
                "weak_and_clipped_rejected": True, "selection_not_applied": True}

    return Case("wb_exposure_quality", "WB: 12 original planes, 144 ROIs, 3 exposures, one complete cohort",
        lambda: review_wb_exposures(images, params), verify, arrays, params,
        {"source_planes": 12, "plane_shape_yx": list(shape), "roi_shape_yx": [36, 80],
         "roi_count": 144, "roi_pixels_measured": 144 * 36 * 80, "synthetic_biological_ids": 24,
         "groups": 2, "exposures": 3, "roles": ["target", "control"]})


def histology_cases() -> list[Case]:
    rng = np.random.default_rng(20261111)
    size, border = 1024, 16
    h = rng.uniform(.4, 1., (size, size))
    d = np.broadcast_to(np.repeat(np.array([.03, .3, .65, 1.1]), size // 4), h.shape).copy()
    # Explicit pure-stain strips give Macenko two identifiable directions.
    d[:, border:border + 40] = 0
    h[:, -border - 40:-border] = 0
    d[:, -border - 40:-border] = rng.choice([.3, .65, 1.1], size=(size, 40))
    tissue = np.zeros(h.shape, bool)
    tissue[border:-border, border:-border] = True
    h[~tissue] = 0
    d[~tissue] = 0
    rgb = np.rint(255 * np.exp(-(h[..., None] * DEFAULT_VECTORS[0] + d[..., None] * DEFAULT_VECTORS[1]))).astype(np.uint8)
    thresholds = [.15, .5, .85]
    expected_counts = np.bincount(np.searchsorted(thresholds, d[tissue], side="right"), minlength=4).tolist()
    tissue_count = int(tissue.sum())
    cases = []
    for stain in ("fixed", "macenko"):
        params = {"method": "ihc_advanced", "channel_axis": -1, "stain_method": stain,
                  "grading_method": "manual", "grade_thresholds": thresholds,
                  "rgb_white_level": 255, "um_per_px": .5}

        def verify(result, stain=stain):
            counts = [item["area_px"] for item in result["rows"]]
            require(counts == expected_counts, "Four synthetic OD populations must retain their expected grades after RGB quantization.")
            require(result["summary"]["tissue_area_px"] == tissue_count, "White background must be excluded.")
            require(sum(counts) == tissue_count, "Every tissue pixel must have exactly one grade.")
            require(not result["method"]["display_lut_used"], "IHC must use original RGB values.")
            estimation = result["method"]["stain_estimation"]
            agreement = np.sum(np.asarray(result["method"]["stain_vectors"]) * DEFAULT_VECTORS, axis=1)
            require(bool(np.all(agreement > .995)), "Estimated stain directions must match the known synthetic directions.")
            if stain == "macenko":
                require(estimation["algorithm"] == "macenko_angular_extremes", "Macenko must run without fixed-vector fallback.")
                require(estimation["sample_count"] <= 100000 and estimation["sampled"], "Macenko estimation must remain bounded.")
            return {"tissue_pixels": tissue_count, "grade_counts": counts,
                    "known_grade_counts_exact": True, "stain_estimation": estimation,
                    "stain_reference_cosines": [float(value) for value in agreement],
                    "pixel_weighted_score": result["summary"]["pixel_weighted_score"]}

        cases.append(Case(f"ihc_{stain}_manual_grades", f"IHC: 1024² native uint8 RGB, {stain} H/DAB, manual OD grades",
            lambda params=params: analyze_histology(rgb, params), verify, {"original_rgb": rgb}, params,
            {"shape_yxc": list(rgb.shape), "native_dtype": str(rgb.dtype), "tissue_pixels": tissue_count,
             "expected_grade_counts": expected_counts, "stain_method": stain, "grading_method": "manual",
             "grade_thresholds_natural_log_od": thresholds, "synthetic_pure_stain_strips": True}))
    return cases


def fiber_cases() -> list[Case]:
    size = 1024
    labels = np.zeros((size, size), np.uint16)
    nuclear = np.zeros_like(labels)
    yy, xx = np.mgrid[:size, :size]
    nucleus_id = 1
    for row in range(8):
        for column in range(8):
            fiber_id = row * 8 + column + 1
            cx, cy = 64 + column * 128, 64 + row * 128
            labels[((xx - cx) / 40) ** 2 + ((yy - cy) / 32) ** 2 <= 1] = fiber_id
            for offset in (-8, 8, 35):
                nuclear[cy - 1:cy + 2, cx + offset - 1:cx + offset + 2] = nucleus_id
                nucleus_id += 1
    for index in range(16):
        nuclear[8:11, 18 + index * 64:21 + index * 64] = nucleus_id
        nucleus_id += 1
    raw = np.where(labels > 0, 5000 + labels.astype(np.uint32) * 5, 100).astype(np.uint16)
    marker = np.where((labels > 0) & (labels % 2 == 0), 8000, 800).astype(np.uint16)
    images = {"f": raw, "l": labels, "n": nuclear, "m": marker}
    params = {"method": "fiber_cross_section", "segmentation": "labels", "fiber_channel_id": "f",
              "label_channel_id": "l", "nucleus_label_channel_id": "n", "nucleus_min_area": 1,
              "min_area": 5, "um_per_px_x": .5, "um_per_px_y": .75, "peripheral_fraction": .2,
              "fiber_markers": [{"channel_id": "m", "name": "SyntheticMarker", "threshold": 4000}],
              "channels": [{"id": key, "name": key.upper(),
                            "source": {"asset_id": f"synthetic-{key}", "series": 0, "frame": 0, "channel": 0}}
                           for key in images]}
    revised = labels.copy()
    revised[(labels == 1) & (xx < 64)] = 1001
    original_area = int(np.count_nonzero(labels))
    original_integral = int(raw[labels > 0].sum(dtype=np.uint64))

    def verify(result, edited=False):
        expected_labels = revised if edited else labels
        require(result["count"] == (65 if edited else 64), "All fibers, including the edited split, must be measured.")
        require(np.array_equal(decode_instance_mask(result["instance_mask"]), expected_labels), "The lossless result mask must preserve exact source-grid labels.")
        summary = result["summary"]
        require(summary["total_area_px"] == original_area, "Fiber area must be conserved.")
        require(sum(item["integrated_intensity"] for item in result["objects"]) == original_integral, "Raw integrated intensity must be conserved.")
        require(summary["nuclei_total"] == 208 and summary["nuclei_assigned"] == 192, "All explicit nuclear labels must be counted.")
        require(summary["nuclei_outside"] == 16 and summary["nuclei_uncertain"] == 0, "Outside nuclei must remain in the denominator and unassigned.")
        nuclei = {item["id"]: item for item in result["nuclei"]}
        require(nuclei[1]["fiber_id"] == (1001 if edited else 1), "Edited fiber labels must recompute nuclear ownership.")
        require(nuclei[2]["fiber_id"] == 1 and nuclei[3]["fiber_id"] == 1, "The other nuclei must retain their correct source-grid membership.")
        require(not result["method"]["display_lut_used"], "Fiber measurements must not use a display LUT.")
        return {"fiber_count": result["count"], "total_area_px": original_area,
                "raw_integrated_intensity": original_integral, "nuclei_total": summary["nuclei_total"],
                "nuclei_assigned": summary["nuclei_assigned"], "nuclei_outside": summary["nuclei_outside"],
                "nuclei_uncertain": summary["nuclei_uncertain"],
                "nucleus_1_owner": nuclei[1]["fiber_id"], "exact_label_mask_preserved": True}

    details = {"plane_shape_yx": [size, size], "original_planes": 4, "fiber_count": 64,
               "nucleus_count": 208, "outside_nuclei": 16, "marker_channels": 1,
               "calibration_um_per_px_xy": [.5, .75], "labels_imported_not_inferred": True}
    return [Case("fiber_labels_and_nuclei", "Fibers: 1024² labels, 64 fibers, 208 nuclei, anisotropic geometry and marker",
                 lambda: analyze_fibers(images, params), verify, images, params, details),
            Case("fiber_edited_labels_recompute", "Fibers: 1024² edited labels, 65 fibers, nuclear reassignment and full remeasurement",
                 lambda: recompute_fiber_result(images, params, revised), lambda result: verify(result, True),
                 {**images, "edited_labels": revised}, params,
                 {**details, "edited_fiber_count": 65, "edit": "Left half of fiber 1 relabelled 1001; original source labels preserved."})]


def instance_case() -> Case:
    yy, xx = np.mgrid[:1024, :1024]
    raw = (1000 + xx * 11 + yy * 7).astype(np.uint16)
    labels = np.zeros(raw.shape, np.uint32)
    labels[120:440, 120:440] = 1
    labels[600:900, 600:900] = 2
    area = int(np.count_nonzero(labels))
    integral = int(raw[labels > 0].sum(dtype=np.uint64))
    initial = {"kind": "microscopy", "count": 2,
        "method": {"name": "nuclei", "measurement_basis": "original_pixels", "display_lut_used": False},
        "summary": {"foreground_area_px": area, "object_type": "nucleus_candidate"},
        "objects": [], "warnings": [], "instance_mask": encode_instance_mask(labels)}
    operations = [{"id": "synthetic-split", "type": "split", "instance_id": 1, "seeds": [[180, 180], [380, 380]]},
                  {"id": "synthetic-merge", "type": "merge", "ids": [1, 3]}]
    params = {"method": "nuclei", "um_per_px": .5}

    def verify(result):
        require(result["count"] == 2, "Split then merge must restore two instances.")
        require(np.array_equal(decode_instance_mask(result["instance_mask"]), labels), "Split/merge replay must restore every original label pixel.")
        require(sum(item["area_px"] for item in result["objects"]) == area, "Replay must conserve foreground area.")
        require(sum(item["integrated_intensity"] for item in result["objects"]) == integral, "Replay must conserve raw integrated intensity.")
        trail = result["instance_review"]
        require(trail["cursor"] == 2 and trail["operations"][0]["output_ids"] == [1, 3], "Both operations must actually replay.")
        require(trail["operations"][1]["output_ids"] == [1] and not trail["source_pixels_modified"], "Merge must preserve the immutable source.")
        return {"initial_instances": 2, "instances_after_split": 3, "instances_after_merge": result["count"],
                "replayed_operations": trail["cursor"], "foreground_area_px": area,
                "raw_integrated_intensity": integral, "exact_original_label_mask_restored": True}

    return Case("instance_split_merge_replay", "Instances: 1024² existing lossless result, two-seed split and merge replay",
        lambda: review_instances(initial, raw, operations, cursor=2, params=params), verify,
        {"original_raw": raw, "original_labels": labels}, {"base_result": initial, "operations": operations, "params": params},
        {"plane_shape_yx": [1024, 1024], "initial_instances": 2, "operation_count": 2,
         "split_object_area_px": 320 * 320, "foreground_area_px": area,
         "includes": ["decode base mask", "copy labels", "two-seed geodesic split", "merge", "raw intensity remeasurement", "encode result mask"]})


def run_case(case: Case, repeats: int) -> dict:
    for value in case.arrays.values():
        value.setflags(write=False)
    hashes = {key: array_digest(value) for key, value in case.arrays.items()}
    protected_hash = json_digest(case.protected)
    before = load_snapshot()
    timings, result_hashes, verification = [], [], None
    warmup_ms = None
    for index in range(repeats + 1):
        gc.collect()
        start = perf_counter()
        result = case.operation()
        elapsed = (perf_counter() - start) * 1000
        verification = case.verify(result)
        result_hashes.append(json_digest(stable_result(result)))
        if index == 0:
            warmup_ms = round(elapsed, 3)
        else:
            timings.append(round(elapsed, 3))
        del result
    after = load_snapshot()
    unchanged = hashes == {key: array_digest(value) for key, value in case.arrays.items()}
    metadata_unchanged = protected_hash == json_digest(case.protected)
    require(unchanged and metadata_unchanged, f"{case.name}: original arrays, metadata or operations were mutated.")
    require(len(set(result_hashes)) == 1, f"{case.name}: results changed between identical calls.")
    return {"case": case.name, "title": case.title, "input": case.input_details,
            "arrays": {key: {"shape": list(value.shape), "dtype": str(value.dtype), "nbytes": value.nbytes,
                             "sha256_before_and_after": hashes[key], "writeable": value.flags.writeable}
                       for key, value in case.arrays.items()},
            "total_protected_array_bytes": sum(value.nbytes for value in case.arrays.values()),
            "warmup_calls": 1, "warmup_excluded_ms": warmup_ms,
            "measured_calls": repeats, "samples_ms": timings,
            "median_ms": round(statistics.median(timings), 3), "min_ms": min(timings), "max_ms": max(timings),
            "correctness": {"all_assertions_passed": True, "original_arrays_unchanged": unchanged,
                            "parameters_and_base_results_unchanged": metadata_unchanged,
                            "all_warmup_and_measured_results_identical_except_elapsed_ms": True,
                            "stable_result_sha256": result_hashes[0], "verified_output": verification},
            "load_before": before, "load_after": after}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, choices=range(3, 6), default=5)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "benchmarks" / "workflows_results.json")
    args = parser.parse_args()
    started = load_snapshot()
    source_files = ["workbench/wb_workflow.py", "workbench/histology.py", "workbench/fibers.py",
                    "workbench/instances.py", "workbench/analysis.py", "workbench/microscopy.py",
                    "workbench/microscopy_measurements.py", "benchmarks/benchmark_workflows.py"]
    report = {"benchmark_version": 1, "workbench_release": "0.3.0", "synthetic_data_only": True,
              "status": "running", "environment": environment(), "started": started,
              "method": {"clock": "time.perf_counter wall time", "warmup_calls_per_case": 1,
                         "measured_calls_per_case": args.repeats, "garbage_collection": "gc.collect before each call, outside timer",
                         "case_execution": "serial within this process; shared host not reserved",
                         "timed_scope": "Direct backend callable including its validation and result construction",
                         "excluded": ["fixture generation", "post-call correctness checks", "input/result hashes",
                                      "JSON report serialization", "source-file decoding", "HTTP", "browser", "export rendering"],
                         "immutability": "Read-only NumPy inputs plus before/after SHA-256 of all arrays and parameters/base results",
                         "determinism": "Full result SHA-256 across warmup and every repeat, excluding elapsed_ms fields"},
              "caveats": ["No upstream comparison or speedup claim is supported by this report.",
                          "These are synthetic throughput and invariant checks, not biological validation or diagnostic accuracy.",
                          "Timings are Linux backend wall times, not Windows EXE startup, whole UI interaction or end-to-end import/export times.",
                          "Other agents or host processes may run concurrently; host isolation prevents a complete process inventory. Load averages are recorded, not treated as proof of an idle host.",
                          "One warmup and five or fewer repeats describe this run only; they do not estimate reliable production tail latency.",
                          "Thread environment settings are requests; native thread pools were not independently enumerated.",
                          "The fiber cases consume explicit supplied instance labels. Neural model inference and automatic segmentation accuracy are outside these timings."],
              "source_sha256": {path: hashlib.sha256((PROJECT_ROOT / path).read_bytes()).hexdigest() for path in source_files},
              "cases": []}
    cases = [wb_case(), *histology_cases(), *fiber_cases(), instance_case()]
    try:
        for case in cases:
            result = run_case(case, args.repeats)
            report["cases"].append(result)
            print(f"{case.name}: median {result['median_ms']:.3f} ms; {args.repeats} repeats; invariants passed", flush=True)
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        report["finished"] = load_snapshot()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    print(f"Report: {args.output}", flush=True)


if __name__ == "__main__":
    main()
