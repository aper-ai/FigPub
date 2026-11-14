"""Release archive acceptance boundaries; never executes a Windows binary."""
from io import BytesIO
import importlib.util
from pathlib import Path
import sys
import zipfile

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / 'packaging'
sys.path.insert(0, str(PACKAGING))
spec = importlib.util.spec_from_file_location('upgrade_windows', PACKAGING / 'upgrade_windows.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/root', 'a\\b', 'a//b',
                                   'CON.txt', 'dir/trailing.', 'dir/space ', 'a/../b'])
def test_portable_paths_cannot_alias_or_escape_on_windows(name):
    with pytest.raises(ValueError):
        upgrade.canonical_path(name)


def test_case_collisions_and_digest_mismatches_reject_before_use(tmp_path):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('runtime/A.dll', b'unchanged')
        archive.writestr('runtime/a.dll', b'other')
    with zipfile.ZipFile(buffer) as archive:
        with pytest.raises(ValueError, match='collision'):
            upgrade.inventory(archive)
        with pytest.raises(ValueError, match='digest'):
            upgrade.verify_member(archive, archive.getinfo('runtime/A.dll'),
                                  {'bytes': 9, 'sha256': '0' * 64})


@pytest.mark.parametrize('name', ['runtime\\escape.dll', 'runtime/escape.dll\x00hidden'])
def test_archive_inventory_rejects_original_unsafe_member_names(name):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        member = zipfile.ZipInfo(name)
        # Write literal member names, without the host's constructor cleanup.
        member.filename = member.orig_filename = name
        archive.writestr(member, b'bad')
    with zipfile.ZipFile(buffer) as archive:
        assert archive.infolist()[0].orig_filename == name
        with pytest.raises(ValueError):
            upgrade.inventory(archive)


@pytest.mark.parametrize('name', ['runtime/module.dll', 'runtime/'])
def test_archive_inventory_rejects_windows_normalization_before_skipping_directories(name):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(name, b'')
    with zipfile.ZipFile(buffer) as archive:
        # Reproduce Windows' normalization without changing global os.sep.
        archive.getinfo(name).orig_filename = name.replace('/', '\\')
        with pytest.raises(ValueError):
            upgrade.inventory(archive)


def test_zip_builder_round_trips_exact_bytes_and_refuses_replacement(tmp_path):
    directory = tmp_path / 'Portable'
    (directory / 'app').mkdir(parents=True)
    data = b'original file bytes\x00\xff'
    (directory / 'app/source.dat').write_bytes(data)
    archive = tmp_path / 'Portable.zip'
    record = upgrade.write_zip(directory, archive)
    assert record['crc_verified'] is True and record['entries'] == 1
    assert record['sha256'] == upgrade.sha256(archive)
    with zipfile.ZipFile(archive) as opened:
        assert opened.read('Portable/app/source.dat') == data
    with pytest.raises(ValueError, match='replace'):
        upgrade.write_zip(directory, archive)
