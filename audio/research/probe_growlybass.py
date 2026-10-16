"""One frozen silent native probe, called in the owned managed subprocess."""
import hashlib,json,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'audio/research/growlybass/probe-001'
SR=44100

def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def run(request,emit):
    import ctypes as C
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import correlate,find_peaks
    from audio.render import Sfizz,_configure_offline
    from audio.growlybass import clean_sustain_map,POLICY
    bank=ROOT/'audio/research/growlybass/bank/Growlybass'
    frozen=json.loads((OUT/'frozen.json').read_text());assert request['frozen_sha256']==sha(OUT/'frozen.json')
    mapping,inventory=clean_sustain_map(bank)
    assert hashlib.sha256(mapping.encode()).hexdigest()==frozen['derived_map_sha256']
    assert inventory==frozen['selected_samples']
    def fundamental(x):
        # Steady-state autocorrelation, independent of SFZ filename/octave names.
        x=np.asarray(x,dtype=np.float64);x=x[int(.15*SR):int(.95*SR)];x-=x.mean()
        corr=correlate(x,x,mode='full',method='fft')[len(x)-1:];corr/=corr[0]
        low,high=int(SR/1500),int(SR/20)
        peaks,_=find_peaks(corr[low:high]);peaks=peaks+low
        if not len(peaks):raise ValueError('No pitch correlation peak')
        lag=int(max(peaks,key=lambda p:corr[p]));frequency=SR/lag
        return {'hz':frequency,'midi':float(69+12*np.log2(frequency/440)),'correlation':float(corr[lag]),'lag_samples':lag}
    samples=[]
    for name,center in [('db2',37),('e2',40),('c3',48),('eb5',75)]:
        path=bank/f'sustain/{name}_f_rr1.wav';sr,x=wavfile.read(path);assert sr==SR
        samples.append({'sample':path.name,'map_center':center,**fundamental(x)})
    emit({'phase':'source-pitch','progress':.2})
    api=Sfizz().lib
    def render(name,events,total,cancel_block=None):
        path=OUT/name;partial=path.with_suffix('.partial');assert not path.exists() and not partial.exists()
        synths=[];qualities=[];block=256
        try:
            for voice in sorted({e['voice'] for e in events}):
                synth=api.sfizz_create_synth();assert synth;synths.append((voice,synth))
                api.sfizz_set_sample_rate(synth,SR);api.sfizz_set_samples_per_block(synth,block)
                qualities.append(_configure_offline(api,synth))
                assert api.sfizz_load_string(synth,str(bank/'probe-derived.sfz').encode(),mapping.encode())
            buffers=[(C.c_float*block)(),(C.c_float*block)()];channels=(C.POINTER(C.c_float)*2)(*buffers)
            result=np.zeros((total,2),dtype=np.float32)
            scheduled={voice:sorted([(round(e['start']*SR),1,e['pitch'],e['velocity']) for e in events if e['voice']==voice]+[(round(e['end']*SR),0,e['pitch'],0) for e in events if e['voice']==voice]) for voice,_ in synths}
            positions={voice:0 for voice,_ in synths}
            partial.write_bytes(b'owned incomplete probe')
            for count,start in enumerate(range(0,total,block)):
                if cancel_block is not None and count>=cancel_block:raise InterruptedError('Deliberate probe cancellation')
                length=min(block,total-start)
                for voice,synth in synths:
                    schedule=scheduled[voice];index=positions[voice]
                    while index<len(schedule) and schedule[index][0]<start+length:
                        at,on,pitch,velocity=schedule[index]
                        (api.sfizz_send_note_on if on else api.sfizz_send_note_off)(synth,at-start,pitch,velocity);index+=1
                    positions[voice]=index;api.sfizz_render_block(synth,channels,2,length)
                    result[start:start+length,0]+=np.ctypeslib.as_array(buffers[0])[:length]
                    result[start:start+length,1]+=np.ctypeslib.as_array(buffers[1])[:length]
            assert np.isfinite(result).all()
            wavfile.write(partial,SR,result);partial.replace(path)
            return result,{'file':name,'sha256':sha(path),'bytes':path.stat().st_size,'peak':float(np.abs(result).max()),'quality':qualities,'mode':'freewheeling'}
        finally:
            for _,synth in synths:api.sfizz_free(synth)
            partial.unlink(missing_ok=True)
    events=frozen['events'];total=round(frozen['duration_seconds']*SR)
    a,one=render('known-commands-a.wav',events,total);emit({'phase':'render-a','progress':.5})
    b,two=render('known-commands-b.wav',events,total)
    assert one['sha256']==two['sha256'],'Repeat render differs'
    assert one['peak']<1,'Raw float render exceeds full scale'
    cancelled=False
    try:render('cancelled.wav',events,total,cancel_block=3)
    except InterruptedError:cancelled=True
    assert cancelled and not (OUT/'cancelled.wav').exists() and not list(OUT.glob('*.partial'))
    # Distinct voices: combined waveform must equal independently rendered sums.
    single=[];independent=[]
    for voice in (1,2):
        audio,info=render(f'voice-{voice}.wav',[e for e in events if e['voice']==voice],total);single.append(audio);independent.append(info)
    residual=float(np.abs(a-(single[0]+single[1])).max());assert residual<=1e-7,residual
    metrics=[]
    for e in events:
        if e['case'].startswith('velocity'):
            data=a[round((e['start']+.02)*SR):round((e['start']+.27)*SR)]
            metrics.append({'velocity':e['velocity'],'rms':float(np.sqrt(np.mean(data.astype(float)**2)))})
    rendered_pitch=[]
    for e in events:
        if e['case'].startswith('pitch'):
            rendered_pitch.append({'command_midi':e['pitch'],**fundamental(a[round(e['start']*SR):round(e['end']*SR),0])})
    rests=[]
    for start,end in frozen['rest_windows']:
        rests.append({'start':start,'end':end,'peak':float(np.abs(a[round(start*SR):round(end*SR)]).max())})
    report={'policy':POLICY,'source_pitch':samples,'rendered_pitch':rendered_pitch,'renders':[one,two,*independent],
            'repeat_identical':True,'independent_voice_max_residual':residual,'velocity_windows':metrics,'rests':rests,
            'cancel_cleanup':True,'played_audio':False}
    (OUT/'result.json').write_text(json.dumps(report,indent=2));emit({'phase':'complete','progress':1})
    return {'report_sha256':sha(OUT/'result.json')}

