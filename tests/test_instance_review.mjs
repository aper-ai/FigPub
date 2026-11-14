import test from 'node:test';
import assert from 'node:assert/strict';
import {deflateSync} from 'node:zlib';
import {normalizeReviewDraft,appendReviewOperation,selectedInstanceAt,reviewSignature,decodeReviewMask} from '../web/modules/instance-review.js';

test('a new edit after undo truncates redo only and leaves input immutable',()=>{
  const draft={base_mask_sha256:'base',cursor:1,operations:[{id:'a',type:'delete',ids:[1]},{id:'b',type:'delete',ids:[2]}]};
  const next=appendReviewOperation(draft,{id:'c',type:'add',region:{type:'rectangle',x:0,y:0,width:1,height:1}});
  assert.equal(next.cursor,2);assert.deepEqual(next.operations.map(item=>item.id),['a','c']);assert.equal(draft.operations[1].id,'b');
});

test('draft identity, bounded history and unique operation IDs are required',()=>{
  assert.deepEqual(normalizeReviewDraft(null,'hash'),{base_mask_sha256:'hash',operations:[],cursor:0});
  assert.throws(()=>normalizeReviewDraft({base_mask_sha256:'other',operations:[],cursor:0},'hash'));
  assert.throws(()=>normalizeReviewDraft({base_mask_sha256:'hash',operations:[],cursor:1},'hash'));
  assert.throws(()=>normalizeReviewDraft({base_mask_sha256:'hash',operations:[{id:'a',type:'delete'},{id:'a',type:'delete'}],cursor:1},'hash'));
  assert.throws(()=>normalizeReviewDraft({base_mask_sha256:'hash',operations:[{id:'a',type:'replace-source'}],cursor:0},'hash'));
});

test('history signature changes for cursor and geometry, not just step count',()=>{
  const draft={base_mask_sha256:'a',operations:[{id:'p',type:'split',instance_id:1,seeds:[[1,1],[2,2]]}],cursor:1};
  assert.notEqual(reviewSignature(draft),reviewSignature({...draft,cursor:0}));
  const modified=structuredClone(draft);modified.operations[0].seeds[1][0]=3;
  assert.notEqual(reviewSignature(draft),reviewSignature(modified));
});

test('hit testing uses exact original pixels including ROI offset and holes',()=>{
  const mask={shape:[3,4],origin:[10,20]},labels=new Uint32Array([0,9,9,0,0,9,0,0,0,9,9,0]);
  assert.equal(selectedInstanceAt(labels,mask,{x:11.8,y:20.2}),9);
  assert.equal(selectedInstanceAt(labels,mask,{x:12.2,y:21.9}),0);
  assert.equal(selectedInstanceAt(labels,mask,{x:9.9,y:20}),0);
  assert.equal(selectedInstanceAt(labels,mask,{x:14,y:21}),0);
  assert.equal(selectedInstanceAt(labels,mask,{x:NaN,y:21}),0);
});

test('native label codec decompresses uint32 without contour reconstruction',async()=>{
  const expected=new Uint32Array([0,90001,90001,0,0,90001,0,0]);
  const encoded=deflateSync(new Uint8Array(expected.buffer)).toString('base64');
  const mask={encoding:'uint32-le-zlib-base64-v1',shape:[2,4],nbytes:32,data:encoded};
  assert.deepEqual(await decodeReviewMask(mask),expected);
});

test('client decoder rejects oversized geometry and expanded streams',async()=>{
  await assert.rejects(decodeReviewMask({encoding:'uint32-le-zlib-base64-v1',shape:[16000001,1],nbytes:64000004,data:''}));
  await assert.rejects(decodeReviewMask({encoding:'uint32-le-zlib-base64-v1',shape:[1,1],nbytes:4,data:deflateSync(Buffer.alloc(400)).toString('base64')}));
  await assert.rejects(decodeReviewMask({encoding:'uint32-le-zlib-base64-v1',shape:[1,2],nbytes:8,data:deflateSync(Buffer.alloc(4)).toString('base64')}));
});
