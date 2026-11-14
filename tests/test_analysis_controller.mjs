import test from 'node:test';
import assert from 'node:assert/strict';
import {createAnalysisTaskController} from '../web/modules/analysis-controller.js';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
async function until(predicate) {
  for (let attempt = 0; attempt < 100; attempt++) {if (predicate()) return; await Promise.resolve();}
  assert.ok(predicate(), 'Expected the controlled asynchronous operation to become observable');
}
function fixture(options = {}) {
  const state = {project: {id: 'project-a', revision: 7}, analysisSubmission: null, job: null};
  const requests = [], notices = [], accepted = [], transitions = [];
  const context = {kind: 'microscopy', projectId: 'project-a', sourceAssetId: 'raw-a',
    params: {method: 'intensity', asset_id: 'raw-a'}, inputSignature: 'known-input'};
  const controller = createAnalysisTaskController({state,
    request: async (path, request = {}) => {
      const call = {path, method: request.method || 'GET', body: request.body}; requests.push(call);
      if (options.request) return options.request(call, requests);
      if (call.method === 'POST') return {id: `job-${requests.filter(item => item.method === 'POST').length}`, status: 'queued'};
      if (call.method === 'DELETE') return {id: state.job.id, status: 'cancelled'};
      return {id: state.job.id, status: 'done', result: {mean: 2}};
    },
    prepare: options.prepare || (async () => {}), matchesInput: options.matchesInput || (() => true),
    acceptResult: async (job, update) => {if (options.acceptResult) await options.acceptResult(job, update); accepted.push({job, update});},
    emit: () => transitions.push({submission: state.analysisSubmission?.phase || null, id: state.job?.id, status: state.job?.status, pollingError: state.job?.pollingError}),
    notify: (message, tone) => notices.push({message, tone}), wait: async () => {},
  });
  return {state, controller, context, requests, notices, accepted, transitions,
    posts: () => requests.filter(item => item.method === 'POST'), queries: () => requests.filter(item => item.method === 'GET')};
}

test('autosave and POST latency share one submission lock; cancellation preserves task identity and permits a later run', async () => {
  const saved = deferred(), submitted = deferred(), query = deferred();
  let prepares = 0;
  const f = fixture({prepare: () => ++prepares === 1 ? saved.promise : Promise.resolve(), request: (call, calls) => {
    if (call.method === 'POST') return calls.filter(item => item.method === 'POST').length === 1 ? submitted.promise : {id: 'job-next', status: 'queued'};
    if (call.method === 'DELETE') return {id: 'job-first', status: 'cancelled'};
    return call.path.endsWith('job-first') ? query.promise : {id: 'job-next', status: 'done', result: {mean: 2}};
  }});
  const first = f.controller.submit(f.context);
  await f.controller.submit(f.context);
  assert.equal(prepares, 1); assert.equal(f.posts().length, 0); assert.equal(f.state.analysisSubmission.phase, 'saving');
  saved.resolve(); await until(() => f.posts().length === 1);
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 1); assert.equal(f.state.analysisSubmission.phase, 'submitting');
  submitted.resolve({id: 'job-first', status: 'queued'}); await until(() => f.queries().length === 1);
  assert.equal(f.state.analysisSubmission, null); assert.equal(f.state.job.id, 'job-first');
  assert.equal(f.state.job.requestContext.sourceRevision, 7);
  await f.controller.submit(f.context); assert.equal(f.posts().length, 1);
  await f.controller.cancel(); assert.equal(f.state.job.status, 'cancelled');
  query.resolve({id: 'job-first', status: 'running'}); await first;
  assert.equal(f.state.job.status, 'cancelled', 'A late status response cannot undo cancellation');
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 2); assert.equal(f.state.job.id, 'job-next'); assert.equal(f.state.job.status, 'done');
  assert.equal(f.accepted.length, 1);
});

test('failed autosave releases the submission lock without starting computation and supports a deliberate retry', async () => {
  let prepares = 0;
  const f = fixture({prepare: async () => {if (++prepares === 1) throw new Error('disk write failed');}});
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 0); assert.equal(f.state.analysisSubmission, null); assert.equal(f.state.job, null);
  assert.match(f.notices[0].message, /disk write failed/);
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 1); assert.equal(f.state.job.status, 'done');
});

