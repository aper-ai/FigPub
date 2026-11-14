"""Trust boundaries for the new desktop package; these do not execute Windows."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
from build_desktop_launcher import pe_metadata
from fetch_desktop_dependencies import proxy_wheel
from upgrade_windows_0_5 import install_desktop


def test_proxy_source_normalization_is_deterministic_and_never_executes_setup():
    buffer = io.BytesIO()
    module = b'class Proxy: pass\n'
    metadata = b'Metadata-Version: 1.0\nName: proxy-tools\nVersion: 0.1.0\n'
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, content in {'proxy_tools/__init__.py': module, 'PKG-INFO': metadata,
                              'setup.py': b'raise RuntimeError("must never execute")\n'}.items():
            item = tarfile.TarInfo('proxy_tools-0.1.0/' + name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    first = proxy_wheel(buffer.getvalue())
    assert first == proxy_wheel(buffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(first)) as wheel:
        assert wheel.testzip() is None
        assert wheel.read('proxy_tools/__init__.py') == module
        assert wheel.read('proxy_tools-0.1.0.dist-info/METADATA') == metadata
        assert not any(name.endswith('setup.py') for name in wheel.namelist())
        rows = csv.reader(io.StringIO(wheel.read('proxy_tools-0.1.0.dist-info/RECORD').decode()))
        for name, digest, size in rows:
            if digest:
                content = wheel.read(name)
                assert len(content) == int(size)
                assert digest == 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()


@pytest.mark.parametrize(('machine', 'magic', 'subsystem'), [(0x8664, 0x20b, 3), (0x14c, 0x10b, 2)])
def test_console_or_wrong_architecture_launcher_cannot_pass_the_desktop_gate(tmp_path, machine, magic, subsystem):
    data = bytearray(160)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 64)
    data[64:68] = b'PE\0\0'
    struct.pack_into('<H', data, 68, machine)
    struct.pack_into('<H', data, 88, magic)
    struct.pack_into('<H', data, 156, subsystem)
    candidate = tmp_path / 'candidate.exe'
    candidate.write_bytes(data)
    with pytest.raises(ValueError, match='GUI-subsystem'):
        pe_metadata(candidate)


def test_modified_desktop_dependency_is_rejected_before_installation(tmp_path):
    source = tmp_path / 'source'
    (source / 'packaging').mkdir(parents=True)
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    expected = b'official wheel bytes'
    wheel = wheels / 'sample-1.0-py3-none-any.whl'
    wheel.write_bytes(b'modified wheel bytes')
    specification = {'sources': [{'kind': 'wheel', 'filename': wheel.name, 'name': 'sample', 'version': '1.0',
                                  'bytes': len(expected), 'sha256': hashlib.sha256(expected).hexdigest()}]}
    (source / 'packaging/desktop-sources.lock.json').write_text(json.dumps(specification))
    runtime = tmp_path / 'runtime'
    with pytest.raises(ValueError, match='official-source lock'):
        install_desktop(wheels, runtime, {'dependencies': []}, source)
    assert not runtime.exists()


def test_native_evidence_rejects_a_package_from_another_source_snapshot(tmp_path, monkeypatch):
    specification = importlib.util.spec_from_file_location('native_acceptance_checks', ROOT / 'tests/desktop_windows_smoke.py')
    smoke = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(smoke)
    checkout, distribution = tmp_path / 'checkout', tmp_path / 'distribution'
    checkout.mkdir()
    (distribution / 'app').mkdir(parents=True)
    content = b'current source\n'
    (checkout / 'run.py').write_bytes(content)
    (distribution / 'app/run.py').write_bytes(content)
    manifest = {'files': [{'path': 'run.py', 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}],
                'file_count': 1}
    (distribution / 'app/SOURCE_MANIFEST.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(smoke, 'ROOT', checkout)
    digest, count = smoke.verify_packaged_source(distribution)
    assert len(digest) == 64 and count == 1
    (checkout / 'run.py').write_bytes(b'changed source\n')
    with pytest.raises(AssertionError, match='Source mismatch'):
        smoke.verify_packaged_source(distribution)
