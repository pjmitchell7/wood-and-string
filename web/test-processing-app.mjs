import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { mixedPreviewRequest } from './mixed-preview.mjs';

// Execute current app handlers with only browser/storage/transport boundaries
// replaced. No AudioContext, browser, HTTP request, native process or audio.
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
function between(start,end){const a=app.indexOf(start),b=app.indexOf(end,a);assert.ok(a>=0&&b>a,`Source markers: ${start}`);return app.slice(a,b)}
const enqueueSource=between("$('mixedQueue').onclick=",'function selectExtraction(');
const uploadSource=between('async function processingUpload(', 'async function processingAPI(');
const bytesSource=between('async function processingBytes(', 'const processingOutbox=');
const aliasesSource=between('let assetAliases=', 'const hash =');
const processingDrawSource=between('async function drawProcessing(){', "$('processingSync').onclick=");
const mimeSource=app.slice(app.indexOf('function processingMime(')).split('\n')[0];
const removeSource=app.slice(app.indexOf('async function removeDownload(')).split('\n')[0];
const id=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
const copy=value=>JSON.parse(JSON.stringify(value));
const source={track_id:id(1),sha256:'a'.repeat(64),size:4,name:'original.wav',type:'audio/wav'};
const original={id:id(1),sha256:source.sha256,size:4,name:source.name,type:source.type,variant:'Original',cached:true,favorite:true};
const asset={id:id(2),source_id:id(3),sha256:source.sha256,size:4,filename:source.name,mime:'audio/wav',kind:'original'};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
let checks=0;
async function test(name,fn){await fn();checks++;console.log(`PASS ${name}`)}

function enqueueRig(overrides={}){
  const nodes=new Map(),saved=[];let syncs=0;
  const $=name=>{if(!nodes.has(name))nodes.set(name,{value:'',disabled:false,textContent:''});return nodes.get(name)};
  for(const [key,value]of Object.entries({mixedStart:'0.25',mixedEnd:'6.25',mixedPerformers:'1',mixedInstrument:'acoustic',mixedBassPart:'melody',mixedAccompaniment:'melody-only',mixedSourceNotes:'Ringing line',mixedArrangementNotes:'Keep the rests'}))$(key).value=value;
  const context=vm.createContext({$,enqueueingProcessing:false,action:fn=>fn(),withProcessingLock:fn=>fn(),
    mixedSource:copy(original),mixedPreviewRequest,mediaPath:key=>'/offline-media/'+key,
    processingBytes:async()=>new ArrayBuffer(4),processingOutbox:{enqueue:async(s,r)=>saved.push(copy({source:s,request:r}))},
    syncProcessing:()=>{syncs++;return new Promise(()=>{})},play:()=>{throw Error('Playback forbidden')},...overrides});
  vm.runInContext(mimeSource+enqueueSource,context);
  return {context,$,saved,get syncs(){return syncs}};
}

await test('actual queue click is single-flight through durable enqueue and never awaits network flush',async()=>{
  const read=deferred(),r=enqueueRig({processingBytes:()=>read.promise});
  const first=r.$('mixedQueue').onclick(),second=r.$('mixedQueue').onclick();
  assert.equal(second,undefined);assert.equal(r.$('mixedQueue').disabled,true);assert.equal(r.saved.length,0);
  read.resolve(new ArrayBuffer(4));await first;
  assert.equal(r.saved.length,1);assert.equal(r.syncs,1);assert.equal(r.$('mixedQueue').disabled,false);
  assert.equal(r.saved[0].request.asset_id,undefined);assert.equal(r.saved[0].request.start_seconds,.25);
  assert.equal(r.saved[0].request.guidance.accompaniment,'melody-only');
});

await test('actual handler preserves readonly original identity and captures selected source before byte verification',async()=>{
  const read=deferred(),before=Object.freeze(copy(original));const r=enqueueRig({mixedSource:before,processingBytes:()=>read.promise});
  const work=r.$('mixedQueue').onclick();r.context.mixedSource={...copy(original),id:id(9),name:'different.wav'};
  r.$('mixedStart').value='9';read.resolve(new ArrayBuffer(4));await work;
  assert.equal(r.saved[0].source.track_id,original.id);assert.equal(r.saved[0].source.name,original.name);
  assert.equal(r.saved[0].request.start_seconds,.25);assert.deepEqual(before,original);
});

await test('queue intent is captured before a delayed cross-tab lock and MIME fallback supports local known extensions',async()=>{
  const lock=deferred(),r=enqueueRig({mixedSource:{...copy(original),name:'take.M4A',type:''},withProcessingLock:async fn=>{await lock.promise;return fn()}});
  const work=r.$('mixedQueue').onclick();r.context.mixedSource={...copy(original),id:id(9)};r.$('mixedEnd').value='9';
  lock.resolve();await work;
  assert.equal(r.saved[0].source.name,'take.M4A');assert.equal(r.saved[0].source.type,'audio/mp4');assert.equal(r.saved[0].request.end_seconds,6.25);
  assert.equal(r.context.processingMime({name:'track.flac',type:'application/octet-stream'}),'audio/flac');
  assert.equal(r.context.processingMime({name:'unknown.bin',type:''}),null);
});

