// Verified saved acoustic parts. Importing creates no audio context or nodes.
import {pcm16} from './transport.mjs?v=22';
import * as StemModule from './stem-player.js';
import {mixIdentity} from './acoustic-mix-store.mjs?v=28';

export const VERSION='acoustic-companion-v1';
const STORE_VERSION='acoustic-stem-store-v1', RATE=44100, MAX_FRAMES=31*RATE;
const MANIFEST_LIMIT=128*1024, METADATA_LIMIT=192*1024;
const AUDIO_LIMIT=2*(44+MAX_FRAMES*4), DECODED_LIMIT=2*MAX_FRAMES*8;
const encoder=new TextEncoder(), scheduler=globalThis.StemPlayer||StemModule.default;
const fail=(ok,message)=>{if(!ok)throw Error(message)};
const integer=(v,min=0,max=Number.MAX_SAFE_INTEGER)=>Number.isSafeInteger(v)&&v>=min&&v<=max;
const sha=v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v);
const uuid=v=>typeof v==='string'&&/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(v);
const text=v=>typeof v==='string'&&v.length>0&&v.length<=128&&!/[\x00-\x1f\ud800-\udfff]/u.test(v);
function shape(value,keys){fail(value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join('|')===[...keys].sort().join('|'),'Saved parts metadata has changed.');}
export function canonical(value){
  if(value===null||typeof value!=='object'){fail(typeof value!=='number'||Number.isFinite(value),'Invalid saved number.');return JSON.stringify(value)}
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';
}
const equal=(a,b)=>canonical(a)===canonical(b);
const copy=value=>JSON.parse(JSON.stringify(value));
async function digest(bytes,crypto){fail(crypto?.subtle,'Saved parts need a secure browser connection.');return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('')}
function roundEven(v){const lo=Math.floor(v),fraction=v-lo;return fraction===.5?(lo%2?lo+1:lo):Math.round(v)}

