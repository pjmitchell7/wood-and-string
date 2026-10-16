import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { IndexedProcessingStore, ProcessingOutbox, transitionOperation, validateOperation } from './processing-outbox.mjs';

const copy=x=>x===undefined?undefined:JSON.parse(JSON.stringify(x));
const id=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const sha=async bytes=>createHash('sha256').update(bytes).digest('hex');
const bytes=new TextEncoder().encode('silent synthetic data, not an audio performance');
const source={sha256:await sha(bytes),size:bytes.byteLength,name:'original.wav',type:'audio/wav',track_id:id(99)};
const request={start_seconds:0,end_seconds:10,performers:1};
class MemoryStore {
  constructor(){this.rows=new Map();this.serial=Promise.resolve()}
  async get(key){await this.serial;const value=copy(this.rows.get(key));if(value)validateOperation(value);return value}
  async list(){await this.serial;return [...this.rows.values()].map(value=>copy(validateOperation(value)))}
  update(key,change){const work=this.serial.then(()=>{const next=transitionOperation(this.rows.get(key),change);assert.equal(next.operation_id,key);this.rows.set(key,next);return copy(next)});this.serial=work.catch(()=>{});return work}
}
function rig(store=new MemoryStore(),overrides={}){
  const calls=[],remote=new Map();let uploadLost=false,submitLost=false,cancelState='cancelled';
  const deps={store,hashBytes:sha,uuid:()=>id(1),now:()=>100,
    getBytes:async()=>bytes,
    upload:async value=>{calls.push(['upload',value.operation_id]);assert.equal(await sha(value.bytes),source.sha256);if(uploadLost){uploadLost=false;throw Error('Upload reply lost')}return {id:id(2),sha256:source.sha256,size:bytes.byteLength}},
    submit:async value=>{calls.push(['submit',value.operation_id,value.request]);const receipt=remote.get(value.operation_id)||{operation_id:value.operation_id,job_id:id(3),state:'queued',reason:null};remote.set(value.operation_id,receipt);if(submitLost){submitLost=false;throw Error('Submit reply lost')}return copy(receipt)},
    poll:async key=>{calls.push(['poll',key]);return copy(remote.get(key))},
    cancel:async key=>{calls.push(['cancel',key]);const receipt={...remote.get(key),state:cancelState};remote.set(key,receipt);return copy(receipt)},...overrides};
  return {store,calls,remote,deps,box:new ProcessingOutbox(deps),setUploadLost:()=>{uploadLost=true},setSubmitLost:()=>{submitLost=true},setCancelState:value=>{cancelState=value}};
}
let checks=0;
async function test(name,fn){await fn();checks++;console.log(`PASS ${name}`)}

