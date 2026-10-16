import assert from 'node:assert/strict';
import { CaptureJournal } from './capture-journal.mjs';
import { mountCapture } from './capture-ui.mjs';
import { ForegroundCapture } from './capture.mjs';
class MemoryStore {
  constructor(){this.records=new Map()}
  async update(id,change){const result=change(this.records.get(id));if(result)this.records.set(id,result);else this.records.delete(id);return result}
  async list(){return [...this.records.values()]}
}
const store=new MemoryStore(),journal=new CaptureJournal(store);
await journal.begin('interrupted','Recovered');await journal.append('interrupted',new Blob(['prefix-one'],{type:'audio/webm'}));await journal.append('interrupted',new Blob(['prefix-two'],{type:'audio/webm'}));
// Simulated process loss: new journal instance, no stop/final-data/complete callback.
const reopened=new CaptureJournal(store),record=(await reopened.list())[0],recovered=await reopened.recover(record);
assert.equal(await recovered.blob.text(),'prefix-oneprefix-two');assert.equal(recovered.complete,false);assert.equal(recovered.chunkCount,2);
record.hashes[1]='corrupt';await assert.rejects(()=>reopened.recover(record),/verification/);assert.equal((await reopened.list()).length,1,'corrupt evidence remains recoverable for inspection');
await journal.remove('interrupted');
function fakeDOM(){const elements=new Map();return {visibilityState:'visible',listeners:{},getElementById(id){if(!elements.has(id))elements.set(id,{hidden:false,disabled:false,textContent:'',dataset:{}});return elements.get(id)},addEventListener(name,callback){this.listeners[name]=callback},createElement(){return {click(){}}}}}
const documentRef=fakeDOM(),windowRef={listeners:{},addEventListener(name,callback){this.listeners[name]=callback}};
let releaseBudget,budgetCalls=0,requests=0,saved=0,created,resolveSaved;
const savedPromise=new Promise(resolve=>{resolveSaved=resolve});
class FakeCapture {
  constructor(options){this.options=options;this.state='idle';this.finished=new Promise(resolve=>{this.resolve=resolve})}
  async start(){requests++;this.state='recording';this.options.onState(this.state);await this.options.onChunk(new Blob(['delivered'],{type:'audio/webm'}))}
  stop(){this.state='complete';this.options.onState(this.state);this.resolve({blob:new Blob(['delivered'],{type:'audio/webm'}),journalComplete:true});return this.finished}
}
const mounted=mountCapture({documentRef,windowRef,journal,lease:async()=>({exclusive:true,release(){}}),saveFile:async file=>{saved++;assert.equal(await file.text(),'delivered');resolveSaved()},checkBudget:()=>{budgetCalls++;return new Promise(resolve=>{releaseBudget=resolve})},pausePlayback(){},isBusy:()=>false,createCapture:options=>(created=new FakeCapture(options))});
await mounted.ready;
const first=documentRef.getElementById('captureStart').onclick(),second=documentRef.getElementById('captureStart').onclick();
assert.equal(budgetCalls,1);assert.equal(documentRef.getElementById('captureStart').disabled,true);releaseBudget();await Promise.all([first,second]);assert.equal(requests,1,'R016 exactly one recorder owns both rapid Start events');
documentRef.getElementById('captureCancel').onclick();await savedPromise;await new Promise(resolve=>setImmediate(resolve));assert.equal(saved,1);assert.equal((await journal.list()).length,0,'journal removed only after successful final library save');
// A new mount offers delivered incomplete bytes without claiming full recovery.
await journal.begin('abrupt','Interrupted take');await journal.append('abrupt',new Blob(['saved-prefix'],{type:'audio/webm'}));
const recoveryDOM=fakeDOM();const next=mountCapture({documentRef:recoveryDOM,windowRef,journal,lease:async()=>({exclusive:true,release(){}}),saveFile:async()=>{throw Error('quota')},checkBudget:async()=>{},pausePlayback(){},isBusy:()=>false});await next.ready;
assert.match(recoveryDOM.getElementById('captureStatus').textContent,/may be missing/);assert.equal(recoveryDOM.getElementById('captureStart').disabled,true);
await recoveryDOM.getElementById('captureRetry').onclick();assert.equal((await journal.list()).length,1,'failed final save preserves durable prefix');assert.equal(recoveryDOM.getElementById('captureFallback').hidden,false);
console.log('PASS: R016 delayed-budget double Start is single-flight; R017 durable prefix recovery, hash validation, warning, failed-save retention and post-success removal');
// Pagehide while a permission result is pending must cancel a later grant.
let announcePermission,grantPermission,lateStopped=0;
const requested=new Promise(resolve=>{announcePermission=resolve}),hiddenDOM=fakeDOM(),hiddenWindow={listeners:{},addEventListener(name,callback){this.listeners[name]=callback}};
const hidden=mountCapture({documentRef:hiddenDOM,windowRef:hiddenWindow,journal:new CaptureJournal(new MemoryStore()),lease:async()=>({exclusive:true,release(){}}),saveFile:async()=>{assert.fail('cancelled permission cannot save a take')},checkBudget:async()=>{},pausePlayback(){},isBusy:()=>false,createCapture:options=>new ForegroundCapture({...options,mediaDevices:{getUserMedia:()=>{announcePermission();return new Promise(resolve=>{grantPermission=resolve})}},Recorder:class{static isTypeSupported(){return true}},AudioContext:null})});
await hidden.ready;const pendingStart=hiddenDOM.getElementById('captureStart').onclick();await requested;hiddenWindow.listeners.pagehide();grantPermission({getTracks:()=>[{stop(){lateStopped++}}]});await pendingStart;assert.equal(lateStopped,1);assert.match(hiddenDOM.getElementById('captureStatus').textContent,/cancelled/);
console.log('PASS: pagehide during permission cancels late grant without recording or retaining acquired microphone');
