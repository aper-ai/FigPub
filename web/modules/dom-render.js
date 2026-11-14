/**
 * Replace rendered markup while keeping an unchanged focused form control attached.
 * Number inputs do not expose selectionStart: reconstructing one cannot restore its
 * native selection. Preserve its ancestor branch and refresh the sibling branches.
 */
export function renderMarkup(root,markup,focusedElement=null) {
  const next=root.cloneNode(false);next.innerHTML=markup;
  const replacement=focusedElement?.id?next.querySelector(`#${CSS.escape(focusedElement.id)}`):null;
  const sameControl=replacement&&focusedElement.matches('input:not([type=checkbox]):not([type=radio]):not([type=file]),textarea')&&sameControlBinding(focusedElement,replacement)&&replacement.value===focusedElement.value;
  if(sameControl) {
    const oldPath=ancestorPath(root,focusedElement),newPath=ancestorPath(next,replacement);
    if(oldPath.length===newPath.length&&oldPath.every((element,index)=>element.tagName===newPath[index].tagName&&element.id===newPath[index].id)) {
      for(let index=0;index<oldPath.length;index++) {
        const current=oldPath[index],incoming=newPath[index];
        syncAttributes(current,incoming);
        if(current===focusedElement)break;
        const retained=oldPath[index+1],incomingBranch=newPath[index+1];
        const siblings=[...incoming.childNodes],pivot=siblings.indexOf(incomingBranch);
        // Never detach the retained branch, including during a temporary reorder.
        // Snapshot live DOM collections before removing their entries.
        Array.from(current.childNodes).forEach(child=>{if(child!==retained)child.remove();});
        for(const child of siblings.slice(0,pivot))current.insertBefore(child,retained);
        for(const child of siblings.slice(pivot+1))current.append(child);
      }
      return true;
    }
  }
  root.replaceChildren(...next.childNodes);
  return false;
}

export function sameControlBinding(current,incoming) {
  if(current.tagName!==incoming.tagName||current.id!==incoming.id||current.type!==incoming.type)return false;
  const bindings=element=>Array.from(element.attributes).filter(attribute=>attribute.name.startsWith('data-')).map(attribute=>[attribute.name,attribute.value]).sort(([a],[b])=>a.localeCompare(b));
  return JSON.stringify(bindings(current))===JSON.stringify(bindings(incoming));
}

function ancestorPath(root,element) {
  const path=[];
  for(let current=element;current;current=current.parentElement){path.unshift(current);if(current===root)return path;}
  return [];
}

function syncAttributes(current,incoming) {
  Array.from(current.attributes).forEach(attribute=>{if(!incoming.hasAttribute(attribute.name))current.removeAttribute(attribute.name);});
  for(const attribute of incoming.attributes)if(current.getAttribute(attribute.name)!==attribute.value)current.setAttribute(attribute.name,attribute.value);
}
