import assert from 'node:assert/strict';
import {originalPassage,passagePlayer,canPreviewMixed} from './mixed-preview.mjs';
for(const type of ['audio/wav','audio/mpeg','audio/mp4','audio/aac','audio/flac','audio/webm']) {
  assert.equal(canPreviewMixed({url:'/saved',variant:'original',type}),true);
  assert.equal(canPreviewMixed({variant:'original',type}),false);
  assert.equal(canPreviewMixed({url:'/saved',variant:'mixed-preview-1',type}),false);
}
assert.equal(canPreviewMixed({url:'/saved',variant:'original',type:'audio/ogg'}),false);
class Audio extends EventTarget {
  readyState=1;duration=60;paused=true;error=null;plays=0;
  removeAttribute(){this.src=''} load(){} pause(){this.paused=true}
  async play(){this.plays++;this.paused=false}
  set currentTime(value){this.time=value;queueMicrotask(()=>this.dispatchEvent(new Event('seeked')))}
  get currentTime(){return this.time||0}
}
const track={id:'original',variant:'original',name:'Song.wav',sha256:'abc'};
const job={state:'complete',request:{kind:'mixed',asset_id:'original',sha256:'abc',start_seconds:10,end_seconds:13}};
const passage=originalPassage(job,[track]);assert.match(passage.label,/Song.wav.*10.00.*13.00/);
assert.equal(passage.label,'Original · Song.wav · 10.00–13.00 seconds');
const decodedJob={...job,result:{analysis:{timebase:{decoded:true}}}};
const decodedPassage=originalPassage(decodedJob,[track]);
assert.equal(decodedPassage.label,'Approximate original cue · Song.wav · 10.00–13.00 seconds');
assert.equal(decodedPassage.track,track);
assert.equal(decodedPassage.start,passage.start);assert.equal(decodedPassage.end,passage.end);
for(const decoded of [false,undefined,'true']){
 assert.equal(originalPassage({...job,result:{analysis:{timebase:{decoded}}}},[track]).label,passage.label);
}
assert.throws(()=>originalPassage(decodedJob,[{...track,sha256:'wrong'}]));
assert.throws(()=>originalPassage(decodedJob,[{...track,id:'different'}]));
assert.throws(()=>originalPassage({...decodedJob,request:{...job.request,end_seconds:41}},[track]));
assert.throws(()=>originalPassage({...decodedJob,state:'running'},[track]));
assert.throws(()=>originalPassage(job,[{...track,sha256:'wrong'}]));
assert.throws(()=>originalPassage(job,[{...track,variant:'mixed-preview-1'}]));
let pausedMain=0,released=0,timers=new Map(),nextTimer=0;
const audio=new Audio();
const player=passagePlayer({audio,load:async()=>({url:'blob:test',release:()=>released++}),pauseMain:()=>pausedMain++,
 setTimer:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,ms});return id},clearTimer:id=>timers.delete(id)});
assert.equal(audio.plays,0);
await player.start(passage);assert.equal(audio.currentTime,10);assert.equal(audio.plays,1);assert.equal(pausedMain,1);
assert.equal([...timers.values()][0].ms,3000);
[...timers.values()][0].fn();assert.equal(audio.paused,true);assert.equal(released,1);assert.equal(timers.size,0);
for(const event of ['pause','seeking','ended','error','emptied','ratechange']){
 await player.start(passage);audio.dispatchEvent(new Event(event));assert.equal(audio.paused,true);assert.equal(timers.size,0);
}
await player.start(passage);audio.time=13;audio.dispatchEvent(new Event('timeupdate'));assert.equal(audio.paused,true);
let resolveLoad,lateReleased=0;
const pending=passagePlayer({audio:new Audio(),load:()=>new Promise(r=>resolveLoad=r),pauseMain:()=>{}});
const started=pending.start(passage);pending.stop();resolveLoad({url:'blob:late',release:()=>lateReleased++});await started;assert.equal(lateReleased,1);
const bad=new Audio();bad.duration=11;
const invalid=passagePlayer({audio:bad,load:async()=>({url:'blob:bad',release:()=>{}}),pauseMain:()=>{}});
await assert.rejects(()=>invalid.start(passage));assert.equal(bad.plays,0);
console.log('Original comparison: selection, source cue, stop bounds, cleanup, stale-load and invalid duration passed; synthetic media only.');
