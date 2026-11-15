import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from scripts import release_gate


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    identity = {'commit': 'a' * 40, 'tracked_files_sha256': 'b' * 64, 'clean': True}
    monkeypatch.setattr(release_gate, 'source_identity', lambda: identity)
    data = {
        'frontend-checks.json': {'source': identity, 'passed': True, 'lint_passed': True,
                                 'node_passed': True, 'test_count': 1, 'skipped': 0},
        'acceptance/browser-acceptance.json': {'source': identity, 'passed': True, 'real_browser': True,
            'cellpose_component_manifest_sha256': '1' * 64,
            'suites': [{'suite': suite, 'passed': True} for suite in release_gate.SUITES]},
        'desktop/native-acceptance.json': {'passed': True, 'platform': 'win32', 'native_window_verified': True,
            'cellpose_component_manifest_sha256': '1' * 64,
            'backend_stopped': True, 'source_commit': identity['commit'],
            'scenarios': [{'name': name, 'passed': True} for name in sorted(release_gate.NATIVE_REQUIRED_SCENARIOS)],
            'tested_application_sha256': 'c' * 64, 'source_snapshot_sha256': 'e' * 64, 'source_identity': identity},
        'windows-build.json': {'native_launcher': {'sha256': 'c' * 64},
                              'source_snapshot_sha256': 'e' * 64, 'source_identity': identity,
                              'cellpose_component': {'source_manifest_sha256': '1' * 64, 'sha256': '2' * 64}},
        'portable-cellpose-build.json': {'source_manifest_sha256': '1' * 64, 'archive': {'sha256': '2' * 64}},
        'portable-cellpose-acceptance.json': {'source': identity, 'passed': True, 'platform': 'win32',
            'portable_runtime_executed': True, 'source_manifest_sha256': '1' * 64,
            'package_unchanged_after_execution': True,
            'scenarios': [{'name': 'actual portable inference', 'passed': True}]},
    }
    full_archive = {'file': release_gate.FULL, 'bytes': 100, 'sha256': '4' * 64}
    full_manifest, full_count = '5' * 64, 20
    data['windows-build.json'].update(version=release_gate.VERSION, edition='complete',
        archives=[full_archive], full_toolkit_manifest_sha256=full_manifest, full_toolkit_files=full_count)
    data['windows-build.json']['cellpose_component']['bundled'] = True
    extraction = {'passed': True, 'extracted_from_final_archive': True, 'archive': full_archive,
        'full_toolkit_manifest_sha256': full_manifest, 'verified_files': full_count, 'source_identity': identity,
        'verified_directory': {'manifest_sha256': full_manifest, 'verified_files': full_count,
            'source_identity': identity, 'source_snapshot_sha256': 'e' * 64,
            'launcher_sha256': 'c' * 64, 'cellpose_component_manifest_sha256': '1' * 64}}
    data['full-toolkit-extraction.json'] = extraction
    extraction_hash = hashlib.sha256(json.dumps(extraction).encode()).hexdigest()
    for name in ('desktop/native-acceptance.json', 'portable-cellpose-acceptance.json'):
        data[name].update(full_toolkit_archive=full_archive, full_toolkit_manifest_sha256=full_manifest,
            full_toolkit_extraction_receipt_sha256=extraction_hash, verified_full_toolkit_files=full_count,
            full_toolkit_unchanged_after_execution=True)
    data['acceptance/browser-acceptance.json'].update(full_toolkit_archive_sha256=full_archive['sha256'],
        full_toolkit_manifest_sha256=full_manifest)
    (tmp_path / 'node.xml').write_text('<testsuites><testsuite><testcase name="frontend"/></testsuite></testsuites>')
    (tmp_path / 'lint.log').write_text('Lint completed successfully.')
    data['frontend-checks.json']['junit_sha256'] = hashlib.sha256((tmp_path / 'node.xml').read_bytes()).hexdigest()
    data['frontend-checks.json']['lint_sha256'] = hashlib.sha256((tmp_path / 'lint.log').read_bytes()).hexdigest()
    for item in data['acceptance/browser-acceptance.json']['suites']:
        name = f"acceptance/{release_gate.Path(item['suite']).stem}/{release_gate.REPORT_NAMES.get(item['suite'], 'browser-report.json')}"
        value = {'passed': True, 'scenarios': [{'name': 'actual scenario', 'passed': True}]}
        if item['suite'] in release_gate.OFFLINE_SUITES:
            value.update(entry=release_gate.BROWSER, real_browser=True, html_sha256='3' * 64, html_bytes=1234)
        data[name] = value
        item['evidence_sha256'] = hashlib.sha256(json.dumps(value).encode()).hexdigest()
    for name, value in data.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
    (tmp_path / 'python.xml').write_text('<testsuites><testsuite>'
        '<testcase name="test_real_official_model_inference_preserves_input[nuclei]"/>'
        '<testcase name="test_real_official_model_inference_preserves_input[cyto3]"/>'
        '</testsuite></testsuites>', encoding='utf-8')
    (tmp_path / 'python-checks.json').write_text(json.dumps({
        'source': identity, 'passed': True, 'test_count': 2,
        'junit_sha256': hashlib.sha256((tmp_path / 'python.xml').read_bytes()).hexdigest(),
    }), encoding='utf-8')
    (tmp_path / 'portable-models.xml').write_bytes((tmp_path / 'python.xml').read_bytes())
    data['portable-cellpose-acceptance.json']['junit_sha256'] = hashlib.sha256((tmp_path / 'portable-models.xml').read_bytes()).hexdigest()
    (tmp_path / 'portable-cellpose-acceptance.json').write_text(json.dumps(data['portable-cellpose-acceptance.json']))
    return tmp_path, identity, data


