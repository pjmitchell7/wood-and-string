"""Silent UMX-HQ comparison; official MIT weights; no automatic downloads."""
from pathlib import Path
import hashlib,json,os,shutil,sys,time
import run_mixed as base
P=Path(__file__).parent/'umx';M=P.parent/'mixed';base.P=P
NAMES=['vocals','drums','bass','other']
def child():
    (P/'child-pid.partial').write_text(str(os.getpid()));(P/'child-pid.partial').replace(P/'child-pid')
    import torch,openunmix,numpy as np
    torch.set_num_threads(2);torch.set_num_interop_threads(1)
    model=openunmix.umxhq(targets=NAMES,pretrained=False,niter=1,wiener_win_len=300,device='cpu')
    manifest=json.loads((P/'manifest.json').read_text())
    for name in NAMES:
        item=next(i for i in manifest if i['name'].startswith(name+'-'));path=P/item['name']
        assert hashlib.file_digest(path.open('rb'),'sha256').hexdigest()==item['sha256']
        state=torch.load(path,map_location='cpu',weights_only=True)
        # Historical checkpoint carries transform buffers; current factory owns
        # the STFT. Official loader uses strict=False; narrow that exception.
        extras=set(state)-set(model.target_models[name].state_dict())
        assert extras=={'sample_rate','stft.window','transform.0.window'},extras
        assert float(state['sample_rate'])==44100
        for key in extras:del state[key]
        model.target_models[name].load_state_dict(state,strict=True)
    model.eval();x=torch.from_numpy(base.read(M/'mixed.wav'));assert x.shape==(2,300032)
    (P/'ready').write_text('ready');t=time.perf_counter()
    with torch.inference_mode():y=model(x[None])[0]
    seconds=time.perf_counter()-t
    assert y.shape==(4,2,300032) and torch.isfinite(y).all()
    np.save(P/'estimated.partial.npy',y.numpy());(P/'estimated.partial.npy').replace(P/'estimated.npy')
    (P/'inference.json').write_text(json.dumps({'seconds':seconds,'peak_working_set_bytes':base.memory(os.getpid()),'sample_rate':44100,'source_origin_sample':0,'original_song_origin':'unverified','shape':list(y.shape),'targets':NAMES,'package':'openunmix1.3.0','torch':torch.__version__,'niter':1,'wiener_win_len':300,'normalization':'official model internal normalization only','weights_only':True,'state_dict_strict_after_excluding_legacy_transform_buffers':True},indent=2))
def main():
    import numpy as np
    assert not (P/'estimated.npy').exists()
    c=base.supervise(True,__file__);assert c['stop_reason'] and not (P/'estimated.npy').exists()
    base.supervise(False,__file__)
    y=np.load(P/'estimated.npy');mix=base.read(M/'mixed.wav');metrics={}
    def sisdr(est,ref):
        e=est.astype(float).reshape(-1);r=ref.astype(float).reshape(-1);e-=e.mean();r-=r.mean();t=r*np.dot(e,r)/np.dot(r,r)
        return float(10*np.log10(np.dot(t,t)/np.dot(e-t,e-t)))
    for i,n in enumerate(NAMES):
        r=base.read(M/('reference-'+n+'.wav'));assert r.shape==mix.shape
        base.write(P/('estimated-'+n+'.wav'),y[i]);base.write(P/('estimated-'+n+'-mono.wav'),y[i].mean(0,keepdims=True))
        x=y[i].mean(0);ref=r.mean(0);size=1<<(2*len(x)-1).bit_length();c=np.fft.irfft(np.fft.rfft(x,size)*np.conj(np.fft.rfft(ref,size)),size);lags=np.arange(-1024,1025)
        metrics[n]={'mixture_si_sdr_db':sisdr(mix,r),'estimate_si_sdr_db':sisdr(y[i],r),'diagnostic_xcorr_peak_lag_samples':int(lags[np.argmax(c[lags%size])]),'sha256':hashlib.file_digest((P/('estimated-'+n+'.wav')).open('rb'),'sha256').hexdigest()}
    accompaniment=y[1:].sum(0);base.write(P/'estimated-accompaniment.wav',accompaniment)
    metrics['accompaniment_si_sdr_db']=sisdr(accompaniment,sum(base.read(M/('reference-'+n+'.wav')) for n in NAMES[1:]))
    metrics['method']='zero-lag stereo concatenated SI-SDR; xcorr diagnostic only, no time fitting/correction; training-track only'
    metrics['input_sha256']=hashlib.file_digest((M/'mixed.wav').open('rb'),'sha256').hexdigest()
    metrics['free_after_bytes']=shutil.disk_usage(P).free
    metrics['directory_bytes']=sum(f.stat().st_size for f in P.rglob('*') if f.is_file())
    (P/'metrics.json').write_text(json.dumps(metrics,indent=2));print(json.dumps(metrics,indent=2))
if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='child':child()
    else:main()
