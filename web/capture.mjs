// Foreground-only recorder. Dependency injection keeps lifecycle tests off real microphones.
export function assessLevel(samples) {
  let square=0,peak=0,clipped=0;
  for(const value of samples){const magnitude=Math.abs(value);square+=value*value;peak=Math.max(peak,magnitude);if(magnitude>=.98)clipped++}
  const rms=Math.sqrt(square/Math.max(1,samples.length)),db=20*Math.log10(Math.max(rms,1e-8));
  if(clipped>0)return {state:'red',label:'Possible clipping · move farther from the microphone',db,peak};
  if(db < -45)return {state:'amber',label:'Too quiet · move closer or play a little louder',db,peak};
  return {state:'green',label:'Level in range · noise and musical clarity not assessed',db,peak};
}

export class ForegroundCapture {
  constructor({mediaDevices=globalThis.navigator?.mediaDevices,Recorder=globalThis.MediaRecorder,AudioContext=globalThis.AudioContext||globalThis.webkitAudioContext,onState=()=>{},onMeter=()=>{},onChunk=async()=>{},setTimer=(fn,ms)=>globalThis.setTimeout(fn,ms),clearTimer=id=>globalThis.clearTimeout(id),setTick=(fn,ms)=>globalThis.setInterval(fn,ms),clearTick=id=>globalThis.clearInterval(id),now=()=>performance.now()}={}) {
    Object.assign(this,{mediaDevices,Recorder,AudioContext,onState,onMeter,onChunk,setTimer,clearTimer,setTick,clearTick,now});
    this.state='idle';this.chunks=[];this.limitMs=30000;this.cancelled=false;this.stopping=false;this.chunkWrites=Promise.resolve();
  }
  emit(state){this.state=state;this.onState(state)}
  async start(){
    if(this.state!=='idle')throw Error('A recording is already active.');
    if(!this.mediaDevices?.getUserMedia||!this.Recorder)throw Error('Microphone recording is unavailable here. Use a supported browser on localhost or trusted HTTPS.');
    this.emit('requesting');
    try{
      this.stream=await this.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false});
      if(this.stopRequested)throw Error('Recording setup was cancelled before capture began.');
      const type=['audio/webm;codecs=opus','audio/mp4;codecs=mp4a.40.2','audio/mp4','audio/webm'].find(type=>this.Recorder.isTypeSupported(type));
      this.recorder=new this.Recorder(this.stream,type?{mimeType:type,audioBitsPerSecond:128000}:{});
      this.finished=new Promise((resolve,reject)=>{this.resolve=resolve;this.reject=reject});
      this.finished.catch(()=>{}); // A setup interruption can precede the caller attaching its handler.
      this.recorder.addEventListener('dataavailable',event=>{if(event.data?.size){this.chunks.push(event.data);this.chunkWrites=this.chunkWrites.then(()=>this.onChunk(event.data)).catch(error=>{this.journalError=true;this.recordingError=`Temporary recording save failed: ${error.message}`;this.stop()})}});
      this.recorder.addEventListener('stop',()=>this.finish());
      this.recorder.addEventListener('error',event=>{this.recordingError=event.error?.message||'The microphone recorder stopped unexpectedly.';this.stop()});
      for(const track of this.stream.getTracks())track.addEventListener?.('ended',()=>{if(!this.stopping)this.stop()});
      if(this.stopRequested)throw Error('Recording setup was cancelled before capture began.');
      this.started=this.now();this.recorder.start(250);this.emit('recording');
      this.limit=this.setTimer(()=>this.stop(),this.limitMs);
      this.tick=this.setTick(()=>{const elapsed=Math.min(this.limitMs,this.now()-this.started);if(this.state==='recording'&&this.analyser){this.analyser.getFloatTimeDomainData(this.samples);this.onMeter({...assessLevel(this.samples),elapsed})}else this.onMeter({state:'amber',label:this.state==='paused'?'Paused · noise not assessed':'Level unavailable · noise not assessed',db:null,elapsed})},100);
      // Optional metering cannot delay recording, its timer, or microphone release.
      void this.setupMeter();
      return this;
    }catch(error){await this.cleanup();this.emit('failed');throw error}
  }
  pause(){if(this.state==='recording'){this.recorder.pause();this.emit('paused')}}
  resume(){if(this.state==='paused'){this.recorder.resume();this.emit('recording')}}
  async setupMeter(){try{if(this.AudioContext&&!this.stopping){const context=new this.AudioContext();this.context=context;await context.resume();if(this.stopping||this.stopRequested){try{context.close()?.catch(()=>{})}catch{};return}this.input=context.createMediaStreamSource(this.stream);this.analyser=context.createAnalyser();this.analyser.fftSize=2048;this.input.connect(this.analyser);this.samples=new Float32Array(this.analyser.fftSize)}}catch{if(!this.stopping)this.onMeter({state:'amber',label:'Live level unavailable · noise not assessed',db:null})}}
  stop({cancelled=false}={}){
    this.cancelled ||= cancelled;
    if(this.state==='requesting'){this.stopRequested=true;this.stopping=true;this.cleanup();this.emit('cancelled');return this.finished}
    if(this.stopping||!this.recorder)return this.finished;
    this.stopping=true;this.emit('saving');this.clearTimer(this.limit);this.clearTick(this.tick);
    if(this.recorder.state!=='inactive'){try{this.recorder.stop()}catch(error){this.recordingError=error.message;this.finish()}}else this.finish();
    this.cleanup(); // Release acquired tracks synchronously, before final data callbacks.
    return this.finished;
  }
  cleanup(){this.clearTimer(this.limit);this.clearTick(this.tick);for(const track of this.stream?.getTracks()||[])if(!this.stoppedTracks?.has(track)){this.stoppedTracks??=new Set();this.stoppedTracks.add(track);track.stop()}try{this.input?.disconnect();this.analyser?.disconnect();this.context?.close()?.catch(()=>{})}catch{}}
  async finish(){
    if(this.finishedOnce)return;this.finishedOnce=true;this.cleanup();await this.chunkWrites;
    const type=(this.recorder?.mimeType||this.chunks[0]?.type||'application/octet-stream').split(';')[0],blob=new Blob(this.chunks,{type});
    this.chunks=[];
    if(!blob.size){this.emit('failed');this.reject(Error(this.recordingError||'No audio was captured. Your existing music is unchanged.'));return}
    this.emit('complete');this.resolve({blob,cancelled:this.cancelled,warning:this.recordingError||null,journalComplete:!this.journalError,elapsedMs:this.now()-this.started});
  }
}
