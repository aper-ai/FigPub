import test from 'node:test';
import assert from 'node:assert/strict';
import {Window} from 'happy-dom';
import {numberField,readNumericInput,markLayoutNumberEdited} from '../web/browser/forms.js';
import {newProject, panelForAsset, hasWorkspaceContent} from '../web/browser/model.js';
import {applyPanelLayout} from '../web/modules/layout-engine.js';

test('fractional journal layouts remain editable and retain exact unchanged geometry', () => {
  const project = newProject();
  project.figure.width_mm = 89;
  const asset = {id: 'synthetic', kind: 'image', width_px: 1200, height_px: 800};
  project.assets.push(asset);
  project.panels = Array.from({length: 6}, () => panelForAsset(asset));
  applyPanelLayout(project, {columns: 3, rows: 2});
  const window = new Window();
  const form = window.document.createElement('form');
  const panel = project.panels[0];
  form.innerHTML = ['x_mm', 'y_mm', 'width_mm', 'height_mm'].map(name =>
    numberField(name, name, panel[name], name.includes('width') || name.includes('height') ? 'min=".1" max="2000"' : '')).join('');
  assert.equal(form.checkValidity(), true);
  for (const input of form.elements) {
    assert.equal(readNumericInput(input), panel[input.name]);
    assert.ok(input.value.length <= 8, 'layout coordinates have a concise display');
  }
  const width = form.elements.namedItem('width_mm');
  width.value = '-1';
  assert.equal(form.checkValidity(), false, 'allowing decimals must retain physical dimension bounds');
});

test('an explicit edit to a rounded display value commits that number, while focus and blur preserve precision', () => {
  const window = new Window(), document = window.document;
  document.body.innerHTML = `<form>${numberField('X / mm', 'x_mm', 45.89866666666667)}<button>应用</button></form>`;
  const input = document.querySelector('input');
  document.addEventListener('input', event => markLayoutNumberEdited(event.target));
  assert.equal(input.value, '45.899');
  input.focus(); input.blur();
  assert.equal(readNumericInput(input), 45.89866666666667);
  input.focus(); input.value = '45.899';
  input.dispatchEvent(new window.InputEvent('input', {bubbles: true, inputType: 'insertText', data: '45.899'}));
  input.blur();
  assert.equal(readNumericInput(input), 45.899, 'a real user edit has priority over the stored precision');
});

test('original image crop coordinates do not receive layout display rounding', () => {
  const window = new Window(), document = window.document;
  document.body.innerHTML = numberField('原图 X / px', 'crop_x', 12.123456789);
  const input = document.querySelector('input');
  assert.equal(input.value, '12.123456789');
  assert.equal(input.dataset.layoutValue, undefined);
  assert.equal(readNumericInput(input), 12.123456789);
});

test('annotations, retained measurements and edited empty canvases are user work', () => {
  assert.equal(hasWorkspaceContent(newProject()), false);
  for (const key of ['assets', 'panels', 'annotations', 'measurements']) {
    const project = newProject();
    project[key].push({id: 'user-content'});
    assert.equal(hasWorkspaceContent(project), true, key);
  }
  const edited = newProject();
  edited.revision = 1;
  assert.equal(hasWorkspaceContent(edited), true);
});
