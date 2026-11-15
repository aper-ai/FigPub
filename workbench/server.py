"""Loopback HTTP API. Routes only coordinate typed storage, workers and exporters."""
from __future__ import annotations
import asyncio
import json
import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .jobs import JobManager
from . import __version__
from .runtime_paths import bundle_root, default_data_dir
from .storage import (ConflictError, WorkspaceRecoveryPending, WorkspaceCleanupPending,
                      MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, ProjectStore,
                      json_safe, json_bytes, validate_project)

VERSION = __version__
PACKAGE_ROOT = bundle_root()
LOGGER = logging.getLogger(__name__)


class WorkspaceRequestGate:
    """Drain API work around cleanup so an old request cannot rewrite copies."""

    def __init__(self, app, store):
        self.app, self.store = app, store
        self.condition = asyncio.Condition()
        self.active = 0
        self.clearing = False
        self.waiting = 0
        self.generation = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/api/'):
            return await self.app(scope, receive, send)
        path, method = scope['path'], scope['method']
        if path == '/api/demo':
            # Retired production endpoint must not fall through to the static
            # site's catch-all mount or invoke a testing fixture.
            return await JSONResponse({'detail': 'Not Found'}, status_code=404)(scope, receive, send)
        clearing = path == '/api/workspace/clear' and method == 'POST'
        safe_paths = {'/api/health', '/api/cellpose/status', '/api/project', '/api/workspace/status', '/api/benchmarks'}
        safe_read = path in safe_paths and method in {'GET', 'HEAD'}
        request_generation = self.generation
        reject_stale = False
        async with self.condition:
            if clearing:
                # The boundary begins when cleanup is requested, not only when
                # deletion starts. Late uploads must not queue behind it and
                # recreate private sources immediately after successful clear.
                self.generation += 1
                self.waiting += 1
                try:
                    await asyncio.wait_for(self.condition.wait_for(lambda: not self.active and not self.clearing), 10)
                except asyncio.TimeoutError:
                    response = JSONResponse({'detail': '仍有文件或预览请求正在处理，请稍后重试清理。',
                                             'code': 'workspace_busy'}, status_code=409)
                    return await response(scope, receive, send)
                finally:
                    # Client disconnect/cancellation must release its queue
                    # ticket as well; otherwise every later API request waits.
                    self.waiting -= 1
                    self.condition.notify_all()
                self.clearing = True
            else:
                reject_stale = not safe_read and (self.clearing or self.waiting
                                                  or request_generation != self.generation)
                if not reject_stale:
                    await self.condition.wait_for(lambda: not self.clearing and not self.waiting)
                    reject_stale = not safe_read and request_generation != self.generation
                    if not reject_stale:
                        self.active += 1
        if reject_stale:
            return await JSONResponse({'detail': '工作区正在清理或已发生切换。此操作未执行，请清理完成后重新选择文件。',
                                       'code': 'workspace_changed'}, status_code=409)(scope, receive, send)
        try:
            allowed = safe_read
            if self.store.cleanup_pending and not (allowed or clearing):
                response = JSONResponse({'detail': '本机工作区清理尚未完成，请先重试清理。',
                                         'code': 'workspace_cleanup_pending'}, status_code=409)
                return await response(scope, receive, send)
            choosing = method == 'POST' and path in {'/api/project/new', '/api/workspace/restore', '/api/workspace/clear'}
            if self.store.has_pending_recovery and not (allowed or choosing):
                response = JSONResponse({'detail': '检测到上次工作区，请先选择恢复、明确新建或清除本机工作区。',
                                         'code': 'workspace_recovery_pending'}, status_code=409)
                return await response(scope, receive, send)
            source_request = re.fullmatch(r'/api/assets/([a-f0-9]{64})/[^/]+', path)
            if source_request and method in {'GET', 'HEAD'} and source_request.group(1) not in self.store.registry:
                return await JSONResponse({'detail': 'Source is no longer present in this workspace.'},
                                          status_code=404)(scope, receive, send)
            return await self.app(scope, receive, send)
        finally:
            async with self.condition:
                if clearing:
                    self.clearing = False
                else:
                    self.active -= 1
                self.condition.notify_all()


