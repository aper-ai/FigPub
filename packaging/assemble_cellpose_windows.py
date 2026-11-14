"""Build the optional offline CPU component without executing Windows binaries.

The resulting cellpose/ directory goes beside FigureWorkbench.exe. It uses a
separate embedded Python and NumPy ABI and never modifies the main runtime.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

_spec = importlib.util.spec_from_file_location('windows_distribution', Path(__file__).with_name('assemble_windows.py'))
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

ROOT_REQUIREMENTS = builder.CELLPOSE_ROOT_REQUIREMENTS
MODEL_HASHES = {name: record['sha256'] for name, record in builder.CELLPOSE_MODELS.items()}


def assemble(args):
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Output must be a new directory; existing components are never deleted.')
    if builder.sha256(args.python_zip) != builder.PYTHON_SHA256:
        raise ValueError('The official embedded Python release hash does not match.')
    for filename, digest in MODEL_HASHES.items():
        if builder.sha256(args.models / filename) != digest:
            raise ValueError(f'Official Cellpose model hash mismatch: {filename}')
    wheels = sorted(args.wheels.glob('*.whl'))
    records = [builder.wheel_metadata(path) for path in wheels]
    builder.validate_dependencies(records, ROOT_REQUIREMENTS)
    component = output / 'cellpose'
    runtime = component / 'runtime'
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(args.python_zip) as archive:
        for member in archive.infolist():
            parts = builder.safe_parts(member.filename)
            if member.is_dir():
                continue
            destination = runtime.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
    for wheel in wheels:
        builder.install_wheel(wheel, runtime)
    (runtime / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\nimport site\n', 'utf-8')
    for filename in ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'vcomp140.dll']:
        if not (args.vc_runtime / filename).is_file():
            raise ValueError(f'Missing official Microsoft x64 runtime: {filename}')
    for path in args.vc_runtime.glob('*.dll'):
        shutil.copy2(path, runtime / path.name)
    notices = component / 'licenses' / 'Microsoft-VC-Runtime'
    notices.mkdir(parents=True)
    for path in args.vc_runtime.iterdir():
        if path.is_file() and path.suffix.lower() != '.dll':
            shutil.copy2(path, notices / path.name)
    original_notices = args.vc_runtime / 'licenses'
    if not (original_notices / 'Microsoft-VC-Redist-License.txt').is_file():
        raise ValueError('Complete original Microsoft runtime notices are required.')
    shutil.copytree(original_notices, notices / 'licenses')
    models = component / 'models'
    models.mkdir()
    for filename in [*MODEL_HASHES, 'MODEL_SOURCES.json', 'CELLPOSE-LICENSE.txt']:
        shutil.copy2(args.models / filename, models / filename)
    # Preserve optional original HTTP records referenced by MODEL_SOURCES.json.
    provenance = json.loads((args.models / 'MODEL_SOURCES.json').read_text('utf-8'))
    for model in provenance.get('models', []):
        filename = model.get('http_headers')
        if filename and builder.safe_parts(filename) == (filename,) and (args.models / filename).is_file():
            shutil.copy2(args.models / filename, models / filename)
    (component / 'windows-packages.lock').write_text('\n'.join(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}" for item in records) + '\n', 'utf-8')
    (output / 'Cellpose_CPU_使用说明.txt').write_text(
        '图研 0.2.0 · 可选 Cellpose CPU 离线组件\n\n'
        '把本包的 cellpose 文件夹放到 FigureWorkbench.exe 所在文件夹。\n'
        '双击 FigureWorkbench.exe，在显微分析中选择 Cellpose，点击重新检测即可。\n'
        '不需要安装 Python、pip、CUDA 或下载模型；本包内含官方 cyto3 和 nuclei 权重。\n'
        '主程序与本组件各自拥有独立 Python/NumPy；不要将两套 runtime 文件夹混合。\n'
        '组件只在本机运行。模型首次启动较慢，可在任务中取消。\n\n'
        '已在 Linux 对同版本 CPU 依赖与两套官方模型做真实推理验证。\n'
        'Windows 文件为 Windows x64 原生依赖包；当前没有实际 Windows 环境，未声称完成 Windows 启动验收。\n'
        '本组件不含主程序，也不会自动联网下载。\n', 'utf-8')
    manifest = {'application': 'Figure Workbench Cellpose CPU component', 'version': '0.2.0',
                'platform': 'Windows x64', 'created_at': datetime.now(timezone.utc).isoformat(),
                'windows_execution_tested': False, 'python_version': builder.PYTHON_VERSION,
                'python_source': builder.PYTHON_URL, 'python_sha256': builder.PYTHON_SHA256,
                'dependencies': records, 'models': MODEL_HASHES,
                'numba_threading_layer': 'workqueue',
                'runtime_abi_isolated': True,
                'license_records_include_original_microsoft_notices': True,
                'files': [{'path': file.relative_to(output).as_posix(), 'bytes': file.stat().st_size,
                           'sha256': builder.sha256(file)} for file in sorted(output.rglob('*')) if file.is_file()]}
    (output / 'CELLPOSE_BUILD_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    result = {'directory': str(output), 'files': len(manifest['files']) + 1,
              'uncompressed_bytes': sum(item['bytes'] for item in manifest['files']) + (output / 'CELLPOSE_BUILD_MANIFEST.json').stat().st_size}
    if not args.no_zip:
        path = output.with_name(output.name + '.zip')
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for file in sorted(output.rglob('*')):
                if file.is_file():
                    archive.write(file, output.name + '/' + file.relative_to(output).as_posix())
        result.update(archive=str(path), bytes=path.stat().st_size, sha256=builder.sha256(path))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('python-zip', 'wheels', 'vc-runtime', 'models', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--no-zip', action='store_true')
    print(json.dumps(assemble(parser.parse_args()), ensure_ascii=False))


if __name__ == '__main__':
    main()