await test('enqueue copies immutable source/settings and deduplicates one operation',async()=>{
  const r=rig(),s=copy(source),q={...request,guidance:{source_notes:'ringing'}};
  await r.box.enqueue(s,q);s.name='changed';q.guidance.source_notes='changed';
  const row=(await r.box.list())[0];assert.equal(row.source.name,source.name);assert.equal(row.request.guidance.source_notes,'ringing');
  await r.box.enqueue(source,{...request,guidance:{source_notes:'ringing'}});assert.equal((await r.box.list()).length,1);
  await assert.rejects(()=>r.box.enqueue(source,{...request,end_seconds:9}),/different/);assert.equal(r.calls.length,0);
});
await test('only supported mixed preview fields enter the outbox',async()=>{
  const r=rig();for(const q of [{...request,asset_id:id(2)},{...request,kind:'mixed'},{...request,instrument:'electric'},
    {...request,end_seconds:31},{...request,performers:3},{...request,start_seconds:NaN},{...request,bass_part:'bass-line'}])
    await assert.rejects(()=>r.box.enqueue(source,q));
  await r.box.enqueue(source,{...request,instrument:'jazz-bass',bass_part:'bass-line'});
});
await test('verified upload then idempotent submit saves immutable receipt',async()=>{
  const r=rig();await r.box.enqueue(source,request);await r.box.flush();const row=await r.store.get(id(1));
  assert.equal(row.status,'accepted');assert.deepEqual(row.receipt,{operation_id:id(1),job_id:id(3)});assert.equal(row.remote_may_exist,true);
  assert.equal(r.calls[1][2].asset_id,id(2));assert.equal((await r.box.list())[0].request.asset_id,undefined);
});
await test('lost upload reply restarts with same audio and does not lose local intent',async()=>{
  const r=rig();r.setUploadLost();await r.box.enqueue(source,request);await r.box.flush();
  assert.equal((await r.store.get(id(1))).status,'waiting');assert.equal((await r.store.get(id(1))).uploaded_asset_id,null);
  await new ProcessingOutbox(r.deps).flush();assert.equal(r.calls.filter(x=>x[0]==='upload').length,2);assert.equal(r.remote.size,1);
});
await test('lost submit reply resubmits original operation after restart without reupload',async()=>{
  const r=rig();r.setSubmitLost();await r.box.enqueue(source,request);await r.box.flush();
  const before=await r.store.get(id(1));assert.equal(before.status,'waiting');assert.equal(before.remote_may_exist,true);assert.equal(before.receipt,null);
  await new ProcessingOutbox(r.deps).flush();assert.equal(r.remote.size,1);assert.equal(r.calls.filter(x=>x[0]==='upload').length,1);
  assert.deepEqual(r.calls.filter(x=>x[0]==='submit').map(x=>x[1]),[id(1),id(1)]);
});
await test('uncertain submit cancellation persists then resubmits same ID before cancel',async()=>{
  const r=rig();r.setSubmitLost();await r.box.enqueue(source,request);await r.box.flush();await r.box.requestCancel(id(1));
  assert.equal((await r.store.get(id(1))).status,'cancel-requested');
  await new ProcessingOutbox(r.deps).flush();assert.deepEqual(r.calls.slice(-2).map(x=>x[0]),['submit','cancel']);
  const row=await r.store.get(id(1));assert.equal(row.status,'cancelled');assert.equal(row.cancel_requested,true);assert.equal(r.remote.size,1);
});
await test('unsent cancellation remains durable without uploading or deleting audio',async()=>{
  const r=rig();await r.box.enqueue(source,request);await r.box.requestCancel(id(1));await r.box.flush();
  assert.equal(r.calls.length,0);assert.equal((await r.box.list())[0].status,'cancelled');assert.equal((await r.box.list()).length,1);
});
await test('cancel during in-flight submission cannot be overwritten by late acceptance',async()=>{
  let resolveSubmit,started;const ready=new Promise(resolve=>{started=resolve});const r=rig();
  r.deps.submit=async value=>{r.remote.set(value.operation_id,{operation_id:value.operation_id,job_id:id(3),state:'queued',reason:null});started();return new Promise(resolve=>{resolveSubmit=resolve})};
  r.box=new ProcessingOutbox(r.deps);await r.box.enqueue(source,request);const flushing=r.box.flush();await ready;
  const second=new ProcessingOutbox(r.deps);await second.requestCancel(id(1));resolveSubmit(copy(r.remote.get(id(1))));await flushing;
  assert.equal((await r.store.get(id(1))).status,'cancelled');assert.equal((await r.store.get(id(1))).cancel_requested,true);
});
await test('running cancellation stays active until server terminal acknowledgement',async()=>{
  const r=rig();await r.box.enqueue(source,request);await r.box.flush();await r.box.requestCancel(id(1));r.setCancelState('cancelling');await r.box.flush();
  assert.equal((await r.store.get(id(1))).status,'cancel-requested');r.setCancelState('cancelled');await r.box.flush();assert.equal((await r.store.get(id(1))).status,'cancelled');
});
await test('completion winning cancellation is reported truthfully',async()=>{
  const r=rig();await r.box.enqueue(source,request);await r.box.flush();await r.box.requestCancel(id(1));
  r.remote.get(id(1)).state='complete';await r.box.flush();assert.equal((await r.store.get(id(1))).status,'complete');assert.equal(r.calls.some(x=>x[0]==='cancel'),false);
});
await test('semantic and auth errors stop automatic retries and explicit retry preserves operation',async()=>{
  for(const status of [400,401,403,404,409,415,507]){
    const r=rig();r.deps.submit=async()=>{throw Object.assign(Error('Needs review'),{status})};r.box=new ProcessingOutbox(r.deps);
    await r.box.enqueue(source,request);const fingerprint=(await r.store.get(id(1))).fingerprint;await r.box.flush();
    assert.equal((await r.store.get(id(1))).status,'attention');await r.box.flush();
    await r.box.retry(id(1));assert.equal((await r.store.get(id(1))).fingerprint,fingerprint);assert.equal((await r.store.get(id(1))).remote_may_exist,true);
  }
});
await test('network/server/rate-limit errors retain uncertainty and retry on next pass',async()=>{
  for(const status of [undefined,408,429,503]){
    const r=rig();r.deps.submit=async()=>{throw Object.assign(Error('Connection interrupted'),{status})};r.box=new ProcessingOutbox(r.deps);
    await r.box.enqueue(source,request);await r.box.flush();const row=await r.store.get(id(1));assert.equal(row.status,'waiting');assert.equal(row.remote_may_exist,true);
  }
});
await test('local missing/corrupt bytes stop before upload',async()=>{
  for(const getBytes of [async()=>new Uint8Array([1]),async()=>{throw Error('Missing cache')}]){
    const r=rig(undefined,{getBytes});await r.box.enqueue(source,request);await r.box.flush();assert.equal(r.calls.length,0);assert.equal((await r.store.get(id(1))).status,'attention');
  }
});
await test('wrong returned upload hash or size never reaches processing submit',async()=>{
  const r=rig(undefined,{upload:async()=>({id:id(2),sha256:'0'.repeat(64),size:bytes.length})});await r.box.enqueue(source,request);await r.box.flush();
  assert.equal((await r.store.get(id(1))).status,'attention');assert.equal(r.remote.size,0);
});
await test('changed receipt identity requires attention and retains original receipt',async()=>{
  const r=rig();await r.box.enqueue(source,request);await r.box.flush();r.remote.get(id(1)).job_id=id(4);await r.box.flush();
  const row=await r.store.get(id(1));assert.equal(row.status,'attention');assert.equal(row.receipt.job_id,id(3));
});
await test('concurrent flushes on one instance share the same sequential pass',async()=>{
  const r=rig();await r.box.enqueue(source,request);const a=r.box.flush(),b=r.box.flush();assert.equal(a,b);await a;
  assert.equal(r.calls.filter(x=>x[0]==='submit').length,1);
});
await test('a late stale poll cannot revive another tab cancellation',async()=>{
  const r=rig();await r.box.enqueue(source,request);await r.box.flush();let resolvePoll,started;
  const ready=new Promise(resolve=>{started=resolve});const a=new ProcessingOutbox({...r.deps,poll:async()=>{started();return new Promise(resolve=>{resolvePoll=resolve})}});
  const stale=a.flush();await ready;await r.box.requestCancel(id(1));await r.box.flush();assert.equal((await r.store.get(id(1))).status,'cancelled');
  resolvePoll({operation_id:id(1),job_id:id(3),state:'running',reason:null});await stale;assert.equal((await r.store.get(id(1))).status,'cancelled');
});
await test('cancel while reading local bytes prevents later upload/submit',async()=>{
  let resolveBytes,started;const ready=new Promise(resolve=>{started=resolve});const r=rig(undefined,{getBytes:async()=>{started();return new Promise(resolve=>{resolveBytes=resolve})}});
  await r.box.enqueue(source,request);const flushing=r.box.flush();await ready;await new ProcessingOutbox(r.deps).requestCancel(id(1));resolveBytes(bytes);await flushing;
  assert.equal(r.calls.length,0);assert.equal((await r.store.get(id(1))).status,'cancelled');
});
await test('fresh-state store transition rejects immutable corruption and clearing cancel intent',async()=>{
  const r=rig();await r.box.enqueue(source,request);await r.box.requestCancel(id(1));
  await assert.rejects(()=>r.store.update(id(1),row=>({...row,cancel_requested:false})),/undo/);
  await assert.rejects(()=>r.store.update(id(1),row=>({...row,source:{...row.source,name:'different'}})),/fingerprint/);
  await assert.rejects(()=>r.store.update(id(1),row=>({...row,status:'waiting'})),/revive/);
});
await test('corrupt accepted/remote state without a receipt fails closed',async()=>{
  const r=rig();await r.box.enqueue(source,request);
  await assert.rejects(()=>r.store.update(id(1),row=>({...row,status:'accepted'})),/receipt/);
  await assert.rejects(()=>r.store.update(id(1),row=>({...row,remote_state:'running'})),/receipt/);
});
await test('attention recovery preserves a pending cancellation',async()=>{
  const r=rig();r.setSubmitLost();await r.box.enqueue(source,request);await r.box.flush();await r.box.requestCancel(id(1));
  const retrying=new ProcessingOutbox({...r.deps,submit:async()=>{throw Object.assign(Error('Reconnect required'),{status:403})}});
  await retrying.flush();assert.equal((await r.store.get(id(1))).status,'attention');await r.box.retry(id(1));
  assert.equal((await r.store.get(id(1))).status,'cancel-requested');await r.box.flush();assert.equal((await r.store.get(id(1))).status,'cancelled');
});

