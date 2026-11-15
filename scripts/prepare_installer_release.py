"""Prepare one Windows installer for release without weakening acceptance gates.

All operations are offline. A release candidate needs actual installer lifecycle
evidence; a stable release additionally needs the existing, fully verified six-
artifact preparation. Reports remain local Actions evidence, not release assets.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import prepare_verified_release as verified
from scripts.release_config import (VERSION, TAG, FULL, INSTALLER, PACKAGES, ATTACHMENTS,
                                     RECEIPT)

REQUIRED_INSTALLER_SCENARIOS = frozenset({
    'per_user_install', 'reinstall', 'project_preservation', 'uninstall',
})
VerificationError = verified.VerificationError
require = verified.require


def identity(repository, expected_commit, tag, prerelease):
    require(isinstance(repository, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository),
            'Invalid repository identity.')
    require(isinstance(expected_commit, str) and re.fullmatch(r'[a-f0-9]{40}', expected_commit),
            'Expected commit must be a complete SHA.')
    require(type(prerelease) is bool, 'Prerelease status must be a boolean.')
    require(isinstance(tag, str) and (bool(re.fullmatch(re.escape(TAG) + r'-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*', tag))
            if prerelease else tag == TAG), 'Release tag or prerelease status differs from this application version.')


def load_record(path):
    verified.file_record(path)
    return verified.load_object(path)


def verify_installer_evidence(installer, build, smoke, expected_commit):
    actual = verified.file_record(installer)
    require(actual['file'] == INSTALLER and verified.positive_integer(actual['bytes']),
            'The release requires the correctly named, nonempty Setup.exe.')
    require(build.get('schema_version') == 1 and build.get('version') == VERSION,
            'Installer build schema or version differs.')
    source = build.get('source_identity')
    require(isinstance(source, dict) and source.get('commit') == expected_commit
            and source.get('clean') is True and verified.hex_digest(source.get('tracked_files_sha256')),
            'Installer must belong to the requested clean source snapshot.')
    require(build.get('payload_subdirectory') == f'versions/{VERSION}-{expected_commit[:12]}'
            and verified.hex_digest(build.get('payload_manifest_sha256'))
            and verified.positive_integer(build.get('verified_payload_files')),
            'Installer is missing its complete payload identity.')
    require(build.get('installer') == actual, 'Installer bytes differ from the build record.')
    compiler, webview = build.get('compiler'), build.get('webview2_bootstrapper')
    require(isinstance(compiler, dict) and compiler.get('version') == '6.7.1'
            and verified.hex_digest(compiler.get('sha256')),
            'Installer must identify the pinned Inno Setup compiler.')
    require(isinstance(webview, dict) and verified.hex_digest(webview.get('sha256')),
            'Installer must identify its Microsoft WebView2 bootstrapper.')
    require(build.get('windows_installer_executed') is False
            and build.get('native_application_acceptance_passed') is False
            and build.get('packaged_model_inference_executed') is False,
            'Static packaging evidence cannot claim runtime acceptance.')
    require(smoke.get('schema_version') == 1 and smoke.get('source_commit') == expected_commit
            and smoke.get('installer_sha256') == actual['sha256'] and smoke.get('passed') is True
            and smoke.get('windows_installer_executed') is True,
            'Successful Windows installer execution must match the exact installer and source.')
    scenarios = smoke.get('scenarios')
    require(isinstance(scenarios, list) and bool(scenarios)
            and all(isinstance(item, dict) and isinstance(item.get('name'), str)
                    and item['name'] and item.get('passed') is True for item in scenarios),
            'Installer scenarios must all pass.')
    names = [item['name'] for item in scenarios]
    require(len(names) == len(set(names)) and REQUIRED_INSTALLER_SCENARIOS.issubset(names),
            'Installer evidence must cover per-user install, reinstall, project preservation and uninstall.')
    require(smoke.get('native_application_acceptance_passed') is False
            and smoke.get('packaged_model_inference_executed') is False,
            'Installer lifecycle evidence cannot substitute for native application or model acceptance.')
    return actual


def verify_acceptance_receipt(receipt, build, repository, expected_commit):
    require(receipt.get('schema_version') == 1 and receipt.get('version') == VERSION
            and receipt.get('tag') == TAG and receipt.get('repository') == repository
            and receipt.get('commit') == expected_commit
            and receipt.get('stage') == 'verified_before_publication'
            and receipt.get('published') is False and receipt.get('candidate_bytes_repacked') is False,
            'Stable release requires the original verified acceptance receipt.')
    gate = receipt.get('release_gate')
    require(isinstance(gate, dict) and gate.get('passed') is True
            and gate.get('expected_commit') == expected_commit
            and gate.get('source') == build['source_identity'] and gate.get('blockers') == []
            and receipt.get('source_identity') == build['source_identity'],
            'Formal acceptance and installer must identify the same clean source.')
    run = receipt.get('acceptance_run')
    require(isinstance(run, dict) and verified.positive_integer(run.get('id'))
            and run.get('head_sha') == expected_commit and run.get('status') == 'completed'
            and run.get('conclusion') == 'success'
            and run.get('html_url') == f"https://github.com/{repository}/actions/runs/{run['id']}",
            'Formal acceptance run identity differs.')


def verify_formal_preparation(directory, build, repository, expected_commit):
    directory = Path(directory)
    manifest = load_record(directory / 'upload-manifest.json')
    require(manifest.get('schema_version') == 1 and manifest.get('version') == VERSION
            and manifest.get('tag') == TAG and manifest.get('commit') == expected_commit
            and manifest.get('repository') == repository, 'Internal verified release identity differs.')
    assets = manifest.get('assets')
    require(isinstance(assets, list) and len(assets) == len(ATTACHMENTS)
            and all(isinstance(item, dict) for item in assets)
            and {item.get('file') for item in assets} == ATTACHMENTS,
            'All six internally verified release attachments are required.')
    for item in assets:
        name = item['file']
        expected_path = f'candidates/{name}' if name in PACKAGES else name
        require(item.get('path') == expected_path, 'Internal verified attachment path differs.')
        require(verified.file_record(directory / expected_path) ==
                {key: item.get(key) for key in ('file', 'bytes', 'sha256')},
                f'Internal verified attachment changed: {name}')
    receipt = load_record(directory / RECEIPT)
    verify_acceptance_receipt(receipt, build, repository, expected_commit)
    gate = verified.release_gate.evaluate(directory / 'evidence', expected_commit)
    require(gate.get('passed') is True and gate == receipt['release_gate'],
            'The existing full release gate no longer accepts the evidence.')
    full = load_record(directory / 'evidence/windows-build.json')
    require(full.get('source_identity') == build['source_identity']
            and full.get('full_toolkit_manifest_sha256') == build['payload_manifest_sha256']
            and full.get('full_toolkit_files') == build['verified_payload_files'],
            'Installer payload differs from the fully accepted Windows toolkit.')
    packages = verified.verify_package_records(directory / 'candidates', directory / 'evidence')
    require(any(item['file'] == FULL for item in packages), 'Accepted complete toolkit is missing.')
    return receipt


def release_notes(manifest, receipt=None):
    repository, tag = manifest['repository'], manifest['tag']
    url = f'https://github.com/{repository}/releases/download/{tag}/{INSTALLER}'
    status = '候选版 · 请手动测试' if manifest['prerelease'] else 'Windows 安装版'
    text = f'''# Figure Workbench {VERSION} · {status}

## 下载与安装

[下载 Windows x64 安装程序：{INSTALLER}]({url})

双击安装程序，按当前 Windows 用户安装，再从开始菜单打开 Figure Workbench。
安装程序包含完整应用、独立 Python 环境、Cellpose CPU 引擎及 nuclei、cyto3 官方模型。
主程序需要 WebView2 Runtime；安装器内嵌 Microsoft 官方引导程序，仅在缺少 Runtime 时联网下载并安装。
已有 WebView2 Runtime 的电脑无需为此额外下载。此包不包含 CUDA/GPU 环境。

项目工作区独立保存；重复安装及卸载保留用户项目。建议在安装新版本前另外备份重要工程。

## 验证范围

来源提交：`{manifest['commit']}`。
本安装程序已完成 Windows 当前用户安装、重复安装、项目保留及卸载检查。
'''
    if manifest['prerelease']:
        text += '''
这是供手动测试的预发行候选版。完整原生客户端验收及包内两模型的真实推理仍待验证，
安装器检查不代表上述验收已通过，也不代表正式稳定版。
'''
    else:
        counts = receipt.get('test_counts', {})
        text += f'''
完整工具包已通过[对应提交的正式验收]({receipt['acceptance_run']['html_url']})；
安装器封装同一份经过完整清单校验的工具包目录。
验收包含 Python 数值、接口和持久化测试（{counts.get('python', '见报告')} 项）、
前端测试（{counts.get('frontend', '见报告')} 项）、真实浏览器工作流、原生 Windows 客户端生命周期，
以及包内 nuclei、cyto3 两种官方模型的真实 CPU 推理。安装器生命周期检查单独执行。
这些验证不等于所有 Windows / DPI 环境兼容性或真实实验图像准确率认证。
'''
    text += '''
发布页仅提供这一份可安装 EXE。源码包、独立浏览器版、原始测试证据、JSON 报告与校验清单
保留在对应 GitHub Actions 的内部构建产物中，供追溯与维护使用。
'''
    return text


def prepare(installer, build_record, smoke_record, output, *, repository, expected_commit,
            tag, prerelease=False, verified_release_directory=None):
    identity(repository, expected_commit, tag, prerelease)
    build, smoke = load_record(build_record), load_record(smoke_record)
    actual = verify_installer_evidence(installer, build, smoke, expected_commit)
    require(prerelease or verified_release_directory is not None,
            'A stable release requires the existing full verified release directory.')
    receipt = None
    if verified_release_directory is not None:
        receipt = verify_formal_preparation(verified_release_directory, build, repository, expected_commit)
    output = Path(output)
    require(not output.is_symlink() and not output.exists(),
            'Use a new preparation directory; previous results are never overwritten.')
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.prepare-installer-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'prepared'
        staged.mkdir()
        shutil.copyfile(installer, staged / INSTALLER)
        require(verified.file_record(staged / INSTALLER) == actual,
                'Installer changed during preparation.')
        evidence = []
        for name, value in [('installer-build.json', build), ('installer-smoke.json', smoke)]:
            verified.write_json(staged / name, value)
            evidence.append(dict(verified.file_record(staged / name), path=name))
        if receipt is not None:
            verified.write_json(staged / RECEIPT, receipt)
            evidence.append(dict(verified.file_record(staged / RECEIPT), path=RECEIPT))
        manifest = {'schema_version': 1, 'version': VERSION, 'tag': tag, 'commit': expected_commit,
                    'repository': repository, 'prerelease': prerelease,
                    'release_gate_passed': receipt is not None,
                    'assets': [dict(actual, path=INSTALLER)], 'internal_evidence': evidence}
        verified.write_json(staged / 'upload-manifest.json', manifest)
        (staged / 'RELEASE_NOTES.md').write_text(release_notes(manifest, receipt), encoding='utf-8')
        os.replace(staged, output)
    return manifest


def verified_prepared_manifest(prepared, repository, expected_commit):
    prepared = Path(prepared)
    manifest = load_record(prepared / 'upload-manifest.json')
    identity(repository, expected_commit, manifest.get('tag'), manifest.get('prerelease'))
    require(manifest.get('schema_version') == 1 and manifest.get('version') == VERSION
            and manifest.get('repository') == repository and manifest.get('commit') == expected_commit,
            'Prepared installer release identity differs.')
    assets = manifest.get('assets')
    require(isinstance(assets, list) and len(assets) == 1 and isinstance(assets[0], dict)
            and assets[0].get('file') == INSTALLER and assets[0].get('path') == INSTALLER,
            'The public release must contain exactly one Setup.exe attachment.')
    actual = verified.file_record(prepared / INSTALLER)
    require(assets[0] == dict(actual, path=INSTALLER), 'Prepared installer changed after verification.')
    formal = manifest.get('release_gate_passed')
    require(type(formal) is bool and (manifest['prerelease'] or formal),
            'Stable installer publication requires full acceptance.')
    expected_evidence = {'installer-build.json', 'installer-smoke.json'} | ({RECEIPT} if formal else set())
    evidence = manifest.get('internal_evidence')
    require(isinstance(evidence, list) and len(evidence) == len(expected_evidence)
            and all(isinstance(item, dict) for item in evidence)
            and {item.get('file') for item in evidence} == expected_evidence,
            'Prepared internal verification evidence differs.')
    for item in evidence:
        name = item['file']
        require(item.get('path') == name, 'Internal evidence path differs.')
        require(item == dict(verified.file_record(prepared / name), path=name),
                f'Prepared verification evidence changed: {name}')
    build = load_record(prepared / 'installer-build.json')
    verify_installer_evidence(prepared / INSTALLER, build,
                              load_record(prepared / 'installer-smoke.json'), expected_commit)
    if formal:
        verify_acceptance_receipt(load_record(prepared / RECEIPT), build, repository, expected_commit)
    return manifest, actual


def verify_upload(prepared, release_path, tag_path, *, repository, expected_commit):
    manifest, actual = verified_prepared_manifest(prepared, repository, expected_commit)
    tag_name = manifest['tag']
    release = load_record(release_path)
    require(verified.positive_integer(release.get('id')) and release.get('tag_name') == tag_name
            and release.get('target_commitish') == expected_commit and release.get('draft') is True
            and release.get('prerelease') is manifest['prerelease'],
            'Remote release must still be the expected unpublished draft.')
    require(release.get('url') == f"https://api.github.com/repos/{repository}/releases/{release['id']}",
            'Remote draft belongs to another repository.')
    tag = load_record(tag_path)
    if tag.get('status') == 404:
        require(tag.get('repository') == repository and tag.get('ref') == f'refs/tags/{tag_name}'
                and tag.get('exists') is False, 'Missing-tag observation belongs to another release.')
    else:
        require(tag.get('ref') == f'refs/tags/{tag_name}' and isinstance(tag.get('object'), dict)
                and tag['object'].get('type') == 'commit' and tag['object'].get('sha') == expected_commit,
                'Remote tag does not point to the tested commit.')
    uploaded = release.get('assets')
    require(isinstance(uploaded, list) and len(uploaded) == 1 and isinstance(uploaded[0], dict)
            and uploaded[0].get('name') == INSTALLER,
            'Remote release must contain exactly one Setup.exe attachment.')
    asset = uploaded[0]
    require(asset.get('state') == 'uploaded' and verified.positive_integer(asset.get('size'))
            and asset.get('size') == actual['bytes'] and asset.get('digest') == 'sha256:' + actual['sha256'],
            'Remote installer size, digest or upload state differs.')
    result = {'passed': True, 'draft_verified': True, 'published': False, 'release_id': release['id'],
              'repository': repository, 'tag': tag_name, 'commit': expected_commit,
              'prerelease': manifest['prerelease'], 'git_tag_created': tag.get('status') != 404,
              'assets': [actual]}
    verified.write_json(Path(prepared) / 'upload-verification.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('prepare', help='Prepare a single installer and internal verification receipts.')
    for name in ('installer', 'build-record', 'smoke-record', 'output'):
        build.add_argument('--' + name, type=Path, required=True)
    build.add_argument('--tag', required=True)
    build.add_argument('--prerelease', action='store_true')
    build.add_argument('--verified-release-directory', type=Path)
    check = commands.add_parser('verify-upload', help='Verify an unpublished single-installer draft.')
    for name in ('prepared', 'release-json', 'tag-json'):
        check.add_argument('--' + name, type=Path, required=True)
    for command in (build, check):
        command.add_argument('--repository', required=True)
        command.add_argument('--expected-commit', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args.installer, args.build_record, args.smoke_record, args.output,
                repository=args.repository, expected_commit=args.expected_commit, tag=args.tag,
                prerelease=args.prerelease, verified_release_directory=args.verified_release_directory)
        else:
            result = verify_upload(args.prepared, args.release_json, args.tag_json,
                repository=args.repository, expected_commit=args.expected_commit)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Installer release verification failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
