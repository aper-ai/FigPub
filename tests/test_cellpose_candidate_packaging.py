"""Portable CPU trust-boundary checks using synthetic wheels, never inference.

The tiny PE headers below only exercise static identity validation. Actual
Windows execution and both official weights remain separate acceptance gates.
"""
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
import build_cellpose_candidate as builder
from assemble_windows import install_wheel


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pe_header():
    data = bytearray(160)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 64)
    data[64:68] = b'PE\0\0'
    struct.pack_into('<H', data, 68, 0x8664)
    struct.pack_into('<H', data, 88, 0x20b)
    return bytes(data)


def wheel(directory, name, version, payload, requires=()):
    dist = f'{name}-{version}.dist-info'
    files = {**payload,
             dist + '/METADATA': (f'Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nRequires-Python: >=3.9\n'
                                  + ''.join(f'Requires-Dist: {value}\n' for value in requires)).encode(),
             dist + '/WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    output = io.StringIO(newline='')
    record = csv.writer(output, lineterminator='\n')
    for path, content in sorted(files.items()):
        record.writerow([path, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode(), str(len(content))])
    record.writerow([dist + '/RECORD', '', ''])
    files[dist + '/RECORD'] = output.getvalue().encode()
    path = directory / f'{name}-{version}-py3-none-any.whl'
    with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return path


def fixture(tmp_path, monkeypatch, *, extra_pth=None):
    home, wheels = tmp_path / 'tested-environment', tmp_path / 'wheels'
    venv = home / 'venv'
    (venv / 'Scripts').mkdir(parents=True)
    (venv / 'Scripts/python.exe').write_bytes(pe_header())
    (venv / 'pyvenv.cfg').write_text('home = C:\\runner\\Python\nversion = 3.12.10\ninclude-system-site-packages = false\n')
    wheels.mkdir()
    payloads = [
        ('cellpose', '3.1.1.3', {'cellpose/models.py': b'# synthetic model entry\n',
                               'cellpose-3.1.1.3.dist-info/licenses/LICENSE': b'Original synthetic license fixture\n'},
         ['numpy==2.0.2', 'torch==2.7.1+cpu']),
        ('torch', '2.7.1+cpu', {'torch/__init__.py': b'# synthetic torch entry\n'}, ['setuptools>=70']),
        ('numpy', '2.0.2', {'numpy/__init__.py': b'# synthetic numpy entry\n'}, []),
        ('setuptools', '78.1.0', {'_distutils_hack/__init__.py': b'# synthetic local shim\n',
                                'distutils-precedence.pth': builder.DISTUTILS_PTH}, []),
    ]
    if extra_pth is not None:
        payloads[-1][2]['external.pth'] = extra_pth
    for name, version, payload, requires in payloads:
        install_wheel(wheel(wheels, name, version, payload, requires), venv)
    site = venv / 'Lib/site-packages'
    # pip's installed metadata and compiled caches are not runtime wheel inputs.
    (site / 'cellpose/__pycache__').mkdir()
    (site / 'cellpose/__pycache__/models.cpython-312.pyc').write_bytes(b'nonportable cache')
    (site / 'cellpose-3.1.1.3.dist-info/INSTALLER').write_text('pip\n')
    (site / 'cellpose-3.1.1.3.dist-info/REQUESTED').write_text('')
    (site / 'pip-25.0.dist-info').mkdir()
    (site / 'pip-25.0.dist-info/METADATA').write_text('Metadata-Version: 2.3\nName: pip\nVersion: 25.0\n')
    (site / 'pip').mkdir()
    (site / 'pip/__init__.py').write_text('# bootstrap installer\n')
    (home / 'dependencies.txt').write_text(''.join(f'{name}=={version}\n' for name, version, _, _ in payloads))
    (home / 'models').mkdir()
    specs, receipts = {}, []
    for name in ('cyto3', 'nucleitorch_0'):
        content = ('synthetic-weight-' + name).encode()
        specs[name] = {'bytes': len(content), 'sha256': digest(content), 'source_url': 'https://www.cellpose.org/models/' + name}
        (home / 'models' / name).write_bytes(content)
        receipts.append({'model': 'nuclei' if name == 'nucleitorch_0' else name, 'bytes': len(content),
                         'sha256': digest(content), 'url': specs[name]['source_url']})
    monkeypatch.setattr(builder, 'CELLPOSE_MODELS', specs)
    (home / 'models.json').write_text(json.dumps(receipts))
    foundation = {
        'runtime/python.exe': pe_header(), 'runtime/pythonw.exe': pe_header(), 'runtime/python312.dll': pe_header(),
        'runtime/python312.zip': b'synthetic embedded standard library',
        'runtime/python312._pth': b'python312.zip\n.\nLib/site-packages\n../app\nimport site\n',
        'runtime/LICENSE.txt': b'Original Python license fixture\n',
        'runtime/Lib/site-packages/numpy/__init__.py': b'MUST NOT COPY MAIN NUMPY ABI',
        'runtime/Scripts/unrelated.py': b'MUST NOT COPY MAIN SCRIPT',
        'licenses/Microsoft-VC-Runtime/licenses/Microsoft-VC-Redist-License.txt': b'Original Microsoft notice fixture\n',
    }
    manifest = {'application': 'Figure Workbench', 'version': '0.4.0',
                'python': {'version': builder.PYTHON_VERSION, 'source': builder.PYTHON_URL, 'sha256': builder.PYTHON_SHA256},
                'files': [{'path': path, 'bytes': len(content), 'sha256': digest(content)} for path, content in foundation.items()]}
    base = tmp_path / 'verified-base.zip'
    with ZipFile(base, 'w', compression=ZIP_DEFLATED) as archive:
        prefix = 'Figure_Workbench_0.4.0_Windows_x64/'
        for path, content in foundation.items():
            archive.writestr(prefix + path, content)
        archive.writestr(prefix + 'WINDOWS_BUILD_MANIFEST.json', json.dumps(manifest))
    monkeypatch.setattr(builder, 'BASE_MAIN_SHA256', builder.sha256(base))
    return home, wheels, base, tmp_path / 'candidate'


def test_candidate_isolated_modules_match_tested_environment_and_zip_has_installable_root(tmp_path, monkeypatch):
    home, wheels, base, output = fixture(tmp_path, monkeypatch)
    original_numpy = (home / 'venv/Lib/site-packages/numpy/__init__.py').read_bytes()
    report = builder.build(base, home, output, wheels=wheels)
    assert report['portable_runtime_executed'] is False and report['release_gate_passed'] is False
    assert report['dependency_count'] == 4
    assert (output / 'cellpose/runtime/Lib/site-packages/numpy/__init__.py').read_bytes() == original_numpy
    assert (home / 'venv/Lib/site-packages/numpy/__init__.py').read_bytes() == original_numpy
    assert (output / 'cellpose/runtime/python312._pth').read_bytes() == builder.ISOLATED_PTH
    assert not (output / 'cellpose/runtime/Scripts/unrelated.py').exists()
    assert not (output / 'cellpose/runtime/Lib/site-packages/pip').exists()
    assert not list(output.rglob('*.pyc'))
    assert not list(output.rglob('pyvenv.cfg'))
    manifest = json.loads((output / builder.MANIFEST).read_text())
    assert manifest['foundation']['main_scientific_packages_copied'] is False
    assert manifest['tested_environment_comparison']['module_resource_bytes_equal'] is True
    assert all(record['sha256'] and record['tested_environment_metadata']['installed_record_sha256'] for record in manifest['dependencies'])
    assert builder.verify_candidate(output)['source_manifest_sha256'] == report['source_manifest_sha256']
    archive = output.parent / builder.CPU_INTERMEDIATE
    with ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        assert 'cellpose/runtime/python.exe' in zipped.namelist()
        assert builder.MANIFEST in zipped.namelist()
        assert not any(name.startswith('candidate/') for name in zipped.namelist())
    assert report['archive']['sha256'] == builder.sha256(archive)


def test_tested_module_tampering_is_rejected_without_a_candidate(tmp_path, monkeypatch):
    home, wheels, base, output = fixture(tmp_path, monkeypatch)
    (home / 'venv/Lib/site-packages/numpy/__init__.py').write_text('modified after testing\n')
    with pytest.raises(ValueError, match='differs from downloaded wheel'):
        builder.build(base, home, output, wheels=wheels)
    assert not output.exists()
    assert not (output.parent / builder.CPU_INTERMEDIATE).exists()


@pytest.mark.parametrize('content', [b'C:\\runner\\tested\\Lib\\site-packages\n', b'../../outside\n',
                                    b'import sys; sys.path.append("C:/runner")\n'])
def test_external_or_unreviewed_pth_cannot_enter_portable_candidate(tmp_path, monkeypatch, content):
    home, wheels, base, output = fixture(tmp_path, monkeypatch, extra_pth=content)
    with pytest.raises(ValueError, match='Unsafe|Unreviewed|External'):
        builder.build(base, home, output, wheels=wheels)
    assert not output.exists()


def test_changed_model_or_freeze_does_not_get_relabelled_as_tested(tmp_path, monkeypatch):
    home, wheels, base, output = fixture(tmp_path, monkeypatch)
    (home / 'models/cyto3').write_bytes(b'wrong weights')
    with pytest.raises(ValueError, match='Official model hash/length mismatch'):
        builder.build(base, home, output, wheels=wheels)
    assert not output.exists()
    (home / 'dependencies.txt').write_text('cellpose==3.1.1.3\nnumpy==2.1.0\n')
    with pytest.raises(ValueError, match='complete tested freeze'):
        builder.inspect_environment(home)


def test_wheel_payload_not_covered_by_record_is_rejected(tmp_path, monkeypatch):
    home, wheels, base, output = fixture(tmp_path, monkeypatch)
    target = next(wheels.glob('numpy-*.whl'))
    with ZipFile(target, 'a') as archive:
        archive.writestr('unrecorded.py', b'not authenticated by wheel RECORD\n')
    with pytest.raises(ValueError, match='Every wheel payload'):
        builder.build(base, home, output, wheels=wheels)
    assert not output.exists()


def test_candidate_verifier_rejects_postbuild_changes(tmp_path, monkeypatch):
    home, wheels, base, output = fixture(tmp_path, monkeypatch)
    builder.build(base, home, output, wheels=wheels, make_zip=False)
    (output / 'cellpose/models/cyto3').write_bytes(b'changed after build')
    with pytest.raises(ValueError, match='Candidate file was modified'):
        builder.verify_candidate(output)


def test_modified_base_cannot_supply_an_interpreter(tmp_path, monkeypatch):
    home, wheels, base, output = fixture(tmp_path, monkeypatch)
    with base.open('ab') as stream:
        stream.write(b'not the verified archive')
    with pytest.raises(ValueError, match='verified 0.4.0 main ZIP'):
        builder.build(base, home, output, wheels=wheels)
    assert not output.exists()


def test_modified_wheel_payload_fails_its_original_record(tmp_path, monkeypatch):
    home, wheels, base, output = fixture(tmp_path, monkeypatch)
    path = next(wheels.glob('numpy-*.whl'))
    with ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    contents['numpy/__init__.py'] = b'replaced after wheel RECORD was written\n'
    with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    with pytest.raises(ValueError, match='Wheel RECORD integrity failure'):
        builder.build(base, home, output, wheels=wheels)
    assert not output.exists()


def test_nonisolated_or_wrong_python_environment_is_rejected(tmp_path, monkeypatch):
    home, _, _, _ = fixture(tmp_path, monkeypatch)
    (home / 'venv/pyvenv.cfg').write_text('version = 3.12.14\ninclude-system-site-packages = false\n')
    with pytest.raises(ValueError, match='isolated Windows Python 3.12.10'):
        builder.inspect_environment(home)


def test_local_editable_dependency_is_rejected(tmp_path, monkeypatch):
    home, _, _, _ = fixture(tmp_path, monkeypatch)
    (home / 'venv/Lib/site-packages/cellpose-3.1.1.3.dist-info/direct_url.json').write_text(
        json.dumps({'url': 'file:///C:/runner/custom-cellpose', 'dir_info': {'editable': True}}))
    with pytest.raises(ValueError, match='Local/editable'):
        builder.inspect_environment(home)
