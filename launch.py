"""First-run bootstrap. Only the standard library is needed before installation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main() -> int:
    if sys.version_info < (3, 12):
        print('Figure Workbench requires Python 3.12 or newer. Python 3.12 is recommended.')
        print('Install Python from https://www.python.org/downloads/ and run this launcher again.')
        return 1
    environment = ROOT / '.venv'
    interpreter = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    requirements = ROOT / 'requirements.txt'
    requirement_files = [requirements]
    if os.name == 'nt':
        requirement_files.append(ROOT / 'requirements-desktop.txt')
    digest = hashlib.sha256(b'\0'.join(path.read_bytes() for path in requirement_files)).hexdigest()
    marker = environment / '.figure-workbench-dependencies'
    try:
        if not interpreter.is_file():
            print('Creating the private Python environment...', flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        installed = marker.read_text('ascii').strip() if marker.exists() else ''
        if installed != digest:
            print('Installing the pinned dependencies. This first run needs an internet connection.', flush=True)
            print('After installation, source images and analysis stay on this computer.', flush=True)
            requirement_args = [argument for path in requirement_files for argument in ('-r', str(path))]
            subprocess.run([str(interpreter), '-m', 'pip', 'install', '--disable-pip-version-check',
                            *requirement_args], cwd=ROOT, check=True)
            subprocess.run([str(interpreter), '-B', str(ROOT / 'run.py'), '--check'], cwd=ROOT, check=True)
            marker.write_text(digest, 'ascii')
        return subprocess.call([str(interpreter), '-B', str(ROOT / 'run.py'), *sys.argv[1:]], cwd=ROOT)
    except (OSError, subprocess.CalledProcessError) as error:
        print('\nCould not start Figure Workbench:', error)
        print('Keep this window open to read the error. Check Python version, free disk space,')
        print('and your connection to the Python package index. No source image has been uploaded.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
