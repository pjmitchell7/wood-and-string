import assert from 'node:assert/strict';
import test from 'node:test';
import {webcrypto,createHash} from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';
import {canonical,committedContext,validateMetadata,validatePartBytes,createAcousticParts,mountAcousticParts,VERSION} from '../web/acoustic-parts.mjs';

const RATE=44100,FRAMES=44111,ORIGIN=987654;
const digest=bytes=>createHash('sha256').update(new Uint8Array(bytes)).digest('hex');
const utf8=value=>new TextEncoder().encode(typeof value==='string'?value:canonical(value));
const clone=value=>JSON.parse(JSON.stringify(value));
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
function wav(amplitude=1000){
  const bytes=new ArrayBuffer(44+FRAMES*4),v=new DataView(bytes),word=(at,text)=>[...text].forEach((s,i)=>v.setUint8(at+i,s.charCodeAt(0)));
  word(0,'RIFF');v.setUint32(4,bytes.byteLength-8,true);word(8,'WAVEfmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,2,true);
  v.setUint32(24,RATE,true);v.setUint32(28,RATE*4,true);v.setUint16(32,4,true);v.setUint16(34,16,true);word(36,'data');v.setUint32(40,FRAMES*4,true);
  for(const i of [0,3,FRAMES-1]){v.setInt16(44+i*4,amplitude,true);v.setInt16(46+i*4,-amplitude/2,true)}return bytes;
}
function fixture(performers=2){
  const jobId='00000000-0000-4000-8000-000000000001',sourceId='00000000-0000-4000-8000-000000000002',assetId='00000000-0000-4000-8000-000000000003';
  const files=new Map(),audio=[];
  for(let p=1;p<=performers;p++){const bytes=wav(1000*p),name=`performer-${p}.wav`;files.set(name,bytes);audio.push({name,bytes:bytes.byteLength,sha256:digest(bytes)})}
  const combined=wav(performers===2?3000:1000);files.set('combined.wav',combined);audio.push({name:'combined.wav',bytes:combined.byteLength,sha256:digest(combined)});
  const sourceSha=digest(utf8('unchanged source')),arrangementSha=digest(utf8('Python canonical final arrangement including float spellings'));
  const notes=Array.from({length:performers},(_,i)=>({id:`note-${i+1}`,start:ORIGIN,end:ORIGIN+11,pitch:40+i,velocity:80,player:i+1,string:i,fret:0,role:i?'bass':'melody',confidence:0.0}));
  const arrangement={version:1,source_id:sourceId,sample_rate:RATE,performers,render_origin_sample:ORIGIN,notes,adaptations:[],source:{source_id:sourceId,sample_rate:RATE,notes:clone(notes)}};
  const binding={source:{id:sourceId,sha256:sourceSha},clock:{sample_rate:RATE,origin_sample:ORIGIN,body_frames:11,tail_frames:RATE,frames:FRAMES},
    arrangement:{revision:jobId,sha256:arrangementSha,performers,label:performers===1?'Acoustic Solo':'Acoustic Duo'},
    renderer:{id:'sfizz-meta-acoustic',version:'sfizz-freewheel-live-quality-v1',receipt_sha256:'6'.repeat(64),source_kind:'synthesized',content_exclusion:'declared_not_verified'}};
  const file=row=>({...row,format:'pcm_s16le',channels:2,sample_rate:RATE,frames:FRAMES});
  const manifest={version:'stem-bundle-v1',...clone(binding),stems:notes.map((n,i)=>({id:`performer-${i+1}`,instrument:'acoustic',role:n.role,gain:1,default_audible:true,file:file(audio[i]),
    provenance:{source_ids:[`arrangement:${arrangementSha}:player:${i+1}`],assignment:'existing assigned source events; see bound command ledger',
      adaptations:['Existing adaptations retained in the bound arrangement; capture changes none'],newly_composed:false,source_kind:'source_event_lineage'}})),
    combined:{file:file(audio.at(-1)),mix_point:'linear_pre_master',active_stem_ids:notes.map((_,i)=>`performer-${i+1}`),quantization:'sum_retained_pcm16_then_nearest_ties_even',tolerance_lsb:1}};
  const manifestBytes=utf8(manifest),manifestSha=digest(manifestBytes);
  const reference={version:'acoustic-stem-store-v1',bundle_id:manifestSha,manifest:{bytes:manifestBytes.byteLength,sha256:manifestSha},renderer_receipt:{bytes:1234,sha256:binding.renderer.receipt_sha256},verification:{bytes:1000,sha256:'7'.repeat(64)},audio};
  const job={id:jobId,state:'complete',request:{kind:'mixed',asset_id:sourceId,sha256:sourceSha,start_seconds:ORIGIN/RATE,end_seconds:(ORIGIN+11)/RATE,performers,acoustic_companion_version:VERSION},
    result:{asset:{id:assetId,source_id:sourceId,sha256:audio.at(-1).sha256,size:audio.at(-1).bytes},arrangement,
      metrics:{frames:FRAMES,sample_rate:RATE,source_origin_sample:ORIGIN},acoustic_companion:{version:VERSION,status:'available',reference,arrangement_sha256:arrangementSha}}};
  const metadata={version:VERSION,manifest,manifest_sha256:manifestSha,binding:clone(binding),media:Object.fromEntries(audio.map(row=>[row.name,{...row,url:`/api/jobs/${jobId}/parts/${row.name}`}]))};
  for(const row of Object.values(metadata.media))delete row.name;
  return {job,metadata,files};
}
function response(bytes,headers={}){return new Response(bytes,{headers:{'Content-Length':String(bytes.byteLength),...headers}})}
function fetching(f){const calls=[];return {calls,fetcher:async(url,options)=>{calls.push({url,options});if(url.endsWith('/parts'))return response(utf8(JSON.stringify(f.metadata)));const name=url.split('/').at(-1);if(!f.files.has(name))return new Response('missing',{status:404});return response(f.files.get(name))}}}
class Param{events=[];setValueAtTime(value,time){this.events.push({value,time})}linearRampToValueAtTime(value,time){this.events.push({value,time})}cancelScheduledValues(){}}
class Node{connections=[];connect(n){this.connections.push(n)}disconnect(){this.connections=[]}}
class Context{
  sampleRate=48000;currentTime=0;state='suspended';destination=new Node();sources=[];gains=[];buffers=[];resumes=0;closes=0;
  createBuffer(channels,length,rate){const data=Array.from({length:channels},()=>new Float32Array(length));const buffer={numberOfChannels:channels,length,sampleRate:rate,duration:length/rate,getChannelData:i=>data[i]};this.buffers.push(buffer);return buffer}
  async resume(){this.resumes++;if(this.resumeWait)await this.resumeWait.promise;this.state='running'}
  close(){this.closes++;this.state='closed';return Promise.resolve()}
  createGain(){const n=new Node();n.gain=new Param();this.gains.push(n);return n}
  createBufferSource(){const n=new Node();Object.assign(n,{buffer:null,stopped:false,start(when,offset){this.when=when;this.offset=offset},stop(){this.stopped=true}});this.sources.push(n);return n}
}
function rig(f=fixture(),extra={}){
  const fetch=fetching(f),contexts=[],ticks=new Map(),events=[];let next=0;
  const player=createAcousticParts({fetcher:fetch.fetcher,crypto:webcrypto,contextFactory:()=>{events.push('context');const c=new Context();contexts.push(c);return c},
    beforePlay:()=>events.push('pause-main'),setTick:fn=>{ticks.set(++next,fn);return next},clearTick:id=>ticks.delete(id),...extra});
  return {player,contexts,ticks,events,fetch,f,tick(){for(const fn of [...ticks.values()])fn()}};
}

test('pure metadata binding and byte validation need no DOM or AudioContext',async()=>{
  const f=fixture(),trusted=committedContext(f.job);assert.equal(trusted.binding.arrangement.sha256,f.job.result.acoustic_companion.arrangement_sha256);
  const accepted=await validateMetadata(f.metadata,trusted,webcrypto);assert.deepEqual(accepted,f.metadata);
  for(const stem of accepted.manifest.stems)assert.equal(await validatePartBytes(f.files.get(stem.file.name),stem.file,accepted.manifest.clock,webcrypto),f.files.get(stem.file.name));
  f.job.result.arrangement.notes[0].role='harmony';assert.equal(trusted.roles[0].role,'melody','trusted snapshot is detached');
});
test('load verifies encoded files without context, nodes, resume or fetching combined master',async()=>{
  const r=rig();await r.player.load(r.f.job);assert.equal(r.player.snapshot().status,'ready');assert.equal(r.contexts.length,0);assert.equal(r.fetch.calls.length,3);
  assert.ok(r.fetch.calls.every(c=>c.options.cache==='no-store'&&c.options.credentials==='same-origin'&&c.options.redirect==='error'));
  assert.ok(r.fetch.calls.every(c=>!c.url.endsWith('/combined.wav')));r.player.dispose();
});
test('explicit play preserves44100Hz PCM on48000Hz context and one common source start',async()=>{
  const r=rig();await r.player.load(r.f.job);await r.player.play();const c=r.contexts[0];
  assert.deepEqual(r.events,['pause-main','context']);assert.equal(c.resumes,1);assert.equal(c.sources.length,2);
  assert.ok(c.buffers.every(b=>b.sampleRate===44100&&b.length===FRAMES&&b.numberOfChannels===2));
  assert.equal(c.buffers[0].getChannelData(0)[0],1000/32768);assert.equal(c.buffers[1].getChannelData(1)[FRAMES-1],-1000/32768);
  assert.equal(c.sources[0].when,c.sources[1].when);assert.equal(c.sources[0].offset,c.sources[1].offset);r.player.dispose();assert.equal(c.closes,1);
});
test('pause/resume, seek and complete shared tail use the existing audio clock',async()=>{
  const r=rig();await r.player.load(r.f.job);await r.player.seek(.2);assert.equal(r.contexts.length,0);await r.player.play();const c=r.contexts[0];assert.equal(c.sources[0].offset,.2);
  c.currentTime=.32;r.player.pause();const paused=r.player.snapshot().position;assert.ok(paused>.49&&paused<.51);assert.equal(r.ticks.size,0);
  await r.player.play();assert.equal(c.sources[2].offset,paused);await r.player.seek(.7);assert.equal(c.sources.at(-1).offset,.7);
  c.currentTime=2;r.tick();assert.equal(r.player.snapshot().status,'ended');assert.equal(r.player.snapshot().position,FRAMES/RATE);assert.equal(r.ticks.size,0);r.player.dispose();
});
test('all-muted Duo keeps advancing and never changes its arrangement identity',async()=>{
  const r=rig();await r.player.load(r.f.job);r.player.setMuted('performer-1',true);r.player.setMuted('performer-2',true);await r.player.play();const c=r.contexts[0];
  c.currentTime=.4;r.tick();assert.equal(r.player.snapshot().allMuted,true);assert.ok(r.player.snapshot().position>.3);assert.equal(r.player.snapshot().label,'Duo mix');
  r.player.all();assert.ok(r.player.snapshot().parts.every(p=>p.audible));assert.equal(r.player.snapshot().label,'Duo mix');r.player.dispose();
});
test('committed reference, source, final arrangement, clock and master mismatches refuse before fetch',async()=>{
  const mutations=[j=>j.request.sha256='x',j=>j.request.asset_id='../original',j=>j.request.instrument='electric',j=>delete j.request.acoustic_companion_version,
    j=>j.result.asset.source_id='another-source',j=>j.result.asset.sha256='1'.repeat(64),j=>j.result.arrangement.performers=1,
    j=>j.result.arrangement.notes.pop(),j=>j.result.arrangement.notes[0].start=ORIGIN-1,j=>j.result.metrics.frames--,
    j=>delete j.result.acoustic_companion.arrangement_sha256,j=>j.result.acoustic_companion.reference.audio[0].name='../performer-1.wav',
    j=>j.result.acoustic_companion.reference.manifest.bytes=true];
  for(const change of mutations){const f=fixture();change(f.job);const r=rig(f);await assert.rejects(r.player.load(f.job));assert.equal(r.fetch.calls.length,0);assert.equal(r.contexts.length,0);r.player.dispose()}
});
test('tampered manifest, binding, route, missing part and committed arrangement SHA are rejected',async()=>{
  for(const change of [f=>f.metadata.manifest.stems.pop(),f=>f.metadata.binding.clock.origin_sample++,f=>f.metadata.manifest_sha256='f'.repeat(64),
    f=>f.metadata.media['performer-1.wav'].url='https://example.invalid/audio',f=>delete f.metadata.media['performer-2.wav'],
    f=>f.metadata.manifest.stems[0].role='harmony',f=>f.job.result.acoustic_companion.arrangement_sha256='e'.repeat(64)]){
    const f=fixture();change(f);const r=rig(f);await assert.rejects(r.player.load(f.job));assert.equal(r.contexts.length,0);r.player.dispose();
  }
});
test('encoded truncation, extra bytes, bad hash, bad WAV geometry and oversized metadata leave master fallback',async()=>{
  for(const change of [f=>f.files.set('performer-1.wav',f.files.get('performer-1.wav').slice(0,-1)),
    f=>{const old=new Uint8Array(f.files.get('performer-1.wav'));const bytes=new Uint8Array(old.length+1);bytes.set(old);f.files.set('performer-1.wav',bytes.buffer)},
    f=>new Uint8Array(f.files.get('performer-1.wav'))[100]++,
    f=>{new DataView(f.files.get('performer-1.wav')).setUint32(24,48000,true)}]){
    const f=fixture();change(f);const r=rig(f);await assert.rejects(r.player.load(f.job));assert.equal(r.player.snapshot().ready,false);assert.equal(r.contexts.length,0);r.player.dispose();
  }
  const r=rig(fixture(),{fetcher:async()=>response(new Uint8Array(193*1024))});await assert.rejects(r.player.load(r.f.job),/length|limit/);assert.equal(r.contexts.length,0);r.player.dispose();
  const f=fixture();const bytes=f.files.get('performer-1.wav');new DataView(bytes).setUint32(24,48000,true);
  await assert.rejects(validatePartBytes(bytes,{bytes:bytes.byteLength,sha256:digest(bytes)},f.metadata.manifest.clock,webcrypto),/geometry/);
});
test('metadata and media streams are bounded even when Content-Length is missing or false',async()=>{
  const f=fixture(),base=fetching(f);const r=rig(f,{fetcher:async(url,options)=>{
    if(url.endsWith('/parts'))return base.fetcher(url,options);
    const expected=f.files.get(url.split('/').at(-1));return new Response(new ReadableStream({start(c){c.enqueue(new Uint8Array(expected));c.enqueue(new Uint8Array([1]));c.close()}}));
  }});await assert.rejects(r.player.load(f.job),/limit/);assert.equal(r.contexts.length,0);r.player.dispose();
});
test('stale loads abort and cannot replace a newer result or create audio',async()=>{
  const f=fixture(),gate=deferred(),base=fetching(f);let firstSignal,first=true;
  const r=rig(f,{fetcher:async(url,options)=>{if(first){first=false;firstSignal=options.signal;return gate.promise}return base.fetcher(url,options)}});
  const old=r.player.load(f.job);await flush();await r.player.load(f.job);assert.equal(firstSignal.aborted,true);gate.resolve(response(utf8(JSON.stringify(f.metadata))));assert.equal(await old,false);
  assert.equal(r.player.snapshot().status,'ready');assert.equal(r.contexts.length,0);r.player.dispose();
});
test('a stalled load has a bounded deadline and failure keeps audio uncreated',async()=>{
  let timeout,cleared=0;
  const r=rig(fixture(),{setDeadline:fn=>{timeout=fn;return 1},clearDeadline:()=>cleared++,fetcher:async(url,{signal})=>
    new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('authored timed-out fetch')),{once:true}))});
  const loading=r.player.load(r.f.job);await flush();timeout();await assert.rejects(loading,/timed-out/);
  assert.equal(r.player.snapshot().status,'error');assert.equal(r.contexts.length,0);assert.equal(cleared,1);r.player.dispose();
});
test('disposing a pending resume prevents late sources and closes only the owned context',async()=>{
  const wait=deferred(),c=new Context();c.resumeWait=wait;const r=rig(fixture(),{contextFactory:()=>c});await r.player.load(r.f.job);
  const playing=r.player.play();await flush();assert.equal(c.resumes,1);r.player.dispose();wait.resolve();assert.equal(await playing,false);assert.equal(c.sources.length,0);assert.equal(c.closes,1);assert.equal(r.player.snapshot().status,'disposed');r.player.dispose();assert.equal(c.closes,1);
});
test('pause invalidates a pending play and a fresh explicit play can proceed',async()=>{
  const wait=deferred(),c=new Context();c.resumeWait=wait;const r=rig(fixture(),{contextFactory:()=>c});await r.player.load(r.f.job);
  const first=r.player.play();await flush();r.player.pause();const second=r.player.play();await flush();wait.resolve();assert.equal(await first,false);assert.equal(await second,true);assert.equal(c.sources.length,2);r.player.dispose();
});
test('pause during the first preseek handoff cannot start a stale hidden generation',async()=>{
  const r=rig();await r.player.load(r.f.job);await r.player.seek(.2);
  const playing=r.player.play();await Promise.resolve();r.player.pause();assert.equal(await playing,false);
  assert.equal(r.contexts[0].resumes,0);assert.equal(r.contexts[0].sources.length,0);assert.equal(r.player.snapshot().playing,false);r.player.dispose();
});
test('a failed explicit context start releases resources and leaves a retryable loaded preview',async()=>{
  let attempts=0;const c=new Context(),r=rig(fixture(),{contextFactory:()=>{if(!attempts++)throw Error('authored construction failure');return c}});
  await r.player.load(r.f.job);await assert.rejects(r.player.play(),/construction/);assert.equal(r.player.snapshot().status,'ready');assert.equal(await r.player.play(),true);r.player.dispose();
});

