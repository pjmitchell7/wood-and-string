import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import { mixedPreviewRequest, previewChoice, previewSelection, previewGuidance } from './mixed-preview.mjs';
import { ProcessingOutbox, transitionOperation, validateOperation } from './processing-outbox.mjs';
import {electricTonePreset,electricToneForPreset,electricToneLabel,electricToneDescription} from './electric-tone.mjs';

// Pure settings/description checks: no browser, media, network or native calls.
const source={id:'original',url:'/api/media/original',name:'Synthetic original'};
const snapshot=JSON.stringify(source);
for(const performers of [1,2]){
  assert.deepEqual(previewSelection({instrument:'electric'},performers),{instrument:'electric'});
  const request=mixedPreviewRequest(source,121,123.1,performers,{}, {instrument:'electric'});
  assert.deepEqual(request,{asset_id:'original',start_seconds:121,end_seconds:123.1,performers,instrument:'electric'});
  const saved=JSON.parse(JSON.stringify(request));
  assert.deepEqual(previewSelection(saved,performers),{instrument:'electric'});
  assert.equal(previewChoice(saved),previewChoice(request));
  assert.match(previewChoice(saved),/dry Electric guitar/);
  assert.match(previewChoice(saved),/estimated melody/);
  assert.match(previewChoice(saved),/estimated bass|melody and bass/);
  assert.match(previewChoice(saved),performers===2?/two players/:/one player/);
  assert.doesNotMatch(previewChoice(saved),/Jazz Bass|Acoustic|identified|recovered|original guitar|accurate/i);
  const notes=Object.freeze({source_notes:' cafe\u0301 arpeggios ',arrangement_notes:' Keep the rests ',accompaniment:'melody-only'});
  const selected=Object.freeze({instrument:'electric'});
  const guided=mixedPreviewRequest(source,1,3,performers,notes,selected);
  assert.deepEqual(guided.guidance,{version:1,source_notes:'café arpeggios',arrangement_notes:'Keep the rests',accompaniment:'melody-and-bass'});
  assert.equal(guided.bass_part,undefined);assert.equal(guided.electric_planner_version,undefined);
  assert.equal(notes.accompaniment,'melody-only');
  assert.equal(previewChoice(guided),previewChoice(saved),'Description must not claim to interpret saved text.');
  assert.throws(()=>previewChoice({...saved,guidance:{accompaniment:'melody-only'}}));
  for(const bass_part of ['melody','bass-line','melody-and-bass',null]){
    assert.throws(()=>previewSelection({instrument:'electric',bass_part},performers),/Jazz Bass/);
    assert.throws(()=>mixedPreviewRequest(source,0,2,performers,{}, {instrument:'electric',bass_part}),/Jazz Bass/);
  }
}
for(const performers of [0,3,true,'1'])assert.throws(()=>previewSelection({instrument:'electric'},performers),/one or two/);
for(const notes of [{source_notes:[]},{source_notes:'x'.repeat(257)},{arrangement_notes:'bad\0text'}])
  assert.throws(()=>mixedPreviewRequest(source,0,2,1,notes,{instrument:'electric'}));
assert.equal(JSON.stringify(source),snapshot);

// Explicit Electric support must not add fields to the existing requests.
assert.deepEqual(mixedPreviewRequest(source,0,5,1),{asset_id:'original',start_seconds:0,end_seconds:5,performers:1});
assert.deepEqual(mixedPreviewRequest(source,0,5,1,{accompaniment:'melody-only'}, {instrument:'acoustic'}),
  {asset_id:'original',start_seconds:0,end_seconds:5,performers:1,guidance:{version:1,source_notes:'',arrangement_notes:'',accompaniment:'melody-only'}});
assert.deepEqual(mixedPreviewRequest(source,0,5,1,{}, {instrument:'jazz-bass',bass_part:'bass-line'}),
  {asset_id:'original',start_seconds:0,end_seconds:5,performers:1,instrument:'jazz-bass',bass_part:'bass-line'});
assert.deepEqual(mixedPreviewRequest(source,0,5,2,{}, {instrument:'jazz-bass'}),
  {asset_id:'original',start_seconds:0,end_seconds:5,performers:2,instrument:'jazz-bass',bass_part:'melody-and-bass'});
console.log('PASS Electric solo/duo requests, fixed accompaniment, metadata notes, saved descriptions, role guards and unchanged legacy shapes; silent.');

