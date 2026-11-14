/* Worker boundary: no image analysis or DOM blocking on the UI thread. */
'use strict';
importScripts('/gel-engine.js');
let cataloguePromise;
const catalogue = () => cataloguePromise ||= fetch('/data/ladders.json').then(response => response.json()).then(data => data.ladders);
self.onmessage = async ({data: message}) => {
  const {id, image, ...options} = message;
  try {
    if (options.action === 'calibrate') {
      self.postMessage({id, result: FigureGelEngine.calibrate(options.points, options.target_y)});
      return;
    }
    const result = FigureGelEngine.analyze(image, options);
    const ladders = await catalogue();
    const eligible = ladders.filter(ladder => options.ladder_id ? ladder.id === options.ladder_id : ladder.kind === (options.mode || 'protein'));
    const candidates = [];
    for (let laneIndex = 0; laneIndex < result.lane_centers.length; laneIndex++) {
      const positions = result.bands.filter(band => band.lane_index === laneIndex).map(band => band.y);
      if (positions.length < 3) continue;
      for (const ladder of eligible) {
        const match = FigureGelEngine.matchMarker(positions, ladder.sizes, result.source_height);
        if (match && match.score > 0.4) candidates.push({...match, lane_index: laneIndex, ladder_id: ladder.id, ladder_name: ladder.name, unit: ladder.kind === 'dna' ? 'bp' : 'kDa'});
      }
    }
    result.marker_candidates = candidates.sort((a, b) => b.score - a.score).slice(0, 5);
    // An identity guess never silently becomes confirmed scientific calibration.
    if (result.marker_candidates.length) result.warnings.push('Marker 候选需要确认型号与条带，不会自动写入已确认标定。');
    self.postMessage({id, result});
  } catch (error) {
    self.postMessage({id, error: error instanceof Error ? error.message : String(error)});
  }
};
