import {state,emit} from './state.js';
import {markLayoutNumberEdited} from './numeric-input.js';

let commitTimer=null,pointerTimer=null;
const editableSelector='input:not([type=file]):not([type=checkbox]):not([type=radio]),textarea';
/** Keep a native input node alive until its user edit has produced change/blur. */
export function markInputDraft(element) {
  if(element.matches(editableSelector)){
    element.dataset.pendingEdit='true';
    markLayoutNumberEdited(element);
  }
}
export function draftBlocksRender(activeElement) {
  return Boolean(state.formCommitPending||state.formPointerPending||activeElement?.matches('[data-pending-edit]'));
}
function flushDeferredRender() {
  if(state.renderDeferred&&!state.formCommitPending&&!state.formPointerPending)emit();
}
export function beginInputCommit(element) {
  const wasDraft=element.dataset.pendingEdit;delete element.dataset.pendingEdit;
  if(!wasDraft)return;
  state.formCommitPending=true;clearTimeout(commitTimer);
  // Native Tab/focus/click must finish before the focused DOM subtree is replaced.
  commitTimer=setTimeout(()=>{commitTimer=null;state.formCommitPending=false;flushDeferredRender();},0);
}
export function holdPointerDraft(event) {
  // A redraw between pointerdown and click can replace the target button even
  // when no text draft exists. Finish the native pointer transaction first.
  if(event.target.closest?.('#app'))state.formPointerPending=true;
}
export function releasePointerDraft() {
  clearTimeout(pointerTimer);pointerTimer=null;state.formPointerPending=false;flushDeferredRender();
}
export function releasePointerDraftSoon() {
  if(!state.formPointerPending)return;
  clearTimeout(pointerTimer);pointerTimer=setTimeout(releasePointerDraft,0);
}
export function discardInputDraft(element) {
  if(element?.matches('[data-pending-edit]')){
    // Removing a focused native input can itself dispatch change in Chromium.
    element.dataset.discardedEdit='true';delete element.dataset.pendingEdit;
    delete element.dataset.layoutEdited;
    element.value=element.defaultValue;state.discardInputDraft=true;
  }
}
