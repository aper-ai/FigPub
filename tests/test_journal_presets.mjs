import test from 'node:test';
import assert from 'node:assert/strict';
import {JOURNAL_PRESETS, journalPresetById, journalPreflight} from '../web/modules/journal-presets.js';

function project(presetId, width = 89, height = 120) {
  return {
    figure: {width_mm: width, height_mm: height},
    settings: {composition: {journal_preset_id: presetId}},
    panels: [], annotations: [],
  };
}

test('catalogue separates verified journal data, custom choices, and generic suggestions', () => {
  assert.equal(new Set(JOURNAL_PRESETS.map(item => item.id)).size, JOURNAL_PRESETS.length);
  for (const preset of JOURNAL_PRESETS) {
    assert.equal(preset.height_mm, null);
    assert.equal(preset.checked_at, '2026-11-12');
    assert.ok(Array.isArray(preset.notes));
    if (preset.verification_status === 'verified') {
      assert.match(preset.source_url, /^https:\/\//);
      assert.ok(preset.verified_fields.length);
    }
  }
  assert.equal(journalPresetById('nature-single').width_mm, 89);
  assert.equal(journalPresetById('nature-double').width_mm, 183);
  assert.equal(journalPresetById('nature-double').max_height_mm, 170);
  assert.equal(journalPresetById('nature-single').min_ppi, null);
  assert.equal(journalPresetById('nature-single').resolution_by_type.photograph.min_ppi, 300);
  assert.equal(journalPresetById('elife-standard').width_semantics, 'minimum');
  assert.equal(journalPresetById('elife-standard').min_ppi, 300);
  assert.equal(journalPresetById('cell-custom').width_mm, null);
  assert.equal(journalPresetById('cell-single').width_mm, 85);
  assert.equal(journalPresetById('cell-one-half').width_mm, 114);
  assert.equal(journalPresetById('cell-double').width_mm, 174);
  assert.equal(journalPresetById('cell-double').verification_status, 'archived');
  assert.equal(journalPresetById('cell-double').source_date, '2025-09-11');
  assert.equal(journalPresetById('cell-double').resolution_by_type.line_art.min_ppi, 1000);
  assert.deepEqual(journalPresetById('cell-custom').verified_fields, []);
  assert.equal(journalPresetById('generic-85').category, 'generic');
  assert.equal(journalPresetById('not-present'), null);
  assert.equal(journalPresetById('__proto__'), null);
});

test('catalogue records cannot be changed by applying a preset to one project', () => {
  const preset = journalPresetById('nature-single');
  assert.throws(() => { preset.width_mm = 999; }, TypeError);
  assert.throws(() => { preset.notes.push('changed'); }, TypeError);
  assert.throws(() => { preset.body_font_size_pt.min = 1; }, TypeError);
  const copy = structuredClone(preset);
  copy.notes.push('local note');
  assert.notEqual(copy.notes.length, preset.notes.length);
});

test('Nature figure height uses 170 mm and width changes remain informational', () => {
  assert.deepEqual(journalPreflight(project('nature-single', 89, 170)), []);
  const oversized = journalPreflight(project('nature-single', 89, 247));
  assert.deepEqual(oversized.map(issue => issue.code), ['JOURNAL_MAX_HEIGHT']);
  assert.equal(oversized[0].severity, 'warning');
  const resized = journalPreflight(project('nature-single', 120, 120));
  assert.equal(resized[0].code, 'JOURNAL_PRESET_WIDTH_CHANGED');
  assert.equal(resized[0].severity, 'info');
  assert.deepEqual(journalPreflight(project('nature-single', 89.02, 170.01)), []);
});

test('eLife upload sizes are minimum widths, never mandatory exact widths', () => {
  assert.equal(journalPreflight(project('elife-standard', 99))[0].code, 'JOURNAL_MIN_WIDTH');
  assert.deepEqual(journalPreflight(project('elife-standard', 100, 300)), []);
  assert.deepEqual(journalPreflight(project('elife-standard', 180, 300)), []);
  assert.equal(journalPreflight(project('elife-full', 183))[0].code, 'JOURNAL_MIN_WIDTH');
  assert.deepEqual(journalPreflight(project('elife-full', 220, 300)), []);
});

test('label and body typography are checked separately without rewriting manual content', () => {
  const value = project('nature-single');
  value.panels = [{id: 'p1', label: 'a', label_mode: 'auto', label_font_size_pt: 8},
    {id: 'p2', label: 'B', label_mode: 'manual', label_font_size_pt: 10}];
  value.annotations = [{type: 'text', text: 'Treatment', font_size_pt: 8},
    {type: 'text', text: 'Control', font_size_pt: 7},
    {type: 'text', text: '', font_size_pt: 50},
    {type: 'rect', font_size_pt: 50}];
  const original = structuredClone(value);
  const issues = journalPreflight(value);
  assert.deepEqual(issues.map(item => item.code), ['JOURNAL_PANEL_LABEL_STYLE', 'JOURNAL_PANEL_LABEL_SIZE', 'JOURNAL_TEXT_SIZE']);
  assert.equal(issues[0].panel_id, 'p2');
  assert.deepEqual(value, original);
});

test('unverified, unknown, generic and absent selections never invent journal rules', () => {
  assert.equal(journalPreflight(project('cell-custom', 85, 999))[0].code, 'JOURNAL_RULES_UNVERIFIED');
  assert.equal(journalPreflight(project('untrusted'))[0].code, 'JOURNAL_PRESET_UNKNOWN');
  assert.deepEqual(journalPreflight(project('generic-85', 17, 999)), []);
  assert.deepEqual(journalPreflight({}), []);
  assert.deepEqual(journalPreflight(null), []);
  const value = project('elife-standard', 100);
  value.settings.composition.journal_preset_snapshot = {min_width_mm: 1000, max_height_mm: 1};
  assert.deepEqual(journalPreflight(value), []);
});

test('archived official rules remain usable presets without pretending to be current', () => {
  const issues = journalPreflight(project('cell-double', 190, 300));
  assert.deepEqual(issues.map(item => item.code), ['JOURNAL_RULES_ARCHIVED']);
  assert.match(issues[0].message, /2025-09-11/);
  assert.equal(issues[0].severity, 'warning');
});

test('incomplete imported geometry is left to the general project validator', () => {
  const value = project('nature-single');
  value.figure = {width_mm: null, height_mm: NaN};
  value.panels = [null, {label: 'a', label_font_size_pt: 'not a size'}];
  value.annotations = [null];
  assert.deepEqual(journalPreflight(value), []);
});