def test_complete_matching_evidence_passes(evidence):
    path, identity, _ = evidence
    assert release_gate.evaluate(path, identity['commit'])['passed']


def test_static_build_cannot_replace_native_window_acceptance(evidence):
    path, identity, data = evidence
    native = data['desktop/native-acceptance.json']
    native['native_window_verified'] = False
    (path / 'desktop/native-acceptance.json').write_text(json.dumps(native))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed'] and any('Native Windows window' in item for item in result['blockers'])


def test_old_source_report_and_missing_suite_block_release(evidence):
    path, identity, data = evidence
    browser = data['acceptance/browser-acceptance.json']
    browser['source'] = {**identity, 'commit': 'd' * 40}
    browser['suites'].pop()
    (path / 'acceptance/browser-acceptance.json').write_text(json.dumps(browser))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('different source' in item for item in result['blockers'])
    assert any('Every real browser' in item for item in result['blockers'])


def test_skipped_real_model_is_not_a_success(evidence):
    path, identity, _ = evidence
    (path / 'python.xml').write_text('<testsuites><testsuite><testcase name="inference"><skipped/>'
                                    '</testcase></testsuite></testsuites>')
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('Both official CPU models' in item for item in result['blockers'])


def test_failed_native_scenario_blocks_even_when_summary_claims_pass(evidence):
    path, identity, data = evidence
    native = data['desktop/native-acceptance.json']
    native['scenarios'].append({'name': 'save failed', 'passed': False})
    (path / 'desktop/native-acceptance.json').write_text(json.dumps(native))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('Native Windows window' in item for item in result['blockers'])


@pytest.mark.parametrize('scenario', sorted(release_gate.NATIVE_REQUIRED_SCENARIOS))
@pytest.mark.parametrize('failure', ['missing', 'duplicate', 'failed'])
def test_current_native_behaviors_cannot_be_omitted_or_replaced(evidence, scenario, failure):
    path, identity, data = evidence
    native = data['desktop/native-acceptance.json']
    selected = next(item for item in native['scenarios'] if item['name'] == scenario)
    if failure == 'missing':
        native['scenarios'].remove(selected)
    elif failure == 'duplicate':
        native['scenarios'].append(dict(selected))
    else:
        selected['passed'] = False
    (path / 'desktop/native-acceptance.json').write_text(json.dumps(native))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('Native Windows' in item for item in result['blockers'])


