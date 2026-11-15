"""Owned Windows desktop window and backend lifetime.

The GUI is a WinForms window containing WebView2, never an external browser.
The scientific backend stays in its own process: closing an analysis-heavy
window cannot leave Python's executor threads holding the desktop process open.
No filesystem or process API is exposed to page JavaScript.
"""
from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid
import webbrowser

from .runtime_paths import bundle_root

TITLE = '图研 · Figure Workbench'
RUNTIME_HELP = 'https://developer.microsoft.com/microsoft-edge/webview2/'
SPLASH = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>body{margin:0;background:#f6f8fb;color:#172b43;font:16px 'Microsoft YaHei',sans-serif;
display:grid;place-items:center;min-height:100vh}main{max-width:440px;padding:40px}h1{font-size:30px}
p{line-height:1.8;color:#52657c}.dots{height:4px;border-radius:2px;background:#3271ae;width:84px}</style>
<main><div class="dots"></div><h1>图研 · Figure Workbench</h1><p>正在准备你的工作台…</p>
<p>原图、分析和工程保存在这台电脑上。首次打开可能需要稍等片刻。</p></main></html>'''


def local_navigation_allowed(target: str, origin: str | None, *, booting: bool = False) -> bool:
    """Only the exact owned loopback origin may occupy the application window."""
    if booting and target == 'about:blank':
        return True
    if not origin or not isinstance(target, str) or any(ord(c) < 32 for c in target):
        return False
    if target.startswith('blob:'):
        target = target[5:]
    try:
        candidate, expected = urlsplit(target), urlsplit(origin)
        return (candidate.scheme == expected.scheme == 'http'
                and candidate.hostname == expected.hostname == '127.0.0.1'
                and candidate.port == expected.port
                and candidate.username is None and candidate.password is None)
    except ValueError:
        return False


def external_reference_allowed(target: str) -> bool:
    try:
        value = urlsplit(target)
        return (value.scheme == 'https' and bool(value.hostname)
                and value.username is None and value.password is None
                and not any(ord(c) < 32 for c in target))
    except (TypeError, ValueError):
        return False


def clear_browser_profile(path: Path, *, attempts: int = 1) -> None:
    """Remove an owned WebView profile without following directory symlinks."""
    path = Path(path)
    for attempt in range(attempts):
        try:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                if not path.is_dir():
                    raise OSError('浏览缓存位置不是文件夹。')
                shutil.rmtree(path)
            return
        except OSError as exc:
            if attempt + 1 == attempts:
                raise RuntimeError('软件预览缓存未能清理。请关闭残留的图研窗口后重试；清理完成前不会载入工作区。') from exc
            time.sleep(.15)


def prepare_browser_profile(directory: Path) -> Path:
    """Called only after acquiring the single-desktop lock."""
    cache = Path(directory) / 'cache'
    if cache.is_symlink():
        raise RuntimeError('软件缓存目录是外部链接，未执行清理。请先恢复常规缓存目录。')
    cache.mkdir(parents=True, exist_ok=True)
    clear_browser_profile(cache / 'desktop', attempts=3)
    # A previous crash may have interrupted private-mode cleanup. All of these
    # directories are created by this application and never contain user saves.
    for path in cache.glob('desktop-private-*'):
        clear_browser_profile(path, attempts=3)
    return Path(tempfile.mkdtemp(prefix='desktop-private-', dir=cache))


def initial_window_geometry():
    """Keep restore/minimum sizes inside the primary working area at its DPI."""
    width, height = 1280, 800
    try:
        from ctypes import wintypes
        area = wintypes.RECT()
        user32 = ctypes.windll.user32
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):  # SPI_GETWORKAREA
            dpi = user32.GetDpiForSystem() or 96
            width = max(640, min(1440, int((area.right - area.left) * 96 / dpi) - 32))
            height = max(480, min(960, int((area.bottom - area.top) * 96 / dpi) - 32))
    except (AttributeError, OSError):
        pass
    return width, height, (min(1024, width), min(680, height))


def service_identity(directory: Path, owner: str) -> dict | None:
    """Verify an owned service using both its metadata and live health identity."""
    connection = None
    try:
        with (directory / 'instance.json').open(encoding='utf-8') as stream:
            value = json.loads(stream.read(8192))
        if (not isinstance(value, dict) or value.get('desktop_owner') != owner
                or value.get('application') != 'figure-workbench'):
            return None
        port, instance_id = value.get('port'), value.get('instance_id', '')
        if type(port) is not int or not 1 <= port <= 65535 or not re.fullmatch('[0-9a-f]{32}', instance_id):
            return None
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.4)
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        health = json.loads(response.read(8192))
        if (response.status != 200 or health.get('ok') is not True
                or health.get('application') != 'figure-workbench'
                or health.get('instance_id') != instance_id):
            return None
        return {**value, 'url': f'http://127.0.0.1:{port}'}
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return None
    finally:
        if connection is not None:
            connection.close()


class OwnedBackend:
    """A private stdin pipe is the parent's lifetime signal, not a public API."""

    def __init__(self, directory: Path, port: int, logger: logging.Logger):
        self.directory, self.port, self.logger = directory, port, logger
        self.owner = uuid.uuid4().hex
        self.process: subprocess.Popen | None = None
        self.log_path = directory / 'logs' / f'desktop-backend-{os.getpid()}.log'
        self._stop_lock = threading.Lock()

    def start(self):
        if self.process is not None:
            raise RuntimeError('The owned backend has already been started.')
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, '-B', '-X', 'utf8', '-u', str(bundle_root() / 'run.py'),
                   '--no-browser', '--data-dir', str(self.directory), '--port', str(self.port),
                   '--desktop-owner', self.owner]
        with self.log_path.open('wb') as output:
            self.process = subprocess.Popen(command, cwd=bundle_root(), stdin=subprocess.PIPE,
                                            stdout=output, stderr=subprocess.STDOUT,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def wait_ready(self, cancelled: threading.Event, timeout: float = 45.0) -> str | None:
        if self.process is None:
            raise RuntimeError('The owned backend has not been started.')
        deadline = time.monotonic() + timeout
        while not cancelled.is_set() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f'本地计算服务未能启动或提前退出。详细记录：{self.log_path}\n{self.log_tail()}')
            identity = service_identity(self.directory, self.owner)
            if identity:
                return identity['url']
            cancelled.wait(.08)
        if cancelled.is_set():
            return None
        raise RuntimeError(f'本地计算服务启动超过 {int(timeout)} 秒。详细记录：{self.log_path}\n{self.log_tail()}')

    def log_tail(self):
        try:
            with self.log_path.open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 8000))
                return stream.read().decode('utf-8', 'replace')
        except OSError:
            return '后台尚未写入诊断日志。'

    def stop(self, grace: float = 8.0):
        with self._stop_lock:
            process = self.process
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.logger.warning('Backend did not stop after its lifetime pipe closed; terminating PID %s.', process.pid)
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def focus_existing_window(directory: Path) -> bool:
    """Focus the current GUI owner; never turn a second launch into a browser."""
    if sys.platform != 'win32':
        return False
    try:
        with (directory / 'desktop.json').open(encoding='utf-8') as stream:
            identity = json.loads(stream.read(8192))
        pid = identity.get('pid')
        if identity.get('application') != 'figure-workbench-desktop' or type(pid) is not int or pid <= 0:
            return False
        from ctypes import wintypes
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found = []
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]

        @callback_type
        def visit(handle, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(handle):
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(handle, title, len(title))
                if title.value.startswith(TITLE):
                    user32.ShowWindow(handle, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(handle)
                    found.append(handle)
                    return False
            return True

        user32.EnumWindows(visit, 0)
        return bool(found)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


class CloseCoordinator:
    """Cancel native close until the editor's pending save has settled."""

    SCRIPT = '''Promise.resolve().then(() => {
      if (typeof window.figureWorkbenchPrepareClose !== 'function')
        return {saved:false,busy:false,message:'尚未确认当前修改已保存。'};
      return window.figureWorkbenchPrepareClose();
    }).catch(error => ({saved:false,busy:false,message:String(error?.message || error)}))
      .then(result => window.chrome.webview.postMessage({
        channel:'figure-workbench:close', request:REQUEST_ID, result
      }));'''

    def __init__(self, window, logger: logging.Logger, closed: threading.Event, confirmation=None):
        self.window, self.logger, self.closed = window, logger, closed
        self.confirmation = confirmation or window.create_confirmation_dialog
        self.download_pending = lambda: False
        self.ready = False
        self.allowed = False
        self._pending = threading.Lock()
        self._reply_lock = threading.Lock()
        self._request = None
        self._answer = {}
        self._received = threading.Event()

    def receive(self, payload):
        if not isinstance(payload, dict) or payload.get('channel') != 'figure-workbench:close':
            return False
        with self._reply_lock:
            if not self._request or payload.get('request') != self._request:
                return True
            value = payload.get('result')
            self._answer = value if isinstance(value, dict) else {}
            self._received.set()
        return True

    def closing(self):
        if self.allowed or not self.ready:
            return None
        if self._pending.acquire(blocking=False):
            threading.Thread(target=self._prepare, name='desktop-save-before-close', daemon=True).start()
        return False

    def _confirm(self, message):
        try:
            return bool(self.confirmation('退出图研', message))
        except Exception:
            self.logger.exception('Exit confirmation failed; keeping the editor open.')
            return False

    def _prepare(self):
        try:
            with self._reply_lock:
                self._request = uuid.uuid4().hex
                self._answer = {}
                self._received.clear()
                script = self.SCRIPT.replace('REQUEST_ID', json.dumps(self._request))
            # run_js calls ExecuteScriptAsync directly, without eval. The
            # application's strict script-src CSP remains unchanged.
            self.window.run_js(script)
            if not self._received.wait(15):
                answer = {'saved': False, 'message': '保存尚未完成，建议返回工作台重试保存。'}
            else:
                with self._reply_lock:
                    answer = dict(self._answer)
            if self.closed.is_set():
                return
            saved, busy = answer.get('saved') is True, answer.get('busy') is True
            downloading = self.download_pending()
            busy = busy or downloading
            if not saved or busy:
                reason = str(answer.get('message') or '')[:1000]
                if answer.get('busy') is True:
                    reason += '\n仍有任务在运行，退出会取消这些任务。'
                if downloading:
                    reason += '\n仍有文件正在保存，退出会中止保存。'
                if not saved:
                    reason += '\n部分修改尚未确认保存，退出可能丢失这些修改。'
                if not self._confirm(reason.strip() + '\n\n仍要退出吗？'):
                    return
            self.allowed = True
            self.window.destroy()
        except Exception:
            self.logger.exception('Preparing the editor for close failed; keeping the window open.')
            if not self.closed.is_set() and self._confirm(
                    '未能确认保存状态。建议取消退出并保存工程副本。\n\n仍要退出吗？'):
                self.allowed = True
                self.window.destroy()
        finally:
            with self._reply_lock:
                self._request = None
            self._pending.release()


class DownloadCoordinator:
    """Report real WebView2 download outcomes, including native dialog cancel."""

    def __init__(self, notify, logger):
        self.notify, self.logger = notify, logger
        self.active = {}
        self.cancelled = {}

    def start(self, url, operation):
        now = time.monotonic()
        self.cancelled = {key: when for key, when in self.cancelled.items() if now - when < 30}
        if url in self.cancelled:
            self.cancelled.pop(url)
            self.notify({'url': url, 'status': 'canceled'})
            return False
        if url in self.active:
            raise RuntimeError('A download with the same identity is already active.')

        def changed(sender, args):
            self.updated(url)

        self.active[url] = (operation, changed)
        operation.StateChanged += changed
        return True

    def updated(self, url):
        active = self.active.get(url)
        if not active:
            return
        operation = active[0]
        status = str(operation.State)
        if status == 'Completed':
            self.finish(url, 'completed')
        elif status == 'Interrupted':
            reason = str(operation.InterruptReason)
            self.finish(url, 'canceled' if reason == 'UserCanceled' else 'error',
                        '' if reason == 'UserCanceled' else '文件未能保存，请检查保存位置和可用空间后重试。')

    def finish(self, url, status, message=''):
        active = self.active.pop(url, None)
        if not active:
            return
        operation, callback = active
        try:
            operation.StateChanged -= callback
        except Exception:
            self.logger.exception('Could not detach a completed native download callback.')
        self.logger.info('Native download finished: %s.', status)
        self.notify({'url': url, 'status': status, **({'message': message} if message else {})})

    def cancel(self, url):
        active = self.active.get(url)
        if active:
            try:
                active[0].Cancel()
                self.updated(url)
            except Exception:
                self.logger.exception('Canceling the native download failed.')
                self.finish(url, 'error', '客户端未能确认取消保存，请检查目标文件。')
        else:
            # A cancel message may reach the UI just before DownloadStarting.
            # Keep only a short, bounded set of these one-use download URLs.
            if len(self.cancelled) >= 128:
                self.cancelled.pop(next(iter(self.cancelled)))
            self.cancelled[url] = time.monotonic()


def run_desktop(directory: Path, port: int, logger: logging.Logger, *, lock_factory, write_identity,
                notify) -> int:
    if sys.platform != 'win32':
        raise RuntimeError('当前桌面客户端支持 Windows 10/11。此平台请使用 --browser 或 --no-browser。')
    lock = lock_factory(directory, filename='desktop.lock')
    backend = None
    browser_profile = None
    owner = uuid.uuid4().hex
    closed, guarded = threading.Event(), threading.Event()
    state = {'url': None, 'startup_error': None}
    try:
        if not lock.acquire():
            # A double click while the first window is initializing should not
            # create another writer or silently open a browser tab.
            for _ in range(15):
                if focus_existing_window(directory):
                    return 0
                time.sleep(.1)
            notify(TITLE, '图研已经在运行或正在启动。请从任务栏切换到图研窗口。')
            return 0
        browser_profile = prepare_browser_profile(directory)
        write_identity(directory / 'desktop.json',
                       {'application': 'figure-workbench-desktop', 'pid': os.getpid(), 'owner': owner})
        try:
            import webview
        except ImportError as error:
            raise RuntimeError('桌面运行组件不完整，请重新完整解压客户端包；从源码运行请安装 requirements-desktop.txt。') from error
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FigureWorkbench.Desktop')
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
        webview.settings.update(ALLOW_DOWNLOADS=True, ALLOW_FILE_URLS=False,
                                OPEN_EXTERNAL_LINKS_IN_BROWSER=False, OPEN_DEVTOOLS_IN_DEBUG=False,
                                IGNORE_SSL_ERRORS=False, REMOTE_DEBUGGING_PORT=None)
        test_port = os.environ.get('FIGURE_WORKBENCH_DESKTOP_TEST_PORT')
        if test_port:
            if (os.environ.get('FIGURE_WORKBENCH_DESKTOP_TEST') != '1'
                    or not test_port.isdecimal() or not 1 <= int(test_port) <= 65535):
                raise ValueError('Desktop CDP is available only in explicitly enabled Windows acceptance tests.')
            # Edge binds the debugging endpoint to loopback. Production starts
            # leave it disabled; the acceptance runner sets both test variables.
            webview.settings['REMOTE_DEBUGGING_PORT'] = int(test_port)
        fixed_runtime = bundle_root().parent / 'webview2'
        if fixed_runtime.is_dir():
            if not (fixed_runtime / 'msedgewebview2.exe').is_file():
                raise RuntimeError('随包的 WebView2 目录不完整，请重新完整解压客户端。')
            webview.settings['WEBVIEW2_RUNTIME_PATH'] = str(fixed_runtime)
        width, height, minimum = initial_window_geometry()
        window = webview.create_window(TITLE, html=SPLASH, width=width, height=height,
                                       min_size=minimum, maximized=True, background_color='#f6f8fb',
                                       text_select=True, confirm_close=False)
        def confirmation(title, message):
            # The library's generic confirmation is ownerless. Attach our
            # dialogs to the app on its UI thread so they cannot hide behind it
            # or leave the editor interactive while an exit decision is open.
            from System import Boolean, Func
            from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon, DialogResult
            form = window.native

            def show():
                return MessageBox.Show(form, message, title, MessageBoxButtons.OKCancel,
                                       MessageBoxIcon.Question) == DialogResult.OK

            return bool(form.Invoke(Func[Boolean](show))) if form.InvokeRequired else show()

        close = CloseCoordinator(window, logger, closed, confirmation=confirmation)

        def trace_native_download(event, url, **details):
            # Only the explicitly enabled acceptance run records synthetic
            # destinations. Ordinary user save paths never enter app logs.
            if not test_port:
                return
            record = {'event': event, 'download_id': hashlib.sha256(url.encode('utf-8')).hexdigest(), **details}
            try:
                with (directory / 'logs' / 'native-download-events.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=True) + '\n')
            except OSError:
                logger.exception('Writing native acceptance download evidence failed.')

        def report_download(result):
            trace_native_download('finished', result['url'], status=result['status'])
            def dispatch():
                if closed.is_set():
                    return
                try:
                    window.run_js("window.dispatchEvent(new CustomEvent('figure-workbench:download', {detail:"
                                  + json.dumps(result, ensure_ascii=True) + '}));')
                except Exception:
                    logger.exception('Returning the native download result to the editor failed.')
            # ExecuteScriptAsync's completion is marshalled to the UI thread;
            # invoking run_js from a download UI callback would deadlock it.
            threading.Thread(target=dispatch, name='desktop-download-result', daemon=True).start()

        downloads = DownloadCoordinator(report_download, logger)
        close.download_pending = lambda: bool(downloads.active)
        window.events.closing += close.closing
        window.events.closed += closed.set

        def check_renderer(renderer):
            if renderer != 'edgechromium':
                state['startup_error'] = RuntimeError(
                    '此电脑缺少可用的 Microsoft Edge WebView2 运行时，不能打开桌面工作台。\n'
                    '请安装微软 WebView2 运行时后重试；不会改用旧版 IE 或自动打开浏览器。\n' + RUNTIME_HELP)
                return False
            return None

        window.events.initialized += check_renderer

        def request_reference(url):
            if external_reference_allowed(url) and not closed.is_set():
                if confirmation('打开外部参考资料',
                                                     '将在系统浏览器打开以下参考页面，工作台继续保留。\n\n' + url[:1800]):
                    webbrowser.open(url)

        def attach_native_guards():
            try:
                form = window.native
                engine, control = form.browser, form.browser.webview

                def navigating(sender, args):
                    if not local_navigation_allowed(str(args.Uri), state['url'], booting=not close.ready):
                        args.Cancel = True

                def install_core_guards(sender, args):
                    if guarded.is_set():
                        return
                    if not args.IsSuccess:
                        state['startup_error'] = RuntimeError('WebView2 初始化失败：' + str(args.InitializationException))
                        guarded.set()
                        return
                    core = control.CoreWebView2
                    # Replace the library's broad URL opening handler with the
                    # desktop's constrained, explicitly confirmed link handler.
                    core.NewWindowRequested -= engine.on_new_window_request
                    core.DownloadStarting -= engine.on_download_starting

                    def new_window(_, event):
                        event.Handled = True
                        if event.IsUserInitiated and external_reference_allowed(str(event.Uri)):
                            threading.Thread(target=request_reference, args=(str(event.Uri),), daemon=True).start()

                    def download(sender, event):
                        logger.info('WebView2 DownloadStarting reached the native host.')
                        uri = str(event.DownloadOperation.Uri)
                        if not local_navigation_allowed(uri, state['url']):
                            logger.warning('Blocked a download outside the owned application origin.')
                            event.Cancel = True
                            return
                        event.Handled = True  # our owned dialog replaces Edge's download UI
                        trace_native_download('started', uri, suggested_path=str(event.ResultFilePath))
                        deferral = None
                        try:
                            if not downloads.start(uri, event.DownloadOperation):
                                event.Cancel = True
                                return
                            # Microsoft requires modal prompts to run after
                            # DownloadStarting returns, with an explicit
                            # deferral, to avoid WebView2 callback reentrancy.
                            deferral = event.GetDeferral()

                            def choose_destination():
                                try:
                                    if closed.is_set() or uri not in downloads.active:
                                        event.Cancel = True
                                    else:
                                        logger.info('Opening the native Save As dialog.')
                                        engine.on_download_starting(sender, event)
                                        logger.info('Native Save As choice returned: %s.',
                                                    'canceled' if event.Cancel else 'destination selected')
                                    trace_native_download('choice', uri,
                                                          status='canceled' if event.Cancel else 'selected',
                                                          **({} if event.Cancel else {'path': str(event.ResultFilePath)}))
                                    if event.Cancel:
                                        downloads.finish(uri, 'canceled')
                                    else:
                                        downloads.updated(uri)
                                except Exception:
                                    logger.exception('Native save dialog failed.')
                                    event.Cancel = True
                                    downloads.finish(uri, 'error', '未能打开保存对话框，请重试。')
                                finally:
                                    try:
                                        deferral.Complete()
                                    except Exception:
                                        logger.exception('Completing the native download choice failed.')
                                        downloads.finish(uri, 'error', '客户端未能确认保存结果，请检查目标文件。')

                            from System import Action
                            form.BeginInvoke(Action(choose_destination))
                        except Exception:
                            logger.exception('Native save dialog/download failed.')
                            event.Cancel = True
                            downloads.finish(uri, 'error', '未能打开保存对话框，请重试。')
                            if deferral is not None:
                                try:
                                    deferral.Complete()
                                except Exception:
                                    logger.exception('Releasing the failed native download deferral failed.')

                    def message(sender, event):
                        try:
                            payload = json.loads(event.WebMessageAsJson)
                            if isinstance(payload, dict) and payload.get('channel') == 'figure-workbench:close':
                                if local_navigation_allowed(str(event.Source), state['url']):
                                    close.receive(payload)
                                return
                            if isinstance(payload, dict) and payload.get('channel') == 'figure-workbench:download-cancel':
                                url = payload.get('url')
                                if (isinstance(url, str) and len(url) <= 4096
                                        and local_navigation_allowed(str(event.Source), state['url'])
                                        and local_navigation_allowed(url, state['url'])):
                                    downloads.cancel(url)
                                return
                        except (ValueError, TypeError):
                            pass
                        engine.on_script_notify(sender, event)

                    core.NewWindowRequested += new_window
                    core.DownloadStarting += download
                    control.WebMessageReceived -= engine.on_script_notify
                    control.WebMessageReceived += message
                    # Hold delegate callbacks for the complete window lifetime.
                    state['native_callbacks'] = (navigating, ready, new_window, download, message)
                    logger.info('Native WebView2 navigation, download and close handlers installed.')
                    guarded.set()

                def ready(sender, args):
                    try:
                        install_core_guards(sender, args)
                    except Exception as error:
                        logger.exception('Installing WebView2 event guards failed.')
                        state['startup_error'] = error
                        guarded.set()

                control.NavigationStarting += navigating
                control.CoreWebView2InitializationCompleted += ready
                state['native_callbacks'] = (navigating, ready)
                if control.CoreWebView2 is not None:
                    ready(control, SimpleNamespace(IsSuccess=True))
            except Exception as error:
                logger.exception('Installing native navigation guards failed.')
                state['startup_error'] = error
                guarded.set()

        window.events.before_show += attach_native_guards
        backend = OwnedBackend(directory, port, logger)
        backend.start()

        def boot_and_monitor():
            try:
                url = backend.wait_ready(closed)
                if url is None or closed.is_set():
                    return
                if not guarded.wait(20):
                    raise RuntimeError('桌面渲染组件未能完成初始化，请检查 WebView2 运行时。')
                if state['startup_error']:
                    raise state['startup_error']
                state['url'] = url
                window.load_url(url + '/?desktop=1')
                close.ready = True
                while not closed.wait(.5):
                    if backend.process.poll() is not None:
                        raise RuntimeError(f'本地计算服务意外退出。已保存的工程仍在本机。\n详细记录：{backend.log_path}')
            except Exception as error:
                logger.exception('Desktop initialization or backend monitoring failed.')
                state['startup_error'] = error
                if not closed.is_set():
                    close.allowed = True
                    window.destroy()

        icon = bundle_root() / 'resources' / 'app.ico'
        webview.start(boot_and_monitor, gui='edgechromium', debug=False, http_server=False,
                      private_mode=True, storage_path=str(browser_profile),
                      icon=str(icon) if icon.is_file() else None,
                      localization={'global.ok': '确定', 'global.cancel': '取消',
                                    'windows.fileFilter.allFiles': '所有文件'})
        if state['startup_error']:
            raise state['startup_error']
        return 0
    finally:
        closed.set()
        if backend is not None:
            backend.stop()
        try:
            path = directory / 'desktop.json'
            if json.loads(path.read_text('utf-8')).get('owner') == owner:
                path.unlink()
        except (OSError, ValueError):
            pass
        try:
            if browser_profile is not None:
                # pywebview's private mode also attempts removal, but catches
                # failures internally. Verify the owned directory ourselves.
                clear_browser_profile(browser_profile, attempts=20)
        finally:
            lock.close()