// Exercise actual app handlers. The enabled variants change only the in-memory
// test copy of the gate, to check future branch behavior without enabling UI.
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
assert.match(app,/const ELECTRIC_PREVIEW_ENABLED=false;/);
const gate=app.slice(app.indexOf('const ELECTRIC_PREVIEW_ENABLED='),app.indexOf('let bassAvailability='));
const forms=app.slice(app.indexOf("$('mixedForm').onsubmit="),app.indexOf('function selectExtraction('));
const settings=app.slice(app.indexOf('function selectMixed('),app.indexOf("$('mixedClose').onclick"));
function appRig({enabled=false,ready=false}={}){
  const fields=new Map(),requests=[],saved=[],messages=[];let reads=0,syncs=0,statusReply={bass_ready:true,electric_ready:ready};
  const node=()=>({children:[],value:'',options:[{},{}],disabled:false,textContent:'',append(...items){this.children.push(...items)},replaceChildren(...items){this.children=items},scrollIntoView(){},focus(){}});
  const $=id=>{if(!fields.has(id))fields.set(id,node());return fields.get(id)};
  for(const [id,value]of Object.entries({mixedInstrument:'electric',mixedPerformers:'2',mixedStart:'1',mixedEnd:'3',mixedBassPart:'bass-line',mixedAccompaniment:'melody-only',mixedSourceNotes:'Ringing electric',mixedArrangementNotes:'Keep rests',mixedElectricTone:'dry'}))$(id).value=value;
  const local={...source,cached:true,sha256:'a'.repeat(64),size:4,type:'audio/wav'};
  const scope={$ ,mixedSource:local,tracks:[local],activeJob:null,refinementStarting:false,enqueueingProcessing:false,bassAvailability:{ready:true},
    mixedPreviewRequest,previewSelection,previewChoice,previewGuidance,electricTonePreset,electricToneForPreset,electricToneLabel,electricToneDescription,restoredElectricTone:undefined,action:fn=>fn(),withProcessingLock:fn=>fn(),
    apiJSON:async(url,options)=>{if(!options)return statusReply;requests.push({url,request:JSON.parse(options.body)});return {id:'new-job',state:'queued'}},
    claimResult:()=>1,showJob:async()=>{},comparison:{stop(){}},jobMessage:text=>messages.push(text),
    processingBytes:async()=>{reads++;return new ArrayBuffer(4)},processingMime:()=> 'audio/wav',mediaPath:sha=>'/offline-media/'+sha,
    processingOutbox:{enqueue:async(s,q)=>saved.push(JSON.parse(JSON.stringify({source:s,request:q})))},syncProcessing:()=>{syncs++},
    document:{createElement:node}};
  vm.createContext(scope);vm.runInContext(enabled?gate.replace('ELECTRIC_PREVIEW_ENABLED=false','ELECTRIC_PREVIEW_ENABLED=true'):gate,scope);
  vm.runInContext(`electricAvailability={ready:${ready},reason:'Electric unavailable on this laptop.'};`,scope);
  vm.runInContext(forms+settings,scope);
  return {scope,$,requests,saved,messages,get reads(){return reads},get syncs(){return syncs},setStatus:value=>{statusReply=value}};
}
for(const ready of [false,true]){
  const r=appRig({ready});await r.$('mixedForm').onsubmit({preventDefault(){}});await r.$('mixedQueue').onclick();
  assert.equal(r.requests.length,0);assert.equal(r.saved.length,0);assert.equal(r.reads,0);assert.equal(r.syncs,0);
  assert.match(r.$('mixedError').textContent,/still being tested/);
}
const offline=appRig({enabled:true});
await offline.$('mixedForm').onsubmit({preventDefault(){}});assert.equal(offline.requests.length,0);
assert.match(offline.$('mixedError').textContent,/unavailable/);
await offline.$('mixedQueue').onclick();assert.equal(offline.saved.length,1);assert.equal(offline.syncs,1);
assert.equal(offline.saved[0].request.instrument,'electric');assert.equal(offline.saved[0].request.bass_part,undefined);
assert.equal(offline.saved[0].request.asset_id,undefined);assert.equal(offline.saved[0].request.guidance.accompaniment,'melody-and-bass');
assert.equal(offline.saved[0].request.guidance.source_notes,'Ringing electric');
const online=appRig({enabled:true,ready:true});await online.$('mixedForm').onsubmit({preventDefault(){}});
assert.equal(online.requests[0].url,'/api/arrange-preview');assert.equal(online.requests[0].request.instrument,'electric');
assert.equal(online.requests[0].request.bass_part,undefined);assert.equal(online.requests[0].request.performers,2);
assert.match(online.messages[0],/dry Electric/);
const oldServer=appRig({enabled:true,ready:true});oldServer.setStatus({bass_ready:true});await oldServer.scope.loadBassAvailability();
assert.equal(vm.runInContext('electricAvailability.ready',oldServer.scope),false);assert.equal(oldServer.$('mixedElectricOption').disabled,true);
await oldServer.$('mixedForm').onsubmit({preventDefault(){}});assert.equal(oldServer.requests.length,0);
const saved=appRig();saved.scope.restoreMixedSettings(mixedPreviewRequest(source,1,3,2,{source_notes:'Saved observation'}, {instrument:'electric'}));
assert.equal(saved.$('mixedInstrument').value,'electric');assert.equal(saved.$('mixedElectricOption').disabled,true);
assert.equal(saved.$('mixedBassPartLabel').hidden,true);assert.equal(saved.$('mixedAccompanimentLabel').hidden,true);
assert.equal(saved.$('mixedAccompaniment').value,'melody-and-bass');assert.match(saved.$('mixedPerformers').options[1].textContent,/Two Electric/);
assert.match(saved.$('mixedPartDescription').textContent,/Electric samples.*estimated melody and bass/);
const paragraphs=[];saved.scope.showSavedGuidance(mixedPreviewRequest(source,1,3,1,{source_notes:'Saved observation'},{instrument:'electric'}),{append:p=>paragraphs.push(p.textContent)});
assert.match(paragraphs[0],/dry Electric guitar/);assert.match(paragraphs[1],/saved note, not analyzed/);
assert.equal(saved.requests.length,0);assert.equal(saved.saved.length,0);

