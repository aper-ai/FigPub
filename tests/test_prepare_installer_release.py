"""Offline publication boundaries; no Windows execution or remote writes."""
import copy
import json

import pytest

from scripts import prepare_installer_release as installer_release
from test_prepare_verified_release import bundle


@pytest.fixture
def installer_candidate(bundle, tmp_path):
    commit = bundle['commit']
    installer = tmp_path / installer_release.INSTALLER
    installer.write_bytes(b'MZ\0synthetic installer used only by offline verification tests')
    build = {
        'schema_version': 1, 'version': installer_release.VERSION,
        'source_identity': bundle['identity'],
        'payload_subdirectory': f'versions/{installer_release.VERSION}-{commit[:12]}',
        'payload_manifest_sha256': '1' * 64, 'verified_payload_files': 20,
        'installer': installer_release.verified.file_record(installer),
        'compiler': {'version': '6.7.1', 'sha256': 'c' * 64},
        'webview2_bootstrapper': {'sha256': 'd' * 64},
        'windows_installer_executed': False,
        'native_application_acceptance_passed': False,
        'packaged_model_inference_executed': False,
    }
    smoke = {
        'schema_version': 1, 'source_commit': commit,
        'installer_sha256': build['installer']['sha256'], 'passed': True,
        'scenarios': [{'name': name, 'passed': True}
                      for name in sorted(installer_release.REQUIRED_INSTALLER_SCENARIOS)],
        'windows_installer_executed': True,
        'native_application_acceptance_passed': False,
        'packaged_model_inference_executed': False,
    }
    paths = {'installer': installer, 'build_record': tmp_path / 'installer-build.json',
             'smoke_record': tmp_path / 'installer-smoke.json', 'output': tmp_path / 'installer-prepared'}

    def materialize():
        installer_release.verified.write_json(paths['build_record'], build)
        installer_release.verified.write_json(paths['smoke_record'], smoke)

    def prepare(*, prerelease=True, formal=False):
        materialize()
        return installer_release.prepare(**paths, repository=bundle['repository'], expected_commit=commit,
            tag=installer_release.TAG + '-rc.1' if prerelease else installer_release.TAG,
            prerelease=prerelease,
            verified_release_directory=bundle['paths']['output'] if formal else None)

    def remote():
        manifest = installer_release.verified.load_object(paths['output'] / 'upload-manifest.json')
        asset = manifest['assets'][0]
        release = {'id': 809, 'tag_name': manifest['tag'], 'target_commitish': commit,
            'draft': True, 'prerelease': manifest['prerelease'],
            'url': f"https://api.github.com/repos/{bundle['repository']}/releases/809",
            'assets': [{'name': asset['file'], 'state': 'uploaded', 'size': asset['bytes'],
                        'digest': 'sha256:' + asset['sha256']}]}
        tag = {'status': 404, 'exists': False, 'repository': bundle['repository'],
               'ref': 'refs/tags/' + manifest['tag']}
        return release, tag

    def verify(release, tag):
        release_path, tag_path = tmp_path / 'remote-release.json', tmp_path / 'remote-tag.json'
        installer_release.verified.write_json(release_path, release)
        installer_release.verified.write_json(tag_path, tag)
        return installer_release.verify_upload(paths['output'], release_path, tag_path,
            repository=bundle['repository'], expected_commit=commit)

    return {'paths': paths, 'build': build, 'smoke': smoke, 'prepare': prepare,
            'remote': remote, 'verify': verify, 'bundle': bundle}


def test_candidate_publishes_only_installer_and_keeps_truthful_scope(installer_candidate):
    candidate = installer_candidate
    manifest = candidate['prepare']()
    assert [asset['file'] for asset in manifest['assets']] == [installer_release.INSTALLER]
    assert manifest['release_gate_passed'] is False
    output = candidate['paths']['output']
    assert (output / installer_release.INSTALLER).read_bytes() == candidate['paths']['installer'].read_bytes()
    assert {item['file'] for item in manifest['internal_evidence']} == {'installer-build.json', 'installer-smoke.json'}
    notes = (output / 'RELEASE_NOTES.md').read_text('utf-8')
    assert '真实推理仍待验证' in notes and '仅在缺少 Runtime 时联网下载并安装' in notes
    assert candidate['verify'](*candidate['remote']())['passed'] is True


def test_stable_release_requires_original_six_artifacts_and_full_gate(installer_candidate):
    candidate = installer_candidate
    with pytest.raises(installer_release.VerificationError, match='full verified release directory'):
        candidate['prepare'](prerelease=False)
    original_manifest = candidate['bundle']['prepare']()
    original_path = candidate['bundle']['paths']['output'] / 'upload-manifest.json'
    original_bytes = original_path.read_bytes()
    manifest = candidate['prepare'](prerelease=False, formal=True)
    assert manifest['release_gate_passed'] is True
    assert len(manifest['assets']) == 1
    assert len(original_manifest['assets']) == 6 and original_path.read_bytes() == original_bytes
    assert candidate['verify'](*candidate['remote']())['prerelease'] is False


