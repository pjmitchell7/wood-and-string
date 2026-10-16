"""Silent private train-fixture experiment. Run with mixed/env Python from app root.
Supervisor bounds wall time and working set; reference stems are evaluation-only.
"""
from pathlib import Path
import ctypes,hashlib,json,os,shutil,subprocess,sys,time,wave
P=Path(__file__).parent/'mixed'
NAMES=['drums','bass','other','vocals']
def memory(pid):
    class C(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in ['peak','working','a','b','c','d','e','f']]
    k=ctypes.windll.kernel32;k.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];k.OpenProcess.restype=ctypes.c_void_p;k.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=k.OpenProcess(0x410,False,pid)
    if not handle:raise OSError('Cannot monitor worker process')
    c=C();c.cb=ctypes.sizeof(c);f=ctypes.windll.psapi.GetProcessMemoryInfo;f.argtypes=[ctypes.c_void_p,ctypes.POINTER(C),ctypes.c_ulong]
    try:
        if not f(handle,ctypes.byref(c),c.cb):raise OSError('Cannot read worker memory')
        return c.peak
    finally:k.CloseHandle(handle)
def read(path):
    import numpy as np
    with wave.open(str(path)) as w:
        assert w.getframerate()==44100 and w.getsampwidth()==2
        return np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype('float32').reshape(-1,w.getnchannels()).T/32768
def write(path,x):
    import numpy as np
    with wave.open(str(path),'wb') as w:
        w.setparams((x.shape[0],2,44100,0,'NONE','not compressed'));w.writeframes((np.clip(x.T,-1,1)*32767).round().astype('<i2').tobytes())
def child():
    (P/'child-pid.partial').write_text(str(os.getpid()));(P/'child-pid.partial').replace(P/'child-pid')
    import torch,torchaudio,numpy as np
    torch.set_num_threads(2);torch.set_num_interop_threads(1)
    assert hashlib.file_digest((P/'model.pt').open('rb'),'sha256').hexdigest()=='85ce4420e0852344b816487b4c4bb821bd406376b93bf684b2b76aa56404c46a'
    model=torchaudio.models.hdemucs_high(sources=NAMES).eval()
    model.load_state_dict(torch.load(P/'model.pt',map_location='cpu',weights_only=True))
    x=torch.from_numpy(read(P/'mixed.wav'));assert x.shape==(2,300032)
    ref=x.mean(0);mean=ref.mean();std=ref.std()
    (P/'ready').write_text('inference-ready')
    t=time.perf_counter()
    with torch.inference_mode():y=model(((x-mean)/std)[None])[0]*std+mean
    seconds=time.perf_counter()-t
    assert y.shape==(4,2,300032) and torch.isfinite(y).all()
    np.save(P/'estimated.partial.npy',y.numpy());(P/'estimated.partial.npy').replace(P/'estimated.npy')
    (P/'inference.json').write_text(json.dumps({'seconds':seconds,'torch':torch.__version__,'torchaudio':torchaudio.__version__,'threads':2,'peak_working_set_bytes':memory(os.getpid()),'input_shape':list(x.shape),'output_shape':list(y.shape),'source_origin_sample':0,'original_song_origin':'unverified','normalization':'official tutorial mono mean/std; inverse per estimated source'},indent=2))
