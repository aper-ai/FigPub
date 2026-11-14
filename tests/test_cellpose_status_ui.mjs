import test from 'node:test';
import assert from 'node:assert/strict';
import {Window} from 'happy-dom';
import {cellposeStatusView,renderCellposeStatus} from '../web/modules/cellpose-status.js';

const status = {
  available: true, probe_required: false,
  runtime: {available: true, status: 'ready', cellpose_version: '3.1.1.3'},
  models: [{id: 'cyto3', available: true, status: 'ready', sha256: 'a'.repeat(64)},
    {id: 'nuclei', available: false, status: 'missing', sha256: 'b'.repeat(64)}],
};

test('startup health with unprobed Cellpose is pending, not a failed model', () => {
  const unprobed = {...status, available: false, probe_required: true,
    runtime: {available: false, status: 'not_probed'},
    models: status.models.map(model => ({...model, available: false}))};
  assert.equal(cellposeStatusView(null, 'cyto3').state, 'pending');
  assert.equal(cellposeStatusView(unprobed, 'cyto3').state, 'pending');
  assert.equal(cellposeStatusView({...unprobed, probe_required: false}, 'cyto3').state, 'pending');
  assert.equal(cellposeStatusView(unprobed, 'cyto3', true).state, 'checking');
  assert.equal(cellposeStatusView(status, 'cyto3').state, 'available');
});

test('selected model readiness does not inherit availability from another model', () => {
  assert.equal(cellposeStatusView(status, 'nuclei').state, 'unavailable');
  assert.equal(cellposeStatusView({...status, runtime: {available: false, status: 'runtime_incompatible'}}, 'cyto3').state, 'unavailable');
  for (const failure of ['missing','hash_mismatch','unreadable']) {
    const broken={...status, models: [{id:'cyto3',available:false,status:failure}]};
    assert.equal(cellposeStatusView(broken, 'cyto3').state, 'unavailable');
  }
});

test('model status keeps checking controls disabled and diagnostics safely escaped', () => {
  const window = new Window(), document = window.document;
  document.body.innerHTML = renderCellposeStatus(status, 'cyto3', true);
  assert.equal(document.querySelector('button').disabled, true);
  assert.equal(document.querySelector('[role="status"]').getAttribute('aria-live'), 'polite');
  document.body.innerHTML = renderCellposeStatus({...status, runtime: {available: false, reason: '<script>broken</script>'}}, 'cyto3');
  assert.equal(document.querySelector('script'), null);
  assert.ok(document.querySelector('.model-diagnostics').textContent.includes('<script>broken</script>'));
  assert.equal(document.querySelector('button').disabled, false);
});
