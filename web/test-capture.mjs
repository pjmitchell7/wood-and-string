import assert from 'node:assert/strict';
import { ForegroundCapture, assessLevel } from './capture.mjs';
class Recorder extends EventTarget {
  static isTypeSupported(type){return type==='audio/webm;codecs=opus'}
  constructor(stream,options){super();this.state='inactive';this.mimeType=options.mimeType;this.stops=0}
  start(){this.state='recording'} pause(){this.state='paused'} resume(){this.state='recording'}
  stop(){this.stops++;this.state='inactive';queueMicrotask(()=>{const event=new Event('dataavailable');event.data=new Blob(['synthetic-test-bytes'],{type:this.mimeType});this.dispatchEvent(event);this.dispatchEvent(new Event('stop'))})}
}
let requested=0,stopped=0,timer,delay;
const deps={mediaDevices:{getUserMedia:async()=>{requested++;const track={stop(){stopped++}};return {getTracks:()=>[track]}}},Recorder,AudioContext:null,setTimer:(callback,ms)=>{timer=callback;delay=ms;return 1},clearTimer(){},setTick:()=>2,clearTick(){},now:()=>100};
const take=new ForegroundCapture(deps);
assert.equal(requested,0,'constructing the recorder never requests a microphone');
await take.start();assert.equal(requested,1);assert.equal(delay,30000);assert.equal(take.state,'recording');
take.pause();assert.equal(take.state,'paused');take.resume();assert.equal(take.state,'recording');
const first=take.stop({cancelled:true}),second=take.stop();assert.equal(first,second);
const saved=await first;assert.equal(saved.cancelled,true);assert.ok(saved.blob.size>0);assert.equal(saved.blob.type,'audio/webm');assert.equal(stopped,1);assert.equal(take.recorder.stops,1);
const bounded=new ForegroundCapture(deps);await bounded.start();timer();await bounded.finished;assert.equal(stopped,2,'duration limit stops microphone tracks');
const broken=new ForegroundCapture({...deps,Recorder:class extends Recorder{constructor(){throw Error('constructor failed')}}});
await assert.rejects(()=>broken.start(),/constructor failed/);assert.equal(stopped,3,'setup errors release acquired microphone');
const denied=new ForegroundCapture({...deps,mediaDevices:{getUserMedia:async()=>{throw Error('denied')}}});await assert.rejects(()=>denied.start(),/denied/);assert.equal(denied.state,'failed');
assert.equal(assessLevel(new Float32Array(64)).state,'amber');
assert.match(assessLevel(new Float32Array([.99,.1])).label,/clipping/);
assert.equal(assessLevel(new Float32Array([.1,-.1,.1,-.1])).state,'green');
assert.match(assessLevel(new Float32Array([.1,-.1])).label,/not assessed/);
console.log('PASS: explicit permission, pause/resume, stop idempotence, cancel preserves bytes, 30s limit, cleanup on errors/denial, quiet/clipping/level labels');
// REVIEW-004 R015: optional metering may hang forever; stop must not wait for it.
let resolveResume,closedContexts=0,meterConnections=0;
class HungContext {resume(){return new Promise(resolve=>{resolveResume=resolve})}close(){closedContexts++;return Promise.resolve()}createMediaStreamSource(){meterConnections++;return {connect(){},disconnect(){}}}createAnalyser(){return {fftSize:0,disconnect(){}}}}
const stalled=new ForegroundCapture({...deps,AudioContext:HungContext});
await stalled.start();assert.equal(stalled.state,'recording','meter resume never blocks recording or timer');
const stoppedBefore=stopped;const ended=stalled.stop();assert.equal(stopped,stoppedBefore+1,'track release is synchronous');
await ended;resolveResume();await Promise.resolve();assert.equal(meterConnections,0,'late resume cannot attach to cancelled take');assert.ok(closedContexts>0);
let grant;const lateTrack={stop(){stopped++}};
const late=new ForegroundCapture({...deps,mediaDevices:{getUserMedia:()=>new Promise(resolve=>{grant=resolve})}});
const pending=late.start();late.stop();const beforeGrant=stopped;grant({getTracks:()=>[lateTrack]});await assert.rejects(()=>pending,/cancelled/);assert.equal(stopped,beforeGrant+1);assert.equal(late.recorder,undefined);
console.log('PASS: R015 stalled metering releases immediately; late resume/grant cannot restart cancelled capture');
