"""Create an isolated, official CPU model environment for acceptance only.

Never called by the application. Downloads are explicit CI/developer setup;
model bytes must match the application's pinned digests before inference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.cellpose_worker import MODEL_SPECS


def prepare(destination: Path, record: Path | None = None):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError('Choose a new acceptance environment directory.')
    venv.EnvBuilder(with_pip=True).create(destination / 'venv')
    python = destination / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-m', 'pip', 'install', 'torch==2.7.1+cpu',
                    '--index-url', 'https://download.pytorch.org/whl/cpu'], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', 'cellpose==3.1.1.3', 'numpy==2.0.2',
                    'opencv-python-headless==4.11.0.86', 'numba==0.61.2'], check=True)
    models = destination / 'models'
    models.mkdir()
    records = []
    for name, spec in MODEL_SPECS.items():
        with urllib.request.urlopen(spec['url'], timeout=90) as response:
            data = response.read(spec['bytes'] + 1)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != spec['bytes'] or digest != spec['sha256']:
            raise ValueError(f'Official {name} weights did not match the pinned identity.')
        (models / spec['filename']).write_bytes(data)
        records.append({'model': name, 'bytes': len(data), 'sha256': digest, 'url': spec['url']})
    freeze = subprocess.check_output([str(python), '-m', 'pip', 'freeze'], text=True)
    (destination / 'dependencies.txt').write_text(freeze, encoding='utf-8')
    (destination / 'models.json').write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
    receipt = {'platform': sys.platform, 'python_version': sys.version, 'models': records,
               'dependencies': freeze.splitlines(), 'acceptance_environment_only': True,
               'existing_portable_component_executed': False}
    if record:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    github_env = os.environ.get('GITHUB_ENV')
    if github_env:
        with open(github_env, 'a', encoding='utf-8') as stream:
            stream.write(f'FIGURE_WORKBENCH_CELLPOSE_HOME={destination}\n')
            stream.write('FIGURE_WORKBENCH_TEST_CELLPOSE=1\n')
    print(json.dumps({'home': str(destination), 'python': str(python), 'models_verified': len(records)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--record', type=Path)
    args = parser.parse_args()
    prepare(args.output, args.record)
