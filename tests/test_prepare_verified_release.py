"""Fail-closed publication preparation using complete synthetic acceptance evidence.

These tests perform no network requests or GitHub writes. The existing release
gate is executed, with only the current Git checkout identity replaced by a
fixture so mismatched evidence and modified files exercise the real checks.
"""
import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from scripts import prepare_verified_release as release


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False) + '\n').encode()


def zip_bytes(files):
    output = BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in files.items():
            # ZipInfo normalizes backslashes on Windows and truncates NULs.
            # Preserve the actual hostile bytes in fixtures instead of testing
            # an already-sanitized archive that production rightly accepts.
            info = ZipInfo(name)
            info.filename = info.orig_filename = name
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    commit, repository, run_id = 'a' * 40, 'tuantuan007/figure-workbench', 901
    identity = {'commit': commit, 'tracked_files_sha256': 'b' * 64, 'clean': True}
    monkeypatch.setattr(release.release_gate, 'source_identity', lambda: identity)
    packages = {
        release.FULL: zip_bytes({f'Figure_Workbench_{release.VERSION}_Windows_x64/FigureWorkbench.exe': b'SYNTHETIC PE'}),
        release.SOURCE: zip_bytes({f'Figure_Workbench_{release.VERSION}_Source/app.py': b'# SYNTHETIC source\n'}),
        release.BROWSER: b'<!doctype html><title>SYNTHETIC offline studio</title>',
    }
    records = {name: {'file': name, 'bytes': len(data), 'sha256': digest(data)} for name, data in packages.items()}
    component_record = {'file': release.CPU_INTERMEDIATE, 'bytes': 100, 'sha256': 'f' * 64}
    files = {'python.xml': b'<testsuites><testsuite>'
        b'<testcase name="test_real_official_model_inference_preserves_input[nuclei]"/>'
        b'<testcase name="test_real_official_model_inference_preserves_input[cyto3]"/>'
        b'</testsuite></testsuites>',
        'node.xml': b'<testsuites><testsuite><testcase name="frontend"/></testsuite></testsuites>',
        'lint.log': b'Lint completed successfully.\n',
        'acceptance/browser_onboarding/synthetic.png': b'SYNTHETIC SCREENSHOT',
    }
    files['portable-models.xml'] = files['python.xml']
    reports = {
        'python-checks.json': {'passed': True, 'source': identity, 'test_count': 2, 'junit_sha256': digest(files['python.xml'])},
        'frontend-checks.json': {'passed': True, 'lint_passed': True, 'node_passed': True, 'source': identity,
            'test_count': 1, 'skipped': 0, 'junit_sha256': digest(files['node.xml']), 'lint_sha256': digest(files['lint.log'])},
        'acceptance/browser-acceptance.json': {'passed': True, 'real_browser': True, 'source': identity,
            'cellpose_component_manifest_sha256': 'c' * 64, 'suites': []},
        'desktop/native-acceptance.json': {'passed': True, 'platform': 'win32', 'native_window_verified': True,
            'backend_stopped': True, 'source_commit': commit, 'source_identity': identity,
            'tested_application_sha256': 'd' * 64, 'source_snapshot_sha256': 'e' * 64,
            'cellpose_component_manifest_sha256': 'c' * 64, 'scenarios': [{'name': 'synthetic native scenario', 'passed': True}]},
        'windows-build.json': {'version': release.VERSION, 'edition': 'complete', 'source_identity': identity,
            'native_launcher': {'sha256': 'd' * 64}, 'source_snapshot_sha256': 'e' * 64,
            'archives': [records[release.FULL], records[release.SOURCE]],
            'full_toolkit_manifest_sha256': '1' * 64, 'full_toolkit_files': 20,
            'cellpose_component': {'bundled': True, 'source_manifest_sha256': 'c' * 64, 'sha256': component_record['sha256']}},
        'portable-cellpose-build.json': {'version': release.VERSION, 'source_manifest_sha256': 'c' * 64,
            'archive': component_record},
        'portable-cellpose-acceptance.json': {'passed': True, 'source': identity, 'platform': 'win32',
            'portable_runtime_executed': True, 'package_unchanged_after_execution': True,
            'source_manifest_sha256': 'c' * 64, 'junit_sha256': digest(files['portable-models.xml']),
            'scenarios': [{'name': 'synthetic portable scenario', 'passed': True}]},
    }
    extraction = {'passed': True, 'extracted_from_final_archive': True, 'archive': records[release.FULL],
        'full_toolkit_manifest_sha256': '1' * 64, 'verified_files': 20, 'source_identity': identity,
        'verified_directory': {'manifest_sha256': '1' * 64, 'verified_files': 20,
            'source_identity': identity, 'source_snapshot_sha256': 'e' * 64,
            'launcher_sha256': 'd' * 64, 'cellpose_component_manifest_sha256': 'c' * 64}}
    reports['full-toolkit-extraction.json'] = extraction
    for name in ('desktop/native-acceptance.json', 'portable-cellpose-acceptance.json'):
        reports[name].update(full_toolkit_archive=records[release.FULL], full_toolkit_manifest_sha256='1' * 64,
            full_toolkit_extraction_receipt_sha256=digest(encoded(extraction)), verified_full_toolkit_files=20,
            full_toolkit_unchanged_after_execution=True)
    reports['acceptance/browser-acceptance.json'].update(full_toolkit_archive_sha256=records[release.FULL]['sha256'],
        full_toolkit_manifest_sha256='1' * 64)
    for suite in release.release_gate.SUITES:
        name = f"acceptance/{Path(suite).stem}/{release.release_gate.REPORT_NAMES.get(suite, 'browser-report.json')}"
        report = {'passed': True, 'browser_errors': [], 'scenarios': [{'name': 'synthetic actual scenario', 'passed': True}]}
        if suite == 'test_browser_studio_ui.py':
            report.update(html_sha256=records[release.BROWSER]['sha256'], html_bytes=records[release.BROWSER]['bytes'])
        reports[name] = report
        reports['acceptance/browser-acceptance.json']['suites'].append({
            'suite': suite, 'passed': True, 'evidence_sha256': digest(encoded(report))})
    files.update({name: encoded(value) for name, value in reports.items()})
    metadata = {'schema_version': 1, 'repository': repository, 'repository_id': 123,
        'workflow': {'id': 10, 'path': release.ACCEPTANCE_WORKFLOW},
        'run': {'id': run_id, 'run_attempt': 1, 'workflow_id': 10, 'path': release.ACCEPTANCE_WORKFLOW,
            'head_sha': commit, 'head_branch': release.ACCEPTANCE_BRANCH, 'event': 'push',
            'status': 'completed', 'conclusion': 'success',
            'repository': {'id': 123, 'full_name': repository}, 'head_repository': {'id': 123, 'full_name': repository},
            'html_url': f'https://github.com/{repository}/actions/runs/{run_id}',
            'run_started_at': '2026-01-01T12:00:00Z', 'updated_at': '2026-01-01T12:02:00Z'},
        'artifacts': []}
    for aid, prefix in [(201, 'acceptance-evidence'), (202, 'verified-candidate')]:
        metadata['artifacts'].append({'id': aid, 'name': f'{prefix}-{commit}', 'expired': False,
            'created_at': '2026-01-01T12:01:00Z', 'archive_download_url': f'https://api.github.com/repos/{repository}/actions/artifacts/{aid}/zip',
            'workflow_run': {'id': run_id, 'head_sha': commit, 'head_branch': release.ACCEPTANCE_BRANCH,
                'repository_id': 123, 'head_repository_id': 123}})
    paths = {'metadata': tmp_path / 'github.json', 'evidence_archive': tmp_path / 'evidence.zip',
             'candidate_archive': tmp_path / 'candidate.zip', 'output': tmp_path / 'prepared'}

    def materialize():
        for key, content, artifact in [('evidence_archive', files, metadata['artifacts'][0]),
                                        ('candidate_archive', packages, metadata['artifacts'][1])]:
            data = zip_bytes(content)
            paths[key].write_bytes(data)
            artifact.update(size_in_bytes=len(data), digest='sha256:' + digest(data))
        paths['metadata'].write_bytes(encoded(metadata))

    def prepare():
        return release.prepare(paths['metadata'], paths['evidence_archive'], paths['candidate_archive'], paths['output'],
            repository=repository, expected_commit=commit, expected_run_id=run_id)

    materialize()
    return {'paths': paths, 'identity': identity, 'metadata': metadata, 'files': files, 'reports': reports,
            'packages': packages, 'records': records, 'materialize': materialize, 'prepare': prepare,
            'repository': repository, 'commit': commit, 'run_id': run_id}