@pytest.mark.parametrize('mutation', ['payload', 'inner_evidence', 'original_package'])
def test_installer_cannot_replace_or_bypass_formal_acceptance(installer_candidate, mutation):
    candidate = installer_candidate
    candidate['bundle']['prepare']()
    original = candidate['bundle']['paths']['output']
    if mutation == 'payload':
        candidate['build']['payload_manifest_sha256'] = '9' * 64
    elif mutation == 'inner_evidence':
        (original / 'evidence/desktop/native-acceptance.json').unlink()
    else:
        with (original / 'candidates' / installer_release.FULL).open('ab') as stream:
            stream.write(b'changed after acceptance')
    with pytest.raises(installer_release.VerificationError):
        candidate['prepare'](prerelease=False, formal=True)
    assert not candidate['paths']['output'].exists()


@pytest.mark.parametrize('mutation', [
    'installer_bytes', 'source_commit', 'unclean', 'smoke_hash', 'smoke_commit',
    'not_executed', 'missing_uninstall', 'failed_project_preservation', 'claims_model_acceptance',
])
def test_only_exact_successful_installer_evidence_is_accepted(installer_candidate, mutation):
    candidate = installer_candidate
    build, smoke = candidate['build'], candidate['smoke']
    if mutation == 'installer_bytes':
        candidate['paths']['installer'].write_bytes(b'changed')
    elif mutation == 'source_commit':
        build['source_identity'] = dict(build['source_identity'], commit='f' * 40)
    elif mutation == 'unclean':
        build['source_identity'] = dict(build['source_identity'], clean=False)
    elif mutation == 'smoke_hash':
        smoke['installer_sha256'] = 'f' * 64
    elif mutation == 'smoke_commit':
        smoke['source_commit'] = 'f' * 40
    elif mutation == 'not_executed':
        smoke['windows_installer_executed'] = False
    elif mutation == 'missing_uninstall':
        smoke['scenarios'] = [item for item in smoke['scenarios'] if item['name'] != 'uninstall']
    elif mutation == 'failed_project_preservation':
        next(item for item in smoke['scenarios'] if item['name'] == 'project_preservation')['passed'] = False
    else:
        smoke['packaged_model_inference_executed'] = True
    with pytest.raises(installer_release.VerificationError):
        candidate['prepare']()


@pytest.mark.parametrize('mutation', ['extra_asset', 'different_digest', 'missing_digest',
    'wrong_size', 'already_published', 'wrong_commit', 'wrong_repository', 'wrong_tag', 'wrong_channel'])
def test_remote_publication_must_be_the_exact_single_installer_draft(installer_candidate, mutation):
    candidate = installer_candidate
    candidate['prepare']()
    release, tag = candidate['remote']()
    if mutation == 'extra_asset':
        release['assets'].append(copy.deepcopy(release['assets'][0]))
    elif mutation == 'different_digest':
        release['assets'][0]['digest'] = 'sha256:' + 'f' * 64
    elif mutation == 'missing_digest':
        del release['assets'][0]['digest']
    elif mutation == 'wrong_size':
        release['assets'][0]['size'] += 1
    elif mutation == 'already_published':
        release['draft'] = False
    elif mutation == 'wrong_commit':
        release['target_commitish'] = 'main'
    elif mutation == 'wrong_repository':
        release['url'] = 'https://api.github.com/repos/someone/other/releases/809'
    elif mutation == 'wrong_tag':
        tag = {'ref': tag['ref'], 'object': {'type': 'commit', 'sha': 'f' * 40}}
    else:
        release['prerelease'] = False
    with pytest.raises(installer_release.VerificationError):
        candidate['verify'](release, tag)


@pytest.mark.parametrize('target', ['installer', 'smoke', 'manifest_path'])
def test_local_bytes_and_paths_are_rechecked_before_publication(installer_candidate, target):
    candidate = installer_candidate
    candidate['prepare']()
    release, tag = candidate['remote']()
    output = candidate['paths']['output']
    if target == 'installer':
        (output / installer_release.INSTALLER).write_bytes(b'changed after preparation')
    elif target == 'smoke':
        (output / 'installer-smoke.json').write_text('{}\n', encoding='utf-8')
    else:
        path = output / 'upload-manifest.json'
        manifest = json.loads(path.read_text())
        manifest['assets'][0]['path'] = '../' + installer_release.INSTALLER
        installer_release.verified.write_json(path, manifest)
    with pytest.raises(installer_release.VerificationError):
        candidate['verify'](release, tag)


def test_existing_preparation_is_never_overwritten(installer_candidate):
    candidate = installer_candidate
    candidate['prepare']()
    before = (candidate['paths']['output'] / 'upload-manifest.json').read_bytes()
    with pytest.raises(installer_release.VerificationError, match='never overwritten'):
        candidate['prepare']()
    assert (candidate['paths']['output'] / 'upload-manifest.json').read_bytes() == before
