import { mountRefinement, restoreSavedPreview } from './refinement.mjs?v=24';
import { scheduledTransport, prepareBuffer, engineWithFallback, playNative, nextQueueIndex } from './transport.mjs?v=22';
import { mountCapture } from './capture-ui.mjs';
import { preflightRestore } from './backup.mjs';
import { mixedPreviewRequest, previewSelection, previewChoice, previewGuidance, mixedCandidateSummary, originalPassage, passagePlayer, canPreviewMixed } from './mixed-preview.mjs?v=24';
import { instrumentalRequest, instrumentalPassage, instrumentalChanges, mountInstrumentalCandidates } from './instrumental.mjs?v=24';
import { ProcessingOutbox, IndexedProcessingStore } from './processing-outbox.mjs?v=24';
import { mountSoundDiagnostics } from './sound-diagnostics.mjs?v=24';

const $ = id => document.getElementById(id);

let audio = $('audio');
const MEDIA = 'wood-media-v1';

let tracks = [], queue = [], current = -1, objectUrl, context, source, eq, sleepTimer;
let transport, playGeneration=0, queueSignature="";
function pausePlayback(){playGeneration++;audio.pause();transport?.pause()}

const status = text => $('status').textContent = text;
const compareAudio=document.createElement('audio');compareAudio.preload='metadata';
const comparison=passagePlayer({audio:compareAudio,load:verifiedComparisonMedia,pauseMain:()=>pausePlayback()});

window.addEventListener('pagehide',()=>{comparison.stop();transport?.stop()});
async function verifiedBytes(track){
  let response;
  if(track.cached)response=await(await caches.open(MEDIA)).match(mediaPath(track.sha256));
  if(!response){if(!track.url)throw Error('Original audio is missing. Download it again.');await session();response=await fetch(track.url);if(!response.ok)throw Error('The original could not be loaded from the laptop.')}
  const bytes=await response.arrayBuffer();
  if(bytes.byteLength!==track.size||await hash(bytes)!==track.sha256)throw Error('Original audio verification failed.');
  return bytes;
}
async function verifiedComparisonMedia(track){
  const bytes=await verifiedBytes(track);
  const url=URL.createObjectURL(new Blob([bytes],{type:track.type}));
  return {url,release:()=>URL.revokeObjectURL(url)};
}


const ELECTRIC_PREVIEW_ENABLED=false;
const ELECTRIC_TESTING_REASON='Experimental Electric previews are still being tested. Saved Electric previews and settings remain available.';
let electricAvailability={ready:false,reason:'Connect to the laptop to check experimental Electric availability.'};
function requireElectricPreview({online=true}={}){
  if(!ELECTRIC_PREVIEW_ENABLED)throw Error(ELECTRIC_TESTING_REASON);
  if(online&&electricAvailability.ready!==true)throw Error(electricAvailability.reason);
}
let bassAvailability={ready:false,reason:'Connect to the laptop to check experimental Jazz Bass availability.'};
let instrumentalSource,instrumentalAvailability={ready:false,reason:'Connect to the laptop to check instrumental inspection availability.'};
let extractionSource, mixedSource, activeJobKind, activeJob, pollTimer, pendingActions=0, restoreBusy=false;
let enqueueingProcessing=false;
function processingMime(track){const known=['audio/wav','audio/mpeg','audio/mp4','audio/aac','audio/flac','audio/webm'];if(known.includes(track.type))return track.type;return {wav:'audio/wav',mp3:'audio/mpeg',m4a:'audio/mp4',aac:'audio/aac',flac:'audio/flac',webm:'audio/webm'}[track.name?.split('.').pop().toLowerCase()]||null}

async function action(fn){if(restoreBusy)throw Error('Wait for the restore to finish.');pendingActions++;try{return await fn()}finally{pendingActions--}}

function jobMessage(text){$('diagnosticStatus').textContent=text}
function describeAdaptation(note){if(typeof note==='string')return note;if(note.kind==='octave')return `${note.note_id}: moved ${Math.abs(note.semitones)/12} octave${Math.abs(note.semitones)===12?'':'s'} ${note.semitones>0?'up':'down'}`;if(note.kind==='omission')return `${note.note_id}: ${note.reason||'No available sampled string met the hand-position limits'}${note.essential?' (melody note)':''}`;if(note.kind==='contour-not-rendered')return `${note.note_id}: pitch bends or vibrato are retained in the analysis but not reproduced in this preview`;return `${note.note_id||'Note'}: ${note.reason||note.kind||'adapted'}`}

async function session(signal){const response=await fetch('/session',{cache:'no-store',signal});if(response.status===401||response.status===403){$('connectionHelp').hidden=false;const error=Error('This device needs laptop approval again. Downloaded music still plays; use Reconnect to the laptop.');error.status=response.status;throw error}if(!response.ok)throw Error('The laptop connection could not be established. Downloaded music still plays.')}

async function apiJSON(url,options){await session(options?.signal);const response=await fetch(url,options);let data;try{data=await response.json()}catch{throw Error('The laptop did not return a usable response.')}if(!response.ok){const error=Error(data.detail||`Request failed (${response.status}).`);error.status=response.status;throw error}return data}

