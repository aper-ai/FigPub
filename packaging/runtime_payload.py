"""Select the client payload independently from the complete developer source.

Only application code, reviewed static reference data, fonts, the application
icon and licenses belong in app/. Documentation screenshots, scientific input
files and saved workspaces are never client resources. Dependency distributions
and the separately verified Cellpose models retain their original inventories.
"""
from __future__ import annotations

from pathlib import PurePosixPath


APPLICATION_ROOT_FILES = frozenset({
    'LICENSE', 'run.py', 'launch.py', 'start_windows.bat', 'start_unix.sh',
    'requirements.txt', 'requirements-desktop.txt', 'package.json',
})
APPLICATION_METADATA = frozenset({'SOURCE_MANIFEST.json', 'packaging/windows_validation.json'})
WEB_REFERENCE_FILES = frozenset({'web/data/ladders.json', 'web/modules/package.json'})
FONT_SUFFIXES = frozenset({'.ttf', '.ttc', '.otf', '.woff', '.woff2'})
EXCLUDED_APPLICATION_DIRECTORIES = frozenset({
    '__pycache__', '.git', '.cache', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    '.venv', 'venv', 'node_modules', 'dist', 'test-results', 'playwright-report',
    'cache', 'caches', 'uploads', 'projects', 'workspace', 'workspaces', 'autosave',
    'screenshots', 'examples', 'samples', 'fixtures', 'tests',
})
COMPLETE_ROOT_FILES = frozenset({
    'FigureWorkbench.exe', 'LICENSE', 'WINDOWS_START_HERE.txt', 'DESKTOP_BUILD.json',
    'WINDOWS_BUILD_MANIFEST.json', 'CELLPOSE_BUILD_MANIFEST.json',
    'CELLPOSE_INTEGRATION.json', 'CELLPOSE_START_HERE.txt',
    'windows-packages.lock', 'desktop-packages.lock',
})


def is_application_source(name: str) -> bool:
    """Use a positive list; an unfamiliar data file must be reviewed explicitly."""
    if not isinstance(name, str) or '\\' in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or path.as_posix() != name or '..' in path.parts:
        return False
    if any(part.casefold() in EXCLUDED_APPLICATION_DIRECTORIES for part in path.parts[:-1]):
        return False
    if len(path.parts) == 1:
        return name in APPLICATION_ROOT_FILES
    root = path.parts[0]
    if root == 'workbench':
        return path.suffix.lower() == '.py'
    if root == 'web':
        return path.suffix.lower() in {'.html', '.css', '.js', '.mjs'} or name in WEB_REFERENCE_FILES
    if root == 'resources':
        return name == 'resources/app.ico' or (
            len(path.parts) >= 3 and path.parts[1] == 'fonts' and path.suffix.lower() in FONT_SUFFIXES)
    if root == 'licenses':
        return path.suffix.lower() in {'.txt', '.md'} or path.name in {'LICENSE', 'NOTICE', 'COPYING'}
    return False


def verify_application_payload(names) -> None:
    """Reject data even when an otherwise valid manifest declares its hash."""
    for name in names:
        if name not in APPLICATION_METADATA and not is_application_source(name):
            raise ValueError('Client payload contains a non-runtime file or workspace data: ' + str(name))


def verify_complete_payload(names) -> None:
    """Keep client data out of app/ and out of undeclared distribution folders."""
    for name in names:
        if not isinstance(name, str) or '\\' in name:
            raise ValueError('Complete client payload has an invalid path: ' + str(name))
        path = PurePosixPath(name)
        if path.is_absolute() or not path.parts or path.as_posix() != name or '..' in path.parts:
            raise ValueError('Complete client payload has an invalid path: ' + name)
        if len(path.parts) == 1:
            allowed = name in COMPLETE_ROOT_FILES
        elif path.parts[0] == 'app':
            verify_application_payload(['/'.join(path.parts[1:])])
            allowed = True
        else:
            # Scientific/Python dependencies and model weights have independent
            # pinned manifests. Do not strip their fonts, icons or data resources.
            allowed = path.parts[0] in {'runtime', 'cellpose', 'licenses'}
            if len(path.parts) > 1 and path.parts[1].casefold() in {
                    'cache', '.cache', '__pycache__', 'uploads', 'projects', 'workspace',
                    'workspaces', 'autosave', 'project.json', 'instance.json', 'desktop.json'}:
                allowed = False
        if not allowed:
            raise ValueError('Complete client payload contains an unexpected file or workspace data: ' + str(name))