def test_complete_verified_files_are_delivered_byte_for_byte_with_auditable_receipts(bundle):
    result = bundle['prepare']()
    output = bundle['paths']['output']
    assert {item['file'] for item in result['assets']} == release.ATTACHMENTS
    for name, expected in bundle['packages'].items():
        assert (output / 'candidates' / name).read_bytes() == expected
    assert (output / release.QA).read_bytes() == bundle['paths']['evidence_archive'].read_bytes()
    receipt = release.load_object(output / release.RECEIPT)
    assert receipt['published'] is False and receipt['candidate_bytes_repacked'] is False
    assert receipt['release_gate']['passed'] and receipt['source_identity'] == bundle['identity']
    assert receipt['test_counts'] == {'python': 2, 'frontend': 1, 'browser_suites': len(release.release_gate.SUITES),
        'browser_scenarios': len(release.release_gate.SUITES), 'native_scenarios': 1, 'portable_official_models': 2}
    checksums = dict(line.split('  ', 1)[::-1] for line in (output / release.CHECKSUMS).read_text().splitlines())
    assert set(checksums) == release.PACKAGES | {release.QA, release.RECEIPT}
    for record in result['assets']:
        path = output / record['path']
        assert release.sha256(path) == record['sha256'] and path.stat().st_size == record['bytes']
        if record['file'] in checksums:
            assert checksums[record['file']] == record['sha256']
    assert '独立浏览器组图版' in (output / 'RELEASE_NOTES.md').read_text('utf-8')
    assert bundle['commit'] in (output / 'RELEASE_NOTES.md').read_text('utf-8')