// Small deterministic IndexedDB interface double: persistence/abort handling,
// not a claim about physical browser persistence or eviction behavior.
function fakeFactory(){
  const rows=new Map();let quota=false,closed=0;
  const db={objectStoreNames:{contains:()=>true},close(){closed++},transaction(){
    const tx={error:null,abort(){tx.aborted=true;queueMicrotask(()=>tx.onabort?.())}},pending=[];
    const run=value=>{const req={};queueMicrotask(()=>{req.result=copy(value());req.onsuccess?.();queueMicrotask(()=>{if(!tx.aborted){for(const row of pending)rows.set(row.operation_id,copy(row));tx.oncomplete?.()}})});return req};
    tx.objectStore=()=>({get:key=>run(()=>rows.get(key)),getAll:()=>run(()=>[...rows.values()]),put:row=>{if(quota)throw Error('Quota exceeded');pending.push(row)}});return tx;
  }};
  return {rows,setQuota:value=>{quota=value},get closed(){return closed},open(name,version){assert.equal(name,'wood-processing-v1');assert.equal(version,1);const req={result:db};queueMicrotask(()=>req.onsuccess?.());return req}};
}
await test('injected IndexedDB store persists across store instances and abort keeps prior row',async()=>{
  const factory=fakeFactory(),store=new IndexedProcessingStore(factory),r=rig(store);await r.box.enqueue(source,request);
  assert.equal((await new IndexedProcessingStore(factory).list()).length,1);factory.setQuota(true);
  await assert.rejects(()=>r.box.requestCancel(id(1)),/Quota/);factory.setQuota(false);assert.equal((await store.get(id(1))).status,'local');
});
await test('observer errors do not discard committed intent',async()=>{
  const r=rig(undefined,{onChange:async()=>{throw Error('Display failed')}});await r.box.enqueue(source,request);await r.box.flush();assert.equal((await r.store.get(id(1))).status,'accepted');
});
console.log(`PASS: ${checks} processing-outbox checks; no browser, audio, network or native execution.`);
