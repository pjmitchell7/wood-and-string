import assert from 'node:assert/strict';
import {scheduledTransport,prepareBuffer,pcm16,validateBuffer,engineWithFallback,playNative,nextQueueIndex,ENCODED_LIMIT,DECODED_LIMIT} from './transport.mjs';
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
class Context {
  currentTime=0;sampleRate=48000;nodes=[];resumes=0;
  async resume(){this.resumes++}
  createGain(){return {connect(){},disconnect(){},gain:{events:[],setValueAtTime(v,t){this.events.push(['set',v,t])},linearRampToValueAtTime(v,t){this.events.push(['ramp',v,t])}}}}
  createBufferSource(){const node={connect(){},disconnect(){this.disconnected=true},stop(){this.stopped=true},start(when,offset){this.when=when;this.offset=offset}};this.nodes.push(node);return node}
  createBuffer(channels,length,sampleRate){return {numberOfChannels:channels,length,sampleRate,duration:length/sampleRate,channels:Array.from({length:channels},()=>new Float32Array(length)),getChannelData(i){return this.channels[i]}}}
}
const buffer=(duration=10)=>({duration,length:duration*48000,numberOfChannels:2,sampleRate:48000});
const a={id:'a',index:0},b={id:'b',index:1},c={id:'c',index:2};
{
 let native=0,issues=[];
 assert.equal(await engineWithFallback({configure:async()=>{throw Error('AudioContext unavailable')},useNative:async()=>native++,isCurrent:()=>true,onIssue:s=>issues.push(s)}),true);
 assert.equal(native,1);assert.match(issues[0],/Using native/);
 await engineWithFallback({configure:async()=>{throw Error('old failure')},useNative:async()=>native++,isCurrent:()=>false,onIssue:()=>assert.fail()});assert.equal(native,1);
 let paused=0,announced=0;
 await assert.rejects(()=>playNative({audio:{play:async()=>{throw Error('blocked')},pause:()=>paused++},isCurrent:()=>true,onStarted:()=>announced++}));
 assert.equal(paused,1);assert.equal(announced,0);
 assert.equal(await playNative({audio:{play:async()=>{throw Error('old failure')},pause:()=>assert.fail()},isCurrent:()=>false,onStarted:()=>assert.fail()}),false);
 assert.equal(nextQueueIndex(['same','same'],0),1);
 assert.equal(nextQueueIndex(['same','same'],1),-1);
 assert.equal(nextQueueIndex(['same','same'],1,{repeat:'all'}),0);
 assert.equal(nextQueueIndex(['same','same'],1,{repeat:'one'}),1);
 assert.equal(nextQueueIndex(['a','b'],0,{shuffle:true,random:()=>.75}),1);
 assert.equal(nextQueueIndex([],0),-1);
}
function rig(load=async()=>buffer()){
 const context=new Context(),current=[],issues=[],ended=[];let order=[a,b,c],tick;
 const player=scheduledTransport({context,destination:{},load,next:entry=>order[order.findIndex(x=>x===entry)+1],onCurrent:e=>current.push(e.id),onEnded:e=>ended.push(e.id),onIssue:e=>issues.push(e),setTick:fn=>{tick=fn;return 1},clearTick:()=>{tick=null}});
 return {context,player,current,issues,ended,setOrder:value=>order=value,tick:()=>tick?.()};
}
{
 // One pending resume owns exactly one source even under rapid double clicks.
 const r=rig();r.setOrder([a]);await r.player.start(a);r.player.pause();
 let release,calls=0;r.context.resume=()=>{calls++;return new Promise(resolve=>release=resolve)};
 const first=r.player.resume(),second=r.player.resume();assert.equal(calls,1);
 release();await first;await second;assert.equal(r.context.nodes.length,2);
 assert.equal(r.context.nodes.filter(n=>!n.stopped).length,1);r.player.stop();
}
for(const interruption of ['pause','seek','stop']){
 const r=rig();r.setOrder([a]);await r.player.start(a);r.player.pause();
 let release;r.context.resume=()=>new Promise(resolve=>release=resolve);
 const resumed=r.player.resume();
 if(interruption==='seek')await r.player.seek(3);else r.player[interruption]();
 release();await resumed;assert.equal(r.context.nodes.length,1);assert(r.context.nodes[0].stopped);
 if(interruption==='seek')assert.equal(r.player.position(),3);r.player.stop();
}
for(const interruption of ['queueChanged','stop','pause']){
 let release;const r=rig(()=>new Promise(resolve=>release=resolve));
 const started=r.player.start(a);await flush();assert.equal(r.player.pending,true);
 r.player[interruption]();release(buffer());assert.equal(await started,false);
 assert.equal(r.context.nodes.length,0);assert.deepEqual(r.current,[]);assert.equal(r.player.pending,false);r.player.stop();
}
{
 let reject;const r=rig(()=>new Promise((resolve,failure)=>reject=failure));
 const started=r.player.start(a);await flush();r.player.queueChanged();reject(Error('stale failed decode'));
 assert.equal(await started,false);assert.equal(r.context.nodes.length,0);r.player.stop();
}
{
 const r=rig();await r.player.start(a);await flush();
 assert.equal(r.context.nodes[0].when,.02);assert.equal(r.context.nodes[1].when,10.02);
 r.context.currentTime=10.02;r.tick();assert.deepEqual(r.current,['a','b']);
 await flush();assert.equal(r.context.nodes[2].when,20.02);
 r.player.stop();assert(r.context.nodes.every(n=>n.stopped&&n.disconnected&&n.buffer===null));
}
{
 const r=rig();r.player.setFade(2);await r.player.start(a);await flush();
 assert.equal(r.context.nodes[1].when,8.02);
 r.context.currentTime=8.52;r.tick();assert.equal(r.player.entry.id,'b');
 r.player.pause();assert(Math.abs(r.player.position()-.5)<1e-8);
 assert(r.context.nodes.every(n=>n.stopped));
 await r.player.resume();await flush();assert(Math.abs(r.context.nodes[2].offset-.5)<1e-8);
 await r.player.seek(4);await flush();assert.equal(r.player.position(),4);
 r.player.stop();
}
{
 // Queue reorder cancels the already scheduled old neighbor before rearming.
 const r=rig();await r.player.start(a);await flush();const old=r.context.nodes[1];
 r.setOrder([a,c,b]);r.player.queueChanged(a);await flush();assert(old.stopped);
 r.context.currentTime=10.04;r.tick();assert.equal(r.player.entry.id,'c');r.player.stop();
}
{
 // Pause during preparation invalidates the result; it must never get a node.
 let release;const r=rig(async entry=>entry===a?buffer():new Promise(resolve=>release=resolve));
 await r.player.start(a);await flush();r.player.pause();release(buffer());await flush();
 assert.equal(r.context.nodes.length,1);assert.equal(r.player.paused,true);r.player.stop();
}
{
 // New selection waits for stale decode; stale a cannot announce or play.
 let release,active=0,max=0;
 const r=rig(async entry=>{active++;max=Math.max(max,active);if(entry===a)await new Promise(resolve=>release=resolve);active--;return buffer()});
 const first=r.player.start(a);await flush();const second=r.player.start(c);await flush();
 assert.equal(r.context.nodes.length,0);release();await first;await second;await flush();
 assert.equal(max,1);assert.deepEqual(r.current,['c']);r.player.stop();
}
{
 const r=rig(async entry=>{if(entry===b)throw Error('native needed');return buffer()});
 await r.player.start(a);await flush();assert.deepEqual(r.issues,['native needed']);
 r.context.currentTime=10.02;r.context.nodes[0].onended();assert.deepEqual(r.ended,['a']);r.player.stop();
}
{
 const context=new Context(),gains=[];const make=context.createGain.bind(context);context.createGain=()=>{const gain=make();gains.push(gain);return gain};
 const player=scheduledTransport({context,destination:{},load:async()=>buffer(),next:entry=>entry===a?b:null,setTick:()=>1,clearTick(){}});
 player.setFade(2);player.setVolume(.3);await player.start(a);await flush();
 assert.equal(gains[0].gain.value,.3);
 assert.deepEqual(gains[1].gain.events,[['set',1,8.02],['ramp',0,10.02]]);
 assert.deepEqual(gains[2].gain.events,[['set',0,8.02],['ramp',1,10.02]]);player.stop();
}
{
 let release;const r=rig(async entry=>entry===a?buffer():new Promise(resolve=>release=resolve));
 await r.player.start(a);await flush();r.context.currentTime=10.015;release(buffer());await flush();
 assert.equal(r.context.nodes.length,1);assert.match(r.issues[0],/not ready/);r.player.stop();
}
{
 // Actual PCM unpacking and preallocation limits, not a mocked decode result.
 const bytes=new ArrayBuffer(48),v=new DataView(bytes);const text=(p,s)=>[...s].forEach((c,i)=>v.setUint8(p+i,c.charCodeAt(0)));
 text(0,'RIFF');v.setUint32(4,40,true);text(8,'WAVE');text(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,8000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);text(36,'data');v.setUint32(40,4,true);v.setInt16(44,-32768,true);v.setInt16(46,16384,true);
 assert.deepEqual([...pcm16(new Context(),bytes).getChannelData(0)],[-1,.5]);
 assert.throws(()=>validateBuffer({...buffer(),length:DECODED_LIMIT,numberOfChannels:2}));
 assert.throws(()=>validateBuffer({...buffer(),numberOfChannels:6}));
 let reads=0;
 await assert.rejects(()=>prepareBuffer({track:{size:ENCODED_LIMIT+1},read:()=>reads++}));assert.equal(reads,0);
 const ctx=new Context();let decodes=0;ctx.decodeAudioData=async()=>{decodes++;return buffer()};
 await assert.rejects(()=>prepareBuffer({context:ctx,track:{size:12},read:async()=>new ArrayBuffer(12),metadata:async()=>Infinity,isCurrent:()=>true}));assert.equal(decodes,0);
 const prepared=await prepareBuffer({context:ctx,track:{size:12},read:async()=>new ArrayBuffer(12),metadata:async()=>20,isCurrent:()=>true});assert.equal(prepared.duration,10);assert.equal(decodes,1);
 ctx.decodeAudioData=async()=>({...buffer(),numberOfChannels:8});
 await assert.rejects(()=>prepareBuffer({context:ctx,track:{size:12},read:async()=>new ArrayBuffer(12),metadata:async()=>20,isCurrent:()=>true}));
}
console.log('PASS: silent audio-clock scheduling, overlap, seek/pause, queue changes, stale serialized preparation, fallback boundary, PCM parsing and memory admission.');
