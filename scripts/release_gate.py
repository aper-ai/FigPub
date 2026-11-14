"""Hard release gate: current-source numerical, browser and native Windows evidence.

This command never publishes anything and never upgrades a static build claim to
runtime verification. A missing, skipped, stale or failed requirement blocks it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_acceptance import REPORT_NAMES, SUITES, source_identity, verified_scenarios
from scripts.release_config import VERSION, FULL
from scripts.model_acceptance import verified_official_model_cases


def evaluate(reports: Path, expected_commit: str) -> dict:
    problems = []
    current = source_identity()
    if current['commit'] != expected_commit:
        problems.append('The tested checkout does not match the requested release commit.')
    if current.get('clean') is not True:
        problems.append('Uncommitted source changes must be tested and committed before release.')

    def load(name):
        try:
            value = json.loads((reports / name).read_text('utf-8'))
            if not isinstance(value, dict):
                raise ValueError('A report must be an object.')
            return value
        except (OSError, ValueError) as error:
            problems.append(f'Missing or invalid {name}: {error}')
            return {}

    python = load('python-checks.json')
    try:
        xml = ET.parse(reports / 'python.xml').getroot()
        cases = xml.findall('.//testcase')
        if not cases or xml.findall('.//failure') or xml.findall('.//error') or xml.findall('.//skipped'):
            problems.append('Python numerical/service tests contain failures, skips, or no results.')
        if not verified_official_model_cases(xml):
            problems.append('Both official CPU models must complete real inference exactly once: nuclei and cyto3.')
        if (python.get('passed') is not True or python.get('source') != current
                or python.get('junit_sha256') != hashlib.sha256((reports / 'python.xml').read_bytes()).hexdigest()
                or python.get('test_count') != len(cases)):
            problems.append('Python evidence must match the current source and complete JUnit report.')
    except (OSError, ET.ParseError) as error:
        problems.append(f'Missing or invalid Python evidence: {error}')

    frontend = load('frontend-checks.json')
    if (frontend.get('passed') is not True or frontend.get('lint_passed') is not True
            or frontend.get('node_passed') is not True or not frontend.get('test_count')
            or frontend.get('skipped') != 0 or frontend.get('source') != current):
        problems.append('Frontend lint and complete Node checks must pass for this source snapshot.')
    try:
        node = ET.parse(reports / 'node.xml').getroot()
        if (not node.findall('.//testcase') or node.findall('.//failure') or node.findall('.//error')
                or node.findall('.//skipped') or frontend.get('test_count') != len(node.findall('.//testcase'))
                or frontend.get('junit_sha256') != hashlib.sha256((reports / 'node.xml').read_bytes()).hexdigest()
                or frontend.get('lint_sha256') != hashlib.sha256((reports / 'lint.log').read_bytes()).hexdigest()):
            problems.append('Frontend evidence must match its complete successful JUnit and lint reports.')
    except (OSError, ET.ParseError):
        problems.append('Missing or invalid frontend JUnit/lint evidence.')

    browser = load('acceptance/browser-acceptance.json')
    suites = browser.get('suites', [])
    suites = suites if isinstance(suites, list) and all(isinstance(item, dict) for item in suites) else []
    if (browser.get('passed') is not True or browser.get('real_browser') is not True
            or {item.get('suite') for item in suites} != set(SUITES)
            or len(suites) != len(SUITES) or any(item.get('passed') is not True for item in suites)):
        problems.append('Every real browser workflow suite must pass with nonempty scenario evidence.')
    if browser.get('source') != current:
        problems.append('Browser results belong to a different source snapshot.')
    for item in suites:
        suite = item.get('suite')
        if suite not in SUITES:
            continue
        name = f'acceptance/{Path(suite).stem}/{REPORT_NAMES.get(suite, "browser-report.json")}'
        evidence = load(name)
        path = reports / name
        if (not verified_scenarios(evidence) or not path.is_file()
                or item.get('evidence_sha256') != hashlib.sha256(path.read_bytes()).hexdigest()):
            problems.append(f'Browser scenario evidence is missing, failed or changed: {suite}')
        if suite == 'test_browser_studio_ui.py':
            size = evidence.get('html_bytes')
            if (not re.fullmatch('[a-f0-9]{64}', str(evidence.get('html_sha256', '')))
                    or not isinstance(size, int) or isinstance(size, bool) or size <= 0):
                problems.append('Offline browser evidence must identify the exact tested HTML artifact.')

    native = load('desktop/native-acceptance.json')
    if (native.get('passed') is not True or native.get('platform') != 'win32'
            or native.get('native_window_verified') is not True or native.get('backend_stopped') is not True
            or not verified_scenarios(native)):
        problems.append('Native Windows window, operations, and backend shutdown must be verified.')
    if native.get('source_commit') != expected_commit:
        problems.append('Native Windows evidence belongs to a different commit.')
    if native.get('source_identity') != current:
        problems.append('Native Windows evidence belongs to different source contents.')
    build = load('windows-build.json')
    launcher_hash = build.get('native_launcher', {}).get('sha256')
    if not launcher_hash or launcher_hash != native.get('tested_application_sha256'):
        problems.append('Native Windows tests did not execute the candidate launcher recorded by the build.')
    snapshot = build.get('source_snapshot_sha256')
    if (not snapshot or snapshot != native.get('source_snapshot_sha256')
            or build.get('source_identity') != current):
        problems.append('The tested complete desktop source must match the candidate and current checkout.')

    component = load('portable-cellpose-build.json')
    portable = load('portable-cellpose-acceptance.json')
    manifest = component.get('source_manifest_sha256')
    if (portable.get('portable_runtime_executed') is not True or portable.get('platform') != 'win32'
            or portable.get('package_unchanged_after_execution') is not True
            or portable.get('source') != current or not verified_scenarios(portable)
            or not manifest or portable.get('source_manifest_sha256') != manifest):
        problems.append('The portable CPU component must pass real Windows inference for the current source.')
    try:
        models = ET.parse(reports / 'portable-models.xml').getroot()
        if (not verified_official_model_cases(models, only_models=True)
                or models.findall('.//failure') or models.findall('.//error')
                or models.findall('.//skipped')
                or portable.get('junit_sha256') != hashlib.sha256((reports / 'portable-models.xml').read_bytes()).hexdigest()):
            problems.append('Both portable CPU models must have complete matching inference evidence exactly once: nuclei and cyto3.')
    except (OSError, ET.ParseError):
        problems.append('Missing portable CPU model JUnit evidence.')
    if (browser.get('cellpose_component_manifest_sha256') != manifest
            or native.get('cellpose_component_manifest_sha256') != manifest
            or build.get('cellpose_component', {}).get('source_manifest_sha256') != manifest
            or build.get('cellpose_component', {}).get('sha256') != component.get('archive', {}).get('sha256')):
        problems.append('Browser workflows and the native desktop must use the tested portable CPU component.')

    extraction = load('full-toolkit-extraction.json')
    archives = build.get('archives', [])
    archives = archives if isinstance(archives, list) else []
    full_archives = [item for item in archives if isinstance(item, dict) and item.get('file') == FULL]
    full = {key: full_archives[0].get(key) for key in ('file', 'bytes', 'sha256')} if len(full_archives) == 1 else {}
    toolkit_manifest = build.get('full_toolkit_manifest_sha256')
    toolkit_files = build.get('full_toolkit_files')
    if (build.get('version') != VERSION or build.get('edition') != 'complete'
            or not full or type(full.get('bytes')) is not int or full.get('bytes', 0) <= 0
            or not re.fullmatch('[a-f0-9]{64}', str(full.get('sha256', '')))
            or not re.fullmatch('[a-f0-9]{64}', str(toolkit_manifest or ''))
            or type(toolkit_files) is not int or toolkit_files <= 0
            or build.get('cellpose_component', {}).get('bundled') is not True):
        problems.append('The release must identify one complete Windows ZIP with bundled models and a full file manifest.')
    extracted = extraction.get('verified_directory', {})
    if (extraction.get('passed') is not True or extraction.get('extracted_from_final_archive') is not True
            or extraction.get('source_identity') != current or extraction.get('archive') != full
            or extraction.get('full_toolkit_manifest_sha256') != toolkit_manifest
            or extraction.get('verified_files') != toolkit_files
            or not isinstance(extracted, dict)
            or extracted.get('manifest_sha256') != toolkit_manifest
            or extracted.get('verified_files') != toolkit_files
            or extracted.get('source_identity') != current
            or extracted.get('source_snapshot_sha256') != snapshot
            or extracted.get('launcher_sha256') != launcher_hash
            or extracted.get('cellpose_component_manifest_sha256') != manifest):
        problems.append('The full toolkit must be freshly extracted from its authenticated final ZIP before acceptance.')
    extraction_path = reports / 'full-toolkit-extraction.json'
    extraction_hash = hashlib.sha256(extraction_path.read_bytes()).hexdigest() if extraction_path.is_file() else None
    for name, evidence in [('Native Windows', native), ('Portable CPU', portable)]:
        if (evidence.get('full_toolkit_archive') != full
                or evidence.get('full_toolkit_manifest_sha256') != toolkit_manifest
                or evidence.get('full_toolkit_extraction_receipt_sha256') != extraction_hash
                or evidence.get('verified_full_toolkit_files') != toolkit_files
                or evidence.get('full_toolkit_unchanged_after_execution') is not True):
            problems.append(f'{name} acceptance must execute the exact full ZIP extraction and leave its files unchanged.')
    if (browser.get('full_toolkit_archive_sha256') != full.get('sha256')
            or browser.get('full_toolkit_manifest_sha256') != toolkit_manifest):
        problems.append('Browser workflows must use the model runtime extracted from the tested complete toolkit.')

    return {'passed': not problems, 'expected_commit': expected_commit, 'source': current,
            'requirements': ['complete numerical/API tests including both official CPU models',
                             'all real browser workflows', 'native Windows client lifecycle',
                             'portable Windows CPU inference', 'exact final full ZIP extraction and unchanged payload',
                             'candidate/source identity'], 'blockers': problems}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--expected-commit', required=True)
    args = parser.parse_args()
    result = evaluate(args.reports, args.expected_commit)
    args.reports.mkdir(parents=True, exist_ok=True)
    (args.reports / 'release-gate.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result['passed'] else 1)