await test('failed cache verification or durable enqueue leaves controls reusable and never starts networking',async()=>{
  for(const overrides of [{processingBytes:async()=>{throw Error('Hash mismatch')}},
    {processingOutbox:{enqueue:async()=>{throw Error('Storage quota')}}},{mixedSource:{...copy(original),cached:false}}]){
    const r=enqueueRig(overrides);await r.$('mixedQueue').onclick();
    assert.equal(r.saved.length,0);assert.equal(r.syncs,0);assert.equal(r.$('mixedQueue').disabled,false);
    assert.ok(r.$('mixedError').textContent);assert.equal(r.context.enqueueingProcessing,false);
  }
});

await test('actual processingBytes checks cache presence, complete length and digest',async()=>{
  for(const kind of ['missing','length','hash','good']){
    const data=new ArrayBuffer(kind==='length'?3:4);
    const c=vm.createContext({MEDIA:'cache',mediaPath:key=>key,caches:{open:async()=>({match:async()=>kind==='missing'?null:{arrayBuffer:async()=>data}})},hash:async()=>kind==='hash'?'b'.repeat(64):source.sha256});
    vm.runInContext(bytesSource,c);
    if(kind==='good')assert.equal(await c.processingBytes(source),data);
    else await assert.rejects(()=>c.processingBytes(source));
  }
});

function uploadRig({reply=asset,fail=false,hold=false}={}){
  const started=deferred(),release=deferred();let transactions=0,draws=0;
  const rows=new Map([[original.id,copy(original)],[asset.id,{...copy(original),id:asset.id,url:'/api/media/'+asset.id}]]);
  const settings=new Map([['queue',[original.id,original.id]]]);
  const db={transaction(names,mode){
    transactions++;if(Array.isArray(names)){assert.deepEqual(Array.from(names),['tracks','settings']);assert.equal(mode,'readwrite')}
    else{assert.equal(names,'settings');assert.ok(['readonly','readwrite'].includes(mode))}
    const tx={error:fail?Error('Injected transaction failure'):null},ops=[];
    tx.objectStore=name=>({put:(value,key)=>ops.push(()=>{(name==='tracks'?rows:settings).set(name==='tracks'?value.id:key,copy(value))}),delete:key=>ops.push(()=>rows.delete(key)),
      get:key=>{const request={};queueMicrotask(()=>{const value=(name==='tracks'?rows:settings).get(key);request.result=value===undefined?undefined:copy(value);request.onsuccess?.()});return request}});
    queueMicrotask(async()=>{started.resolve();await Promise.resolve();if(hold)await release.promise;if(fail){tx.onabort?.();return}for(const op of ops)op();tx.oncomplete?.()});
    return tx;
  }};
  const context=vm.createContext({tracks:[copy(original),{...copy(original),id:asset.id,url:'/api/media/'+asset.id}],queue:[original.id,original.id],db,
    processingAPI:async(url,options,timeout)=>{assert.equal(url,'/api/import');assert.equal(timeout,180000);assert.equal(options.body.byteLength,4);return copy(reply)},
    draw:()=>draws++,play:()=>{throw Error('Playback forbidden')}});
  vm.runInContext(aliasesSource+uploadSource,context);
  return {context,rows,settings,started,release,get transactions(){return transactions},get draws(){return draws}};
}

await test('wrong upload identity never starts a catalog transaction or changes local state',async()=>{
  for(const change of [{id:'bad'},{source_id:'bad'},{sha256:'b'.repeat(64)},{size:5},{kind:'preview'}]){
    const r=uploadRig({reply:{...asset,...change}}),before=copy(r.context.tracks);
    await assert.rejects(()=>r.context.processingUpload({source,bytes:new ArrayBuffer(4)}),/different original identity/);
    assert.equal(r.transactions,0);assert.deepEqual(copy(r.context.tracks),before);assert.equal(r.draws,0);
  }
});

await test('successful upload atomically adopts catalog and queue without duplicate original or source mutation',async()=>{
  const r=uploadRig({hold:true}),immutable=Object.freeze(copy(source));
  const work=r.context.processingUpload({source:immutable,bytes:new ArrayBuffer(4)});await r.started.promise;
  assert.equal(r.context.tracks[0].id,original.id);assert.deepEqual(copy(r.context.queue),[original.id,original.id]);
  r.release.resolve();assert.deepEqual(await work,asset);
  assert.equal(r.transactions,1);assert.equal(r.context.tracks.length,1);assert.equal(r.context.tracks[0].id,asset.id);
  assert.equal(r.context.tracks[0].cached,true);assert.equal(r.context.tracks[0].favorite,true);
  assert.deepEqual(copy(r.context.queue),[asset.id,asset.id]);assert.deepEqual(r.settings.get('queue'),[asset.id,asset.id]);
  assert.equal(r.rows.has(original.id),false);assert.equal(r.rows.size,1);assert.deepEqual(immutable,source);
});

