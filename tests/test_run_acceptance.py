"""Check evidence routing without launching or claiming a real browser run."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from scripts import run_acceptance


def test_auto_apply_runner_uses_final_html_and_its_own_report_directory(tmp_path, monkeypatch):
    identity = {'commit': 'a' * 40, 'tracked_files_sha256': 'b' * 64, 'clean': True}
    monkeypatch.setattr(run_acceptance, 'source_identity', lambda: identity)
    monkeypatch.setattr(run_acceptance, 'SUITES', ['browser_auto_apply.py'])
    monkeypatch.setenv('BROWSER_STUDIO_HTML', str(tmp_path / 'stale.html'))
    monkeypatch.setenv('FW_AUTO_APPLY_RESULTS', str(tmp_path / 'stale-evidence'))
    calls = []

    def execute(command, **options):
        calls.append((command, options))
        folder = Path(options['env']['FW_AUTO_APPLY_RESULTS'])
        (folder / 'report.json').write_text(json.dumps({
            'passed': True, 'scenarios': [{'name': 'Runner routing fixture only', 'passed': True}],
        }), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout='Fixture process finished.', stderr='')

    monkeypatch.setattr(run_acceptance.subprocess, 'run', execute)
    output = tmp_path / 'evidence'
    assert run_acceptance.run(output) == 0
    assert len(calls) == 1
    command, options = calls[0]
    assert Path(command[1]) == run_acceptance.ROOT / 'tests/browser_auto_apply.py'
    assert options['env']['BROWSER_STUDIO_HTML'] == str(run_acceptance.ROOT / 'dist' / run_acceptance.BROWSER)
    assert options['env']['FW_AUTO_APPLY_RESULTS'] == str(output / 'browser_auto_apply')
    summary = json.loads((output / 'browser-acceptance.json').read_text('utf-8'))
    assert summary['passed'] is True
    assert summary['suites'][0]['evidence_file'] == 'browser_auto_apply/report.json'
    assert summary['suites'][0]['evidence_sha256']
    assert not (tmp_path / 'stale-evidence').exists()


def test_auto_apply_launch_failure_cannot_reuse_an_old_success_report(tmp_path, monkeypatch):
    identity = {'commit': 'a' * 40, 'tracked_files_sha256': 'b' * 64, 'clean': True}
    monkeypatch.setattr(run_acceptance, 'source_identity', lambda: identity)
    monkeypatch.setattr(run_acceptance, 'SUITES', ['browser_auto_apply.py'])
    output = tmp_path / 'evidence'
    previous = output / 'browser_auto_apply/report.json'
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps({'passed': True, 'scenarios': [{'passed': True}]}), encoding='utf-8')

    def fail(command, **options):
        assert not previous.exists()
        raise subprocess.TimeoutExpired(command, options['timeout'], output=b'Browser did not start.')

    monkeypatch.setattr(run_acceptance.subprocess, 'run', fail)
    assert run_acceptance.run(output) == 1
    summary = json.loads((output / 'browser-acceptance.json').read_text('utf-8'))
    assert summary['passed'] is False
    assert summary['suites'][0]['passed'] is False
    assert summary['suites'][0]['evidence_sha256'] is None
    assert summary['suites'][0]['return_code'] == 124