def test_changed_or_failed_browser_detail_blocks_summary_only_success(evidence):
    path, identity, data = evidence
    name = next(name for name in data if name.startswith('acceptance/browser_workflow/'))
    data[name]['scenarios'][0]['passed'] = False
    (path / name).write_text(json.dumps(data[name]))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('scenario evidence' in item for item in result['blockers'])


def test_dirty_checkout_cannot_publish(evidence):
    path, identity, _ = evidence
    identity['clean'] = False
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('Uncommitted source' in item for item in result['blockers'])


def test_same_launcher_with_different_packaged_app_is_blocked(evidence):
    path, identity, data = evidence
    data['windows-build.json']['source_snapshot_sha256'] = 'f' * 64
    (path / 'windows-build.json').write_text(json.dumps(data['windows-build.json']))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('complete desktop source' in item for item in result['blockers'])


def test_virtual_environment_success_cannot_replace_portable_cpu_execution(evidence):
    path, identity, data = evidence
    data['portable-cellpose-acceptance.json']['portable_runtime_executed'] = False
    (path / 'portable-cellpose-acceptance.json').write_text(json.dumps(data['portable-cellpose-acceptance.json']))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('portable CPU component' in item for item in result['blockers'])


def test_browser_using_another_model_runtime_cannot_pass_portable_gate(evidence):
    path, identity, data = evidence
    data['acceptance/browser-acceptance.json']['cellpose_component_manifest_sha256'] = None
    (path / 'acceptance/browser-acceptance.json').write_text(json.dumps(data['acceptance/browser-acceptance.json']))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('tested portable CPU component' in item for item in result['blockers'])


def test_native_using_another_model_runtime_cannot_pass_portable_gate(evidence):
    path, identity, data = evidence
    data['desktop/native-acceptance.json']['cellpose_component_manifest_sha256'] = None
    (path / 'desktop/native-acceptance.json').write_text(json.dumps(data['desktop/native-acceptance.json']))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('tested portable CPU component' in item for item in result['blockers'])


def test_frontend_summary_cannot_hide_failed_or_changed_test_detail(evidence):
    path, identity, _ = evidence
    (path / 'node.xml').write_text('<testsuites><testsuite><testcase name="frontend"><failure/></testcase></testsuite></testsuites>')
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('Frontend evidence' in item for item in result['blockers'])


@pytest.mark.parametrize('suite', ['test_browser_studio_ui.py', 'browser_auto_apply.py'])
def test_offline_browser_report_without_delivered_artifact_identity_is_blocked(evidence, suite):
    path, identity, data = evidence
    name = f'acceptance/{release_gate.Path(suite).stem}/report.json'
    del data[name]['html_sha256']
    encoded = json.dumps(data[name]).encode()
    (path / name).write_bytes(encoded)
    browser = data['acceptance/browser-acceptance.json']
    next(item for item in browser['suites'] if item['suite'] == suite)['evidence_sha256'] = hashlib.sha256(encoded).hexdigest()
    (path / 'acceptance/browser-acceptance.json').write_text(json.dumps(browser))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('exact tested HTML artifact' in item for item in result['blockers'])