test('a rejected POST releases its lock and is never automatically submitted again', async () => {
  let submissions = 0;
  const f = fixture({request: call => {
    if (call.method === 'POST') {if (++submissions === 1) throw new Error('service rejected request'); return {id: 'retry-job', status: 'queued'};}
    return {id: 'retry-job', status: 'done', result: {mean: 2}};
  }});
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 1); assert.equal(f.state.analysisSubmission, null); assert.equal(f.state.job, null);
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 2); assert.equal(f.state.job.id, 'retry-job'); assert.equal(f.state.job.status, 'done');
});

test('changed scientific input during autosave is not submitted under an obsolete signature', async () => {
  const saved = deferred(); let matches = true;
  const f = fixture({prepare: () => saved.promise, matchesInput: () => matches});
  const pending = f.controller.submit(f.context); matches = false; saved.resolve(); await pending;
  assert.equal(f.posts().length, 0); assert.equal(f.state.analysisSubmission, null); assert.equal(f.state.job, null);
  assert.match(f.notices[0].message, /输入已改变/);
});

test('an obsolete submission cancels its returned job without clearing a newer project submission', async () => {
  const firstPost = deferred(), secondPost = deferred();
  const f = fixture({request: call => {
    if (call.method === 'POST') return call.body.params.asset_id === 'raw-a' ? firstPost.promise : secondPost.promise;
    if (call.method === 'DELETE') return {id: 'old-job', status: 'cancelled'};
    return {id: 'new-job', status: 'done', result: {mean: 2}};
  }});
  const first = f.controller.submit(f.context); await until(() => f.posts().length === 1);
  f.state.project = {id: 'project-b', revision: 1}; f.state.analysisSubmission = null;
  const second = f.controller.submit({...f.context, projectId: 'project-b', sourceAssetId: 'raw-b', params: {method: 'intensity', asset_id: 'raw-b'}});
  await until(() => f.posts().length === 2);
  const currentSubmission = f.state.analysisSubmission;
  firstPost.resolve({id: 'old-job', status: 'queued'}); await first;
  assert.equal(f.state.analysisSubmission, currentSubmission);
  assert.ok(f.requests.some(call => call.path === '/api/jobs/old-job' && call.method === 'DELETE'));
  secondPost.resolve({id: 'new-job', status: 'queued'}); await second;
  assert.equal(f.state.job.id, 'new-job'); assert.equal(f.state.job.status, 'done');
});

test('temporary GET failures retry status only and preserve the same task until successful completion', async () => {
  let queries = 0;
  const f = fixture({request: call => {
    if (call.method === 'POST') return {id: 'known-job', status: 'queued'};
    if (++queries < 3) throw new Error('temporary connection error');
    return {id: 'known-job', status: 'done', result: {mean: 2}};
  }});
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 1); assert.equal(queries, 3); assert.equal(f.accepted.length, 1);
  assert.equal(f.state.job.id, 'known-job'); assert.equal(f.state.job.status, 'done'); assert.equal(f.state.job.pollingError, null);
  assert.ok(f.notices.every(item => item.tone !== 'error'));
});

test('bounded status failure keeps the job active, blocks duplicate computation, and allows one shared manual retry', async () => {
  let available = false; const resumed = deferred();
  const f = fixture({request: call => {
    if (call.method === 'POST') return {id: 'recoverable-job', status: 'running'};
    if (!available) throw new Error('temporarily unavailable');
    return resumed.promise;
  }});
  await f.controller.submit(f.context);
  assert.equal(f.queries().length, 3); assert.equal(f.state.job.id, 'recoverable-job');
  assert.equal(f.state.job.status, 'running'); assert.match(f.state.job.pollingError, /unavailable/); assert.equal(f.state.job.polling, false);
  await f.controller.submit(f.context); assert.equal(f.posts().length, 1);
  available = true;
  const first = f.controller.retry(), repeated = f.controller.retry();
  assert.equal(first, repeated); assert.equal(f.queries().length, 4);
  resumed.resolve({id: 'recoverable-job', status: 'done', result: {mean: 2}}); await first;
  assert.equal(f.posts().length, 1); assert.equal(f.accepted.length, 1); assert.equal(f.state.job.status, 'done'); assert.equal(f.state.job.pollingError, null);
});

