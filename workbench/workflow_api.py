"""Read-only, revision-bound review endpoints for scientific workflows.

Original measurements remain immutable. The browser persists review operations
as an editing recipe; every reconstructed result reads the original pixel plane.
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import BoundedSemaphore

from fastapi import Body, HTTPException
from fastapi.responses import Response

from .models import ChannelSource
from .storage import ConflictError, json_bytes, json_safe

MAX_REVIEW_SOURCE_BYTES = 512 * 1024 * 1024
MAX_REVIEW_CHANNELS = 32


def register_workflow_routes(app, store):
    slots = BoundedSemaphore(2)

    @contextmanager
    def bounded_compute():
        if not slots.acquire(blocking=False):
            raise HTTPException(429, '已有两项复核正在计算，请稍后重试。', headers={'Retry-After': '1'})
        try:
            yield
        finally:
            slots.release()

    def current_project(payload):
        if len(json_bytes(payload)) > 8 * 1024 * 1024:
            raise ValueError('复核请求超过 8 MiB，请减少操作或测量区域。')
        project = store.get_project()
        revision = payload.get('revision', payload.get('project_revision'))
        if (payload.get('project_id') != project['id'] or isinstance(revision, bool)
                or not isinstance(revision, int) or revision != project['revision']):
            raise ConflictError('工程或修订已改变，请保存当前设置后重新复核。')
        return project

    def still_current(project):
        latest = store.get_project()
        if (latest['id'], latest['revision']) != (project['id'], project['revision']):
            raise ConflictError('计算期间工程已改变，结果未应用，请重新复核。')

    def read_source(source, project):
        source = ChannelSource.model_validate(source).model_dump()
        if source['asset_id'] not in {asset['id'] for asset in project['assets']}:
            raise ValueError('原始来源不属于当前工程。')
        return store.read_plane(source)

    def measurement_context(payload, project):
        record = next((entry for entry in project['measurements']
                       if entry.get('id') == payload.get('measurement_id')), None)
        if not record or record.get('kind') != 'microscopy':
            raise ValueError('请选择当前工程中已经保存的显微分割结果。')
        base_result = record.get('result') or {}
        mask = base_result.get('instance_mask')
        if not isinstance(mask, dict):
            raise ValueError('这份结果没有无损实例标签，请用当前版本重新分割。')
        expected = payload.get('base_mask_sha256')
        if expected is not None and expected != mask.get('sha256'):
            raise ConflictError('分割标签版本与复核记录不一致，未应用修改。')
        params = record.get('params') or {}
        operations = payload.get('operations', [])
        if (not isinstance(operations, list) or len(operations) > 200
                or any(not isinstance(operation, dict) for operation in operations)):
            raise ValueError('实例复核最多接受 200 条结构化操作。')
        channels = params.get('channels')
        if channels is not None:
            if (not isinstance(channels, list) or not 1 <= len(channels) <= MAX_REVIEW_CHANNELS
                    or any(not isinstance(channel, dict) for channel in channels)):
                raise ValueError('实例复核需要 1–32 个明确的原始通道。')
            fiber = params.get('method') in {'fiber_cross_section', 'myotube'}
            if not fiber and len(channels) != 1:
                raise ValueError('细胞与细胞核实例复核需要唯一的原始强度通道。')
            images, total_bytes = {}, 0
            for channel in channels:
                key = str(channel.get('id', ''))
                if not key or key in images:
                    raise ValueError('保存的原始通道标识无效。')
                image = read_source(channel.get('source') or {}, project)
                total_bytes += image.nbytes
                if total_bytes > MAX_REVIEW_SOURCE_BYTES:
                    raise ValueError('单次实例复核原始平面累计超过 512 MiB，请减少同批通道。')
                images[key] = image
            raw = images if fiber else next(iter(images.values()))
        else:
            source = dict(params.get('source') or {})
            source.setdefault('asset_id', params.get('asset_id'))
            for name in ('series', 'frame', 'channel'):
                source.setdefault(name, params.get(name, params.get('page', 0) if name == 'frame' else 0))
            raw = read_source(source, project)
            if raw.nbytes > MAX_REVIEW_SOURCE_BYTES:
                raise ValueError('实例复核原始平面超过 512 MiB，请使用较小原始图像。')
        return record, raw, operations

    def reconstructed(payload):
        from .instances import review_instances
        project = current_project(payload)
        record, raw, operations = measurement_context(payload, project)
        result = review_instances(record['result'], raw, operations,
                                  cursor=payload.get('cursor'), params=record.get('params') or {})
        still_current(project)
        return json_safe(result)

    @app.post('/api/instances/review')
    def instance_review(payload: dict = Body(...)):
        with bounded_compute():
            return reconstructed(payload)

    @app.post('/api/instances/export')
    def instance_export(payload: dict = Body(...)):
        from .instances import label_tiff
        with bounded_compute():
            result = reconstructed(payload)
            data = label_tiff(result)
        return Response(data, media_type='image/tiff', headers={
            'Content-Disposition': 'attachment; filename="reviewed_instance_labels.tiff"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        })

    @app.post('/api/wb/exposure-review')
    def exposure_review(payload: dict = Body(...)):
        from .wb_workflow import review_wb_exposures
        with bounded_compute():
            project = current_project(payload)
            rois = payload.get('rois')
            if (not isinstance(rois, list) or not 1 <= len(rois) <= 5000
                    or any(not isinstance(roi, dict) for roi in rois)):
                raise ValueError('请选择 1–5000 个具备实验身份的测量区域。')
            images, total_bytes = {}, 0
            for roi in rois:
                source = dict(roi.get('source') or {})
                source.setdefault('asset_id', roi.get('asset_id'))
                source = ChannelSource.model_validate(source).model_dump()
                key = tuple(source[name] for name in ('asset_id', 'series', 'frame', 'channel'))
                if key in images:
                    continue
                if len(images) >= 32:
                    raise ValueError('单次曝光复核最多比较 32 个原始平面，请按实验系列分批。')
                image = read_source(source, project)
                total_bytes += image.nbytes
                if total_bytes > 512 * 1024 * 1024:
                    raise ValueError('单次曝光原始平面累计超过 512 MiB，请减少同批曝光。')
                images[key] = image
            result = review_wb_exposures(images, payload)
            still_current(project)
            return json_safe(result)
