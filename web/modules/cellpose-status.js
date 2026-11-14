import {escapeHtml as e} from './domain.js';

/** Readiness is established by an actual runtime probe, not by startup health. */
export function cellposeStatusView(status, modelId, loading = false) {
  const model = status?.models?.find(item => item.id === modelId);
  if (loading) return {state: 'checking', title: '正在检查本地模型…', description: '核验模型文件与 CPU 运行环境，请稍候。', button: '正在检查…'};
  if (!status || status.probe_required || status.runtime?.status === 'not_probed') return {state: 'pending', title: '待检查', description: '运行前检查模型文件与 CPU 运行环境。', button: '检查模型'};
  if (model?.available && status.runtime?.available !== false) return {state: 'available', title: '模型可用', description: `${modelId} · CPU 本地推理`, button: '重新检查'};
  const issues = {
    missing: '未找到模型文件',
    hash_mismatch: '模型文件校验失败',
    unreadable: '模型文件无法读取',
  };
  const title = issues[model?.status] || (!status.runtime?.available ? '运行环境不可用' : '所选模型不可用');
  return {state: 'unavailable', title, description: '请重新完整解压 Windows Full 工具包，并保留其中的所有文件。', button: '重新检查'};
}

export function renderCellposeStatus(status, modelId, loading = false) {
  const view = cellposeStatusView(status, modelId, loading);
  const model = status?.models?.find(item => item.id === modelId);
  const diagnostic = view.state === 'unavailable' ? status?.runtime?.reason || status?.reason : null;
  const version = status?.runtime?.cellpose_version;
  return `<div class="model-status ${view.state}" role="status" aria-live="polite"><strong>${view.title}</strong><p>${e(view.description)}</p>${diagnostic || model?.sha256 || version ? `<details class="model-diagnostics"><summary>模型信息${diagnostic ? '与诊断' : ''}</summary>${diagnostic ? `<p>${e(diagnostic)}</p>` : ''}${version ? `<p>Cellpose ${e(version)}</p>` : ''}${model?.sha256 ? `<div class="model-hash"><span>预期模型 SHA-256</span><code>${e(model.sha256)}</code></div>` : ''}${model?.actual_sha256 && model.actual_sha256 !== model.sha256 ? `<div class="model-hash"><span>当前文件 SHA-256</span><code>${e(model.actual_sha256)}</code></div>` : ''}</details>` : ''}<button class="button small" data-action="refresh-cellpose" ${loading ? 'disabled' : ''}>${view.button}</button></div>`;
}