@pytest.mark.parametrize('name', sorted(release.PACKAGES))
def test_authenticated_candidate_with_changed_inner_file_is_rejected(bundle, name):
    bundle['packages'][name] += b'changed after tests'
    bundle['materialize']()  # Outer digest is valid, but tested inner bytes differ.
    with pytest.raises(release.VerificationError, match='Tested candidate bytes'):
        bundle['prepare']()
    assert not bundle['paths']['output'].exists()


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'nested', 'case'])
def test_candidate_file_names_are_an_exact_root_level_allowlist(bundle, mutation):
    packages = bundle['packages']
    if mutation == 'missing':
        packages.pop(release.BROWSER)
    elif mutation == 'extra':
        packages['unreviewed.exe'] = b'extra'
    elif mutation == 'nested':
        packages['dist/' + release.BROWSER] = packages.pop(release.BROWSER)
    else:
        packages[release.BROWSER.lower()] = packages.pop(release.BROWSER)
    bundle['materialize']()
    with pytest.raises(release.VerificationError, match='exactly the three'):
        bundle['prepare']()


@pytest.mark.parametrize('name', ['python.xml', 'portable-models.xml', 'desktop/native-acceptance.json',
                                'acceptance/browser_onboarding/onboarding-report.json'])
def test_missing_required_evidence_cannot_be_replaced_by_a_summary(bundle, name):
    del bundle['files'][name]
    bundle['materialize']()
    with pytest.raises(release.VerificationError, match='Release gate rejected'):
        bundle['prepare']()


def test_changed_browser_result_fails_even_if_summary_still_claims_success(bundle):
    name = 'acceptance/browser_composition/report.json'
    report = copy.deepcopy(bundle['reports'][name])
    report['scenarios'][0]['passed'] = False
    bundle['files'][name] = encoded(report)
    bundle['materialize']()
    with pytest.raises(release.VerificationError, match='Release gate rejected'):
        bundle['prepare']()


def test_current_checkout_must_match_the_tested_source_and_be_clean(bundle):
    bundle['identity']['clean'] = False
    with pytest.raises(release.VerificationError, match='Release gate rejected'):
        bundle['prepare']()


@pytest.mark.parametrize('field,value', [
    ('head_sha', 'f' * 40), ('id', 902), ('head_branch', 'main'), ('path', '.github/workflows/other.yml'),
    ('workflow_id', 11), ('status', 'in_progress'), ('conclusion', 'failure'), ('event', 'pull_request'),
    ('run_attempt', 0), ('html_url', 'https://github.com/other/repo/actions/runs/901'),
])
def test_wrong_run_sha_branch_workflow_or_completion_blocks_preparation(bundle, field, value):
    bundle['metadata']['run'][field] = value
    bundle['materialize']()
    with pytest.raises(release.VerificationError):
        bundle['prepare']()
    assert not bundle['paths']['output'].exists()


