"""Local project and immutable-source storage, independent of Qt.

Figure Workbench, AGPL-3.0-only. The source/manifest boundaries are informed by
ScieFig's public .sfig design; this is a new implementation of a .figwork format.
A hash verifies byte identity, not instrument origin or scientific authenticity.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import warnings
from collections import OrderedDict
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

import numpy as np
from PIL import Image, ImageDraw

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_PROJECT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000
MAX_DECODE_BYTES = 512 * 1024 * 1024
MAX_ASSETS = 256
MAX_TABLE_ROWS = 100_000
IMAGE_EXTENSIONS = {'.png', '.tif', '.tiff', '.jpg', '.jpeg', '.bmp', '.webp'}
TABLE_EXTENSIONS = {'.csv', '.tsv', '.txt', '.xlsx'}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | TABLE_EXTENSIONS | {'.svg', '.pdf'}
ID_RE = re.compile(r'^[a-f0-9]{64}$')
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ConflictError(ValueError):
    """The caller edited a project revision which is no longer current."""


class WorkspaceRecoveryPending(ConflictError):
    """An autosave must be explicitly restored or replaced before edits."""


class WorkspaceCleanupPending(ConflictError):
    """An interrupted cleanup must finish before source data may be used."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, Path)):
        return str(value)
    raise ValueError(f'Unsupported result value: {type(value).__name__}')


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ByteLRU:
    """Thread-safe cache bounded by actual byte size and entry count."""
    def __init__(self, max_bytes: int, max_entries: int = 128):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.bytes = 0
        self.items = OrderedDict()
        self.lock = threading.RLock()

    def get(self, key):
        with self.lock:
            item = self.items.pop(key, None)
            if item is None:
                return None
            self.items[key] = item
            return item[0]

    def put(self, key, value, size: int | None = None):
        size = int(size if size is not None else len(value))
        with self.lock:
            old = self.items.pop(key, None)
            if old:
                self.bytes -= old[1]
            if size > self.max_bytes:
                return value
            self.items[key] = (value, size)
            self.bytes += size
            while self.bytes > self.max_bytes or len(self.items) > self.max_entries:
                _, (_, removed_size) = self.items.popitem(last=False)
                self.bytes -= removed_size
        return value

    def clear(self):
        with self.lock:
            self.items.clear()
            self.bytes = 0


def new_project(title: str = '未命名图稿') -> dict:
    timestamp = now_iso()
    return {
        'schema_version': 1, 'id': str(uuid.uuid4()), 'title': str(title)[:240],
        'revision': 0, 'created_at': timestamp, 'updated_at': timestamp,
        'assets': [], 'figure': {'width_mm': 180, 'height_mm': 120, 'background': '#ffffff'},
        'panels': [], 'annotations': [], 'measurements': [], 'audit': [],
        'settings': {'min_ppi': 300, 'font_family': 'DejaVu Sans'},
    }


def _audit(project: dict, action: str, detail: str) -> None:
    project.setdefault('audit', []).append({'at': now_iso(), 'action': action, 'detail': detail})
    # Bound a project file while preserving earlier events in its portable saves.
    if len(project['audit']) > 20_000:
        raise ValueError('Project audit is full; archive this project and start a new one.')


def _finite_number(value, label: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number.')
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f'{label} is outside its allowed range.')
    return float(value)


