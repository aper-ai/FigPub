"""Wrap a verified complete Windows payload in one per-user Inno Setup EXE.

The embedded payload keeps its independent Python/Cellpose runtimes and model
weights. Compiler, bootstrapper, source, payload and final EXE identities are
recorded internally; this build does not claim application or model acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packaging'))
from build_windows_full import verify_full_directory
from fetch_webview2_bootstrapper import verify_bootstrapper, windows_file_metadata
from scripts.release_config import VERSION, INSTALLER

APP_ID = 'FigureWorkbench.Desktop'
COMPILER_VERSION = '6.7.1'
TEMPLATE = Path(__file__).with_name('windows_installer.iss')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_payload(toolkit: Path, expected_commit: str) -> tuple[dict, str]:
    if not re.fullmatch(r'[0-9a-f]{40}', expected_commit):
        raise ValueError('Expected source commit must be a complete Git SHA.')
    if toolkit.is_symlink() or not toolkit.is_dir():
        raise ValueError('The installer must receive a verified complete toolkit directory.')
    verified = verify_full_directory(toolkit)
    source = verified.get('source_identity') or {}
    if source.get('commit') != expected_commit or source.get('clean') is not True:
        raise ValueError('The complete payload must come from the exact clean source commit.')
    return verified, f'{VERSION}-{expected_commit[:12]}'


def find_compiler(explicit: Path | None = None) -> tuple[Path, dict]:
    candidates = [explicit] if explicit else [
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Inno Setup 6/ISCC.exe',
        Path(local) / 'Programs/Inno Setup 6/ISCC.exe' if (local := os.environ.get('LOCALAPPDATA')) else None,
        Path(found) if (found := shutil.which('ISCC.exe')) else None,
    ]

    def distribution(path):
        if not path or not path.is_file() or path.is_symlink():
            return False
        engine = path.with_name('ISCmplr.dll')
        return engine.is_file() and not engine.is_symlink()

    compiler = next((path.resolve() for path in candidates if distribution(path)), None)
    if compiler is None:
        raise ValueError('The Windows runner must provide the real Inno Setup compiler beside ISCmplr.dll.')
    engine = compiler.with_name('ISCmplr.dll')
    metadata = windows_file_metadata(compiler)
    engine_metadata = windows_file_metadata(engine)
    # Inno 6.7.1 PE resources can say 0.0.0.0; ask the loaded engine instead.
    # /? exits before loading it, and /Q or /Qp hides its version banner.
    with tempfile.TemporaryDirectory(prefix='fw-compiler-version-') as temporary:
        root = Path(temporary).resolve()
        output = root / 'output'
        output.mkdir()
        script = ('[Setup]\nAppName=CompilerVersionProbe\nAppVersion=1.0\n'
                  'DefaultDirName={autopf}\\CompilerVersionProbe\nUninstallable=no\n'
                  f'Output=no\nOutputDir={output}\nOutputBaseFilename=compiler-version-probe\n')
        probe = subprocess.run([str(compiler), '/O-', '-'], input=script.encode('utf-8'),
                               capture_output=True, cwd=root, timeout=30)
        stdout = probe.stdout.decode('utf-8', 'replace')
        banners = [line.strip() for line in stdout.splitlines()
                   if line.startswith('Compiler engine version: ')]
        expected_banner = f'Compiler engine version: Inno Setup {COMPILER_VERSION}'
        if probe.returncode or banners != [expected_banner]:
            diagnostic = (stdout + probe.stderr.decode('utf-8', 'replace'))[-4000:]
            raise ValueError(f'Expected Inno Setup {COMPILER_VERSION} compiler engine at {compiler}; '
                             f'probe exited {probe.returncode}.\n{diagnostic}')
        if any(output.iterdir()):
            raise ValueError('Compiler version probe unexpectedly emitted installer files.')
    return compiler, {'file': str(compiler), 'version': COMPILER_VERSION,
                      'sha256': digest(compiler), 'pe_metadata': metadata,
                      'engine': {'file': str(engine), 'sha256': digest(engine), 'pe_metadata': engine_metadata},
                      'version_probe': {'returncode': probe.returncode, 'banner': banners[0], 'output_disabled': True}}


def check_setup_executable(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size < 4096:
        raise ValueError('The installer compiler did not produce a complete setup executable.')
    with path.open('rb') as stream:
        header = stream.read(64)
        if header[:2] != b'MZ':
            raise ValueError('The setup output is not a PE executable.')
        position = struct.unpack_from('<I', header, 60)[0]
        stream.seek(position)
        if stream.read(4) != b'PE\0\0':
            raise ValueError('The setup output has no valid PE signature.')


def build(toolkit: Path, bootstrapper: Path, bootstrapper_record: Path, output: Path,
          record: Path, expected_commit: str, compiler: Path | None = None) -> dict:
    if os.name != 'nt':
        raise OSError('Compile the installer on the Windows build runner.')
    if output.name != INSTALLER:
        raise ValueError('Use the versioned installer filename: ' + INSTALLER)
    if any(path.exists() or path.is_symlink() for path in (output, record)):
        raise ValueError('Existing installers and receipts are never overwritten.')
    if toolkit.is_symlink() or bootstrapper.is_symlink():
        raise ValueError('Installer input roots may not be symlinks.')
    toolkit, output, record = toolkit.resolve(), output.resolve(), record.resolve()
    if output.is_relative_to(toolkit) or record.is_relative_to(toolkit):
        raise ValueError('Installer outputs must remain outside the verified payload.')
    verified, build_directory = verified_payload(toolkit, expected_commit)
    bootstrapper = bootstrapper.resolve()
    prerequisite = verify_bootstrapper(bootstrapper, json.loads(bootstrapper_record.read_text('utf-8')))
    compiler, compiler_identity = find_compiler(compiler)
    template_sha = digest(TEMPLATE)
    output.parent.mkdir(parents=True, exist_ok=True)
    record.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        str(compiler), '/Qp', f'/DPayloadDir={toolkit}',
        f'/DBootstrapperPath={bootstrapper}', f'/DAppVersion={VERSION}',
        f'/DBuildDirectory={build_directory}', f'/DOutputDirectory={output.parent}',
        f'/DOutputName={output.stem}', str(TEMPLATE),
    ]
    print(f'Compiling one Windows setup executable with Inno Setup {COMPILER_VERSION}.', flush=True)
    completed = subprocess.run(arguments, capture_output=True, timeout=1800)
    log = record.with_suffix('.compiler.log')
    log.write_bytes(completed.stdout + completed.stderr)
    if completed.returncode:
        raise RuntimeError(f'Installer compilation failed ({completed.returncode}); see {log}.')
    check_setup_executable(output)
    if verified_payload(toolkit, expected_commit)[0] != verified or digest(TEMPLATE) != template_sha:
        raise ValueError('Installer source or payload changed during compilation.')
    verify_bootstrapper(bootstrapper, prerequisite)
    result = {
        'schema_version': 1, 'version': VERSION, 'application_id': APP_ID,
        'source_identity': verified['source_identity'],
        'payload_subdirectory': 'versions/' + build_directory,
        'payload_manifest_sha256': verified['manifest_sha256'],
        'verified_payload_files': verified['verified_files'],
        'installer': {'file': output.name, 'bytes': output.stat().st_size, 'sha256': digest(output)},
        'compiler': compiler_identity, 'template_sha256': template_sha,
        'webview2_bootstrapper': prerequisite, 'installer_signing': 'unsigned',
        'install_scope': 'current_user', 'administrator_prompt_required': False,
        'user_workspace_removed_on_uninstall': False,
        'windows_installer_executed': False, 'native_application_acceptance_passed': False,
        'packaged_model_inference_executed': False,
    }
    record.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolkit', type=Path, required=True)
    parser.add_argument('--webview-bootstrapper', type=Path, required=True)
    parser.add_argument('--webview-record', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--compiler', type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.toolkit, args.webview_bootstrapper, args.webview_record,
                           args.output, args.record, args.expected_commit, args.compiler), indent=2))
