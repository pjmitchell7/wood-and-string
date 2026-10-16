// Durable foreground intent queue. Audio stays in the existing SHA-keyed cache.
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA=/^[0-9a-f]{64}$/;
const STATUSES=new Set(['local','waiting','accepted','cancel-requested','complete','cancelled','failed','attention']);
const TERMINAL=new Set(['complete','cancelled','failed']);
const REMOTE=new Set(['queued','waiting','running','cancelling','complete','cancelled','failed']);
const clone=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));
const record=value=>value&&typeof value==='object'&&!Array.isArray(value)&&[Object.prototype,null].includes(Object.getPrototypeOf(value));
class Attention extends Error {}
function need(test,message){if(!test)throw new Attention(message)}
function canonical(value,depth=0){
  need(depth<=12,'Processing request is nested too deeply.');
  if(value===null||typeof value==='string'||typeof value==='boolean')return JSON.stringify(value);
  if(typeof value==='number'){need(Number.isFinite(value),'Processing numbers must be finite.');return JSON.stringify(value)}
  if(Array.isArray(value))return '['+value.map(v=>canonical(v,depth+1)).join(',')+']';
  need(record(value),'Processing data must contain only JSON values.');
  return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonical(value[key],depth+1)).join(',')+'}';
}
function immutable(source,request){
  need(record(source)&&Object.keys(source).sort().join(',')==='name,sha256,size,track_id,type','Expected verified local source identity.');
  need(SHA.test(source.sha256)&&Number.isSafeInteger(source.size)&&source.size>0&&source.size<=250*1048576,'Invalid local audio hash or size.');
  for(const key of ['name','type','track_id'])need(typeof source[key]==='string'&&source[key].length>0&&source[key].length<=240,'Invalid local source description.');
  need(record(request),'Expected a mixed-preview request.');
  const allowed=new Set(['start_seconds','end_seconds','performers','instrument','bass_part','guidance','electric_tone']);
  need(Object.keys(request).every(k=>allowed.has(k)),'Only bounded mixed-preview fields can be queued; asset_id is assigned by the laptop.');
  const {start_seconds:start,end_seconds:end,performers}=request;
  need(typeof start==='number'&&Number.isFinite(start)&&typeof end==='number'&&Number.isFinite(end)&&start>=0&&end>start&&end-start<=30,'Choose a passage up to 30 seconds.');
  need(Number.isInteger(performers)&&[1,2].includes(performers),'Choose one or two performers.');
  need(request.instrument===undefined||['acoustic','jazz-bass','electric'].includes(request.instrument),'This queue supports acoustic, Electric and Jazz Bass mixed previews.');
  need(request.bass_part===undefined||['melody','bass-line','melody-and-bass'].includes(request.bass_part),'Unsupported bass part.');
  if((request.instrument||'acoustic')!=='jazz-bass')need(request.bass_part===undefined,'Bass role requires Jazz Bass.');
  else need((performers===2?['melody-and-bass']:['melody','bass-line']).includes(request.bass_part||(performers===2?'melody-and-bass':'melody')),'Bass role does not match the performer count.');
  need(request.guidance===undefined||record(request.guidance),'Expected guidance data.');
  if(request.instrument==='electric')need(request.guidance?.accompaniment===undefined||request.guidance.accompaniment==='melody-and-bass','Electric previews use estimated melody and bass together.');
  if(request.electric_tone!==undefined){
    need(request.instrument==='electric'&&record(request.electric_tone),'Tone settings require Electric guitar.');
    const defaults={mode:'bypass',gain_db:0,low_cut_hz:90,high_cut_hz:5500,ambience:'off',ambience_mix:0,overload:'refuse'},given=request.electric_tone;
    need(Object.keys(given).every(key=>Object.hasOwn(defaults,key)),'Unknown tone setting.');
    const tone={...defaults,...given};
    need(['bypass','clean'].includes(tone.mode)&&['off','room','delay'].includes(tone.ambience)&&tone.overload==='refuse','Unsupported tone setting.');
    for(const [key,lo,hi] of [['gain_db',-24,6],['low_cut_hz',60,160],['high_cut_hz',3500,7500],['ambience_mix',0,.25]])
      need(typeof tone[key]==='number'&&Number.isFinite(tone[key])&&tone[key]>=lo&&tone[key]<=hi,'Tone value is outside its bound.');
    need(tone.ambience!=='off'||tone.ambience_mix===0,'Choose ambience before setting its mix.');
    need(tone.mode!=='bypass'||['gain_db','low_cut_hz','high_cut_hz','ambience','ambience_mix'].every(key=>tone[key]===defaults[key]),'Bypass cannot silently ignore tone settings.');
  }
  need(new TextEncoder().encode(canonical(request)).byteLength<=4096,'Processing settings exceed the bounded request size.');
  return canonical({source,request});
}
export function validateOperation(row){
  need(record(row)&&UUID.test(row.operation_id),'Invalid processing operation ID.');
  need(row.fingerprint===immutable(row.source,row.request),'Processing source/settings fingerprint changed.');
  need(STATUSES.has(row.status)&&typeof row.cancel_requested==='boolean'&&typeof row.remote_may_exist==='boolean','Invalid processing state.');
  need(Number.isFinite(row.created_at)&&Number.isFinite(row.updated_at),'Invalid processing timestamp.');
  need(row.reason===null||(typeof row.reason==='string'&&row.reason.length<=2048),'Invalid processing explanation.');
  need(row.remote_state===null||REMOTE.has(row.remote_state),'Invalid laptop processing state.');
  need(row.uploaded_asset_id===null||UUID.test(row.uploaded_asset_id),'Invalid uploaded asset identity.');
  if(row.receipt!==null){
    need(record(row.receipt)&&Object.keys(row.receipt).sort().join(',')==='job_id,operation_id'&&row.receipt.operation_id===row.operation_id&&UUID.test(row.receipt.job_id),'Invalid immutable laptop receipt.');
    need(row.remote_may_exist&&row.uploaded_asset_id!==null,'Laptop receipt lacks durable submission identity.');
  }
  need(row.status!=='accepted'||row.receipt!==null,'Accepted processing lacks a laptop receipt.');
  need(!['complete','failed'].includes(row.status)||row.receipt!==null,'Finished processing lacks a laptop receipt.');
  need(row.remote_state===null||row.receipt!==null,'Laptop state lacks a saved receipt.');
  need(!row.remote_may_exist||row.uploaded_asset_id!==null,'Uncertain submission lacks its saved uploaded identity.');
  need(row.status!=='cancel-requested'||row.cancel_requested,'Missing durable cancellation intent.');
  return row;
}
export function transitionOperation(current,change){
  if(current!==undefined)validateOperation(current);
  const next=change(clone(current));validateOperation(next);
  if(current!==undefined){
    need(next.operation_id===current.operation_id&&next.fingerprint===current.fingerprint&&next.created_at===current.created_at,'Cannot replace an existing operation or its source/settings.');
    need(!current.cancel_requested||next.cancel_requested,'Cannot undo saved cancellation intent.');
    need(!current.remote_may_exist||next.remote_may_exist,'Cannot forget an uncertain laptop submission.');
    need(current.receipt===null||canonical(next.receipt)===canonical(current.receipt),'Laptop receipt changed.');
    need(current.uploaded_asset_id===null||next.uploaded_asset_id===current.uploaded_asset_id,'Uploaded audio identity changed.');
    need(!TERMINAL.has(current.status)||next.status===current.status,'Cannot revive a finished operation.');
  }
  return clone(next);
}