def validate_project(project: dict, registry: dict[str, dict] | None = None) -> dict:
    """Validate structural safety and every source reference, without hiding errors."""
    from .models import validate_schema
    validate_schema(project)
    if not isinstance(project, dict) or project.get('schema_version') != 1:
        raise ValueError('Unsupported project schema; expected schema_version 1.')
    if not isinstance(project.get('id'), str) or len(project['id']) > 100:
        raise ValueError('Project id is missing or invalid.')
    if not isinstance(project.get('revision'), int) or project['revision'] < 0:
        raise ValueError('Project revision is invalid.')
    if len(json_bytes(project)) > MAX_PROJECT_BYTES:
        raise ValueError('Project JSON exceeds the size limit.')
    assets = project.get('assets')
    if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
        raise ValueError('Invalid project assets.')
    asset_ids = set()
    for asset in assets:
        if not isinstance(asset, dict) or not ID_RE.fullmatch(str(asset.get('id', ''))):
            raise ValueError('Asset ids must be content SHA-256 identities.')
        aid = asset['id']
        if aid in asset_ids or asset.get('sha256') != aid:
            raise ValueError('Duplicate asset or mismatched SHA-256 identity.')
        if registry is not None and aid not in registry:
            raise ValueError(f'Missing source asset: {aid[:12]}')
        asset_ids.add(aid)
    figure = project.get('figure', {})
    for key in ('width_mm', 'height_mm'):
        _finite_number(figure.get(key), f'Figure {key}', 1, 2000)
    if not isinstance(project.get('panels'), list) or len(project['panels']) > 1000:
        raise ValueError('Invalid panel list.')
    seen_panels = set()
    for panel in project['panels']:
        if not isinstance(panel, dict) or not isinstance(panel.get('id'), str) or not panel['id'] or panel['id'] in seen_panels:
            raise ValueError('Panels need distinct ids.')
        seen_panels.add(panel['id'])
        if panel.get('asset_id') not in asset_ids:
            raise ValueError('A panel references a missing original asset.')
        for key in ('x_mm', 'y_mm'):
            _finite_number(panel.get(key), f'Panel {key}', -2000, 4000)
        for key in ('width_mm', 'height_mm'):
            _finite_number(panel.get(key), f'Panel {key}', 0.01, 4000)
        _finite_number(panel.get('rotation', 0), 'Panel rotation', -36000, 36000)
        crop = panel.get('crop')
        if crop is not None:
            if not isinstance(crop, dict):
                raise ValueError('Invalid panel crop.')
            for key in ('x', 'y'):
                _finite_number(crop.get(key), f'Crop {key}', 0, MAX_IMAGE_PIXELS)
            for key in ('width', 'height'):
                _finite_number(crop.get(key), f'Crop {key}', 0.001, MAX_IMAGE_PIXELS)
        display = panel.get('display', {})
        _finite_number(display.get('gamma', 1), 'Display gamma', 0.1, 5)
        for key in ('low', 'high'):
            if display.get(key) is not None:
                _finite_number(display[key], f'Display {key}')
    if not isinstance(project.get('annotations', []), list) or len(project.get('annotations', [])) > 5000:
        raise ValueError('Invalid annotations.')
    for item in project.get('annotations', []):
        if item.get('type') not in {'text', 'line', 'rect'}:
            raise ValueError('Unsupported annotation type.')
        for key in ('x_mm', 'y_mm', 'width_mm', 'height_mm'):
            _finite_number(item.get(key, 0), f'Annotation {key}', -4000, 4000)
        _finite_number(item.get('font_size_pt', 8), 'Annotation font size', 0.1, 1000)
    if not isinstance(project.get('measurements', []), list):
        raise ValueError('Invalid measurements.')
    rois = project.get('settings', {}).get('analysis_rois', [])
    if not isinstance(rois, list) or len(rois) > 10000:
        raise ValueError('Analysis ROIs must be a bounded list.')
    for roi in rois:
        if not isinstance(roi, dict):
            raise ValueError('Each analysis ROI must be an object.')
        if 'id' in roi and (not isinstance(roi['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', roi['id'])):
            raise ValueError('ROI ids must use only letters, numbers, underscores and hyphens.')

    def references(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'asset_id' and value and value not in asset_ids:
                    raise ValueError(f'Missing referenced original asset: {str(value)[:12]}')
                if key in {'asset_ids', 'source_asset_ids'} and isinstance(value, list):
                    if any(x not in asset_ids for x in value):
                        raise ValueError('An analysis references missing source assets.')
                references(value)
        elif isinstance(node, list):
            for value in node:
                references(value)
    references(project)
    from .channels import validate_view
    source_registry = registry if registry is not None else {asset['id']: asset for asset in assets}
    edits = [(panel['asset_id'], panel) for panel in project['panels']]
    edits.extend(project.get('settings', {}).get('asset_edits', {}).items())
    edits.extend(project.get('asset_edits', {}).items())
    for base_id, edit in edits:
        if isinstance(edit, dict) and edit.get('channel_view') is not None:
            if base_id not in asset_ids or source_registry[base_id].get('kind') != 'image':
                raise ValueError('A channel view must be attached to an original raster asset in this project.')
            validate_view(edit['channel_view'], source_registry)
    return project


def safe_svg(data: bytes) -> bytes:
    """Reject executable SVG and all external resource references; retain source bytes."""
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('SVG exceeds the 8 MB parsing limit.')
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('SVG DTD/entity declarations are not supported.')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError('Invalid SVG XML.') from exc
    if root.tag.split('}')[-1].lower() != 'svg':
        raise ValueError('The file is not an SVG document.')
    def allowed_css_target(target):
        return target.startswith('#') or re.fullmatch(r'data:font/(?:otf|ttf|woff|woff2);base64,[A-Za-z0-9+/=]+', target, re.I) is not None
    blocked = {'script', 'foreignobject', 'iframe', 'audio', 'video', 'animate', 'animatetransform', 'set'}
    for node in root.iter():
        tag = node.tag.split('}')[-1].lower()
        if tag in blocked:
            raise ValueError(f'Unsafe SVG element: {tag}')
        for key, value in node.attrib.items():
            local_key = key.split('}')[-1].lower()
            value = str(value).strip()
            if local_key.startswith('on'):
                raise ValueError('SVG event handlers are not supported.')
            if local_key in {'href', 'src'} and value and not value.startswith('#'):
                if not re.match(r'^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=\s]+$', value, re.I):
                    raise ValueError('SVG external/active resource references are not supported.')
            if re.search(r'url\s*\(', value, re.I):
                targets = re.findall(r'url\s*\(\s*[\'"]?([^\)\'"\s]+)', value, re.I)
                if any(not allowed_css_target(target) for target in targets):
                    raise ValueError('SVG external CSS resources are not supported.')
        if tag == 'style':
            content = ''.join(node.itertext())
            if re.search(r'@import|expression\s*\(|javascript:|\\', content, re.I):
                raise ValueError('SVG active or external CSS is not supported.')
            targets = re.findall(r'url\s*\(\s*[\'"]?([^\)\'"\s]+)', content, re.I)
            if any(not allowed_css_target(target) for target in targets):
                raise ValueError('SVG external CSS resources are not supported.')
    return data


def _validate_zip(archive: ZipFile, max_total: int = MAX_ARCHIVE_BYTES, max_entries: int = 1024) -> dict:
    members = {}
    total = 0
    for info in archive.infolist():
        # ZipInfo truncates NULs and, on Windows, replaces backslashes in
        # filename. Validate the original archive spelling before trusting
        # that normalized value, so every platform rejects the same input.
        if info.orig_filename != info.filename:
            raise ValueError('Unsafe archive member path.')
        name = info.filename
        path = PurePosixPath(name)
        if not name or '\\' in name or '\x00' in name or path.is_absolute() or any(part in {'', '.', '..'} for part in name.rstrip('/').split('/')) or ':' in name:
            raise ValueError('Unsafe archive member path.')
        if name in members:
            raise ValueError('Duplicate archive member name.')
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError('Archive symlinks are not supported.')
        if info.flag_bits & 1:
            raise ValueError('Encrypted archives are not supported.')
        total += info.file_size
        if total > max_total or info.file_size > max_total or len(members) >= max_entries:
            raise ValueError('Archive expanded size or member count exceeds limit.')
        # High compression is normal for scientific background images. Absolute
        # expanded-size limits, rather than a low ratio, prevent decompression bombs.
        members[name] = info
    return members


def _table_from_path(path: Path, suffix: str) -> dict:
    from .qpcr import _canonical_header, choose_text_delimiter

    def qpcr_header(rows):
        for index, row in enumerate(rows[:100]):
            mapping = _canonical_header(row)
            if {'sample', 'target', 'ct'}.issubset(mapping.values()):
                return index, len(mapping)
        return None

    text_info = {}
    if suffix == '.xlsx':
        with ZipFile(path) as z:
            _validate_zip(z, max_total=MAX_ARCHIVE_BYTES, max_entries=10_000)
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            # Instrument workbooks commonly open on an information sheet. Use
            # the same documented header/name score as qPCR analysis, while
            # retaining the active sheet for ordinary, non-qPCR spreadsheets.
            candidates = []
            for sheet in workbook:
                head = list(_bounded_rows(islice(sheet.iter_rows(values_only=True), 100)))
                found = qpcr_header(head)
                if found is not None:
                    index, columns = found
                    candidates.append((columns + (20 if 'result' in sheet.title.casefold() else 0), sheet.title))
            sheet = workbook[max(candidates, key=lambda item: item[0])[1]] if candidates else workbook.active
            values = list(_bounded_rows(sheet.iter_rows(values_only=True)))
            sheet_name = sheet.title
        finally:
            workbook.close()
    else:
        payload = path.read_bytes()
        try:
            text = payload.decode('utf-16' if payload.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = payload.decode('gb18030')
            except UnicodeDecodeError as exc:
                raise ValueError('Text tables must be UTF-8, BOM-marked UTF-16, or GB18030 encoded.') from exc
        if suffix == '.tsv':
            delimiter = '\t'
        elif suffix == '.txt':
            delimiter = choose_text_delimiter(text)
        else:
            try:
                delimiter = choose_text_delimiter(text)
            except ValueError:
                # Generic CSV may be a valid single-column table. Preserve that
                # support; candidate qPCR ambiguity is checked again below.
                delimiter = ','
                try:
                    delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',;\t').delimiter
                except csv.Error:
                    pass
        try:
            values = list(_bounded_rows(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)))
        except csv.Error as exc:
            raise ValueError('表格格式无效或文本字段过长，请检查分隔符和字段引号。') from exc
        text_info['delimiter'] = delimiter
        sheet_name = None
    recognized = qpcr_header(values)
    header_index = recognized[0] if recognized is not None else next(
        (index for index, row in enumerate(values) if any(value not in (None, '') for value in row)), 0)
    values = values[header_index:]
    text_info['header_row'] = header_index + 1
    if suffix == '.txt':
        if not values or len(values[0]) < 2:
            raise ValueError('TXT needs an explicit multicolumn header; single-column text is not imported as data.')
        if any(len(row) > len(values[0]) for row in values[1:]):
            raise ValueError('A TXT data row has more columns than its header; check its delimiter and field quoting.')
    nonempty = [row for row in values if any(value not in (None, '') for value in row)]
    if not nonempty:
        raise ValueError('The table is empty.')
    columns = [str(value).strip() if value is not None and str(value).strip() else f'column_{index+1}'
               for index, value in enumerate(nonempty[0])]
    if len(set(columns)) != len(columns):
        raise ValueError('Table column names must be unique.')
    for row_number, row in enumerate(values[1:], header_index + 2):
        if any(value not in (None, '') for value in row[len(columns):]):
            raise ValueError(f'第 {row_number} 行包含超出表头的非空数据，请检查分隔符和字段引号。')
    rows = [{key: json_safe(row[i]) if i < len(row) else None for i, key in enumerate(columns)} for row in nonempty[1:]]
    return {'columns': columns, 'rows': rows, 'total_rows': len(rows), 'sheet': sheet_name, **text_info}


def _bounded_rows(rows):
    cells = 0
    for index, row in enumerate(rows):
        if index > MAX_TABLE_ROWS or len(row) > 512:
            raise ValueError('Table row or column limit exceeded.')
        cells += len(row)
        if cells > 2_000_000:
            raise ValueError('Table exceeds the two-million-cell limit.')
        yield list(row)


def _image_metadata(path: Path, suffix: str) -> dict:
    from .planes import describe_raster
    return describe_raster(path)


def inspect_source(path: Path, name: str) -> dict:
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f'Unsupported source format: {suffix or "no extension"}')
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('An original file must be at most 128 MB.')
    if suffix in IMAGE_EXTENSIONS:
        return {'kind': 'image', **_image_metadata(path, suffix)}
    if suffix in TABLE_EXTENSIONS:
        table = _table_from_path(path, suffix)
        return {'kind': 'table', 'width_px': 0, 'height_px': 0, 'channels': 0, 'frames': 1,
                'bit_depth': 0, 'dtype': 'table', 'metadata': {'columns': table['columns'], 'table_preview': table['rows'][:10], 'total_rows': table['total_rows'], 'sheet': table['sheet'],
                                                           **{key: table[key] for key in ('header_row', 'delimiter') if key in table}}}
    import fitz
    if suffix == '.svg':
        from .vectors import open_svg
        with open_svg(path.read_bytes()) as doc:
            rect = doc[0].rect
            width, height = max(1, round(rect.width)), max(1, round(rect.height))
        return {'kind': 'svg', 'width_px': width, 'height_px': height, 'channels': 4,
                'frames': 1, 'bit_depth': 8, 'dtype': 'vector',
                'metadata': {'vector': True, 'preview_resolution_dpi': 72,
                             'svg_renderer': 'svglib-reportlab', 'default_svg_font': 'DejaVu Sans'}}
    with fitz.open(path) as doc:
        if doc.needs_pass or not doc.page_count:
            raise ValueError('Encrypted or empty PDFs are not supported.')
        if doc.page_count > 500:
            raise ValueError('PDF page count exceeds 500.')
        rect = doc[0].rect
        if max(rect.width, rect.height) > 100_000:
            raise ValueError('PDF page dimensions are too large.')
        return {'kind': 'pdf', 'width_px': max(1, round(rect.width)), 'height_px': max(1, round(rect.height)),
                'channels': 3, 'frames': doc.page_count, 'bit_depth': 8, 'dtype': 'vector',
                'metadata': {'vector': True, 'pages': doc.page_count, 'width_pt': rect.width, 'height_pt': rect.height, 'preview_resolution_dpi': 72}}


