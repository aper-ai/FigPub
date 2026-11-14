"""Bounded local analysis jobs with cancellation and optimistic result commits."""
from __future__ import annotations
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .storage import ProjectStore, json_safe, json_bytes, now_iso

MAX_ANALYSIS_SOURCE_BYTES = 512 * 1024 * 1024
MAX_ANALYSIS_PLANES = 32


def _check_cancel(event):
    if event is not None and event.is_set():
        raise ValueError('分析已取消。')


class JobManager:
    def __init__(self, store: ProjectStore, max_workers: int = 2, max_pending: int = 12,
                 runner: Callable[[str, dict], dict] | None = None):
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='figure-analysis')
        self.max_pending = max_pending
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.runner = runner or self._analyze
        self._default_runner = runner is None
        self.closed = False

    def submit(self, kind: str, params: dict) -> dict:
        if kind not in {'wb', 'qpcr', 'microscopy'}:
            raise ValueError('Unsupported analysis kind.')
        if not isinstance(params, dict):
            raise ValueError('Analysis params must be an object.')
        params = json_safe(copy.deepcopy(params))
        if len(json_bytes(params)) > 16 * 1024 * 1024:
            raise ValueError('Analysis parameters exceed the 16 MB limit.')
        if kind == 'wb':
            rois = params.get('rois', [])
            if not isinstance(rois, list) or len(rois) > 5000 or any(not isinstance(roi, dict) for roi in rois):
                raise ValueError('WB rois must be a list of at most 5,000 ROI objects.')
            if any(roi.get('source') is not None and not isinstance(roi['source'], dict) for roi in rois):
                raise ValueError('Each ROI source must be an original plane object.')
        if kind == 'qpcr' and 'rows' in params and not isinstance(params['rows'], list):
            raise ValueError('Inline qPCR rows must be a list of records.')
        if params.get('source') is not None and not isinstance(params['source'], dict):
            raise ValueError('Analysis source must be an original plane object.')
        if kind == 'microscopy' and 'channels' in params:
            channels = params['channels']
            if not isinstance(channels, list) or not 1 <= len(channels) <= 32 or any(
                    not isinstance(c, dict) or not isinstance(c.get('source'), dict) for c in channels):
                raise ValueError('Microscopy channels must contain 1–32 original plane objects.')
            if params.get('method') in {'ihc', 'ihc_advanced'}:
                raise ValueError('IHC needs the complete true RGB source; use asset_id/series/frame without scalar channels.')
        project = self.store.get_project()
        sources = self._source_ids(kind, params)
        current_ids = {asset['id'] for asset in project['assets']}
        if any(aid not in current_ids for aid in sources):
            raise ValueError('Analysis references an asset outside the current project.')
        with self.lock:
            if self.closed:
                raise ValueError('Analysis worker is shutting down.')
            if sum(job.get('_active', False) for job in self.jobs.values()) >= self.max_pending:
                raise ValueError('分析队列已满，或已取消任务仍在释放计算资源；请稍后重试。')
            self._prune()
            jid = str(uuid.uuid4())
            record = {'id': jid, 'kind': kind, 'status': 'queued', 'params': params,
                      'source_asset_ids': sources, 'project_id': project['id'],
                      'project_revision': project['revision'], 'created_at': now_iso(),
                      '_cancel': threading.Event(), '_started': time.perf_counter(), '_active': True}
            self.jobs[jid] = record
            record['_future'] = self.executor.submit(self._run, jid)
            return self.get(jid)

    @staticmethod
    def _source_ids(kind, params):
        if kind == 'wb':
            return sorted({str((roi.get('source') or {}).get('asset_id') or roi.get('asset_id'))
                           for roi in params.get('rois', []) if (roi.get('source') or {}).get('asset_id') or roi.get('asset_id')})
        ids = {str(params['asset_id'])} if params.get('asset_id') else set()
        if (params.get('source') or {}).get('asset_id'):
            ids.add(str(params['source']['asset_id']))
        for channel in params.get('channels', []) if kind == 'microscopy' else []:
            if (channel.get('source') or {}).get('asset_id'):
                ids.add(str(channel['source']['asset_id']))
        return sorted(ids)

    def _prune(self):
        if len(self.jobs) < 100:
            return
        for jid in list(self.jobs):
            if not self.jobs[jid].get('_active', False):
                del self.jobs[jid]
            if len(self.jobs) < 80:
                break

    def get(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise KeyError(jid)
            record = {k: v for k, v in self.jobs[jid].items() if not k.startswith('_') and k != 'params'}
            return copy.deepcopy(record)

    def cancel(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise KeyError(jid)
            job = self.jobs[jid]
            if job['status'] in {'queued', 'running'}:
                job['_cancel'].set()
                job['status'] = 'cancelled'
                job['cancel_requested'] = True
                # Cancelling a queued Future actually removes its execution.
                # A running Python/native operation may only stop at its next
                # cancellation checkpoint; retain its slot until _run exits.
                future = job.get('_future')
                if future is not None and future.cancel():
                    job['_active'] = False
                job['cleanup_pending'] = job['_active']
                job['elapsed_ms'] = round((time.perf_counter() - job['_started']) * 1000, 2)
            return self.get(jid)

    def _run(self, jid):
        with self.lock:
            job = self.jobs[jid]
            if job['_cancel'].is_set():
                job['_active'] = False
                job['cleanup_pending'] = False
                return
            job['status'] = 'running'
            kind, params = job['kind'], copy.deepcopy(job['params'])
        try:
            result = json_safe(self._analyze(kind, params, cancel_event=job['_cancel'])
                               if self._default_runner else self.runner(kind, params))
            with self.lock:
                if job['_cancel'].is_set():
                    return
                measurement = {
                    'id': jid, 'kind': kind, 'params': params,
                    'source_asset_ids': job['source_asset_ids'],
                    'source_project_revision': job['project_revision'], 'created_at': now_iso(), 'result': result,
                }
                committed = self.store.commit_measurement(job['project_id'], job['project_revision'], measurement)
                job.update(status='done', result=result, stale=not committed, committed=committed,
                           elapsed_ms=round((time.perf_counter() - job['_started']) * 1000, 2))
                if not committed:
                    job['message'] = 'Project changed during analysis; result is available but was not attached. Run again for the current project.'
        except Exception as exc:
            with self.lock:
                if not job['_cancel'].is_set():
                    job.update(status='error', error=str(exc)[:2000],
                               elapsed_ms=round((time.perf_counter() - job['_started']) * 1000, 2))
        finally:
            with self.lock:
                job['_active'] = False
                if job.get('cancel_requested'):
                    job['cleanup_pending'] = False

    def _analyze(self, kind, params, *, cancel_event=None):
        _check_cancel(cancel_event)
        total_source_bytes = 0

        def read_plane(source):
            nonlocal total_source_bytes
            _check_cancel(cancel_event)
            image = self.store.read_plane(source)
            total_source_bytes += image.nbytes
            if total_source_bytes > MAX_ANALYSIS_SOURCE_BYTES:
                raise ValueError('单次分析的原始平面累计超过 512 MiB，请减少同批通道或曝光并分批分析。')
            _check_cancel(cancel_event)
            return image

        if kind == 'wb':
            from .analysis import analyze_wb
            images = {}
            for roi in params.get('rois', []):
                source = dict(roi.get('source') or {})
                source.setdefault('asset_id', roi.get('asset_id', ''))
                source.setdefault('series', params.get('series', 0))
                source.setdefault('frame', params.get('frame', params.get('page', 0)))
                source.setdefault('channel', params.get('channel', 0))
                key = (str(source['asset_id']), int(source['series']), int(source['frame']), int(source['channel']))
                if key not in images:
                    if len(images) >= MAX_ANALYSIS_PLANES:
                        raise ValueError('单次 WB 分析最多读取 32 个原始平面，请按实验系列分批。')
                    images[key] = read_plane(source)
            return analyze_wb(images, params, cancel_event=cancel_event)
        if kind == 'qpcr':
            from .qpcr import analyze_qpcr, read_qpcr_table
            if params.get('asset_id'):
                if self.store.public_asset(params['asset_id'])['kind'] != 'table':
                    raise ValueError('qPCR analysis needs a CSV/TSV/XLSX source table.')
                rows = read_qpcr_table(self.store.resolve(params['asset_id']), sheet=params.get('sheet'))['rows']
            else:
                rows = params.get('rows', [])
            _check_cancel(cancel_event)
            return analyze_qpcr(rows, params, cancel_event=cancel_event)
        from .microscopy import analyze_microscopy
        if params.get('channels'):
            channels = params['channels']
            if not isinstance(channels, list) or len(channels) > 32:
                raise ValueError('Select between 1 and 32 original channels.')
            images = {}
            for channel in channels:
                cid = str(channel.get('id', ''))
                if not cid or cid in images:
                    raise ValueError('Analysis channel IDs must be nonempty and unique.')
                images[cid] = read_plane(channel.get('source') or {})
            if len({array.shape for array in images.values()}) > 1:
                raise ValueError('All analysis channels must have identical source dimensions; register them before comparing.')
            return analyze_microscopy(images, params, cancel_event=cancel_event)
        source = dict(params.get('source') or {})
        source.setdefault('asset_id', params.get('asset_id', ''))
        if not source['asset_id']:
            raise ValueError('Select a source image for microscopy analysis.')
        source.setdefault('series', params.get('series', 0))
        source.setdefault('frame', params.get('frame', params.get('page', 0)))
        source.setdefault('channel', params.get('channel', 0))
        if params.get('method') in {'ihc', 'ihc_advanced'}:
            from .channels import source_spec
            _, spec = source_spec(self.store, source)
            description = self.store.describe_channels(source['asset_id'], series=int(source['series']), frame=int(source['frame']))
            if (spec.get('logical_channels', 1) != 1 or spec.get('samples', spec.get('channels')) not in {3, 4}
                    or sum(channel.get('role') == 'rgb_component' for channel in description['channels']) != 3):
                raise ValueError('IHC requires a true RGB brightfield source image, not pseudocolor fluorescence channels.')
            image = self.store.read_array(source['asset_id'], page=int(source['frame']), series=int(source['series']))
            if image.nbytes > MAX_ANALYSIS_SOURCE_BYTES:
                raise ValueError('IHC 原始图像超过 512 MiB，请使用较小的原始图像。')
            _check_cancel(cancel_event)
            analysis_params = {**params, 'source': source, 'channel_axis': -1}
        else:
            image = read_plane(source)
            analysis_params = {**params, 'source': source, 'channel': 0}
        return analyze_microscopy(image, analysis_params, cancel_event=cancel_event)

    def shutdown(self, *, wait=False):
        with self.lock:
            self.closed = True
            for jid in list(self.jobs):
                self.cancel(jid)
        self.executor.shutdown(wait=wait, cancel_futures=True)