test('project/result synchronization failures keep the known task recoverable without recomputing', async () => {
  let attempts = 0;
  const f = fixture({acceptResult: async () => {if (++attempts <= 3) throw new Error('project fetch failed');}});
  await f.controller.submit(f.context);
  assert.equal(attempts, 3); assert.equal(f.state.job.status, 'queued'); assert.match(f.state.job.pollingError, /project fetch failed/);
  assert.equal(f.accepted.length, 0); assert.equal(f.posts().length, 1);
  await f.controller.retry();
  assert.equal(f.posts().length, 1); assert.equal(f.accepted.length, 1); assert.equal(f.state.job.status, 'done');
});

test('cancel failure preserves the tracked job and a later confirmed cancellation permits a fresh analysis', async () => {
  let cancellations = 0, available = false;
  const f = fixture({request: (call, calls) => {
    if (call.method === 'POST') return {id: `cancel-test-${calls.filter(item => item.method === 'POST').length}`, status: 'running'};
    if (call.method === 'DELETE') {if (++cancellations === 1) throw new Error('cancel request failed'); return {id: 'cancel-test-1', status: 'cancelled'};}
    if (!available) throw new Error('status request failed');
    return {id: 'cancel-test-2', status: 'done', result: {mean: 2}};
  }});
  await f.controller.submit(f.context); await f.controller.cancel();
  assert.equal(f.state.job.id, 'cancel-test-1'); assert.equal(f.state.job.status, 'running');
  await f.controller.submit(f.context); assert.equal(f.posts().length, 1);
  await f.controller.cancel(); assert.equal(f.state.job.status, 'cancelled');
  available = true; await f.controller.submit(f.context);
  assert.equal(f.posts().length, 2); assert.equal(f.state.job.id, 'cancel-test-2'); assert.equal(f.state.job.status, 'done');
});

test('a genuine computation error is terminal and a later run can complete normally', async () => {
  let submissions = 0;
  const f = fixture({request: call => {
    if (call.method === 'POST') return {id: `computation-${++submissions}`, status: 'queued'};
    return submissions === 1 ? {id: 'computation-1', status: 'error', error: 'invalid specimen'} : {id: 'computation-2', status: 'done', result: {mean: 2}};
  }});
  await f.controller.submit(f.context);
  assert.equal(f.state.job.status, 'error'); assert.match(f.notices.at(-1).message, /invalid specimen/);
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 2); assert.equal(f.state.job.status, 'done'); assert.equal(f.accepted.length, 1);
});

test('a successful completed task does not block a later deliberate analysis and is not polled again', async () => {
  const f = fixture();
  await f.controller.submit(f.context);
  assert.equal(f.state.job.id, 'job-1'); assert.equal(f.state.job.status, 'done');
  const queries = f.queries().length;
  await f.controller.retry(); assert.equal(f.queries().length, queries);
  await f.controller.submit(f.context);
  assert.equal(f.posts().length, 2); assert.equal(f.state.job.id, 'job-2'); assert.equal(f.state.job.status, 'done');
  assert.equal(f.accepted.length, 2);
});

test('a completion that wins a cancellation race is loaded rather than falsely reported as cancelled', async () => {
  const query = deferred();
  const f = fixture({request: call => {
    if (call.method === 'POST') return {id: 'completed-first', status: 'running'};
    if (call.method === 'DELETE') return {id: 'completed-first', status: 'done', result: {mean: 2}};
    return query.promise;
  }});
  const running = f.controller.submit(f.context); await until(() => f.queries().length === 1);
  const cancelling = f.controller.cancel(); await until(() => f.notices.some(item => /任务已完成/.test(item.message)));
  query.resolve({id: 'completed-first', status: 'done', result: {mean: 2}});
  await Promise.all([running, cancelling]);
  assert.equal(f.state.job.status, 'done'); assert.equal(f.accepted.length, 1);
  assert.ok(f.notices.every(item => !/已取消/.test(item.message)));
});