export function committedContext(job){
  fail(job?.state==='complete'&&uuid(job.id),'Choose a complete saved preview.');
  const r=job.request,a=job.result?.arrangement,asset=job.result?.asset,c=job.result?.acoustic_companion;
  fail(r&&['mixed','refine','instrumental-arrange'].includes(r.kind)&&r.acoustic_companion_version===VERSION&&
    (!r.instrument||r.instrument==='acoustic')&&integer(r.performers,1,2)&&uuid(r.asset_id),'This preview has no saved acoustic parts.');
  shape(c,['version','status','reference','arrangement_sha256']);
  fail(c.version===VERSION&&c.status==='available'&&sha(c.arrangement_sha256),'Saved parts are unavailable.');
  fail(a&&(!a.instrument||a.instrument==='acoustic')&&a.sample_rate===RATE&&a.performers===r.performers&&
    text(a.source_id)&&uuid(asset?.id)&&asset.source_id===a.source_id&&sha(r.sha256)&&sha(asset.sha256)&&asset.sha256!==r.sha256&&
    integer(a.render_origin_sample)&&Array.isArray(a.notes)&&a.notes.length>0&&a.notes.length<=4000,
    'The saved arrangement no longer matches this preview.');
  fail(Number.isFinite(r.start_seconds)&&Number.isFinite(r.end_seconds)&&r.start_seconds>=0&&r.end_seconds>r.start_seconds&&
    r.end_seconds-r.start_seconds<=30&&roundEven(r.start_seconds*RATE)===a.render_origin_sample,'The saved passage clock has changed.');
  const origin=a.render_origin_sample,boundary=roundEven(r.end_seconds*RATE),ids=new Set(),players=new Set(),roles=new Map();
  const ledger=a.refinement_source||a.source;
  fail(a.source?.source_id===a.source_id&&a.source?.sample_rate===RATE&&ledger?.source_id===a.source_id&&
    ledger?.sample_rate===RATE&&Array.isArray(ledger.notes),'The saved source ledger has changed.');
  const sourceIds=new Set(ledger.notes.map(n=>n.id));
  for(const n of a.notes){
    fail(text(n.id)&&!ids.has(n.id)&&sourceIds.has(n.id)&&integer(n.start,origin,boundary-1)&&integer(n.end,n.start+1,boundary)&&
      integer(n.pitch,0,127)&&integer(n.velocity,1,127)&&integer(n.player,1,a.performers)&&integer(n.string,0,5)&&
      integer(n.fret,0,36)&&['melody','bass','harmony'].includes(n.role),'The saved acoustic commands have changed.');
    ids.add(n.id);players.add(n.player);if(!roles.has(n.player))roles.set(n.player,new Set());roles.get(n.player).add(n.role);
  }
  fail(players.size===a.performers,'A saved player is missing.');
  const frames=Math.max(...a.notes.map(n=>n.end))-origin+RATE;
  fail(integer(frames,RATE+1,MAX_FRAMES)&&integer(asset.size,44+frames*4,44+frames*4)&&
    job.result.metrics?.sample_rate===RATE&&job.result.metrics?.frames===frames&&job.result.metrics?.source_origin_sample===origin,
    'The saved render clock has changed.');
  const reference=c.reference;
  shape(reference,['version','bundle_id','manifest','renderer_receipt','verification','audio']);
  fail(reference.version===STORE_VERSION&&sha(reference.bundle_id),'Saved parts identity is invalid.');
  for(const [key,max] of [['manifest',MANIFEST_LIMIT],['renderer_receipt',2*1024*1024],['verification',16*1024]]){
    shape(reference[key],['bytes','sha256']);fail(integer(reference[key].bytes,1,max)&&sha(reference[key].sha256),'Saved parts reference is invalid.');
  }
  fail(reference.bundle_id===reference.manifest.sha256&&Array.isArray(reference.audio)&&reference.audio.length===a.performers+1,'Saved parts are incomplete.');
  const filenames=[...Array(a.performers)].map((_,i)=>`performer-${i+1}.wav`).concat('combined.wav');
  reference.audio.forEach((row,i)=>{shape(row,['name','bytes','sha256']);fail(row.name===filenames[i]&&row.bytes===44+frames*4&&sha(row.sha256),'Saved parts reference has changed.')});
  fail(reference.audio.at(-1).sha256===asset.sha256,'The saved parts do not belong to this master preview.');
  return copy({jobId:job.id,reference,roles:[...roles].sort(([a],[b])=>a-b).map(([player,values])=>({id:`performer-${player}`,role:[...values].sort().join(' and ')})),
    binding:{source:{id:a.source_id,sha256:r.sha256},clock:{sample_rate:RATE,origin_sample:origin,body_frames:frames-RATE,tail_frames:RATE,frames},
      arrangement:{revision:job.id,sha256:c.arrangement_sha256,performers:a.performers,label:a.performers===1?'Acoustic Solo':'Acoustic Duo'},
      renderer:{id:'sfizz-meta-acoustic',version:'sfizz-freewheel-live-quality-v1',receipt_sha256:reference.renderer_receipt.sha256,
        source_kind:'synthesized',content_exclusion:'declared_not_verified'}}});
}

