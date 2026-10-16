import { scheduledTransport, pcm16, prepareBuffer, decodedBytes } from './transport.mjs?v=18';

const rate=48000;
function assert(ok,message){if(!ok)throw Error(message)}
function near(actual,expected,message){assert(Math.abs(actual-expected)<2e-5,`${message}: ${actual} vs ${expected}`)}
async function check(fade){
  const raw=new OfflineAudioContext(2,rate*3,rate);
  // Resume is meaningful only for real-time contexts; all DSP nodes/time here
  // are the actual browser OfflineAudioContext, with no speaker destination.
  const context={get currentTime(){return raw.currentTime},createGain:()=>raw.createGain(),
    createBufferSource:()=>raw.createBufferSource(),resume:async()=>{}};
  const a=raw.createBuffer(2,rate,rate),b=raw.createBuffer(2,rate,rate);
  for(let channel=0;channel<2;channel++){a.getChannelData(channel).fill(.1);b.getChannelData(channel).fill(.2)}
  const eq=raw.createBiquadFilter();eq.type='lowshelf';eq.frequency.value=180;eq.gain.value=0;eq.connect(raw.destination);
  const entries=[{id:'a',index:0},{id:'b',index:1}];let prepared;
  const loaded=new Promise(resolve=>prepared=resolve);
  const player=scheduledTransport({context,destination:eq,setTick:()=>0,clearTick:()=>{},
    load:async entry=>{if(entry.index===1)prepared();return entry.index===0?a:b},
    next:entry=>entries[entry.index+1]});
  player.setFade(fade);await player.start(entries[0]);await loaded;
  // Await the exact preparation continuation before rendering the offline clock.
  await Promise.resolve();await Promise.resolve();
  const output=await raw.startRendering(),data=output.getChannelData(0);
  const sample=time=>data[Math.round(time*rate)];
  near(sample(.01),0,'initial silence');near(sample(.12),.1,'first track');
  if(fade===0){near(sample(1.02-1/rate),.1,'last first-track sample');near(sample(1.02),.2,'first second-track sample')}
  else{near(sample(1.02-fade),.1,'fade begins');near(sample(1.02-fade/2),.15,'linear fade midpoint');near(sample(1.02),.2,'fade completes')}
  near(sample(2.2),0,'final silence');
  assert(data.every(Number.isFinite),'finite output');
  player.stop();eq.disconnect();
  return {case:fade?'linear crossfade':'gapless',passed:true,frames:output.length,sampleRate:output.sampleRate,
    channels:output.numberOfChannels,fadeSeconds:fade,initialStart:.02,nextStart:1.02-fade};
}

export async function runOfflineChecks(){
  if(typeof OfflineAudioContext!=='function')throw Error('OfflineAudioContext unavailable');
  const results=[];
  for(const fade of [0,.4])results.push(await check(fade));
  // Real browser AudioBuffer allocation and interleaving conversion.
  const context=new OfflineAudioContext(2,480,rate),bytes=new ArrayBuffer(44+16),v=new DataView(bytes);
  for(const [at,text] of [[0,'RIFF'],[8,'WAVE'],[12,'fmt '],[36,'data']])for(let j=0;j<text.length;j++)v.setUint8(at+j,text.charCodeAt(j));
  v.setUint32(4,52,true);v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,2,true);
  v.setUint32(24,rate,true);v.setUint32(28,rate*4,true);v.setUint16(32,4,true);v.setUint16(34,16,true);v.setUint32(40,16,true);
  for(let j=0;j<8;j++)v.setInt16(44+j*2,j%2?8192:-8192,true);
  const buffer=pcm16(context,bytes);
  near(buffer.getChannelData(0)[0],-.25,'PCM left');near(buffer.getChannelData(1)[3],.25,'PCM right');
  results.push({case:'PCM16 into real AudioBuffer',passed:true});
  return {passed:true,realOfflineAudioContext:true,audiblePlayback:false,results,
    limits:'Short generated controls only; no device/background/full-song or perceptual acceptance.'};
}

const button=document.getElementById('run'),result=document.getElementById('result');
button.onclick=async()=>{button.disabled=true;result.textContent='Running silent checks…';try{result.textContent=JSON.stringify(await runOfflineChecks(),null,2)}catch(error){result.textContent=JSON.stringify({passed:false,error:error.message},null,2)}finally{button.disabled=false}};

document.getElementById('decodeFile').onchange=async event=>{
  const target=document.getElementById('decodeResult'),file=event.target.files[0];if(!file)return;
  target.textContent='Preparing in memory only…';
  const began=performance.now(),context=new OfflineAudioContext(2,480,44100);
  let digest,metadataDuration;
  try{
    let buffer=await prepareBuffer({context,track:{size:file.size,type:file.type},isCurrent:()=>true,
      read:async()=>{const bytes=await file.arrayBuffer();digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(n=>n.toString(16).padStart(2,'0')).join('');return bytes},
      metadata:async(bytes,type)=>new Promise((resolve,reject)=>{
        const media=document.createElement('audio'),url=URL.createObjectURL(new Blob([bytes],{type}));let timer;
        const done=(error)=>{clearTimeout(timer);metadataDuration=media.duration;media.onloadedmetadata=null;media.onerror=null;media.removeAttribute('src');media.load();URL.revokeObjectURL(url);error?reject(error):resolve(metadataDuration)};
        media.preload='metadata';media.onloadedmetadata=()=>done();media.onerror=()=>done(Error('Metadata decode failed'));
        timer=setTimeout(()=>done(Error('Metadata timeout')),8000);media.src=url;media.load();
      })});
    target.textContent=JSON.stringify({passed:true,file:file.name,sha256:digest,encodedBytes:file.size,
      duration:buffer.duration,metadataDuration,frames:buffer.length,sampleRate:buffer.sampleRate,
      channels:buffer.numberOfChannels,decodedBytes:decodedBytes(buffer),milliseconds:performance.now()-began,
      audiblePlayback:false,uploaded:false,limit:'Preparation only; not playback, background or perceptual acceptance.'},null,2);
    buffer=null;
  }catch(error){target.textContent=JSON.stringify({passed:false,error:error.message},null,2)}
  finally{event.target.value=''}
};
