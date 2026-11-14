import {escapeXml} from './model.js';
import {layoutNumberAttributes,readNumericInput,markLayoutNumberEdited} from '../modules/numeric-input.js';

// Original image crop coordinates remain exact; compact display is layout-only.
export function numberField(label, name, value, options = '') {
  return `<label class="field">${escapeXml(label)}<input name="${escapeXml(name)}" type="number" step="any" ${name.startsWith('crop_') ? `value="${Number(value)}"` : layoutNumberAttributes(value)} ${options} required></label>`;
}

export {readNumericInput,markLayoutNumberEdited};
