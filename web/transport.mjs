// Two-buffer, audio-clock transport. No media is fetched or played at import.
export const ENCODED_LIMIT=64*1048576, DECODED_LIMIT=192*1048576;
export function nextQueueIndex(queue,index,{repeat='off',shuffle=false,random=Math.random}={}){
  if(!queue.length||index<0||index>=queue.length)return -1;
  if(repeat==='one')return index;
  if(shuffle)return Math.min(queue.length-1,Math.floor(random()*queue.length));
  return index+1<queue.length?index+1:repeat==='all'?0:-1;
}
export async function engineWithFallback({configure,useNative,isCurrent,onIssue}){
  try{await configure()}catch(error){
    if(!isCurrent())return false;
    await useNative();if(!isCurrent())return false;
    onIssue(`${error.message} Using native playback.`);
  }
  return isCurrent();
}
export async function playNative({audio,isCurrent,onStarted}){
  try{await audio.play()}catch(error){if(!isCurrent())return false;audio.pause();throw error}
  if(!isCurrent())return false;onStarted();return true;
}
export function decodedBytes(buffer){return buffer.length*buffer.numberOfChannels*4}
export function validateBuffer(buffer){
  if(!Number.isFinite(buffer.duration)||buffer.duration<=0||buffer.duration>600||
     ![1,2].includes(buffer.numberOfChannels)||!Number.isFinite(buffer.sampleRate)||
     buffer.sampleRate<8000||buffer.sampleRate>96000||decodedBytes(buffer)>DECODED_LIMIT)
    throw Error('This track needs native playback (duration, channels or memory limit).');
  return buffer;
}
export function pcm16(context,bytes){
  const v=new DataView(bytes),text=(p,n)=>String.fromCharCode(...new Uint8Array(bytes,p,n));
  if(bytes.byteLength<12||text(0,4)!=='RIFF'||text(8,4)!=='WAVE')return null;
  let format,data;
  for(let p=12;p+8<=bytes.byteLength;){
    const size=v.getUint32(p+4,true),end=p+8+size;
    if(end>bytes.byteLength)throw Error('Incomplete WAV file.');
    if(text(p,4)==='fmt '&&size>=16)format={code:v.getUint16(p+8,true),channels:v.getUint16(p+10,true),rate:v.getUint32(p+12,true),align:v.getUint16(p+20,true),bits:v.getUint16(p+22,true)};
    if(text(p,4)==='data')data={start:p+8,size};
    p=end+(size%2);
  }
  if(!format||!data)throw Error('WAV metadata is incomplete.');
  if(format.code!==1||format.bits!==16)return null;
  const {channels,rate,align}=format;
  if(![1,2].includes(channels)||align!==channels*2||data.size%align)throw Error('Unsupported WAV channel layout.');
  const length=data.size/align;
  validateBuffer({length,numberOfChannels:channels,sampleRate:rate,duration:length/rate});
  const buffer=context.createBuffer(channels,length,rate);
  for(let channel=0;channel<channels;channel++){
    const samples=buffer.getChannelData(channel);
    for(let i=0;i<length;i++)samples[i]=v.getInt16(data.start+(i*channels+channel)*2,true)/32768;
  }
  return buffer;
}

export async function prepareBuffer({context,track,read,metadata,isCurrent}){
  if(!Number.isSafeInteger(track.size)||track.size<=0||track.size>ENCODED_LIMIT)
    throw Error('This large track needs native playback.');
  let bytes=await read(track);
  if(!isCurrent())return null;
  if(bytes.byteLength!==track.size||bytes.byteLength>ENCODED_LIMIT)throw Error('Track size changed.');
  let buffer=pcm16(context,bytes);
  if(!buffer){
    const duration=await metadata(bytes,track.type,isCurrent);
    if(!isCurrent())return null;
    if(!Number.isFinite(duration)||duration<=0||duration>600||duration*context.sampleRate*2*4>DECODED_LIMIT)
      throw Error('This track needs native playback (unknown duration or memory limit).');
    buffer=await context.decodeAudioData(bytes);bytes=null;
  }
  if(!isCurrent())return null;
  return validateBuffer(buffer);
}