@pytest.mark.parametrize('field,value', [('id', 902), ('head_sha', 'f' * 40), ('head_branch', 'other'),
                                      ('repository_id', 999), ('head_repository_id', 999)])
def test_same_named_artifact_from_another_run_or_fork_is_rejected(bundle, field, value):
    bundle['metadata']['artifacts'][0]['workflow_run'][field] = value
    bundle['materialize']()
    with pytest.raises(release.VerificationError, match='Artifact run'):
        bundle['prepare']()


@pytest.mark.parametrize('field,value', [('expired', True), ('digest', None), ('digest', 'sha256:' + '0' * 64),
    ('size_in_bytes', 0), ('size_in_bytes', 999), ('created_at', '2025-12-31T12:00:00Z'),
    ('name', 'verified-candidate-wrong-sha')])
def test_missing_digest_expiry_changed_bytes_or_wrong_artifact_identity_fails(bundle, field, value):
    bundle['metadata']['artifacts'][0][field] = value
    bundle['paths']['metadata'].write_bytes(encoded(bundle['metadata']))
    with pytest.raises(release.VerificationError):
        bundle['prepare']()


def test_raw_download_corruption_fails_before_any_extraction(bundle):
    with bundle['paths']['candidate_archive'].open('ab') as target:
        target.write(b'corrupt transport')
    with pytest.raises(release.VerificationError, match='Artifact byte count'):
        bundle['prepare']()
    assert not bundle['paths']['output'].exists()


@pytest.mark.parametrize('name', ['../outside.txt', '/absolute.txt', r'nested\escape.txt',
                                'report.json:stream', 'NUL.txt', 'nested/report.json\x00ignored'])
def test_authenticated_evidence_zip_still_rejects_unsafe_windows_paths(bundle, name):
    bundle['files'][name] = b'unsafe'
    bundle['materialize']()
    with ZipFile(bundle['paths']['evidence_archive']) as archive:
        assert name in [info.orig_filename for info in archive.infolist()]
    with pytest.raises(release.VerificationError, match='Unsafe artifact archive path'):
        bundle['prepare']()
    assert not (bundle['paths']['output'].parent / 'outside.txt').exists()
    assert not bundle['paths']['output'].exists()


def test_member_names_normalized_by_windows_cannot_hide_the_original_path():
    with ZipFile(BytesIO(zip_bytes({'nested/escape.txt': b'unsafe'}))) as archive:
        # Reproduce ZipInfo's state when it reads a raw backslash name on
        # Windows without patching global OS/path behaviour on other hosts.
        archive.getinfo('nested/escape.txt').orig_filename = r'nested\escape.txt'
        with pytest.raises(release.VerificationError, match='Unsafe artifact archive path'):
            release.safe_members(archive)


