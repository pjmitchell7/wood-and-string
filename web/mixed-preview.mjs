// The form starts a background job only. Playback is always a separate action.
import {normalizeElectricTone,electricToneLabel} from './electric-tone.mjs?v=25';
export function canPreviewMixed(source) {
  return Boolean(source?.url && ['original','Original'].includes(source.variant) &&
    ['audio/wav','audio/mpeg','audio/mp4','audio/aac','audio/flac','audio/webm'].includes(source.type));
}

export function previewGuidance(value={},performers=1){
  const result={version:1};
  for(const field of ['source_notes','arrangement_notes']){
    if(value[field]!==undefined&&typeof value[field]!=='string')throw Error('Preview notes must be text.');
    const text=(value[field]||'').normalize('NFC').trim();
    if([...text].length>256||new TextEncoder().encode(text).length>1024||/[\u0000-\u0008\u000b-\u001f]/u.test(text))throw Error('Keep each preview note within 256 characters.');
    result[field]=text;
  }
  result.accompaniment=value.accompaniment??'melody-and-bass';
  if(!['melody-and-bass','melody-only'].includes(result.accompaniment))throw Error('Choose a supported accompaniment option.');
  if(performers!==1&&result.accompaniment==='melody-only')throw Error('Melody only needs one guitarist.');
  return result;
}
export function previewSelection(value={},performers=1){
  const instrument=value.instrument??'acoustic';
  if(!['acoustic','jazz-bass','electric'].includes(instrument))throw Error('Choose acoustic guitar, experimental Electric guitar or Jazz Bass.');
  if(instrument!=='electric'&&value.electric_tone!==undefined)throw Error('Tone settings require Electric guitar.');
  if(instrument==='acoustic')return {instrument};
  if(instrument==='electric'){
    if(![1,2].includes(performers))throw Error('Choose one or two Electric guitarists.');
    if(value.bass_part!==undefined)throw Error('Bass part choices require Jazz Bass.');
    const tone=normalizeElectricTone(value.electric_tone);
    return tone?{instrument,electric_tone:tone}:{instrument};
  }
  const bass_part=value.bass_part??(performers===2?'melody-and-bass':'melody');
  if(!(performers===2?['melody-and-bass']:['melody','bass-line']).includes(bass_part))throw Error('Choose a bass part for the selected number of players.');
  return {instrument,bass_part};
}
export function previewChoice(request){
  const selected=previewSelection(request,request.performers);
  if(selected.instrument==='jazz-bass')return selected.bass_part==='melody-and-bass'?'Experimental Jazz Bass · two players: upper estimated melody and low bass foundation.':`Experimental Jazz Bass · one player following the estimated ${selected.bass_part==='melody'?'melody':'bass line'}.`;
  const guidance=previewGuidance(request.guidance,request.performers);
  if(selected.instrument==='electric'){
    if(guidance.accompaniment!=='melody-and-bass')throw Error('Electric previews use estimated melody and bass together.');
    return `Experimental ${electricToneLabel(selected.electric_tone)} · ${request.performers===2?'two players: upper estimated melody and low estimated bass foundation.':'one player: estimated melody and bass together.'}`;
  }
  return guidance.accompaniment==='melody-only'?'Acoustic guitar · estimated melody only; bass omitted.':'Acoustic guitar · estimated melody with bass.';
}
export function mixedPreviewRequest(source, start, end, performers, notes={}, selection={}) {
  if (!source?.url) throw Error('Save this recording to the laptop first.');
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || end-start > 30)
    throw Error('Choose a passage longer than zero and at most 30 seconds.');
  if (![1, 2].includes(performers)) throw Error('Choose one or two guitarists.');
  const request={asset_id:source.id, start_seconds:start, end_seconds:end, performers};
  const selected=previewSelection(selection,performers);
  if(selected.instrument!=='acoustic')Object.assign(request,selected);
  const guidance=previewGuidance(selected.instrument!=='acoustic'?{...notes,accompaniment:'melody-and-bass'}:notes,performers);
  if(guidance.source_notes||guidance.arrangement_notes||guidance.accompaniment!=='melody-and-bass')request.guidance=guidance;
  return request;
}

export function mixedCandidateSummary(report) {
  if (!report?.roles) return '';
  const melody = report.roles.melody, bass = report.roles.bass;
  const dropped = (melody?.discarded_short_groups?.length||0)+(bass?.discarded_short_groups?.length||0);
  return `${melody?.retained_notes||0} melody notes and ${bass?.retained_notes||0} bass notes estimated. ${dropped} short pitch groups excluded. These are estimates, not an accuracy score.`;
}

export function originalPassage(job, tracks) {
  const request=job?.request, track=tracks.find(t=>t.id===request?.asset_id);
  if(job?.state!=='complete'||!['mixed','refine'].includes(request?.kind)||!track||!['original','Original'].includes(track.variant))
    throw Error('The original recording is not available in this library.');
  if(request.sha256&&request.sha256!==track.sha256)throw Error('The original identity no longer matches this preview.');
  const start=request.start_seconds,end=request.end_seconds;
  if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end-start>30)throw Error('Invalid original passage.');
  const cue=job.result?.analysis?.timebase?.decoded===true?'Approximate original cue':'Original';
  return {track,start,end,label:`${cue} · ${track.name} · ${start.toFixed(2)}–${end.toFixed(2)} seconds`};
}

// Dedicated media element: never writes the main queue or resumes it implicitly.
export function passagePlayer({audio, load, pauseMain, setTimer=setTimeout, clearTimer=clearTimeout}) {
  let generation=0, timer, release, listeners=[], pending=[];
  const listen=(name,fn)=>{audio.addEventListener(name,fn);listeners.push([name,fn])};
  function stop(){
    generation++;clearTimer(timer);timer=undefined;
    for(const [name,fn] of listeners)audio.removeEventListener(name,fn);listeners=[];
    for(const resolve of pending)resolve();pending=[];
    audio.pause();audio.removeAttribute('src');audio.load();
    if(release)release();release=undefined;
  }
  function wait(name){return new Promise(resolve=>{pending.push(resolve);listen(name,resolve);listen('error',resolve)})}
  async function start(passage){
    stop();const token=generation;pauseMain();
    try{
      const resource=await load(passage.track);
      if(token!==generation){resource.release();return}
      release=resource.release;audio.src=resource.url;audio.load();
      if(audio.readyState<1)await wait('loadedmetadata');
      if(token!==generation)return;
      if(audio.error||!Number.isFinite(audio.duration)||passage.end>audio.duration+.02)throw Error('The original passage could not be loaded.');
      if(Math.abs(audio.currentTime-passage.start)>.001){const sought=wait('seeked');audio.currentTime=passage.start;await sought}
      if(token!==generation)return;
      if(audio.error)throw Error('The original passage could not be cued.');
      for(const [name,fn]of listeners)audio.removeEventListener(name,fn);listeners=[];pending=[];
      for(const event of ['pause','seeking','ended','error','emptied'])listen(event,stop);
      listen('timeupdate',()=>{if(audio.currentTime>=passage.end||audio.currentTime<passage.start-.02)stop()});
      listen('ratechange',stop);
      await audio.play();
      if(token!==generation)return
      timer=setTimer(stop,Math.max(0,(passage.end-audio.currentTime)*1000));
    }catch(error){if(token===generation)stop();throw error}
  }
  return {start,stop};
}
