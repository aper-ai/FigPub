"""Cancellation must release real work, not merely change a UI status string."""
import threading

import numpy as np
import pytest

from workbench.analysis import analyze_wb
from workbench.jobs import JobManager
from workbench.qpcr import analyze_qpcr
from workbench.storage import ProjectStore


@pytest.mark.parametrize('roi_count', [5001, 10000])
def test_oversized_wb_submission_is_rejected_before_queueing_or_decoding(roi_count, tmp_path, monkeypatch):
    manager = JobManager(ProjectStore(tmp_path))

    def unexpected_queue(*args, **kwargs):
        pytest.fail('Oversized WB work reached the executor before ROI count validation.')

    def unexpected_read(*args, **kwargs):
        pytest.fail('Oversized WB work decoded an original image before ROI count validation.')

    monkeypatch.setattr(manager.executor, 'submit', unexpected_queue)
    monkeypatch.setattr(manager.store, 'read_plane', unexpected_read)
    try:
        with pytest.raises(ValueError, match='5,000 ROI'):
            manager.submit('wb', {'rois': [{} for _ in range(roi_count)]})
        assert not manager.jobs
        assert not manager.store.get_project()['measurements']
    finally:
        manager.shutdown()


def test_wb_submission_accepts_the_5000_roi_boundary(tmp_path):
    # This checks the admission boundary; scientific measurement is covered by
    # separate known-value tests, not by this controlled queue runner.
    manager = JobManager(ProjectStore(tmp_path), runner=lambda kind, params: {
        'kind': kind, 'roi_count': len(params['rois']), 'rows': []})
    try:
        submitted = manager.submit('wb', {'rois': [{} for _ in range(5000)]})
        manager.jobs[submitted['id']]['_future'].result(timeout=2)
        completed = manager.get(submitted['id'])
        assert completed['status'] == 'done' and completed['committed']
        assert completed['result']['roi_count'] == 5000
    finally:
        manager.shutdown()


def test_cancelling_running_work_does_not_bypass_queue_limit(tmp_path):
    started, release = threading.Event(), threading.Event()

    def runner(kind, params):
        started.set()
        assert release.wait(3)
        return {'kind': kind, 'rows': []}

    store = ProjectStore(tmp_path)
    manager = JobManager(store, max_workers=1, max_pending=1, runner=runner)
    try:
        job = manager.submit('microscopy', {})
        assert started.wait(1)
        cancelled = manager.cancel(job['id'])
        assert cancelled['status'] == 'cancelled' and cancelled['cleanup_pending']
        with pytest.raises(ValueError, match='仍在释放计算资源'):
            manager.submit('microscopy', {})
        release.set()
        manager.jobs[job['id']]['_future'].result(timeout=2)
        assert not manager.get(job['id'])['cleanup_pending']
        assert not store.get_project()['measurements']
        next_job = manager.submit('microscopy', {})
        manager.jobs[next_job['id']]['_future'].result(timeout=2)
        assert manager.get(next_job['id'])['committed']
    finally:
        release.set()
        manager.shutdown()


def test_cancelling_queued_work_removes_future_without_running_it(tmp_path):
    started, release, seen = threading.Event(), threading.Event(), []

    def runner(kind, params):
        seen.append(params['name'])
        started.set()
        assert release.wait(3)
        return {'kind': kind, 'rows': []}

    manager = JobManager(ProjectStore(tmp_path), max_workers=1, max_pending=2, runner=runner)
    try:
        first = manager.submit('microscopy', {'name': 'running'})
        assert started.wait(1)
        queued = manager.submit('microscopy', {'name': 'cancelled-before-start'})
        cancelled = manager.cancel(queued['id'])
        assert cancelled['status'] == 'cancelled' and not cancelled['cleanup_pending']
        assert manager.jobs[queued['id']]['_future'].cancelled()
        replacement = manager.submit('microscopy', {'name': 'replacement'})
        release.set()
        for job in (first, replacement):
            manager.jobs[job['id']]['_future'].result(timeout=2)
        assert seen == ['running', 'replacement']
    finally:
        release.set()
        manager.shutdown()


def test_cancel_between_plane_reads_stops_decoding_further_channels(tmp_path, monkeypatch):
    manager = JobManager(ProjectStore(tmp_path))
    cancelled, reads = threading.Event(), []

    def read(source):
        reads.append(source)
        cancelled.set()
        return np.zeros((4, 4), np.uint16)

    monkeypatch.setattr(manager.store, 'read_plane', read)
    try:
        with pytest.raises(ValueError, match='已取消'):
            manager._analyze('microscopy', {'method': 'channel_intensity', 'channels': [
                {'id': f'c{index}', 'source': {'asset_id': 'a'*64, 'channel': index}}
                for index in range(3)]}, cancel_event=cancelled)
        assert len(reads) == 1
    finally:
        manager.shutdown()


@pytest.mark.parametrize('kind', ['wb', 'microscopy'])
def test_analysis_caps_total_decoded_sources_before_loading_more(kind, tmp_path, monkeypatch):
    from workbench import jobs
    manager = JobManager(ProjectStore(tmp_path))
    reads = []

    def read(source):
        reads.append(source)
        return np.zeros((4, 4), np.uint16)

    monkeypatch.setattr(manager.store, 'read_plane', read)
    monkeypatch.setattr(jobs, 'MAX_ANALYSIS_SOURCE_BYTES', 40)
    sources = [{'asset_id': 'a'*64, 'channel': index} for index in range(3)]
    params = {'rois': [{'source': source} for source in sources]} if kind == 'wb' else {
        'method': 'channel_intensity', 'channels': [{'id': f'c{i}', 'source': source} for i, source in enumerate(sources)]}
    try:
        with pytest.raises(ValueError, match='累计超过 512 MiB'):
            manager._analyze(kind, params)
        assert len(reads) == 2
    finally:
        manager.shutdown()


def test_cancelled_wb_and_qpcr_do_not_compute_remaining_records():
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ValueError, match='已取消'):
        analyze_qpcr([], {}, cancel_event=cancelled)
    with pytest.raises(ValueError, match='已取消'):
        analyze_wb({'a': np.zeros((4, 4), np.uint16)}, {'rois': [{'asset_id': 'a'}]}, cancel_event=cancelled)