class ProjectStore:
    def __init__(self, root: str | Path, *, restore_on_start: bool = True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / 'originals'
        self.objects.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.array_cache = ByteLRU(96 * 1024 * 1024, 12)
        self.preview_cache = ByteLRU(32 * 1024 * 1024, 128)
        self.table_cache = ByteLRU(24 * 1024 * 1024, 12)
        self.localization_cache = ByteLRU(16 * 1024 * 1024, 12)
        self.registry = self._read_json(self.root / 'assets.json', {})
        self.cleanup_marker = self.root / '.workspace-clear-pending.json'
        self.cleanup_pending = self.cleanup_marker.exists()
        self._recovery_project = None
        self._cleanup_project = new_project()
        self.project = None
        for path in (() if self.cleanup_pending else (self.root / 'current.json', self.root / 'current.backup.json')):
            try:
                value = json.loads(path.read_text('utf-8'))
                validate_project(value, self.registry)
                self.project = value
                break
            except (OSError, ValueError, TypeError):
                continue
        if self.project is None:
            self.project = new_project()
            if not self.cleanup_pending:
                self._persist()
        elif not restore_on_start and self._has_workspace_content(self.project):
            # Do not persist the new blank view: doing so would overwrite the
            # user's only autosave before they have made a recovery decision.
            self._recovery_project = copy.deepcopy(self.project)
            self.project = new_project()

    @staticmethod
    def _has_workspace_content(project):
        return bool(project.get('assets') or project.get('panels') or project.get('annotations')
                    or project.get('measurements') or project.get('revision', 0))

    def _require_workspace_ready(self):
        if self.cleanup_pending:
            raise WorkspaceCleanupPending('本机工作区清理尚未完成，请先重试清理。')
        if self._recovery_project is not None:
            raise WorkspaceRecoveryPending('检测到上次工作区，请先选择恢复、明确新建或清除本机工作区。')

    @property
    def has_pending_recovery(self):
        return self._recovery_project is not None and not self.cleanup_pending

    def workspace_status(self):
        with self.lock:
            recovery = self._recovery_project
            return {
                'has_recovery': self.has_pending_recovery,
                'cleanup_pending': self.cleanup_pending,
                'recovery': ({'title': recovery.get('title', ''),
                              'updated_at': recovery.get('updated_at'),
                              'asset_count': len(recovery.get('assets', []))}
                             if recovery is not None and not self.cleanup_pending else None),
                'stored_asset_count': len(self.registry),
                'stored_bytes': sum(int(item.get('size_bytes', 0)) for item in self.registry.values()),
                'current_asset_count': 0 if self.cleanup_pending else len(self.project['assets']),
            }

    def restore_workspace(self):
        with self.lock:
            if self.cleanup_pending:
                raise WorkspaceCleanupPending('本机工作区清理尚未完成，请先重试清理。')
            if self._recovery_project is None:
                raise ConflictError('没有待恢复的本机工作区。')
            recovery = self._recovery_project
            self._recovery_project = None
            try:
                # Recheck every source before displaying a recovered project.
                self.verify_originals(recovery)
                return self._commit_project(copy.deepcopy(recovery))
            except Exception:
                self._recovery_project = recovery
                raise

    def clear_workspace(self):
        """Delete only application-owned working copies, never external files.

        The HTTP layer drains its requests and rejects active jobs before this
        operation. A durable marker prevents a partially cleared autosave from
        reappearing after an I/O failure or process interruption.
        """
        with self.lock:
            atomic_write(self.cleanup_marker, json_bytes({'version': 1, 'started_at': now_iso()}))
            self.cleanup_pending = True
            deleted = 0
            if self.objects.is_symlink():
                # Never follow a replacement link outside the owned directory.
                self.objects.unlink()
                self.objects.mkdir()
            elif self.objects.exists():
                for path in self.objects.iterdir():
                    if path.is_symlink() or not path.is_dir():
                        path.unlink()
                        deleted += 1
                    else:
                        shutil.rmtree(path)
            else:
                self.objects.mkdir()
            owned_names = {'current.json', 'current.backup.json', 'assets.json'}
            for path in self.root.iterdir():
                name = path.name
                owned_temporary = (name.startswith(('.current.json.', '.current.backup.json.', '.assets.json.', '.import-'))
                                   or re.fullmatch(r'tmp[a-z0-9_]{8}\.(?:png|tiff?|jpe?g|bmp|webp|svg|pdf|csv|tsv|txt|xlsx)', name))
                if name in owned_names or owned_temporary:
                    if path.is_dir() and not path.is_symlink():
                        raise OSError('工作区文件被目录替换，清理未完成。')
                    path.unlink()
            self.clear_caches()
            project = new_project()
            atomic_write(self.root / 'current.json', json_bytes(project))
            self.cleanup_marker.unlink()
            self.registry = {}
            self.project = project
            self._recovery_project = None
            self.cleanup_pending = False
            return {'project': copy.deepcopy(project), 'cleared': True, 'deleted_originals': deleted,
                    'requires_restart': True, 'browser_cache': 'close_client_or_clear_site_cache'}

    def clear_caches(self):
        """Release owned decoded data after requests and analysis have stopped."""
        for cache in (self.array_cache, self.preview_cache, self.table_cache, self.localization_cache):
            cache.clear()

    @staticmethod
    def _read_json(path, fallback):
        try:
            value = json.loads(path.read_text('utf-8'))
            return value if isinstance(value, dict) else fallback
        except (OSError, ValueError):
            return fallback

    def _persist(self):
        current = self.root / 'current.json'
        if current.exists():
            atomic_write(self.root / 'current.backup.json', current.read_bytes())
        atomic_write(current, json_bytes(self.project))

    def _advance(self, project: dict, action: str, detail: str):
        project['revision'] = self.project['revision'] + 1
        project['updated_at'] = now_iso()
        _audit(project, action, detail)
        return self._commit_project(project)

    def _commit_project(self, project: dict):
        """Install a validated snapshot only when its durable save succeeds.

        Callers hold ``self.lock``. In particular, a failed new/open operation
        must leave the active identity unchanged so the UI can retry safely.
        Validation after appending results/audit also prevents writing a state
        which this same application would reject on the next startup.
        """
        self._require_workspace_ready()
        validate_project(project, self.registry)
        previous = self.project
        self.project = project
        try:
            self._persist()
        except Exception:
            self.project = previous
            raise
        return copy.deepcopy(project)

    def get_project(self):
        with self.lock:
            return copy.deepcopy(self._cleanup_project if self.cleanup_pending else self.project)

    def update_project(self, payload: dict):
        validate_project(payload, self.registry)
        with self.lock:
            if payload['id'] != self.project['id'] or payload['revision'] != self.project['revision']:
                raise ConflictError('This project changed in another operation. Reload before saving.')
            value = copy.deepcopy(payload)
            value['assets'] = [self.project_asset(a) for a in value['assets']]
            value['created_at'] = self.project['created_at']
            value['audit'] = copy.deepcopy(self.project['audit'])
            return self._advance(value, 'edit_project', 'Saved figure and analysis settings')

    def reset(self, title='未命名图稿'):
        with self.lock:
            if self.cleanup_pending:
                raise WorkspaceCleanupPending('本机工作区清理尚未完成，请先重试清理。')
            project = new_project(title)
            _audit(project, 'new_project', project['title'])
            recovery = self._recovery_project
            self._recovery_project = None
            try:
                result = self._commit_project(project)
            except Exception:
                self._recovery_project = recovery
                raise
            return result

    def public_asset(self, asset_id):
        if asset_id not in self.registry:
            raise ValueError('Source asset not found.')
        with self.lock:
            existing = self.registry[asset_id]
            if existing.get('kind') == 'image' and not existing.get('metadata', {}).get('plane_schema_version'):
                # Older projects did not record per-series/frame/sample axes.
                # Refresh descriptive metadata from the unchanged original once.
                from .planes import describe_raster
                fresh = describe_raster(self.resolve(asset_id))
                refreshed = copy.deepcopy(existing)
                refreshed.update({key: value for key, value in fresh.items() if key != 'metadata'})
                refreshed['metadata'].update(fresh['metadata'])
                registry = {**self.registry, asset_id: refreshed}
                atomic_write(self.root / 'assets.json', json_bytes(registry))
                self.registry = registry
        record = copy.deepcopy(self.registry[asset_id])
        record.pop('_filename', None)
        return record

    def project_asset(self, supplied: dict) -> dict:
        record = self.public_asset(supplied['id'])
        metadata = supplied.get('metadata', {})
        for key in ('derived_from', 'analysis_source', 'chart_type', 'recipe_sha256', 'synthetic_demo', 'derivations', 'legacy'):
            if key in metadata:
                record['metadata'][key] = copy.deepcopy(metadata[key])
        return record

    def add_derived_source(self, name: str, data: bytes, metadata: dict, expected_identity: tuple):
        record = self._store_bytes(data, name)
        with self.lock:
            if expected_identity != (self.project['id'], self.project['revision']):
                raise ConflictError('Project changed while rendering the chart; generate it again for the current project.')
            project = copy.deepcopy(self.project)
            record['metadata'].update(json_safe(metadata))
            previous = next((a for a in project['assets'] if a['id'] == record['id']), None)
            if previous:
                previous['metadata'].update(record['metadata'])
                record = previous
            else:
                project['assets'].append(record)
            validate_project(project, self.registry)
            saved = self._advance(project, 'render_analysis_chart', name)
            return {'asset': copy.deepcopy(record), 'project': saved}

    def resolve(self, asset_id: str) -> Path:
        if not ID_RE.fullmatch(str(asset_id)):
            raise ValueError('Invalid asset id.')
        with self.lock:
            if self.cleanup_pending:
                raise WorkspaceCleanupPending('本机工作区清理尚未完成，请先重试清理。')
            record = self.registry.get(asset_id)
            if not record:
                raise ValueError('Source asset not found.')
            filename = record.get('_filename', '')
            if Path(filename).name != filename or not filename.startswith(asset_id + '.'):
                raise ValueError('Corrupt source registry.')
            path = self.objects / filename
            if not path.is_file():
                raise ValueError('Original source file is missing.')
            return path

    def _store_bytes(self, data: bytes, name: str, expected_id: str | None = None) -> dict:
        self._require_workspace_ready()
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError('Empty original or file exceeds 128 MB.')
        name = str(name).replace('\\', '/').split('/')[-1][:240]
        if not name or '\x00' in name:
            raise ValueError('Invalid source filename.')
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError(f'Unsupported file type: {suffix}')
        digest = hashlib.sha256(data).hexdigest()
        if expected_id and expected_id != digest:
            raise ValueError('Original source SHA-256 mismatch.')
        with self.lock:
            if digest in self.registry:
                existing = self.resolve(digest)
                if hashlib.sha256(existing.read_bytes()).hexdigest() != digest:
                    raise ValueError('Stored original failed its SHA-256 verification.')
                return self.public_asset(digest)
        fd, temp_name = tempfile.mkstemp(prefix='.import-', suffix=suffix, dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
            try:
                meta = inspect_source(Path(temp_name), name)
            except (OSError, RuntimeError, csv.Error, BadZipFile, IndexError, TypeError) as exc:
                raise ValueError('Source file cannot be decoded as its declared type: ' + str(exc).replace(temp_name, 'uploaded file')[:240]) from exc
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        filename = digest + suffix
        record = {'id': digest, 'sha256': digest, 'name': name, 'size_bytes': len(data),
                  **meta, 'preview_url': f'/api/assets/{digest}/preview', '_filename': filename}
        with self.lock:
            destination = self.objects / filename
            if not destination.exists():
                atomic_write(destination, data)
            elif hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError('A stored original has been modified outside the application.')
            # A failed registry write must not make this source appear registered
            # in memory: a subsequent retry would otherwise skip the missing
            # durable entry and save an unrecoverable project reference.
            registry = {**self.registry, digest: record}
            atomic_write(self.root / 'assets.json', json_bytes(registry))
            self.registry = registry
        return self.public_asset(digest)

    def add_files(self, files: list[tuple[str, bytes]]):
        if not files or len(files) > 64:
            raise ValueError('Import between 1 and 64 files at a time.')
        with self.lock:
            identity = (self.project['id'], self.project['revision'])
        records = [self._store_bytes(data, name) for name, data in files]
        with self.lock:
            if identity != (self.project['id'], self.project['revision']):
                raise ConflictError('Project changed while importing files; import again into the current project.')
            project = copy.deepcopy(self.project)
            existing = {a['id'] for a in project['assets']}
            for record in records:
                if record['id'] not in existing:
                    project['assets'].append(record)
                    existing.add(record['id'])
            if len(existing) > MAX_ASSETS:
                raise ValueError('This project supports at most 256 source assets.')
            validate_project(project, self.registry)
            self._advance(project, 'import_sources', ', '.join(a['name'] for a in records))
        return records

    def read_array(self, asset_id: str, page: int = 0, *, series: int = 0, frame: int | None = None) -> np.ndarray:
        from .models import ChannelSource
        from .planes import read_raster
        source = ChannelSource(asset_id=asset_id, series=series, frame=page if frame is None else frame, channel=0).model_dump()
        key = ('frame', asset_id, source['series'], source['frame'])
        cached = self.array_cache.get(key)
        if cached is not None:
            return cached
        path = self.resolve(asset_id)
        meta = self.public_asset(asset_id)
        if meta['kind'] != 'image':
            raise ValueError('Numerical image analysis requires an original raster image, not a PDF/SVG/table.')
        array = read_raster(path, series=source['series'], frame=source['frame'])
        return self.array_cache.put(key, array, array.nbytes)

    def read_plane(self, source: dict) -> np.ndarray:
        from .channels import source_spec
        from .models import ChannelSource
        from .planes import read_raster
        identity = ChannelSource.model_validate(source).model_dump()
        key = ('plane',) + tuple(identity[key] for key in ('asset_id', 'series', 'frame', 'channel'))
        cached = self.array_cache.get(key)
        if cached is not None:
            return cached
        source_spec(self, identity)
        frame = self.array_cache.get(('frame', identity['asset_id'], identity['series'], identity['frame']))
        if frame is not None:
            # The ordinary RGB preview may already have decoded this exact
            # frame. Reuse it, while copying strided components so their cache
            # byte charge does not conceal a retained whole RGB allocation.
            array = np.ascontiguousarray(frame[..., identity['channel']] if frame.ndim == 3 else frame)
            array.setflags(write=False)
        else:
            array = read_raster(self.resolve(identity['asset_id']), series=identity['series'],
                                frame=identity['frame'], channel=identity['channel'])
        return self.array_cache.put(key, array, array.nbytes)

    def describe_channels(self, asset_id, series=0, frame=0, percentiles=None, channel_indices=None):
        from .channels import describe_channels
        return describe_channels(self, asset_id, series, frame, percentiles, channel_indices)

    def read_table(self, asset_id: str):
        cached = self.table_cache.get(asset_id)
        if cached is not None:
            return copy.deepcopy(cached)
        path = self.resolve(asset_id)
        if self.public_asset(asset_id)['kind'] != 'table':
            raise ValueError('The selected asset is not a table.')
        value = _table_from_path(path, path.suffix.lower())
        self.table_cache.put(asset_id, value, len(json_bytes(value)))
        return copy.deepcopy(value)

    def preview(self, asset_id, max_size=1200, page=0, low=None, high=None, gamma=1.0, invert=False,
                channel=None, *, series=0, frame=None) -> bytes:
        max_size = max(32, min(int(max_size), 4096))
        gamma = _finite_number(gamma, 'Gamma', 0.1, 5)
        for name, value in [('low', low), ('high', high)]:
            if value is not None:
                _finite_number(value, name)
        page = page if frame is None else frame
        key = (asset_id, max_size, series, page, low, high, gamma, bool(invert), channel)
        cached = self.preview_cache.get(key)
        if cached is not None:
            return cached
        path = self.resolve(asset_id)
        meta = self.public_asset(asset_id)
        if meta['kind'] in {'svg', 'pdf'}:
            if series != 0:
                raise ValueError('Vector sources do not contain raster series.')
            import fitz
            if meta['kind'] == 'svg':
                from .vectors import open_svg
                doc = open_svg(path.read_bytes())
            else:
                doc = fitz.open(path)
            try:
                if page < 0 or page >= len(doc):
                    raise ValueError('PDF page is outside its source range.')
                source_page = doc[page]
                factor = max_size / max(source_page.rect.width, source_page.rect.height)
                pix = source_page.get_pixmap(matrix=fitz.Matrix(factor, factor), alpha=False)
                result = pix.tobytes('png')
            finally:
                doc.close()
        elif meta['kind'] == 'table':
            table = self.read_table(asset_id)
            image = Image.new('RGB', (900, 340), '#f7f9f8')
            draw = ImageDraw.Draw(image)
            draw.text((24, 20), f'TABLE / {table["total_rows"]} rows', fill='#153f37')
            lines = [', '.join(table['columns'])] + [', '.join(str(x) for x in row.values()) for row in table['rows'][:12]]
            for y, line in enumerate(lines):
                draw.text((24, 55 + y * 20), line[:125], fill='#333333')
            out = io.BytesIO()
            image.save(out, format='PNG')
            result = out.getvalue()
        else:
            from .channels import source_spec
            identity = {'asset_id': asset_id, 'series': series, 'frame': page, 'channel': 0 if channel is None else channel}
            _, spec = source_spec(self, identity)
            scalar_default = not spec.get('rgb_samples', False) or spec.get('logical_channels', 1) > 1
            if scalar_default or channel is not None:
                arr = self.read_plane(identity)
            else:
                arr = self.read_array(asset_id, page=page, series=series)
            from .imaging import display_image, display_bounds
            settings = {'low':low, 'high':high, 'gamma':gamma, 'invert':invert}
            settings['low'], settings['high'] = display_bounds(arr, settings)
            stride = max(1, int(max(arr.shape[:2]) // (max_size * 2)))
            image = display_image(arr[::stride, ::stride], settings)
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            image.save(out, format='PNG')
            result = out.getvalue()
        return self.preview_cache.put(key, result)

    def localization_array(self, asset_id, max_size=1024, *, series=0, frame=None, channel=None):
        frame = 0 if frame is None else frame
        max_size = max(32, min(int(max_size), 2048))
        key = (asset_id, max_size, series, frame, channel)
        cached = self.localization_cache.get(key)
        if cached is not None:
            return cached
        from .channels import source_spec
        identity = {'asset_id': asset_id, 'series': series, 'frame': frame, 'channel': 0 if channel is None else channel}
        _, spec = source_spec(self, identity)
        if channel is not None or not spec.get('rgb_samples') or spec.get('logical_channels', 1) > 1:
            arr = self.read_plane(identity)
        else:
            arr = self.read_array(asset_id, series=series, frame=frame)
        source_h, source_w = arr.shape[:2]
        max_size = max(32, min(int(max_size), 2048))
        stride = max(1, int(max(source_h, source_w) // (max_size * 2)))
        sampled = arr[::stride, ::stride]
        if sampled.ndim == 3:
            selected = sampled[..., :3]
            gray = np.mean(selected.astype(np.float32), axis=2)
        else:
            gray = sampled.astype(np.float32)
        finite = gray[np.isfinite(gray)]
        low, high = (float(finite.min()), float(finite.max())) if finite.size else (0, 1)
        normalized = np.nan_to_num((gray - low) / max(high - low, 1e-12), nan=0, posinf=1, neginf=0).clip(0, 1)
        image = Image.fromarray(normalized.astype(np.float32), mode='F')
        image.thumbnail((max(32, min(max_size, 2048)),) * 2, Image.Resampling.BILINEAR)
        small = np.ascontiguousarray(np.asarray(image), dtype='<f4')
        small.setflags(write=False)
        result = {'width': image.width, 'height': image.height, 'source_width': source_w, 'source_height': source_h,
                  'array': small, 'bit_depth': spec['bit_depth'],
                  'source': {'asset_id': asset_id, 'series': series, 'frame': frame, 'channel': channel},
                  'purpose': 'localization_preview_only'}
        return self.localization_cache.put(key, result, small.nbytes + 256)

    def localization_pixels(self, asset_id, max_size=1024, *, series=0, frame=None, channel=None):
        result = self.localization_array(asset_id, max_size, series=series, frame=frame, channel=channel)
        return {**{key: value for key, value in result.items() if key != 'array'},
                'data': result['array'].ravel().tolist()}

    def commit_measurement(self, project_id: str, revision: int, record: dict) -> bool:
        with self.lock:
            if self.project['id'] != project_id or self.project['revision'] != revision:
                return False
            project = copy.deepcopy(self.project)
            project['measurements'].append(json_safe(record))
            self._advance(project, 'analysis_completed', str(record.get('kind', 'analysis')))
            return True

    def verify_originals(self, project: dict) -> None:
        """Before delivery, reject sources modified outside the workbench."""
        validate_project(project, self.registry)
        for asset in project['assets']:
            path = self.resolve(asset['id'])
            with path.open('rb') as handle:
                actual = hashlib.file_digest(handle, 'sha256').hexdigest()
            if actual != asset['sha256']:
                raise ValueError('Original source SHA-256 no longer matches the project; restore the original before export.')

    def export_archive(self, project: dict | None = None) -> bytes:
        value = copy.deepcopy(project) if project is not None else self.get_project()
        validate_project(value, self.registry)
        output = io.BytesIO()
        records = []
        with ZipFile(output, 'w', compression=ZIP_DEFLATED, compresslevel=4) as archive:
            payload = json_bytes(value)
            archive.writestr('project.json', payload)
            total = len(payload)
            for asset in value['assets']:
                path = self.resolve(asset['id'])
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if digest != asset['sha256']:
                    raise ValueError('Original source failed SHA-256 verification; export aborted.')
                name = 'originals/' + asset['id'] + path.suffix.lower()
                total += len(data)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError('Project originals exceed archive size limit.')
                archive.writestr(name, data)
                records.append({'id': asset['id'], 'path': name, 'name': asset['name'], 'sha256': digest, 'size_bytes': len(data)})
            archive.writestr('manifest.json', json_bytes({'format': 'figwork', 'version': 1, 'project_sha256': hashlib.sha256(payload).hexdigest(), 'assets': records}))
        return output.getvalue()

    def open_archive(self, payload: bytes):
        with self.lock:
            opening_identity = (self.project['id'], self.project['revision'])
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise ValueError('Project archive exceeds 512 MB.')
        staged = []
        try:
            with ZipFile(io.BytesIO(payload)) as archive:
                members = _validate_zip(archive)
                if not {'project.json', 'manifest.json'}.issubset(members):
                    raise ValueError('Missing project.json or manifest.json.')
                if members['project.json'].file_size > MAX_PROJECT_BYTES or members['manifest.json'].file_size > 2 * 1024 * 1024:
                    raise ValueError('Project metadata exceeds size limit.')
                raw_project = archive.read('project.json')
                project = json.loads(raw_project)
                manifest = json.loads(archive.read('manifest.json'))
                if not isinstance(manifest, dict) or not isinstance(manifest.get('assets'), list) or len(manifest['assets']) > MAX_ASSETS:
                    raise ValueError('Invalid portable-project manifest structure.')
                if manifest.get('format') != 'figwork' or manifest.get('version') != 1:
                    raise ValueError('Unsupported portable project format.')
                if hashlib.sha256(raw_project).hexdigest() != manifest.get('project_sha256'):
                    raise ValueError('Project JSON SHA-256 mismatch.')
                validate_project(project)
                expected = {a['id']: a for a in project['assets']}
                seen = set()
                declared = {'project.json', 'manifest.json'}
                for entry in manifest.get('assets', []):
                    if not isinstance(entry, dict):
                        raise ValueError('Invalid portable-project source manifest.')
                    aid = entry.get('id')
                    name = entry.get('path')
                    if aid not in expected or aid in seen or name not in members or not name.startswith('originals/'):
                        raise ValueError('Invalid, duplicate or missing manifest source.')
                    if entry.get('sha256') != aid or members[name].file_size > MAX_FILE_BYTES:
                        raise ValueError('Invalid source hash or source size.')
                    data = archive.read(name)
                    if len(data) != entry.get('size_bytes') or hashlib.sha256(data).hexdigest() != aid:
                        raise ValueError('Original source SHA-256/size mismatch.')
                    filename = str(entry.get('name') or expected[aid]['name'])
                    if Path(filename).suffix.lower() != Path(name).suffix.lower():
                        raise ValueError('Source extension mismatch in project archive.')
                    staged.append((aid, filename, data))
                    seen.add(aid)
                    declared.add(name)
                if seen != set(expected):
                    raise ValueError('Portable project is missing one or more original source files.')
                if any(name not in declared and not info.is_dir() for name, info in members.items()):
                    raise ValueError('Unexpected undeclared file in portable project.')
        except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError('Invalid or damaged .figwork project.') from exc
        # All membership, hashes and size checks complete before active project changes.
        records = [self._store_bytes(data, name, aid) for aid, name, data in staged]
        project['assets'] = [self.project_asset({**record, 'metadata': {**record['metadata'], **expected[record['id']].get('metadata', {})}}) for record in records]
        validate_project(project, self.registry)
        with self.lock:
            if opening_identity != (self.project['id'], self.project['revision']):
                raise ConflictError('Project changed while opening the archive; open it again after saving the current work.')
            project['id'] = str(uuid.uuid4())  # new session identity prevents old jobs from attaching
            project['revision'] = 0
            project['updated_at'] = now_iso()
            _audit(project, 'open_portable_project', 'Verified original bytes and manifest')
            return self._commit_project(project)

    def import_legacy(self, payload: bytes, filename: str):
        from .migration import migrate_legacy
        with self.lock:
            opening_identity = (self.project['id'], self.project['revision'])
        migrated = migrate_legacy(payload, filename)
        project = copy.deepcopy(migrated.project)
        records = []
        seen = set()
        for source in migrated.sources:
            if source.asset_id in seen:
                continue
            record = self._store_bytes(source.data, source.name, expected_id=source.asset_id)
            if source.metadata:
                record['metadata']['legacy'] = json_safe(source.metadata.get('legacy', source.metadata))
            records.append(record)
            seen.add(record['id'])
        project['assets'] = records
        project['id'] = str(uuid.uuid4())
        project['revision'] = 0
        project.setdefault('created_at', now_iso())
        project['updated_at'] = now_iso()
        project.setdefault('settings', {})['migration_warnings'] = [str(message)[:2000] for message in migrated.warnings]
        project['settings']['migration_source_format'] = Path(filename).suffix.lower()
        project.setdefault('audit', [])
        _audit(project, 'import_legacy_project', Path(filename).name[:240])
        validate_project(project, self.registry)
        with self.lock:
            if opening_identity != (self.project['id'], self.project['revision']):
                raise ConflictError('Project changed during legacy import; import it again after saving.')
            return self._commit_project(project)

    def demo(self):
        """Deterministic, visibly labelled synthetic images and a synthetic table."""
        import tifffile
        rng = np.random.default_rng(1701)
        h, w = 360, 640
        yy, xx = np.mgrid[:h, :w]
        gel = np.full((h, w), 53000.0) + rng.normal(0, 500, (h, w))
        centers = [70, 170, 270, 370, 470, 570]
        for i, cx in enumerate(centers):
            rows = [60, 100, 150, 210, 280] if i == 0 else [105 + 3 * i, 255]
            for cy in rows:
                amplitude = 20000 + i * 2000
                gel -= amplitude * np.exp(-((xx-cx)/22)**6 - ((yy-cy)/5)**2)
        gel = np.clip(gel, 0, 65535).astype(np.uint16)
        out = io.BytesIO()
        tifffile.imwrite(out, gel, photometric='minisblack', metadata={'axes': 'YX', 'description': 'SYNTHETIC DEMO — not experimental data'})
        nuclei = np.zeros((360, 480), dtype=np.float64) + 400
        cytoplasm = np.full((360, 480), 250.0)
        marker = np.full((360, 480), 350.0)
        centers_nuclei = [(50,60,10),(100,140,12),(180,75,13),(260,90,12),(380,100,14),(330,220,11),(200,240,14),(100,280,10),(420,290,12)]
        for index, (cx, cy, radius) in enumerate(centers_nuclei):
            distance2 = (xx[:,:480]-cx)**2+(yy[:,:480]-cy)**2
            core = np.exp(-distance2/(2*radius**2))
            # C0 is byte-for-byte the original nine-nucleus demonstration.
            # The other channels are independent deterministic synthetic signals,
            # not dye validation, registered experimental data, or cell labels.
            nuclei += 30000 * core
            cytoplasm += 22000 * (np.exp(-distance2/(2*(radius*1.8)**2)) - .85*core)
            marker += (7000 + 3000*(index % 4)) * np.exp(
                -((xx[:,:480]-cx-3)**2+(yy[:,:480]-cy+2)**2)/(2*(radius*.8)**2))
        fluorescence = np.stack([nuclei.astype(np.uint16), np.clip(cytoplasm,0,65535).astype(np.uint16),
                                  np.clip(marker,0,65535).astype(np.uint16)])
        nuc = io.BytesIO()
        tifffile.imwrite(nuc, fluorescence, photometric='minisblack', ome=True,
                         metadata={'axes':'CYX', 'Name':'SYNTHETIC DEMO — three-channel fluorescence',
                                   'Description':'All values are synthetic. For workflow demonstration only, not experimental results.',
                                   'Channel': {'Name':['SYNTHETIC DAPI (nuclei)', 'SYNTHETIC cytoplasm', 'SYNTHETIC marker signal'],
                                               'Color':[65535,16711935,-16776961]}})
        table_text = 'sample,target,ct,group,biological_replicate,technical_replicate\n'
        for group, base in [('Control',24),('Treatment',22)]:
            for bio in range(1,4):
                for tech in range(1,3):
                    for target, ct in [('ACTB',20 + bio * .1),('GeneX',base+bio*.12+tech*.03)]:
                        table_text += f'{group}_{bio},{target},{ct:.4f},{group},{group}_{bio},{tech}\n'
        with self.lock:
            old = self.project
            self.project = new_project('示例数据 · 凝胶、荧光与 qPCR')
            try:
                assets = self.add_files([('DEMO_synthetic_gel_16bit.tif',out.getvalue()),('DEMO_synthetic_nuclei_16bit.tif',nuc.getvalue()),('DEMO_synthetic_qpcr.csv',table_text.encode())])
                project = copy.deepcopy(self.project)
                project['settings']['synthetic_demo'] = True
                project['settings']['demo_notice'] = '全部为合成示例，仅演示工作流，不代表实验结果。'
                project['panels'] = [
                    self.default_panel(assets[0], 'A', 8, 16, 100, 56.25),
                    self.default_panel(assets[1], 'B', 114, 16, 58, 43.5),
                ]
                project['panels'][0]['gel'].update({'lane_centers': centers, 'lane_names':['M','Control 1','Control 2','Treat 1','Treat 2','Treat 3']})
                project['annotations'] = [{'id':str(uuid.uuid4()),'type':'text','x_mm':8,'y_mm':5,'width_mm':160,'height_mm':7,'text':'SYNTHETIC DEMO / 示例数据','font_size_pt':11,'color':'#22665c'},
                                          {'id':str(uuid.uuid4()),'type':'text','x_mm':8,'y_mm':96,'width_mm':160,'height_mm':7,'text':'For workflow demonstration only. Not experimental results.','font_size_pt':8,'color':'#666666'}]
                self._advance(project,'create_demo','Deterministic synthetic sources, not experimental results')
                return self.get_project()
            except Exception:
                self.project = old
                self._persist()
                raise

    @staticmethod
    def default_panel(asset, label='A', x=10, y=10, width=80, height=50):
        return {'id':str(uuid.uuid4()),'asset_id':asset['id'],'x_mm':x,'y_mm':y,'width_mm':width,'height_mm':height,'label':label,'title':'',
                'crop':{'x':0,'y':0,'width':asset['width_px'],'height':asset['height_px']},'rotation':0,'flip_h':False,'flip_v':False,
                'display':{'low':None,'high':None,'gamma':1,'invert':False},
                'gel':{'lane_centers':[],'lane_names':[],'marker_points':[],'marker_unit':'kDa','hidden_lanes':[]}}