export function scheduledTransport({context,destination,load,next,onCurrent=()=>{},onEnded=()=>{},onIssue=()=>{},onPosition=()=>{},
    setTick=fn=>setInterval(fn,100),clearTick=clearInterval}){
  let generation=0,current=null,upcoming=null,tail=null,paused=true,offset=0,timer,fade=0,serial=Promise.resolve(),preparing=false,loading=false,resumeFlight;
  const output=context.createGain();output.connect(destination);
  const discard=voice=>{if(!voice)return;voice.node.onended=null;try{voice.node.stop()}catch{}voice.node.disconnect();voice.gain.disconnect();voice.node.buffer=null};
  function sync(){
    if(!paused&&upcoming&&context.currentTime>=upcoming.when){
      tail=current;current=upcoming;upcoming=null;offset=0;onCurrent(current.entry);
    }
    if(tail&&context.currentTime>=tail.end){discard(tail);tail=null;prefetch()}
    if(current)onPosition(position(),current.buffer.duration,paused);
  }
  function position(){return current?Math.min(current.buffer.duration,paused?offset:current.offset+Math.max(0,context.currentTime-current.when)):0}
  function voice(entry,buffer,when,start=0){
    const node=context.createBufferSource(),gain=context.createGain();node.buffer=buffer;node.connect(gain);gain.connect(output);
    const result={entry,buffer,node,gain,when,offset:start,end:when+buffer.duration-start};
    node.onended=()=>{
      if(paused)return;
      sync();
      if(result===tail){discard(tail);tail=null;prefetch();return}
      if(result===current&&!upcoming){paused=true;offset=buffer.duration;discard(result);clearTick(timer);onPosition(offset,buffer.duration,true);onEnded(entry)}
    };
    node.start(when,start);return result;
  }
  function serialized(entry,token){
    const operation=serial.then(()=>token===generation?load(entry,()=>token===generation):null);
    serial=operation.then(()=>undefined,()=>undefined);return operation;
  }
  async function prefetch(){
    if(paused||!current||upcoming||tail||preparing)return;
    const entry=next(current.entry);if(!entry)return;
    const token=generation,owner=current;preparing=true;
    try{
      const buffer=await serialized(entry,token);
      if(token!==generation||paused||current!==owner||!buffer)return;
      const remaining=owner.end-context.currentTime;
      if(remaining<=.03){onIssue('Next track was not ready in time; a seamless transition is unavailable.');return}
      const overlap=Math.min(fade,buffer.duration/2,(owner.buffer.duration-owner.offset)/2,Math.max(0,remaining-.03));
      const when=owner.end-overlap;
      upcoming=voice(entry,buffer,when);
      if(overlap>0){
        owner.gain.gain.setValueAtTime(1,when);owner.gain.gain.linearRampToValueAtTime(0,owner.end);
        upcoming.gain.gain.setValueAtTime(0,when);upcoming.gain.gain.linearRampToValueAtTime(1,owner.end);
      }
    }catch(error){if(token===generation)onIssue(error.message)}
    finally{if(token===generation)preparing=false}
  }
  function pause(){
    sync();offset=position();paused=true;generation++;preparing=false;loading=false;clearTick(timer);
    discard(upcoming);upcoming=null;discard(tail);tail=null;discard(current);
    if(current)onPosition(offset,current.buffer.duration,true);
  }
  function stop(){pause();current=null;offset=0}
  async function resume(){
    if(!current||!paused)return;
    if(resumeFlight?.token===generation)return resumeFlight.promise;
    const token=generation,flight={token};
    flight.promise=(async()=>{
      await context.resume();if(token!==generation)return;
      if(offset>=current.buffer.duration)offset=0;
      current=voice(current.entry,current.buffer,context.currentTime+.02,offset);paused=false;
      timer=setTick(sync);prefetch();
    })();
    resumeFlight=flight;
    try{await flight.promise}finally{if(resumeFlight===flight)resumeFlight=null}
  }
  async function start(entry){
    stop();const token=generation;loading=true;
    try{
      let buffer=await serialized(entry,token);if(token!==generation||!buffer)return false;
      current={entry,buffer,node:{disconnect(){},stop(){}},gain:{disconnect(){}}};offset=0;
      buffer=null; // A cancelled pending resume must not retain the former buffer here.
      onCurrent(entry);await resume();return token===generation;
    }catch(error){if(token!==generation)return false;throw error
    }finally{if(token===generation)loading=false}
  }
  async function seek(seconds){
    if(!current)return;
    const playing=!paused;pause();offset=Math.max(0,Math.min(current.buffer.duration,Number(seconds)||0));
    onPosition(offset,current.buffer.duration,true);if(playing)await resume();
  }
  function queueChanged(entry){
    if(!current){pause();return}const playing=!paused;pause();if(entry)current.entry=entry;
    if(playing)resume().catch(error=>onIssue(error.message));
  }
  return {start,pause,resume,seek,stop,queueChanged,sync,position,
    setVolume(value){output.gain.value=Math.max(0,Math.min(1,Number(value)||0))},
    setFade(seconds){fade=Math.max(0,Math.min(2,Number(seconds)||0));queueChanged()},
    get pending(){return loading},get paused(){return paused},get entry(){sync();return current?.entry}};
}
