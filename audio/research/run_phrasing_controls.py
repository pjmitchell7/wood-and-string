"""Silent same-frame comparison; frozen baseline then explicitly separate repair."""
import hashlib
import json
import math
import os
from pathlib import Path
from dataclasses import asdict
os.environ['NUMBA_NUM_THREADS']='2'
os.environ['OMP_NUM_THREADS']='2'
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'phrasing-002'
CASES=('same-pitch-three-plucks','same-pitch-one-sustain','intentional-short-rest',
       'sustain-with-recording-dropout','phrase-accent-pattern','phrase-flat-dynamics')


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,allow_nan=False)


def run(request,emit):
    import librosa
    import numpy as np
    from scipy.signal import resample_poly
    from audio.mixed_analysis import segment_frames
    from audio.phrasing import energy_evidence,segment
    from audio.research.run_basic_pitch_controls import specification,matching,structure
    spec=specification();origin=spec['source_origin_sample'];results={}
    for name in CASES:
        case=next(c for c in spec['cases'] if c['id']==name)
        path=ROOT/'basic-pitch'/'comparison-003'/(name+'-shared.npy')
        x=np.load(path,allow_pickle=False).mean(axis=1).astype(np.float64)
        y=resample_poly(x,1,2)
        hz,voiced,probs=librosa.pyin(y,sr=22050,hop_length=220,center=False,
                                  frame_length=1024,fmin=70,fmax=1200)
        rows=[]
        for i,(f,v,p) in enumerate(zip(hz,voiced,probs)):
            pitch=float(69+12*math.log2(f/440)) if np.isfinite(f) and f>0 else None
            rows.append(dict(sample=(i*220+512)*2,pitch=pitch if v and pitch is not None and p>=.10 else None,
                             candidate_pitch=pitch,confidence=float(p),pyin_voiced=bool(v)))
        production,dropped=segment_frames(rows,'melody',origin,len(x))
        measured=energy_evidence(x,rows,440)
        report=dict(role='melody',hop_samples=440,input=dict(frames=origin+len(x)),
                    analysis_offset_samples=origin,frames=[dict(r,sample=r['sample']+origin) for r in measured])
        phrased,audit=segment(report)
        payload=dict(input_path=str(path),input_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            report=report,raw_local_rows=rows,production=[asdict(n) for n in production],
            production_discarded=dropped,phrasing_v1=[asdict(n) for n in phrased],phrasing_v1_audit=audit)
        save(OUT/(name+'-baseline.json'),payload)
        truth=[dict(n,start=n['start']+origin,end=n['end']+origin) for n in case['notes']]
        results[name]={}
        for policy,events in [('production',payload['production']),('phrasing_v1',payload['phrasing_v1'])]:
            results[name][policy]=dict(events=len(events),strict20ms=matching(events,truth,882),
                within50ms=matching(events,truth,2205),structure=structure(events,truth))
        emit(dict(case=name,done=True))
    save(OUT/'baseline-summary.json',results)
    return results


def main():
    from audio.managed_process import run_worker
    OUT.mkdir(exist_ok=False)
    save(OUT/'frozen-baseline-plan.json',dict(cases=CASES,source='comparison003 shared float arrays unchanged',
        settings=dict(rate=22050,frame_length=1024,hop=220,center=False,fmin=70,fmax=1200,probability_floor=.10),
        baseline_source_sha256=hashlib.sha256((ROOT.parent/'phrasing.py').read_bytes()).hexdigest(),
        comparison='same pitch frames and measured RMS; production versus existing phrasing v1; no new renders',
        evaluation='matching20/50ms maximum cardinality then minimum onset error, full offsets/rests/splits'))
    result=run_worker('audio.research.run_phrasing_controls',{},python=ROOT/'mixed'/'env'/'Scripts'/'python.exe',
                      wall_seconds=120,memory_bytes=2*1024**3)
    save(OUT/'baseline-run.json',result)
    print(json.dumps({c:{p:(v['events'],v['within50ms']['matched']) for p,v in a.items()} for c,a in result['result'].items()},indent=2))


def repair_saved():
    from audio.phrasing import segment_with_attack_association
    from audio.research.run_basic_pitch_controls import specification,matching,structure
    spec=specification();origin=spec['source_origin_sample']
    save(OUT/'frozen-repair-plan.json',dict(lookback_hops=8,minimum_stable_pitch_frames=3,
        minimum_between_frame_rms_fraction=.1,fixed_velocity=80,no_end_changes=True,
        source_sha256=hashlib.sha256((ROOT.parent/'phrasing.py').read_bytes()).hexdigest(),
        uncertainty='Earlier RMS rise may be noise/consonant; pitch unobserved until stable voicedframes',
        cases=CASES,no_new_inference=True,no_tuning_after_results=True))
    results={}
    for name in CASES:
        source=OUT/(name+'-baseline.json');before=hashlib.sha256(source.read_bytes()).hexdigest()
        data=json.loads(source.read_text());notes,audit=segment_with_attack_association(data['report'])
        case=next(c for c in spec['cases'] if c['id']==name)
        truth=[dict(n,start=n['start']+origin,end=n['end']+origin) for n in case['notes']]
        events=[asdict(n) for n in notes]
        result=dict(events=len(events),associated=sum(e['attack_association'] is not None for e in audit['events']),
            strict20ms=matching(events,truth,882),within50ms=matching(events,truth,2205),structure=structure(events,truth))
        save(OUT/(name+'-candidate.json'),dict(baseline_sha256=before,notes=events,audit=audit,summary=result))
        assert hashlib.sha256(source.read_bytes()).hexdigest()==before
        results[name]=result
    save(OUT/'candidate-summary.json',results)
    print(json.dumps({c:dict(events=v['events'],associated=v['associated'],matched20=v['strict20ms']['matched'],matched50=v['within50ms']['matched']) for c,v in results.items()},indent=2))


if __name__=='__main__':
    import sys
    repair_saved() if '--repair-saved' in sys.argv else main()
