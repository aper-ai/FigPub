"""Local entry point for source, embedded-Python portable, and frozen builds."""
from __future__ import annotations

import argparse
import errno
import http.client
import importlib
import importlib.metadata
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
import webbrowser

from workbench import __version__


class InstanceLock:
    """One OS-held byte/file lock per working directory; no stale PID locking."""
    def __init__(self, directory: Path, filename: str = 'instance.lock'):
        directory.mkdir(parents=True, exist_ok=True)
        if filename not in {'instance.lock', 'desktop.lock'}:
            raise ValueError('Unsupported application lock name.')
        self.stream = (directory / filename).open('a+b')
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)

    def acquire(self):
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise

    def close(self):
        self.stream.close()


def reserve_port(preferred: int):
    for port in [preferred, 0]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(('127.0.0.1', port))
            listener.listen(128)
            return listener
        except OSError:
            listener.close()
    raise RuntimeError('Cannot listen on a local port.')


def _native_message(title: str, message: str, *, error=False) -> bool:
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, (0x10 if error else 0x40) | 0x10000)
        return True
    except (AttributeError, OSError):
        return False


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)

    def _print_message(self, message, file=None):
        stream = file or sys.stdout or sys.stderr
        if message and stream is not None:
            stream.write(message)
        elif message:
            _native_message('Figure Workbench', message)


class _LogStream:
    """A non-console Python executable still needs usable stdout/stderr."""
    encoding = 'utf-8'
    errors = 'backslashreplace'
    closed = False

    def __init__(self, logger, level):
        self.logger, self.level = logger, level
        self.pending = threading.local()

    def write(self, value):
        value = str(value)
        if getattr(self.pending, 'logging', False):
            # A full/unwritable log disk can make logging report to stderr.
            # Do not recursively feed its internal error into this same log.
            return len(value)
        pending = getattr(self.pending, 'text', '') + value
        while '\n' in pending or len(pending) > 8192:
            if '\n' in pending:
                line, pending = pending.split('\n', 1)
            else:
                line, pending = pending[:8192], pending[8192:]
            if line.strip():
                self._emit(line.rstrip())
        self.pending.text = pending
        return len(value)

    def flush(self):
        value = getattr(self.pending, 'text', '')
        self.pending.text = ''
        if value.strip():
            self._emit(value)

    def _emit(self, value):
        self.pending.logging = True
        try:
            self.logger.log(self.level, value)
        finally:
            self.pending.logging = False

    def isatty(self):
        return False


def _configure_logging(directory: Path | None):
    from workbench.runtime_paths import default_data_dir
    candidates = ([directory / 'logs'] if directory is not None else [])
    candidates += [default_data_dir(allow_override=False) / 'logs',
                   Path(tempfile.gettempdir()) / 'FigureWorkbench' / 'logs']
    last_error = None
    for folder in dict.fromkeys(candidates):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f'launcher-{os.getpid()}.log'
            handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=1, encoding='utf-8')
            break
        except OSError as error:
            last_error = error
    else:
        raise OSError('Cannot create a startup log in a user-writable folder.') from last_error
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger = logging.getLogger('figure_workbench.launcher')
    for previous in logger.handlers[:]:
        logger.removeHandler(previous)
        previous.close()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    # Each launch has its own log: a short second instance cannot interfere
    # with the active process's Windows file handle during log rotation.
    history = []
    for item in folder.glob('launcher-*.log*'):
        try:
            history.append((item.stat().st_mtime, item))
        except OSError:
            pass
    for _, old in sorted(history, reverse=True)[20:]:
        if old != path:
            try:
                old.unlink()
            except OSError:
                pass
    original_stderr = sys.stderr
    if sys.stdout is None:
        sys.stdout = _LogStream(logger, logging.INFO)
    if sys.stderr is None:
        sys.stderr = _LogStream(logger, logging.ERROR)
    server_logger = logging.getLogger('uvicorn')
    server_logger.handlers = [handler]
    server_logger.propagate = False
    if original_stderr is not None and not isinstance(original_stderr, _LogStream):
        server_logger.addHandler(logging.StreamHandler(original_stderr))
    logging.getLogger('uvicorn.error').handlers = []
    logging.getLogger('uvicorn.error').propagate = True
    return logger, path


