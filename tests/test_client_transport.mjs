import assert from 'node:assert/strict';
import test from 'node:test';
import {Window} from 'happy-dom';
import {request, exportFile, saveBlob, downloadOriginal, downloadMessage, ApiError} from '../web/modules/api.js';

test('field validation errors describe the input instead of exposing a JSON array', async t => {
  const detail = [{loc: ['body', 'dpi'], msg: 'Input should be a valid integer'}];
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({detail}), {
    status: 422, headers: {'Content-Type': 'application/json'},
  }));
  await assert.rejects(request('/api/export', {method: 'POST', body: {dpi: 'bad'}}), error => {
    assert.ok(error instanceof ApiError);
    assert.match(error.message, /请检查输入.*dpi：Input should be a valid integer/);
    assert.deepEqual(error.detail, detail);
    return true;
  });
});

test('disconnection is actionable while intentional cancellation retains AbortError', async t => {
  const mock = t.mock.method(globalThis, 'fetch', async () => {throw new TypeError('Failed to fetch');});
  await assert.rejects(request('/api/project'), error => error.status === 0 && /本机计算服务/.test(error.message));
  mock.mock.mockImplementation(async () => {throw new DOMException('Aborted', 'AbortError');});
  await assert.rejects(request('/api/project'), {name: 'AbortError'});
});

test('an HTML error page is not shown to users as an unexplained wall of markup', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('<html><body>Internal error</body></html>', {
    status: 500, headers: {'Content-Type': 'text/html'},
  }));
  await assert.rejects(exportFile({project: {title: 'Example'}, format: 'pdf'}), error => {
    assert.equal(error.status, 500);
    assert.match(error.message, /本机计算服务/);
    assert.doesNotMatch(error.message, /<html>/);
    return true;
  });
});

test('truncated JSON reports a transport failure without pretending a project was loaded', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('{"id":', {
    headers: {'Content-Type': 'application/json'},
  }));
  await assert.rejects(request('/api/project'), error => error instanceof ApiError && /不完整/.test(error.message));
});

function downloadEnvironment(t,{native=true}={}) {
  const window=new Window({url:'http://127.0.0.1:9123/?desktop=1'}),messages=[],clicks=[];
  if(native)window.chrome={webview:{postMessage:message=>messages.push(message)}};
  for(const [name,value] of Object.entries({window,document:window.document})){
    const previous=Object.getOwnPropertyDescriptor(globalThis,name);
    Object.defineProperty(globalThis,name,{value,writable:true,configurable:true});
    t.after(()=>{if(previous)Object.defineProperty(globalThis,name,previous);else delete globalThis[name];});
  }
  let sequence=0;
  t.mock.method(URL,'createObjectURL',()=>`blob:http://127.0.0.1:9123/request-${++sequence}`);
  const revoke=t.mock.method(URL,'revokeObjectURL',()=>{});
  t.mock.method(window.HTMLAnchorElement.prototype,'click',function(){clicks.push(this.href);});
  const remove=t.mock.method(window,'removeEventListener');
  t.mock.timers.enable({apis:['setTimeout']});
  const event=(url,status,message)=>window.dispatchEvent(new window.CustomEvent('figure-workbench:download',{detail:{url,status,message}}));
  return {window,messages,clicks,revoke,remove,event};
}

test('native save waits for the matching completed download and releases its one-use URL',async t=>{
  const environment=downloadEnvironment(t);
  let settled=false;
  const pending=saveBlob(new Blob(['original bytes']),'original.txt').then(value=>{settled=true;return value;});
  const url=environment.clicks[0];
  environment.event('blob:http://127.0.0.1:9123/stale-request','completed');
  environment.event(url,'in-progress');
  await Promise.resolve();assert.equal(settled,false);
  environment.event(url,'completed');
  const saved=await pending;
  assert.equal(saved.delivery,'saved');assert.match(downloadMessage(saved),/^已保存文件/);
  assert.equal(environment.revoke.mock.callCount(),1);
  assert.equal(environment.remove.mock.callCount(),1);
  environment.event(url,'error','late error must not change a completed save');
  assert.equal(environment.revoke.mock.callCount(),1);
});

test('native cancel and interrupted writes reject without reporting saved success',async t=>{
  const environment=downloadEnvironment(t);
  const canceled=saveBlob(new Blob(['bytes']),'canceled.txt');
  environment.event(environment.clicks[0],'canceled');
  await assert.rejects(canceled,{name:'AbortError'});
  const failed=saveBlob(new Blob(['bytes']),'failed.txt');
  environment.event(environment.clicks[1],'error','目标磁盘空间不足');
  await assert.rejects(failed,/磁盘空间不足/);
  assert.equal(environment.revoke.mock.callCount(),2);
  assert.equal(environment.remove.mock.callCount(),2);
});

test('native cancellation requests the owned operation and waits for its actual outcome',async t=>{
  const environment=downloadEnvironment(t),controller=new AbortController();
  let settled=false;
  const pending=saveBlob(new Blob(['bytes']),'cancel-request.txt',{signal:controller.signal});
  pending.then(()=>{settled=true;},()=>{settled=true;});
  controller.abort();
  assert.deepEqual(environment.messages,[{channel:'figure-workbench:download-cancel',url:environment.clicks[0]}]);
  await Promise.resolve();assert.equal(settled,false);
  environment.event(environment.clicks[0],'canceled');
  await assert.rejects(pending,{name:'AbortError'});
});

test('missing native results time out, cancel the operation and remove the listener',async t=>{
  const environment=downloadEnvironment(t);
  const pending=saveBlob(new Blob(['bytes']),'unknown-result.txt');
  t.mock.timers.tick(10*60*1000+1);
  await assert.rejects(pending,/尚未确认保存完成/);
  assert.equal(environment.messages.length,1);
  assert.equal(environment.revoke.mock.callCount(),1);
  assert.equal(environment.remove.mock.callCount(),1);
});

test('original downloads use unique identities so a prior result cannot settle a retry',async t=>{
  const environment=downloadEnvironment(t);
  const first=downloadOriginal('a'.repeat(64),'original.tif');
  environment.event(environment.clicks[0],'canceled');
  await assert.rejects(first,{name:'AbortError'});
  let settled=false;
  const second=downloadOriginal('a'.repeat(64),'original.tif').then(value=>{settled=true;return value;});
  assert.notEqual(environment.clicks[0],environment.clicks[1]);
  environment.event(environment.clicks[0],'completed');
  await Promise.resolve();assert.equal(settled,false);
  environment.event(environment.clicks[1],'completed');
  assert.equal((await second).delivery,'saved');
});

test('browser downloads report initiation without claiming a verified disk save',async t=>{
  const environment=downloadEnvironment(t,{native:false});
  const result=await saveBlob(new Blob(['bytes']),'browser.txt');
  assert.equal(result.delivery,'browser');assert.match(downloadMessage(result),/^已发起下载/);
  assert.equal(environment.revoke.mock.callCount(),0);
  t.mock.timers.tick(30001);
  assert.equal(environment.revoke.mock.callCount(),1);
  assert.deepEqual(environment.messages,[]);
});