@pytest.mark.parametrize('failure', ['omitted', 'missing-report', 'failed-scenario'])
def test_real_auto_apply_suite_cannot_be_omitted_or_replaced_by_success_summary(evidence, failure):
    path, identity, data = evidence
    suite = 'browser_auto_apply.py'
    assert suite in release_gate.SUITES
    browser = data['acceptance/browser-acceptance.json']
    report_name = 'acceptance/browser_auto_apply/report.json'
    if failure == 'omitted':
        browser['suites'] = [item for item in browser['suites'] if item['suite'] != suite]
    elif failure == 'missing-report':
        (path / report_name).unlink()
    else:
        report = data[report_name]
        report['scenarios'][0]['passed'] = False
        encoded = json.dumps(report).encode()
        (path / report_name).write_bytes(encoded)
        next(item for item in browser['suites'] if item['suite'] == suite)['evidence_sha256'] = hashlib.sha256(encoded).hexdigest()
    (path / 'acceptance/browser-acceptance.json').write_text(json.dumps(browser))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('browser' in item.lower() for item in result['blockers'])


@pytest.mark.parametrize(('field', 'value'), [('html_sha256', 'f' * 64), ('html_bytes', 1235)])
def test_standalone_suites_cannot_test_different_html_builds(evidence, field, value):
    path, identity, data = evidence
    name = 'acceptance/browser_auto_apply/report.json'
    data[name][field] = value
    encoded = json.dumps(data[name]).encode()
    (path / name).write_bytes(encoded)
    browser = data['acceptance/browser-acceptance.json']
    next(item for item in browser['suites'] if item['suite'] == 'browser_auto_apply.py')['evidence_sha256'] = hashlib.sha256(encoded).hexdigest()
    (path / 'acceptance/browser-acceptance.json').write_text(json.dumps(browser))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('same final HTML artifact' in item for item in result['blockers'])


@pytest.mark.parametrize(('report_name', 'field', 'value'), [
    ('windows-build.json', 'edition', 'main'),
    ('windows-build.json', 'archives', []),
    ('windows-build.json', 'full_toolkit_manifest_sha256', 'f' * 64),
    ('full-toolkit-extraction.json', 'extracted_from_final_archive', False),
    ('full-toolkit-extraction.json', 'archive', {'file': 'separately-assembled.zip', 'bytes': 100, 'sha256': '4' * 64}),
    ('desktop/native-acceptance.json', 'full_toolkit_extraction_receipt_sha256', '6' * 64),
    ('desktop/native-acceptance.json', 'full_toolkit_unchanged_after_execution', False),
    ('portable-cellpose-acceptance.json', 'full_toolkit_archive', {'file': 'component-only.zip', 'bytes': 100, 'sha256': '4' * 64}),
    ('portable-cellpose-acceptance.json', 'verified_full_toolkit_files', 19),
    ('portable-cellpose-acceptance.json', 'full_toolkit_unchanged_after_execution', False),
    ('acceptance/browser-acceptance.json', 'full_toolkit_archive_sha256', None),
    ('acceptance/browser-acceptance.json', 'full_toolkit_manifest_sha256', '7' * 64),
])
def test_separate_component_or_stale_final_zip_evidence_cannot_pass_release(evidence, report_name, field, value):
    path, identity, data = evidence
    data[report_name][field] = value
    (path / report_name).write_text(json.dumps(data[report_name]))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert any('complete Windows ZIP' in item or 'final ZIP' in item or 'full ZIP' in item
               or 'complete toolkit' in item for item in result['blockers'])


def test_extraction_receipt_modification_after_tests_is_not_hidden_by_equal_claims(evidence):
    path, identity, data = evidence
    receipt = data['full-toolkit-extraction.json']
    # All logical fields still agree, but the native and portable signatures
    # must continue to identify the actual receipt bytes they tested.
    (path / 'full-toolkit-extraction.json').write_text(json.dumps(receipt, indent=2))
    result = release_gate.evaluate(path, identity['commit'])
    assert not result['passed']
    assert sum('exact full ZIP extraction' in item for item in result['blockers']) == 2


