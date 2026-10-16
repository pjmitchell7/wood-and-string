// Explicit review of uncertain source-part candidates, never automatic acceptance.
export function instrumentalChanges(arrangement){
  const originals=arrangement?.source?.notes||[],notes=arrangement?.notes||[];
  const rate=arrangement?.sample_rate||arrangement?.source?.sample_rate,origin=arrangement?.render_origin_sample??0;
  const byId=new Map(originals.map(n=>[n.id,n])),changes=[],excluded=new Set();
  const label=change=>{
    const source=byId.get(change.note_id);
    if(!source||!Number.isInteger(source.pitch)||!Number.isFinite(source.start)||!Number.isFinite(rate)||rate<=0)return 'A selected note';
    const pitch=['C','C♯','D','E♭','E','F','F♯','G','A♭','A','B♭','B'][source.pitch%12]+(Math.floor(source.pitch/12)-1);
    return `${pitch} at ${((source.start-origin)/rate).toFixed(2)} s`;
  };
  for(const change of arrangement?.adaptations||[]){
    if(change.kind==='candidate-not-selected'){excluded.add(change.note_id);continue}
    if(change.kind==='octave'){
      const octaves=Math.abs(change.semitones)/12;
      changes.push(`${label(change)} moved ${octaves===1?'one':octaves} octave${octaves===1?'':'s'} ${change.semitones>0?'up':'down'}.`);
    }else if(change.kind==='omission')changes.push(`${label(change)} left out because it could not meet this instrument's range or playing limits.`);
    else if(change.kind==='contour-not-rendered')changes.push(`${label(change)} uses a fixed sampled pitch; estimated bends or vibrato remain in the review.`);
    else changes.push(`${label(change)} has an additional arrangement change; check the saved review.`);
  }
  const count=`${originals.length} note${originals.length===1?'':'s'} selected; ${notes.length} included in this preview.`;
  if(excluded.size)changes.push(`${excluded.size} unchecked candidate${excluded.size===1?' remains':'s remain'} saved in the review.`);
  return [count,...changes].join(' ');
}

export function instrumentalRequest(source,start,end){
  if(!source?.url||!['original','Original'].includes(source.variant))throw Error('Choose an original saved on the laptop.');
  if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end-start>10)throw Error('Choose a passage longer than zero and at most 10 seconds.');
  return {asset_id:source.id,start_seconds:start,end_seconds:end};
}

export function candidateBundle(job){
  const bundle=job?.result?.candidates;
  if(job?.state!=='complete'||job.request?.kind!=='instrumental'||bundle?.version!==1||!bundle.interpretations||bundle.source_part?.id!=='umx:other'||bundle.source_part?.instrument!=='unknown')throw Error('No supported saved instrumental candidates are available.');
  const rate=bundle.sample_rate,origin=bundle.origin,end=bundle.end;
  if(!Number.isInteger(rate)||rate<=0||!Number.isInteger(origin)||!Number.isInteger(end)||origin<0||end<=origin)throw Error('Invalid candidate timing.');
  for(const key of ['published','onset_only']){
    const rows=bundle.interpretations[key]?.events;
    if(!Array.isArray(rows)||rows.length>4000)throw Error('Invalid candidate list.');
    const ids=new Set();
    for(const row of rows){
      if(typeof row.id!=='string'||!row.id||ids.has(row.id)||!Number.isInteger(row.start)||!Number.isInteger(row.end)||row.start<origin||row.end> end||row.end<=row.start||!Number.isInteger(row.pitch)||row.pitch<0||row.pitch>127)throw Error('Invalid candidate note.');
      ids.add(row.id);
    }
  }
  return bundle;
}

export function instrumentalPassage(job,tracks){
  const request=job?.request,track=tracks.find(t=>t.id===request?.asset_id);
  if(job?.state!=='complete'||!['instrumental','instrumental-arrange'].includes(request?.kind)||!track)throw Error('The original recording is unavailable.');
  instrumentalRequest(track,request.start_seconds,request.end_seconds);
  if(request.sha256&&request.sha256!==track.sha256)throw Error('Original recording identity changed.');
  return {track,start:request.start_seconds,end:request.end_seconds,label:`Original passage (cue may be approximate) · ${track.name} · ${request.start_seconds.toFixed(2)}–${request.end_seconds.toFixed(2)} seconds`};
}