export class IndexedProcessingStore {
  constructor(factory=globalThis.indexedDB){
    this.ready=new Promise((resolve,reject)=>{
      if(!factory){reject(Error('Durable browser storage is unavailable.'));return}
      let failed=false;const request=factory.open('wood-processing-v1',1);
      request.onupgradeneeded=()=>{if(!request.result.objectStoreNames.contains('operations'))request.result.createObjectStore('operations',{keyPath:'operation_id'})};
      request.onblocked=()=>{failed=true;reject(Error('Close other processing tabs before continuing.'))};
      request.onerror=()=>{failed=true;reject(request.error||Error('Processing storage could not open.'))};
      request.onsuccess=()=>{if(failed){request.result.close();return}const db=request.result;db.onversionchange=()=>db.close();resolve(db)};
    });
  }
  async get(id){
    need(UUID.test(id),'Invalid operation identity.');const db=await this.ready;
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('operations','readonly'),request=tx.objectStore('operations').get(id);
      tx.oncomplete=()=>{try{resolve(request.result===undefined?undefined:clone(validateOperation(request.result)))}catch(error){reject(error)}};
      tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||Error('Processing read stopped.'));
    });
  }
  async list(){
    const db=await this.ready;
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('operations','readonly'),request=tx.objectStore('operations').getAll();
      tx.oncomplete=()=>{try{resolve(request.result.map(row=>clone(validateOperation(row))).sort((a,b)=>a.created_at-b.created_at||a.operation_id.localeCompare(b.operation_id)))}catch(error){reject(error)}};
      tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||Error('Processing read stopped.'));
    });
  }
  async update(id,change){
    need(UUID.test(id),'Invalid operation identity.');const db=await this.ready;
    return new Promise((resolve,reject)=>{
      const tx=db.transaction('operations','readwrite'),store=tx.objectStore('operations'),request=store.get(id);let result,failure;
      request.onsuccess=()=>{try{result=transitionOperation(request.result,change);need(result.operation_id===id,'Operation key changed.');store.put(result)}catch(error){failure=error;tx.abort()}};
      tx.oncomplete=()=>resolve(clone(result));tx.onerror=()=>reject(failure||tx.error);tx.onabort=()=>reject(failure||tx.error||Error('Processing write stopped.'));
    });
  }
}