test('unload stops audio and clears data while allowing a freshly verified reload',async()=>{
  const r=rig();await r.player.load(r.f.job);await r.player.play();const c=r.contexts[0];r.player.setMuted('performer-2',true);
  r.player.unload();const empty=r.player.snapshot();assert.equal(empty.status,'empty');assert.equal(empty.ready,false);assert.equal(empty.playing,false);
  assert.deepEqual(empty.parts,[]);assert.equal(empty.duration,0);assert.equal(empty.position,0);assert.equal(c.closes,1);assert.ok(c.sources.every(s=>s.stopped));assert.equal(r.ticks.size,0);
  assert.throws(()=>r.player.play(),/Load/);assert.throws(()=>r.player.setMuted('performer-2',false),/unavailable/);
  await r.player.load(r.f.job);assert.equal(r.fetch.calls.length,6);assert.equal(r.contexts.length,1);assert.ok(r.player.snapshot().parts.every(p=>p.audible));
  await r.player.play();assert.equal(r.contexts.length,2);r.player.dispose();await assert.rejects(r.player.load(r.f.job),/closed/);r.player.unload();assert.equal(r.player.snapshot().status,'disposed');
});

test('unload aborts an in-flight load and fences a pending resume without hidden playback',async()=>{
  const f=fixture(),late=deferred(),fetch=fetching(f);let request;
  const r=rig(f,{fetcher:(url,options)=>{if(!request){request=options;return late.promise}return fetch.fetcher(url,options)}});
  const loading=r.player.load(f.job);await flush();r.player.unload();assert.equal(request.signal.aborted,true);
  late.resolve(response(utf8(JSON.stringify(f.metadata))));assert.equal(await loading,false);assert.equal(r.player.snapshot().status,'empty');assert.equal(r.contexts.length,0);
  const wait=deferred(),c=new Context();c.resumeWait=wait;
  const pending=rig(f,{contextFactory:()=>c});await pending.player.load(f.job);const playing=pending.player.play();await flush();pending.player.unload();
  wait.resolve();assert.equal(await playing,false);assert.equal(c.sources.length,0);assert.equal(c.closes,1);assert.equal(pending.player.snapshot().ready,false);assert.equal(pending.ticks.size,0);
  pending.player.dispose();r.player.dispose();
});

