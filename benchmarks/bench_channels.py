"""Reproducible local multichannel renderer benchmark (requires psutil).

Run from the project root: python benchmarks/bench_channels.py
Cold means a fresh Python process with an empty ProjectStore array cache.
The operating-system file cache is NOT flushed. Warm still executes rendering;
it does not measure the faster HTTP encoded-PNG cache-hit path.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_worker(project_path, scenario):
    import numpy as np
    import psutil
    from workbench.channels import normalize_view, render_view
    from workbench.imaging import png_bytes
    from workbench.storage import ProjectStore

    store = ProjectStore(project_path)
    asset = store.get_project()['assets'][0]
    view = normalize_view({'mode': 'split' if scenario.endswith('split') else 'merge',
                          'split': {'columns': 3, 'gap_px': 8, 'include_merge': True},
                          'channels': [
                              {'id': f'c{index}', 'name': f'Synthetic channel {index + 1}',
                               'color': color, 'black': 0, 'white': 65535, 'gamma': 1,
                               'source': {'asset_id': asset['id'], 'series': 0, 'frame': 0, 'channel': index}}
                              for index, color in enumerate(('#FF0000', '#00FF00', '#0000FF', '#FFFFFF'))]})
    process = psutil.Process()
    measurements = []
    for iteration in range(3):
        gc.collect()
        before = process.memory_info().rss
        samples = [before]
        stopped = threading.Event()
        def sample():
            while not stopped.wait(.005):
                samples.append(process.memory_info().rss)
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        started = time.perf_counter()
        image, manifest = render_view(store, view, max_size=1024 if scenario.startswith('preview') else None)
        render_ms = (time.perf_counter() - started) * 1000
        encode_ms, encoded_bytes = None, None
        if scenario.startswith('preview'):
            started = time.perf_counter()
            payload = png_bytes(image)
            encode_ms = (time.perf_counter() - started) * 1000
            encoded_bytes = len(payload)
            del payload
        samples.append(process.memory_info().rss)
        stopped.set()
        thread.join()
        record = {'cache_state': 'cold_application_cache' if iteration == 0 else 'warm_original_plane_cache',
                  'render_ms': round(render_ms, 3), 'png_encode_ms': round(encode_ms, 3) if encode_ms is not None else None,
                  'encoded_png_bytes': encoded_bytes, 'output_width_px': image.width, 'output_height_px': image.height,
                  'rss_before_bytes': before, 'rss_peak_sampled_bytes': max(samples),
                  'rss_delta_peak_bytes': max(samples) - before, 'rss_sample_count': len(samples),
                  'retained_plane_cache_bytes': store.array_cache.bytes,
                  'output_rgb_bytes': image.width * image.height * 3,
                  'tile_accumulator_bytes': manifest['tiles'][0]['width_px'] * manifest['tiles'][0]['height_px'] * 12
                    if not scenario.startswith('preview') else None}
        measurements.append(record)
        image.close()
        del image
    planes = [store.read_plane(channel['source']) for channel in view['channels']]
    return {'scenario': scenario, 'measurements': measurements,
            'warm_render_median_ms': round(statistics.median(item['render_ms'] for item in measurements[1:]), 3),
            'original_dtypes': [str(plane.dtype) for plane in planes],
            'originals_read_only': all(not plane.flags.writeable for plane in planes),
            'raw_plane_sha256_after': [hashlib.sha256(plane.tobytes()).hexdigest() for plane in planes],
            'source_file_sha256_after': hashlib.sha256(store.resolve(asset['id']).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', type=int, default=2048)
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks' / 'channels_results.json')
    parser.add_argument('--worker-project', type=Path)
    parser.add_argument('--scenario')
    args = parser.parse_args()
    if args.worker_project:
        print(json.dumps(run_worker(args.worker_project, args.scenario)))
        return
    if not 64 <= args.size <= 4096:
        parser.error('--size must be between 64 and 4096')
    import numpy as np
    import tifffile
    from workbench.storage import ProjectStore
    started = time.time()
    with tempfile.TemporaryDirectory(prefix='figwork-channel-benchmark-') as directory:
        directory = Path(directory)
        y, x = np.indices((args.size, args.size), dtype=np.uint32)
        # Deterministic gradients and patterned signal cover the full recorded
        # uint16 range. These are synthetic data, not scientific observations.
        raw = np.stack([((x * (17 + c * 7) + y * (37 + c * 11) + (x * y // (c + 3))) % 65536).astype(np.uint16)
                        for c in range(4)])
        expected = [hashlib.sha256(plane.tobytes()).hexdigest() for plane in raw]
        path = directory / 'synthetic-four-channel.tif'
        tifffile.imwrite(path, raw, metadata={'axes': 'CYX', 'description': 'SYNTHETIC PERFORMANCE FIXTURE'},
                         photometric='minisblack', compression='deflate')
        original_size = path.stat().st_size
        store = ProjectStore(directory / 'project')
        asset = store.add_files([(path.name, path.read_bytes())])[0]
        del raw, x, y, store
        gc.collect()
        scenarios = []
        environment = os.environ.copy()
        environment['PYTHONPATH'] = os.pathsep.join([str(ROOT), environment.get('PYTHONPATH', '')])
        for name in ['preview_merge', 'native_merge', 'preview_split', 'native_split']:
            output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()), '--worker-project',
                                              str(directory / 'project'), '--scenario', name],
                                             env=environment, cwd=ROOT, text=True)
            result = json.loads(output)
            assert result['raw_plane_sha256_after'] == expected
            assert result['source_file_sha256_after'] == asset['id']
            assert result['originals_read_only'] and result['original_dtypes'] == ['uint16'] * 4
            scenarios.append(result)
        package_names = ['numpy', 'Pillow', 'tifffile', 'imagecodecs', 'psutil']
        results = {'recorded_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                   'benchmark_version': 1, 'platform': platform.platform(), 'python': platform.python_version(),
                   'cpu_logical_count': os.cpu_count(),
                   'packages': {name: importlib.metadata.version(name) for name in package_names},
                   'fixture': {'synthetic': True, 'axes': 'CYX', 'shape': [4, args.size, args.size],
                               'dtype': 'uint16', 'compression': 'deflate', 'file_bytes': original_size,
                               'decoded_plane_bytes': args.size * args.size * 2,
                               'source_sha256': asset['id'], 'raw_plane_sha256': expected},
                   'methodology': {'cold': 'Fresh process and empty original-plane cache; OS file cache not flushed.',
                                   'warm': 'Two renders with original-plane LRU populated; encoded HTTP cache not used.',
                                   'preview': '1024 px maximum total layout; PNG encoding timed separately.',
                                   'split': 'Four individual channels plus merge in three columns; 8 original-pixel gaps.',
                                   'rss': 'psutil resident working set sampled every 5 ms; may miss shorter peaks; delta excludes preexisting process memory.',
                                   'scope': 'Local Linux backend measurements only. No Windows timing or comparison with the upstream projects is claimed.',
                                   'render_memory': 'One source/LUT channel at a time. Native LUT rows <= approximately 262144 pixels. An RGB accumulator above 8 million tile pixels uses a temporary mapped file.'},
                   'source_preservation_verified': True, 'scenarios': scenarios,
                   'elapsed_seconds': round(time.time() - started, 3)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(str(args.output))


if __name__ == '__main__':
    main()
