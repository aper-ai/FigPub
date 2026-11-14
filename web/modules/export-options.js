/** Physical dimensions accepted by the presentation format; no implicit scaling. */
export function exportSizeIssue(format, figure) {
  if (format !== 'pptx') return null;
  const dimensions = [Number(figure?.width_mm), Number(figure?.height_mm)];
  if (dimensions.every(value => Number.isFinite(value) && value >= 25.4 && value <= 1422.4)) return null;
  return 'PPTX 的宽、高均需为 25.4–1422.4 mm。请调整画布尺寸，或选择 PDF、SVG。';
}
