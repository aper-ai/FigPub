"""Run each real UI suite and retain every failure. Never marks blocked UI as passed."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.release_config import BROWSER

OFFLINE_SUITES = frozenset({'test_browser_studio_ui.py', 'browser_auto_apply.py'})
SUITES = [
    'browser_workflow.py', 'browser_inputs.py', 'browser_channels.py',
    'browser_wb_workflow.py', 'browser_histology_fibers.py', 'browser_instance_review.py',
    'browser_composition.py', 'browser_onboarding.py', 'browser_professional_ui.py', 'browser_task_lifecycle.py',
    'test_browser_studio_ui.py', 'browser_auto_apply.py',
]
REPORT_NAMES = {
    'browser_channels.py': 'browser-channel-results.json',
    'browser_wb_workflow.py': 'browser-wb-report.json',
    'browser_composition.py': 'report.json',
    'browser_onboarding.py': 'onboarding-report.json',
    'test_browser_studio_ui.py': 'report.json',
    'browser_auto_apply.py': 'report.json',
}


def source_identity():
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    digest = hashlib.sha256()
    for name in sorted(filter(None, names)):
        digest.update(name.encode() + b'\0' + hashlib.sha256((ROOT / name).read_bytes()).digest())
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    clean = not untracked and subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=ROOT).returncode == 0
    return {'commit': commit, 'tracked_files_sha256': digest.hexdigest(), 'clean': clean}


def verified_scenarios(evidence):
    scenarios = evidence.get('scenarios') if isinstance(evidence, dict) else None
    return (isinstance(scenarios, list) and bool(scenarios)
            and all(isinstance(item, dict) and item.get('passed') is True for item in scenarios)
            and evidence.get('passed') is True and not evidence.get('browser_errors')
            and not evidence.get('failure') and not evidence.get('error'))


def run(output: Path):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'source': source_identity(), 'platform': sys.platform, 'real_browser': True,
              'passed': False, 'suites': []}
    # Windows acceptance uses the runtime extracted from the final full ZIP.
    # Preserve its provenance so release checks cannot substitute another bundle.
    report['full_toolkit_archive_sha256'] = os.environ.get('FIGURE_WORKBENCH_TOOLKIT_ARCHIVE_SHA256') or None
    report['full_toolkit_manifest_sha256'] = os.environ.get('FIGURE_WORKBENCH_TOOLKIT_MANIFEST_SHA256') or None
    component_home = os.environ.get('FIGURE_WORKBENCH_CELLPOSE_HOME')
    component_manifest = Path(component_home).parent / 'CELLPOSE_BUILD_MANIFEST.json' if component_home else None
    report['cellpose_component_manifest_sha256'] = (hashlib.sha256(component_manifest.read_bytes()).hexdigest()
        if component_manifest and component_manifest.is_file() else None)
    for suite in SUITES:
        folder = output / Path(suite).stem
        folder.mkdir(exist_ok=True)
        evidence_path = folder / REPORT_NAMES.get(suite, 'browser-report.json')
        # A failed launch must never inherit a previous run's success report.
        evidence_path.unlink(missing_ok=True)
        environment = {**os.environ, 'FW_BROWSER_RESULTS': str(folder), 'FW_BROWSER_STUDIO_RESULTS': str(folder),
                       'FW_AUTO_APPLY_RESULTS': str(folder),
                       'BROWSER_STUDIO_HTML': str(ROOT / 'dist' / BROWSER)}
        start = time.monotonic()
        try:
            completed = subprocess.run([sys.executable, str(ROOT / 'tests' / suite)], cwd=ROOT,
                                       env=environment, capture_output=True, text=True, encoding='utf-8',
                                       errors='replace', timeout=360)
            code, log = completed.returncode, completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as exc:
            code = 124
            stdout = exc.stdout.decode('utf-8', 'replace') if isinstance(exc.stdout, bytes) else exc.stdout or ''
            stderr = exc.stderr.decode('utf-8', 'replace') if isinstance(exc.stderr, bytes) else exc.stderr or ''
            log = stdout + stderr + '\nAcceptance timed out; it did not pass.\n'
        (folder / 'execution.log').write_text(log, encoding='utf-8')
        try:
            evidence = json.loads(evidence_path.read_text('utf-8'))
        except (OSError, ValueError):
            evidence = {}
        scenarios = evidence.get('scenarios', []) if isinstance(evidence, dict) else []
        verified = code == 0 and verified_scenarios(evidence)
        record = {'suite': suite, 'passed': verified, 'return_code': code,
                  'scenario_count': len(scenarios), 'evidence_file': str(evidence_path.relative_to(output)),
                  'evidence_sha256': hashlib.sha256(evidence_path.read_bytes()).hexdigest() if evidence_path.exists() else None,
                  'elapsed_seconds': round(time.monotonic() - start, 2)}
        report['suites'].append(record)
        print(json.dumps(record), flush=True)
        if not verified:
            print(f'Failure details for {suite}:\n{log[-12000:]}', flush=True)
        (output / 'browser-acceptance.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    report['source_unchanged'] = source_identity() == report['source']
    report['passed'] = (all(item['passed'] for item in report['suites'])
                        and len(report['suites']) == len(SUITES) and report['source_unchanged'])
    (output / 'browser-acceptance.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'test-results/acceptance')
    sys.exit(run(parser.parse_args().output))