def supervise(cancel=False,worker_script=__file__):
    ready=P/'ready'
    if ready.exists():ready.unlink()
    pidfile=P/'child-pid'
    if pidfile.exists():pidfile.unlink()
    begin=time.perf_counter();peak=0;reason=None;latency=None
    with (P/('cancel.log' if cancel else 'run.log')).open('w') as log:
        proc=subprocess.Popen([sys.executable,worker_script,'child'],stdout=log,stderr=subprocess.STDOUT)
        actual=proc.pid
        try:
            while proc.poll() is None:
                # Windows venv python is a redirector; monitor the actual worker.
                if pidfile.exists():actual=int(pidfile.read_text())
                try:peak=max(peak,memory(actual))
                except OSError:
                    if proc.wait(timeout=1)==0:break
                    raise
                if cancel and ready.exists():reason='requested cancellation after model ready'
                elif time.perf_counter()-begin>180:reason='180 second wall cap'
                elif peak>4*1024**3:reason='4GiB sampled working set threshold'
                elif shutil.disk_usage(P).free<8*1024**3:reason='8GiB disk reserve'
                if reason:
                    t=time.perf_counter()
                    subprocess.run(['taskkill','/PID',str(actual),'/T','/F'],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=10)
                    proc.wait(timeout=10);latency=time.perf_counter()-t;break
                time.sleep(.1)
        finally:
            if proc.poll() is None:
                subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=log,stderr=subprocess.STDOUT,timeout=10)
                proc.wait(timeout=10)
    result={'returncode':proc.returncode,'elapsed_seconds':time.perf_counter()-begin,'peak_observed_working_set_bytes':peak,'stop_reason':reason,'termination_seconds':latency}
    (P/('cancel-supervisor.json' if cancel else 'supervisor.json')).write_text(json.dumps(result,indent=2))
    if not cancel and proc.returncode:raise RuntimeError(result)
    return result
def main():
    import numpy as np
    assert shutil.disk_usage(P).free>8*1024**3
    ff=P.parent/'decoder/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
    for idx,name in enumerate(['mixed']+['reference-'+n for n in NAMES]):
        dest=P/(name+'.wav')
        if not dest.exists():subprocess.run([str(ff),'-v','error','-nostdin','-n','-i',str(P.parent/'fixture.stem.mp4'),'-map',f'0:a:{idx}','-c:a','pcm_s16le',str(dest)],check=True,timeout=30)
    # Cancel before any successful output exists, then run the real inference.
    assert not (P/'estimated.npy').exists()
    cancellation=supervise(True);assert cancellation['stop_reason'] and not (P/'estimated.npy').exists()
    supervise()
    y=np.load(P/'estimated.npy');mix=read(P/'mixed.wav');metrics={}
    def sisdr(est,ref):
        est=est.astype('float64').reshape(-1);ref=ref.astype('float64').reshape(-1)
        est-=est.mean();ref-=ref.mean();target=ref*np.dot(est,ref)/np.dot(ref,ref)
        return float(10*np.log10(np.dot(target,target)/np.dot(est-target,est-target)))
    for i,name in enumerate(NAMES):
        ref=read(P/('reference-'+name+'.wav'));assert ref.shape==mix.shape
        write(P/('estimated-'+name+'.wav'),y[i]);write(P/('estimated-'+name+'-mono.wav'),y[i].mean(0,keepdims=True))
        metrics[name]={'mixture_si_sdr_db':sisdr(mix,ref),'estimate_si_sdr_db':sisdr(y[i],ref),'peak':float(abs(y[i]).max())}
    accompaniment=y[:3].sum(0);write(P/'estimated-accompaniment.wav',accompaniment)
    ref_accomp=sum(read(P/('reference-'+n+'.wav')) for n in NAMES[:3])
    metrics['accompaniment']={'estimate_si_sdr_db':sisdr(accompaniment,ref_accomp)}
    metrics['method']='zero-lag stereo flattened SI-SDR, no time fitting; same AAC fixture local origin0; diagnostic train-track only'
    metrics['input_sha256']=hashlib.file_digest((P/'mixed.wav').open('rb'),'sha256').hexdigest()
    metrics['environment_bytes']=sum(f.stat().st_size for f in (P/'env').rglob('*') if f.is_file())
    metrics['experiment_total_bytes']=sum(f.stat().st_size for f in P.rglob('*') if f.is_file())
    metrics['free_after_bytes']=shutil.disk_usage(P).free
    (P/'metrics.json').write_text(json.dumps(metrics,indent=2));print(json.dumps(metrics,indent=2))
if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='child':child()
    else:main()
