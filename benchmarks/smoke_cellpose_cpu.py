"""Reproducible real-model CPU smoke check; not biological accuracy validation.

Run in the application environment with the separate Cellpose component paths
configured, or with the component installed in its normal portable layout.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.cellpose_runtime import analyze_cellpose


def synthetic_fixture() -> np.ndarray:
    y, x = np.mgrid[:160, :192]
    values = np.full((160, 192), 1000.0)
    for center_x, center_y in ((36, 40), (84, 40), (132, 40), (36, 112), (84, 112), (132, 112)):
        values += 30000 * np.exp(-((x - center_x) ** 2 + (y - center_y) ** 2) / 80)
    return values.astype(np.uint16)


def run() -> dict:
    image = synthetic_fixture()
    original = image.copy()
    source_hash = hashlib.sha256(image.tobytes()).hexdigest()
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(),
              "scope": "Real official-model CPU execution on a deterministic synthetic fixture; not biological accuracy validation.",
              "fixture": {"shape": list(image.shape), "dtype": str(image.dtype), "sha256": source_hash,
                          "constructed_gaussian_objects": 6, "generator": "synthetic_fixture in smoke_cellpose_cpu.py"},
              "host": {"platform": platform.platform(), "python": platform.python_version(),
                       "logical_cpus": os.cpu_count()}, "runs": []}
    for model, diameter in (("nuclei", 17), ("cyto3", 25)):
        params = {"method": "cellpose", "model": model, "diameter_px": diameter,
                  "min_area": 20, "threads": 2,
                  "source": {"asset_id": f"synthetic-{source_hash}", "series": 0, "frame": 0, "channel": 0}}
        started = perf_counter()
        result = analyze_cellpose(image, params)
        wall_ms = (perf_counter() - started) * 1000
        record = result["method"]["model_record"]
        unchanged = np.array_equal(image, original)
        coordinates_valid = all(0 <= item["x"] < image.shape[1] and 0 <= item["y"] < image.shape[0]
                                for item in result["objects"])
        assert unchanged and coordinates_valid
        assert record["numba_threading_layer"] == "workqueue"
        assert record["numba_num_threads"] == 2
        report["runs"].append({"model": model, "count": result["count"],
                               "foreground_area_px": result["summary"]["foreground_area_px"],
                               "wall_ms": wall_ms, "worker_record": record,
                               "source_unchanged": unchanged, "all_centroids_inside_source": coordinates_valid})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("cellpose_cpu_smoke.json"))
    options = parser.parse_args()
    report = run()
    options.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(options.output), "runs": [{key: row[key] for key in
                      ("model", "count", "foreground_area_px", "wall_ms", "source_unchanged")}
                      for row in report["runs"]]}, ensure_ascii=False))
