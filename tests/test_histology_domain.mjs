import test from 'node:test';
import assert from 'node:assert/strict';
import {advancedDefaults,buildAdvancedParams} from '../web/modules/histology-domain.js';
const channels=[{id:'c0',name:'Structure'},{id:'c1',name:'Labels'},{id:'c2',name:'Nuclei'}];
const fiber={...advancedDefaults,method:'fiber_cross_section',segmentation:'labels',fiber_channel_id:'c0',label_channel_id:'c1'};
test('fiber maps dedicated fields without inheriting WB polarity',()=>{
  const result=buildAdvancedParams({...fiber,segmentation:'threshold',polarity:'dark',fiber_polarity:'bright',fiber_min_area:50}, {},channels);
  assert.equal(result.polarity,'bright');assert.equal(result.min_area,50);
});
test('label identity cannot be the structural intensity channel',()=>assert.throws(()=>buildAdvancedParams({...fiber,label_channel_id:'c0'}, {},channels),/不同/));
test('nuclear label preference is explicit',()=>{
  const result=buildAdvancedParams({...fiber,nucleus_channel_id:'c1',nucleus_label_channel_id:'c2'}, {},channels);
  assert.equal(result.nucleus_label_channel_id,'c2');assert.equal(result.nucleus_channel_id,undefined);
});
test('empty or reversed grade thresholds are not coerced to zero',()=>{
  assert.throws(()=>buildAdvancedParams({...advancedDefaults,method:'ihc_advanced',grade_threshold_1:''}),/请填写/);
  assert.throws(()=>buildAdvancedParams({...advancedDefaults,method:'ihc_advanced',grade_threshold_1:.7}),/递增/);
});
test('IHC examples are source geometry and independent of analysis ROI',()=>{
  const negative={x:1,y:2,width:10,height:12},positive={x:20,y:2,width:10,height:12};
  const result=buildAdvancedParams({...advancedDefaults,method:'ihc_advanced',grading_method:'examples'}, {ihc_negative:negative,ihc_positive:positive});
  assert.deepEqual(result.example_rois,{negative,positive});assert.notEqual(result.example_rois.negative,negative);
});
test('GMM does not transmit artificial manual grade values',()=>{
  const result=buildAdvancedParams({...advancedDefaults,method:'ihc_advanced',grading_method:'gmm'});
  assert.equal(result.grade_thresholds,undefined);
});
test('locked profile replaces per-image estimation options',()=>{
  const profile={version:1,measurement_basis:'natural_log_OD',grade_thresholds:[.2,.5,.8]};
  const result=buildAdvancedParams({...advancedDefaults,method:'ihc_advanced',locked_profile:profile,stain_method:'bad'});
  assert.deepEqual(result.locked_profile,profile);assert.equal(result.stain_method,undefined);
});
test('unknown marker channels and duplicate names fail',()=>{
  assert.throws(()=>buildAdvancedParams({...fiber,fiber_markers:[{channel_id:'missing',name:'A',threshold:2}]},{},channels),/分型/);
  assert.throws(()=>buildAdvancedParams({...fiber,fiber_markers:[{channel_id:'c0',name:'A',threshold:2},{channel_id:'c2',name:'A',threshold:2}]},{},channels),/重复/);
});
test('advanced result renderer accepts microscopy before the first analysis',async()=>{
  const {renderAdvancedResults}=await import('../web/modules/histology-fibers.js');
  assert.equal(renderAdvancedResults(null),'');
  assert.equal(renderAdvancedResults(undefined),'');
  assert.equal(renderAdvancedResults({kind:'microscopy',method:'threshold'}),'');
});
