"""Exercise the packaged WinForms/WebView2 client on a real Windows desktop.

The application EXE creates the window, WebView2 and owned backend. Playwright
attaches to that existing WebView2 over a test-only loopback CDP endpoint; it
never starts a substitute browser. Native save/confirmation dialogs are driven
through their Win32 controls, and both the native window and its rendered
content are captured. Unsupported or noninteractive environments fail the gate.

Run with the ordinary development dependencies on Windows:
  python tests/desktop_windows_smoke.py --app-dir DIST --output RESULTS
"""
from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def wait_until(check, description, timeout=30, pump=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        if pump:
            pump(50)
        else:
            time.sleep(.05)
    raise TimeoutError(description)


def read_identity(path):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None


def read_native_download_events(data):
    path = data / 'logs' / 'native-download-events.jsonl'
    try:
        content = path.read_text('utf-8')
    except FileNotFoundError:
        return []
    # A UI callback may be appending the final line while the test reads it.
    return [json.loads(line) for line in content.splitlines(keepends=True) if line.endswith('\n')]


def local_json(port, path, diagnostics=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        if diagnostics is not None:
            diagnostics['attempts'] = diagnostics.get('attempts', 0) + 1
        connection.request('GET', path)
        response = connection.getresponse()
        if diagnostics is not None:
            diagnostics['last_http_status'] = response.status
        if response.status != 200:
            return None
        return json.loads(response.read(32 * 1024 * 1024))
    except (OSError, ValueError, http.client.HTTPException) as error:
        if diagnostics is not None:
            diagnostics['last_error'] = f'{type(error).__name__}: {error}'
        return None
    finally:
        connection.close()


def available_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def port_closed(port):
    with socket.socket() as connection:
        connection.settimeout(.2)
        return connection.connect_ex(('127.0.0.1', port)) != 0


class KeyboardInput(ctypes.Structure):
    _fields_ = [('virtual_key', ctypes.c_uint16), ('scan_code', ctypes.c_uint16),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class MouseInput(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_int32), ('dy', ctypes.c_int32), ('mouse_data', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class HardwareInput(ctypes.Structure):
    _fields_ = [('message', ctypes.c_uint32), ('low', ctypes.c_uint16), ('high', ctypes.c_uint16)]


class InputPayload(ctypes.Union):
    _fields_ = [('keyboard', KeyboardInput), ('mouse', MouseInput), ('hardware', HardwareInput)]


class NativeInput(ctypes.Structure):
    _anonymous_ = ('payload',)
    _fields_ = [('type', ctypes.c_uint32), ('payload', InputPayload)]


class GuiThreadInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('active', wintypes.HWND), ('focus', wintypes.HWND), ('capture', wintypes.HWND),
                ('menu_owner', wintypes.HWND), ('move_size', wintypes.HWND), ('caret', wintypes.HWND),
                ('caret_rect', wintypes.RECT)]


class NativeWindows:
    """Operate only controls owned by the launched application process family."""

    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('Native desktop acceptance requires Windows, not a simulated GUI.')
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        definitions = {
            'EnumWindows': ([self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            'EnumChildWindows': ([wintypes.HWND, self.callback_type, wintypes.LPARAM], wintypes.BOOL),
            'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
            'IsWindowEnabled': ([wintypes.HWND], wintypes.BOOL),
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'IsIconic': ([wintypes.HWND], wintypes.BOOL),
            'ShowWindow': ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            'SetForegroundWindow': ([wintypes.HWND], wintypes.BOOL),
            'GetForegroundWindow': ([], wintypes.HWND),
            'GetGUIThreadInfo': ([wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)], wintypes.BOOL),
            'GetWindowRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            'WindowFromPoint': ([wintypes.POINT], wintypes.HWND),
            'IsChild': ([wintypes.HWND, wintypes.HWND], wintypes.BOOL),
            'SetCursorPos': ([ctypes.c_int, ctypes.c_int], wintypes.BOOL),
            'SendInput': ([wintypes.UINT, ctypes.POINTER(NativeInput), ctypes.c_int], wintypes.UINT),
            'RedrawWindow': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.HANDLE,
                              wintypes.UINT], wintypes.BOOL),
            'GetDlgCtrlID': ([wintypes.HWND], ctypes.c_int),
            'SendMessageTimeoutW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], wintypes.LPARAM),
            'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (args, result) in definitions.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = args, result
        assert ctypes.sizeof(NativeInput) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        assert ctypes.sizeof(GuiThreadInfo) == (72 if ctypes.sizeof(ctypes.c_void_p) == 8 else 48)
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass

    def describe(self, handle):
        title, kind = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(256)
        process = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        self.user32.GetWindowTextW(handle, title, len(title))
        self.user32.GetClassNameW(handle, kind, len(kind))
        return {'handle': int(handle), 'pid': process.value, 'title': title.value,
                'class': kind.value, 'visible': bool(self.user32.IsWindowVisible(handle)),
                'enabled': bool(self.user32.IsWindowEnabled(handle)),
                'control_id': self.user32.GetDlgCtrlID(handle)}

    def control_message(self, handle, message, wparam=0, lparam=0):
        result = ctypes.c_size_t()
        # System messages below WM_USER are marshalled across process boundaries
        # by Windows. A bounded send also exposes an unresponsive native dialog.
        ctypes.set_last_error(0)
        if not self.user32.SendMessageTimeoutW(handle, message, wparam, lparam, 0x0002, 2000,
                                               ctypes.byref(result)):
            error = ctypes.get_last_error()
            raise OSError(error, f'Native control message {message:#x} failed or timed out.')
        return result.value

    def control_text(self, handle):
        # GetWindowTextW cannot retrieve another process's Edit contents. Send
        # WM_GETTEXT directly, as documented for cross-process control access.
        value = ctypes.create_unicode_buffer(32768)
        length = self.control_message(handle, 0x000D, len(value), ctypes.addressof(value))
        if length >= len(value) - 1:
            raise AssertionError('Native control text exceeded the diagnostic limit.')
        return value.value

    def stable_control_text(self, handle, *, expected=None):
        observed, changed = None, time.monotonic()

        def settled():
            nonlocal observed, changed
            actual = self.control_text(handle)
            if actual != observed:
                observed, changed = actual, time.monotonic()
            return actual if (actual and (expected is None or actual == expected)
                              and time.monotonic() - changed >= .25) else None

        return wait_until(settled, 'Native filename field did not finish initialization or retain the entered path.',
                          timeout=5)

    def send_input(self, events):
        values = (NativeInput * len(events))(*events)
        inserted = self.user32.SendInput(len(values), values, ctypes.sizeof(NativeInput))
        if inserted != len(values):
            raise OSError(ctypes.get_last_error(), 'Windows did not accept every native input event.')

    @staticmethod
    def keyboard_input(key=0, scan=0, flags=0):
        event = NativeInput(type=1)  # INPUT_KEYBOARD
        event.keyboard = KeyboardInput(key, scan, flags, 0, 0)
        return event

    def press_key(self, key):
        self.send_input([self.keyboard_input(key), self.keyboard_input(key, flags=0x0002)])

    def type_text(self, text):
        # Shell's modern file dialog owns more state than its visible Edit
        # text. Use real input (including Ctrl+A), so it receives keyboard,
        # edit-change and later focus-loss notifications in their normal order.
        events = [self.keyboard_input(0x11), self.keyboard_input(0x41),
                  self.keyboard_input(0x41, flags=0x0002), self.keyboard_input(0x11, flags=0x0002)]
        encoded = text.encode('utf-16-le')
        for offset in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[offset:offset + 2], 'little')
            events.extend((self.keyboard_input(scan=unit, flags=0x0004),
                           self.keyboard_input(scan=unit, flags=0x0004 | 0x0002)))
        self.send_input(events)

    def focused_control(self, dialog):
        thread = self.user32.GetWindowThreadProcessId(dialog, None)
        info = GuiThreadInfo(size=ctypes.sizeof(GuiThreadInfo))
        if not self.user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.focus

    def click_control(self, handle):
        if not self.user32.IsWindowVisible(handle) or not self.user32.IsWindowEnabled(handle):
            raise AssertionError('Native input target is not visible and enabled.')
        rectangle = wintypes.RECT()
        if not self.user32.GetWindowRect(handle, ctypes.byref(rectangle)):
            raise ctypes.WinError(ctypes.get_last_error())
        if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
            raise AssertionError('Native input target has no clickable area.')
        x, y = (rectangle.left + rectangle.right) // 2, (rectangle.top + rectangle.bottom) // 2
        hit = self.user32.WindowFromPoint(wintypes.POINT(x, y))
        if hit != handle and not self.user32.IsChild(handle, hit):
            raise AssertionError('Another native window covers the intended input target.')
        if not self.user32.SetCursorPos(x, y):
            raise ctypes.WinError(ctypes.get_last_error())
        down, up = NativeInput(type=0), NativeInput(type=0)  # INPUT_MOUSE
        down.mouse.flags, up.mouse.flags = 0x0002, 0x0004  # LEFTDOWN / LEFTUP
        self.send_input([down, up])
        return {'left': rectangle.left, 'top': rectangle.top, 'right': rectangle.right,
                'bottom': rectangle.bottom, 'click_x': x, 'click_y': y}

    def windows(self, parent=None):
        result = []

        @self.callback_type
        def visit(handle, _):
            result.append(self.describe(handle))
            return True

        if parent is None:
            self.user32.EnumWindows(visit, 0)
        else:
            self.user32.EnumChildWindows(parent, visit, 0)
        return result

    def app_window(self, pid):
        matches = [item for item in self.windows() if item['pid'] == pid and item['visible']
                   and item['title'].startswith('图研 · Figure Workbench')
                   and item['class'].startswith('WindowsForms10.')]
        if len(matches) > 1:
            raise AssertionError('More than one native application window was created.')
        return matches[0] if matches else None

    def running(self, pid):
        handle = self.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            # Restricted Chromium children may deny OpenProcess; permission
            # denial is never evidence that a process has exited.
            return any(item['pid'] == pid for item in self.processes())
        try:
            result = wintypes.DWORD()
            if not self.kernel32.GetExitCodeProcess(handle, ctypes.byref(result)):
                raise ctypes.WinError(ctypes.get_last_error())
            return result.value == 259  # STILL_ACTIVE
        finally:
            self.kernel32.CloseHandle(handle)

    def processes(self):
        class ProcessEntry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                        ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                        ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                        ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

        self.kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ('Process32FirstW', 'Process32NextW'):
            function = getattr(self.kernel32, name)
            function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
            function.restype = wintypes.BOOL
        handle = self.kernel32.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        result = []
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            valid = self.kernel32.Process32FirstW(handle, ctypes.byref(entry))
            while valid:
                result.append({'pid': entry.th32ProcessID, 'parent_pid': entry.th32ParentProcessID,
                               'executable': entry.szExeFile})
                valid = self.kernel32.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(handle)
        return result

    def family(self, root_pid):
        processes, owned = self.processes(), {root_pid}
        while True:
            previous = len(owned)
            owned.update(item['pid'] for item in processes if item['parent_pid'] in owned)
            if len(owned) == previous:
                return [item for item in processes if item['pid'] in owned]

    def close(self, handle):
        if not self.user32.PostMessageW(handle, 0x0010, 0, 0):  # WM_CLOSE
            raise ctypes.WinError(ctypes.get_last_error())

    def screenshot(self, handle, path):
        from PIL import ImageGrab, ImageStat
        rectangle = wintypes.RECT()
        if not self.user32.GetWindowRect(handle, ctypes.byref(rectangle)):
            raise ctypes.WinError(ctypes.get_last_error())
        if rectangle.right - rectangle.left < 400 or rectangle.bottom - rectangle.top < 300:
            raise AssertionError('Native application window is too small or minimized.')
        picture = ImageGrab.grab(bbox=(rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
                                 all_screens=True)
        picture.save(path)
        if max(ImageStat.Stat(picture.convert('RGB')).stddev) < 2:
            raise AssertionError('Native screenshot is blank; interactive desktop acceptance is unavailable.')

    def dialog_controls(self, pids, *, file_dialog):
        for dialog in self.windows():
            if (dialog['pid'] not in pids or not dialog['visible'] or not dialog['enabled']
                    or dialog['class'] != '#32770'):
                continue
            children = [item for item in self.windows(dialog['handle']) if item['visible'] and item['enabled']]
            if file_dialog:
                filename = [item for item in children if item['class'] == 'Edit' and item['control_id'] == 1001]
                buttons = [item for item in children if item['class'] == 'Button' and item['control_id'] in (1, 2)]
                if len(filename) == 1 and any(item['control_id'] == 1 for item in buttons):
                    return dialog, filename[0], buttons
            elif dialog['title'] in ('退出图研', '打开外部参考资料'):
                buttons = [item for item in children if item['class'] == 'Button' and item['control_id'] in (1, 2)]
                if any(item['control_id'] == 2 for item in buttons):
                    return dialog, None, buttons
        return None

    def handle_dialog(self, root_pid, destination, *, file_dialog=True, cancel=False, capture=None):
        selected = wait_until(lambda: self.dialog_controls(
            {item['pid'] for item in self.family(root_pid)}, file_dialog=file_dialog),
            'Expected native save/confirmation dialog did not appear.', timeout=35)
        dialog, filename, buttons = selected
        evidence = {key: dialog[key] for key in ('pid', 'title', 'class')}
        self.last_dialog = evidence
        self.user32.SetForegroundWindow(dialog['handle'])
        wait_until(lambda: self.user32.GetForegroundWindow() == dialog['handle'],
                   'The native dialog did not become the active window.', timeout=5)
        if file_dialog:
            evidence['suggested_filename'] = self.stable_control_text(filename['handle'])
            evidence['filename_control'] = filename
            if not cancel:
                destination = Path(destination).resolve()
                if not destination.is_absolute() or not destination.parent.is_dir():
                    raise AssertionError('Native save requires an existing absolute destination directory.')
                evidence['requested_destination'] = str(destination)
                evidence['filename_target'] = self.click_control(filename['handle'])
                wait_until(lambda: self.focused_control(dialog['handle']) == filename['handle'],
                           'The real File name control did not receive keyboard focus.', timeout=5)
                evidence['focused_control'] = self.describe(self.focused_control(dialog['handle']))
                evidence['input_method'] = 'SendInput: mouse focus, Ctrl+A, Unicode typing, Tab, mouse Save'
                self.type_text(str(destination))
                evidence['entered_filename'] = self.stable_control_text(filename['handle'], expected=str(destination))
                self.press_key(0x09)  # Tab commits the filename through the dialog's normal focus path.
                wait_until(lambda: self.focused_control(dialog['handle']) != filename['handle'],
                           'The native filename edit did not commit focus before Save.', timeout=5)
                evidence['committed_filename'] = self.stable_control_text(filename['handle'], expected=str(destination))
        # Capture only after the actual filename control is initialized and
        # its absolute destination has been read back from the owning process.
        self.user32.RedrawWindow(dialog['handle'], None, None, 0x0001 | 0x0080 | 0x0100)
        if capture:
            # Small confirmation boxes are captured through the desktop image;
            # the full-size window screenshot has its own nonblank assertion.
            from PIL import ImageGrab
            ImageGrab.grab(all_screens=True).save(capture)
        target_id = 2 if cancel else 1  # IDCANCEL / IDOK, verified against pinned pywebview
        button = next(item for item in buttons if item['control_id'] == target_id)
        if file_dialog and not cancel:
            assert self.control_text(filename['handle']) == str(destination), 'The filename changed before Save.'
        evidence['button_control'] = button
        evidence['button_target'] = self.click_control(button['handle'])
        wait_until(lambda: not self.user32.IsWindow(dialog['handle']), 'Native dialog did not close.', timeout=15)
        evidence['choice'] = 'canceled' if cancel else 'accepted'
        return evidence


class DialogAction:
    """Native modal dialogs need a second thread while WebView2 dispatches UI."""

    def __init__(self, action):
        self.result, self.error = None, None

        def run():
            try:
                self.result = action()
            except BaseException as error:
                self.error = error

        self.thread = threading.Thread(target=run, name='acceptance-native-dialog', daemon=True)
        self.thread.start()

    def finish(self, page):
        wait_until(lambda: not self.thread.is_alive(), 'Native dialog automation did not finish.',
                   timeout=45, pump=page.wait_for_timeout)
        if self.error:
            raise self.error
        return self.result


def synthetic_inputs(output):
    from PIL import Image, ImageDraw
    result = []
    for name, fill in [('green', '#67a58c'), ('blue', '#6697b1')]:
        target = output / f'SYNTHETIC-{name}.png'
        picture = Image.new('RGB', (320, 220), 'white')
        draw = ImageDraw.Draw(picture)
        draw.rectangle((20, 20, 300, 200), fill=fill)
        draw.ellipse((95, 45, 225, 175), fill='#163a59')
        picture.save(target)
        result.append(target)
    rows = ['sample,target,ct,group,biological_replicate,technical_replicate']
    for group in ('Control', 'Treatment'):
        for replicate in range(1, 4):
            sample = f'{group}-{replicate}'
            for target, ct in [('ACTB', 18), ('MYC', 20 if group == 'Control' else 19)]:
                rows.append(f'{sample},{target},{ct},{group},{sample},1')
    table = output / 'SYNTHETIC-known-qpcr.csv'
    table.write_text('\n'.join(rows) + '\n', 'utf-8')
    return result, table


def verify_packaged_source(app_dir):
    """Bind native evidence to both the complete package and this checkout."""
    manifest_path = app_dir / 'app/SOURCE_MANIFEST.json'
    manifest = json.loads(manifest_path.read_text('utf-8'))
    records = manifest['files']
    assert records and len(records) == manifest['file_count']
    for item in records:
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise AssertionError('Unsafe source manifest path.')
        packaged = app_dir / 'app' / relative
        assert packaged.stat().st_size == item['bytes'] and file_hash(packaged) == item['sha256'], str(relative)
        # The builder creates this one provenance report; every actual source
        # file must match the checked-out revision's working tree byte for byte.
        if relative.as_posix() != 'packaging/windows_validation.json':
            assert file_hash(ROOT / relative) == item['sha256'], 'Source mismatch: ' + str(relative)
    return file_hash(manifest_path), len(records)


def run(app_dir, output, *, toolkit_archive=None, extraction_record=None):
    app_dir, output = app_dir.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / 'native-acceptance.json'
    report = {'passed': False, 'platform': sys.platform, 'native_window_verified': False,
              'backend_stopped': False, 'scenarios': [], 'browser_errors': [],
              'native_dialogs': [],
              'tested_application_sha256': None, 'source_commit': None,
              'browser_attached_to': 'actual embedded WebView2; no standalone browser launched',
              'synthetic_data': True}
    launcher = page = playwright = browser = native = None
    active_scenario = 'native client startup'
    application = app_dir / 'FigureWorkbench.exe'
    data = None
    cdp_port = None

    def checked(name, started, **evidence):
        report['scenarios'].append({'name': name, 'passed': True,
                                    'elapsed_ms': round((time.perf_counter() - started) * 1000, 2), **evidence})
        print('PASS', name, flush=True)

    def save_report():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')

    try:
        native = NativeWindows()
        if report_path.exists():
            raise ValueError('Use a fresh output directory; prior test workspaces are never overwritten.')
        # The evidence artifact must not contain a WebView2 browser profile,
        # caches or cookie databases. Copy only diagnostic files in finally.
        data = Path(tempfile.mkdtemp(prefix='figure-native-验收 '))
        report['source_commit'] = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        if os.environ.get('GITHUB_SHA') and os.environ['GITHUB_SHA'] != report['source_commit']:
            raise AssertionError('The checked-out commit does not match the CI acceptance commit.')
        report['tested_application_sha256'] = file_hash(application)
        if toolkit_archive is None or extraction_record is None:
            raise AssertionError('Native acceptance requires the final complete ZIP and its fresh extraction receipt.')
        sys.path.insert(0, str(ROOT / 'packaging'))
        from build_windows_full import verify_extraction_binding
        full_binding = verify_extraction_binding(app_dir, toolkit_archive, extraction_record)
        report.update(full_toolkit_archive=full_binding['archive'],
                      full_toolkit_manifest_sha256=full_binding['manifest_sha256'],
                      full_toolkit_extraction_receipt_sha256=full_binding['receipt_sha256'],
                      verified_full_toolkit_files=full_binding['verified_files'])
        report['source_snapshot_sha256'], report['verified_source_files'] = verify_packaged_source(app_dir)
        sys.path.insert(0, str(ROOT))
        from scripts.run_acceptance import source_identity
        report['source_identity'] = source_identity()
        expected_version = json.loads((app_dir / 'app/package.json').read_text('utf-8'))['version']
        report['version'] = expected_version
        images, table = synthetic_inputs(output)
        cdp_port = available_port()
        environment = dict(os.environ, FIGURE_WORKBENCH_DESKTOP_TEST='1',
                           FIGURE_WORKBENCH_DESKTOP_TEST_PORT=str(cdp_port))
        for name in ('FIGURE_WORKBENCH_CELLPOSE_HOME', 'FIGURE_WORKBENCH_CELLPOSE_PYTHON',
                     'FIGURE_WORKBENCH_CELLPOSE_MODELS'):
            environment.pop(name, None)
        component_manifest = app_dir / 'CELLPOSE_BUILD_MANIFEST.json'
        if not component_manifest.is_file():
            raise AssertionError('The complete toolkit is missing its bundled Cellpose manifest.')
        report['cellpose_component_manifest_sha256'] = file_hash(component_manifest)
        started = time.perf_counter()
        launcher = subprocess.Popen([str(application), '--data-dir', str(data), '--port', str(available_port())],
                                    cwd=app_dir, env=environment)
        desktop = wait_until(lambda: read_identity(data / 'desktop.json'), 'Desktop owner metadata missing.', timeout=60)
        service = wait_until(lambda: read_identity(data / 'instance.json'), 'Owned backend metadata missing.', timeout=60)
        assert service['desktop_owner'] and service['pid'] != desktop['pid']
        origin = f"http://127.0.0.1:{service['port']}"
        report['health_probe'] = {}
        health = wait_until(lambda: local_json(service['port'], '/api/health', report['health_probe']),
                            'Owned backend health failed.')
        assert health['ok'] is True and health['version'] == expected_version
        assert health['instance_id'] == service['instance_id']
        native_window = wait_until(lambda: native.app_window(desktop['pid']), 'Real WinForms window missing.')
        wait_until(lambda: local_json(cdp_port, '/json/version'), 'Embedded WebView2 CDP did not start.', timeout=60)

        from playwright.sync_api import sync_playwright, expect
        playwright = sync_playwright().start()
        # A normal CDP attachment enables Playwright's allowAndName download
        # override. That bypasses the host's DownloadStarting / SaveFileDialog
        # path and saves a GUID-named file into Playwright's temporary directory.
        # Keep the actual client's download and focus behavior for acceptance.
        browser = playwright.chromium.connect_over_cdp(
            f'http://127.0.0.1:{cdp_port}', timeout=30000, no_defaults=True)
        report['cdp_attachment'] = {'no_defaults': True, 'download_owner': 'native WebView2 host'}

        def application_page():
            nonlocal page
            deadline = time.monotonic() + 30
            contexts = browser.contexts
            assert len(contexts) == 1, 'The owned WebView2 must expose exactly one browser context.'
            context = contexts[0]
            candidates = context.pages
            assert len(candidates) <= 1, 'The owned WebView2 unexpectedly created multiple pages.'
            page = candidates[0] if candidates else context.wait_for_event('page', timeout=30000)
            evidence = {'scenario': active_scenario, 'initial_url': page.url,
                        'expected_url': origin + '/?desktop=1'}
            report.setdefault('page_readiness', []).append(evidence)
            # contexts/pages/url are cached Python properties. Polling them
            # with time.sleep prevents the sync API from dispatching navigation
            # events if attachment happens while the real window is on splash.
            # Observe the host's own navigation; never navigate on its behalf.
            page.wait_for_url(evidence['expected_url'], wait_until='domcontentloaded',
                              timeout=max(1, int((deadline - time.monotonic()) * 1000)))
            evidence['ready_url'] = page.url
            return page

        page = application_page()
        page.set_default_timeout(20000)
        page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
        page.locator('#home-title').wait_for()
        assert page.locator('.home-task').count() == 4
        assert page.url == origin + '/?desktop=1'
        assert page.evaluate('typeof window.figureWorkbenchPrepareClose') == 'function'
        response = page.request.get(origin + '/')
        assert "'unsafe-eval'" not in response.headers.get('content-security-policy', '')
        assert "script-src 'self'" in response.headers['content-security-policy']
        report['native_window_verified'] = True
        report['native_window'] = native_window
        report['initial_pids'] = {'launcher': launcher.pid, 'host': desktop['pid'], 'backend': service['pid']}
        processes = native.family(launcher.pid)
        assert any(item['executable'].lower() == 'msedgewebview2.exe' for item in processes)
        assert not any(item['executable'].lower() in ('chrome.exe', 'msedge.exe', 'firefox.exe') for item in processes)
        initial_project = page.request.get(origin + '/api/project').json()
        initial_workspace = page.request.get(origin + '/api/workspace/status').json()
        assert not any(initial_project[key] for key in ('assets', 'panels', 'annotations', 'measurements'))
        assert initial_workspace['has_recovery'] is False and initial_workspace['cleanup_pending'] is False
        assert initial_workspace['stored_asset_count'] == initial_workspace['current_asset_count'] == 0
        assert page.locator('.asset-thumb img, [data-action="resume-workspace"]').count() == 0
        native.user32.SetForegroundWindow(native_window['handle'])
        page.screenshot(path=str(output / '01-home-webview.png'))
        native.screenshot(native_window['handle'], output / '01-home-native.png')
        checked(active_scenario, started, window_class=native_window['class'], health=health,
                process_family=processes, csp=response.headers['content-security-policy'],
                blank_workspace=initial_workspace)

        active_scenario = 'adjacent portable Cellpose component discovery and real isolated dependency probe'
        started = time.perf_counter()
        # The probe performs actual imports in the sibling packaged interpreter;
        # development-environment overrides were removed before launching EXE.
        component_response = page.request.get(origin + '/api/cellpose/status', timeout=60000)
        assert component_response.status == 200
        component = component_response.json()
        assert component['available'] is True and component['has_component'] is True, component
        assert component['runtime']['available'] is True and component['runtime']['status'] == 'ready', component
        assert {model['id'] for model in component['models']} == {'cyto3', 'nuclei'}
        assert all(model['status'] == 'ready' and model['available'] is True
                   and model['actual_sha256'] == model['sha256'] for model in component['models'])
        checked(active_scenario, started, component=component)

        def state_value(expression):
            return page.evaluate('async()=>{const {state,analysisSelection}=await import("/modules/state.js");return (' + expression + ')}')

        def wait_state(expression, timeout=30):
            return wait_until(lambda: state_value(expression), 'Application state did not settle: ' + expression,
                              timeout=timeout, pump=page.wait_for_timeout)

        def saved():
            wait_state('!state.busy && !state.dirty && state.saveState==="saved"')

        def click(action, suffix=''):
            page.locator(f'[data-action="{action}"]' + suffix).first.click()

        def change(selector, value):
            page.locator(selector).fill(value)
            page.locator(selector).press('Tab')

        def import_files(action, paths, suffix=''):
            with page.expect_file_chooser() as chooser:
                click(action, suffix)
            chooser.value.set_files([str(path) for path in paths])

        def native_save(action, destination, screenshot, *, cancel=False):
            before = len(read_native_download_events(data))
            handler = DialogAction(lambda: native.handle_dialog(
                launcher.pid, destination, cancel=cancel, capture=output / screenshot))
            action()
            evidence = handler.finish(page)
            report['native_dialogs'].append(evidence)

            def chosen_path():
                return next((item for item in read_native_download_events(data)[before:]
                             if item['event'] == 'choice'), None)

            choice = wait_until(chosen_path, 'The native host did not report its actual Save As result.',
                                pump=page.wait_for_timeout)
            evidence['host_choice'] = choice
            assert choice['status'] == ('canceled' if cancel else 'selected'), choice
            if not cancel:
                selected_path = Path(choice['path'])
                evidence['selected_path_exists'] = selected_path.is_file()
                assert selected_path.is_absolute(), choice
                assert os.path.normcase(str(selected_path.resolve())) == os.path.normcase(str(destination.resolve())), (
                    'SaveFileDialog returned a different destination: ' + json.dumps(evidence, ensure_ascii=False))

            def finished():
                return next((item for item in read_native_download_events(data)[before:]
                             if item['event'] == 'finished' and item['download_id'] == choice['download_id']), None)

            result = wait_until(finished, 'The native download did not reach a terminal state.',
                                pump=page.wait_for_timeout)
            evidence['host_result'] = result
            assert result['status'] == ('canceled' if cancel else 'completed'), result
            if not cancel:
                wait_until(lambda: destination.is_file() and destination.stat().st_size > 0,
                           'Native save produced no file.', pump=page.wait_for_timeout)
                evidence['selected_path_exists'] = True
                evidence['bytes'] = destination.stat().st_size
                evidence['sha256'] = file_hash(destination)
            wait_state('!state.busy')
            message=page.locator('#toast-region .toast').last.inner_text()
            if cancel:
                assert '取消' in message and '已保存文件' not in message, message
            else:
                assert message.startswith('已保存文件'), message
            return evidence

        active_scenario = 'native navigation guard keeps the client at its owned local origin'
        started = time.perf_counter()
        from playwright.sync_api import Error as PlaywrightError
        try:
            page.goto('https://figure-workbench.invalid/acceptance-navigation-probe',
                      wait_until='commit', timeout=5000)
        except PlaywrightError as error:
            assert 'ERR_ABORTED' in str(error), str(error)
        else:
            raise AssertionError('The native host allowed an external document to replace the editor.')
        assert page.url == origin + '/?desktop=1'
        assert page.locator('.home-task').count() == 4
        assert native.app_window(desktop['pid'])
        checked(active_scenario, started)

        active_scenario = 'guided image import, automatic labels, undo and redo'
        started = time.perf_counter()
        import_files('start-task', images, '[data-task="compose"]')
        wait_state('!state.busy && state.tab==="compose" && state.project.panels.length===2')
        assert state_value('state.project.panels.map(panel=>panel.label)') == ['A', 'B']
        assert page.locator('.figure-panel').count() == 2
        click('undo')
        assert state_value('state.project.panels.length') == 0
        assert state_value('state.project.assets.length') == 2
        click('redo')
        assert state_value('state.project.panels.length') == 2
        saved()
        page.screenshot(path=str(output / '02-import-compose.png'))
        checked(active_scenario, started)

        active_scenario = 'native composition parameters apply without confirmation and retain atomic undo'
        started = time.perf_counter()
        click('composition-inspector', '[data-view="layout"]')
        assert page.locator('[data-action="apply-journal-preset"], [data-action="apply-layout"]').count() == 0
        original_figure = state_value('state.project.figure')
        original_panels = state_value('state.project.panels')
        original_history = state_value('state.history.length')
        page.locator('#journal-preset').select_option('nature-single')
        wait_state('state.project.figure.width_mm===89')
        assert state_value('state.project.settings.composition.journal_preset_id') == 'nature-single'
        assert state_value('state.history.length') == original_history + 1
        assert state_value('state.project.panels.map(panel=>panel.label)') == ['a', 'b']
        assert state_value('state.project.panels.map(panel=>panel.crop)') == [panel['crop'] for panel in original_panels]
        click('undo')
        assert state_value('state.project.figure') == original_figure
        assert state_value('state.project.panels') == original_panels
        change('#layout-margin_mm', '8')
        wait_state('state.project.settings.composition.layout.margin_mm===8')
        arranged_panels = state_value('state.project.panels')
        assert arranged_panels != original_panels, 'Margin edits must rearrange the canvas without an Apply action.'
        assert state_value('state.history.length') == original_history + 1
        arranged_geometry = state_value('state.project.panels.map(panel=>[panel.x_mm,panel.y_mm,panel.width_mm,panel.height_mm])')
        page.locator('#numbering-style').select_option('lower')
        wait_state('state.project.panels[0].label==="a"')
        assert state_value('state.project.panels.map(panel=>[panel.x_mm,panel.y_mm,panel.width_mm,panel.height_mm])') == arranged_geometry
        click('undo'); click('undo')
        assert state_value('state.project.figure') == original_figure
        assert state_value('state.project.panels') == original_panels
        assert state_value('state.history.length') == original_history
        saved()
        page.screenshot(path=str(output / '02b-auto-apply-properties.png'))
        checked(active_scenario, started)

        active_scenario = 'actual qPCR computation and derived chart in the desktop client'
        started = time.perf_counter()
        click('home')
        import_files('start-task', [table], '[data-task="qpcr"]')
        wait_state('!state.busy && !state.tableLoading && state.table?.canonical')
        expect(page.locator('[data-action="run-analysis"]')).to_be_disabled()
        change('#analysis-reference_gene', 'ACTB')
        change('#analysis-control_group', 'Control')
        expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
        click('run-analysis')
        wait_state('state.job && ["done","error"].includes(state.job.status)', timeout=60)
        assert state_value('state.job.status') == 'done', state_value('state.job')
        wait_state('analysisSelection().persisted && analysisSelection().record.id===state.job.id')
        groups = {item['group']: item for item in state_value('analysisSelection().result.groups')}
        assert groups['Control']['mean'] == 1 and groups['Treatment']['mean'] == 2
        assert all(item['n_biological'] == 3 for item in groups.values())
        expected_rows = state_value('analysisSelection().result.rows')
        identity = lambda row: (row['group'], row['biological_replicate'], row['target'])
        expected_by_id = {identity(row): row for row in expected_rows}
        known_ids = {(group, f'{group}-{replicate}', 'MYC')
                     for group in ('Control', 'Treatment') for replicate in range(1, 4)}
        assert len(expected_rows) == len(expected_by_id) == 6 and set(expected_by_id) == known_ids
        result_csv = output / 'native-qpcr-results.csv'
        native_save(lambda: click('download-results'), result_csv, '03b-qpcr-csv-native.png')
        with result_csv.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            exported_rows = list(reader)
            assert set(reader.fieldnames) == {field for row in expected_rows for field in row}
        assert len(exported_rows) == 6 and {identity(row) for row in exported_rows} == known_ids
        for exported in exported_rows:
            expected = expected_by_id[identity(exported)]
            for field, value in expected.items():
                actual = exported[field]
                if isinstance(value, (int, float)):
                    assert math.isclose(float(actual), value, rel_tol=1e-12, abs_tol=1e-12), (field, exported)
                elif isinstance(value, (list, dict)):
                    assert json.loads(actual) == value, (field, exported)
                else:
                    assert actual == ('' if value is None else str(value)), (field, exported)
            assert json.loads(exported['sample_ids']) == [exported['biological_replicate']]
            assert int(exported['n_samples']) == int(exported['n_technical']) == 1
            known_expression = 1.0 if exported['group'] == 'Control' else 2.0
            assert all(float(exported[field]) == known_expression for field in ('fold_change', 'normalized', 'value'))
        template = output / 'native-qpcr-template.csv'
        native_save(lambda: click('qpcr-template'), template, '03c-qpcr-template-native.png')
        assert template.read_text('utf-8-sig').strip() == 'sample,target,ct,group,biological_replicate,technical_replicate'
        click('plot-results')
        wait_state('!state.busy && state.tab==="compose" && state.project.panels.length===3')
        assert state_value('state.project.panels.map(panel=>panel.label)') == ['A', 'B', 'C']
        saved()
        page.screenshot(path=str(output / '03-analysis-chart.png'))
        checked(active_scenario, started, known_relative_expression={'Control': 1, 'Treatment': 2},
                saved_csv={'sha256': file_hash(result_csv), 'verified_biological_rows': len(exported_rows),
                           'relative_expression_by_sample': {row['biological_replicate']: float(row['fold_change'])
                                                             for row in exported_rows}})

        active_scenario = 'native Save As and portable project with verified original bytes'
        started = time.perf_counter()
        title = '原生客户端 验收项目'
        change('#project-title', title)
        saved()
        archive_path = output / '完整项目 原生保存.figwork'
        dialog = native_save(lambda: click('save-project'), archive_path, '04-save-as-native.png')
        with ZipFile(archive_path) as archive:
            assert archive.testzip() is None
            project_bytes = archive.read('project.json')
            project = json.loads(project_bytes)
            manifest = json.loads(archive.read('manifest.json'))
            assert project['title'] == title and len(project['panels']) == 3
            assert len(project['measurements']) == 1
            assert manifest['project_sha256'] == hashlib.sha256(project_bytes).hexdigest()
            for item in manifest['assets']:
                assert hashlib.sha256(archive.read(item['path'])).hexdigest() == item['sha256']
            for original in [*images, table]:
                record = next(item for item in manifest['assets'] if item['name'] == original.name)
                assert archive.read(record['path']) == original.read_bytes()
        checked(active_scenario, started, native_dialog=dialog, archive_sha256=file_hash(archive_path))

        active_scenario = 'canceling a native save dialog keeps the editor usable'
        started = time.perf_counter()
        canceled = output / 'must-not-exist.figwork'
        native_save(lambda: click('save-project'), canceled, '05-cancel-save-native.png', cancel=True)
        assert not canceled.exists()
        assert native.app_window(desktop['pid']) and launcher.poll() is None
        checked(active_scenario, started)

        active_scenario = 'native PNG and PDF exports with dimensional and content validation'
        started = time.perf_counter()
        exports = []
        figure = state_value('state.project.figure')
        for format_name in ('png', 'pdf'):
            click('export-dialog')
            format_control = page.locator('#export-format-' + format_name)
            # The accessible radio is clipped inside its visible label card;
            # users select the card, not the hidden 1px input underneath text.
            page.locator('label.export-format').filter(has=format_control).click()
            expect(format_control).to_be_checked()
            destination = output / ('native-figure.' + format_name)
            native_save(lambda: click('export-file'), destination, f'06-export-{format_name}-native.png')
            if format_name == 'png':
                from PIL import Image, ImageStat
                with Image.open(destination) as picture:
                    expected_size = tuple(math.ceil(figure[key] * 300 / 25.4) for key in ('width_mm', 'height_mm'))
                    assert all(abs(actual - expected) <= 1 for actual, expected in zip(picture.size, expected_size))
                    assert max(ImageStat.Stat(picture.convert('RGB')).stddev) > 8
            else:
                import fitz
                with fitz.open(destination) as document:
                    assert len(document) == 1
                    rectangle = document[0].rect
                    assert abs(rectangle.width - figure['width_mm'] * 72 / 25.4) < .2
                    assert abs(rectangle.height - figure['height_mm'] * 72 / 25.4) < .2
                    assert document[0].get_images() or document[0].get_drawings()
            exports.append({'format': format_name, 'sha256': file_hash(destination), 'bytes': destination.stat().st_size})
        checked(active_scenario, started, exports=exports)

        active_scenario = 'original download through native Save As preserves source bytes'
        started = time.perf_counter()
        original_id = file_hash(images[0])
        click('select-asset', '[data-id="' + original_id + '"]')
        source_details = page.locator('#source-file-details')
        if source_details.get_attribute('open') is None:
            source_details.locator('summary').click()
        expect(page.locator('[data-action="download-original"]')).to_be_visible()
        original_copy = output / 'native-original-copy.png'
        native_save(lambda: click('download-original'), original_copy, '07-original-native.png')
        assert original_copy.read_bytes() == images[0].read_bytes()
        checked(active_scenario, started)

        active_scenario = 'second EXE launch restores the existing window and keeps one backend'
        started = time.perf_counter()
        native.user32.ShowWindow(native_window['handle'], 6)  # SW_MINIMIZE
        wait_until(lambda: native.user32.IsIconic(native_window['handle']), 'Window did not minimize.')
        second = subprocess.Popen([str(application), '--data-dir', str(data), '--port', str(service['port'])],
                                  cwd=app_dir, env=environment)
        assert second.wait(timeout=30) == 0
        wait_until(lambda: not native.user32.IsIconic(native_window['handle']), 'Second launch did not restore the window.')
        assert read_identity(data / 'desktop.json') == desktop
        assert read_identity(data / 'instance.json') == service
        assert native.app_window(desktop['pid'])['handle'] == native_window['handle']
        assert local_json(service['port'], '/api/health')['instance_id'] == service['instance_id']
        checked(active_scenario, started)

        active_scenario = 'failed save prevents silent close and native cancel preserves edits'
        started = time.perf_counter()

        def reject_save(route):
            if route.request.method == 'PUT':
                route.fulfill(status=503, content_type='application/json', body='{"detail":"Synthetic acceptance save failure"}')
            else:
                route.continue_()

        page.route('**/api/project', reject_save)
        change('#project-title', '保存失败时仍保留的修改')
        handler = DialogAction(lambda: native.handle_dialog(launcher.pid, None, file_dialog=False, cancel=True,
                                                            capture=output / '08-close-cancel-native.png'))
        native.close(native_window['handle'])
        handler.finish(page)
        assert native.app_window(desktop['pid']) and launcher.poll() is None
        assert state_value('state.dirty') is True
        assert state_value('state.project.title') == '保存失败时仍保留的修改'
        page.unroute('**/api/project', reject_save)
        page.keyboard.press('Control+s')
        saved()
        checked(active_scenario, started)

        active_scenario = 'close commits the focused input and stops the complete owned process family'
        started = time.perf_counter()
        close_title = '关闭前输入尚未失焦的标题'
        page.locator('#project-title').fill(close_title)  # deliberately do not Tab/blur
        family = native.family(launcher.pid)
        assert not any(item['executable'].lower() in ('chrome.exe', 'msedge.exe', 'firefox.exe') for item in family)
        page.screenshot(path=str(output / '09-before-close-webview.png'))
        native.screenshot(native_window['handle'], output / '09-before-close-native.png')
        native.close(native_window['handle'])
        wait_until(lambda: launcher.poll() is not None, 'Native close/save handshake did not finish.',
                   timeout=35, pump=lambda _: time.sleep(.05))
        assert launcher.returncode == 0
        wait_until(lambda: all(not native.running(item['pid']) for item in family), 'Owned process family leaked after close.', timeout=15)
        assert not (data / 'desktop.json').exists() and not (data / 'instance.json').exists()
        assert port_closed(service['port']) and port_closed(cdp_port)
        report['backend_stopped'] = True
        checked(active_scenario, started, stopped_pids=[item['pid'] for item in family])
        playwright.stop()
        playwright = browser = page = None

        active_scenario = 'restart requires explicit recovery before restoring the saved sources and analysis'
        started = time.perf_counter()
        launcher = subprocess.Popen([str(application), '--data-dir', str(data), '--port', str(available_port())],
                                    cwd=app_dir, env=environment)
        desktop2 = wait_until(lambda: read_identity(data / 'desktop.json'), 'Restart failed to acquire desktop lock.')
        service2 = wait_until(lambda: read_identity(data / 'instance.json'), 'Restart failed to acquire service lock.')
        empty_project = wait_until(lambda: local_json(service2['port'], '/api/project'), 'Restart did not provide the empty recovery view.')
        recovery = wait_until(lambda: local_json(service2['port'], '/api/workspace/status'), 'Restart did not expose recovery status.')
        assert recovery['has_recovery'] is True and recovery['cleanup_pending'] is False
        assert empty_project['title'] != close_title
        assert not empty_project['assets'] and not empty_project['panels'] and not empty_project['measurements']
        window2 = wait_until(lambda: native.app_window(desktop2['pid']), 'Restarted native window missing.')
        wait_until(lambda: local_json(cdp_port, '/json/version'), 'Restarted embedded WebView2 unavailable.')
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{cdp_port}', no_defaults=True)
        origin = f"http://127.0.0.1:{service2['port']}"
        page = application_page()
        page.set_default_timeout(20000)
        page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
        page.locator('[data-action="restore-workspace"]').wait_for()
        wait_state('state.modal?.type==="workspace-recovery" && state.workspaceStatus?.has_recovery')
        assert state_value('state.project.assets.length') == 0
        assert page.locator('.asset-thumb img').count() == 0
        assert page.locator('[data-action="resume-workspace"]').count() == 0
        page.screenshot(path=str(output / '10a-explicit-recovery.png'))
        click('restore-workspace')
        wait_state('!state.busy && !state.modal && !state.workspaceStatus.has_recovery && state.project.panels.length===3')
        restored = wait_until(lambda: local_json(service2['port'], '/api/project'), 'Explicit recovery did not restore the saved project.')
        assert restored['title'] == close_title
        assert len(restored['panels']) == 3 and len(restored['measurements']) == 1
        assert {asset['sha256'] for asset in restored['assets']}.issuperset(file_hash(item) for item in [*images, table])
        page.locator('[data-action="resume-workspace"]').wait_for()
        assert page.locator('#project-title').input_value() == close_title
        page.screenshot(path=str(output / '10-restored-workspace.png'))
        checked(active_scenario, started)

        active_scenario = 'real nuclei inference from the adjacent portable component through native UI'
        started = time.perf_counter()
        import base64
        import zlib
        import numpy as np
        import tifffile
        yy, xx = np.mgrid[:96, :96]
        nucleus = (1000 + 30000 * np.exp(-((xx - 48) ** 2 + (yy - 48) ** 2) / (17 / 2) ** 2)).astype(np.uint16)
        nucleus_path = output / 'SYNTHETIC-centered-nucleus.tif'
        tifffile.imwrite(nucleus_path, nucleus)
        nucleus_hash = file_hash(nucleus_path)
        click('start-task', '[data-task="microscopy"]')
        import_files('import', [nucleus_path])
        wait_state('!state.busy && state.selectedAssetId===' + json.dumps(nucleus_hash))
        page.locator('#analysis-method').select_option('cellpose')
        wait_state('!state.cellposeStatusLoading && state.cellposeStatus?.available', timeout=60)
        page.locator('#analysis-model').select_option('nuclei')
        change('#analysis-diameter_px', '17')
        change('#analysis-min_area', '10')
        expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
        previous_job = state_value('state.job?.id || ""')
        click('run-analysis')
        wait_state('state.job && state.job.id!==' + json.dumps(previous_job)
                   + ' && ["done","error"].includes(state.job.status)', timeout=120)
        assert state_value('state.job.status') == 'done', state_value('state.job')
        wait_state('analysisSelection().persisted && analysisSelection().record.id===state.job.id')
        segmentation = state_value('analysisSelection().result')
        model_record = segmentation['method']['model_record']
        expected_nuclei = next(item for item in component['models'] if item['id'] == 'nuclei')
        assert model_record['model'] == 'nuclei'
        assert model_record['weights_sha256'] == expected_nuclei['sha256']
        assert model_record['device'] == 'cpu' and model_record['network_download_performed'] is False
        assert model_record['inference_ms'] > 0 and segmentation['count'] > 0
        mask = segmentation['instance_mask']
        assert mask['shape'] == [96, 96]
        labels = np.frombuffer(zlib.decompress(base64.b64decode(mask['data'])), dtype='<u4').reshape(96, 96)
        assert labels[48, 48] > 0 and 10 < np.count_nonzero(labels) < nucleus.size * .9
        original_response = page.request.get(origin + '/api/assets/' + nucleus_hash + '/original')
        assert original_response.status == 200 and hashlib.sha256(original_response.body()).hexdigest() == nucleus_hash
        saved()
        page.screenshot(path=str(output / '11-native-cellpose-nuclei.png'))
        checked(active_scenario, started, model_record=model_record, foreground_pixels=int(np.count_nonzero(labels)))

        active_scenario = 'native workspace clearing deletes owned copies and preserves external files'
        started = time.perf_counter()
        external_hashes = {path: file_hash(path) for path in [*images, table, nucleus_path, archive_path]}
        owned_assets = state_value('state.project.assets.map(asset=>asset.id)')
        assert owned_assets and any((data / 'originals').iterdir())
        click('home')
        click('clear-workspace-dialog')
        wait_state('state.modal?.type==="clear-workspace"')
        click('clear-workspace')
        wait_state('!state.busy && state.modal?.type==="workspace-cleanup-exit" && state.project.assets.length===0')
        cleared_workspace = page.request.get(origin + '/api/workspace/status').json()
        cleared_project = page.request.get(origin + '/api/project').json()
        assert cleared_workspace['has_recovery'] is False and cleared_workspace['cleanup_pending'] is False
        assert cleared_workspace['stored_asset_count'] == cleared_workspace['current_asset_count'] == 0
        assert not any(cleared_project[key] for key in ('assets', 'panels', 'annotations', 'measurements'))
        assert list((data / 'originals').iterdir()) == []
        assert not (data / 'assets.json').exists() and not (data / 'current.backup.json').exists()
        assert not (data / '.workspace-clear-pending.json').exists()
        for asset_id in owned_assets:
            assert page.request.get(origin + '/api/assets/' + asset_id + '/original').status == 404
        assert all(file_hash(path) == digest for path, digest in external_hashes.items())
        assert page.locator('.asset-thumb img, [data-action="resume-workspace"]').count() == 0
        page.screenshot(path=str(output / '12-cleared-native-workspace.png'))
        checked(active_scenario, started, removed_asset_count=len(owned_assets),
                external_files_preserved=len(external_hashes), workspace_status=cleared_workspace)

        active_scenario = 'restart and completed model worker leave no processes after native close'
        started = time.perf_counter()
        family2 = native.family(launcher.pid)
        native.close(window2['handle'])
        wait_until(lambda: launcher.poll() is not None, 'Restarted client did not close.', timeout=35)
        assert launcher.returncode == 0
        wait_until(lambda: all(not native.running(item['pid']) for item in family2), 'Restarted process family leaked.', timeout=15)
        assert port_closed(service2['port']) and port_closed(cdp_port)
        assert not list((data / 'cache').glob('desktop-private-*')), 'The native preview cache survived clean exit.'
        checked(active_scenario, started)

        browser.close()
        playwright.stop()
        playwright = browser = page = None
        active_scenario = 'cleared native workspace stays empty after restart'
        started = time.perf_counter()
        launcher = subprocess.Popen([str(application), '--data-dir', str(data), '--port', str(available_port())],
                                    cwd=app_dir, env=environment)
        desktop3 = wait_until(lambda: read_identity(data / 'desktop.json'), 'Cleared workspace failed to restart.')
        service3 = wait_until(lambda: read_identity(data / 'instance.json'), 'Cleared workspace backend failed to start.')
        window3 = wait_until(lambda: native.app_window(desktop3['pid']), 'Cleared workspace native window missing.')
        wait_until(lambda: local_json(cdp_port, '/json/version'), 'Cleared workspace WebView2 unavailable.')
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{cdp_port}', no_defaults=True)
        origin = f"http://127.0.0.1:{service3['port']}"
        page = application_page()
        page.set_default_timeout(20000)
        page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
        page.locator('#home-title').wait_for()
        wait_state('state.project && !state.busy && !state.modal')
        status3 = page.request.get(origin + '/api/workspace/status').json()
        project3 = page.request.get(origin + '/api/project').json()
        assert status3['has_recovery'] is False and status3['cleanup_pending'] is False
        assert status3['stored_asset_count'] == status3['current_asset_count'] == 0
        assert not any(project3[key] for key in ('assets', 'panels', 'annotations', 'measurements'))
        assert page.locator('.asset-thumb img, [data-action="restore-workspace"], [data-action="resume-workspace"]').count() == 0
        assert list((data / 'originals').iterdir()) == []
        assert all(file_hash(path) == digest for path, digest in external_hashes.items())
        page.screenshot(path=str(output / '13-empty-after-native-restart.png'))
        family3 = native.family(launcher.pid)
        native.close(window3['handle'])
        wait_until(lambda: launcher.poll() is not None, 'Cleared workspace client did not close.', timeout=35)
        assert launcher.returncode == 0
        wait_until(lambda: all(not native.running(item['pid']) for item in family3),
                   'Cleared workspace process family leaked.', timeout=15)
        assert port_closed(service3['port']) and port_closed(cdp_port)
        assert not list((data / 'cache').glob('desktop-private-*'))
        checked(active_scenario, started, workspace_status=status3, preview_cache_removed=True)

        if report['browser_errors']:
            raise AssertionError('Uncaught application errors: ' + repr(report['browser_errors']))
        if verify_extraction_binding(app_dir, toolkit_archive, extraction_record) != full_binding:
            raise AssertionError('Native execution changed the verified complete toolkit.')
        report['full_toolkit_unchanged_after_execution'] = True
        report['passed'] = True
        save_report()
        return 0
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        report['traceback'] = traceback.format_exc()
        report['scenarios'].append({'name': active_scenario, 'passed': False, 'error': report['error']})
        if cdp_port:
            targets = local_json(cdp_port, '/json/list')
            report['failure_cdp_targets'] = [
                {key: target.get(key) for key in ('id', 'type', 'url', 'title')}
                for target in targets
            ] if isinstance(targets, list) else targets
        if browser:
            report['failure_cached_page_urls'] = [[candidate.url for candidate in context.pages]
                                                  for context in browser.contexts]
        if native and launcher:
            family = native.family(launcher.pid)
            owned = {item['pid'] for item in family}
            report['failure_windows'] = [item for item in native.windows() if item['pid'] in owned]
            # Preserve the real modal control hierarchy when a future Windows
            # version changes a selector; never replace dialog interaction with
            # a renderer download or a direct API write.
            report['failure_dialog_controls'] = [
                {'dialog': item, 'controls': native.windows(item['handle'])}
                for item in report['failure_windows'] if item['visible'] and item['class'] == '#32770'
            ]
            report['last_native_dialog'] = getattr(native, 'last_dialog', None)
            try:
                from PIL import ImageGrab
                ImageGrab.grab(all_screens=True).save(output / 'failure-native-desktop.png')
            except Exception as capture_error:
                report['capture_error'] = str(capture_error)
        if page:
            try:
                page.screenshot(path=str(output / 'failure-webview.png'), timeout=3000)
            except Exception:
                pass
        save_report()
        print(report['traceback'], file=sys.stderr, flush=True)
        return 1
    finally:
        # The parent EXE owns a KILL_ON_JOB_CLOSE job. Terminating only this test's
        # launcher is sufficient to clean up its descendants on a failing run.
        if launcher and launcher.poll() is None:
            launcher.terminate()
            try:
                launcher.wait(timeout=10)
            except subprocess.TimeoutExpired:
                launcher.kill()
                launcher.wait(timeout=5)
        if playwright:
            # Do not Browser.close(): that would terminate the renderer before
            # testing the application's own native close/lifetime contract.
            playwright.stop()
        if data is not None:
            diagnostics = output / 'diagnostics'
            diagnostics.mkdir(exist_ok=True)
            try:
                for name in ('current.json', 'desktop.json', 'instance.json'):
                    source = data / name
                    if source.is_file():
                        shutil.copy2(source, diagnostics / name)
                logs = diagnostics / 'logs'
                logs.mkdir(exist_ok=True)
                for source in (data / 'logs').glob('*.log*'):
                    if source.is_file():
                        shutil.copy2(source, logs / source.name)
                native_events = data / 'logs' / 'native-download-events.jsonl'
                if native_events.is_file():
                    report['native_download_events'] = read_native_download_events(data)
                    shutil.copy2(native_events, logs / native_events.name)
                report['diagnostics_directory'] = 'diagnostics'
            except OSError as error:
                report['diagnostics_error'] = str(error)
            try:
                shutil.rmtree(data)
            except OSError as error:
                report['temporary_workspace_cleanup_error'] = str(error)
            save_report()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--toolkit-archive', type=Path, required=True)
    parser.add_argument('--extraction-record', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.app_dir, args.output, toolkit_archive=args.toolkit_archive,
                         extraction_record=args.extraction_record))
