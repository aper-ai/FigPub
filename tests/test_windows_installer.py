"""Reject incomplete or changed scientific payloads before making a setup EXE."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
import build_windows_installer as setup
from test_full_toolkit_packaging import toolkit


def test_installer_requires_the_complete_exact_source_payload(toolkit):
    verified, directory = setup.verified_payload(toolkit['directory'], toolkit['identity']['commit'])
    assert verified['manifest_sha256'] == toolkit['record']['full_toolkit_manifest_sha256']
    assert verified['source_identity'] == toolkit['identity']
    assert directory == setup.VERSION + '-' + toolkit['identity']['commit'][:12]


@pytest.mark.parametrize('commit', ['b' * 40, 'a' * 12, '../source'])
def test_installer_rejects_a_different_or_abbreviated_source_revision(toolkit, commit):
    with pytest.raises(ValueError, match='source commit|Git SHA'):
        setup.verified_payload(toolkit['directory'], commit)


@pytest.mark.parametrize('path', [
    'cellpose/models/cyto3', 'runtime/python312.dll', 'app/workbench/cellpose_worker.py',
])
def test_installer_cannot_hide_a_changed_runtime_or_model(toolkit, path):
    target = toolkit['directory'] / path
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        setup.verified_payload(toolkit['directory'], toolkit['identity']['commit'])


def test_a_zip_or_naked_launcher_cannot_be_passed_as_a_complete_toolkit(toolkit):
    for path in (toolkit['archive'], toolkit['directory'] / 'FigureWorkbench.exe'):
        with pytest.raises(ValueError, match='complete toolkit directory'):
            setup.verified_payload(path, toolkit['identity']['commit'])
