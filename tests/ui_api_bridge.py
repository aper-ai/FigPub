"""Test-only stdio adapter to the real ASGI app; opens no network listener.

Used by test_ui_service_flow.mjs when a browser cannot start. This is a DOM/API
integration test, not a substitute for WebView2 or browser visual acceptance.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import traceback
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from workbench.server import create_app


def fixture_files():
    from PIL import Image

    png = io.BytesIO()
    Image.new('RGB', (300, 200), (45, 91, 132)).save(png, 'PNG')
    rows = ['sample,target,ct,group,biological_replicate,technical_replicate']
    for group, value in [('Control', 24), ('Treatment', 22)]:
        for repeat in (1, 2, 3):
            sample = f'{group}-{repeat}'
            for target, ct in [('ACTB', 20), ('GeneX', value)]:
                rows.append(f'{sample},{target},{ct},{group},{sample},1')
    return {
        'png': base64.b64encode(png.getvalue()).decode(),
        'qpcr': base64.b64encode(('\n'.join(rows) + '\n').encode()).decode(),
    }


def qpcr_workbook_files():
    """Independent originals intentionally reuse well rows and sample IDs."""
    from openpyxl import Workbook

    result = {}
    for name, delta in [('A', 1), ('B', 2)]:
        book = Workbook()
        book.remove(book.active)
        for suffix, difference in [('results', delta), ('other', delta+2)]:
            sheet = book.create_sheet(f'{name}-{suffix}')
            sheet.append(['sample', 'target', 'ct', 'group', 'biological_replicate', 'technical_replicate'])
            for sample in range(1, 7):
                group = 'Control' if sample <= 3 else 'Treatment'
                for target, ct in [('ACTB', 18), ('GeneX', 20 if group == 'Control' else 20-difference)]:
                    sheet.append([f'S{sample}', target, ct, group, f'S{sample}', '1'])
        notes = book.create_sheet('Notes')
        notes.append(['Description', 'SYNTHETIC fixture; this is not a Ct worksheet'])
        output = io.BytesIO()
        book.save(output)
        book.close()
        result[name] = base64.b64encode(output.getvalue()).decode()
    return result


def main():
    with tempfile.TemporaryDirectory(prefix='figure-dom-api-') as directory:
        with TestClient(create_app(directory)) as client:
            print(json.dumps({'ready': True}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)
                try:
                    if command.get('op') == 'stop':
                        break
                    if command.get('op') == 'fixtures':
                        result = fixture_files()
                    elif command.get('op') == 'qpcr_workbooks':
                        result = qpcr_workbook_files()
                    elif command.get('op') == 'inspect_archive':
                        with zipfile.ZipFile(io.BytesIO(base64.b64decode(command['data']))) as archive:
                            result = {'project': json.loads(archive.read('project.json')),
                                      'members': archive.namelist()}
                    elif command.get('op') == 'inspect_pdf':
                        import fitz
                        with fitz.open(stream=base64.b64decode(command['data']), filetype='pdf') as pdf:
                            result = {'pages': len(pdf), 'width_mm': pdf[0].rect.width * 25.4 / 72,
                                      'text': pdf[0].get_text()}
                    else:
                        path = command['path']
                        if not path.startswith('/') or path.startswith('//'):
                            raise ValueError('Only app-local paths are permitted in the test adapter.')
                        options = {'headers': command.get('headers', {})}
                        if 'files' in command:
                            options['files'] = [(item['field'], (item['name'], base64.b64decode(item['data']), item['type']))
                                                for item in command['files']]
                        elif 'body' in command:
                            options['content'] = command['body'].encode()
                        response = client.request(command.get('method', 'GET'), path, **options)
                        result = {'status': response.status_code, 'headers': dict(response.headers),
                                  'data': base64.b64encode(response.content).decode()}
                    print(json.dumps({'id': command['id'], 'result': result}), flush=True)
                except Exception:
                    print(json.dumps({'id': command['id'], 'error': traceback.format_exc()}), flush=True)


if __name__ == '__main__':
    main()
