/** Keep one submitted task identifiable through delayed and failed requests. */
export const isActiveAnalysisJob = job => Boolean(job && ['queued', 'running'].includes(job.status));

export function createAnalysisTaskController({state, request, emit, notify, prepare, matchesInput, acceptResult,
  wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)), pollInterval = 500, maxStatusFailures = 3}) {
  let monitor = null;
  const current = id => state.job?.id === id && isActiveAnalysisJob(state.job);

  function watch(id, {immediate = false} = {}) {
    if (!current(id)) return Promise.resolve();
    if (monitor?.id === id) return monitor.promise;
    const token = {id, promise: null};
    monitor = token;
    state.job = {...state.job, polling: true, pollingError: null, statusFailures: 0};
    emit();
    token.promise = (async () => {
      let failures = 0, queryImmediately = immediate;
      try {
        while (current(id)) {
          if (!queryImmediately) await wait(pollInterval);
          queryImmediately = false;
          if (!current(id)) break;
          try {
            const update = await request(`/api/jobs/${id}`);
            if (!current(id)) break;
            if (update.status === 'done') {
              state.job = {...state.job, synchronizing: true};
              emit();
              // Keep the known task active until its result and project state
              // have been read. A failed result fetch can then be retried.
              await acceptResult(state.job, update);
              if (!current(id)) break;
            }
            failures = 0;
            state.job = {...state.job, ...update, pollingError: null, statusFailures: 0, synchronizing: false};
            if (update.status === 'error') notify(`分析未完成：${update.error || '请检查输入数据'}`, 'error',
              {code: 'analysis-task-error', context: state.job.requestContext});
            emit();
            if (!isActiveAnalysisJob(state.job)) break;
          } catch (error) {
            if (!current(id)) break;
            failures++;
            state.job = {...state.job, statusFailures: failures, synchronizing: false};
            if (failures >= maxStatusFailures) {
              state.job.pollingError = error.message || '无法读取本机任务状态';
              notify('暂时无法读取任务状态。可以重试查询或取消任务。', 'info',
                {code: 'analysis-status-unavailable', projectId: state.job.projectId, jobId: id});
              emit();
              break;
            }
            emit();
          }
        }
      } finally {
        if (monitor === token) monitor = null;
        if (state.job?.id === id) {
          state.job.polling = false;
          emit();
        }
      }
    })();
    return token.promise;
  }

  async function submit(context) {
    if (state.analysisSubmission || isActiveAnalysisJob(state.job)) return;
    const submission = {...context, phase: 'saving', startedAt: Date.now()};
    // This assignment must precede every await, including autosave. Neither
    // rapid repeated actions nor a slow POST may create a second job.
    state.analysisSubmission = submission;
    emit();
    try {
      await prepare();
      if (state.analysisSubmission !== submission || state.project.id !== context.projectId) return;
      if (!matchesInput(context)) {notify('分析输入已改变，请核对后重新运行', 'info'); return;}
      const sourceRevision = state.project.revision;
      submission.phase = 'submitting';
      emit();
      const job = await request('/api/jobs', {method: 'POST', body: {kind: context.kind, params: context.params}});
      if (state.analysisSubmission !== submission || state.project.id !== context.projectId) {
        await request(`/api/jobs/${job.id}`, {method: 'DELETE'});
        return;
      }
      state.job = {...job, kind: context.kind, sourceAssetId: context.sourceAssetId,
        projectId: context.projectId, startedAt: submission.startedAt,
        requestContext: {...context, sourceRevision}, pollingError: null};
      state.analysisSubmission = null;
      emit();
      await watch(job.id);
    } catch (error) {
      notify(`分析任务未能提交：${error.message}`, 'error', {code: 'analysis-submit-error', context: submission});
    } finally {
      // An old completion must never unlock a newer project's submission.
      if (state.analysisSubmission === submission) {state.analysisSubmission = null; emit();}
    }
  }

  async function cancel() {
    if (!isActiveAnalysisJob(state.job)) return;
    const id = state.job.id;
    const projectId = state.job.projectId;
    try {
      const update = await request(`/api/jobs/${id}`, {method: 'DELETE'});
      if (!current(id)) return;
      if (update.status === 'cancelled' || update.status === 'error') {
        state.job = {...state.job, ...update, polling: false, pollingError: null, synchronizing: false};
        notify(update.status === 'cancelled' ? '已取消分析任务' : `分析未完成：${update.error || '请检查输入数据'}`, update.status === 'cancelled' ? 'info' : 'error',
          update.status === 'error' ? {code: 'analysis-task-error', context: state.job.requestContext} : null);
        emit();
      } else {
        // Completion may win the cancellation race. Query the actual result
        // instead of claiming that a completed job was cancelled.
        if (update.status === 'done') notify('任务已完成，正在读取结果。', 'info');
        await watch(id, {immediate: true});
      }
    } catch (error) {notify(`取消任务未完成：${error.message}`, 'error', {code: 'analysis-cancel-error', projectId, jobId: id});}
  }

  return {submit, cancel, retry: () => state.job?.id ? watch(state.job.id, {immediate: true}) : Promise.resolve()};
}
