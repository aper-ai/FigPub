"""Fetch pinned official desktop inputs; normalize the tiny pure-Python sdist.

No upstream setup script is executed. proxy_tools only publishes a source
archive, so its unmodified module and original metadata are placed into a
deterministic, uncompressed wheel. All byte identities are in the lock file.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request
from urllib.parse import urlsplit
import zipfile

LOCK = Path(__file__).with_name('desktop-sources.lock.json')


def proxy_wheel(source: bytes) -> bytes:
    dist = 'proxy_tools-0.1.0.dist-info'
    files = {}
    with tarfile.open(fileobj=io.BytesIO(source), mode='r:gz') as archive:
        for name, target in [('proxy_tools-0.1.0/proxy_tools/__init__.py', 'proxy_tools/__init__.py'),
                             ('proxy_tools-0.1.0/PKG-INFO', dist + '/METADATA')]:
            item = archive.getmember(name)
            if not item.isfile() or item.size > 256 * 1024:
                raise ValueError('Unexpected pure-Python source member.')
            files[target] = archive.extractfile(item).read()
    files[dist + '/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: figure-workbench-pinned-source\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, content in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b'=').decode()
        writer.writerow([name, 'sha256=' + digest, str(len(content))])
    writer.writerow([dist + '/RECORD', '', ''])
    files[dist + '/RECORD'] = record.getvalue().encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            item = zipfile.ZipInfo(name, date_time=(2026, 11, 12, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            archive.writestr(item, content)
    return buffer.getvalue()


def prepare(destination: Path):
    specification = json.loads(LOCK.read_text('utf-8'))
    destination.mkdir(parents=True, exist_ok=True)
    report = []
    for source in specification['sources']:
        url = urlsplit(source['url'])
        if url.scheme != 'https' or url.hostname != 'files.pythonhosted.org':
            raise ValueError('Only pinned official PyPI file URLs are accepted.')
        path = destination / source['filename']
        if path.exists():
            content = path.read_bytes()
        else:
            with urllib.request.urlopen(source['url'], timeout=45) as response:
                content = response.read(source['bytes'] + 1)
        if len(content) != source['bytes'] or hashlib.sha256(content).hexdigest() != source['sha256']:
            raise ValueError('Official source bytes do not match the lock: ' + source['filename'])
        if not path.exists():
            path.write_bytes(content)
        if source['kind'] == 'sdist':
            wheel = proxy_wheel(content)
            prepared = source['prepared_wheel']
            if len(wheel) != prepared['bytes'] or hashlib.sha256(wheel).hexdigest() != prepared['sha256']:
                raise ValueError('Normalized proxy_tools wheel does not match the lock.')
            path = destination / prepared['filename']
            if path.exists() and path.read_bytes() != wheel:
                raise ValueError('Refusing to overwrite a different wheel: ' + path.name)
            if not path.exists():
                path.write_bytes(wheel)
        report.append({'wheel': path.name, 'bytes': path.stat().st_size,
                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), indent=2))