export async function validateMetadata(metadata,trusted,crypto=globalThis.crypto){
  shape(metadata,['version','manifest','manifest_sha256','binding','media']);
  fail(metadata.version===VERSION&&metadata.manifest_sha256===trusted.reference.bundle_id&&equal(metadata.binding,trusted.binding),'Saved parts belong to another preview.');
  const m=metadata.manifest;shape(m,['version','source','clock','arrangement','renderer','stems','combined']);
  fail(m.version==='stem-bundle-v1'&&['source','clock','arrangement','renderer'].every(k=>equal(m[k],trusted.binding[k])),'Saved parts binding has changed.');
  const encoded=encoder.encode(canonical(m));
  fail(encoded.byteLength===trusted.reference.manifest.bytes&&encoded.byteLength<=MANIFEST_LIMIT&&
    await digest(encoded,crypto)===trusted.reference.bundle_id,'Saved parts manifest verification failed.');
  const count=trusted.binding.arrangement.performers,frames=trusted.binding.clock.frames;
  fail(Array.isArray(m.stems)&&m.stems.length===count,'A saved player is missing.');
  shape(m.combined,['file','mix_point','active_stem_ids','quantization','tolerance_lsb']);
  fail(m.combined.mix_point==='linear_pre_master'&&m.combined.quantization==='sum_retained_pcm16_then_nearest_ties_even'&&m.combined.tolerance_lsb===1&&
    equal(m.combined.active_stem_ids,trusted.roles.map(r=>r.id)),'The saved default mix has changed.');
  const members=[...m.stems.map(s=>s.file),m.combined.file];
  members.forEach((file,i)=>{shape(file,['name','bytes','sha256','format','channels','sample_rate','frames']);
    fail(file.name===trusted.reference.audio[i].name&&file.bytes===44+frames*4&&file.sha256===trusted.reference.audio[i].sha256&&
      file.sha256!==trusted.binding.source.sha256&&file.format==='pcm_s16le'&&file.channels===2&&file.sample_rate===RATE&&file.frames===frames,'Saved WAV identity has changed.')});
  m.stems.forEach((stem,i)=>{
    shape(stem,['id','instrument','role','gain','default_audible','file','provenance']);
    fail(stem.id===trusted.roles[i].id&&stem.role===trusted.roles[i].role&&stem.instrument==='acoustic'&&stem.gain===1&&stem.default_audible===true,'The saved player identity has changed.');
    shape(stem.provenance,['source_ids','assignment','adaptations','newly_composed','source_kind']);
    fail(equal(stem.provenance.source_ids,[`arrangement:${trusted.binding.arrangement.sha256}:player:${i+1}`])&&stem.provenance.newly_composed===false&&
      stem.provenance.source_kind==='source_event_lineage','Saved player provenance has changed.');
  });
  shape(metadata.media,members.map(r=>r.name));
  for(const file of members){const row=metadata.media[file.name];shape(row,['url','bytes','sha256']);
    fail(row.url===`/api/jobs/${trusted.jobId}/parts/${file.name}`&&row.bytes===file.bytes&&row.sha256===file.sha256,'Saved media route has changed.')}
  fail(m.stems.reduce((sum,s)=>sum+s.file.bytes,0)<=AUDIO_LIMIT&&count*frames*8<=DECODED_LIMIT,'Saved parts exceed this player limit.');
  return copy(metadata);
}

async function boundedBytes(response,limit,expected,signal){
  fail(response.ok&&response.status===200,'Saved parts could not be loaded.');
  const length=response.headers?.get('content-length');
  if(length!==null&&length!==undefined){fail(/^\d+$/.test(length)&&integer(Number(length),1,limit)&&(expected===undefined||Number(length)===expected),'Saved file length has changed.')}
  fail(response.body?.getReader,'This browser cannot safely load saved parts.');
  const reader=response.body.getReader(),chunks=[];let count=0;
  try{while(true){if(signal.aborted)throw Error('Parts loading cancelled.');const {done,value}=await reader.read();if(done)break;
      count+=value.byteLength;fail(count<=limit&&(expected===undefined||count<=expected),'Saved file exceeds its byte limit.');chunks.push(value)}
    fail(expected===undefined||count===expected,'Saved file is incomplete.');
    const output=new Uint8Array(count);let at=0;for(const chunk of chunks){output.set(chunk,at);at+=chunk.byteLength}return output.buffer;
  }finally{try{await reader.cancel()}catch{}reader.releaseLock?.()}
}
function wavGeometry(bytes,frames){
  fail(bytes.byteLength===44+frames*4,'Saved WAV length has changed.');
  const view=new DataView(bytes),word=(at,size)=>String.fromCharCode(...new Uint8Array(bytes,at,size));
  fail(word(0,4)==='RIFF'&&view.getUint32(4,true)===bytes.byteLength-8&&word(8,8)==='WAVEfmt '&&view.getUint32(16,true)===16&&
    view.getUint16(20,true)===1&&view.getUint16(22,true)===2&&view.getUint32(24,true)===RATE&&view.getUint32(28,true)===RATE*4&&
    view.getUint16(32,true)===4&&view.getUint16(34,true)===16&&word(36,4)==='data'&&view.getUint32(40,true)===frames*4,'Saved WAV geometry has changed.');
}
export async function validatePartBytes(bytes,file,clock,crypto=globalThis.crypto){
  fail(bytes instanceof ArrayBuffer&&bytes.byteLength===file.bytes&&bytes.byteLength<=44+MAX_FRAMES*4&&sha(file.sha256),'Saved part length has changed.');
  fail(await digest(bytes,crypto)===file.sha256,'Saved part verification failed.');wavGeometry(bytes,clock.frames);return bytes;
}

