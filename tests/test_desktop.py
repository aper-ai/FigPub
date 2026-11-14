"""Desktop lifetime and close handshake regressions; native UI has a separate Windows gate."""
import json
import logging
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from workbench.desktop import CloseCoordinator, DownloadCoordinator, OwnedBackend, local_navigation_allowed, service_identity


def test_owner_pipe_monitor_does_not_block_first_scientific_dll_import(tmp_path):
    """Windows CRT/Fortran stdin inspection must run while the owner is alive."""
    root = Path(__file__).resolve().parents[1]
    ready = tmp_path / 'science-import-ready'
    log = tmp_path / 'pipe-import.log'
    script = r'''
import faulthandler, os, sys, threading
from pathlib import Path
from run import _wait_for_desktop_pipe
faulthandler.dump_traceback_later(10, file=sys.stderr)
monitor_ready, owner_closed = threading.Event(), threading.Event()
def monitor():
    _wait_for_desktop_pipe(sys.stdin, monitor_ready)
    owner_closed.set()
thread = threading.Thread(target=monitor, daemon=True)
thread.start()
assert monitor_ready.wait(3)
assert not owner_closed.is_set()
# create_app imports NumPy but first /api/health imports SciPy's scientific DLLs.
# Keep this process fresh: importing scipy before the monitor hides the defect.
from scipy import ndimage, linalg
assert linalg.det([[2., 0.], [0., 3.]]) == 6.
assert ndimage.label([[0, 1], [0, 1]])[1] == 1
os.fstat(sys.stdin.fileno())
Path(sys.argv[1]).write_text('ready', encoding='utf-8')
faulthandler.cancel_dump_traceback_later()
assert owner_closed.wait(10)
thread.join(2)
assert not thread.is_alive()
'''
    with log.open('wb') as output:
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', '-c', script, str(ready)],
                                 cwd=root, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.025)
        assert ready.exists(), log.read_text('utf-8', errors='replace')
        assert child.poll() is None, 'The backend lifetime ended before its owner closed the pipe.'
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    assert child.returncode == 0, log.read_text('utf-8', errors='replace')


def test_owned_backend_exits_when_the_gui_lifetime_pipe_closes(tmp_path):
    backend = OwnedBackend(tmp_path, 8765, logging.getLogger('desktop-test'))
    try:
        backend.start()
        url = backend.wait_ready(threading.Event(), timeout=20)
        assert url
        identity = service_identity(tmp_path, backend.owner)
        assert identity and identity['pid'] == backend.process.pid
        assert service_identity(tmp_path, 'f' * 32) is None
        with urllib.request.urlopen(url + '/api/project', timeout=3) as response:
            project = json.load(response)
        project['title'] = 'desktop-owned-preserved-work'
        request = urllib.request.Request(url + '/api/project', data=json.dumps(project).encode(),
                                         headers={'Content-Type': 'application/json'}, method='PUT')
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.load(response)['title'] == project['title']
    finally:
        backend.stop()
    assert backend.process.returncode == 0
    assert not (tmp_path / 'instance.json').exists()
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', identity['port'])) != 0
    from workbench.storage import ProjectStore
    assert ProjectStore(tmp_path).get_project()['title'] == 'desktop-owned-preserved-work'


def test_second_owned_backend_does_not_claim_or_stop_an_existing_writer(tmp_path):
    first = OwnedBackend(tmp_path, 8765, logging.getLogger('desktop-test'))
    second = OwnedBackend(tmp_path, 8765, logging.getLogger('desktop-test'))
    try:
        first.start()
        assert first.wait_ready(threading.Event(), timeout=20)
        second.start()
        with pytest.raises(RuntimeError, match='提前退出'):
            second.wait_ready(threading.Event(), timeout=10)
        second.stop()
        assert first.process.poll() is None
        assert service_identity(tmp_path, first.owner)
        assert service_identity(tmp_path, second.owner) is None
    finally:
        second.stop()
        first.stop()


@pytest.mark.parametrize(('target', 'allowed'), [
    ('http://127.0.0.1:1234/', True),
    ('http://127.0.0.1:1234/api/export?format=pdf', True),
    ('blob:http://127.0.0.1:1234/abcdef', True),
    ('http://127.0.0.1:1235/', False),
    ('https://127.0.0.1:1234/', False),
    ('http://localhost:1234/', False),
    ('http://127.0.0.1.evil.test:1234/', False),
    ('http://user@127.0.0.1:1234/', False),
    ('file:///C:/Users/example/private.txt', False),
    ('javascript:alert(1)', False),
    ('data:text/html,hello', False),
    ('https://example.org', False),
    ('http://127.0.0.1:bad/', False),
    ('about:blank', False),
])
def test_desktop_navigation_stays_at_owned_origin(target, allowed):
    assert local_navigation_allowed(target, 'http://127.0.0.1:1234') is allowed
    assert local_navigation_allowed('about:blank', None, booting=True)