// The actual form, settings restore and offline click snapshot retain tone.
for(const preset of ['clean','room','delay']){
  const r=appRig({enabled:true,ready:true});r.$('mixedElectricTone').value=preset;
  await r.$('mixedForm').onsubmit({preventDefault(){}});
  assert.deepEqual(r.requests[0].request.electric_tone,electricToneForPreset(preset));
  assert.doesNotMatch(r.messages[0],/dry Electric/);
  const q=appRig({enabled:true});q.$('mixedElectricTone').value=preset;
  let release;q.scope.withProcessingLock=fn=>new Promise(resolve=>{release=()=>Promise.resolve(fn()).then(resolve)});
  const pending=q.$('mixedQueue').onclick();q.$('mixedElectricTone').value='dry';await release();await pending;
  assert.deepEqual(q.saved[0].request.electric_tone,electricToneForPreset(preset));
}
const customTone={...electricToneForPreset('room'),gain_db:-3,ambience_mix:.17};
const customRig=appRig({enabled:true,ready:true});customRig.scope.restoreMixedSettings(mixedPreviewRequest(source,1,3,1,{}, {instrument:'electric',electric_tone:customTone}));
assert.equal(customRig.$('mixedElectricTone').value,'custom');assert.equal(customRig.$('mixedElectricToneCustom').hidden,false);
await customRig.$('mixedForm').onsubmit({preventDefault(){}});assert.deepEqual(customRig.requests[0].request.electric_tone,customTone);
const invalidTone=appRig({enabled:true,ready:true});invalidTone.$('mixedElectricTone').value='unknown';
await invalidTone.$('mixedForm').onsubmit({preventDefault(){}});assert.equal(invalidTone.requests.length,0);assert.match(invalidTone.$('mixedError').textContent,/available Electric tone/);

// Saved results stay readable and correctly labelled while new revisions and
// candidate submissions remain gated, even if a caller bypasses disabled UI.
const viewed=appRig({ready:true});let refinementSubmit,candidateSubmit;
Object.assign(viewed.scope,{tracks:[{...source},{id:'render'}],localStorage:{setItem(){},removeItem(){}},
  refresh:async()=>{},button:(text,onclick)=>({textContent:text,onclick}),setTimeout:()=>1,clearTimeout(){},
  mixedCandidateSummary:()=>'',describeAdaptation:()=>'',mountRefinement:options=>{refinementSubmit=options.submit},mountSoundDiagnostics(){},
  mountInstrumentalCandidates:options=>{candidateSubmit=options.submit},
  play:()=>{throw Error('Playback forbidden')},download:()=>{throw Error('Download forbidden')}});
