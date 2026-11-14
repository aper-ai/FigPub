"""Build the GUI-subsystem Windows launcher with pinned Zig 0.15.2.

This cross-build produces a real PE executable, but does not claim Windows
runtime acceptance. Run tests/desktop_windows_smoke.py on Windows afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPILER_VERSION = '0.15.2'


def sha256(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pe_metadata(path: Path):
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError('Launcher is not a Windows PE file.')
        offset = struct.unpack_from('<I', header, 0x3c)[0]
        stream.seek(offset)
        pe = stream.read(96)
    if len(pe) != 96 or pe[:4] != b'PE\0\0':
        raise ValueError('Launcher PE header is incomplete.')
    result = {'machine_hex': hex(struct.unpack_from('<H', pe, 4)[0]),
              'optional_header_hex': hex(struct.unpack_from('<H', pe, 24)[0]),
              'subsystem': struct.unpack_from('<H', pe, 24 + 68)[0]}
    if result != {'machine_hex': '0x8664', 'optional_header_hex': '0x20b', 'subsystem': 2}:
        raise ValueError(f'Expected x64 GUI-subsystem executable: {result}')
    return result


def create_icon(path: Path):
    from PIL import Image, ImageDraw
    picture = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(picture)
    draw.rounded_rectangle((8, 8, 248, 248), radius=54, fill='#163a59')
    for box, color in [((45, 45, 117, 117), '#ffffff'), ((139, 45, 211, 117), '#82c3d6'),
                       ((45, 139, 117, 211), '#82c3d6'), ((139, 139, 211, 211), '#ffffff')]:
        draw.rounded_rectangle(box, radius=13, fill=color)
    path.parent.mkdir(parents=True, exist_ok=True)
    picture.save(path, format='ICO', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def build(zig: Path, output: Path, *, icon: Path | None = None):
    zig, output = zig.resolve(), output.resolve()
    version = subprocess.check_output([str(zig), 'version'], text=True).strip()
    if version != COMPILER_VERSION:
        raise ValueError(f'Expected Zig {COMPILER_VERSION}, got {version}.')
    if output.exists():
        raise ValueError('Use a new output filename; verified launchers are not overwritten.')
    output.parent.mkdir(parents=True, exist_ok=True)
    icon = (icon or ROOT / 'resources/app.ico').resolve()
    if not icon.exists():
        create_icon(icon)
    source = ROOT / 'packaging/windows_launcher.c'
    app_version = json.loads((ROOT / 'package.json').read_text())['version']
    numeric_version = ','.join(app_version.split('.') + ['0'])
    with tempfile.TemporaryDirectory(prefix='figure-desktop-compiler-') as folder:
        build_dir = Path(folder)
        manifest = build_dir / 'app.manifest'
        manifest.write_text('''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <assemblyIdentity type="win32" name="FigureWorkbench.Desktop" version="1.0.0.0" processorArchitecture="amd64"/>
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
  <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
 </requestedPrivileges></security></trustInfo>
 <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
  <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
  <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
 </windowsSettings></application>
</assembly>''', encoding='utf-8')
        resource = build_dir / 'app.rc'
        resource.write_text(f'''#include <windows.h>
1 ICON "{icon.as_posix()}"
1 RT_MANIFEST "{manifest.as_posix()}"
1 VERSIONINFO
FILEVERSION {numeric_version}
PRODUCTVERSION {numeric_version}
FILEOS VOS_NT_WINDOWS32
FILETYPE VFT_APP
BEGIN
 BLOCK "StringFileInfo"
 BEGIN
  BLOCK "040904b0"
  BEGIN
   VALUE "FileDescription", "Figure Workbench Desktop\\0"
   VALUE "ProductName", "Figure Workbench\\0"
   VALUE "FileVersion", "{app_version}\\0"
   VALUE "ProductVersion", "{app_version}\\0"
  END
 END
 BLOCK "VarFileInfo"
 BEGIN
  VALUE "Translation", 0x0409, 1200
 END
END
''', encoding='utf-8')
        res = build_dir / 'app.res'
        subprocess.run([str(zig), 'rc', '/fo', str(res), str(resource)], check=True)
        command = [str(zig), 'cc', '-target', 'x86_64-windows-gnu', '-O2', '-s', '-municode',
                   '-Wl,--subsystem,windows', str(source), str(res), '-luser32', '-lshell32', '-o', str(output)]
        subprocess.run(command, check=True)
    record = {'application_version': app_version, 'filename': output.name,
              'bytes': output.stat().st_size, 'sha256': sha256(output), **pe_metadata(output),
              'compiler': 'Zig', 'compiler_version': version, 'compiler_sha256': sha256(zig),
              'source_sha256': sha256(source), 'icon_sha256': sha256(icon),
              'windows_runtime_executed': False, 'external_browser_launch': False}
    output.with_suffix('.build.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zig', type=Path, default=shutil.which('zig'), required=shutil.which('zig') is None)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--icon', type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.zig, args.output, icon=args.icon), indent=2))


if __name__ == '__main__':
    main()