class WorkbenchStaticFiles(StaticFiles):
    """Do not let Windows file associations change executable module MIME types."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        suffix = Path(getattr(response, 'path', path)).suffix.lower()
        content_types = {
            '.js': 'text/javascript; charset=utf-8',
            '.mjs': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.html': 'text/html; charset=utf-8',
            '.json': 'application/json',
        }
        if suffix in content_types:
            response.headers['Content-Type'] = content_types[suffix]
            # Upgrading the portable directory must not reuse an older UI module.
            response.headers['Cache-Control'] = 'no-cache'
        return response


def _download(data, mime, filename):
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', filename) or 'figure'
    return Response(content=data, media_type=mime, headers={
        'Content-Disposition': f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}',
        'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'no-store',
    })


async def _read_upload(upload: UploadFile, maximum=MAX_FILE_BYTES):
    chunks, size = [], 0
    while True:
        chunk = await upload.read(min(1024 * 1024, maximum + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            raise ValueError(f'Upload exceeds {maximum // 1024 // 1024} MB.')
        chunks.append(chunk)
    return b''.join(chunks)


def create_app(data_dir: str | Path | None = None, *, runner=None, instance_id: str | None = None,
               restore_workspace: bool = False) -> FastAPI:
    resource_root = bundle_root()
    store = ProjectStore(data_dir or default_data_dir(), restore_on_start=restore_workspace)
    manager = JobManager(store, runner=runner)
    from .preview_cache import PreviewCache, PreviewBusyError
    channel_previews = PreviewCache()

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            # The HTTP server has drained requests. Cancellation must also finish
            # worker cleanup before the application releases its decoded caches.
            await run_in_threadpool(manager.shutdown, wait=True)
            store.clear_caches()
            channel_previews.clear()

    app = FastAPI(title='Figure Workbench', version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.jobs = manager
    app.add_middleware(WorkspaceRequestGate, store=store)
    from .workflow_api import register_workflow_routes
    register_workflow_routes(app, store)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])

    @app.middleware('http')
    async def local_request_guard(request: Request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            allowed_origin = f'{request.url.scheme}://{request.headers.get("host", "")}'
            if origin and origin.rstrip('/') != allowed_origin:
                return JSONResponse({'detail': 'Cross-origin writes to the local workbench are blocked.'}, status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': 'Cross-site writes are blocked.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path.startswith('/api/'):
            # Originals, rendered previews, pixel buffers and project metadata
            # belong to the user and must not become persistent browser assets.
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Pragma'] = 'no-cache'
        if request.url.path == '/api/workspace/clear' and 200 <= response.status_code < 300:
            response.headers['Clear-Site-Data'] = '"cache"'
        # App code, sources and workers are local. No external CDN/telemetry/API.
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; worker-src 'self' blob:; connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ConflictError)
    async def conflict_handler(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'revision_conflict', 'current_revision': store.get_project()['revision']}, status_code=409)

    @app.exception_handler(WorkspaceRecoveryPending)
    async def recovery_handler(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'workspace_recovery_pending'}, status_code=409)

    @app.exception_handler(WorkspaceCleanupPending)
    async def cleanup_handler(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'workspace_cleanup_pending'}, status_code=409)

    @app.exception_handler(ValidationError)
    async def schema_handler(request, exc):
        messages = ['.'.join(str(x) for x in error['loc']) + ': ' + error['msg'] for error in exc.errors(include_input=False)]
        return JSONResponse({'detail': '; '.join(messages)[:3000], 'code': 'invalid_project'}, status_code=400)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'invalid_input'}, status_code=400)

    @app.exception_handler(PreviewBusyError)
    async def preview_busy_handler(request, exc):
        return JSONResponse({'detail': str(exc), 'code': 'preview_busy'}, status_code=429, headers={'Retry-After': '1'})

    @app.exception_handler(FileNotFoundError)
    async def file_handler(request, exc):
        return JSONResponse({'detail': 'Required local source file is missing.'}, status_code=404)

    @app.exception_handler(PermissionError)
    async def permission_handler(request, exc):
        LOGGER.exception('Cannot access a local project file', exc_info=exc)
        return JSONResponse({'detail': '无法读写本机工程文件。请检查文件是否被其他程序占用，以及工程目录的读写权限。',
                             'code': 'file_permission'}, status_code=403)

    @app.exception_handler(OSError)
    async def local_io_handler(request, exc):
        LOGGER.exception('Local project I/O failed', exc_info=exc)
        return JSONResponse({'detail': '本地文件读写失败，操作未完成。请检查可用磁盘空间及存储设备状态后重试。',
                             'code': 'local_io_error'}, status_code=503)

    @app.get('/api/health')
    def health():
        from .cellpose_runtime import cellpose_status
        cellpose = cellpose_status(probe=False)
        return {'ok': True, 'application': 'figure-workbench', 'version': VERSION,
                'instance_id': instance_id, 'capabilities': {
            'local_only': True, 'raw_16bit': True, 'portable_projects': True,
            'wb': True, 'qpcr': True, 'microscopy_cpu': True, 'multichannel': True,
            'wb_draft': True, 'exposure_review': True, 'advanced_ihc': True,
            'fibers': True, 'instance_review': True,
            'cellpose': bool(cellpose.get('available')), 'cellpose_status': cellpose,
            'exports': ['pdf', 'svg', 'png', 'tiff', 'pptx', 'sources', 'project'],
            'max_original_mb': MAX_FILE_BYTES // 1024 // 1024,
        }}

    @app.get('/api/cellpose/status')
    def cellpose_runtime_status(refresh: bool = False):
        from .cellpose_runtime import cellpose_status
        return json_safe(cellpose_status(probe=True, refresh=refresh))

    @app.get('/api/project')
    def project_get():
        return store.get_project()

    @app.put('/api/project')
    def project_put(payload: dict = Body(...)):
        return store.update_project(payload)

    @app.post('/api/project/new')
    def project_new(payload: dict = Body(default={})):
        return store.reset(str(payload.get('title') or '未命名图稿'))

    @app.get('/api/workspace/status')
    def workspace_status():
        return store.workspace_status()

    @app.post('/api/workspace/restore')
    def workspace_restore():
        return store.restore_workspace()

    @app.post('/api/workspace/clear')
    def workspace_clear(payload: dict = Body(default={})):
        if payload.get('confirm') is not True:
            raise HTTPException(400, '请明确确认删除软件保存的本机工作区副本。')
        with manager.lock:
            if any(job.get('_active', False) for job in manager.jobs.values()):
                return JSONResponse({'detail': '分析任务尚未完全结束，请等待完成或取消后重试清理。',
                                     'code': 'workspace_busy'}, status_code=409)
            result = store.clear_workspace()
            manager.jobs.clear()
            channel_previews.clear()
            return result

    @app.post('/api/assets')
    async def assets(files: list[UploadFile] = File(...)):
        if not files or len(files) > 64:
            raise ValueError('Import between 1 and 64 source files.')
        uploads = []
        total = 0
        try:
            for upload in files:
                data = await _read_upload(upload)
                total += len(data)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError('Combined upload exceeds 512 MB.')
                uploads.append((upload.filename or 'unnamed', data))
        finally:
            for upload in files:
                await upload.close()
        result = await run_in_threadpool(store.add_files, uploads)
        return {'assets': result}

    @app.get('/api/assets/{asset_id}/preview')
    def preview(asset_id: str, max_size: int = Query(1200, ge=32, le=4096), page: int = Query(0, ge=0),
                low: float | None = None, high: float | None = None, gamma: float = 1, invert: bool = False,
                channel: int | None = Query(None, ge=0), series: int = Query(0, ge=0), frame: int | None = Query(None, ge=0)):
        data = store.preview(asset_id, max_size, page, low, high, gamma, invert, channel, series=series, frame=frame)
        return Response(data, media_type='image/png', headers={'Cache-Control': 'no-store'})

    @app.get('/api/assets/{asset_id}/original')
    def original(asset_id: str):
        path = store.resolve(asset_id)
        name = store.public_asset(asset_id)['name']
        return FileResponse(path, media_type='application/octet-stream', filename=name,
                            headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})

    @app.get('/api/assets/{asset_id}/channels')
    def channel_descriptor(asset_id: str, series: int = Query(0, ge=0), frame: int = Query(0, ge=0),
                           channel_indices: list[int] | None = Query(None)):
        return json_safe(store.describe_channels(asset_id, series=series, frame=frame, channel_indices=channel_indices))

    @app.post('/api/channel-view/describe')
    def describe_channel_view(payload: dict = Body(...)):
        from .channels import validate_view, tile_layout
        validated = validate_view(payload.get('channel_view'), store)
        layout = tile_layout(validated['view'], validated['width_px'], validated['height_px'], crop=payload.get('crop'))
        return json_safe({'channel_view': validated['view'], 'layout': layout,
                          'source_asset_ids': validated['source_ids']})

    @app.post('/api/channel-view/preview')
    def channel_view_preview(payload: dict = Body(...)):
        from .channels import validate_view, render_view
        from .imaging import png_bytes
        max_size = payload.get('max_size', 1600)
        if isinstance(max_size, bool) or not isinstance(max_size, int) or not 32 <= max_size <= 4096:
            raise ValueError('Preview size must be between 32 and 4096 pixels.')
        validated = validate_view(payload.get('channel_view'), store)
        view, crop = validated['view'], payload.get('crop')
        key = hashlib.sha256(json_bytes({'view': view, 'crop': crop, 'max_size': max_size})).hexdigest()
        data = channel_previews.get_or_create(key, lambda: png_bytes(render_view(store, view, crop=crop, max_size=max_size)[0]))
        return Response(data, media_type='image/png', headers={'Cache-Control': 'no-store'})

    @app.post('/api/channel-view/auto-range')
    def channel_view_range(payload: dict = Body(...)):
        from .channels import auto_range, validate_view
        view = validate_view(payload.get('channel_view'), store)['view']
        percentiles = payload.get('percentiles', [1, 99.5])
        selected = payload.get('channel_ids')
        if selected is not None and (not isinstance(selected, list) or any(not isinstance(cid, str) for cid in selected)
                                     or not set(selected).issubset({c['id'] for c in view['channels']})):
            raise ValueError('Select valid channel IDs for automatic display levels.')
        result = auto_range(store, view, percentiles=percentiles, channel_ids=selected)
        result['statistics'] = result.get('levels', [])
        return json_safe(result)

    @app.get('/api/assets/{asset_id}/pixels')
    def pixels(asset_id: str, max_size: int = Query(1024, ge=32, le=2048), series: int = Query(0, ge=0),
               frame: int = Query(0, ge=0), channel: int | None = Query(None, ge=0)):
        return store.localization_pixels(asset_id, max_size, series=series, frame=frame, channel=channel)

    @app.get('/api/assets/{asset_id}/pixels.bin')
    def pixel_binary(asset_id: str, max_size: int = Query(1024, ge=32, le=2048), series: int = Query(0, ge=0),
                     frame: int = Query(0, ge=0), channel: int | None = Query(None, ge=0)):
        result = store.localization_array(asset_id, max_size, series=series, frame=frame, channel=channel)
        headers = {
            'X-Image-Width': str(result['width']), 'X-Image-Height': str(result['height']),
            'X-Source-Width': str(result['source_width']), 'X-Source-Height': str(result['source_height']),
            'X-Bit-Depth': str(result['bit_depth']), 'X-Pixel-Format': 'float32-le',
            'Cache-Control': 'no-store',
        }
        return Response(result['array'].tobytes(order='C'), media_type='application/octet-stream', headers=headers)

    @app.get('/api/assets/{asset_id}/table')
    def table(asset_id: str, limit: int = Query(1000, ge=1, le=10000)):
        value = store.read_table(asset_id)
        value['rows'] = value['rows'][:limit]
        value['truncated'] = value['total_rows'] > limit
        return value

    @app.get('/api/assets/{asset_id}/qpcr')
    def qpcr_table(asset_id: str, sheet: str | None = None, limit: int = Query(1000, ge=1, le=10000)):
        from .qpcr import included_flag, read_qpcr_table
        if store.public_asset(asset_id)['kind'] != 'table':
            raise ValueError('Choose a qPCR data table.')
        data = read_qpcr_table(store.resolve(asset_id), sheet=sheet)
        rows, samples = data['rows'], {}
        for row in rows:
            row['included'] = included_flag(row.get('included', True))
            experiment = str(row.get('experiment_id') or 'experiment-1').strip()
            sample = str(row.get('sample') or '').strip()
            key = (experiment, sample)
            value = {'experiment_id': experiment, 'sample': sample, 'group': str(row.get('group') or '').strip(),
                     'biological_replicate': str(row.get('biological_replicate') or sample).strip()}
            if key in samples and samples[key] != value:
                data['warnings'].append(f'样本 {experiment}::{sample} 在不同孔的组别或生物重复身份不一致，请复核原表。')
            samples[key] = value
        data.update(total_rows=len(rows), rows=rows[:limit], truncated=len(rows) > limit,
                    targets=sorted({str(row.get('target') or '').strip() for row in rows if row.get('target')}),
                    groups=sorted({str(row.get('group') or '').strip() for row in rows if row.get('group')}),
                    sample_groups=list(samples.values()))
        data['warnings'] = list(dict.fromkeys(data['warnings']))
        return json_safe(data)

    @app.post('/api/jobs')
    def submit_job(payload: dict = Body(...)):
        return manager.submit(str(payload.get('kind', '')), payload.get('params', {}))

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: str):
        try:
            return manager.get(job_id)
        except KeyError:
            raise HTTPException(404, 'Analysis job not found.')

    @app.delete('/api/jobs/{job_id}')
    def cancel_job(job_id: str):
        try:
            return manager.cancel(job_id)
        except KeyError:
            raise HTTPException(404, 'Analysis job not found.')

    @app.post('/api/charts')
    def create_chart(payload: dict = Body(...)):
        from .charts import analysis_chart
        project = store.get_project()
        measurement_id = str(payload.get('measurement_id', ''))
        measurement = next((entry for entry in project['measurements'] if entry.get('id') == measurement_id), None)
        if not measurement or not isinstance(measurement.get('result'), dict):
            raise ValueError('Run an analysis and select its saved result before creating a chart.')
        kind = str(payload.get('chart_type', 'bar'))
        width, height = float(payload.get('width_mm', 80)), float(payload.get('height_mm', 60))
        if not 10 <= width <= 500 or not 10 <= height <= 500:
            raise ValueError('Chart dimensions must be between 10 and 500 mm.')
        result = measurement['result']
        recipe = {'measurement_id': measurement_id, 'source_asset_ids': measurement.get('source_asset_ids', []),
                  'params': measurement.get('params', {}), 'result_sha256': hashlib.sha256(json_bytes(result)).hexdigest()}
        data = analysis_chart(result, chart_type=kind, width_mm=width, height_mm=height, provenance=recipe)
        metadata = {'derived_from': measurement_id, 'analysis_source': recipe, 'chart_type': kind,
                    'recipe_sha256': hashlib.sha256(json_bytes(recipe)).hexdigest()}
        return store.add_derived_source('analysis_' + measurement_id[:8] + '.svg', data, metadata,
                                        (project['id'], project['revision']))

    @app.post('/api/preflight')
    def preflight(payload: dict = Body(default={})):
        from .preflight import check_project
        project = payload.get('project') or store.get_project()
        validate_project(project, store.registry)
        minimum = float(payload.get('min_ppi', project.get('settings', {}).get('min_ppi', 300)))
        if not 20 <= minimum <= 2400:
            raise ValueError('Minimum resolution must be between 20 and 2400 PPI.')
        return json_safe(check_project(project, store.resolve, min_ppi=minimum))

    @app.post('/api/export')
    def export(payload: dict = Body(...)):
        project = payload.get('project') or store.get_project()
        validate_project(project, store.registry)
        fmt = str(payload.get('format', 'pdf')).lower()
        title = str(project.get('title', 'figure'))[:100]
        if fmt in {'project', 'sources'}:
            archive = store.export_archive(project)
            extension = '.figwork' if fmt == 'project' else '_sources.zip'
            return _download(archive, 'application/zip', title + extension)
        if fmt not in {'pdf', 'svg', 'png', 'tiff', 'pptx'}:
            raise ValueError('Unsupported figure export format.')
        dpi = int(payload.get('dpi', 300))
        if not 72 <= dpi <= 1200:
            raise ValueError('Export resolution must be between 72 and 1200 DPI.')
        store.verify_originals(project)
        from .exporting import export_project
        data, mime, filename = export_project(project, store.resolve, fmt, dpi=dpi, outlined_text=bool(payload.get('outlined_text', False)))
        return _download(data, mime, filename)

    @app.post('/api/project/open')
    async def project_open(file: UploadFile = File(...)):
        filename = str(file.filename or '')
        suffix = Path(filename).suffix.lower()
        if suffix not in {'.figwork', '.gelfig', '.sfig'}:
            raise ValueError('Choose a .figwork project or a supported legacy .gelfig / .sfig project.')
        try:
            data = await _read_upload(file, MAX_ARCHIVE_BYTES)
        finally:
            await file.close()
        if suffix == '.figwork':
            return await run_in_threadpool(store.open_archive, data)
        return await run_in_threadpool(store.import_legacy, data, filename)

    @app.get('/api/benchmarks')
    def benchmarks():
        path = resource_root / 'benchmarks' / 'results.json'
        if not path.exists():
            path = resource_root / 'benchmarks' / 'aggregatedresults.json'
        if path.exists():
            try:
                return json.loads(path.read_text('utf-8'))
            except ValueError:
                pass
        return {'status': 'not_run', 'message': 'No measured benchmark results have been recorded for this build.'}

    @app.get('/fonts/{font_name}')
    def font_file(font_name: str):
        allowed = {'DejaVuSans.ttf', 'DejaVuSans-Bold.ttf', 'NotoSansCJK-Regular.ttc', 'NotoSansCJK-SC.woff2'}
        if font_name not in allowed:
            raise HTTPException(404, 'Font resource not found.')
        path = resource_root / 'resources' / 'fonts' / font_name
        if not path.is_file():
            raise HTTPException(404, 'Font resource not installed.')
        mime = 'font/collection' if path.suffix == '.ttc' else 'font/woff2' if path.suffix == '.woff2' else 'font/ttf'
        return FileResponse(path, media_type=mime,
                            headers={'Cache-Control': 'private, max-age=86400'})

    web = resource_root / 'web'
    if web.is_dir():
        app.mount('/', WorkbenchStaticFiles(directory=web, html=True), name='web')
    return app


# Use create_app as an ASGI factory. Importing this module never opens a project
# or starts worker pools; the launcher acquires its single-writer lock first.

