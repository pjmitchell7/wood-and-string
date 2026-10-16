const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/pair.js','utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function harness(replies){
  let now=100000,next=0;const storage=new Map([['guitar-pairing',JSON.stringify({id:'id',secret:'secret',code:'123456',expires:110})]]),timers=new Map(),listeners={},calls=[];
  const button={disabled:false},form={querySelector:()=>button},status={},code={},device={value:'Test device'};
  const context={document:{getElementById:id=>({pairForm:form,pairStatus:status,pairCode:code,deviceName:device}[id])},sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},window:{addEventListener:(name,callback)=>listeners[name]=callback},location:{replace:value=>{context.redirect=value}},Date:{now:()=>now},setTimeout:(callback,delay)=>{const id=++next;timers.set(id,{callback,delay});return id},clearTimeout:id=>timers.delete(id),fetch:async(path,options)=>{calls.push(path);const response=replies.shift();if(response instanceof Error)throw response;return response}};
  vm.createContext(context);vm.runInContext(source,context);return {storage,timers,listeners,calls,button,form,status,code,context,advance:value=>now=value};
}
const reply=(status,data)=>({status,ok:status>=200&&status<300,json:async()=>data});
(async()=>{
  const denied=harness([reply(403)]);await flush();assert.equal(denied.storage.size,0);assert.equal(denied.timers.size,0);assert.equal(denied.button.disabled,false);assert.equal(denied.code.textContent,'');assert.match(denied.status.textContent,/denied/);
  const expired=harness([reply(410)]);await flush();assert.equal(expired.button.disabled,false);assert.equal(expired.storage.size,0);
  const temporary=harness([reply(503)]);await flush();assert.equal(temporary.timers.size,1);assert.equal(temporary.button.disabled,true);assert.equal(temporary.storage.size,1);temporary.advance(111000);const timer=[...temporary.timers.values()][0];await timer.callback();assert.equal(temporary.storage.size,0);assert.equal(temporary.timers.size,0);assert.equal(temporary.button.disabled,false);assert.equal(temporary.calls.length,1,'expired request does not make another network call');
  const disconnected=harness([new Error('offline')]);await flush();assert.equal(disconnected.timers.size,1);disconnected.listeners.pagehide();assert.equal(disconnected.timers.size,0);
  const paired=harness([reply(200,{state:'paired'})]);await flush();assert.equal(paired.storage.size,0);assert.equal(paired.timers.size,0);assert.equal(paired.context.redirect,'/');
  const lost=harness([new Error('committed reply was lost'),reply(200,{state:'paired'})]);await flush();const saved=lost.storage.get('guitar-pairing');await [...lost.timers.values()][0].callback();assert.equal(JSON.parse(saved).id,'id');assert.equal(lost.context.redirect,'/');assert.equal(lost.storage.size,0);assert.equal(lost.timers.size,0);
  const suspended=harness([reply(200,{state:'pending'}),reply(200,{state:'pending'})]);await flush();suspended.listeners.pagehide();assert.equal(suspended.timers.size,0);suspended.listeners.pageshow();await flush();suspended.listeners.pageshow();assert.equal(suspended.timers.size,1,'repeated visible events do not duplicate retry timers');assert.equal(suspended.calls.length,2);
  const retry=harness([reply(403),reply(200,{id:'new',secret:'new-secret',code:'654321',expires:120}),reply(200,{state:'pending'})]);await flush();await retry.form.onsubmit({preventDefault(){}});assert.deepEqual(retry.calls,['/pair/claim','/pair/request','/pair/claim']);assert.equal(retry.button.disabled,true);assert.equal(retry.timers.size,1);assert.equal(JSON.parse(retry.storage.get('guitar-pairing')).id,'new');
  console.log('PASS: terminal denial clears secret/timer and enables new request; transient failures expire; success clears; new request works');
})().catch(error=>{console.error(error);process.exit(1)});