def test_archive_symlinks_are_not_extracted(tmp_path):
    path = tmp_path / 'links.zip'
    info = ZipInfo('link')
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    with ZipFile(path, 'w') as archive:
        archive.writestr(info, '/outside')
    with pytest.raises(release.VerificationError, match='linked or special'):
        release.extract_verified(path, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_duplicate_json_keys_are_not_silently_overwritten(tmp_path):
    path = tmp_path / 'metadata.json'
    path.write_text('{"head_sha":"old","head_sha":"new"}')
    with pytest.raises(release.VerificationError, match='Duplicate JSON'):
        release.load_object(path)


@pytest.mark.parametrize('change', ['main-name', 'cpu-name', 'html-size', 'html-hash'])
def test_build_records_must_identify_the_actual_three_files(bundle, change):
    if change == 'main-name':
        name = 'windows-build.json'
        report = copy.deepcopy(bundle['reports'][name])
        report['archives'][0]['file'] = 'Other.zip'
    elif change == 'cpu-name':
        name = 'portable-cellpose-build.json'
        report = copy.deepcopy(bundle['reports'][name])
        report['archive']['file'] = 'Other-CPU.zip'
    else:
        name = 'acceptance/test_browser_studio_ui/report.json'
        report = copy.deepcopy(bundle['reports'][name])
        report['html_bytes' if change == 'html-size' else 'html_sha256'] = 1 if change == 'html-size' else 'f' * 64
        summary_name = 'acceptance/browser-acceptance.json'
        summary = copy.deepcopy(bundle['reports'][summary_name])
        next(item for item in summary['suites'] if item['suite'] == 'test_browser_studio_ui.py')['evidence_sha256'] = digest(encoded(report))
        bundle['files'][summary_name] = encoded(summary)
    bundle['files'][name] = encoded(report)
    bundle['materialize']()
    with pytest.raises(release.VerificationError):
        bundle['prepare']()


def test_preparation_never_overwrites_previous_output(bundle):
    bundle['prepare']()
    receipt = (bundle['paths']['output'] / release.RECEIPT).read_bytes()
    with pytest.raises(release.VerificationError, match='never overwritten'):
        bundle['prepare']()
    assert (bundle['paths']['output'] / release.RECEIPT).read_bytes() == receipt


def remote_fixture(bundle):
    manifest = bundle['prepare']()
    repo, commit = bundle['repository'], bundle['commit']
    remote = {'id': 301, 'url': f'https://api.github.com/repos/{repo}/releases/301',
              'tag_name': release.TAG, 'target_commitish': commit, 'draft': True, 'prerelease': False,
              'assets': [{'id': 400 + i, 'name': item['file'], 'size': item['bytes'],
                          'digest': 'sha256:' + item['sha256'], 'state': 'uploaded'}
                         for i, item in enumerate(manifest['assets'])]}
    tag = {'ref': f'refs/tags/{release.TAG}', 'object': {'type': 'commit', 'sha': commit}}
    base = bundle['paths']['output'].parent
    remote_path, tag_path = base / 'remote.json', base / 'tag.json'

    def verify():
        remote_path.write_bytes(encoded(remote))
        tag_path.write_bytes(encoded(tag))
        return release.verify_upload(bundle['paths']['output'], remote_path, tag_path,
            repository=repo, expected_commit=commit)

    return remote, tag, verify


def test_remote_draft_checks_all_six_uploads_and_exact_target(bundle):
    _, _, verify = remote_fixture(bundle)
    result = verify()
    assert result['passed'] and result['draft_verified'] and result['published'] is False
    assert result['git_tag_created'] and len(result['assets']) == 6


def test_new_draft_can_defer_tag_creation_until_publication(bundle):
    _, tag, verify = remote_fixture(bundle)
    tag.clear()
    tag.update(status=404, exists=False, repository=bundle['repository'], ref=f'refs/tags/{release.TAG}')
    result = verify()
    assert result['passed'] and result['git_tag_created'] is False


@pytest.mark.parametrize('field,value', [('name', 'wrong.zip'), ('size', 0), ('digest', None),
                                      ('digest', 'sha256:' + '0' * 64), ('state', 'starter')])
def test_incomplete_changed_or_misnamed_remote_asset_keeps_draft_unverified(bundle, field, value):
    remote, _, verify = remote_fixture(bundle)
    remote['assets'][0][field] = value
    with pytest.raises(release.VerificationError, match='Remote attachment'):
        verify()
    assert not (bundle['paths']['output'] / 'upload-verification.json').exists()


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'duplicate', 'published', 'target', 'repository', 'tag'])
def test_wrong_remote_release_or_tag_never_authorizes_publication(bundle, mutation):
    remote, tag, verify = remote_fixture(bundle)
    if mutation == 'extra':
        remote['assets'].append({**remote['assets'][0], 'name': 'extra.exe'})
    elif mutation == 'missing':
        remote['assets'].pop()
    elif mutation == 'duplicate':
        remote['assets'][-1] = remote['assets'][0]
    elif mutation == 'published':
        remote['draft'] = False
    elif mutation == 'target':
        remote['target_commitish'] = 'main'
    elif mutation == 'repository':
        remote['url'] = 'https://api.github.com/repos/other/repo/releases/301'
    else:
        tag['object']['sha'] = 'f' * 40
    with pytest.raises(release.VerificationError):
        verify()


def test_local_attachment_tampering_after_preparation_is_rejected(bundle):
    _, _, verify = remote_fixture(bundle)
    path = bundle['paths']['output'] / 'candidates' / release.FULL
    path.write_bytes(path.read_bytes() + b'changed before upload')
    with pytest.raises(release.VerificationError, match='changed after verification'):
        verify()