const defaultHash=async bytes=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(n=>n.toString(16).padStart(2,'0')).join('');
export class ProcessingOutbox {
  constructor({store=new IndexedProcessingStore(),getBytes,upload,submit,poll,cancel,hashBytes=defaultHash,uuid=()=>crypto.randomUUID(),now=()=>Date.now(),onChange=()=>{}}={}){
    Object.assign(this,{store,getBytes,upload,submit,poll,cancel,hashBytes,uuid,now,onChange});this.inflight=null;
  }
  list(){return this.store.list()}
  async changed(id,fn){const row=await this.store.update(id,fn);try{await this.onChange(clone(row))}catch{}return row}
  async enqueue(source,request,{operation_id=this.uuid()}={}){
    const fingerprint=immutable(source,request),created=this.now();need(UUID.test(operation_id),'Expected UUID operation identity.');
    return this.changed(operation_id,current=>{
      if(current){need(current.fingerprint===fingerprint,'That operation already names different audio/settings.');return current}
      return {operation_id,source:clone(source),request:clone(request),fingerprint,status:'local',receipt:null,uploaded_asset_id:null,
        cancel_requested:false,remote_may_exist:false,remote_state:null,reason:null,created_at:created,updated_at:created};
    });
  }
  async requestCancel(id){
    return this.changed(id,row=>{
      need(row,'Processing operation was not found.');if(TERMINAL.has(row.status))return row;
      return {...row,cancel_requested:true,status:row.remote_may_exist?'cancel-requested':'cancelled',reason:null,updated_at:this.now()};
    });
  }
  async retry(id){
    return this.changed(id,row=>{need(row,'Processing operation was not found.');need(row.status==='attention','Only an operation needing attention can be retried.');
      return {...row,status:row.cancel_requested?'cancel-requested':'waiting',reason:null,updated_at:this.now()};});
  }
  async failure(id,error,{local=false}={}){
    const status=Number(error?.status),attention=local||error instanceof Attention||status===507||(status>=400&&status<500&&![408,425,429].includes(status));
    return this.changed(id,row=>{if(TERMINAL.has(row.status))return row;
      return {...row,status:attention?'attention':row.cancel_requested?'cancel-requested':'waiting',reason:String(error?.message||'Laptop connection interrupted; acceptance is unknown.').slice(0,2048),updated_at:this.now()};});
  }
  async accept(id,value){
    need(record(value)&&value.operation_id===id&&UUID.test(value.job_id)&&REMOTE.has(value.state),'Laptop returned an invalid processing receipt.');
    need(value.reason===null||value.reason===undefined||typeof value.reason==='string','Laptop returned an invalid processing explanation.');
    return this.changed(id,row=>{
      const receipt={operation_id:id,job_id:value.job_id};
      need(row.receipt===null||canonical(row.receipt)===canonical(receipt),'Laptop changed the saved processing receipt.');
      if(TERMINAL.has(row.status))return row;
      const status=TERMINAL.has(value.state)?value.state:row.cancel_requested||value.state==='cancelling'?'cancel-requested':'accepted';
      return {...row,receipt,cancel_requested:row.cancel_requested||value.state==='cancelling',status,remote_state:value.state,
        reason:value.reason?.slice(0,2048)||null,updated_at:this.now()};
    });
  }
  flush(){
    if(this.inflight)return this.inflight;
    this.inflight=this.run().finally(()=>{this.inflight=null});return this.inflight;
  }
  async run(){
    for(const item of await this.list()){
      if(TERMINAL.has(item.status)||item.status==='attention')continue;
      try{await this.step(item.operation_id)}catch(error){await this.failure(item.operation_id,error)}
    }
    return this.list();
  }
  async step(id){
    let row=await this.store.get(id);if(!row||TERMINAL.has(row.status)||row.status==='attention')return;
    if(!row.receipt){
      if(!row.uploaded_asset_id){
        let bytes;
        try{
          const value=await this.getBytes(clone(row.source));
          need(value instanceof ArrayBuffer||value instanceof Uint8Array,'Verified local audio is missing.');
          bytes=value instanceof Uint8Array?value:new Uint8Array(value);
          need(bytes.byteLength===row.source.size&&await this.hashBytes(bytes)===row.source.sha256,'Local audio hash or size changed. Original retained for review.');
        }catch(error){await this.failure(id,error,{local:true});return}
        row=await this.store.get(id);if(TERMINAL.has(row.status))return;
        const asset=await this.upload({source:clone(row.source),bytes,operation_id:id});
        need(record(asset)&&UUID.test(asset.id)&&asset.sha256===row.source.sha256&&asset.size===row.source.size,'Laptop returned a different audio identity.');
        row=await this.changed(id,current=>{if(TERMINAL.has(current.status))return current;
          need(current.uploaded_asset_id===null||current.uploaded_asset_id===asset.id,'Laptop changed uploaded audio identity.');
          return {...current,uploaded_asset_id:asset.id,updated_at:this.now()};});
      }
      // Commit uncertainty BEFORE the request can reach the laptop. Read the
      // latest row atomically so another tab's cancellation cannot be undone.
      row=await this.changed(id,current=>{
        if(TERMINAL.has(current.status)||current.status==='attention')return current;
        if(current.cancel_requested&&!current.remote_may_exist)return {...current,status:'cancelled',updated_at:this.now()};
        return {...current,remote_may_exist:true,status:current.cancel_requested?'cancel-requested':'waiting',updated_at:this.now()};
      });
      if(TERMINAL.has(row.status)||row.status==='attention')return;
      row=await this.accept(id,await this.submit({operation_id:id,request:{...clone(row.request),asset_id:row.uploaded_asset_id}}));
    }else row=await this.accept(id,await this.poll(id));
    row=await this.store.get(id);
    if(row.cancel_requested&&!TERMINAL.has(row.status))await this.accept(id,await this.cancel(id));
  }
}