vm.runInContext(app.slice(app.indexOf('let resultGeneration='),app.indexOf("$('cancelDiagnostic').onclick")),viewed.scope);
await viewed.scope.showJob({id:'electric-saved',state:'complete',request:{kind:'mixed',asset_id:'original',start_seconds:1,end_seconds:3,performers:2,instrument:'electric'},
  result:{asset:{id:'render'},arrangement:{notes:[],adaptations:[]}}});
assert.ok(viewed.$('diagnosticResult').children.some(n=>n.textContent==='Play Electric guitar preview'));
assert.ok(viewed.$('diagnosticResult').children.some(n=>n.textContent?.includes('Experimental dry Electric guitar')));
await assert.rejects(()=>refinementSubmit({parent_job_id:'electric-saved',operations:[{kind:'omit',note_ids:['x']}]}),/still being tested/);
await viewed.scope.showJob({id:'candidates',state:'complete',request:{kind:'instrumental'},result:{candidates:{}}});
await assert.rejects(()=>candidateSubmit({instrument:'electric'}),/still being tested/);assert.equal(viewed.requests.length,0);

// Reproduce a real controller's lost POST reply, then restart that controller
// using the actual gated app callbacks. Only storage/transport are replaced.
const copy=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));
const uuid=n=>`00000000-0000-4000-8000-${String(n).padStart(12,'0')}`;
class MemoryStore{
  constructor(){this.rows=new Map()}
  async get(id){const row=this.rows.get(id);return row===undefined?undefined:copy(validateOperation(row))}
  async list(){return [...this.rows.values()].map(row=>copy(validateOperation(row)))}
  async update(id,change){const row=transitionOperation(this.rows.get(id),change);assert.equal(row.operation_id,id);this.rows.set(id,row);return copy(row)}
}
async function uncertainElectric({state='queued',cancelRequested=true,lookupError=null,changeRequest=request=>request,cancelState='cancelled',tone}={}){
  const store=new MemoryStore(),calls=[],bytes=new TextEncoder().encode('silent Electric queue identity');
  const sha=createHash('sha256').update(bytes).digest('hex');
  const localSource={track_id:uuid(9),name:'original.wav',type:'audio/wav',sha256:sha,size:bytes.length};
  const request={start_seconds:1,end_seconds:3,performers:2,instrument:'electric',
    guidance:{source_notes:' cafe\u0301 ',arrangement_notes:' Keep rests ',accompaniment:'melody-and-bass'}};
  if(tone)request.electric_tone=tone;
  let remote;
  const first=new ProcessingOutbox({store,uuid:()=>uuid(1),now:()=>1,getBytes:async()=>bytes,
    hashBytes:async data=>createHash('sha256').update(data).digest('hex'),
    upload:async()=>({id:uuid(2),sha256:sha,size:bytes.length}),
    submit:async value=>{
      remote={operation_id:value.operation_id,job_id:uuid(3),state,reason:null,request:changeRequest({
        ...copy(value.request),kind:'mixed',sha256:sha,pipeline_version:'frozen-pipeline',runtime_version:'frozen-runtime',
        renderer_version:'frozen-renderer',electric_planner_version:'frozen-planner',electric_profile_version:'frozen-profile',
        guidance:{version:1,source_notes:'café',arrangement_notes:'Keep rests',accompaniment:'melody-and-bass'}})};
      throw Error('POST reply lost after laptop acceptance');
    }});
  await first.enqueue(localSource,request);await first.flush();if(cancelRequested)await first.requestCancel(uuid(1));
  const before=await store.get(uuid(1));assert.equal(before.remote_may_exist,true);assert.equal(before.receipt,null);assert.equal(before.uploaded_asset_id,uuid(2));
  let box,forbidden=0;
  const context=vm.createContext({previewGuidance,previewSelection,
    ProcessingOutbox:class extends ProcessingOutbox{constructor(options){super(options);box=this}},IndexedProcessingStore:class{constructor(){return store}},
    processingBytes:async()=>{forbidden++;throw Error('Unexpected source reread')},processingUpload:async()=>{forbidden++;throw Error('Unexpected upload')},drawProcessing(){},
    processingAPI:async(url,options={})=>{
      calls.push([options.method||'GET',url]);
      if((options.method||'GET')==='GET'&&url===`/api/processing-queue/${uuid(1)}`){if(lookupError)throw lookupError;return copy(remote)}
      if(options.method==='POST'&&url===`/api/processing-queue/${uuid(1)}/cancel`){remote.state=cancelState;return copy(remote)}
      throw Error('A gated request tried to create new processing work');
    }});
  const bootstrap=app.slice(app.indexOf('function checkedElectricProcessingReceipt('),app.indexOf('async function processingUpload('));
  vm.runInContext(gate+bootstrap,context);await box.flush();
  const after=await store.get(uuid(1));
  assert.equal(after.fingerprint,before.fingerprint);assert.deepEqual(after.source,before.source);assert.deepEqual(after.request,before.request);
  assert.equal(after.remote_may_exist,true);assert.equal(after.cancel_requested,cancelRequested);assert.equal(forbidden,0);
  assert.ok(calls.every(([method,url])=>method==='GET'||url.endsWith('/cancel')),'No gated creation POST is allowed.');
  return {box,store,calls,before,after,remote};
}
const recovered=await uncertainElectric();
assert.equal(recovered.after.status,'cancelled');assert.deepEqual(recovered.after.receipt,{operation_id:uuid(1),job_id:uuid(3)});
assert.deepEqual(recovered.calls,[['GET',`/api/processing-queue/${uuid(1)}`],['POST',`/api/processing-queue/${uuid(1)}/cancel`]]);
const cancelling=await uncertainElectric({state:'running',cancelState:'cancelling'});
assert.equal(cancelling.after.status,'cancel-requested');cancelling.remote.state='cancelled';await cancelling.box.flush();
assert.equal((await cancelling.store.get(uuid(1))).status,'cancelled');assert.equal(cancelling.calls.filter(([method])=>method==='POST').length,1);
const completed=await uncertainElectric({state:'complete'});assert.equal(completed.after.status,'complete');assert.equal(completed.calls.length,1);
const accepted=await uncertainElectric({cancelRequested:false});assert.equal(accepted.after.status,'accepted');assert.equal(accepted.calls.length,1);
for(const tone of [electricToneForPreset('room'),customTone]){
  const valid=await uncertainElectric({tone});assert.equal(valid.after.status,'cancelled');
  for(const changeRequest of [q=>({...q,electric_tone:{...q.electric_tone,ambience_mix:.2}}),q=>{delete q.electric_tone;return q}]){
    const refused=await uncertainElectric({tone,changeRequest});assert.equal(refused.after.status,'attention');
    assert.equal(refused.calls.length,1);assert.equal(refused.remote.state,'queued');
  }
}
for(const status of [404,401,403,503,undefined]){
  const r=await uncertainElectric({lookupError:Object.assign(Error('Lookup unavailable'),status===undefined?{}:{status})});
  assert.equal(r.after.receipt,null);assert.equal(r.after.status,[404,401,403].includes(status)?'attention':'cancel-requested');
  assert.equal(r.calls.length,1);if(status===404)assert.match(r.after.reason,/still being tested.*no new request was sent/);
}
for(const changeRequest of [
  q=>({...q,asset_id:uuid(8)}),q=>({...q,start_seconds:0}),q=>({...q,end_seconds:4}),q=>({...q,performers:1}),
  q=>({...q,instrument:'acoustic'}),q=>({...q,bass_part:'melody-and-bass'}),q=>({...q,unexpected:'not frozen metadata'}),
  ...[{source_notes:'different'},{arrangement_notes:'different'},{accompaniment:'melody-only'},{version:2},{unknown:'different'}]
    .map(change=>q=>({...q,guidance:{...q.guidance,...change}}))]){
  const r=await uncertainElectric({changeRequest});assert.equal(r.after.status,'attention');assert.equal(r.after.receipt,null);
  assert.equal(r.calls.length,1);assert.match(r.after.reason,/different audio or settings/);assert.equal(r.remote.state,'queued');
}
console.log('PASS real Electric outbox lost-reply recovery: GET-bound intent, cancellation/terminal acknowledgement,404/auth/network uncertainty and mismatched-intent refusal; no new POST.');
const html=fs.readFileSync(new URL('./index.html',import.meta.url),'utf8');
for(const id of ['mixedElectricOption','mixedElectricAvailability'])assert.equal((html.match(new RegExp(`id="${id}"`,'g'))||[]).length,1);
console.log('PASS actual Electric Make/Queue gate, enabled-branch identity, offline intent, old-server refusal and gated saved settings; silent.');