class Element{
  children=[];attributes={};textContent='';disabled=false;hidden=false;value='';
  constructor(tag){this.tagName=tag}
  append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n)}}
  setAttribute(k,v){this.attributes[k]=v}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this)}
}
const document={createElement:tag=>new Element(tag)};
function nodes(root){return [root,...root.children.flatMap(nodes)]}
test('visible controls load then play, keep Duo naming when muted, and dispose without removing master fallback',async()=>{
  const f=fixture(),root=new Element('div'),master=new Element('button');master.textContent='Play acoustic preview';root.append(master);
  const fetched=fetching(f),c=new Context(),ticks=new Map();let id=0,paused=0;
  const mounted=mountAcousticParts({job:f.job,root,capability:VERSION,document,fetcher:fetched.fetcher,crypto:webcrypto,contextFactory:()=>c,
    beforePlay:()=>paused++,setTick:fn=>{ticks.set(++id,fn);return id},clearTick:id=>ticks.delete(id)});
  const button=label=>nodes(root).find(n=>n.tagName==='button'&&n.textContent===label);
  assert.equal(fetched.calls.length,0);assert.equal(c.resumes,0);await button('Load saved parts').onclick();assert.equal(c.resumes,0);assert.equal(button('Play parts').disabled,false);
  button('Player 2 · bass').onclick();assert.equal(button('Player 2 · bass').attributes['aria-pressed'],'false');assert.ok(nodes(root).some(n=>n.textContent==='Duo mix'));
  button('Play parts').onclick();await flush();assert.equal(paused,1);assert.equal(c.sources.length,2);button('Pause parts').onclick();assert.equal(ticks.size,0);
  mounted.dispose();assert.deepEqual(root.children,[master]);assert.equal(c.closes,1);
});

