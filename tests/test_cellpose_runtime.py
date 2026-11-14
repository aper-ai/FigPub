"""Worker protocol/identity/cancellation tests; real inference is opt-in below.

Small mocked label fixtures test geometry and measurements, never model accuracy.
The separate real CPU smoke record identifies actual official weights/runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from time import perf_counter

import numpy as np
import pytest

from workbench import cellpose_runtime as runtime
from workbench.microscopy import analyze_microscopy
from workbench.cellpose_worker import configure_cpu_threads


@pytest.fixture
def no_component(monkeypatch, tmp_path):
    monkeypatch.setenv("FIGURE_WORKBENCH_CELLPOSE_HOME", str(tmp_path / "missing"))
    monkeypatch.setenv("FIGURE_WORKBENCH_CELLPOSE_PYTHON", str(tmp_path / "missing" / "python.exe"))
    monkeypatch.setenv("FIGURE_WORKBENCH_CELLPOSE_MODELS", str(tmp_path / "models"))
    monkeypatch.setenv("FIGURE_WORKBENCH_DATA", str(tmp_path / "data"))
    runtime._PROBE_CACHE.clear()
    runtime._HASH_CACHE.clear()


def test_missing_model_is_honest_and_never_calls_inference(no_component):
    status = runtime.cellpose_status()
    assert status["available"] is False
    assert status["has_component"] is False
    assert status["weights_available"] is False
    assert all(not model["available"] for model in status["models"])
    with pytest.raises(ValueError, match="离线模型"):
        analyze_microscopy(np.zeros((20, 20), np.uint16), {"method": "cellpose"})


def test_invalid_model_and_parameters_fail_before_launch(no_component):
    image = np.zeros((20, 20), np.uint16)
    for params in ({"model": "../untrusted.pt"}, {"diameter_px": .01}, {"flow_threshold": float("nan")},
                   {"threads": 1.5}, {"min_area": -1}, {"normalize": "yes"}):
        with pytest.raises(ValueError):
            runtime.run_cellpose_labels(image, params)


def test_weight_hash_mismatch_is_not_available(no_component, monkeypatch, tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "cyto3").write_bytes(b"not a model")
    monkeypatch.setattr(runtime, "_probe", lambda *_: {"available": True, "status": "ready"})
    status = runtime.cellpose_status()
    model = next(item for item in status["models"] if item["id"] == "cyto3")
    assert model["status"] == "hash_mismatch"
    assert not model["available"]


def test_cached_probe_is_used_without_starting_another_worker(no_component, monkeypatch, tmp_path):
    # This fixture tests only the probe protocol; it does not load a model.
    models = tmp_path / "models"
    models.mkdir()
    sample = b"protocol-fixture"
    (models / "test-weights").write_bytes(sample)
    worker = tmp_path / "probe_worker.py"
    worker.write_text('print(\'{"available": true, "device": "cpu", "cellpose_version": "3.1.1.3"}\')\n')
    layout = runtime.RuntimeLayout(Path(sys.executable), models, worker)
    monkeypatch.setattr(runtime, "_runtime_layout", lambda: layout)
    monkeypatch.setattr(runtime, "MODEL_SPECS", {"cyto3": {"filename": "test-weights", "bytes": len(sample),
                        "sha256": hashlib.sha256(sample).hexdigest(), "url": "https://example.invalid/test-fixture"}})
    initial = runtime.cellpose_status(probe=False)
    assert initial["has_component"] and initial["weights_available"]
    assert initial["probe_required"] and not initial["available"]
    verified = runtime.cellpose_status(probe=True)
    assert verified["available"]
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("probe=False must not spawn"))
    cached = runtime.cellpose_status(probe=False)
    assert cached["available"] and not cached["probe_required"]


def test_virtualenv_interpreter_symlink_is_not_resolved(no_component, monkeypatch, tmp_path):
    interpreter = tmp_path / "venv-python"
    try:
        interpreter.symlink_to(sys.executable)
    except OSError:
        pytest.skip("Symbolic links unavailable on this test host")
    monkeypatch.setenv("FIGURE_WORKBENCH_CELLPOSE_PYTHON", str(interpreter))
    assert runtime._runtime_layout().interpreter == interpreter
    assert runtime._runtime_layout().interpreter != interpreter.resolve()


def test_child_environment_does_not_mix_core_numpy_abi(no_component, monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", "/application/numpy-2.5")
    monkeypatch.setenv("PYTHONHOME", "/application/python")
    environment = runtime._child_environment(runtime.RuntimeLayout(Path(sys.executable), tmp_path / "models", tmp_path / "worker.py"))
    assert "PYTHONPATH" not in environment and "PYTHONHOME" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"


@pytest.mark.parametrize("operation", ["probe", "inference"])
def test_isolated_worker_import_preserves_component_files(no_component, monkeypatch, tmp_path, operation):
    """Execute both launch paths with real -I imports, independently of model accuracy."""
    component = tmp_path / "portable component"
    models = component / "models"
    models.mkdir(parents=True)
    sample = b"isolated-import-protocol-fixture"
    checksum = hashlib.sha256(sample).hexdigest()
    (models / "fixture-weights").write_bytes(sample)
    (component / "component_module.py").write_text('TOKEN = "actually imported"\n', encoding="utf-8")
    worker = component / "worker.py"
    worker.write_text('''import argparse
import json
from pathlib import Path
import sys

# -I omits the script directory; add only this fixture's component module.
sys.path.insert(0, str(Path(__file__).parent))
import component_module

parser = argparse.ArgumentParser()
parser.add_argument("--probe", action="store_true")
for name in ("models-dir", "input", "output", "metadata", "request"):
    parser.add_argument("--" + name)
args = parser.parse_args()
record = {"available": True, "isolated": sys.flags.isolated,
          "dont_write_bytecode": sys.dont_write_bytecode,
          "imported": component_module.TOKEN}
if args.probe:
    print(json.dumps(record))
else:
    import numpy as np
    import hashlib
    image = np.load(args.input, allow_pickle=False)
    np.save(args.output, np.ones(image.shape, dtype=np.int32), allow_pickle=False)
    request = json.loads(Path(args.request).read_text())
    record.update(model=request["model"], weights_sha256=hashlib.sha256(
        (Path(args.models_dir) / "fixture-weights").read_bytes()).hexdigest())
    Path(args.metadata).write_text(json.dumps(record))
''', encoding="utf-8")
    layout = runtime.RuntimeLayout(Path(sys.executable), models, worker)
    environment = runtime._child_environment(layout)
    # Positive control: the environment flag alone does not stop writes in
    # isolated mode. This import must really create a cache before the fix.
    baseline = subprocess.run([sys.executable, "-I", str(worker), "--probe"],
                              env=environment, capture_output=True, text=True, check=True)
    assert json.loads(baseline.stdout)["dont_write_bytecode"] is False
    assert list(component.rglob("component_module.*.pyc"))
    shutil.rmtree(component / "__pycache__")

    before = {path.relative_to(component): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in component.rglob("*") if path.is_file()}
    monkeypatch.setattr(runtime, "_runtime_layout", lambda: layout)
    monkeypatch.setattr(runtime, "MODEL_SPECS", {"cyto3": {
        "filename": "fixture-weights", "bytes": len(sample), "sha256": checksum,
        "url": "https://example.invalid/isolated-import-fixture", "training_diameter_px": 30.0}})
    real_popen = subprocess.Popen

    def isolated_process(command, *args, **kwargs):
        # Apply the portable interpreter's isolation at the real subprocess
        # boundary. Do not replace execution or inspect a command string.
        return real_popen([command[0], "-I", *command[1:]], *args, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "Popen", isolated_process)
    if operation == "probe":
        record = runtime._probe(layout, refresh=True)
        assert record["available"] is True
    else:
        image = np.full((8, 8), 1234, np.uint16)
        labels, record = runtime.run_cellpose_labels(image, {"model": "cyto3"})
        assert np.all(labels == 1) and np.all(image == 1234)
    assert record["isolated"] == 1 and record["imported"] == "actually imported"
    assert record["dont_write_bytecode"] is True
    after = {path.relative_to(component): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in component.rglob("*") if path.is_file()}
    assert after == before
    assert not list(component.rglob("*.pyc"))


def test_worker_uses_portable_bounded_numba_threads(monkeypatch):
    for name in ("NUMBA_THREADING_LAYER", "NUMBA_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        monkeypatch.setenv(name, "inherited-unbounded-setting")
    configure_cpu_threads(2)
    assert os.environ["NUMBA_THREADING_LAYER"] == "workqueue"
    assert os.environ["NUMBA_NUM_THREADS"] == "2"
    assert os.environ["OMP_NUM_THREADS"] == "2"
    assert os.environ["MKL_NUM_THREADS"] == "2"
    with pytest.raises(ValueError):
        configure_cpu_threads(0)


def test_running_worker_is_really_terminated_on_cancellation(no_component, tmp_path):
    event = threading.Event()
    timer = threading.Timer(.2, event.set)
    timer.start()
    started = perf_counter()
    try:
        with pytest.raises(runtime.CellposeCancelledError, match="终止"):
            runtime._run_worker([sys.executable, "-c", "import time; time.sleep(30)"],
                                runtime.RuntimeLayout(Path(sys.executable), tmp_path, tmp_path / "worker.py"),
                                tmp_path, 10, event)
    finally:
        timer.cancel()
    assert perf_counter() - started < 4


def test_pre_cancelled_inference_does_not_probe_or_start_process(monkeypatch):
    event = threading.Event()
    event.set()
    monkeypatch.setattr(runtime, "cellpose_status", lambda **_: pytest.fail("Cancelled work must not probe"))
    with pytest.raises(runtime.CellposeCancelledError):
        runtime.run_cellpose_labels(np.zeros((4, 4), np.uint16), {}, cancel_event=event)


def test_running_worker_timeout_terminates_child(no_component, tmp_path):
    started = perf_counter()
    with pytest.raises(ValueError, match="超时且进程已终止"):
        runtime._run_worker([sys.executable, "-c", "import time; time.sleep(30)"],
                            runtime.RuntimeLayout(Path(sys.executable), tmp_path, tmp_path / "worker.py"),
                            tmp_path, .2)
    assert perf_counter() - started < 4


def test_label_postprocessing_uses_original_values_and_roi_offsets(monkeypatch):
    raw = np.full((50, 60), 40001, np.uint16)
    original = raw.copy()
    observed = {}

    def label_fixture(image, params, **kwargs):
        observed["input_dtype"] = image.dtype
        labels = np.zeros(image.shape, np.int32)
        labels[2:6, 3:8] = 7
        return labels, {"model": "nuclei", "fixture": "geometry test only"}

    monkeypatch.setattr(runtime, "run_cellpose_labels", label_fixture)
    result = analyze_microscopy({"c1": raw}, {"method": "cellpose", "channels": [{"id": "c1",
                    "source": {"asset_id": "source", "series": 2, "frame": 4, "channel": 3}}],
                    "roi": {"x": 10, "y": 20, "width": 30, "height": 20}, "min_area": 2, "um_per_px": .5,
                    "display": {"gamma": 4, "low": 40000, "high": 40002}})
    assert observed["input_dtype"] == np.uint16
    assert result["count"] == 1
    instance = result["objects"][0]
    assert instance["model_instance_id"] == 7
    assert (instance["x"], instance["y"]) == (15, 23.5)
    assert instance["area_px"] == 20 and instance["area_um2"] == 5
    assert instance["mean_intensity"] == 40001
    assert result["method"]["source_channel"]["source"]["channel"] == 3
    assert np.array_equal(raw, original)
    json.dumps(result, allow_nan=False)


def test_polygon_limits_model_instances_and_border_exclusion(monkeypatch):
    def label_fixture(image, params, **kwargs):
        return np.ones(image.shape, np.int32), {"fixture": "geometry test only"}

    monkeypatch.setattr(runtime, "run_cellpose_labels", label_fixture)
    roi = {"type": "ellipse", "x": 10, "y": 10, "width": 4, "height": 4}
    result = analyze_microscopy(np.full((30, 30), 100, np.uint16), {"method": "cellpose", "roi": roi, "min_area": 1})
    assert result["objects"][0]["area_px"] == 12
    assert result["objects"][0]["touches_roi_border"]
    excluded = analyze_microscopy(np.full((30, 30), 100, np.uint16), {"method": "cellpose", "roi": roi,
                                  "min_area": 1, "exclude_border": True})
    assert excluded["count"] == 0


@pytest.mark.skipif(os.environ.get("FIGURE_WORKBENCH_TEST_CELLPOSE") != "1", reason="Requires separately installed official CPU model component")
@pytest.mark.parametrize('model,diameter', [('nuclei', 17), ('cyto3', 30)], ids=['nuclei', 'cyto3'])
def test_real_official_model_inference_preserves_input(monkeypatch, model, diameter):
    monkeypatch.setattr(runtime, "_probe", lambda *args, **kwargs: pytest.fail("Inference must not run an uncancellable pre-probe"))
    yy, xx = np.mgrid[:96, :96]
    image = (1000 + 30000 * np.exp(-((xx - 48) ** 2 + (yy - 48) ** 2) / (diameter / 2) ** 2)).astype(np.uint16)
    original = image.copy()
    labels, metadata = runtime.run_cellpose_labels(image, {"model": model, "diameter_px": diameter, "min_area": 10, "threads": 2})
    assert labels.shape == image.shape and labels.dtype == np.int32
    # The centered synthetic object must actually be detected. Shape and
    # metadata alone would also accept an accidentally empty inference result.
    foreground = labels > 0
    assert 10 < np.count_nonzero(foreground) < image.size * .9
    assert labels[48, 48] > 0
    ys, xs = np.nonzero(foreground)
    assert abs(float(xs.mean()) - 48) < 4 and abs(float(ys.mean()) - 48) < 4
    assert metadata["model"] == model
    assert metadata["weights_sha256"] == runtime.MODEL_SPECS[model]["sha256"]
    assert metadata["weights_filename"] == runtime.MODEL_SPECS[model]["filename"]
    assert metadata["weights_source_url"] == runtime.MODEL_SPECS[model]["url"]
    assert metadata["parameters"]["diameter_px"] == diameter
    assert metadata["parameters"]["model_channels"] == [0, 0]
    assert metadata["native_output_shape"] == list(image.shape)
    assert np.isfinite(metadata["inference_ms"]) and metadata["inference_ms"] > 0
    assert metadata["cellpose_version"] == "3.1.1.3"
    assert metadata["device"] == "cpu" and metadata["network_download_performed"] is False
    assert metadata["numba_threading_layer_requested"] == "workqueue"
    assert metadata["numba_threading_layer"] == "workqueue"
    assert metadata["numba_num_threads"] == 2
    assert np.array_equal(image, original)
