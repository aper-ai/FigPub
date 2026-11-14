"""Record lint and complete Node regression evidence for the release gate."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from run_acceptance import ROOT, source_identity


def run(output: Path):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = source_identity()
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node is required for frontend acceptance.')
    package = ROOT / 'node_modules/oxlint/package.json'
    info = json.loads(package.read_text('utf-8'))
    executable = info['bin']['oxlint'] if isinstance(info['bin'], dict) else info['bin']
    tests = sorted((ROOT / 'tests').glob('*.mjs'))
    lint_files = [ROOT / 'web', *tests, *sorted((ROOT / 'scripts').glob('*.mjs'))]
    lint = subprocess.run([node, str(package.parent / executable), *map(str, lint_files)], cwd=ROOT,
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'lint.log').write_text(lint.stdout + lint.stderr, encoding='utf-8')
    result = subprocess.run([node, '--test', '--test-reporter=junit', *map(str, tests),
                             str(ROOT / 'tests/test_gel.cjs')], cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    (output / 'node.xml').write_text(result.stdout, encoding='utf-8')
    (output / 'node.stderr.log').write_text(result.stderr, encoding='utf-8')
    count, failures, skipped = 0, 1, 0
    try:
        xml = ET.fromstring(result.stdout)
        count = len(xml.findall('.//testcase'))
        failures = len(xml.findall('.//failure')) + len(xml.findall('.//error'))
        skipped = len(xml.findall('.//skipped'))
    except ET.ParseError:
        pass
    unchanged = source_identity() == identity
    report = {'source': identity, 'source_unchanged': unchanged, 'lint_passed': lint.returncode == 0,
              'node_passed': result.returncode == 0 and count > 0 and failures == 0 and skipped == 0,
              'test_count': count, 'failures': failures, 'skipped': skipped,
              'junit_sha256': hashlib.sha256((output / 'node.xml').read_bytes()).hexdigest(),
              'lint_sha256': hashlib.sha256((output / 'lint.log').read_bytes()).hexdigest(),
              'lint_returncode': lint.returncode, 'node_returncode': result.returncode}
    report['passed'] = report['lint_passed'] and report['node_passed'] and unchanged
    (output / 'frontend-checks.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'test-results')
    sys.exit(run(parser.parse_args().output))
