/**
 * Physical layout fields use a concise display without losing stored precision.
 * Scientific measurements and original image coordinates do not use this helper.
 */
export function formatLayoutNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(Number(number.toFixed(3))) : '';
}

export function layoutNumberAttributes(value) {
  if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return 'value=""';
  const precise = String(Number(value)), display = formatLayoutNumber(value);
  return `value="${display}" data-layout-value="${precise}" data-layout-display="${display}"${precise !== display ? ` title="完整值：${precise}"` : ''}`;
}

export function markLayoutNumberEdited(element) {
  if (element.dataset?.layoutValue !== undefined) element.dataset.layoutEdited = 'true';
}

/** Native input events distinguish a deliberate edit from an untouched submit. */
export function readNumericInput(element) {
  if (element.value === '') return null;
  const data = element.dataset || {};
  if (data.layoutValue !== undefined && data.layoutEdited !== 'true' && element.value === data.layoutDisplay) return Number(data.layoutValue);
  return Number(element.value);
}