def main():
    from audio.analysis_runtime import runtime_status
    from audio.managed_process import run_worker
    from audio.growlybass import clean_sustain_map,POLICY
    from audio.research.setup_growlybass import budget,tree_size
    budget(32*1024**2);OUT.mkdir(exist_ok=False)
    mapping,inventory=clean_sustain_map(ROOT/'audio/research/growlybass/bank/Growlybass')
    events=[]
    for i,pitch in enumerate((37,40,48,75)):events.append({'case':'pitch-'+str(pitch),'voice':1,'start':i*1.6,'end':i*1.6+1.1,'pitch':pitch,'velocity':100})
    for i,velocity in enumerate((60,61,95,96,120,121)):events.append({'case':'velocity-'+str(velocity),'voice':1,'start':7+i*.7,'end':7+i*.7+.3,'pitch':40,'velocity':velocity})
    events.extend([{'case':'repeat','voice':1,'start':12,'end':12.3,'pitch':40,'velocity':95},{'case':'repeat','voice':1,'start':12.5,'end':12.8,'pitch':40,'velocity':95},{'case':'duo-a','voice':1,'start':14,'end':15,'pitch':40,'velocity':95},{'case':'duo-b','voice':2,'start':14.2,'end':15.2,'pitch':48,'velocity':95}])
    runtime=runtime_status();assert runtime['ready']
    frozen={'policy':POLICY,'events':events,'duration_seconds':16,'rest_windows':[[6.2,6.4],[11.3,11.8],[13.3,13.8],[15.7,16]],
            'criteria':{'repeat':'Exact float WAV hashes','voices':'sum residual <=1e-7','headroom':'raw peak <1','pitch':'report autocorrelation, do not presume octave','cancellation':'no partial or output remains'},
            'sample_rate':SR,'output_budget':32*1024**2,'runtime_version':runtime['runtime_version'],
            'code_sha256':{name:sha(ROOT/name) for name in ['audio/growlybass.py','audio/render.py','audio/research/probe_growlybass.py']},
            'derived_map_sha256':hashlib.sha256(mapping.encode()).hexdigest(),'selected_samples':inventory}
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2))
    try:
        managed=run_worker('audio.research.probe_growlybass',{'frozen_sha256':sha(OUT/'frozen.json')},python=runtime['python'],wall_seconds=90,memory_bytes=1024**3,progress=lambda e:print(json.dumps(e),flush=True))
        assert tree_size(OUT)<32*1024**2;budget(0)
        (OUT/'supervisor.json').write_text(json.dumps(managed,indent=2));print(json.dumps(managed),flush=True)
    except BaseException as e:
        (OUT/'failure.json').write_text(json.dumps({'error':repr(e)}));raise

if __name__=='__main__':main()
