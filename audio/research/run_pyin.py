"""Bounded research-only pYIN A/B. No playback, model downloads or app edits."""
import os
os.environ['NUMBA_NUM_THREADS']='2'
os.environ['OMP_NUM_THREADS']='2'
import array,bisect,csv,ctypes,json,math,statistics,time,wave
from dataclasses import asdict
from pathlib import Path
import numpy as np
from scipy.signal import resample_poly
import librosa
from audio.research.run_real_audio import score
from audio.extract import extract_wav
from audio.performance import Note,Performance,arrange
from audio.render import render
from tests_audio.test_extract import CASES,write_fixture
P=Path(__file__).parent

def pyin(path):
    with wave.open(str(path)) as w:
        sr=w.getframerate();x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float)/32768
    divisor=math.gcd(sr,22050);y=resample_poly(x,22050//divisor,sr//divisor)
    start=time.perf_counter()
    hz,voiced,confidence=librosa.pyin(y,sr=22050,fmin=70,fmax=1200,frame_length=1024,hop_length=220,center=False)
    elapsed=time.perf_counter()-start
    return {'frames':[{'sample':round((i*220+512)*2),'pitch':float(69+12*math.log2(h/440)) if v else None,'confidence':float(c)} for i,(h,v,c) in enumerate(zip(hz,voiced,confidence))]},elapsed

def align_grid(report,target):
    times=[f['sample'] for f in report['frames']]
    frames=[]
    for f in target['frames']:
        i=bisect.bisect_left(times,f['sample']);j=min([j for j in [i-1,i] if 0<=j<len(times)],key=lambda j:abs(times[j]-f['sample']))
        frames.append(dict(report['frames'][j],sample=f['sample']))
    return {'frames':frames}

def continuity(report):
    pitches=[None if f['pitch'] is None else round(f['pitch']) for f in report['frames']]
    return {'voicing_transitions':sum((a is None)!=(b is None) for a,b in zip(pitches,pitches[1:])),
      'adjacent_rounded_pitch_changes':sum(a!=b for a,b in zip(pitches,pitches[1:]) if a is not None and b is not None)}

def performance(report,label):
    groups=[];current=[];last=None
    for f in report['frames']+[{'sample':999999999,'pitch':None,'confidence':0}]:
        key=round(f['pitch']) if f['pitch'] is not None else None
        if key!=last:
            if current and current[-1]['sample']-current[0]['sample']>=882:groups.append((last,current))
            current=[];last=key
        if key is not None:current.append(f)
    origin=19*44100
    notes=[Note(f'{label}-{i}',max(origin,origin+g[0]['sample']-220),origin+g[-1]['sample']+220,pitch,80,'melody',statistics.mean(f['confidence'] for f in g),contour=tuple((origin+f['sample'],f['pitch']) for f in g)) for i,(pitch,g) in enumerate(groups)]
    return Performance('MusicDelta_80sRock-'+label,44100,notes)

def main():
    baseline=json.loads((P/'extraction-report.json').read_text());reference=[tuple(map(float,r)) for r in csv.reader((P/'reference.csv').open())]
    candidate,cold=pyin(P/'reference-vocal-mono.wav');_,warm=pyin(P/'reference-vocal-mono.wav')
    candidate=align_grid(candidate,baseline)
    result={'settings':{'librosa':'0.11.0','sr':22050,'frame':1024,'hop':220,'center':False,'fmin':70,'fmax':1200,'grid':'nearest pYIN center to baseline source-sample centers'},'cold_pyin_seconds':cold,'warm_pyin_seconds':warm,'real':{},'synthetic':[]}
    for name,report in [('baseline',baseline),('pyin',candidate)]:
        result['real'][name]={'offsets':{str(delta):score(report,reference,19+delta) for delta in [-.05,0,.05]},'continuity':continuity(report)}
    for name,options in CASES.items():
        path=P/f'controlled-{name}.wav';truth=write_fixture(path,*options)
        _,simple=extract_wav(path,name)
        simple={'frames':[dict(f,sample=round(f['sample']*44100/16000)) for f in simple['frames']]}
        report,timing=pyin(path);report=align_grid(report,simple)
        refs=[(f['sample']/44100,next((440*2**((p-69)/12) for s,e,p in truth if s/16000<=f['sample']/44100<e/16000),0)) for f in simple['frames']]
        result['synthetic'].append({'case':name,'baseline':score(simple,refs,0),'pyin':score(report,refs,0),'pyin_seconds':timing})
    ref_report={'frames':[{'sample':round((t-19)*44100),'pitch':69+12*math.log2(h/440) if h>0 else None,'confidence':1.} for t,h in reference if 19<=t<=19+6.8]}
    for label,report in [('pyin',candidate),('annotation-reference',ref_report)]:
        perf=performance(report,label);a=arrange(perf,1);a['render_origin_sample']=19*44100
        (P/(label+'-performance.json')).write_text(json.dumps(asdict(perf),indent=2));(P/(label+'-arrangement.json')).write_text(json.dumps(a,indent=2))
        result[label+'_render']=render(a,P/(label+'-guitar.wav'))
        result[label+'_notes']=len(perf.notes)
    (P/'pyin-frames.json').write_text(json.dumps(candidate,indent=2))
    class Counters(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong)]+[(x,ctypes.c_size_t) for x in ['PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage']]
    counters=Counters();counters.cb=ctypes.sizeof(counters)
    kernel=ctypes.windll.kernel32;kernel.GetCurrentProcess.restype=ctypes.c_void_p
    memory=ctypes.windll.psapi.GetProcessMemoryInfo
    memory.argtypes=[ctypes.c_void_p,ctypes.POINTER(Counters),ctypes.c_ulong];memory.restype=ctypes.c_int
    if not memory(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):raise OSError('Cannot measure process memory')
    result['peak_process_working_set_bytes']=counters.PeakWorkingSetSize
    result['environment_bytes']=sum(p.stat().st_size for p in (P/'pyin-env').rglob('*') if p.is_file())
    (P/'pyin-evidence.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))

if __name__=='__main__':main()
