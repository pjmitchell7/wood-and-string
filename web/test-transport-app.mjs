// Execute the actual app transport functions with silent injected media/DOM.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {engineWithFallback,playNative,nextQueueIndex} from './transport.mjs';
const app=fs.readFileSync(new URL('./app.js',import.meta.url),'utf8');
const functions=app.slice(app.indexOf('function entryAt('),app.indexOf('async function refresh('));
const bindings=app.slice(app.indexOf('function bindAudioEvents('),app.indexOf('bindAudioEvents();',app.indexOf('function bindAudioEvents(')));
class Audio {
 plays=0;pauses=0;hidden=false;events={};src='';volume=1;muted=false;playbackRate=1;
 async play(){this.plays++;if(this.reject)throw Error('native play denied')}
 pause(){this.pauses++}load(){}removeAttribute(){this.src=''}
 addEventListener(name,fn){this.events[name]=fn}
 cloneNode(){return new Audio()}replaceWith(value){this.replacement=value}
}
function rig(){
 const nodes=Object.fromEntries(['engine','bass','engineStatus','transportControls','transportVolume','repeat','shuffle','now'].map(id=>[id,{value:id==='engine'?'scheduled':id==='repeat'?'off':id==='transportVolume'?'0.35':'0',checked:false}]));
 const messages=[],audio=new Audio();
 const scope={audio,context:null,source:null,eq:null,transport:null,objectUrl:null,playGeneration:0,current:-1,queueSignature:'',
   tracks:[{id:'a',name:'Song',url:'/api/media/a',cached:false}],queue:['a'],
   $:id=>nodes[id],comparison:{stop(){}},navigator:{},drawQueue(){},status:s=>messages.push(s),session:async()=>{},
   engineWithFallback,playNative,nextQueueIndex,URL,
   AudioContext:class{constructor(){throw Error('context unavailable')}}};
 vm.createContext(scope);vm.runInContext(functions+bindings,scope);scope.bindAudioEvents();return {scope,nodes,messages,audio};
}
{
 const r=rig();await r.scope.play(0);
 assert.equal(r.nodes.engine.value,'native');assert.equal(r.audio.plays,1);assert.equal(r.audio.src,'/api/media/a');assert.equal(r.scope.current,0);
 assert.match(r.messages[0],/context unavailable.*Using native/);assert.equal(r.audio.hidden,false);
 assert.equal(r.audio.volume,.35,'fallback must preserve scheduled listening level');
}
{
 const r=rig();let closed=0,disconnected=0;
 r.scope.context={resume:async()=>{throw Error('resume denied')},close:async()=>closed++};
 r.scope.source={disconnect:()=>disconnected++};r.scope.eq={disconnect(){},gain:{value:0}};
 r.audio.muted=true;r.audio.playbackRate=1.25;
 await r.scope.play(0);
 assert.equal(r.audio.plays,0);assert.equal(r.audio.replacement,r.scope.audio);assert.equal(r.scope.audio.plays,1);
 assert.equal(closed,1);assert.equal(disconnected,1);
 assert.equal(r.scope.audio.volume,.35);assert.equal(r.scope.audio.muted,true);assert.equal(r.scope.audio.playbackRate,1.25);
 // Retired element callbacks must not pause a later transport.
 r.scope.transport={pause:()=>assert.fail('retired media affected current mode')};r.audio.events.play();
}
{
 const r=rig();r.audio.reject=true;
 await assert.rejects(()=>r.scope.play(0),/native play denied/);
 assert.equal(r.scope.current,-1);assert(r.audio.pauses>0);
}
{
 const r=rig();let reject;
 r.scope.context={resume:()=>new Promise((resolve,failure)=>reject=failure)};
 const playing=r.scope.play(0);await Promise.resolve();r.scope.playGeneration++;
 r.nodes.engine.value='native';reject(Error('stale resume failure'));await playing;
 assert.equal(r.audio.plays,0);assert.deepEqual(r.messages,[]);
}
{
 const r=rig();
 await engineWithFallback({configure:r.scope.configureEngine,useNative:r.scope.useNativeEngine,isCurrent:()=>true,onIssue:r.scope.status});
 assert.equal(r.audio.plays,0,'settings configuration cannot autoplay');
}
console.log('PASS: actual app functions use native fallback for missing/failed contexts, replace failed routed media, suppress stale mode changes and handle rejected play without autoplay.');
