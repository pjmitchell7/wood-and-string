import { ForegroundCapture } from './capture.mjs';
import { CaptureJournal, takeLease } from './capture-journal.mjs';

export function mountCapture({saveFile,checkBudget,pausePlayback,isBusy,createCapture=options=>new ForegroundCapture(options),journal=new CaptureJournal(),lease=takeLease,documentRef=globalThis.document,windowRef=globalThis.window}) {
  const $=id=>documentRef.getElementById(id);
  let capture,pendingTake,pendingRecord,saving=false,starting=false,recovering=true,startRequest;
  const message=text=>$('captureStatus').textContent=text;
  const active=()=>capture&&['requesting','recording','paused','saving'].includes(capture.state);
  function state(value){
    const recording=value==='recording',paused=value==='paused';
    $('captureStart').disabled=starting||recovering||active()||saving||!!pendingTake;
    $('capturePause').hidden=!(recording||paused);$('capturePause').textContent=paused?'Resume':'Pause';
    $('captureStop').hidden=!(recording||paused);$('captureCancel').hidden=!(starting||recording||paused||value==='requesting');
    if(value==='requesting')message('Preparing local recovery storage or waiting for microphone permission...');
    if(recording)message('Recording on this device. Delivered chunks are saved locally; keep this page open.');
    if(paused)message('Paused. The 30-second session limit still applies.');
    if(value==='saving')message('Stopping microphone and keeping this take...');
  }
  const makeFile=(blob,name)=>new File([blob],`${name}.${blob.type==='audio/mp4'?'m4a':blob.type==='audio/webm'?'webm':'audio'}`,{type:blob.type});
  async function recoverNext(){
    if(pendingTake||active()||starting)return;
    recovering=true;state('recovering');
    let skippedPartials=0;
    try{for(const saved of await journal.list()){
      if(!saved.chunks.length)continue;
      const held=await lease(saved.id);if(!held)continue;
      if(!held.exclusive&&!saved.complete){held.release();skippedPartials++;continue}
      let recovered;try{recovered=await journal.recover(saved)}catch(error){held.release();throw error}
      if(!recovered){held.release();continue}
      pendingTake=makeFile(recovered.blob,recovered.name);pendingRecord={id:saved.id,held,owned:false};
      message(`Recovered ${recovered.chunkCount} previously saved recording chunks. ${recovered.complete?'The recorder reached its stop event.':'The last audio chunk or container ending may be missing; the file may not play.'} Save locally or to Files. Other temporary takes are retained.`);
      $('captureFallback').hidden=false;$('captureRetry').hidden=false;break;
    }if(skippedPartials&&!pendingTake)message('Incomplete recording chunks are retained, but this browser cannot coordinate safe recovery across tabs. Use a browser with Web Locks support for interruption recovery.');}catch(error){message(`Temporary recording recovery needs attention: ${error.message}`)}
    finally{recovering=false;state('idle')}
  }
  async function saveTake(){
    if(!pendingTake||saving)return;saving=true;state('saving');$('captureRetry').hidden=true;
    try{await saveFile(pendingTake);const name=pendingTake.name,record=pendingRecord;pendingTake=null;pendingRecord=null;
      if(record){try{await journal.remove(record.id)}catch{message('Library copy saved; a duplicate temporary recovery copy remains.')}finally{record.held?.release()}}
      message(`Saved and verified ${name} on this device. Use Save to laptop in the library when you want to transfer it.`);$('captureFallback').hidden=true;
    }catch(error){message(`The take has not been saved to the library: ${error.message} Keep this page open and save it to Files or retry. Delivered journal chunks remain available for recovery.`);$('captureFallback').hidden=false;$('captureRetry').hidden=false}
    finally{saving=false;state('complete');if(!pendingTake)await recoverNext()}
  }
  $('captureStart').onclick=async()=>{
    // Reserve ownership before the first await; repeated Start cannot pass the gate.
    if(starting||recovering)return;
    starting=true;const request={cancelled:false};startRequest=request;state('requesting');
    let id,held,take;
    try{
      if(active()||pendingTake||saving)throw Error('Keep or save the current take first.');
      if(isBusy())throw Error('Wait for the current library operation before recording.');
      await checkBudget();if(request.cancelled)throw Error('Recording setup cancelled.');
      pausePlayback();id=crypto.randomUUID();const name=`Recording ${new Date().toISOString().replace(/[:.]/g,'-')}`;
      held=await lease(id);if(!held)throw Error('This recording is already owned by another tab.');
      await journal.begin(id,name);if(request.cancelled)throw Error('Recording setup cancelled.');
      $('capturePanel').open=true;$('captureElapsed').textContent='0 / 30 seconds';
      take=createCapture({onState:state,onChunk:blob=>journal.append(id,blob),onMeter:level=>{$('captureLevel').dataset.level=level.state;$('captureLevel').textContent=level.label;$('captureElapsed').textContent=`${Math.floor((level.elapsed||0)/1000)} / 30 seconds`}});
      capture=take;await take.start();
      take.finished.then(async result=>{
        if(result.journalComplete!==false)try{await journal.complete(id)}catch{}
        pendingTake=makeFile(result.blob,name);pendingRecord={id,held,owned:true};
        $('captureFormat').textContent=`Recorded as ${result.blob.type}. Melody extraction requires an isolated PCM16 mono WAV and is unavailable for this recording. Interruption can lose an undelivered final chunk.`;
        if(result.warning)message(`${result.warning} Keeping the captured portion.`);
        await saveTake();
      }).catch(error=>{held?.release();message(error.message);state('failed')});
    }catch(error){take?.stop({cancelled:true});if(id)try{const saved=(await journal.list()).find(t=>t.id===id);if(saved&&!saved.chunks.length)await journal.remove(id)}catch{}held?.release();message(error.name==='NotAllowedError'?'Microphone permission was not granted. Nothing was recorded.':error.message)}
    finally{if(startRequest===request)startRequest=null;starting=false;state(capture?.state||'idle')}
  };
  const stopSetupOrTake=()=>{if(startRequest)startRequest.cancelled=true;if(active())capture.stop({cancelled:true})};
  $('capturePause').onclick=()=>{if(capture?.state==='paused')capture.resume();else capture?.pause()};
  $('captureStop').onclick=()=>capture?.stop();$('captureCancel').onclick=stopSetupOrTake;
  $('captureRetry').onclick=saveTake;
  $('captureFallback').onclick=()=>{if(!pendingTake)return;const url=URL.createObjectURL(pendingTake),link=documentRef.createElement('a');link.href=url;link.download=pendingTake.name;link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);message('Save the take in Files or downloads. The local recovery journal is retained until library saving succeeds.');};
  documentRef.addEventListener('visibilitychange',()=>{if(documentRef.visibilityState==='hidden')stopSetupOrTake()});
  windowRef.addEventListener('pagehide',stopSetupOrTake);
  windowRef.addEventListener('beforeunload',event=>{if(starting||active()||pendingTake||saving){event.preventDefault();event.returnValue=''}});
  const ready=recoverNext();return {ready};
}