await test('catalog transaction failure retains local catalog, queue and outbox source identity',async()=>{
  const r=uploadRig({fail:true}),before=copy(r.context.tracks);
  await assert.rejects(()=>r.context.processingUpload({source,bytes:new ArrayBuffer(4)}),/transaction failure/);
  assert.deepEqual(copy(r.context.tracks),before);assert.deepEqual(copy(r.context.queue),[original.id,original.id]);
  assert.equal(r.rows.has(original.id),true);assert.deepEqual(r.settings.get('queue'),[original.id,original.id]);assert.equal(r.draws,0);
});

await test('listening queue edit during adoption is retained and remapped',async()=>{
  const r=uploadRig({hold:true}),work=r.context.processingUpload({source,bytes:new ArrayBuffer(4)});
  await r.started.promise;r.context.queue.push(id(8));r.release.resolve();await work;
  assert.deepEqual(copy(r.context.queue),[asset.id,asset.id,id(8)]);
  assert.deepEqual(r.settings.get('assetAliases'),{[original.id]:asset.id});
  // An already queued older-ID settings write can land after adoption. A
  // fresh load must repair those references from the same durable aliases.
  r.settings.set('queue',[original.id,id(8),original.id]);
  assert.deepEqual(copy(await r.context.loadQueue()),[asset.id,id(8),asset.id]);
});

await test('completed queue preview cannot replace an active or starting preview',async()=>{
  class Node{constructor(text=''){this.textContent=text;this.children=[]}append(...items){this.children.push(...items)}replaceChildren(...items){this.children=items}}
  const nodes=new Map(),$=name=>{if(!nodes.has(name))nodes.set(name,new Node());return nodes.get(name)};let opened=0;
  const row={status:'complete',source,receipt:{job_id:id(7)},reason:null,
    request:{start_seconds:.25,end_seconds:6.25,performers:1,instrument:'jazz-bass',bass_part:'bass-line',guidance:{arrangement_notes:'Keep <space> & rests'}}};
  const c=vm.createContext({$,processingOutbox:{list:async()=>[row]},document:{createElement:()=>new Node()},
    button:(text,fn)=>Object.assign(new Node(text),{onclick:fn}),viewSavedJob:async()=>opened++,activeJob:id(8),refinementStarting:false});
  vm.runInContext(processingDrawSource,c);await c.drawProcessing();
  const label=$('processingQueue').children[0].children[0].textContent;
  assert.match(label,/0\.25–6\.25 seconds/);assert.match(label,/Jazz Bass · 1 player · bass line/);
  assert.equal($('processingQueue').children[0].children[1].textContent,'Keep <space> & rests');
  const open=$('processingQueue').children[0].children.find(n=>n.textContent==='Open preview');
  await assert.rejects(async()=>open.onclick(),/current preview/);assert.equal(opened,0);assert.equal(c.activeJob,id(8));
  c.activeJob=null;c.refinementStarting=true;await assert.rejects(async()=>open.onclick(),/current preview/);assert.equal(opened,0);
  c.refinementStarting=false;await open.onclick();assert.equal(opened,1);
});

await test('actual cache removal preserves pending unreceipted original and fails closed on unreadable intent storage',async()=>{
  for(const state of ['local','waiting','attention','cancel-requested','storage-error','received','cancelled']){
    const track={...copy(original),url:'/api/media/'+asset.id};let deletes=0,puts=0;
    const row={source,status:state==='received'?'accepted':state,receipt:state==='received'?{job_id:id(7)}:null};
    const c=vm.createContext({tracks:[track],navigator:{locks:{request:async(name,fn)=>fn()}},processingOutbox:{list:async()=>{if(state==='storage-error')throw Error('Storage unavailable');return [row]}},
      caches:{open:async()=>({delete:async()=>deletes++})},MEDIA:'cache',mediaPath:id=>id,put:async()=>puts++,draw(){},storage:async()=>{},status(){}});
    vm.runInContext(removeSource,c);
    if(['received','cancelled'].includes(state)){
      await c.removeDownload(track);assert.equal(deletes,1);assert.equal(track.cached,false);assert.equal(puts,1);
    }else{
      await assert.rejects(()=>c.removeDownload(track));assert.equal(deletes,0);assert.equal(track.cached,true);assert.equal(puts,0);
    }
  }
});

await test('cache removal refuses unavailable cross-tab coordination before touching storage or audio',async()=>{
  for(const navigator of [undefined,{}, {locks:{}}]){
    const track={...copy(original),url:'/api/media/'+asset.id};let touched=0;
    const c=vm.createContext({navigator,tracks:[track],processingOutbox:{list:async()=>{touched++;return []}},
      caches:{open:async()=>{touched++;throw Error('Cache must not open')}}});
    vm.runInContext(removeSource,c);
    await assert.rejects(()=>c.removeDownload(track),/safe removal is unavailable/);
    assert.equal(touched,0);assert.equal(track.cached,true);
  }
});

console.log(`PASS: ${checks} actual processing app handler checks; silent injected boundaries only.`);
