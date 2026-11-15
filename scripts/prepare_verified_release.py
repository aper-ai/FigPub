"""Offline release preparation and uploaded-asset verification; never publishes.

The workflow obtains GitHub metadata and the original artifact ZIP bytes. This
script validates their provenance, re-runs the existing release gate against the
checked-out source, and delivers the three tested files without repacking them.
It contains no HTTP client, token handling, or GitHub write operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from zipfile import BadZipFile, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import release_gate
from scripts.release_config import (VERSION, TAG, ACCEPTANCE_BRANCH, ACCEPTANCE_WORKFLOW,
                                     FULL, SOURCE, BROWSER, CPU_INTERMEDIATE, PACKAGES,
                                     QA, RECEIPT, CHECKSUMS, ATTACHMENTS)
MAX_EXPANDED_BYTES = 8 * 1024**3
MAX_MEMBER_BYTES = 2 * 1024**3
MAX_MEMBERS = 50_000


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def positive_integer(value):
    return type(value) is int and value > 0


def hex_digest(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def file_record(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), f'Missing or nonregular file: {path.name}')
    return {'file': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)}


def load_object(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'Duplicate JSON key in {path}: {key}')
            result[key] = value
        return result

    def reject_constant(value):
        raise VerificationError(f'Nonfinite JSON value in {path}: {value}')

    value = json.loads(Path(path).read_text('utf-8-sig'), object_pairs_hook=unique,
                       parse_constant=reject_constant)
    require(isinstance(value, dict), f'Expected a JSON object: {path}')
    return value


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def timestamp(value, field):
    require(isinstance(value, str), f'Missing GitHub {field} timestamp.')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise VerificationError(f'Invalid GitHub {field} timestamp.') from error
    require(result.tzinfo is not None, f'GitHub {field} must identify its timezone.')
    return result


def verify_provenance(metadata, repository, expected_commit, expected_run_id):
    """Bind an API-selected successful run and both immutable artifacts."""
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'Invalid repository identity.')
    require(re.fullmatch(r'[a-f0-9]{40}', expected_commit), 'Expected commit must be a complete SHA.')
    require(positive_integer(expected_run_id), 'Expected run ID must be a positive integer.')
    require(metadata.get('schema_version') == 1 and metadata.get('repository') == repository,
            'Provenance belongs to a different repository or schema.')
    repository_id = metadata.get('repository_id')
    require(positive_integer(repository_id), 'Missing source repository ID.')
    run, workflow = metadata.get('run'), metadata.get('workflow')
    require(isinstance(run, dict) and isinstance(workflow, dict), 'Missing workflow/run provenance.')
    require(run.get('id') == expected_run_id and run.get('head_sha') == expected_commit,
            'Acceptance run ID or head SHA does not match the selected release source.')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success',
            'Acceptance run has not completed successfully.')
    require(run.get('head_branch') == ACCEPTANCE_BRANCH and run.get('event') in {'push', 'workflow_dispatch', 'create'},
            'Acceptance run must originate on the designated development branch.')
    valid_paths = {ACCEPTANCE_WORKFLOW, f'{ACCEPTANCE_WORKFLOW}@{ACCEPTANCE_BRANCH}',
                   f'{ACCEPTANCE_WORKFLOW}@refs/heads/{ACCEPTANCE_BRANCH}'}
    require(run.get('path') in valid_paths and workflow.get('path') == ACCEPTANCE_WORKFLOW
            and positive_integer(workflow.get('id')) and run.get('workflow_id') == workflow['id'],
            'Acceptance workflow path or ID does not match.')
    require(positive_integer(run.get('run_attempt')), 'Missing acceptance run attempt.')
    for key in ('repository', 'head_repository'):
        entry = run.get(key)
        require(isinstance(entry, dict) and entry.get('id') == repository_id
                and entry.get('full_name') == repository, 'A fork or different repository supplied this run.')
    require(run.get('html_url') == f'https://github.com/{repository}/actions/runs/{expected_run_id}',
            'Acceptance run URL does not match its repository and ID.')
    start, end = timestamp(run.get('run_started_at'), 'run start'), timestamp(run.get('updated_at'), 'run completion')
    require(start <= end, 'Acceptance run timestamps are reversed.')
    artifacts = metadata.get('artifacts')
    expected = {f'acceptance-evidence-{expected_commit}', f'verified-candidate-{expected_commit}'}
    require(isinstance(artifacts, list) and len(artifacts) == 2
            and all(isinstance(item, dict) for item in artifacts)
            and {item.get('name') for item in artifacts} == expected, 'Exactly the two named source artifacts are required.')
    ids = set()
    for artifact in artifacts:
        aid = artifact.get('id')
        require(positive_integer(aid) and aid not in ids, 'Artifact IDs must be distinct positive integers.')
        ids.add(aid)
        require(artifact.get('expired') is False and positive_integer(artifact.get('size_in_bytes')),
                'Artifact is expired or has no byte count.')
        digest = artifact.get('digest')
        require(isinstance(digest, str) and digest.startswith('sha256:') and hex_digest(digest[7:]),
                'An authenticated SHA-256 artifact digest is required.')
        source = artifact.get('workflow_run')
        require(isinstance(source, dict) and source.get('id') == expected_run_id
                and source.get('head_sha') == expected_commit and source.get('head_branch') == ACCEPTANCE_BRANCH
                and source.get('repository_id') == repository_id and source.get('head_repository_id') == repository_id,
                'Artifact run, branch, repository or head SHA does not match the successful acceptance run.')
        require(artifact.get('archive_download_url') == f'https://api.github.com/repos/{repository}/actions/artifacts/{aid}/zip',
                'Artifact download identity does not match the source repository.')
        created = timestamp(artifact.get('created_at'), 'artifact creation')
        require(start <= created <= end, 'Artifact was not created during this successful run attempt.')
    return {item['name'].split('-' + expected_commit)[0]: item for item in artifacts}


def verify_artifact_archive(path, metadata):
    actual = file_record(path)
    require(actual['bytes'] == metadata['size_in_bytes'], f"Artifact byte count differs: {metadata['name']}")
    require('sha256:' + actual['sha256'] == metadata['digest'], f"Artifact digest differs: {metadata['name']}")
    return actual


def safe_members(archive):
    entries, seen, files, total = [], set(), set(), 0
    members = archive.infolist()
    require(len(members) <= MAX_MEMBERS, 'Artifact has too many archive members.')
    for member in members:
        raw = member.orig_filename
        require(raw == member.filename and '\\' not in raw and not raw.startswith('/'), 'Unsafe artifact archive path.')
        name = raw[:-1] if member.is_dir() else raw
        parts = name.split('/')
        require(bool(name) and all(part and part not in {'.', '..'} and part == part.rstrip(' .')
                and not re.search(r'[<>:"|?*\x00-\x1f]', part)
                and not re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)
                for part in parts), 'Unsafe artifact archive path.')
        key = name.casefold()
        require(key not in seen, 'Duplicate or case-colliding artifact member.')
        seen.add(key)
        kind = stat.S_IFMT(member.external_attr >> 16)
        require(not member.flag_bits & 1 and kind in {0, stat.S_IFREG, stat.S_IFDIR},
                'Encrypted, linked or special artifact members are forbidden.')
        require(member.is_dir() or kind != stat.S_IFDIR, 'Artifact directory metadata is inconsistent.')
        if not member.is_dir():
            require(0 <= member.file_size <= MAX_MEMBER_BYTES, 'Artifact member exceeds the file limit.')
            total += member.file_size
            require(total <= MAX_EXPANDED_BYTES, 'Artifact exceeds the expanded byte limit.')
            files.add(key)
            entries.append((name, member))
    for name, _ in entries:
        require(not any(str(parent).casefold() in files for parent in PurePosixPath(name).parents if str(parent) != '.'),
                'Artifact uses a file as a directory.')
    return entries


def extract_verified(path, destination, *, exact_names=None):
    with ZipFile(path) as archive:
        entries = safe_members(archive)
        if exact_names is not None:
            require({name for name, _ in entries} == set(exact_names),
                    'Candidate artifact must contain exactly the three correctly named release files at its root.')
        destination.mkdir(parents=True)
        for name, member in entries:
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            require(target.stat().st_size == member.file_size, 'Truncated artifact extraction.')


def verify_package_records(candidates, reports):
    build = load_object(reports / 'windows-build.json')
    component = load_object(reports / 'portable-cellpose-build.json')
    offline = load_object(reports / 'acceptance/test_browser_studio_ui/report.json')
    require(build.get('version') == VERSION and component.get('version') == VERSION, 'Candidate version differs from the release.')
    records = build.get('archives')
    require(isinstance(records, list) and len(records) == 2 and all(isinstance(item, dict) for item in records)
            and {item.get('file') for item in records} == {FULL, SOURCE}, 'Desktop build must identify exactly the complete and source ZIP files.')
    cpu = component.get('archive')
    require(isinstance(cpu, dict) and cpu.get('file') == CPU_INTERMEDIATE, 'Portable CPU intermediate archive filename differs.')
    require(build.get('edition') == 'complete' and build.get('cellpose_component', {}).get('bundled') is True,
            'The Windows candidate must include the verified Cellpose component.')
    records = [*records, {'file': BROWSER, 'bytes': offline.get('html_bytes'), 'sha256': offline.get('html_sha256')}]
    result = []
    for expected in records:
        require(positive_integer(expected.get('bytes')) and hex_digest(expected.get('sha256')),
                f"Missing byte count or SHA-256 for {expected['file']}.")
        actual = file_record(candidates / expected['file'])
        require(actual['bytes'] == expected['bytes'] and actual['sha256'] == expected['sha256'],
                f"Tested candidate bytes do not match: {expected['file']}")
        result.append(actual)
    return sorted(result, key=lambda item: item['file'])


def release_notes(receipt):
    counts = receipt['test_counts']
    return f'''# Figure Workbench {VERSION}

本版提供包含 Cellpose CPU 引擎与官方 nuclei、cyto3 模型的 Windows 完整工具包。
一次解压后运行 `FigureWorkbench.exe`，工作区在独立客户端窗口中打开，无需额外下载或合并模型组件。

## 下载与使用

| 文件 | 用途 |
| --- | --- |
| `{FULL}` | Windows x64 完整工具包，内置主程序、独立运行环境和两种 Cellpose CPU 模型；完整解压后运行 EXE。 |
| `{BROWSER}` | 独立浏览器组图版；本地打开即可使用。它提供图片排版与导出，完整科学分析请使用客户端。 |
| `{SOURCE}` | 与本次验收对应的完整源码包。 |

## 本版改进

- 去除期刊尺寸、布局和对象属性中的重复“应用”按钮，参数修改后自动生效；连续输入保留光标和中文输入状态，并合并撤销记录。
- 保存与导出前提交有效参数，无效草稿保留原图稿并提示修正；期刊默认编号仅在选择预设时设置，尺寸调整不覆盖用户编号样式。
- 正式客户端使用运行文件白名单，不包含实验图片、案例工程、文档截图或测试数据；首次启动为空白工作区，历史自动保存需要明确选择恢复。
- 增加工作区清理和失败重试，阻止清理期间的旧请求重新写入素材，并避免浏览器旧标签页恢复已经清除的数据。
- 保留完整工具包的 WB、qPCR、多通道、IHC、纤维、Cellpose、实例编辑、工程重开与导出能力，并对同一候选提交重新执行数值、浏览器及原生客户端验收。
- 最终完整 ZIP 在新目录解压后执行包内两模型及原生客户端验收；发行附件直接采用这些已测试的文件字节。

## 本次验收

提交：`{receipt['commit']}`  
Windows 验收：[运行 {receipt['acceptance_run']['id']}]({receipt['acceptance_run']['html_url']})

- Python 数值、接口和持久化测试：{counts['python']} 项通过。
- 前端测试：{counts['frontend']} 项通过，lint 通过。
- 真实浏览器工作流：{counts['browser_suites']} 套、{counts['browser_scenarios']} 个场景通过。
- 原生 Windows 客户端：{counts['native_scenarios']} 个场景通过，含窗口、保存及退出清理。
- 内置 CPU 引擎：nuclei、cyto3 两个官方模型均使用完整工具包中的解释器完成真实推理。

交付文件直接取自上述成功验收运行，未重新打包。`{QA}` 包含原始报告、日志和截图，
`{RECEIPT}` 记录来源、各文件哈希及重新执行的发行门槛，`{CHECKSUMS}` 可用于核对下载文件。

Windows 10/11 需要 WebView2 Runtime。完整包包含 CPU 模型环境，没有 CUDA/GPU 环境；模型分割结果仍需结合原图复核。
本次测试包括合成数据的已知结果与异常恢复验证，不等于真实实验图像准确率、全 Windows / DPI 环境兼容性或完整无障碍认证。撤销历史目前仍主要覆盖排版与各自编辑工作流，素材导入会重置全局排版历史。
'''


def prepare(metadata_path, evidence_archive, candidate_archive, output, *, repository, expected_commit, expected_run_id):
    metadata = load_object(metadata_path)
    artifacts = verify_provenance(metadata, repository, expected_commit, expected_run_id)
    verify_artifact_archive(evidence_archive, artifacts['acceptance-evidence'])
    verify_artifact_archive(candidate_archive, artifacts['verified-candidate'])
    output = Path(output).resolve()
    require(not output.exists(), 'Use a new preparation directory; previous results are never overwritten.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.prepare-release-', dir=output.parent) as temporary:
        prepared = Path(temporary) / 'prepared'
        prepared.mkdir()
        reports, candidates = prepared / 'evidence', prepared / 'candidates'
        extract_verified(evidence_archive, reports)
        extract_verified(candidate_archive, candidates, exact_names=PACKAGES)
        gate = release_gate.evaluate(reports, expected_commit)
        require(gate.get('passed') is True, 'Release gate rejected the evidence: ' + '; '.join(gate.get('blockers', [])))
        packages = verify_package_records(candidates, reports)
        # Retain the authenticated evidence ZIP itself; even QA evidence is not repacked.
        shutil.copyfile(evidence_archive, prepared / QA)
        qa = file_record(prepared / QA)
        require('sha256:' + qa['sha256'] == artifacts['acceptance-evidence']['digest'], 'Evidence copy changed.')
        python = load_object(reports / 'python-checks.json')
        frontend = load_object(reports / 'frontend-checks.json')
        native = load_object(reports / 'desktop/native-acceptance.json')
        browser_scenarios = sum(len(load_object(reports / 'acceptance' / Path(suite).stem /
            release_gate.REPORT_NAMES.get(suite, 'browser-report.json'))['scenarios']) for suite in release_gate.SUITES)
        run = metadata['run']
        receipt = {'schema_version': 1, 'version': VERSION, 'tag': TAG, 'repository': repository,
                   'commit': expected_commit, 'prepared_at_utc': datetime.now(timezone.utc).isoformat(),
                   'stage': 'verified_before_publication', 'published': False, 'candidate_bytes_repacked': False,
                   'source_identity': gate['source'], 'release_gate': gate,
                   'acceptance_run': {key: run[key] for key in ('id', 'run_attempt', 'workflow_id', 'path',
                       'head_branch', 'head_sha', 'event', 'status', 'conclusion', 'html_url', 'run_started_at', 'updated_at')},
                   'source_artifacts': [{key: item[key] for key in ('id', 'name', 'size_in_bytes', 'digest',
                       'created_at', 'workflow_run', 'archive_download_url')} for item in metadata['artifacts']],
                   'test_counts': {'python': python['test_count'], 'frontend': frontend['test_count'],
                       'browser_suites': len(release_gate.SUITES), 'browser_scenarios': browser_scenarios,
                       'native_scenarios': len(native['scenarios']), 'portable_official_models': 2},
                   'files': [*packages, qa]}
        write_json(prepared / RECEIPT, receipt)
        (prepared / 'RELEASE_NOTES.md').write_text(release_notes(receipt), encoding='utf-8')
        checks = sorted([*packages, qa, file_record(prepared / RECEIPT)], key=lambda item: item['file'])
        (prepared / CHECKSUMS).write_text(''.join(f"{item['sha256']}  {item['file']}\n" for item in checks), encoding='utf-8')
        assets = [dict(item, path=f"candidates/{item['file']}" if item['file'] in PACKAGES else item['file'])
                  for item in [*checks, file_record(prepared / CHECKSUMS)]]
        manifest = {'schema_version': 1, 'version': VERSION, 'tag': TAG, 'commit': expected_commit,
                    'repository': repository, 'assets': sorted(assets, key=lambda item: item['file'])}
        write_json(prepared / 'upload-manifest.json', manifest)
        os.replace(prepared, output)
    return manifest


def verify_upload(prepared, release_path, tag_path, *, repository, expected_commit):
    """Check fresh API responses against local bytes before a draft is published."""
    prepared = Path(prepared)
    manifest = load_object(prepared / 'upload-manifest.json')
    require(manifest.get('commit') == expected_commit and manifest.get('repository') == repository
            and manifest.get('tag') == TAG and manifest.get('version') == VERSION, 'Prepared release identity differs.')
    records = manifest.get('assets')
    require(isinstance(records, list) and len(records) == len(ATTACHMENTS)
            and all(isinstance(item, dict) for item in records)
            and {item.get('file') for item in records} == ATTACHMENTS, 'Prepared attachment list differs.')
    local = {}
    for item in records:
        name = item['file']
        expected_path = f'candidates/{name}' if name in PACKAGES else name
        require(item.get('path') == expected_path, 'Prepared attachment path differs.')
        actual = file_record(prepared.joinpath(*PurePosixPath(expected_path).parts))
        require(actual['bytes'] == item.get('bytes') and actual['sha256'] == item.get('sha256'),
                f'Prepared attachment changed after verification: {name}')
        local[name] = actual
    release = load_object(release_path)
    require(positive_integer(release.get('id')) and release.get('tag_name') == TAG
            and release.get('target_commitish') == expected_commit and release.get('draft') is True
            and release.get('prerelease') is False, 'Remote release must still be the expected unpublished draft.')
    require(release.get('url') == f"https://api.github.com/repos/{repository}/releases/{release['id']}",
            'Remote draft belongs to another repository.')
    tag = load_object(tag_path)
    # A draft for a new version may not create its Git ref until publication.
    # Accept only an explicit 404 for this repository/ref, or the exact commit.
    if tag.get('status') == 404:
        require(tag.get('repository') == repository and tag.get('ref') == f'refs/tags/{TAG}'
                and tag.get('exists') is False, 'Missing-tag observation belongs to another release.')
    else:
        require(tag.get('ref') == f'refs/tags/{TAG}' and isinstance(tag.get('object'), dict)
                and tag['object'].get('type') == 'commit' and tag['object'].get('sha') == expected_commit,
                'Remote tag does not point to the tested commit.')
    uploaded = release.get('assets')
    require(isinstance(uploaded, list) and len(uploaded) == len(ATTACHMENTS)
            and all(isinstance(item, dict) for item in uploaded)
            and {item.get('name') for item in uploaded} == ATTACHMENTS, 'Remote attachment names or count differ.')
    for item in uploaded:
        expected = local[item['name']]
        require(item.get('state') == 'uploaded' and positive_integer(item.get('size')) and item.get('size') == expected['bytes']
                and item.get('digest') == 'sha256:' + expected['sha256'],
                f"Remote attachment size, digest or upload state differs: {item['name']}")
    result = {'passed': True, 'draft_verified': True, 'published': False, 'release_id': release['id'],
              'repository': repository, 'tag': TAG, 'commit': expected_commit,
              'git_tag_created': tag.get('status') != 404,
              'assets': [local[name] for name in sorted(local)]}
    write_json(prepared / 'upload-verification.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('prepare', help='Verify artifacts and prepare local release attachments.')
    build.add_argument('--metadata', type=Path, required=True)
    build.add_argument('--evidence-archive', type=Path, required=True)
    build.add_argument('--candidate-archive', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('--expected-run-id', type=int, required=True)
    check = commands.add_parser('verify-upload', help='Verify downloaded draft metadata; never publishes it.')
    check.add_argument('--prepared', type=Path, required=True)
    check.add_argument('--release-json', type=Path, required=True)
    check.add_argument('--tag-json', type=Path, required=True)
    for command in (build, check):
        command.add_argument('--repository', required=True)
        command.add_argument('--expected-commit', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args.metadata, args.evidence_archive, args.candidate_archive, args.output,
                repository=args.repository, expected_commit=args.expected_commit, expected_run_id=args.expected_run_id)
        else:
            result = verify_upload(args.prepared, args.release_json, args.tag_json,
                repository=args.repository, expected_commit=args.expected_commit)
    except (OSError, ValueError, BadZipFile, KeyError, TypeError) as error:
        print(f'Release verification failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
