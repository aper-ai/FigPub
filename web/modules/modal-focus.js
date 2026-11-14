/** Restore dialog focus only to the same control in the same workspace. */
const controls='button,a[href],input,textarea,select,summary,[tabindex],[contenteditable]';

function workspaceIdentity(state) {
  return JSON.stringify([state.project?.id,state.tab,state.tab==='analysis'?state.analysisKind:null,
    state.selectedAssetId,state.selectedPanelId,state.selectedAnnotationId]);
}

function binding(element) {
  return JSON.stringify([element.tagName,element.id,element.getAttribute('type'),
    element.getAttribute('role'),element.getAttribute('href'),element.getAttribute('name'),
    element.getAttribute('aria-label'),[...element.classList].sort(),
    [...element.attributes].filter(attribute=>attribute.name.startsWith('data-'))
      .map(attribute=>[attribute.name,attribute.value]).sort(([a],[b])=>a.localeCompare(b))]);
}

function location(element) {
  const path=[];
  for(let parent=element.parentElement;parent&&!parent.classList.contains('app-shell');parent=parent.parentElement){
    path.push([parent.tagName,parent.id,[...parent.classList].sort()]);
  }
  return JSON.stringify(path);
}

function capture(state) {
  const element=document.activeElement;
  if(!element?.matches(controls)||!element.closest('.app-shell'))return null;
  const record={workspace:workspaceIdentity(state),binding:binding(element),location:location(element)};
  const matches=[...document.querySelectorAll(`.app-shell ${controls.split(',').join(',.app-shell ')}`)]
    .filter(candidate=>binding(candidate)===record.binding&&location(candidate)===record.location);
  // An ambiguous selector is not a safe replacement for a removed control.
  return matches.length===1?record:null;
}

function canFocus(element) {
  if(element.matches(':disabled')||element.closest('[hidden],[inert]'))return false;
  for(let parent=element;parent&&parent!==document.body;parent=parent.parentElement){
    const style=getComputedStyle(parent);
    if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return false;
  }
  return true;
}

export function createModalFocusLifecycle() {
  let modalOpen=false,returnFocus=null,pending=false;
  return {
    beforeRender(state) {
      if(returnFocus&&returnFocus.workspace!==workspaceIdentity(state)){returnFocus=null;pending=false;}
      if(state.modal&&!modalOpen){returnFocus=capture(state);pending=false;}
      if(!state.modal&&modalOpen)pending=Boolean(returnFocus);
      modalOpen=Boolean(state.modal);
    },
    afterRender(state) {
      if(!pending||state.modal||state.busy)return;
      const record=returnFocus;returnFocus=null;pending=false;
      if(!record||record.workspace!==workspaceIdentity(state))return;
      const active=document.activeElement;
      // A completed action may intentionally focus a result or another control.
      if(active?.isConnected&&active!==document.body&&active!==document.documentElement&&!active.closest('.modal'))return;
      const matches=[...document.querySelectorAll(`.app-shell ${controls.split(',').join(',.app-shell ')}`)]
        .filter(candidate=>binding(candidate)===record.binding&&location(candidate)===record.location);
      if(matches.length===1&&canFocus(matches[0]))matches[0].focus({preventScroll:true});
    },
  };
}