class FakeWindow:
    def __init__(self, answer, confirmation=False):
        self.answer = answer
        self.confirmation = confirmation
        self.destroyed = threading.Event()
        self.prompted = threading.Event()
        self.requested = threading.Event()
        self.messages = []

    def run_js(self, script):
        assert 'eval(' not in script and 'new Function' not in script
        request = re.search(r'request:"([0-9a-f]+)"', script).group(1)
        # A stale page reply must not race a later close attempt into success.
        self.coordinator.receive({'channel': 'figure-workbench:close', 'request': 'bad',
                                  'result': {'saved': True, 'busy': False}})
        self.coordinator.receive({'channel': 'figure-workbench:close', 'request': request,
                                  'result': self.answer})
        self.requested.set()

    def create_confirmation_dialog(self, title, message):
        self.messages.append(message)
        self.prompted.set()
        return self.confirmation

    def destroy(self):
        self.destroyed.set()


def coordinator(window):
    result = CloseCoordinator(window, logging.getLogger('close-test'), threading.Event())
    result.ready = True
    window.coordinator = result
    return result


def test_close_waits_for_saved_editor_reply_without_confirmation():
    window = FakeWindow({'saved': True, 'busy': False})
    close = coordinator(window)
    assert close.closing() is False
    assert window.destroyed.wait(2)
    assert not window.prompted.is_set()
    assert close.closing() is None


def test_failed_save_keeps_window_open_when_exit_is_cancelled():
    window = FakeWindow({'saved': False, 'busy': False, 'message': '磁盘已满'}, confirmation=False)
    close = coordinator(window)
    assert close.closing() is False
    assert window.prompted.wait(2)
    assert not window.destroyed.is_set()
    assert '磁盘已满' in window.messages[0]
    assert close.allowed is False


def test_active_analysis_needs_explicit_exit_confirmation():
    window = FakeWindow({'saved': True, 'busy': True}, confirmation=True)
    close = coordinator(window)
    assert close.closing() is False
    assert window.destroyed.wait(2)
    assert '取消这些任务' in window.messages[0]


def test_initial_splash_can_close_without_editor_handshake():
    window = FakeWindow({})
    close = coordinator(window)
    close.ready = False
    assert close.closing() is None
    assert not window.requested.is_set()


def test_native_download_in_progress_prevents_silent_close_even_when_editor_is_saved():
    window = FakeWindow({'saved': True, 'busy': False}, confirmation=False)
    close = coordinator(window)
    close.download_pending = lambda: True
    assert close.closing() is False
    assert window.prompted.wait(2)
    assert not window.destroyed.is_set()
    assert '文件正在保存' in window.messages[0]


class FakeDownloadEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def __isub__(self, callback):
        self.handlers.remove(callback)
        return self

    def fire(self):
        for callback in self.handlers[:]:
            callback(None, None)


class FakeDownload:
    def __init__(self):
        self.StateChanged = FakeDownloadEvent()
        self.State = 'InProgress'
        self.InterruptReason = 'None'

    def Cancel(self):
        self.State, self.InterruptReason = 'Interrupted', 'UserCanceled'
        self.StateChanged.fire()


def test_native_download_reports_only_terminal_outcomes_and_detaches_callbacks():
    events, operation = [], FakeDownload()
    downloads = DownloadCoordinator(events.append, logging.getLogger('native-download-test'))
    url = 'blob:http://127.0.0.1:1234/unique-request'
    assert downloads.start(url, operation)
    operation.StateChanged.fire()
    assert events == []
    operation.State = 'Completed'
    operation.StateChanged.fire()
    assert events == [{'url': url, 'status': 'completed'}]
    assert not downloads.active and not operation.StateChanged.handlers
    downloads.updated(url)
    assert len(events) == 1


@pytest.mark.parametrize(('reason', 'expected'), [('UserCanceled', 'canceled'), ('FileNoSpace', 'error')])
def test_native_download_cancellation_and_disk_failure_never_report_success(reason, expected):
    events, operation = [], FakeDownload()
    downloads = DownloadCoordinator(events.append, logging.getLogger('native-download-test'))
    url = 'blob:http://127.0.0.1:1234/unique-request'
    downloads.start(url, operation)
    operation.State, operation.InterruptReason = 'Interrupted', reason
    operation.StateChanged.fire()
    assert len(events) == 1 and events[0]['status'] == expected
    assert not operation.StateChanged.handlers


def test_native_cancel_message_handles_both_active_and_not_yet_started_operations():
    events = []
    downloads = DownloadCoordinator(events.append, logging.getLogger('native-download-test'))
    active, early = 'blob:http://127.0.0.1:1234/active', 'blob:http://127.0.0.1:1234/early'
    operation = FakeDownload()
    downloads.start(active, operation)
    downloads.cancel(active)
    downloads.cancel(early)
    assert not downloads.start(early, FakeDownload())
    assert events == [{'url': active, 'status': 'canceled'}, {'url': early, 'status': 'canceled'}]
    assert not downloads.active and not downloads.cancelled
