"""Fetch Microsoft's small, signed WebView2 bootstrapper for the setup bundle.

The application and Cellpose payload are offline. A machine missing WebView2
still needs a connection when this bootstrapper installs the shared runtime.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request
from urllib.parse import urlsplit

OFFICIAL_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
MAX_BYTES = 10 * 1024 * 1024


def windows_file_metadata(path: Path) -> dict:
    if os.name != 'nt':
        raise OSError('Windows is required for Authenticode and PE version inspection.')
    literal = str(path.resolve()).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
        f"$file=Get-Item -LiteralPath '{literal}';"
        f"$signature=Get-AuthenticodeSignature -LiteralPath '{literal}';"
        "[ordered]@{status=[string]$signature.Status;"
        "subject=[string]$signature.SignerCertificate.Subject;"
        "thumbprint=[string]$signature.SignerCertificate.Thumbprint;"
        "file_version=[string]$file.VersionInfo.FileVersion;"
        "product_version=[string]$file.VersionInfo.ProductVersion}"
        "|ConvertTo-Json -Compress"
    )
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result = subprocess.check_output(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        text=True, encoding='utf-8', errors='strict',
    )
    return json.loads(result.lstrip('\ufeff'))


def verify_bootstrapper(path: Path, receipt: dict) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular, downloaded WebView2 bootstrapper.')
    data = path.read_bytes()
    signature = windows_file_metadata(path)
    if (receipt.get('url') != OFFICIAL_URL or not data.startswith(b'MZ')
            or not 0 < len(data) <= MAX_BYTES
            or receipt.get('bytes') != len(data)
            or receipt.get('sha256') != hashlib.sha256(data).hexdigest()
            or signature.get('status') != 'Valid'
            or not re.search(r'(?:^|,\s*)O=Microsoft Corporation(?:,|$)', signature.get('subject', ''))
            or signature != receipt.get('authenticode')):
        raise ValueError('WebView2 bootstrapper identity or Microsoft Authenticode signature is invalid.')
    return receipt


def fetch(output: Path, record: Path) -> dict:
    if os.name != 'nt':
        raise OSError('Download and validate the bootstrapper on the Windows build runner.')
    if output.exists() or record.exists() or output.is_symlink() or record.is_symlink():
        raise ValueError('Bootstrapper and receipt outputs must be new files.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(OFFICIAL_URL, timeout=90) as response:
        final = urlsplit(response.url)
        if final.scheme != 'https' or not (final.hostname or '').endswith('.microsoft.com'):
            raise ValueError('The official bootstrapper redirected outside Microsoft HTTPS hosting.')
        data = response.read(MAX_BYTES + 1)
    if not data.startswith(b'MZ') or not 0 < len(data) <= MAX_BYTES:
        raise ValueError('The downloaded WebView2 bootstrapper is not a bounded Windows executable.')
    output.write_bytes(data)
    result = {'url': OFFICIAL_URL, 'resolved_url': response.url, 'file': output.name,
              'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
              'authenticode': windows_file_metadata(output),
              'requires_network_when_runtime_missing': True}
    verify_bootstrapper(output, result)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(fetch(arguments.output, arguments.record), indent=2))