let resultGeneration=0;
let refinementStarting=false;
function claimResult(){return ++resultGeneration}
async function viewSavedJob(id){const token=claimResult();const job=await apiJSON(`/api/jobs/${encodeURIComponent(id)}`);if(token===resultGeneration)await showJob(job,token)}
async function showJob(job,token=claimResult()){
  if(token!==resultGeneration)return;
  activeJob=job.id;activeJobKind=job.request?.kind||activeJobKind||'diagnostic';
  localStorage.setItem('activeJob',job.id);localStorage.setItem('activeJobKind',activeJobKind);
  const running=['queued','running'].includes(job.state),extracted=activeJobKind==='extract',mixed=['mixed','refine'].includes(activeJobKind),refined=activeJobKind==='refine',bassPreview=job.request?.instrument==='jazz-bass',electricPreview=job.request?.instrument==='electric',inspection=activeJobKind==='instrumental',accepted=activeJobKind==='instrumental-arrange';
  const previewInstrument=bassPreview?'Jazz Bass':electricPreview?'dry Electric guitar':'acoustic guitar',previewTitle=bassPreview?'Jazz Bass':electricPreview?'Electric guitar':'Acoustic guitar';
  $('diagnostic').disabled=running;$('extractSubmit').disabled=running;$('mixedSubmit').disabled=running;$('instrumentalSubmit').disabled=running;$('cancelDiagnostic').hidden=!running;
  $('diagnosticProgress').hidden=!running;$('diagnosticProgress').value=Math.max(0,Math.min(1,Number(job.progress)||0));
  jobMessage(running?(job.state==='queued'?'Waiting to start…':`${inspection?'Estimating instrumental note candidates':accepted?'Rendering your selected notes':refined?'Rendering your refined passage':mixed?`Analyzing melody and bass, then rendering ${previewInstrument}`:extracted?'Analyzing and rendering melody':'Rendering sample'} · ${Math.round((Number(job.progress)||0)*100)}%`):(job.state==='cancelled'?'Cancelled. Your original and saved results remain unchanged.':job.error||(inspection?'Instrumental candidates are ready for your review.':accepted?'Selected-note preview is ready; source identity remains uncertain.':mixed?`${previewTitle} preview is ready.`:extracted?'Melody preview is ready.':'Sample phrase is ready.')));
  if(running){pollTimer=setTimeout(()=>pollJob(),600);return}
  localStorage.removeItem('activeJob');localStorage.removeItem('activeJobKind');activeJob=null;
  if(inspection&&job.state==='complete'&&job.result?.candidates){
    const root=$('diagnosticResult');root.replaceChildren();
    localStorage.setItem('latestInstrumentalJob',job.id);$('instrumentalSaved').hidden=false;
    mountInstrumentalCandidates({job,root,bassAvailability:()=>bassAvailability,electricAvailability:()=>electricAvailability,electricEnabled:ELECTRIC_PREVIEW_ENABLED,isCurrent:()=>token===resultGeneration,
      submit:request=>action(async()=>{
        if(token!==resultGeneration)throw Error('This candidate review is no longer current.');
        if(activeJob||refinementStarting)throw Error('Wait for the current preview or cancel it first.');
        if(request.instrument==='electric')requireElectricPreview();
        refinementStarting=true;
        try{const next=await apiJSON('/api/arrange-instrumental',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
          if(token!==resultGeneration)return;const nextToken=claimResult();comparison.stop();root.replaceChildren();await showJob(next,nextToken);
        }finally{refinementStarting=false}
      })});
    try{const passage=instrumentalPassage(job,tracks);root.append(button('Compare original passage',()=>{if(refinementStarting)return;return comparison.start(passage)}),button('Stop comparison',()=>comparison.stop()))}
    catch(error){const notice=document.createElement('p');notice.textContent=error.message;root.append(notice)}
    return;
  }
  if(accepted&&job.state!=='complete'&&job.request?.parent_job_id){$('diagnosticResult').replaceChildren(button('Return to candidate review',async()=>{if(activeJob||refinementStarting)throw Error('Wait for the current job first.');await viewSavedJob(job.request.parent_job_id)}))}
  if(mixed){const root=$('diagnosticResult');root.replaceChildren();root.append(button('Use these preview settings',()=>restoreMixedSettings(job.request)));showSavedGuidance(job.request,root)}
  if(refined&&job.state!=='complete'&&job.request?.parent_job_id){$('diagnosticResult').append(button('Return to previous version',async()=>{if(activeJob||refinementStarting)throw Error('Wait for the current preview or cancel it first.');await viewSavedJob(job.request.parent_job_id)}))}
  if(job.state!=='complete')mountSoundDiagnostics({job,root:$('diagnosticResult'),document});
  if(job.state==='complete'&&job.result?.asset){
    if(mixed)localStorage.setItem('latestPreviewJob',job.id);
    const item=job.result.asset;await action(refresh);if(token!==resultGeneration)return;const t=tracks.find(t=>t.id===item.id),root=$('diagnosticResult');root.replaceChildren();
    if(t)root.append(button(mixed||accepted?`Play ${bassPreview?'Jazz Bass':electricPreview?'Electric guitar':'acoustic'} preview`:extracted?'Play melody preview':'Play sample',()=>{queue.push(t.id);return play(queue.length-1)}),button('Download preview',()=>download(t)));
    if(mixed||accepted){
      if(mixed){root.append(button('Use these preview settings',()=>restoreMixedSettings(job.request)));showSavedGuidance(job.request,root)}
      if(accepted){const receipt=document.createElement('p');receipt.textContent=`Your selected instrumental candidates were assigned as a lead part for one ${previewInstrument}. This is your arrangement interpretation; the source instrument remains unknown.`;root.append(receipt,button('Return to candidate review',async()=>{if(activeJob||refinementStarting)throw Error('Wait for the current job first.');comparison.stop();await viewSavedJob(job.request.parent_job_id)}))}
      comparison.stop();
      try{const passage=accepted?instrumentalPassage(job,tracks):originalPassage(job,tracks),label=document.createElement('p');label.textContent=passage.label;
        root.append(label,button('Compare original passage',()=>{if(refinementStarting)return;claimResult();return comparison.start(passage)}),button('Stop comparison',()=>{if(!refinementStarting)claimResult();comparison.stop()}));
      }catch(error){const unavailable=document.createElement('p');unavailable.textContent=error.message;root.append(unavailable)}
    }
    if(mixed){
      mountRefinement({job,root,submit:request=>action(async()=>{
        if(activeJob||refinementStarting)throw Error('Wait for the current preview or cancel it first.');
        if(electricPreview)requireElectricPreview();
        refinementStarting=true;
        try{const nextToken=claimResult();const next=await apiJSON('/api/refine-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
          if(nextToken!==resultGeneration)return;comparison.stop();root.replaceChildren();await showJob(next,nextToken);
        }finally{refinementStarting=false}
      })});
      const parent=job.result.arrangement?.refinement?.parent_job_id;
      if(parent)root.append(button('Previous version',async()=>{if(activeJob||refinementStarting)throw Error('Wait for the current preview or cancel it first.');comparison.stop();await viewSavedJob(parent)}));
    }
    if(job.result.notice){const notice=document.createElement('p');notice.textContent=job.result.notice;root.append(notice)}
    const candidates=mixedCandidateSummary(job.result.analysis);if(candidates){const text=document.createElement('p');text.textContent=candidates;root.append(text)}
    const warnings=job.result.analysis?.warnings||[];
    if(warnings.length){const details=document.createElement('details'),title=document.createElement('summary'),list=document.createElement('ul');title.textContent=`Analysis limits (${warnings.length})`;for(const warning of warnings){const li=document.createElement('li');li.textContent=warning;list.append(li)}details.append(title,list);root.append(details)}
    const notes=job.result.arrangement?.adaptations||[],summary=document.createElement('p');summary.className='muted';summary.textContent=accepted?instrumentalChanges(job.result.arrangement):notes.length?`Arrangement changes: ${notes.map(describeAdaptation).join('; ')}`:'No note adaptations reported for this preview.';root.append(summary);
    mountSoundDiagnostics({job,root,document});
  }
}
async function pollJob(){const token=resultGeneration;try{await showJob(await apiJSON(`/api/jobs/${encodeURIComponent(activeJob)}`),token)}catch(e){if(token!==resultGeneration)return;jobMessage(`${e.message} Reopen the app to check this preview.`);$('diagnostic').disabled=false;$('extractSubmit').disabled=false;$('mixedSubmit').disabled=false;$('instrumentalSubmit').disabled=false;$('cancelDiagnostic').hidden=true}}

$('diagnostic').onclick=async()=>{if(refinementStarting)return;const token=claimResult();try{activeJobKind='diagnostic';clearTimeout(pollTimer);$('diagnostic').disabled=true;comparison.stop();$('diagnosticResult').replaceChildren();jobMessage('Starting sample…');await showJob(await apiJSON('/api/diagnostic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({performers:Number($('performers').value)})}),token)}catch(e){jobMessage(e.message);$('diagnostic').disabled=false}};

$('cancelDiagnostic').onclick=async()=>{if(!activeJob)return;try{await apiJSON(`/api/jobs/${encodeURIComponent(activeJob)}/cancel`,{method:'POST'});jobMessage('Cancellation requested…')}catch(e){jobMessage(e.message)}};
function selectMixed(track){if(mixedSource?.id!==track.id){$('mixedSourceNotes').value='';$('mixedArrangementNotes').value='';$('mixedAccompaniment').value='melody-and-bass';$('mixedBassPart').value='melody'}mixedSource=track;updateAccompaniment();$('mixedPanel').hidden=false;$('mixedSource').textContent=track.name;$('mixedError').textContent='';$('mixedPanel').scrollIntoView({behavior:'smooth',block:'center'});$('mixedStart').focus()}
function updateAccompaniment(){
  const duet=Number($('mixedPerformers').value)===2,bass=$('mixedInstrument').value==='jazz-bass',electric=$('mixedInstrument').value==='electric';
  if(bass||electric)$('mixedAccompaniment').value='melody-and-bass';
  if(duet&&$('mixedAccompaniment').value==='melody-only'){$('mixedAccompaniment').value='melody-and-bass';$('mixedError').textContent='Two guitarists need melody and bass; bass accompaniment is included.'}
  $('mixedAccompaniment').disabled=duet||bass||electric;
  $('mixedAccompanimentLabel').hidden=bass||electric;
  $('mixedBassPartLabel').hidden=!bass||duet;
  $('mixedBassPart').disabled=!bass||duet;
  $('mixedPerformers').options[0].textContent=bass?'One bass player · choose a part':electric?'One Electric guitar · melody and bass':'One · melody and bass together';
  $('mixedPerformers').options[1].textContent=bass?'Two bass players · upper melody + low foundation':electric?'Two Electric guitars · upper melody + low foundation':'Two acoustics · melody + bass accompaniment';
  $('mixedPartDescription').textContent=bass?(duet?'Upper estimated melody and low estimated bass foundation, assigned to separate bass players.':'Follow one estimated part with Jazz Bass samples. The other estimated part is explicitly omitted.'):electric?'Dry Electric samples follow the estimated melody and bass. This does not identify or recover the source guitar, and adds no amplifier or reverb.':'Acoustic guitar follows the accompaniment choice above.';
  $('mixedSubmit').textContent=bass?'Make Jazz Bass preview':electric?'Make Electric preview':'Make acoustic preview';
  $('mixedBassOption').disabled=!bassAvailability.ready;
  $('mixedBassAvailability').textContent=bassAvailability.ready?'Experimental Jazz Bass is available. Fingering and sounding range use conservative assumptions; musical quality remains unverified.':bassAvailability.reason;
  $('mixedElectricOption').disabled=!ELECTRIC_PREVIEW_ENABLED||electricAvailability.ready!==true;
  $('mixedElectricAvailability').textContent=!ELECTRIC_PREVIEW_ENABLED?ELECTRIC_TESTING_REASON:electricAvailability.ready?'Experimental dry Electric is available. Estimated parts and musical fidelity still need review.':electricAvailability.reason;
}
$('mixedPerformers').onchange=updateAccompaniment;
$('mixedInstrument').onchange=updateAccompaniment;
async function loadBassAvailability(){
  try{const status=await apiJSON('/api/status');bassAvailability={ready:status.bass_ready===true,reason:typeof status.bass_reason==='string'?status.bass_reason:'Experimental Jazz Bass is not ready on this laptop.'};electricAvailability={ready:status.electric_ready===true,reason:typeof status.electric_reason==='string'?status.electric_reason:'Experimental Electric is not ready on this laptop.'}}
  catch{bassAvailability={ready:false,reason:'Connect to the laptop to check experimental Jazz Bass availability.'};electricAvailability={ready:false,reason:'Connect to the laptop to check experimental Electric availability.'}}
  updateAccompaniment();
}
function restoreMixedSettings(request){
  const track=tracks.find(t=>t.id===request.asset_id);if(!track)throw Error('The original recording is not in this library.');
  const guidance=previewGuidance(request.guidance,request.performers);
  const selected=previewSelection(request,request.performers);
  selectMixed(track);$('mixedStart').value=request.start_seconds;$('mixedEnd').value=request.end_seconds;$('mixedPerformers').value=request.performers;
  $('mixedInstrument').value=selected.instrument;$('mixedBassPart').value=selected.bass_part==='bass-line'?'bass-line':'melody';
  $('mixedSourceNotes').value=guidance.source_notes;$('mixedArrangementNotes').value=guidance.arrangement_notes;$('mixedAccompaniment').value=guidance.accompaniment;updateAccompaniment();
}
function showSavedGuidance(request,root){
  const guidance=previewGuidance(request.guidance,request.performers);
  const choice=document.createElement('p');choice.textContent=previewChoice(request);root.append(choice);
  for(const [field,label]of [['source_notes','Your observation about the original'],['arrangement_notes','Your desired arrangement']]){
    if(guidance[field]){const text=document.createElement('p');text.textContent=`${label} (saved note, not analyzed): ${guidance[field]}`;root.append(text)}
  }
}
$('mixedClose').onclick=()=>{$('mixedPanel').hidden=true};
function selectInstrumental(track){
  instrumentalSource=track;$('instrumentalSource').textContent=track.name;$('instrumentalError').textContent='';
  $('instrumentalPanel').hidden=false;$('instrumentalPanel').scrollIntoView({behavior:'smooth',block:'center'});$('instrumentalStart').focus();
}
async function loadInstrumentalAvailability(){
  try{const value=await apiJSON('/api/status');instrumentalAvailability={ready:value.instrumental_ready===true,reason:typeof value.instrumental_reason==='string'?value.instrumental_reason:'Instrumental inspection is not ready on this laptop.'}}
  catch{instrumentalAvailability={ready:false,reason:'Connect to the laptop to inspect instrumental notes.'}}
  $('instrumentalAvailability').textContent=instrumentalAvailability.ready?'Instrumental inspection is available. Candidate notes still require your review.':instrumentalAvailability.reason;
}
$('instrumentalClose').onclick=()=>{$('instrumentalPanel').hidden=true};
$('instrumentalSaved').onclick=async()=>{
  if(activeJob||refinementStarting){status('Wait for the current preview or cancel it first.');return}
  const id=localStorage.getItem('latestInstrumentalJob');if(!id)return;
  try{comparison.stop();await viewSavedJob(id)}catch(error){status(error.message)}
};
$('instrumentalForm').onsubmit=async event=>{
  event.preventDefault();
  if(activeJob||refinementStarting){$('instrumentalError').textContent='Wait for the current preview or cancel it first.';return}
  let token;
  try{
    if(!instrumentalAvailability.ready)throw Error(instrumentalAvailability.reason);
    const request=instrumentalRequest(instrumentalSource,Number($('instrumentalStart').value),Number($('instrumentalEnd').value));
    token=claimResult();refinementStarting=true;$('instrumentalSubmit').disabled=true;$('instrumentalError').textContent='';
    comparison.stop();$('diagnosticResult').replaceChildren();jobMessage('Starting instrumental inspection…');
    const job=await apiJSON('/api/instrumental-candidates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
    if(token===resultGeneration)await showJob(job,token);
  }catch(error){if(token===undefined||token===resultGeneration){$('instrumentalError').textContent=error.message;$('instrumentalSubmit').disabled=false}}
  finally{if(token!==undefined)refinementStarting=false}
};
$('mixedForm').onsubmit=async event=>{
  event.preventDefault();
  try{
    if(activeJob||refinementStarting)throw Error('Wait for the current preview or cancel it first.');
    const instrument=$('mixedInstrument').value,performers=Number($('mixedPerformers').value);
    if(instrument==='jazz-bass'&&!bassAvailability.ready)throw Error(bassAvailability.reason);
    if(instrument==='electric')requireElectricPreview();
    const selection=instrument==='jazz-bass'?{instrument,bass_part:performers===2?'melody-and-bass':$('mixedBassPart').value}:{instrument};
    const request=mixedPreviewRequest(mixedSource,Number($('mixedStart').value),Number($('mixedEnd').value),performers,{source_notes:$('mixedSourceNotes').value,arrangement_notes:$('mixedArrangementNotes').value,accompaniment:$('mixedAccompaniment').value},selection);
    const token=claimResult();$('mixedError').textContent='';$('mixedSubmit').disabled=true;activeJobKind='mixed';comparison.stop();$('diagnosticResult').replaceChildren();jobMessage(`Starting ${instrument==='jazz-bass'?'Jazz Bass':instrument==='electric'?'dry Electric':'acoustic'} preview…`);
    await showJob(await apiJSON('/api/arrange-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)}),token);
  }catch(e){$('mixedError').textContent=e.message;$('mixedSubmit').disabled=false}
};
$('mixedQueue').onclick=()=>{
  if(enqueueingProcessing)return;
  enqueueingProcessing=true;$('mixedQueue').disabled=true;
  const selectedSource=mixedSource?{...mixedSource}:null;
  const chosen={start:Number($('mixedStart').value),end:Number($('mixedEnd').value),performers:Number($('mixedPerformers').value),source_notes:$('mixedSourceNotes').value,arrangement_notes:$('mixedArrangementNotes').value,accompaniment:$('mixedAccompaniment').value,instrument:$('mixedInstrument').value,bass_part:$('mixedBassPart').value};
  return action(()=>withProcessingLock(async()=>{
  if(chosen.instrument==='electric')requireElectricPreview({online:false});
  const source=selectedSource;
  if(!source?.cached)throw Error('Download or import this original on this device before queuing it.');
  // The local source is verified below; this URL permits the existing form's
  // choice validation without pretending it has been received by the laptop.
  const selection=chosen.instrument==='jazz-bass'?{instrument:chosen.instrument,bass_part:chosen.performers===2?'melody-and-bass':chosen.bass_part}:{instrument:chosen.instrument};
  const request=mixedPreviewRequest({...source,url:source.url||mediaPath(source.sha256)},chosen.start,chosen.end,chosen.performers,{source_notes:chosen.source_notes,arrangement_notes:chosen.arrangement_notes,accompaniment:chosen.accompaniment},selection);
  await processingBytes({sha256:source.sha256,size:source.size});
  delete request.asset_id;
  const type=processingMime(source);if(!type)throw Error('Choose MP3, WAV, M4A/AAC, FLAC or WebM audio for this preview.');
  await processingOutbox.enqueue({track_id:source.id,sha256:source.sha256,size:source.size,name:source.name,type},request);
  $('mixedError').textContent='Saved on this device. It will transfer when the laptop is reachable and this page is open.';
  })).then(()=>{syncProcessing()}).catch(error=>{$('mixedError').textContent=error.message}).finally(()=>{enqueueingProcessing=false;$('mixedQueue').disabled=false});
};
function selectExtraction(track){extractionSource=track;$('extractionPanel').hidden=false;$('extractSource').textContent=track.name;$('isolatedMelody').checked=false;$('extractError').textContent='';$('extractionPanel').scrollIntoView({behavior:'smooth',block:'center'});$('extractStart').focus()}
$('extractClose').onclick=()=>{$('extractionPanel').hidden=true};
$('extractForm').onsubmit=async event=>{
  event.preventDefault();
  try{
    const start=Number($('extractStart').value),end=Number($('extractEnd').value);
    if(!extractionSource?.url)throw Error('Save this WAV to the laptop first.');
    if(!$('isolatedMelody').checked)throw Error('Confirm the recording contains one isolated melody.');
    if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end-start>30)throw Error('Choose a passage longer than zero and at most 30 seconds.');
    if(activeJob||refinementStarting)throw Error('Wait for the current preview or cancel it first.');
    const token=claimResult();$('extractError').textContent='';$('extractSubmit').disabled=true;activeJobKind='extract';comparison.stop();$('diagnosticResult').replaceChildren();jobMessage('Starting melody analysis…');
    await showJob(await apiJSON('/api/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset_id:extractionSource.id,start_seconds:start,end_seconds:end,performers:1,isolated_melody:true})}),token);
  }catch(e){$('extractError').textContent=e.message;$('extractSubmit').disabled=false}
};

const db = await new Promise((resolve,reject)=>{const req=indexedDB.open('wood-library',2);req.onupgradeneeded=()=>{if(!req.result.objectStoreNames.contains('tracks'))req.result.createObjectStore('tracks',{keyPath:'id'});if(!req.result.objectStoreNames.contains('settings'))req.result.createObjectStore('settings')};req.onblocked=()=>status('Close other Wood & String tabs to finish updating the library. Saved music is kept.');req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error)});

db.onversionchange=()=>{db.close();status('Library updated in another tab. Reload to continue.');};
function transaction(mode, action) { return new Promise((resolve,reject)=>{const tx=db.transaction('tracks',mode);const request=action(tx.objectStore('tracks'));tx.oncomplete=()=>resolve(request?.result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error)}) }

const all = ()=>transaction('readonly',s=>s.getAll());

const put = t=>transaction('readwrite',s=>s.put(t));
let assetAliases={};
function resolveTrackId(id){const seen=new Set();while(typeof assetAliases[id]==='string'&&!seen.has(id)){seen.add(id);id=assetAliases[id]}return id}
function saveQueue(value){return new Promise((resolve,reject)=>{const tx=db.transaction('settings','readwrite');tx.objectStore('settings').put(value.map(resolveTrackId),'queue');tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error)})}
function loadQueue(){return new Promise((resolve,reject)=>{const tx=db.transaction('settings','readonly'),store=tx.objectStore('settings'),req=store.get('queue'),aliases=store.get('assetAliases');tx.oncomplete=()=>{assetAliases=aliases.result||{};resolve(req.result?.map(resolveTrackId))};tx.onerror=()=>reject(tx.error)})}

const hash = async data => [...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(n=>n.toString(16).padStart(2,'0')).join('');

const mediaPath = id => `/offline-media/${id}`;

function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=()=>action(fn).catch(e=>status(e.message));return b}

function draw(){const root=$('tracks');root.replaceChildren();const query=$('search').value.toLowerCase();for(const t of tracks.filter(t=>t.name.toLowerCase().includes(query))){const row=document.createElement('div');row.className='track';const info=document.createElement('div'),title=document.createElement('strong'),meta=document.createElement('span');title.textContent=t.name;meta.className='muted';meta.textContent=`${t.variant||'Original'} · ${(t.size/1048576).toFixed(1)} MB · ${t.cached?'Verified on this device':'Laptop connection needed'}`;info.append(title,meta);const actions=document.createElement('div');actions.className='actions';actions.append(button('Play',()=>{queue.push(t.id);drawQueue();return play(queue.length-1)}),button('+ Queue',()=>{queue.push(t.id);drawQueue()}),button(t.favorite?'♥ Favorite':'♡ Favorite',async()=>{t.favorite=!t.favorite;await put(t);draw()}));if(t.cached&&!t.url&&['Original','original'].includes(t.variant)){actions.append(button('Save to laptop',()=>upload(t)));if(processingMime(t))actions.append(button('Queue laptop preview',()=>selectMixed(t)))}if(canPreviewMixed(t))actions.append(button('Preview instrumental',()=>selectMixed(t)),button('Inspect instrumental notes',()=>selectInstrumental(t)));if(t.url&&['Original','original'].includes(t.variant)&&(t.type==='audio/wav'||/\.wav$/i.test(t.name)))actions.append(button('Isolated melody only',()=>selectExtraction(t)));if(!t.cached)actions.append(button('Download',()=>download(t)));else if(t.url)actions.append(button('Remove download',()=>withProcessingLock(()=>removeDownload(t))));else{const keep=button('Keep local copy',()=>{});keep.disabled=true;keep.title='Save to the laptop or refresh its library before removing this local copy.';actions.append(keep)}row.append(info,actions);root.append(row)}drawQueue()}

function drawQueue(){const signature=JSON.stringify(queue);if(signature!==queueSignature){queueSignature=signature;if(current<0)transport?.stop();else transport?.queueChanged(entryAt(current))}saveQueue(queue).catch(e=>status(`Queue could not be saved: ${e.message}`));const root=$('queue');root.replaceChildren();queue.forEach((id,i)=>{const t=tracks.find(t=>t.id===id);const li=document.createElement('li');li.textContent=`${i===current?'▶ ':''}${t?.name||'Unavailable'}`;li.append(button('↑',()=>{transport?.sync();if(i>0){[queue[i-1],queue[i]]=[queue[i],queue[i-1]];if(current===i)current--;else if(current===i-1)current++;drawQueue()}}),button('×',()=>{transport?.sync();queue.splice(i,1);if(i<current)current--;else if(i===current){pausePlayback();current=-1}drawQueue()}));root.append(li)})}

async function storage(){const e=await navigator.storage?.estimate();$('storage').textContent=e?`${((e.usage||0)/1048576).toFixed(1)} MB used here · ${((e.quota||0)/1048576).toFixed(0)} MB browser quota · ${await navigator.storage.persisted()?'Persistence granted':'Persistence not granted'}`:'Storage estimates unavailable'}
async function checkStorageBudget(additional){const estimate=await navigator.storage?.estimate();if(!estimate||!Number.isFinite(estimate.usage))throw Error('Browser storage usage is unavailable. Cannot safely budget this transfer.');const budget=Number($('budget').value)*1048576;if(estimate.usage+additional>budget)throw Error('This transfer needs more room within the device budget, including temporary files and existing browser storage.');if(Number.isFinite(estimate.quota)&&estimate.usage+additional>estimate.quota)throw Error('The browser reports insufficient storage for this transfer.');}

async function cacheBytes(t,bytes){if(bytes.byteLength!==t.size||await hash(bytes)!==t.sha256)throw Error('Audio verification failed. Nothing was added.');const used=[...new Map(tracks.filter(x=>x.cached&&x.sha256!==t.sha256).map(x=>[x.sha256,x.size])).values()].reduce((n,size)=>n+size,0),budget=Number($('budget').value)*1048576;if(!Number.isFinite(budget)||budget<10485760||used+bytes.byteLength>budget)throw Error('Download exceeds this device’s budget. Increase the budget or remove a download.');await checkStorageBudget(bytes.byteLength);const cache=await caches.open(MEDIA);await cache.put(mediaPath(t.sha256),new Response(bytes,{headers:{'Content-Type':t.type||'application/octet-stream','Content-Length':String(bytes.byteLength),'X-Content-SHA256':t.sha256}}));const saved=await cache.match(mediaPath(t.sha256));if(!saved||await hash(await saved.arrayBuffer())!==t.sha256){await cache.delete(mediaPath(t.sha256));throw Error('Stored audio did not pass verification.')}t.cached=true;await put(t);await storage()}

async function importFile(file){if(file.size>250*1048576)throw Error('This preview supports files up to 250 MB.');status(`Verifying ${file.name}…`);const bytes=await file.arrayBuffer(),sha256=await hash(bytes);let t=tracks.find(t=>t.sha256===sha256&&['Original','original'].includes(t.variant));if(!t){t={id:crypto.randomUUID(),sha256,size:bytes.byteLength,name:file.name,type:file.type||'application/octet-stream',variant:'Original',cached:false}}await cacheBytes(t,bytes);if(!tracks.some(x=>x.id===t.id))tracks.push(t);draw();status(`Saved and verified ${file.name} on this device.`)}

async function download(t){await session();status(`Downloading ${t.name}…`);const res=await fetch(t.url);if(!res.ok)throw Error('The serving laptop is unavailable.');await cacheBytes(t,await res.arrayBuffer());draw();status(`${t.name} is verified for local playback.`)}

async function upload(t){await session();const cached=await(await caches.open(MEDIA)).match(mediaPath(t.sha256));if(!cached)throw Error('Local audio is missing. Import it again.');status(`Saving ${t.name} to the laptop…`);const bytes=await cached.arrayBuffer();if(await hash(bytes)!==t.sha256)throw Error('Local audio verification failed.');const response=await fetch('/api/import',{method:'POST',headers:{'Content-Type':t.type||'application/octet-stream','X-Filename':encodeURIComponent(t.name)},body:bytes});if(!response.ok)throw Error(`Laptop save failed (${response.status}). Your verified local copy remains available.`);const item=await response.json();if(item.sha256!==t.sha256)throw Error('Laptop returned a different audio identity.');const old=t.id;t.id=item.id;t.source_id=item.source_id;t.url=`/api/media/${encodeURIComponent(item.id)}`;await put(t);if(old!==t.id){await transaction('readwrite',s=>s.delete(old));queue=queue.map(id=>id===old?t.id:id);tracks=tracks.filter(x=>x===t||x.id!==t.id)}draw();status(`Saved ${t.name} to the laptop library. Your original is preserved.`)}

async function removeDownload(t){if(!t.url)throw Error('Save to the laptop or reconnect its library before removing this copy.');if(!globalThis.navigator?.locks?.request)throw Error('Keep this download for now; safe removal is unavailable in this browser.');if((await processingOutbox.list()).some(row=>row.source.sha256===t.sha256&&!row.receipt&&!['complete','cancelled','failed'].includes(row.status)))throw Error('This original is needed by a pending processing request. Keep it here until laptop receipt is confirmed, or cancel the request.');if(!tracks.some(x=>x.id!==t.id&&x.cached&&x.sha256===t.sha256))await(await caches.open(MEDIA)).delete(mediaPath(t.sha256));t.cached=false;if(!t.url){await transaction('readwrite',s=>s.delete(t.id));tracks=tracks.filter(x=>x.id!==t.id)}else await put(t);draw();await storage();status('Removed from this device. The laptop library was not deleted.')}

async function withProcessingLock(fn){return navigator.locks?.request?navigator.locks.request('wood-processing-sync',fn):fn()}
async function processingBytes(source){
  const saved=await(await caches.open(MEDIA)).match(mediaPath(source.sha256));
  if(!saved)throw Error('The queued original is missing from this device. Restore the original before retrying.');
  const bytes=await saved.arrayBuffer();
  if(bytes.byteLength!==source.size||await hash(bytes)!==source.sha256)throw Error('The queued original failed verification. Keep it for review.');
  return bytes;
}
function checkedElectricProcessingReceipt(receipt,expected){
  const normalized=request=>{
    const allowed=new Set(['asset_id','start_seconds','end_seconds','performers','instrument','guidance',
      'kind','sha256','pipeline_version','runtime_version','renderer_version','electric_planner_version','electric_profile_version']);
    if(!request||typeof request!=='object'||Array.isArray(request)||Object.keys(request).some(key=>!allowed.has(key)))throw Error('Invalid saved processing settings.');
    if(previewSelection(request,request.performers).instrument!=='electric')throw Error('Different saved instrument.');
    const guidance=request.guidance;
    if(guidance!==undefined&&(!guidance||typeof guidance!=='object'||Array.isArray(guidance)||
      Object.keys(guidance).some(key=>!['version','source_notes','arrangement_notes','accompaniment'].includes(key))||
      (guidance.version!==undefined&&guidance.version!==1)))throw Error('Invalid saved preview notes.');
    const notes=previewGuidance(guidance,request.performers);
    if(notes.accompaniment!=='melody-and-bass')throw Error('Different saved accompaniment.');
    return JSON.stringify([request.asset_id,request.start_seconds,request.end_seconds,request.performers,'electric',notes]);
  };
  try{if(normalized(receipt?.request)!==normalized(expected))throw Error('Different saved processing settings.')}
  catch{const error=Error('The laptop operation names different audio or settings. It was not accepted or cancelled.');error.status=409;throw error}
  return receipt;
}
const processingOutbox=new ProcessingOutbox({store:new IndexedProcessingStore(),getBytes:processingBytes,
  upload:processingUpload,
  submit:async value=>{
    if(value.request?.instrument==='electric'&&!ELECTRIC_PREVIEW_ENABLED){
      // Recover an uncertain earlier acceptance without creating gated work.
      let receipt;
      try{receipt=await processingAPI(`/api/processing-queue/${encodeURIComponent(value.operation_id)}`)}
      catch(error){if(error?.status===404)throw Object.assign(Error(`${ELECTRIC_TESTING_REASON} An existing laptop receipt could not be recovered; no new request was sent.`),{status:404});throw error}
      return checkedElectricProcessingReceipt(receipt,value.request);
    }
    return processingAPI('/api/processing-queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  },
  poll:id=>processingAPI(`/api/processing-queue/${encodeURIComponent(id)}`),
  cancel:id=>processingAPI(`/api/processing-queue/${encodeURIComponent(id)}/cancel`,{method:'POST'}),
  onChange:()=>drawProcessing()});
async function processingUpload({source,bytes}){
  const item=await processingAPI('/api/import',{method:'POST',headers:{'Content-Type':source.type,'X-Filename':encodeURIComponent(source.name)},body:bytes},180000);
  const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  if(!item||!uuid.test(item.id)||!uuid.test(item.source_id)||item.sha256!==source.sha256||item.size!==source.size||item.kind!=='original'){
    const error=Error('The laptop returned a different original identity. Your local copy is preserved.');error.status=409;throw error;
  }
  const local=tracks.find(t=>t.id===source.track_id)||tracks.find(t=>t.sha256===source.sha256&&['Original','original'].includes(t.variant));
  if(local){
    const old=local.id,adopted={...local,id:item.id,source_id:item.source_id,type:source.type,url:`/api/media/${encodeURIComponent(item.id)}`};
    const nextQueue=queue.map(id=>id===old?item.id:id);
    // Catalog replacement and listening-queue references commit together;
    // cached bytes and the outbox's immutable source identity remain untouched.
    let committedAliases;
    await new Promise((resolve,reject)=>{const tx=db.transaction(['tracks','settings'],'readwrite');const catalog=tx.objectStore('tracks'),settings=tx.objectStore('settings');catalog.put(adopted);if(old!==item.id)catalog.delete(old);settings.put(nextQueue,'queue');const aliases=settings.get('assetAliases');aliases.onsuccess=()=>{committedAliases={...(aliases.result||{}),...(old===item.id?{}:{[old]:item.id})};settings.put(committedAliases,'assetAliases')};tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||Error('Local catalog adoption stopped.'))});
    assetAliases=committedAliases;
    Object.assign(local,adopted);tracks=tracks.filter(t=>t===local||t.id!==item.id);
    // Map the current in-memory order after commit, preserving edits made while
    // IndexedDB was busy. Durable aliases also repair an older queued write on
    // the next launch if the page closes between these two commits.
    queue=queue.map(resolveTrackId);draw();
  }
  return item;
}
async function processingAPI(url,options={},milliseconds=20000){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),milliseconds);try{return await apiJSON(url,{...options,signal:controller.signal})}finally{clearTimeout(timer)}}
let processingSync=null;
async function syncProcessing(){
  if(restoreBusy||document.visibilityState==='hidden')return;
  if(processingSync)return processingSync;
  // Store transitions are atomic across tabs. Avoid holding a browser lock
  // across networking: cancellation intent must remain immediately writable.
  processingSync=action(()=>processingOutbox.flush()).then(()=>drawProcessing()).catch(error=>{$('processingStatus').textContent=error.message}).finally(()=>{processingSync=null});
  return processingSync;
}
async function drawProcessing(){
  const root=$('processingQueue');if(!root)return;
  const rows=await processingOutbox.list();root.replaceChildren();
  $('processingStatus').textContent=rows.length?'Processing stays on your laptop. Keep this page open to send pending work or check results.':'No processing requests saved on this device.';
  for(const row of rows){
    const li=document.createElement('li'),label=document.createElement('span');
    const states={local:'Saved on this device',waiting:'Waiting for the laptop; receipt not yet confirmed',accepted:{queued:'Received by laptop · queued',waiting:'Received by laptop · waiting for resources',running:'Processing on laptop'}[row.remote_state]||'Received by laptop','cancel-requested':'Cancellation pending on laptop',complete:'Ready on laptop',cancelled:'Cancelled',failed:'Processing failed',attention:'Needs attention'};
    const request=row.request,choice=`${request.instrument==='jazz-bass'?'Jazz Bass':request.instrument==='electric'?'Dry Electric':'Acoustic'} · ${request.performers} player${request.performers===1?'':'s'}${request.bass_part?' · '+request.bass_part.replaceAll('-',' '):''}`;
    label.textContent=`${row.source.name} · ${request.start_seconds}–${request.end_seconds} seconds · ${choice} · ${states[row.status]||row.status}${row.reason?' · '+row.reason:''}`;li.append(label);
    if(request.guidance?.arrangement_notes){const note=document.createElement('p');note.className='muted';note.textContent=request.guidance.arrangement_notes;li.append(note)}
    if(row.status==='complete')li.append(button('Open preview',()=>{if(activeJob||refinementStarting)throw Error('Wait for the current preview or cancel it first.');return viewSavedJob(row.receipt.job_id)}));
    if(!['complete','cancelled','failed'].includes(row.status))li.append(button('Cancel',async()=>{await withProcessingLock(()=>processingOutbox.requestCancel(row.operation_id));await syncProcessing()}));
    if(row.status==='attention')li.append(button('Retry connection',async()=>{await withProcessingLock(()=>processingOutbox.retry(row.operation_id));await syncProcessing()}));
    root.append(li);
  }
}
$('processingSync').onclick=()=>syncProcessing();
window.addEventListener('online',()=>syncProcessing());
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')syncProcessing()});
setInterval(()=>syncProcessing(),20000);

function entryAt(index){const track=tracks.find(t=>t.id===queue[index]);return track?{index,id:track.id,track}:null}
function following(entry){
  if(queue[entry.index]!==entry.id)return null;
  return entryAt(nextQueueIndex(queue,entry.index,{repeat:$('repeat').value,shuffle:$('shuffle').checked}));
}
function announce(entry){current=entry.index;$('now').textContent=entry.track.name;drawQueue();if('mediaSession'in navigator)navigator.mediaSession.metadata=new MediaMetadata({title:entry.track.name,artist:'Your library',album:entry.track.variant||'Original'})}
function mediaDuration(bytes,type,isCurrent){return new Promise((resolve,reject)=>{
  const probe=document.createElement('audio'),url=URL.createObjectURL(new Blob([bytes],{type}));
  let deadline,poll,settled=false;const done=(error,value)=>{if(settled)return;settled=true;clearTimeout(deadline);clearInterval(poll);probe.onloadedmetadata=null;probe.onerror=null;probe.removeAttribute('src');probe.load();URL.revokeObjectURL(url);error?reject(error):resolve(value)};
  probe.preload='metadata';probe.onloadedmetadata=()=>done(null,probe.duration);probe.onerror=()=>done(Error('This format needs native playback.'));
  deadline=setTimeout(()=>done(Error('Track duration could not be checked; use native playback.')),8000);
  poll=setInterval(()=>{if(!isCurrent())done(Error('Preparation cancelled.'))},100);probe.src=url;probe.load();
})}
async function configureEngine(){
  const selected=$('engine').value,token=playGeneration;
  if(selected==='web'||selected==='scheduled'){
    if(!context)context=new AudioContext();
    await context.resume();if(token!==playGeneration)return;
    if(!source){source=context.createMediaElementSource(audio);eq=context.createBiquadFilter();eq.type='lowshelf';eq.frequency.value=220;source.connect(eq);eq.connect(context.destination)}
    eq.gain.value=Number($('bass').value);
    if(!transport)transport=scheduledTransport({context,destination:eq,
      load:(entry,isCurrent)=>prepareBuffer({context,track:entry.track,read:verifiedBytes,metadata:mediaDuration,isCurrent}),
      next:following,onCurrent:announce,onEnded:entry=>{const after=following(entry);if(after)play(after.index).catch(e=>status(e.message))},
      onIssue:message=>{$('engineStatus').textContent=message},
      onPosition:(position,duration,paused)=>{$('transportSeek').max=duration;$('transportSeek').value=position;$('transportTime').textContent=`${Math.floor(position)} / ${Math.floor(duration)} seconds`;$('transportToggle').textContent=paused?'Resume':'Pause'} });
    $('engineStatus').textContent=selected==='scheduled'?'Prepared transitions · device background behavior unverified':'Web Audio EQ · background behavior unverified';
  }else{
    if(eq){eq.gain.value=0;await context.resume();if(token!==playGeneration)return}
    $('engineStatus').textContent=source?'Native controls · Web Audio route remains until page reload':'Native audio · background behavior needs device testing';
  }
  audio.hidden=selected==='scheduled';$('transportControls').hidden=selected!=='scheduled';
}
async function useNativeEngine(){
  transport?.stop();transport=null;
  const volume=$('engine').value==='scheduled'?Number($('transportVolume').value):audio.volume;
  const muted=audio.muted,rate=audio.playbackRate;
  // A media element cannot be detached back from MediaElementAudioSourceNode.
  // Replace that owned element when abandoning a failed context, then rebind.
  if(source){const replacement=audio.cloneNode(false);replacement.removeAttribute('src');audio.pause();audio.replaceWith(replacement);audio=replacement;bindAudioEvents()}
  audio.volume=Number.isFinite(volume)?Math.max(0,Math.min(1,volume)):1;
  audio.muted=Boolean(muted);if(Number.isFinite(rate)&&rate>0)audio.playbackRate=rate;
  source?.disconnect();eq?.disconnect();source=null;eq=null;
  const retired=context;context=null;if(retired)retired.close().catch(()=>{});
  $('engine').value='native';await configureEngine();
}
async function play(index){
  comparison.stop();if(index<0||index>=queue.length)return;
  const entry=entryAt(index);if(!entry)throw Error('Track unavailable.');
  const token=++playGeneration;transport?.stop();audio.pause();
  queueSignature=JSON.stringify(queue);
  if(objectUrl){URL.revokeObjectURL(objectUrl);objectUrl=null}
  if(!await engineWithFallback({configure:configureEngine,useNative:useNativeEngine,isCurrent:()=>token===playGeneration,onIssue:status}))return;
  if($('engine').value==='scheduled'){
    audio.removeAttribute('src');audio.load();current=index;$('now').textContent=`Preparing ${entry.track.name}…`;$('transportToggle').textContent='Pause preparation';drawQueue();
    try{transport.setVolume(Number($('transportVolume').value));transport.setFade(Number($('crossfade').value));const started=await transport.start(entry);if(!started&&token===playGeneration)$('transportToggle').textContent='Resume';return}catch(error){
      if(token!==playGeneration)return;await useNativeEngine();status(`${error.message} Using native playback.`);
    }
  }
  const t=entry.track;
  if(t.cached){const response=await(await caches.open(MEDIA)).match(mediaPath(t.sha256));if(!response){t.cached=false;await put(t);draw();throw Error('Local audio is missing. Download it again.')}const blob=await response.blob();if(token!==playGeneration)return;objectUrl=URL.createObjectURL(blob);audio.src=objectUrl}
  else{if(!t.url)throw Error('No local audio is available.');await session();if(token!==playGeneration)return;audio.src=t.url}
  await playNative({audio,isCurrent:()=>token===playGeneration,onStarted:()=>announce(entry)});
}

async function refresh(){await session();const res=await fetch('/api/library');if(!res.ok)throw Error('Laptop library is unavailable. Local files remain usable.');const payload=await res.json(),items=Array.isArray(payload)?payload:(payload.items||payload.tracks||[]);for(const item of items){const sha256=item.sha256||item.hash;if(!/^[a-f0-9]{64}$/.test(sha256||''))continue;let t=tracks.find(x=>x.id===item.id);if(!t){t={id:item.id,sha256,cached:!!await(await caches.open(MEDIA)).match(mediaPath(sha256))};tracks.push(t)}Object.assign(t,{source_id:item.source_id,name:item.name||item.filename||item.title||'Untitled',size:item.size||item.byte_length||0,type:item.mime||item.mime_type||item.type,url:`/api/media/${encodeURIComponent(item.id)}`,variant:item.variant||item.kind||'Original'});await put(t)}draw();status('Laptop library refreshed.')}

$('files').onchange=async e=>{try{await action(async()=>{for(const file of e.target.files)await importFile(file)})}catch(err){status(err.message)}e.target.value=''};

$('refresh').onclick=()=>action(refresh).catch(e=>status(e.message));$('search').oninput=draw;$('clearQueue').onclick=()=>{queue=[];current=-1;pausePlayback();drawQueue()};

$('previous').onclick=()=>play(current-1).catch(e=>status(e.message));$('next').onclick=()=>next().catch(e=>status(e.message));

async function next(){if(!queue.length)return;let nextIndex=$('shuffle').checked?Math.floor(Math.random()*queue.length):current+1;if(nextIndex>=queue.length&&$('repeat').value==='all')nextIndex=0;await play(nextIndex)}

function bindAudioEvents(){
  const ownedAudio=audio;
  audio.addEventListener('play',()=>{if(audio!==ownedAudio)return;comparison.stop();transport?.pause()});
  audio.addEventListener('seeking',()=>{if(audio===ownedAudio)comparison.stop()});
  audio.onended=()=>{if(audio!==ownedAudio||$('engine').value==='scheduled')return;if($('repeat').value==='one'){audio.currentTime=0;audio.play().catch(e=>status(e.message))}else next().catch(e=>status(e.message))};
  audio.onerror=()=>{if(audio===ownedAudio)status('This file could not be played. Its audio format may not be supported by this device.')};
}
bindAudioEvents();

$('engine').onchange=()=>{pausePlayback();transport?.stop();const token=playGeneration;engineWithFallback({configure:configureEngine,useNative:useNativeEngine,isCurrent:()=>token===playGeneration,onIssue:status}).catch(e=>status(e.message))};
$('transportToggle').onclick=()=>{comparison.stop();if(transport?.pending){pausePlayback();$('transportToggle').textContent='Resume';return}if(!transport?.entry){if(queue.length)play(current<0?0:current).catch(e=>status(e.message));return}if(transport.paused)transport.resume().catch(e=>status(e.message));else transport.pause()};
$('transportSeek').onchange=()=>{comparison.stop();transport?.seek(Number($('transportSeek').value)).catch(e=>status(e.message))};
$('transportVolume').oninput=()=>transport?.setVolume(Number($('transportVolume').value));
$('crossfade').onchange=()=>transport?.setFade(Number($('crossfade').value));
$('shuffle').onchange=$('repeat').onchange=()=>transport?.queueChanged();$('bass').oninput=()=>{if(eq&&['web','scheduled'].includes($('engine').value))eq.gain.value=Number($('bass').value)};

$('sleep').onchange=()=>{clearTimeout(sleepTimer);const minutes=Number($('sleep').value);if(minutes>0)sleepTimer=setTimeout(()=>{comparison.stop();pausePlayback()},minutes*60000)};

$('performers').value=localStorage.getItem('performers')||'1';$('performers').onchange=()=>localStorage.setItem('performers',$('performers').value);

$('budget').value=localStorage.getItem('budget')||'500';$('budget').onchange=()=>localStorage.setItem('budget',$('budget').value);

$('persist').onclick=async()=>{status(await navigator.storage?.persist()?'Browser granted persistent storage. Keep an external backup.':'Browser did not grant persistence. Export a backup to Files.');await storage()};

const base64=bytes=>{let result='';for(let i=0;i<bytes.length;i+=32768)result+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(result)};

$('export').onclick=async()=>{try{const selected=tracks.filter(t=>t.cached);if(selected.reduce((n,t)=>n+t.size,0)>100*1048576)throw Error('This preview export is limited to 100 MB to protect browser memory. Export larger libraries from the laptop.');status('Preparing local export…');const cache=await caches.open(MEDIA),items=[];for(const t of selected){const response=await cache.match(mediaPath(t.sha256));if(!response)throw Error(`Missing local audio: ${t.name}`);const bytes=await response.arrayBuffer();if(await hash(bytes)!==t.sha256)throw Error(`Verification failed: ${t.name}`);items.push({track:t,audio:base64(new Uint8Array(bytes))})}const blob=new Blob([JSON.stringify({format:'wood-string-export',version:1,items,queue:queue.filter(id=>selected.some(t=>t.id===id))})],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='wood-string-library.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);status('Export prepared. Save the download in Files or a backup folder.')}catch(e){status(e.message)}};

$('restore').onchange=async event=>{

  let stagedName, added=[],committed=false;

  try {

    const file=event.target.files[0]; if(!file)return;

    if(pendingActions||activeJob||restoreBusy)throw Error('Wait for the current import, transfer or preview before restoring.');

    if(file.size>145*1048576)throw Error('Export is too large for this preview.');

    restoreBusy=true;document.body.inert=true;status('Checking every track before restore…');

    const plan=await preflightRestore(JSON.parse(await file.text()),tracks,Number($('budget').value)*1048576);

    await checkStorageBudget([...new Map(plan.prepared.map(({track})=>[track.sha256,track.size])).values()].reduce((n,size)=>n+size,0)*2);
    stagedName=`wood-restore-stage-${crypto.randomUUID()}`;

    const staged=await caches.open(stagedName),primary=await caches.open(MEDIA);

    for(const {track,bytes} of plan.prepared){

      await staged.put(mediaPath(track.sha256),new Response(bytes,{headers:{'Content-Type':track.type,'Content-Length':String(bytes.byteLength),'X-Content-SHA256':track.sha256}}));

      const check=await staged.match(mediaPath(track.sha256));

      if(!check||await hash(await check.arrayBuffer())!==track.sha256)throw Error('Staged audio verification failed.');

    }

    status('Publishing verified music…');

    for(const {track} of plan.prepared){

      const key=mediaPath(track.sha256),old=await primary.match(key);

      if(old){if(await hash(await old.arrayBuffer())!==track.sha256)throw Error('An existing downloaded file failed verification. Re-download it before restoring.');continue}

      await primary.put(key,await staged.match(key));added.push(key);

      const check=await primary.match(key);

      if(!check||await hash(await check.arrayBuffer())!==track.sha256)throw Error('Published audio verification failed.');

    }

    await new Promise((resolve,reject)=>{const tx=db.transaction(['tracks','settings'],'readwrite');for(const {track} of plan.prepared)tx.objectStore('tracks').put(track);tx.objectStore('settings').put(plan.queue,'queue');tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error)});

    committed=true;
    // The durable catalog and queue are committed together. Audio is immutable.

    added=[];

    const merged=new Map(tracks.map(t=>[t.id,t]));for(const {track} of plan.prepared)merged.set(track.id,track);

    tracks=[...merged.values()];queue=plan.queue;current=-1;pausePlayback();draw();await storage();status('Restored and verified local music.');

  } catch(e) {

    const primary=await caches.open(MEDIA);for(const key of added)await primary.delete(key);

    status(committed?`Music was restored, but the display could not finish updating: ${e.message}`:`${e.message} Existing library entries and queue were kept.`);

  } finally {

    if(stagedName)await caches.delete(stagedName);

    restoreBusy=false;document.body.inert=false;event.target.value='';

  }

};

if('mediaSession'in navigator){for(const[action,handler]of Object.entries({play:()=>{comparison.stop();return $('engine').value==='scheduled'?transport?.resume():audio.play()},pause:pausePlayback,previoustrack:()=>play(current-1),nexttrack:next,seekto:d=>{if(Number.isFinite(d.seekTime)){comparison.stop();if($('engine').value==='scheduled')transport?.seek(d.seekTime);else audio.currentTime=d.seekTime}}})){try{navigator.mediaSession.setActionHandler(action,handler)}catch{}}}

tracks=await all();try{queue=((await loadQueue())||JSON.parse(localStorage.getItem('queue')||'[]')).filter(id=>tracks.some(t=>t.id===id))}catch{queue=[]}const cache=await caches.open(MEDIA);for(const t of tracks){if(t.cached&&!await cache.match(mediaPath(t.sha256))){t.cached=false;await put(t)}}draw();await storage();status('Ready. Import music to listen locally.');
await drawProcessing().catch(error=>{$('processingStatus').textContent=error.message});
syncProcessing();

if('serviceWorker'in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>status('Offline shell unavailable. Use localhost or a trusted HTTPS connection.'))}

loadBassAvailability();
loadInstrumentalAvailability();
$('instrumentalSaved').hidden=!localStorage.getItem('latestInstrumentalJob');

if(localStorage.getItem('activeJob')){activeJob=localStorage.getItem('activeJob');activeJobKind=localStorage.getItem('activeJobKind');pollJob()}
else {restoreSavedPreview({savedId:localStorage.getItem('latestPreviewJob'),load:apiJSON,isPending:()=>Boolean(activeJob||refinementStarting||$('mixedSubmit').disabled||$('extractSubmit').disabled||$('diagnostic').disabled),show:showJob})}






mountCapture({saveFile:file=>action(()=>importFile(file)),checkBudget:()=>checkStorageBudget(8*1048576),pausePlayback:()=>{comparison.stop();pausePlayback()},isBusy:()=>restoreBusy||pendingActions>0});

if(location.protocol==='https:')$('connectionHelp').hidden=false;
