"""Ensure a one-time download cleanup cannot expand beyond approved assets."""
import copy
import hashlib
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import cleanup_release_downloads as cleanup


def legacy():
    return {'id': cleanup.LEGACY_RELEASE_ID, 'tag_name': cleanup.LEGACY_TAG,
            'draft': False, 'html_url': f'https://github.com/{cleanup.REPOSITORY}/releases/tag/{cleanup.LEGACY_TAG}',
            'assets': [{'id': cleanup.LEGACY_KEEP_ID, 'name': cleanup.LEGACY_KEEP_NAME,
                        'state': 'uploaded', 'digest': 'sha256:' + cleanup.LEGACY_KEEP_SHA256}]
            + [{'id': key, 'name': value} for key, value in cleanup.LEGACY_REMOVALS.items()]}


def test_cleanup_preserves_the_verified_legacy_windows_package_and_supports_partial_retry():
    release = legacy()
    planned = cleanup.legacy_plan(release)
    assert {item['id'] for item in planned} == set(cleanup.LEGACY_REMOVALS)
    assert cleanup.LEGACY_KEEP_ID not in {item['id'] for item in planned}
    release['assets'] = release['assets'][:1]
    assert cleanup.legacy_plan(release) == []


@pytest.mark.parametrize('change', ['other_release', 'unexpected_asset', 'renamed_asset', 'changed_keep', 'missing_keep'])
def test_cleanup_refuses_new_or_changed_assets_instead_of_guessing(change):
    release = legacy()
    if change == 'other_release':
        release['id'] += 1
    elif change == 'unexpected_asset':
        release['assets'].append({'id': 1, 'name': 'Unapproved.exe'})
    elif change == 'renamed_asset':
        release['assets'][1]['name'] = 'DoNotDelete.exe'
    elif change == 'changed_keep':
        release['assets'][0]['digest'] = 'sha256:' + '0' * 64
    else:
        release['assets'] = release['assets'][1:]
    with pytest.raises(ValueError):
        cleanup.legacy_plan(release)


def test_no_cleanup_is_allowed_until_the_single_public_installer_matches(tmp_path):
    installer = tmp_path / cleanup.SETUP
    installer.write_bytes(b'synthetic setup bytes')
    commit = 'a' * 40
    release = {'tag_name': cleanup.NEW_TAG, 'draft': False, 'prerelease': True,
               'target_commitish': commit,
               'html_url': f'https://github.com/{cleanup.REPOSITORY}/releases/tag/{cleanup.NEW_TAG}',
               'assets': [{'name': cleanup.SETUP, 'state': 'uploaded',
                           'size': installer.stat().st_size,
                           'digest': 'sha256:' + hashlib.sha256(installer.read_bytes()).hexdigest()}]}
    tag = {'ref': f'refs/tags/{cleanup.NEW_TAG}', 'object': {'type': 'commit', 'sha': commit}}
    cleanup.check_new_release(release, tag, installer, commit)
    for changed in [
        dict(release, draft=True),
        dict(release, target_commitish='b' * 40),
        dict(release, assets=release['assets'] * 2),
    ]:
        with pytest.raises(ValueError):
            cleanup.check_new_release(changed, tag, installer, commit)
    changed = copy.deepcopy(release)
    changed['assets'][0]['digest'] = 'sha256:' + '0' * 64
    with pytest.raises(ValueError):
        cleanup.check_new_release(changed, tag, installer, commit)