export function createAcousticParts({fetcher=globalThis.fetch,crypto=globalThis.crypto,session=async()=>{},beforePlay=()=>{},onChange=()=>{},
  contextFactory=()=>new(globalThis.AudioContext||globalThis.webkitAudioContext)(),setTick=fn=>setInterval(fn,100),clearTick=clearInterval,
  setDeadline=(fn,ms)=>setTimeout(fn,ms),clearDeadline=clearTimeout,mixStore}={}){
  let generation=0,operation=0,controller,metadata,trusted,encoded,context,engine,timer,deadline,flight,status='empty',error='',offset=0;
  let mixStatus='default',mixNotice='',mixRevision=0;
  const muted=new Set();
  function snapshot(){return {status,error,ready:Boolean(metadata),duration:metadata?metadata.manifest.clock.frames/RATE:0,
    position:engine?engine.position():offset,playing:engine?.state==='playing',allMuted:metadata?muted.size===metadata.manifest.stems.length:false,
    mixStatus,mixNotice,label:metadata?.manifest.arrangement.performers===2?'Duo mix':'Solo mix',parts:metadata?.manifest.stems.map(s=>({id:s.id,role:s.role,audible:!muted.has(s.id)}))||[]}}
  const emit=()=>onChange(snapshot());
  function stopTick(){if(timer!==undefined){clearTick(timer);timer=undefined}}
  function tick(){if(engine?.state==='ended'){status='ended';stopTick()}emit()}
  function closeAudio(){stopTick();engine?.dispose();engine=null;const old=context;context=null;if(old)Promise.resolve(old.close()).catch(()=>{})}
  function retire(next){generation++;operation++;mixRevision++;controller?.abort();controller=null;if(deadline!==undefined){clearDeadline(deadline);deadline=undefined}closeAudio();metadata=null;trusted=null;encoded=null;flight=null;muted.clear();offset=0;error='';mixStatus='default';mixNotice='';status=next}
  async function rememberedMix(identity,signal){
    let abort;
    const interrupted=new Promise((resolve,reject)=>{abort=()=>reject(Error('Mix settings loading cancelled.'));signal.addEventListener('abort',abort,{once:true});if(signal.aborted)abort()});
    try{
      const value=await Promise.race([Promise.resolve().then(()=>mixStore.load(identity)),interrupted]);
      fail(value===null||(Array.isArray(value)&&value.length<=identity.stem_ids.length),'Saved mix settings have changed.');
      if(value===null)return null;
      const selected=[];
      for(let index=0;index<value.length;index++){
        const entry=Object.getOwnPropertyDescriptor(value,String(index));
        fail(entry&&Object.hasOwn(entry,'value')&&typeof entry.value==='string'&&identity.stem_ids.includes(entry.value)&&!selected.includes(entry.value),'Saved mix settings have changed.');
        selected.push(entry.value);
      }
      return selected;
    }finally{signal.removeEventListener('abort',abort)}
  }
  function saveMix(){
    if(!mixStore)return;
    const token=generation,revision=++mixRevision,identity=mixIdentity(trusted),ids=identity.stem_ids.filter(id=>muted.has(id));
    mixStatus='saving';mixNotice='Remembering your mix on this device…';
    let saved;try{saved=mixStore.save(identity,ids)}catch(e){saved=Promise.reject(e)}
    Promise.resolve(saved).then(()=>{
      if(token!==generation||revision!==mixRevision)return;
      mixStatus='saved';mixNotice='Your listening mix is remembered on this device.';emit();
    },()=>{
      if(token!==generation||revision!==mixRevision)return;
      mixStatus='unavailable';mixNotice='This mix works for now, but could not be remembered on this device.';emit();
    });
  }
  async function load(job){
    fail(status!=='disposed','This saved parts panel is closed.');
    retire('loading');const token=generation;controller=new AbortController();const request=controller,signal=controller.signal;
    deadline=setDeadline(()=>request.abort(),30000);emit();
    try{
      const expected=committedContext(job);await session(signal);if(token!==generation)return false;
      const response=await fetcher(`/api/jobs/${expected.jobId}/parts`,{cache:'no-store',credentials:'same-origin',redirect:'error',signal});
      const raw=await boundedBytes(response,METADATA_LIMIT,undefined,signal);if(token!==generation)return false;
      const value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(raw));const checked=await validateMetadata(value,expected,crypto);if(token!==generation)return false;
      const files=new Map();
      for(const stem of checked.manifest.stems){
        const row=checked.media[stem.file.name],response=await fetcher(row.url,{cache:'no-store',credentials:'same-origin',redirect:'error',signal});
        const bytes=await boundedBytes(response,row.bytes,row.bytes,signal);if(token!==generation)return false;
        await validatePartBytes(bytes,stem.file,checked.manifest.clock,crypto);if(token!==generation)return false;
        files.set(stem.id,bytes);
      }
      let remembered=null;
      if(mixStore){
        try{remembered=await rememberedMix(mixIdentity(expected),signal)}
        catch(e){if(token!==generation)return false;mixStatus='unavailable';mixNotice='Your saved mix could not be loaded. All players are on; choose a mix to remember it again.'}
        if(token!==generation)return false;
      }
      if(remembered!==null){for(const id of remembered)muted.add(id);mixStatus='saved';mixNotice='Your listening mix is remembered on this device.'}
      trusted=expected;metadata=checked;encoded=files;status='ready';clearDeadline(deadline);deadline=undefined;emit();return true;
    }catch(e){if(token!==generation)return false;retire('error');error=e.message;emit();throw e}
  }
  function pause(){operation++;flight=null;if(engine){engine.pause();offset=engine.position()}stopTick();if(metadata)status='paused';emit()}
  function play(){
    fail(metadata&&encoded&&status!=='disposed','Load the saved parts first.');if(flight)return flight;
    const token=generation,action=++operation;beforePlay();status='starting';error='';emit();
    const pending=(async()=>{await Promise.resolve();try{
        if(token!==generation||action!==operation)return false;
        if(!engine){context=contextFactory();const buffers=new Map();for(const [id,bytes] of encoded)buffers.set(id,pcm16(context,bytes));
          fail(scheduler?.createStemPlayer,'Saved parts player is unavailable.');
          engine=scheduler.createStemPlayer({context,manifest:metadata.manifest,buffers,maxDecodedBytes:DECODED_LIMIT,onEnded:()=>{if(token===generation){status='ended';stopTick();emit()}}});
          for(const id of muted)engine.setMuted(id,true);if(offset)await engine.seek(offset);
        }
        if(token!==generation||action!==operation)return false;
        const started=await engine.play();if(token!==generation||action!==operation)return false;
        status=started?'playing':'paused';if(started){stopTick();timer=setTick(tick)}emit();return started;
      }catch(e){if(token!==generation||action!==operation)return false;closeAudio();status='ready';error='Parts could not start. The normal preview is still available.';emit();throw e}
      finally{if(flight===pending)flight=null}})();flight=pending;return pending;
  }
  async function seek(seconds){
    fail(metadata&&Number.isFinite(seconds),'Load the saved parts first.');offset=Math.max(0,Math.min(metadata.manifest.clock.frames/RATE,seconds));
    if(!engine){emit();return false}
    const token=generation,action=++operation;const started=await engine.seek(offset);
    if(token!==generation||action!==operation)return false;status=engine.state;if(started){stopTick();timer=setTick(tick)}else stopTick();emit();return started;
  }
  function setMuted(id,value){fail(metadata?.manifest.stems.some(s=>s.id===id)&&typeof value==='boolean','That saved player is unavailable.');if(value)muted.add(id);else muted.delete(id);engine?.setMuted(id,value);saveMix();emit()}
  function all(){fail(metadata,'Load the saved parts first.');muted.clear();engine?.setAllMuted(false);saveMix();emit()}
  function unload(){if(status==='disposed')return;retire('empty');emit()}
  function dispose(){if(status==='disposed')return;retire('disposed');emit()}
  return {load,play,pause,seek,setMuted,all,unload,dispose,snapshot};
}

