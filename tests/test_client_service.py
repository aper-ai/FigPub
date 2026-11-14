from fastapi.testclient import TestClient

from workbench import __version__
from workbench.server import create_app


def test_health_uses_the_actual_package_version(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/health').json()['version'] == __version__


def test_windows_file_associations_cannot_break_es_modules(tmp_path, monkeypatch):
    # Windows registry/file associations can override Python's MIME guess.
    monkeypatch.setattr('starlette.responses.guess_type', lambda _path: ('text/plain', None))
    with TestClient(create_app(tmp_path)) as client:
        for path, mime in [('/app.js', 'text/javascript'), ('/modules/api.js', 'text/javascript'),
                           ('/styles.css', 'text/css'), ('/', 'text/html')]:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['content-type'].startswith(mime)
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert response.headers['cache-control'] == 'no-cache'


def test_unwritable_workspace_reports_an_actionable_error(tmp_path, monkeypatch):
    app = create_app(tmp_path)

    def fail_write(_project):
        raise PermissionError('locked by another process')

    monkeypatch.setattr(app.state.store, 'update_project', fail_write)
    with TestClient(app) as client:
        response = client.put('/api/project', json=client.get('/api/project').json())
        assert response.status_code == 403
        assert response.json()['code'] == 'file_permission'
        assert '读写权限' in response.json()['detail']
