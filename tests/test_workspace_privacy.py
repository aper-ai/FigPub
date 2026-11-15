"""Explicit recovery and verified deletion of application-owned working copies."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from io import BytesIO
import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest
import httpx

from workbench import desktop, server
from workbench.server import create_app
from workbench.storage import ProjectStore


def image_bytes():
    out = BytesIO()
    Image.new('RGB', (12, 8), '#34897b').save(out, format='PNG')
    return out.getvalue()


def seed(root):
    store = ProjectStore(root)
    store.add_files([('private-test.png', image_bytes())])
    return store.get_project()


def test_production_starts_blank_and_requires_explicit_recovery(tmp_path):
    previous = seed(tmp_path)
    original = (tmp_path / 'current.json').read_bytes()
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/project').json()['assets'] == []
        status = client.get('/api/workspace/status').json()
        assert status['has_recovery'] and not status['cleanup_pending']
        assert status['recovery']['asset_count'] == 1
        assert 'private-test.png' not in json.dumps(status)
        assert (tmp_path / 'current.json').read_bytes() == original
        blocked = client.post('/api/assets', files={'files': ('another.png', image_bytes())})
        assert blocked.status_code == 409
        assert blocked.json()['code'] == 'workspace_recovery_pending'
        assert client.get(f"/api/assets/{previous['assets'][0]['id']}/preview").status_code == 409
        restored = client.post('/api/workspace/restore').json()
        assert restored == previous
        assert not client.get('/api/workspace/status').json()['has_recovery']
        preview = client.get(f"/api/assets/{previous['assets'][0]['id']}/preview")
        assert preview.status_code == 200 and preview.headers['cache-control'] == 'no-store'
        raw = client.get(f"/api/assets/{previous['assets'][0]['id']}/original")
        assert raw.content == image_bytes() and raw.headers['cache-control'] == 'no-store'


def test_failed_recovery_preserves_autosave_and_recovery_choice(tmp_path):
    previous = seed(tmp_path)
    current = (tmp_path / 'current.json').read_bytes()
    next((tmp_path / 'originals').iterdir()).write_bytes(b'changed')
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/workspace/restore')
        assert response.status_code == 400
        assert client.get('/api/workspace/status').json()['has_recovery']
        assert client.get('/api/project').json()['assets'] == []
        assert (tmp_path / 'current.json').read_bytes() == current
        assert previous['assets']


def test_new_and_clear_have_distinct_meanings_and_do_not_delete_external_files(tmp_path):
    root = tmp_path / 'application'
    previous = seed(root)
    external = tmp_path / 'research-original.png'
    external.write_bytes(image_bytes())
    saved = tmp_path / 'saved.figwork'
    saved.write_bytes(ProjectStore(root).export_archive())
    saved_bytes = saved.read_bytes()
    app = create_app(root)
    with TestClient(app) as client:
        new = client.post('/api/project/new', json={'title': 'Blank project'}).json()
        assert new['assets'] == [] and new['id'] != previous['id']
        assert client.get('/api/workspace/status').json()['stored_asset_count'] == 1
        assert list((root / 'originals').iterdir())
        assert client.post('/api/workspace/clear', json={}).status_code == 400
        assert list((root / 'originals').iterdir())
        for cache in (app.state.store.array_cache, app.state.store.preview_cache,
                      app.state.store.table_cache, app.state.store.localization_cache):
            cache.put('sensitive', b'private')
        (root / '.import-leftover.png').write_bytes(image_bytes())
        (root / 'tmpabcdefgh.png').write_bytes(image_bytes())
        (root / '.current.backup.json.abcd').write_bytes(b'old metadata')
        cleared = client.post('/api/workspace/clear', json={'confirm': True})
        assert cleared.status_code == 200
        body = cleared.json()
        assert body['cleared'] and body['project']['assets'] == []
        assert body['requires_restart'] and body['browser_cache'] == 'close_client_or_clear_site_cache'
        assert cleared.headers['clear-site-data'] == '"cache"'
        assert list((root / 'originals').iterdir()) == []
        assert not (root / 'assets.json').exists()
        assert not (root / 'current.backup.json').exists()
        assert not (root / '.import-leftover.png').exists()
        assert not (root / 'tmpabcdefgh.png').exists()
        assert not (root / '.current.backup.json.abcd').exists()
        assert not (root / '.workspace-clear-pending.json').exists()
        assert all(cache.bytes == 0 for cache in (app.state.store.array_cache, app.state.store.preview_cache,
                                                 app.state.store.table_cache, app.state.store.localization_cache))
    with TestClient(create_app(root)) as client:
        assert client.get('/api/project').json()['assets'] == []
        assert not client.get('/api/workspace/status').json()['has_recovery']
    assert external.read_bytes() == image_bytes()
    assert saved.read_bytes() == saved_bytes


def test_partial_delete_is_reported_and_cannot_restore_old_images(tmp_path, monkeypatch):
    previous = seed(tmp_path)
    asset = next((tmp_path / 'originals').iterdir())
    unlink = Path.unlink

    def fail_original(path, *args, **kwargs):
        if path == asset:
            raise PermissionError('test: original is in use')
        return unlink(path, *args, **kwargs)

    with TestClient(create_app(tmp_path), raise_server_exceptions=False) as client:
        monkeypatch.setattr(Path, 'unlink', fail_original)
        failed = client.post('/api/workspace/clear', json={'confirm': True})
        assert failed.status_code == 403
        assert client.get('/api/workspace/status').json()['cleanup_pending']
        assert client.get('/api/project').json()['assets'] == []
        assert client.post('/api/workspace/restore').json()['code'] == 'workspace_cleanup_pending'
        assert client.post('/api/project/new', json={}).status_code == 409
        assert client.get(f"/api/assets/{previous['assets'][0]['id']}/preview").status_code == 409
    monkeypatch.setattr(Path, 'unlink', unlink)
    with TestClient(create_app(tmp_path)) as restarted:
        assert restarted.get('/api/workspace/status').json()['cleanup_pending']
        assert restarted.get('/api/project').json()['assets'] == []
        result = restarted.post('/api/workspace/clear', json={'confirm': True})
        assert result.status_code == 200 and result.json()['cleared']
        assert not restarted.get('/api/workspace/status').json()['cleanup_pending']
        assert list((tmp_path / 'originals').iterdir()) == []


def test_cancelled_worker_must_finish_before_cleanup(tmp_path):
    started, release = threading.Event(), threading.Event()

    def runner(kind, params):
        started.set()
        assert release.wait(5)
        return {'value': 1}

    app = create_app(tmp_path, runner=runner)
    with TestClient(app) as client:
        imported = client.post('/api/assets', files={'files': ('test.png', image_bytes())}).json()
        asset_id = imported['assets'][0]['id']
        job = app.state.jobs.submit('microscopy', {'asset_id': asset_id, 'method': 'threshold'})
        try:
            assert started.wait(2)
            app.state.jobs.cancel(job['id'])
            blocked = client.post('/api/workspace/clear', json={'confirm': True})
            assert blocked.status_code == 409 and blocked.json()['code'] == 'workspace_busy'
            assert list((tmp_path / 'originals').iterdir())
        finally:
            release.set()
        app.state.jobs.jobs[job['id']]['_future'].result(timeout=3)
        assert client.post('/api/workspace/clear', json={'confirm': True}).json()['cleared']
        assert app.state.jobs.jobs == {}


def test_cleanup_drains_import_before_deleting_its_copies(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    app = create_app(tmp_path)
    original = app.state.store._store_bytes

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.store, '_store_bytes', delayed)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
        importing = executor.submit(client.post, '/api/assets', files={'files': ('test.png', image_bytes())})
        assert entered.wait(2)
        clearing = executor.submit(client.post, '/api/workspace/clear', json={'confirm': True})
        try:
            time.sleep(.05)
            assert not clearing.done()
        finally:
            release.set()
        assert importing.result(timeout=3).status_code == 200
        assert clearing.result(timeout=3).json()['cleared']
        assert client.get('/api/project').json()['assets'] == []
        assert list((tmp_path / 'originals').iterdir()) == []


def test_cancelled_cleanup_waiter_does_not_block_later_http_requests(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    app = create_app(tmp_path)
    app.state.store.add_files([('test.png', image_bytes())])
    asset_id = app.state.store.get_project()['assets'][0]['id']
    preview = app.state.store.preview

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return preview(*args, **kwargs)

    monkeypatch.setattr(app.state.store, 'preview', delayed)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                assert (await client.get('/api/workspace/status')).status_code == 200
                gate = app.middleware_stack
                while not isinstance(gate, server.WorkspaceRequestGate):
                    gate = gate.app
                reading = asyncio.create_task(client.get(f'/api/assets/{asset_id}/preview'))
                assert await asyncio.to_thread(entered.wait, 2)
                clearing = asyncio.create_task(client.post('/api/workspace/clear', json={'confirm': True}))
                try:
                    deadline = asyncio.get_running_loop().time() + 2
                    while not gate.waiting and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(.005)
                    assert gate.waiting == 1
                    clearing.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await clearing
                    assert gate.waiting == 0
                    # The preview is still running. This proves an abandoned
                    # writer cannot keep ordinary API traffic blocked.
                    status = await asyncio.wait_for(client.get('/api/workspace/status'), 1)
                    assert status.status_code == 200
                    assert not status.json()['cleanup_pending']
                finally:
                    release.set()
                    if not clearing.done():
                        clearing.cancel()
                assert (await reading).status_code == 200
                assert (await client.post('/api/workspace/clear', json={'confirm': True})).json()['cleared']

    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['waiting', 'deleting'])
def test_requests_arriving_during_clear_cannot_recreate_old_sources(tmp_path, monkeypatch, phase):
    app = create_app(tmp_path)
    app.state.store.add_files([('before-clear.png', image_bytes())])
    old_id = app.state.store.get_project()['assets'][0]['id']
    delete_entered, allow_delete = threading.Event(), threading.Event()
    clear_workspace = app.state.store.clear_workspace

    def paused_clear():
        delete_entered.set()
        assert allow_delete.wait(5)
        return clear_workspace()

    if phase == 'deleting':
        monkeypatch.setattr(app.state.store, 'clear_workspace', paused_clear)

    async def scenario():
        hold_entered, release_hold = asyncio.Event(), asyncio.Event()

        @app.get('/api/privacy-test-hold')
        async def hold():
            hold_entered.set()
            await release_hold.wait()
            return {'released': True}

        # Insert before the production static catch-all. The middleware and
        # clear/upload endpoints remain the real application implementations.
        app.router.routes.insert(0, app.router.routes.pop())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                holding = None
                if phase == 'waiting':
                    holding = asyncio.create_task(client.get('/api/privacy-test-hold'))
                    await asyncio.wait_for(hold_entered.wait(), 2)
                clearing = asyncio.create_task(client.post('/api/workspace/clear', json={'confirm': True}))
                try:
                    if phase == 'waiting':
                        gate = app.middleware_stack
                        while not isinstance(gate, server.WorkspaceRequestGate):
                            gate = gate.app
                        deadline = asyncio.get_running_loop().time() + 2
                        while not gate.waiting and asyncio.get_running_loop().time() < deadline:
                            await asyncio.sleep(.005)
                        assert gate.waiting == 1
                    else:
                        assert await asyncio.to_thread(delete_entered.wait, 2)
                    upload = await asyncio.wait_for(client.post('/api/assets', files={
                        'files': ('late-old-source.png', image_bytes(), 'image/png')}), 1)
                    old_preview = await asyncio.wait_for(client.get(f'/api/assets/{old_id}/preview'), 1)
                    assert upload.status_code == 409 and upload.json()['code'] == 'workspace_changed'
                    assert old_preview.status_code == 409
                    assert not clearing.done()
                finally:
                    release_hold.set()
                    allow_delete.set()
                if holding is not None:
                    assert (await holding).status_code == 200
                result = await asyncio.wait_for(clearing, 3)
                assert result.status_code == 200 and result.json()['cleared']
                assert (await client.get('/api/workspace/status')).json()['stored_asset_count'] == 0
                assert (await client.get('/api/project')).json()['assets'] == []
                assert (await client.get(f'/api/assets/{old_id}/preview')).status_code == 404
                assert list((tmp_path / 'originals').iterdir()) == []

    asyncio.run(scenario())


def test_production_has_no_demo_route_and_optional_benchmarks_may_be_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'bundle_root', lambda: tmp_path / 'application-without-development-files')
    with TestClient(create_app(tmp_path / 'data')) as client:
        assert client.post('/api/demo').status_code == 404
        assert client.get('/api/benchmarks').json()['status'] == 'not_run'
        assert client.get('/api/project').json()['assets'] == []


def test_private_desktop_profiles_remove_legacy_caches_and_preserve_other_files(tmp_path):
    cache = tmp_path / 'cache'
    legacy = cache / 'desktop'
    interrupted = cache / 'desktop-private-old'
    legacy.mkdir(parents=True)
    interrupted.mkdir()
    (legacy / 'preview').write_bytes(b'private')
    (interrupted / 'preview').write_bytes(b'private')
    other = cache / 'other-user-file'
    other.write_bytes(b'keep')
    profile = desktop.prepare_browser_profile(tmp_path)
    assert profile.is_dir() and profile.parent == cache
    assert profile.name.startswith('desktop-private-')
    assert not legacy.exists() and not interrupted.exists()
    assert other.read_bytes() == b'keep'
    desktop.clear_browser_profile(profile)
    assert not profile.exists()


def test_desktop_cache_cleanup_failure_is_not_silenced(tmp_path, monkeypatch):
    legacy = tmp_path / 'cache' / 'desktop'
    legacy.mkdir(parents=True)
    monkeypatch.setattr(desktop.shutil, 'rmtree', lambda path: (_ for _ in ()).throw(PermissionError('in use')))
    with pytest.raises(RuntimeError, match='未能清理'):
        desktop.prepare_browser_profile(tmp_path)
    assert legacy.is_dir()
    assert list(legacy.parent.glob('desktop-private-*')) == []