def replace_model_junit(path, filename, names):
    document = ET.Element('testsuites')
    suite = ET.SubElement(document, 'testsuite')
    for name in names:
        ET.SubElement(suite, 'testcase', name=name)
    xml_path = path / filename
    xml_path.write_bytes(ET.tostring(document, encoding='utf-8'))
    report_path = path / ('python-checks.json' if filename == 'python.xml' else 'portable-cellpose-acceptance.json')
    report = json.loads(report_path.read_text('utf-8'))
    report['junit_sha256'] = hashlib.sha256(xml_path.read_bytes()).hexdigest()
    if filename == 'python.xml':
        report['test_count'] = len(names)
    report_path.write_text(json.dumps(report), encoding='utf-8')


@pytest.mark.parametrize('filename', ['python.xml', 'portable-models.xml'])
@pytest.mark.parametrize('identities', [
    ['nuclei', 'nuclei'], ['cyto3', 'cyto3'], ['nuclei'], ['cyto3'],
    ['nuclei', 'unrelated'], ['unrelated', 'other'], ['nuclei', 'cyto3', 'nuclei'],
    ['nuclei-17', 'cyto3-30'],
])
def test_duplicate_missing_or_unrelated_model_cases_do_not_satisfy_inference_gate(evidence, filename, identities):
    path, identity, _ = evidence
    names = [f'test_real_official_model_inference_preserves_input[{model}]'
             if model not in ('unrelated', 'other') else 'test_' + model for model in identities]
    replace_model_junit(path, filename, names)
    result = release_gate.evaluate(path, identity['commit'])
    expected = ('Both official CPU models must complete real inference exactly once: nuclei and cyto3.'
                if filename == 'python.xml' else
                'Both portable CPU models must have complete matching inference evidence exactly once: nuclei and cyto3.')
    # The hashes and test count above are current: this failure is model identity,
    # rather than a changed JUnit file or missing test summary.
    assert result['passed'] is False and result['blockers'] == [expected]


@pytest.mark.parametrize('filename', ['python.xml', 'portable-models.xml'])
def test_only_the_full_python_report_may_include_unrelated_regressions(evidence, filename):
    path, identity, _ = evidence
    names = ['test_real_official_model_inference_preserves_input[nuclei]',
             'test_real_official_model_inference_preserves_input[cyto3]', 'test_save_project']
    replace_model_junit(path, filename, names)
    result = release_gate.evaluate(path, identity['commit'])
    assert result['passed'] is (filename == 'python.xml')
    if filename != 'python.xml':
        assert len(result['blockers']) == 1 and 'Both portable CPU models' in result['blockers'][0]


@pytest.mark.parametrize('field', ['failure', 'error'])
@pytest.mark.parametrize(('kind', 'expected'), [
    ('browser', 'Browser scenario evidence is missing, failed or changed: browser_workflow.py'),
    ('native', 'Native Windows window, operations, and backend shutdown must be verified.'),
    ('portable', 'The portable CPU component must pass real Windows inference for the current source.'),
])
def test_success_summary_cannot_hide_cleanup_failure(evidence, field, kind, expected):
    path, identity, data = evidence
    name = {'browser': 'acceptance/browser_workflow/browser-report.json',
            'native': 'desktop/native-acceptance.json',
            'portable': 'portable-cellpose-acceptance.json'}[kind]
    report = data[name]
    assert report['passed'] is True and all(item['passed'] is True for item in report['scenarios'])
    report[field] = 'Synthetic cleanup failure after the final successful scenario'
    encoded = json.dumps(report).encode()
    (path / name).write_bytes(encoded)
    if kind == 'browser':
        summary = data['acceptance/browser-acceptance.json']
        next(item for item in summary['suites'] if item['suite'] == 'browser_workflow.py')['evidence_sha256'] = hashlib.sha256(encoded).hexdigest()
        (path / 'acceptance/browser-acceptance.json').write_text(json.dumps(summary), encoding='utf-8')
    result = release_gate.evaluate(path, identity['commit'])
    assert result['passed'] is False and result['blockers'] == [expected]