def _write_instance(path: Path, identity: dict):
    descriptor, temporary = tempfile.mkstemp(prefix='.instance-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(identity, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _wait_for_desktop_pipe(stream, ready: threading.Event | None = None):
    """Wait for the owner's EOF without blocking Windows DLL stdin probes.

    A pending synchronous ReadFile on a Windows pipe can block metadata calls
    made by another thread (including scientific DLL initialization). A daemon
    thread around stdin.read() does not remove that kernel-level deadlock.
    Nothing writes data on this private lifetime pipe: EOF or an explicit byte
    both mean stop. Windows only peeks; it never leaves ReadFile outstanding.
    """
    descriptor = stream.fileno()
    if os.name != 'nt':
        if ready is not None:
            ready.set()
        os.read(descriptor, 1)
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                     ctypes.POINTER(wintypes.DWORD)]
    peek.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(descriptor)
    available = wintypes.DWORD()
    pause = threading.Event()
    while True:
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in {109, 233}:  # ERROR_BROKEN_PIPE / ERROR_PIPE_NOT_CONNECTED
                return
            raise ctypes.WinError(error)
        if ready is not None:
            ready.set()
        if available.value:
            return
        pause.wait(.05)


def _existing_url(directory: Path, timeout: float = 8.0) -> str | None:
    """Only reopen a live service matching this working folder's launch ID."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = None
        try:
            with (directory / 'instance.json').open('r', encoding='utf-8') as stream:
                identity = json.loads(stream.read(8192))
            port = identity.get('port')
            instance_id = identity.get('instance_id', '')
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ValueError('Invalid instance port.')
            if identity.get('application') != 'figure-workbench' or not re.fullmatch(r'[a-f0-9]{32}', instance_id):
                raise ValueError('Unrecognized instance metadata.')
            # HTTPConnection does not consult HTTP_PROXY or follow redirects.
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.35)
            connection.request('GET', '/api/health')
            response = connection.getresponse()
            health = json.loads(response.read(8192))
            if (response.status == 200 and health.get('ok') is True and
                    health.get('application') == 'figure-workbench' and health.get('instance_id') == instance_id):
                return f'http://127.0.0.1:{port}'
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            pass
        finally:
            if connection is not None:
                connection.close()
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    return None


def _open_browser(url, logger, log_path, show_dialog):
    try:
        if webbrowser.open(url):
            return
        logger.warning('No default browser accepted the local URL: %s', url)
    except Exception:
        logger.exception('Opening the default browser failed.')
    message = f'本地工作台已经启动。请在浏览器中打开：\n{url}\n\n日志：{log_path}'
    print(message, flush=True)
    if show_dialog:
        _native_message('Figure Workbench — 打开浏览器', message)


def _check_runtime():
    packages = {'numpy': 'numpy', 'scipy': 'scipy', 'scikit-image': 'skimage', 'tifffile': 'tifffile',
                'imagecodecs': 'imagecodecs', 'Pillow': 'PIL', 'PyMuPDF': 'fitz', 'python-pptx': 'pptx',
                'openpyxl': 'openpyxl', 'fonttools': 'fontTools', 'svglib': 'svglib', 'reportlab': 'reportlab',
                'fastapi': 'fastapi', 'uvicorn': 'uvicorn'}
    print(f'Figure Workbench {__version__} — checking runtime dependencies.')
    print('Python:', sys.version.split()[0])
    for package, module_name in packages.items():
        module = importlib.import_module(module_name)
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = getattr(module, '__version__', 'imported; package metadata not bundled')
        print(package + ':', version)
    print('Runtime dependencies available.')


def main(argv=None) -> int:
    # Embedded Python can run from a read-only application directory.
    sys.dont_write_bytecode = True
    show_dialog = (sys.stdout is None or sys.stderr is None or bool(getattr(sys, 'frozen', False)))
    logger = log_path = directory = lock = listener = application = None
    instance_id = None
    try:
        parser = _ArgumentParser(description='Figure Workbench — local scientific figure editor')
        parser.add_argument('--port', type=int, default=8765, help='Preferred loopback port; falls back to an available port')
        parser.add_argument('--data-dir', type=Path, help='Writable working folder; defaults to the current user profile')
        presentation = parser.add_mutually_exclusive_group()
        presentation.add_argument('--desktop', action='store_true', help='Open the Windows desktop client (Windows default)')
        presentation.add_argument('--browser', action='store_true', help='Explicitly open the workbench in an external browser')
        presentation.add_argument('--no-browser', action='store_true', help='Run only the local backend for development')
        parser.add_argument('--desktop-owner', help=argparse.SUPPRESS)
        parser.add_argument('--check', action='store_true', help='Check packaged resources/dependencies and exit')
        args = parser.parse_args(argv)
        if not 1 <= args.port <= 65535:
            raise ValueError('--port must be 1–65535')
        if args.desktop_owner and (not args.no_browser or not re.fullmatch('[a-f0-9]{32}', args.desktop_owner)):
            raise ValueError('A desktop-owned backend requires --no-browser and a valid owner identity.')
        desktop_mode = args.desktop or (sys.platform == 'win32' and not args.browser and not args.no_browser)
        show_dialog = show_dialog or desktop_mode
        from workbench.runtime_paths import default_data_dir, check_resources
        directory = args.data_dir.expanduser().resolve() if args.data_dir else default_data_dir()
        logger, log_path = _configure_logging(directory)
        logger.info('Starting Figure Workbench. Python=%s, frozen=%s, data=%s',
                    sys.version.split()[0], bool(getattr(sys, 'frozen', False)), directory)
        os.environ['FIGURE_WORKBENCH_DATA'] = str(directory)
        os.environ.setdefault('MPLCONFIGDIR', str(directory / 'cache' / 'matplotlib'))
        check_resources()
        if args.check:
            _check_runtime()
            logger.info('Resource/dependency import check completed.')
            if show_dialog:
                _native_message('Figure Workbench', f'运行依赖与应用资源检查通过。\n日志：{log_path}')
            return 0
        if desktop_mode:
            from workbench.desktop import run_desktop
            return run_desktop(directory, args.port, logger, lock_factory=InstanceLock,
                               write_identity=_write_instance, notify=_native_message)
        lock = InstanceLock(directory)
        if not lock.acquire():
            url = _existing_url(directory)
            if url is None:
                raise RuntimeError('此工作目录已被另一个实例锁定，但尚未确认可用服务。请稍后再试，或先关闭旧版本的启动窗口。')
            print('This working folder is already open:', url, flush=True)
            logger.info('Reusing the existing instance: %s', url)
            if not args.no_browser:
                _open_browser(url, logger, log_path, show_dialog)
            return 0
        # Import and initialize the project only after obtaining its writer lock.
        import uvicorn
        from workbench.server import create_app
        listener = reserve_port(args.port)
        port = listener.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        instance_id = uuid.uuid4().hex
        application = create_app(directory, instance_id=instance_id)
        server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=port,
                                               log_level='warning', log_config=None, access_log=False))
        _write_instance(directory / 'instance.json', {'application': 'figure-workbench', 'port': port,
                                                     'pid': os.getpid(), 'instance_id': instance_id,
                                                     'desktop_owner': args.desktop_owner})

        if args.desktop_owner:
            if sys.stdin is None:
                raise RuntimeError('The desktop lifetime pipe is unavailable.')

            def stop_with_desktop():
                try:
                    # EOF means the owning GUI closed or crashed. There is no
                    # unauthenticated shutdown route exposed by the HTTP app.
                    _wait_for_desktop_pipe(sys.stdin)
                except (OSError, ValueError):
                    pass
                logger.info('Desktop lifetime pipe closed; stopping the owned backend.')
                application.state.jobs.shutdown()
                server.should_exit = True

            threading.Thread(target=stop_with_desktop, name='desktop-lifetime', daemon=True).start()

        def open_when_ready():
            for _ in range(150):
                if server.started:
                    _open_browser(url, logger, log_path, show_dialog)
                    return
                if server.should_exit:
                    return
                time.sleep(.1)
            logger.warning('Browser launch skipped: server startup did not finish within 15 seconds.')

        if not args.no_browser:
            threading.Thread(target=open_when_ready, name='open-browser', daemon=True).start()
        print('\nFigure Workbench / 图研', flush=True)
        print(f'Open: {url}\nWorking folder: {directory}\nStartup log: {log_path}', flush=True)
        if not args.desktop_owner:
            print('Keep this window open. Press Ctrl+C here to stop.\n', flush=True)
        logger.info('Serving only on %s', url)
        server.run(sockets=[listener])
        if not server.started:
            raise RuntimeError('本地服务未能完成启动。请查看启动日志。')
        return 0
    except KeyboardInterrupt:
        return 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code in {None, 0}:
            return 0
        if logger is None:
            try:
                logger, log_path = _configure_logging(directory)
            except Exception:
                pass
        if logger is not None:
            logger.exception('Figure Workbench could not start or stopped unexpectedly.')
        message = f'Figure Workbench 无法启动。\n{type(error).__name__}: {error}\n\n'
        message += f'详细日志：{log_path}' if log_path else '无法创建日志文件，请检查用户目录的写入权限。'
        if sys.stderr is not None:
            print(message, file=sys.stderr, flush=True)
        if show_dialog:
            _native_message('Figure Workbench — 启动失败', message, error=True)
        return 1
    finally:
        if application is not None:
            application.state.jobs.shutdown()
        if listener is not None:
            listener.close()
        if instance_id and directory is not None:
            try:
                path = directory / 'instance.json'
                identity = json.loads(path.read_text('utf-8'))
                if identity.get('instance_id') == instance_id:
                    path.unlink()
            except (OSError, ValueError):
                pass
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
