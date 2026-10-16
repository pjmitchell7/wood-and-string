"""Silent, bounded research worker; all musical evidence remains private."""
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'basic-pitch'/'comparison-001'


def save(path,value):
    with path.open('x',encoding='utf-8') as f:
        json.dump(value,f,indent=2,allow_nan=False)


def run(request,emit):
    sys.path.insert(0,str(ROOT/'basic-pitch'/'runtime'))
    import numpy as np
    from audio.basic_pitch import analyze_pcm
    case=request['case']
    started=time.monotonic()
    if case=='estimated-other':
        path=ROOT/'umx'/'estimated.npy'
        x=np.load(path,allow_pickle=False)[3].mean(axis=0)
        provenance='UMX-HQ float other estimate from mixed stream; not isolated guitar truth'
    elif case=='silence':
        x=np.zeros(44100*4,np.float32);path=None;provenance='digital silence control'
    else:
        path={'mixture':ROOT/'umx'/'mixed.wav',
              'reference-other':ROOT/'mixed'/'reference-other.wav',
              'known-guitar':OUT/'known-guitar.wav'}[case]
        with wave.open(str(path)) as w:
            assert w.getframerate()==44100 and w.getsampwidth()==2
            x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32).reshape(-1,w.getnchannels()).mean(axis=1)/32768
        provenance={'mixture':'original fixture mixed stream; no separation',
                    'reference-other':'provided same-fixture other stem; evaluation comparator, not event truth',
                    'known-guitar':'sfizz Meta samples with known note-on/off commands; acoustic sample onset/decay differs from commands'}[case]
    result=analyze_pcm(x,44100,0,ROOT/'basic-pitch'/'nmp.onnx',progress=lambda p:emit({'case':case,'fraction':p}))
    result['input']={'path':str(path) if path else None,'sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path else None,
                     'provenance':provenance,'mono':'arithmetic channel mean, no normalization'}
    save(OUT/(case+'.json'),result)
    summary={'events':len(result['events']),'attacks':len(result['attacks']),
             'rejected_attacks':len(result['rejected_attacks']),
             'orphan_frame_pitches':result['unassigned_active_frame_pitch_count'],
             'pitches':sorted(set(e['pitch'] for e in result['events'])),
             'seconds':time.monotonic()-started}
    if case=='known-guitar':
        truth=json.loads((OUT/'known-events.json').read_text())['notes']
        # Frozen pitch-exact 50ms gate, one-to-one greedy nearest. No offset fit.
        available=list(result['events']); pairs=[]
        for note in truth:
            candidates=[e for e in available if e['pitch']==note['pitch'] and abs(e['start']-note['start'])<=2205]
            if candidates:
                match=min(candidates,key=lambda e:abs(e['start']-note['start']));available.remove(match)
                pairs.append({'id':note['id'],'detected_id':match['id'],
                              'onset_error_samples':match['start']-note['start'],
                              'offset_error_samples':match['end']-note['end']})
        summary.update(known_events=len(truth),matched=len(pairs),false_or_unmatched=len(available),
                       missed=len(truth)-len(pairs),matches=pairs,
                       note='Command-time comparison; no fitted shift; offset is activation decay not physical release')
        # A separate tighter match avoids 50ms hiding almost an entire short gate.
        remaining=list(result['events']); strict=[]
        for note in truth:
            candidates=[e for e in remaining if e['pitch']==note['pitch'] and abs(e['start']-note['start'])<=882]
            if candidates:
                match=min(candidates,key=lambda e:abs(e['start']-note['start']));remaining.remove(match)
                strict.append({'id':note['id'],'detected_id':match['id'],'error_samples':match['start']-note['start']})
        summary['strict_20ms']={'matches':strict,'false_or_unmatched':len(remaining),'missed':len(truth)-len(strict)}
        summary['fragmentation_by_command']=[{'id':n['id'],'same_pitch_event_starts_in_gate':sum(
            e['pitch']==n['pitch'] and n['start']<=e['start']<n['end'] for e in result['events'])}
            for n in truth]
    return summary


def main():
    import shutil
    from audio.render import render
    from audio.managed_process import run_worker
    if shutil.disk_usage(ROOT).free < 8*1024**3+100*1024**2: raise RuntimeError('8GiB reserve')
    OUT.mkdir(exist_ok=False)
    # Repeated pitch, muted short gate, overlap, and attacks around first two seams.
    commands=[('hold',.20,1.20,64,5),('repeat-a',1.50,1.61,62,4),
              ('repeat-b',1.68,2.00,62,4),('overlap',1.72,2.20,67,5),
              ('mute',2.60,2.65,59,4),('seam-before',3.25,3.28,64,5),
              ('seam-after',3.31,3.65,64,5)]
    notes=[dict(id=i,start=round(a*44100),end=round(b*44100),pitch=p,string=s,player=0,velocity=80)
           for i,a,b,p,s in commands]
    arrangement={'sample_rate':44100,'notes':notes}
    save(OUT/'known-events.json',arrangement)
    save(OUT/'known-render.json',render(arrangement,OUT/'known-guitar.wav'))
    results={}
    for case in ('silence','known-guitar','mixture','estimated-other','reference-other'):
        results[case]=run_worker('audio.research.run_basic_pitch',{'case':case},
                                 python=ROOT/'mixed'/'env'/'Scripts'/'python.exe',
                                 wall_seconds=120,memory_bytes=2*1024**3)
        save(OUT/(case+'-summary.json'),results[case])
    save(OUT/'summary.json',results)
    print(json.dumps(results,indent=2))


if __name__=='__main__':main()
