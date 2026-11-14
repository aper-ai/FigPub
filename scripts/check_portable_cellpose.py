"""Verify and execute the actual portable Windows CPU candidate, never its venv."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_acceptance import ROOT, source_identity
from scripts.model_acceptance import verified_official_model_cases


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(component: Path, output: Path, *, toolkit_archive: Path | None = None,
        extraction_record: Path | None = None):
    component, output = component.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'platform': sys.platform, 'source': source_identity(),
              'portable_runtime_executed': False, 'scenarios': []}
    junit = output / 'portable-models.xml'
    junit.unlink(missing_ok=True)
    try:
        if sys.platform != 'win32':
            raise RuntimeError('Portable Windows CPU acceptance requires real Windows execution.')
        sys.path.insert(0, str(ROOT / 'packaging'))
        from build_cellpose_candidate import verify_candidate
        from build_windows_full import verify_extraction_binding
        if (toolkit_archive is None) != (extraction_record is None):
            raise ValueError('Both complete-toolkit archive and extraction receipt are required together.')
        full_binding = None
        if toolkit_archive is not None:
            full_binding = verify_extraction_binding(component, toolkit_archive, extraction_record)
            verified = {'source_manifest_sha256': full_binding['cellpose_component_manifest_sha256'],
                        'verified_files': full_binding['cellpose_component_verified_files']}
            report.update(full_toolkit_archive=full_binding['archive'],
                          full_toolkit_manifest_sha256=full_binding['manifest_sha256'],
                          full_toolkit_extraction_receipt_sha256=full_binding['receipt_sha256'],
                          verified_full_toolkit_files=full_binding['verified_files'])
        else:
            verified = verify_candidate(component)
        report['source_manifest_sha256'] = verified['source_manifest_sha256']
        report['verified_files'] = verified['verified_files']
        worker = ROOT / 'workbench/cellpose_worker.py'
        manifest = json.loads((component / 'CELLPOSE_BUILD_MANIFEST.json').read_text('utf-8'))
        if manifest['worker_sha256'] != digest(worker):
            raise RuntimeError('Candidate model runtime was built for a different application worker.')
        interpreter = component / 'cellpose/runtime/python.exe'
        environment = dict(os.environ, FIGURE_WORKBENCH_CELLPOSE_HOME=str(component / 'cellpose'),
                           FIGURE_WORKBENCH_TEST_CELLPOSE='1', PYTHONUTF8='1',
                           PYTHONDONTWRITEBYTECODE='1', NUMBA_CACHE_DIR=str(output / 'portable-cache/numba'))
        Path(environment['NUMBA_CACHE_DIR']).mkdir(parents=True, exist_ok=True)
        for key in ('FIGURE_WORKBENCH_CELLPOSE_PYTHON', 'FIGURE_WORKBENCH_CELLPOSE_MODELS', 'PYTHONPATH', 'PYTHONHOME'):
            environment.pop(key, None)
        # The core application must resolve the new embedded interpreter, not
        # a prior successful virtual environment or a system Cellpose install.
        selected = subprocess.check_output([sys.executable, '-c',
            'from workbench.cellpose_runtime import _runtime_layout; print(_runtime_layout().interpreter)'],
            cwd=ROOT, env=environment, text=True, encoding='utf-8').strip()
        if Path(selected).resolve() != interpreter.resolve():
            raise RuntimeError('Application resolved a different model interpreter: ' + selected)
        report['interpreter'] = str(interpreter)
        report['interpreter_sha256'] = digest(interpreter)
        probe = subprocess.run([str(interpreter), '-B', str(worker), '--probe', '--models-dir',
                                str(component / 'cellpose/models')], cwd=component, env=environment,
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
        (output / 'portable-probe.log').write_text(probe.stdout + probe.stderr, encoding='utf-8')
        lines = probe.stdout.strip().splitlines()
        status = json.loads(lines[-1]) if lines else {}
        if probe.returncode != 0 or status.get('available') is not True:
            raise RuntimeError('The portable embedded Python failed to load its model dependencies.')
        report['probe'] = status
        report['scenarios'].append({'name': 'portable embedded interpreter loads isolated CPU dependencies', 'passed': True})
        completed = subprocess.run([sys.executable, '-m', 'pytest', '-q',
            'tests/test_cellpose_runtime.py', '-k', 'real_official', f'--junitxml={junit}'],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        (output / 'portable-models.log').write_text(completed.stdout + completed.stderr, encoding='utf-8')
        xml = ET.parse(junit).getroot()
        cases = xml.findall('.//testcase')
        if (completed.returncode or not verified_official_model_cases(xml, only_models=True) or xml.findall('.//failure')
                or xml.findall('.//error') or xml.findall('.//skipped')):
            raise RuntimeError('Official nuclei and cyto3 must each pass exactly once using the portable component.')
        for case in cases:
            report['scenarios'].append({'name': case.get('name'), 'passed': True})
        report['junit_sha256'] = digest(junit)
        report['portable_runtime_executed'] = True
        if full_binding is not None:
            if verify_extraction_binding(component, toolkit_archive, extraction_record) != full_binding:
                raise RuntimeError('Portable inference changed the verified complete toolkit.')
            report['full_toolkit_unchanged_after_execution'] = True
        elif verify_candidate(component) != verified:
            raise RuntimeError('Portable inference changed the verified component contents.')
        report['package_unchanged_after_execution'] = True
        if source_identity() != report['source']:
            raise RuntimeError('Application source changed while testing the portable model component.')
        # Publish only the tested path to subsequent browser/native CI steps.
        if os.environ.get('GITHUB_ENV'):
            with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
                stream.write(f'FIGURE_WORKBENCH_CELLPOSE_HOME={component / "cellpose"}\n')
                if full_binding is not None:
                    stream.write(f'FIGURE_WORKBENCH_TOOLKIT_ARCHIVE_SHA256={full_binding["archive"]["sha256"]}\n')
                    stream.write(f'FIGURE_WORKBENCH_TOOLKIT_MANIFEST_SHA256={full_binding["manifest_sha256"]}\n')
        report['passed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        report['traceback'] = traceback.format_exc()
    (output / 'portable-cellpose-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'test-results')
    parser.add_argument('--toolkit-archive', type=Path)
    parser.add_argument('--extraction-record', type=Path)
    args = parser.parse_args()
    sys.exit(run(args.component, args.output, toolkit_archive=args.toolkit_archive,
                 extraction_record=args.extraction_record))