export function mountAcousticParts({job,root,capability,document:doc=globalThis.document,...options}){
  if(capability!==VERSION||job?.result?.acoustic_companion?.status!=='available')return null;
  const element=(tag,text)=>{const n=doc.createElement(tag);if(text!==undefined)n.textContent=text;return n};
  const section=element('section');section.className='acoustic-parts';section.setAttribute('aria-label','Saved acoustic parts');
  const title=element('h3','Saved acoustic parts'),label=element('p',job.request?.performers===2?'Duo mix':'Solo mix'),notice=element('p','Load the saved parts to listen to each player. The normal preview above remains available.');
  notice.className='muted';notice.setAttribute('role','status');
  const controls=element('div');controls.className='acoustic-parts-controls';
  const load=element('button','Load saved parts'),play=element('button','Play parts'),all=element('button','All'),seek=element('input'),time=element('span','0:00 / 0:00'),parts=element('div');
  for(const b of [load,play,all])b.type='button';play.disabled=true;all.disabled=true;
  seek.type='range';seek.min='0';seek.max='0';seek.step='0.01';seek.value='0';seek.disabled=true;seek.setAttribute('aria-label','Seek in saved acoustic parts');
  parts.className='acoustic-part-buttons';controls.append(load,play,all);section.append(title,label,notice,controls,parts,seek,time);
  const help=element('p',job.request?.performers===2?'Muting a player changes this Duo mix. A complete Solo is a separate arrangement.':'This is the complete saved Solo part.');help.className='muted';section.append(help);root.append(section);
  const mixNotice=element('p');mixNotice.className='muted';mixNotice.hidden=true;mixNotice.setAttribute('role','status');section.append(mixNotice);
  const buttons=new Map(),format=s=>`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
  const playback=createAcousticParts({...options,onChange:state=>{
    load.disabled=state.status==='loading';load.hidden=state.ready;load.textContent=state.status==='loading'?'Loading parts…':state.status==='error'?'Try loading parts':'Load saved parts';
    play.disabled=!state.ready;all.disabled=!state.ready;seek.disabled=!state.ready;
    play.textContent=state.playing?'Pause parts':state.status==='starting'?'Cancel playback':'Play parts';
    label.textContent=state.ready?state.label+(state.allMuted?' · all players muted':''):(job.request?.performers===2?'Duo mix':'Solo mix');
    notice.textContent=state.error?(state.status==='error'?'Saved parts could not be verified or loaded. Use the normal preview above.':state.error):state.status==='loading'?'Loading saved parts…':state.ready?'Choose the players you want to hear.':'Load the saved parts to listen to each player. The normal preview above remains available.';
    mixNotice.textContent=state.mixNotice;mixNotice.hidden=!state.mixNotice;
    seek.max=String(state.duration);seek.value=String(state.position);time.textContent=`${format(state.position)} / ${format(state.duration)}`;
    for(const [id,b] of buttons){if(!state.parts.some(part=>part.id===id)){b.disabled=true;b.onclick=()=>{};b.remove();buttons.delete(id)}}
    for(const part of state.parts){let b=buttons.get(part.id);if(!b){b=element('button');b.type='button';b.onclick=()=>{const current=playback.snapshot(),saved=current.parts.find(p=>p.id===part.id);if(current.ready&&saved)playback.setMuted(part.id,saved.audible)};buttons.set(part.id,b);parts.append(b)}
      b.textContent=`Player ${part.id.split('-').at(-1)} · ${part.role}`;b.setAttribute('aria-pressed',String(part.audible));b.disabled=!state.ready}
  }});
  load.onclick=()=>playback.load(job).catch(()=>{});play.onclick=()=>{const state=playback.snapshot();if(!state.ready)return;if(state.playing||state.status==='starting')playback.pause();else playback.play().catch(()=>{})};
  all.onclick=()=>{if(playback.snapshot().ready)playback.all()};seek.onchange=()=>{if(playback.snapshot().ready)return playback.seek(Number(seek.value)).catch(()=>{})};
  return {pause:playback.pause,unload:playback.unload,dispose(){playback.dispose();section.remove()},snapshot:playback.snapshot};
}