export function mountInstrumentalCandidates({job,root,submit,document:doc=globalThis.document,bassAvailability=()=>({ready:false}),isCurrent=()=>true}){
  const bundle=candidateBundle(job),selected=new Set();let busy=false,page=0;
  const pageSize=100;
  const node=(tag,text)=>{const n=doc.createElement(tag);if(text!==undefined)n.textContent=text;return n};
  const panel=node('section');panel.append(node('h3','Review instrumental note candidates'));
  panel.append(node('p','Instrument unknown. These notes come from the separated other-instruments part, which can contain guitar, piano, synths and leakage. This does not identify a guitar or recover a verified melody, chords or style.'));
  panel.append(node('p','Expect wrong or extra notes, missed attacks and truncated sustain. Timing follows model frames; physical attacks and damping remain uncertain. Activations are not confidence percentages or performed dynamics. Nothing is selected automatically.'));
  const policyLabel=node('label','Candidate interpretation'),policy=node('select');
  for(const [value,label]of [['published','Default event interpretation (estimated timing)'],['onset_only','Onset-only alternative']]){const o=node('option',label);o.value=value;policy.append(o)}
  policy.value='published';policyLabel.append(policy);panel.append(policyLabel);
  const list=node('div');list.style.maxHeight='24rem';list.style.overflowY='auto';panel.append(list);
  const pageStatus=node('p'),previousPage=node('button','Previous notes'),nextPage=node('button','Next notes');
  previousPage.type=nextPage.type='button';panel.append(pageStatus,previousPage,nextPage);
  const choiceLabel=node('label','Render selected notes with'),instrument=node('select');
  const acoustic=node('option','One acoustic guitar');acoustic.value='acoustic';
  const bass=node('option','One Jazz Bass · experimental');bass.value='jazz-bass';bass.disabled=!bassAvailability().ready;
  instrument.append(acoustic,bass);instrument.value='acoustic';choiceLabel.append(instrument);panel.append(choiceLabel);
  const consentLabel=node('label'),consent=node('input');consent.type='checkbox';consent.checked=false;
  consentLabel.append(consent,node('span','I choose these notes as the lead/melody part for this arrangement. This is my interpretation, not a detected source role.'));panel.append(consentLabel);
  panel.append(node('p','Only checked notes from this interpretation will be proposed for one player. Other candidates remain saved evidence. Physical planning may still transpose or omit selected notes; review those changes. Electric instruments, drums and automatic style interpretation are unavailable.'));
  const message=node('p');message.setAttribute('role','status');message.setAttribute('aria-live','polite');
  const save=node('button','Preview selected notes');save.type='button';
  let boxes=[];
  function state(){save.disabled=busy||!selected.size||!consent.checked;policy.disabled=instrument.disabled=consent.disabled=busy;for(const box of boxes)box.disabled=busy;
    const count=bundle.interpretations[policy.value].events.length;
    previousPage.disabled=busy||page===0;nextPage.disabled=busy||(page+1)*pageSize>=count;
    pageStatus.textContent=`${count?`${page*pageSize+1}–${Math.min((page+1)*pageSize,count)} of ${count}`:'0'} candidates shown. ${selected.size} selected across all pages.`;
  }
  function renderPage(){
    list.replaceChildren();boxes=[];
    const rows=bundle.interpretations[policy.value].events;
    if(!rows.length)list.append(node('p','No candidates in this interpretation. Try the alternative or another passage.'));
    for(const event of rows.slice(page*pageSize,(page+1)*pageSize)){
      const label=node('label'),box=node('input');box.type='checkbox';box.checked=selected.has(event.id);
      const pitch=['C','C#','D','Eb','E','F','F#','G','Ab','A','Bb','B'][event.pitch%12]+(Math.floor(event.pitch/12)-1);
      label.append(box,node('span',`${pitch} · ${((event.start-bundle.origin)/bundle.sample_rate).toFixed(3)}–${((event.end-bundle.origin)/bundle.sample_rate).toFixed(3)} s`));
      label.style.display='block';box.onchange=()=>{if(busy||!isCurrent())return;if(box.checked)selected.add(event.id);else selected.delete(event.id);state()};boxes.push(box);list.append(label);
    }
    state();
  }
  function show(){selected.clear();consent.checked=false;page=0;message.textContent='Choose notes explicitly. Times are relative to the selected passage.';renderPage()}
  previousPage.onclick=()=>{if(!busy&&isCurrent()&&page>0){page--;renderPage()}};
  nextPage.onclick=()=>{if(!busy&&isCurrent()&&(page+1)*pageSize<bundle.interpretations[policy.value].events.length){page++;renderPage()}};
  policy.onchange=()=>{if(!busy&&isCurrent())show()};consent.onchange=()=>{if(!busy&&isCurrent())state()};
  save.onclick=async()=>{
    if(busy||!isCurrent())return;
    try{
      if(!selected.size||!consent.checked)throw Error('Select notes and explicitly accept their lead role first.');
      if(instrument.value==='jazz-bass'&&!bassAvailability().ready)throw Error(bassAvailability().reason||'Jazz Bass is not available.');
      if(!['acoustic','jazz-bass'].includes(instrument.value))throw Error('Choose a supported instrument.');
      busy=true;state();message.textContent='Saving your selected interpretation…';
      await submit({parent_job_id:job.id,interpretation:policy.value,note_ids:[...selected].sort(),instrument:instrument.value,accept_as_lead:true});
    }catch(error){if(isCurrent())message.textContent=error.message}
    finally{busy=false;if(isCurrent())state()}
  };
  panel.append(save,message);root.append(panel);show();
  return {panel,policy,instrument,consent,save,message,previousPage,nextPage,pageStatus,get boxes(){return boxes},get selected(){return [...selected]}};
}