test('actual main-play and comparison handoffs retain an inert panel that can Load and Play again',async()=>{
  const f=fixture(),root=new Element('div'),master=new Element('button');root.append(master);const fetch=fetching(f),contexts=[],ticks=new Map();let timer=0,mainStarts=0,comparisonStarts=0;
  const mounted=mountAcousticParts({job:f.job,root,capability:VERSION,document,fetcher:fetch.fetcher,crypto:webcrypto,
    contextFactory:()=>{const c=new Context();contexts.push(c);return c},setTick:fn=>{ticks.set(++timer,fn);return timer},clearTick:id=>ticks.delete(id)});
  const button=label=>nodes(root).find(n=>n.tagName==='button'&&n.textContent===label),section=root.children[1];
  const app=fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
  const lifecycle=app.slice(app.indexOf('function disposeAcousticParts(){'),app.indexOf('function pausePlayback(){'));
  const claim=app.slice(app.indexOf('function claimResult('),app.indexOf('async function viewSavedJob('));
  const main=app.slice(app.indexOf('async function play(index){'),app.indexOf('async function refresh(){'));
  const compare=app.match(/button\('Compare original passage',(\(\)=>\{if\(refinementStarting\)return;claimResult\(\{keepParts:true\}\);return comparison\.start\(passage\)\})\)/)[1];
  const scope={acousticParts:mounted,queue:['master'],entryAt:()=>({track:{url:'/api/media/master'}}),playGeneration:0,transport:null,audio:{pause(){}},objectUrl:null,
    engineWithFallback:async()=>true,configureEngine(){},useNativeEngine(){},status(){},$:()=>({value:'native'}),session:async()=>{},
    playNative:async()=>{mainStarts++;assert.equal(mounted.snapshot().ready,false)},refinementStarting:false,passage:{},
    comparison:{stop(){},start(){comparisonStarts++;assert.equal(mounted.snapshot().ready,false)}}};
  vm.createContext(scope);vm.runInContext(lifecycle+'let resultGeneration=0;'+claim+main+'globalThis.compareClick='+compare,scope);
  await button('Load saved parts').onclick();button('Play parts').onclick();await flush();const first=contexts[0],oldRole=button('Player 2 · bass'),oldHandler=oldRole.onclick;
  await scope.play(0);assert.equal(mainStarts,1);assert.equal(scope.acousticParts,mounted);assert.equal(root.children[1],section);assert.equal(first.closes,1);assert.ok(first.sources.every(s=>s.stopped));assert.equal(ticks.size,0);
  assert.equal(button('Load saved parts').hidden,false);assert.equal(button('Load saved parts').disabled,false);assert.equal(button('Play parts').disabled,true);assert.equal(button('All').disabled,true);
  assert.equal(nodes(root).find(n=>n.tagName==='input').disabled,true);assert.equal(oldRole.disabled,true);assert.equal(button('Player 2 · bass'),undefined);
  oldHandler();oldRole.onclick();button('Play parts').onclick();button('All').onclick();assert.equal(contexts.length,1);
  await button('Load saved parts').onclick();assert.equal(contexts.length,1);button('Play parts').onclick();await flush();const second=contexts[1];
  scope.compareClick();assert.equal(comparisonStarts,1);assert.equal(vm.runInContext('resultGeneration',scope),1);assert.equal(scope.acousticParts,mounted);assert.equal(root.children[1],section);
  assert.equal(second.closes,1);assert.ok(second.sources.every(s=>s.stopped));assert.equal(ticks.size,0);assert.equal(button('Load saved parts').hidden,false);
  await button('Load saved parts').onclick();assert.equal(contexts.length,2);button('Play parts').onclick();await flush();assert.equal(contexts.length,3);assert.equal(mounted.snapshot().playing,true);
  mounted.dispose();assert.deepEqual(root.children,[master]);assert.equal(contexts[2].closes,1);
});
test('old capability, legacy and unavailable jobs show no controls and never fetch',()=>{
  const f=fixture(),root=new Element('div');for(const capability of [undefined,'old'])assert.equal(mountAcousticParts({job:f.job,root,capability,document}),null);
  delete f.job.result.acoustic_companion;assert.equal(mountAcousticParts({job:f.job,root,capability:VERSION,document}),null);
  f.job.result.acoustic_companion={version:VERSION,status:'unavailable'};assert.equal(mountAcousticParts({job:f.job,root,capability:VERSION,document}),null);assert.equal(root.children.length,0);
});
test('app lifecycle and shell27 wire every main engine and preserve Electric gate and unchanged modules',()=>{
  const app=fs.readFileSync(new URL('../web/app.js',import.meta.url),'utf8'),html=fs.readFileSync(new URL('../web/index.html',import.meta.url),'utf8'),sw=fs.readFileSync(new URL('../web/sw.js',import.meta.url),'utf8');
  assert.match(app,/ELECTRIC_PREVIEW_ENABLED=false/);assert.match(app,/acoustic-parts\.mjs\?v=27/);assert.match(app,/transport\.mjs\?v=22/);
  for(const event of ['pagehide','play','seeking'])assert.ok(app.includes("'"+event+"'"));
  assert.match(app,/audio\.addEventListener\('play',[^\n]+releaseAcousticParts/);
  assert.match(app,/transportToggle'\)\.onclick=\(\)=>\{releaseAcousticParts/);
  assert.match(app,/function claimResult\(\{keepParts=false\}=\{\}\)\{if\(keepParts\)releaseAcousticParts\(\);else disposeAcousticParts/);assert.match(app,/async function play\(index\)\{\s*releaseAcousticParts/);
  assert.match(app,/releaseAcousticParts\(\);comparison\.stop\(\);pausePlayback\(\);restoreBusy=true/);
  assert.match(app,/setTimeout\(\(\)=>\{releaseAcousticParts\(\);comparison\.stop\(\);pausePlayback/);
  assert.match(app,/pagehide',[^\n]+disposeAcousticParts/);assert.match(app,/mountCapture\([^\n]+releaseAcousticParts/);
  assert.match(html,/app\.js\?v=27/);assert.match(html,/style\.css\?v=27/);assert.match(sw,/wood-shell-v27/);
  assert.ok(sw.includes('/acoustic-parts.mjs?v=27')&&sw.includes('/stem-player.js')&&!sw.includes('/api/jobs/'));
});
