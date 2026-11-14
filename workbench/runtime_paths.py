"""Read-only application resources and writable per-user runtime locations.

The portable Windows distribution runs ``app/run.py`` with an embedded Python.
PyInstaller bundles are also understood, without writing into their extraction
directory. Importing this module does not create folders or open an autosave.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def bundle_root() -> Path:
    """Locate web/resources/benchmarks independently of the launch directory."""
    extraction = getattr(sys, '_MEIPASS', None)
    if getattr(sys, 'frozen', False) and extraction:
        return Path(extraction).resolve()
    return Path(__file__).resolve().parents[1]


def default_data_dir(*, allow_override: bool = True) -> Path:
    """Return an absolute user data path; only explicit overrides use the CWD."""
    override = os.environ.get('FIGURE_WORKBENCH_DATA') if allow_override else None
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'win32':
        configured = os.environ.get('LOCALAPPDATA')
        base = Path(configured) if configured else Path.home() / 'AppData' / 'Local'
        if not base.is_absolute():
            base = Path.home() / 'AppData' / 'Local'
        return (base / 'FigureWorkbench').expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'FigureWorkbench'
    configured = os.environ.get('XDG_DATA_HOME')
    base = Path(configured).expanduser() if configured else Path.home() / '.local' / 'share'
    # XDG_DATA_HOME is defined as an absolute path. An invalid value must not
    # redirect default autosave into the portable application's directory.
    if not base.is_absolute():
        base = Path.home() / '.local' / 'share'
    return (base / 'figure-workbench').resolve()


def check_resources() -> None:
    """Fail before starting an empty browser when a bundle is incomplete."""
    root = bundle_root()
    required = ('web/index.html', 'web/app.js', 'web/styles.css', 'web/gel-worker.js',
                'web/gel-engine.js', 'web/data/ladders.json', 'web/modules/view.js',
                'resources/fonts/DejaVuSans.ttf', 'resources/fonts/DejaVuSans-Bold.ttf',
                'resources/fonts/NotoSansCJK-Regular.ttc', 'resources/fonts/NotoSansCJK-SC.woff2')
    missing = [name for name in required if not (root / name).is_file() or (root / name).stat().st_size == 0]
    if missing:
        raise FileNotFoundError('应用资源不完整，请重新完整解压程序文件。缺少：' + ', '.join(missing))
