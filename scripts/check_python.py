"""Run the full Python suite and bind its actual JUnit evidence to the source."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from run_acceptance import ROOT, source_identity


def run(output: Path):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = source_identity()
    junit = output / 'python.xml'
    junit.unlink(missing_ok=True)
    completed = subprocess.run([sys.executable, '-m', 'pytest', '-q', f'--junitxml={junit}'], cwd=ROOT)
    cases, failures, skipped = [], 1, 0
    if junit.is_file():
        try:
            xml = ET.parse(junit).getroot()
            cases = xml.findall('.//testcase')
            failures = len(xml.findall('.//failure')) + len(xml.findall('.//error'))
            skipped = len(xml.findall('.//skipped'))
        except ET.ParseError:
            pass
    unchanged = identity == source_identity()
    report = {'source': identity, 'source_unchanged': unchanged, 'test_count': len(cases),
              'failures': failures, 'skipped': skipped, 'returncode': completed.returncode,
              'junit_sha256': hashlib.sha256(junit.read_bytes()).hexdigest() if junit.is_file() else None,
              'passed': completed.returncode == 0 and bool(cases) and failures == 0 and skipped == 0 and unchanged}
    (output / 'python-checks.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'test-results')
    sys.exit(run(parser.parse_args().output))
