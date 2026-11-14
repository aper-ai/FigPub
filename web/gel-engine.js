/* Figure Workbench — independently rebuilt localization engine.
 * Display/localization pixels ONLY. Quantification always uses original arrays
 * in the Python core. O(pixel count) projections; O(n) sliding filters; no DOM.
 * GelFigure is the functional reference, not a runtime dependency.
 * SPDX-License-Identifier: AGPL-3.0-only
 */
(function (scope) {
  'use strict';
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  function quantile(values, fraction) {
    if (!values.length) return 0;
    const sorted = Array.from(values).sort((a, b) => a - b);
    return sorted[Math.round((sorted.length - 1) * fraction)];
  }
  function smooth(values, radius) {
    const count = values.length;
    const cumulative = new Float64Array(count + 1);
    const result = new Float32Array(count);
    for (let index = 0; index < count; index++) cumulative[index + 1] = cumulative[index] + values[index];
    for (let index = 0; index < count; index++) {
      const left = Math.max(0, index - radius), right = Math.min(count, index + radius + 1);
      result[index] = (cumulative[right] - cumulative[left]) / (right - left);
    }
    return result;
  }
  function peakCandidates(values, minimumDistance, maximumCount = 96) {
    const baseline = quantile(values, 0.5);
    const deviations = Array.from(values, value => Math.abs(value - baseline));
    const noise = Math.max(1e-8, 1.4826 * quantile(deviations, 0.5));
    const highest = Math.max(...values);
    const threshold = baseline + Math.max(noise * 1.3, (highest - baseline) * 0.08);
    const candidates = [];
    for (let index = 1; index < values.length - 1; index++) {
      if (values[index] <= threshold || values[index] < values[index - 1] || values[index] < values[index + 1]) continue;
      let left = index, right = index;
      const halfHeight = baseline + (values[index] - baseline) / 2;
      while (left > 0 && values[left] > halfHeight) left--;
      while (right < values.length - 1 && values[right] > halfHeight) right++;
      let weightedPosition = 0, mass = 0;
      for (let point = left; point <= right; point++) {
        const weight = Math.max(0, values[point] - baseline);
        weightedPosition += point * weight; mass += weight;
      }
      candidates.push({position: mass ? weightedPosition / mass : index, strength: values[index] - baseline, width: right - left, snr: (values[index] - baseline) / noise});
    }
    candidates.sort((a, b) => b.strength - a.strength);
    const accepted = [];
    for (const candidate of candidates) {
      if (accepted.every(other => Math.abs(other.position - candidate.position) >= minimumDistance)) accepted.push(candidate);
      if (accepted.length >= maximumCount) break;
    }
    return accepted.sort((a, b) => a.position - b.position);
  }
  function chooseLaneGrid(peaks, projection, expectedCount) {
    if (!expectedCount) return {lanes: peaks, inferred: []};
    const count = Math.round(expectedCount);
    if (count < 1 || count > 64) throw new Error('泳道数量需为 1–64');
    if (peaks.length === count) return {lanes: peaks, inferred: []};
    if (count === 1) return {lanes: peaks.length ? [peaks.reduce((best, next) => next.strength > best.strength ? next : best)] : [], inferred: []};
    if (peaks.length < 2) return {lanes: peaks, inferred: [], warning: '可见信号不足，请手动设定泳道；没有按数量虚构检测结果。'};
    const width = projection.length;
    let best = null;
    const maximum = Math.max(...projection) || 1;
    const coarsePitch = (peaks[peaks.length - 1].position - peaks[0].position) / (count - 1);
    for (let pitchStep = -6; pitchStep <= 6; pitchStep++) {
      const pitch = coarsePitch * (1 + pitchStep * 0.035);
      for (let offsetStep = -8; offsetStep <= 8; offsetStep++) {
        const start = peaks[0].position + offsetStep * pitch * 0.045;
        if (start < 1 || start + pitch * (count - 1) > width - 2) continue;
        let score = 0;
        const lanes = [], inferred = [];
        const used = new Set();
        for (let laneIndex = 0; laneIndex < count; laneIndex++) {
          const predicted = start + laneIndex * pitch;
          let nearest = null, nearestIndex = -1;
          peaks.forEach((peak, peakIndex) => {
            if (used.has(peakIndex)) return;
            if (!nearest || Math.abs(peak.position - predicted) < Math.abs(nearest.position - predicted)) { nearest = peak; nearestIndex = peakIndex; }
          });
          const offset = nearest ? Math.abs(nearest.position - predicted) / pitch : 1;
          if (nearest && offset < 0.26) {
            lanes.push(nearest); used.add(nearestIndex); score += nearest.strength / maximum - offset * 0.8;
          } else {
            lanes.push({position: predicted, strength: 0, snr: 0, width: pitch * 0.55, inferred: true});
            inferred.push(laneIndex); score -= 0.45;
          }
        }
        if (!best || score > best.score) best = {lanes, inferred, score};
      }
    }
    return best || {lanes: peaks, inferred: [], warning: '未找到稳定泳道间距，请手动校正。'};
  }
  function estimateTilt(signal, width, height) {
    // Use a bounded image sample and require a material score gain. Only suggest:
    // applying it must preserve source-coordinate transforms in the editor.
    const step = Math.max(1, Math.floor(Math.max(width, height) / 180));
    const bins = Math.ceil(Math.hypot(width, height) / step) * 2 + 2;
    const offset = bins / 3;
    let bestAngle = 0, bestScore = 0, zeroScore = 0;
    for (let degree = -10; degree <= 10; degree++) {
      const slope = Math.tan(degree * Math.PI / 180);
      const profile = new Float64Array(bins);
      const populations = new Uint32Array(bins);
      for (let y = 0; y < height; y += step) for (let x = 0; x < width; x += step) {
        const bin = clamp(Math.round((y - slope * (x - width / 2)) / step + offset), 0, bins - 1);
        profile[bin] += signal[y * width + x]; populations[bin]++;
      }
      let score = 0;
      for (let index = 0; index < bins; index++) if (populations[index] > 4) score += profile[index] ** 2 / populations[index];
      if (degree === 0) zeroScore = score;
      if (score > bestScore) { bestScore = score; bestAngle = degree; }
    }
    return bestScore > zeroScore * 1.1 ? -bestAngle : 0;
  }
  function analyze(image, options = {}) {
    const started = performance.now();
    const width = Number(image.width), height = Number(image.height);
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 3 || height < 3 || width > 8192 || height > 8192 || width * height > 4_500_000) throw new Error('定位图像尺寸无效或过大');
    const pixels = image.data instanceof Float32Array ? image.data : Float32Array.from(image.data || []);
    if (pixels.length !== width * height) throw new Error('定位像素数量与尺寸不一致');
    const sample = [];
    for (let index = 0; index < pixels.length; index += Math.max(1, Math.floor(pixels.length / 16000))) {
      if (!Number.isFinite(pixels[index])) throw new Error('定位像素包含非有限值');
      sample.push(pixels[index]);
    }
    const median = quantile(sample, 0.5), average = sample.reduce((sum, value) => sum + value, 0) / sample.length;
    const bright = options.polarity ? options.polarity === 'bright' : median < average;
    const signal = new Float32Array(pixels.length);
    const columnEnergy = new Float64Array(width);
    const rowHistogram = new Uint32Array(128);
    for (let y = 0; y < height; y++) {
      rowHistogram.fill(0);
      for (let x = 0; x < width; x++) {
        const value = pixels[y * width + x];
        if (!Number.isFinite(value)) throw new Error('定位像素包含非有限值');
        rowHistogram[clamp(Math.round(value * 127), 0, 127)]++;
      }
      let cumulative = 0, background = 0;
      for (let bin = 0; bin < 128; bin++) { cumulative += rowHistogram[bin]; if (cumulative >= width * (bright ? 0.35 : 0.65)) {background = bin / 127; break;} }
      for (let x = 0; x < width; x++) {
        const intensity = Math.max(0, (pixels[y * width + x] - background) * (bright ? 1 : -1));
        signal[y * width + x] = intensity;
        columnEnergy[x] += intensity * intensity;
      }
    }
    const projection = smooth(smooth(columnEnergy, Math.max(1, Math.round(width / 600))), Math.max(1, Math.round(width / 600)));
    const minimumDistance = options.lane_count ? width / (options.lane_count * 2.7) : Math.max(5, width / 45);
    const peaks = peakCandidates(projection, minimumDistance, 64);
    const grid = chooseLaneGrid(peaks, projection, options.lane_count);
    const scaleX = Number(image.source_width || width) / width;
    const scaleY = Number(image.source_height || height) / height;
    const lanes = grid.lanes;
    const pitch = lanes.length > 1 ? quantile(lanes.slice(1).map((lane, index) => lane.position - lanes[index].position), 0.5) : width / 4;
    const bands = [];
    lanes.forEach((lane, laneIndex) => {
      const left = Math.max(0, Math.round(lane.position - pitch * 0.3));
      const right = Math.min(width, Math.round(lane.position + pitch * 0.3) + 1);
      const profile = new Float32Array(height);
      for (let y = 0; y < height; y++) {
        let total = 0;
        for (let x = left; x < right; x++) total += signal[y * width + x];
        profile[y] = total / Math.max(1, right - left);
      }
      const background = smooth(profile, Math.max(8, Math.round(height / 12)));
      for (let y = 0; y < height; y++) profile[y] = Math.max(0, profile[y] - background[y]);
      const smoothed = smooth(profile, Math.max(1, Math.round(height / (options.mode === 'protein' ? 350 : 650))));
      peakCandidates(smoothed, Math.max(3, height / 100), 40).forEach(band => {
        const top=Math.max(0,band.position-band.width/2),bottom=Math.min(height,band.position+band.width/2);
        bands.push({lane_index:laneIndex,x:lane.position*scaleX,y:band.position*scaleY,height_px:band.width*scaleY,score:band.snr,
          bbox:{x:left*scaleX,y:top*scaleY,width:(right-left)*scaleX,height:Math.max(scaleY,(bottom-top)*scaleY)},bbox_kind:'localization_window'});
      });
    });
    const warnings = [];
    if (grid.warning) warnings.push(grid.warning);
    if (grid.inferred.length) warnings.push(`${grid.inferred.length} 个位置仅由间距推断，需检查空泳道或弱信号。`);
    if (!lanes.length) warnings.push('未找到稳定泳道信号；请检查模式，或手动添加泳道。');
    return {lane_centers: lanes.map(lane => lane.position * scaleX), bands, inferred_lanes: grid.inferred, polarity: bright ? 'bright' : 'dark', suggested_rotation: estimateTilt(signal, width, height), marker_points: [], warnings, requires_review: true, algorithm: 'projection-grid-v1', elapsed_ms: performance.now() - started, localization_width: width, localization_height: height, source_width: width * scaleX, source_height: height * scaleY};
  }
  function calibrate(points, targetY) {
    targetY = Number(targetY);
    if (!Number.isFinite(targetY)) throw new Error('估计位置必须是有限数值');
    const anchors = points.map(point => ({y: Number(point.y), size: Number(point.size ?? point.value)})).sort((a, b) => a.y - b.y);
    if (anchors.length < 2 || anchors.some(point => !Number.isFinite(point.y) || !Number.isFinite(point.size) || point.size <= 0)) throw new Error('至少需要两个有效 Marker 锚点');
    for (let index = 1; index < anchors.length; index++) if (anchors[index].y <= anchors[index - 1].y || anchors[index].size >= anchors[index - 1].size) throw new Error('Marker 必须随迁移距离增大而递减，且位置不能重复');
    const rightAnchor = anchors.findIndex(point => point.y >= targetY);
    const interval = rightAnchor === 0 ? 0 : rightAnchor < 0 ? anchors.length - 2 : rightAnchor - 1;
    const first = anchors[interval], second = anchors[interval + 1];
    const ratio = (targetY - first.y) / (second.y - first.y);
    const size = Math.exp(Math.log(first.size) + ratio * Math.log(second.size / first.size));
    if (!Number.isFinite(size) || size <= 0) throw new Error('该位置距离校准范围过远，无法稳定估计');
    return {size, extrapolated: ratio < 0 || ratio > 1, method: 'monotonic-log-linear', anchors: [first, second]};
  }
  function matchMarker(bandPositions, ladderSizes, sourceHeight) {
    if (!Number.isFinite(sourceHeight) || sourceHeight <= 0 || bandPositions.some(value => !Number.isFinite(value)) || ladderSizes.some(value => !Number.isFinite(value) || value <= 0)) throw new Error('Marker 候选数据无效');
    const positions = [...bandPositions].sort((a, b) => a - b);
    const sizes = [...ladderSizes].sort((a, b) => b - a);
    if (positions.length < 3 || sizes.length < 3) return null;
    const logs = sizes.map(Math.log), tolerance = Math.max(3, sourceHeight * 0.025);
    let best = null;
    const positionPairs = [[0, positions.length - 1], [0, Math.max(2, positions.length - 2)], [1, positions.length - 1]];
    for (const [firstBand, lastBand] of positionPairs) {
      for (let firstSize = 0; firstSize < Math.min(3, sizes.length - 2); firstSize++) {
        for (let lastSize = Math.max(firstSize + 2, sizes.length - 3); lastSize < sizes.length; lastSize++) {
          const slope = (positions[lastBand] - positions[firstBand]) / (logs[lastSize] - logs[firstSize]);
          const intercept = positions[firstBand] - slope * logs[firstSize];
          const rows = sizes.length + 1, cols = positions.length + 1;
          const cost = new Float64Array(rows * cols), previous = new Uint8Array(rows * cols);
          for (let row = 1; row < rows; row++) cost[row * cols] = row * 0.7;
          for (let column = 1; column < cols; column++) cost[column] = column * 0.9;
          for (let row = 1; row < rows; row++) for (let column = 1; column < cols; column++) {
            const residual = Math.abs(positions[column - 1] - (intercept + slope * logs[row - 1])) / tolerance;
            const alternatives = [cost[(row - 1) * cols + column - 1] + Math.min(4, residual), cost[(row - 1) * cols + column] + 0.7, cost[row * cols + column - 1] + 0.9];
            const operation = alternatives.indexOf(Math.min(...alternatives));
            cost[row * cols + column] = alternatives[operation]; previous[row * cols + column] = operation;
          }
          let row = sizes.length, column = positions.length;
          const matched = [];
          while (row && column) {
            const operation = previous[row * cols + column];
            if (operation === 0) {
              const residual = Math.abs(positions[column - 1] - (intercept + slope * logs[row - 1]));
              if (residual <= tolerance) matched.push({y: positions[column - 1], size: sizes[row - 1], residual_px: residual});
              row--; column--;
            } else if (operation === 1) row--; else column--;
          }
          if (matched.length < 3) continue;
          const coverage = matched.length / Math.max(positions.length, sizes.length);
          const residual = matched.reduce((sum, point) => sum + point.residual_px, 0) / matched.length;
          const score = coverage * Math.exp(-residual / tolerance);
          if (!best || score > best.score) best = {points: matched.reverse(), score, coverage, residual_px: residual};
        }
      }
    }
    return best;
  }
  scope.FigureGelEngine = {analyze, calibrate, matchMarker, smooth};
  if (typeof module !== 'undefined') module.exports = scope.FigureGelEngine;
})(globalThis);

