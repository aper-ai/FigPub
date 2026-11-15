"""Apply the owner's one-time release download cleanup after RC2 is verified.

The fixed allowlist names exact existing release/asset IDs. This never changes a
branch, deletes a tag, touches build-runtime-0.4.0, or removes the old Windows ZIP.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORY = 'aper-ai/figure-workbench'
NEW_TAG = 'v0.6.2-rc.2'
SETUP = 'Figure_Workbench_0.6.2_Setup.exe'
LEGACY_RELEASE_ID = 445335566
LEGACY_TAG = 'v0.6.1'
LEGACY_KEEP_ID = 747666512
LEGACY_KEEP_NAME = 'Figure_Workbench_0.6.1_Windows_Full.zip'
LEGACY_KEEP_SHA256 = '66881bc4ef5578a9651a09af5aa4dd33bcc7676f8728c80e0a0ccaf247ab6a2e'
LEGACY_REMOVALS = {
    747667262: 'Figure_Workbench_0.6.1_Browser.html',
    747667316: 'Figure_Workbench_0.6.1_QA_Evidence.zip',
    747667224: 'Figure_Workbench_0.6.1_Source.zip',
    747667375: 'RELEASE_ACCEPTANCE.json',
    747667387: 'SHA256SUMS',
}
RETIRED_RELEASE_ID = 445978884
RETIRED_TAG = 'v0.6.2-rc.1'
RETIRED_COMMIT = '57f12d12a4b9d0a7ab0f5f23797382ee444ff0d1'


def api(endpoint: str, *, method='GET', body=None, allow_missing=False):
    arguments = ['gh', 'api', '--method', method, endpoint]
    payload = None
    if body is not None:
        arguments += ['--input', '-']
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
    result = subprocess.run(arguments, input=payload, capture_output=True)
    if result.returncode:
        error = result.stderr.decode('utf-8', 'replace')
        if allow_missing and 'HTTP 404' in error:
            return None
        raise RuntimeError(f'GitHub {method} failed for {endpoint}: {error}')
    return json.loads(result.stdout) if result.stdout.strip() else None


def check_new_release(release, tag, installer: Path, expected_commit: str):
    if (release.get('tag_name') != NEW_TAG or release.get('draft') is not False
            or release.get('prerelease') is not True or release.get('target_commitish') != expected_commit
            or release.get('html_url') != f'https://github.com/{REPOSITORY}/releases/tag/{NEW_TAG}'
            or tag.get('ref') != f'refs/tags/{NEW_TAG}'
            or tag.get('object', {}).get('type') != 'commit'
            or tag.get('object', {}).get('sha') != expected_commit):
        raise ValueError('The replacement installer release and tag are not the verified RC2 commit.')
    assets = release.get('assets')
    if not isinstance(assets, list) or len(assets) != 1:
        raise ValueError('The replacement release must contain exactly one installer.')
    with installer.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    asset = assets[0]
    if (installer.is_symlink() or installer.name != SETUP or asset.get('name') != SETUP
            or asset.get('state') != 'uploaded' or asset.get('size') != installer.stat().st_size
            or asset.get('digest') != 'sha256:' + digest):
        raise ValueError('The publicly uploaded installer does not match the verified local bytes.')
    return {'file': SETUP, 'bytes': installer.stat().st_size, 'sha256': digest}


def legacy_plan(release):
    if (release.get('id') != LEGACY_RELEASE_ID or release.get('tag_name') != LEGACY_TAG
            or release.get('draft') is not False
            or release.get('html_url') != f'https://github.com/{REPOSITORY}/releases/tag/{LEGACY_TAG}'):
        raise ValueError('The legacy release no longer matches the approved cleanup target.')
    assets = release.get('assets')
    if not isinstance(assets, list) or any(not isinstance(item, dict) for item in assets):
        raise ValueError('The legacy release has an invalid asset inventory.')
    by_id = {item.get('id'): item for item in assets}
    if len(by_id) != len(assets) or not set(by_id).issubset({LEGACY_KEEP_ID, *LEGACY_REMOVALS}):
        raise ValueError('The legacy release contains unapproved or duplicate assets; leave them unchanged.')
    kept = by_id.get(LEGACY_KEEP_ID, {})
    if (kept.get('name') != LEGACY_KEEP_NAME or kept.get('state') != 'uploaded'
            or kept.get('digest') != 'sha256:' + LEGACY_KEEP_SHA256):
        raise ValueError('The preserved legacy Windows package is missing or has changed.')
    removals = []
    for asset_id, name in LEGACY_REMOVALS.items():
        if asset_id in by_id:
            if by_id[asset_id].get('name') != name:
                raise ValueError('An approved asset ID has an unexpected name; refusing cleanup.')
            removals.append({'id': asset_id, 'name': name})
    return removals


def legacy_body():
    return f'''# Figure Workbench 0.6.1 · Windows 完整工具包

[下载 0.6.1 Windows 完整工具包](https://github.com/{REPOSITORY}/releases/download/{LEGACY_TAG}/{LEGACY_KEEP_NAME})

完整解压后运行 FigureWorkbench.exe，并保留同级 runtime、app、cellpose 目录。
此处保留已验收的旧版完整工具包；最新安装程序见
[0.6.2 RC2 Windows 安装候选版](https://github.com/{REPOSITORY}/releases/tag/{NEW_TAG})。
RC2 的完整原生客户端及包内模型推理仍需验证，不能沿用本旧版的验收结果。

原始 0.6.1 发行文件及验收资料：
[原发行记录](https://github.com/tuantuan007/figure-workbench/releases/tag/v0.6.1) ·
[原始 Windows 验收运行](https://github.com/tuantuan007/figure-workbench/actions/runs/43452637043)。

Windows 完整包 SHA-256：
`{LEGACY_KEEP_SHA256}`
'''


def cleanup(repository, new_tag, expected_commit, installer, record):
    if (repository != REPOSITORY or new_tag != NEW_TAG
            or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_SHA') != expected_commit
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or not re.fullmatch(r'[0-9a-f]{40}', expected_commit)):
        raise ValueError('This one-time cleanup is restricted to the approved repository and RC2 workflow.')
    if record.exists() or record.is_symlink():
        raise ValueError('Use a new cleanup receipt; prior audit records are not overwritten.')
    root = f'repos/{REPOSITORY}'
    current = api(f'{root}/releases/tags/{NEW_TAG}')
    tag = api(f'{root}/git/ref/tags/{NEW_TAG}')
    replacement = check_new_release(current, tag, installer, expected_commit)
    legacy = api(f'{root}/releases/{LEGACY_RELEASE_ID}')
    planned = legacy_plan(legacy)
    retired = api(f'{root}/releases/{RETIRED_RELEASE_ID}', allow_missing=True)
    retained_tag = api(f'{root}/git/ref/tags/{RETIRED_TAG}')
    if (retained_tag.get('ref') != f'refs/tags/{RETIRED_TAG}'
            or retained_tag.get('object', {}).get('type') != 'commit'
            or retained_tag.get('object', {}).get('sha') != RETIRED_COMMIT):
        raise ValueError('The retired candidate source tag does not match the approved snapshot.')
    if retired is not None and (retired.get('id') != RETIRED_RELEASE_ID
            or retired.get('tag_name') != RETIRED_TAG
            or retired.get('target_commitish') != RETIRED_COMMIT
            or retired.get('html_url') != f'https://github.com/{REPOSITORY}/releases/tag/{RETIRED_TAG}'):
        raise ValueError('The superseded candidate release differs from the approved cleanup target.')
    receipt = {'repository': REPOSITORY, 'replacement_tag': NEW_TAG,
               'source_commit': expected_commit, 'replacement_installer': replacement,
               'legacy_release_id': LEGACY_RELEASE_ID, 'preserved_asset_id': LEGACY_KEEP_ID,
               'planned_asset_deletions': planned, 'deleted_assets': [], 'retired_release_deleted': False,
               'retired_tag_preserved': False, 'build_runtime_release_untouched': True, 'passed': False}
    try:
        api(f'{root}/releases/{LEGACY_RELEASE_ID}', method='PATCH', body={'body': legacy_body()})
        for item in planned:
            api(f'{root}/releases/assets/{item["id"]}', method='DELETE')
            receipt['deleted_assets'].append(item)
        remaining = api(f'{root}/releases/{LEGACY_RELEASE_ID}')
        legacy_plan(remaining)
        if {item['id'] for item in remaining['assets']} != {LEGACY_KEEP_ID}:
            raise ValueError('Legacy cleanup did not leave exactly its Windows complete package.')
        if retired is not None:
            api(f'{root}/releases/{RETIRED_RELEASE_ID}', method='DELETE')
        if api(f'{root}/releases/{RETIRED_RELEASE_ID}', allow_missing=True) is not None:
            raise ValueError('The superseded candidate release is still present.')
        receipt['retired_release_deleted'] = True
        if api(f'{root}/git/ref/tags/{RETIRED_TAG}') != retained_tag:
            raise ValueError('The retired source tag changed unexpectedly.')
        receipt['retired_tag_preserved'] = True
        check_new_release(api(f'{root}/releases/tags/{NEW_TAG}'),
                          api(f'{root}/git/ref/tags/{NEW_TAG}'), installer, expected_commit)
        receipt['passed'] = True
    except Exception as exc:
        receipt['failure'] = str(exc)
        raise
    finally:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--new-tag', required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(cleanup(args.repository, args.new_tag, args.expected_commit,
                             args.installer, args.record), ensure_ascii=False, indent=2))
