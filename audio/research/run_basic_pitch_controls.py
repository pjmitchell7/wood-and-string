"""Silent frozen eight-control probe. No threshold search or source-song input."""
import hashlib
import json
import math
from pathlib import Path
import sys
import wave

ROOT=Path(__file__).resolve().parent
SPEC=ROOT/'basic_pitch_controls_v2.json'
SHA='52f730f4a9a8c7f8e9a19b7f86f1829cca5685e481377564a1321a79e7f99cb0'
OUT=ROOT/'basic-pitch'/'comparison-003'


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,allow_nan=False)


def specification():
    data=SPEC.read_bytes()
    if hashlib.sha256(data).hexdigest()!=SHA:raise ValueError('Frozen control hash mismatch')
    return json.loads(data)


def pcm(path):
    import numpy as np
    with wave.open(str(path)) as w:
        if (w.getframerate(),w.getnchannels(),w.getsampwidth())!=(44100,2,2):raise ValueError('PCM format')
        return np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').reshape(-1,2).copy()


def matching(events,notes,tolerance):
    """Exact-pitch max cardinality, then min summed absolute onset error."""
    import numpy as np
    from scipy.optimize import linear_sum_assignment
    n=len(notes);m=len(events);penalty=(n+1)*(tolerance+1)
    costs=np.full((n,m+n),penalty,dtype=np.int64)
    costs[:,:m]=2*penalty
    for i,note in enumerate(notes):
        for j,event in enumerate(events):
            error=abs(note['start']-event['start'])
            if note['pitch']==event['pitch'] and error<=tolerance:costs[i,j]=error
    rows,cols=linear_sum_assignment(costs)
    pairs=[];used=set()
    for i,j in zip(rows,cols):
        if j<m and costs[i,j]<penalty:
            a,b=notes[i],events[j];used.add(int(j))
            pairs.append(dict(command=a['id'],event=b['id'],pitch=a['pitch'],
                onset_error_samples=b['start']-a['start'],offset_error_samples=b['end']-a['end'],
                command_duration=a['end']-a['start'],event_duration=b['end']-b['start']))
    extras=[e for j,e in enumerate(events) if j not in used]
    octave=[e['id'] for e in extras if any(abs(e['pitch']-n['pitch']) in (12,24) and
            n['start']-tolerance<=e['start']<n['end'] for n in notes)]
    return dict(matched=len(pairs),missed=n-len(pairs),false_or_unmatched=m-len(pairs),
                pairs=pairs,extra_event_ids=[e['id'] for e in extras],extra_octave_candidate_ids=octave)


def union_length(intervals):
    end=None;total=0
    for a,b in sorted(intervals):
        if end is None or a>end:total+=b-a;end=b
        elif b>end:total+=b-end;end=b
    return total


def structure(events,notes):
    """Descriptive gate-relative diagnostics; tails are not verified wrong notes."""
    splits=[];merges=[];rests=[]
    for n in notes:
        ids=[e['id'] for e in events if e['pitch']==n['pitch'] and n['start']<=e['start']<n['end']]
        splits.append(dict(command=n['id'],starts_in_gate=ids,extra_start_count=max(0,len(ids)-1)))
    for e in events:
        ids=[n['id'] for n in notes if n['pitch']==e['pitch'] and e['start']<n['end'] and e['end']>n['start']]
        if len(ids)>1:merges.append(dict(event=e['id'],overlapped_commands=ids))
    for pitch in sorted({n['pitch'] for n in notes}):
        seq=sorted([n for n in notes if n['pitch']==pitch],key=lambda n:n['start'])
        for left,right in zip(seq,seq[1:]):
            a,b=left['end'],right['start']
            if b<=a:continue
            intervals=[(max(a,e['start']),min(b,e['end'])) for e in events if e['pitch']==pitch and e['start']<b and e['end']>a]
            rests.append(dict(after=left['id'],before=right['id'],gap_samples=b-a,
                              inferred_union_in_gap=union_length(intervals)))
    return dict(gate_start_counts=splits,merge_candidates=merges,commanded_rest_intrusion=rests,
                caveat='Overlap with commanded rest may include acoustic release tail; not automatically a musical error')


def run(request,emit):
    sys.path.insert(0,str(ROOT/'basic-pitch'/'runtime'))
    import numpy as np
    from audio.basic_pitch import analyze_pcm
    from audio.basic_pitch_upstream import decode_saved
    spec=specification();case=next(c for c in spec['cases'] if c['id']==request['case'])
    stereo=np.load(OUT/(case['id']+'-shared.npy'),allow_pickle=False)
    if stereo.shape!=(132300,2):raise ValueError('Exact three seconds required')
    origin=spec['source_origin_sample']
    report=analyze_pcm(stereo.mean(axis=1),44100,origin,ROOT/'basic-pitch'/'nmp.onnx',
                       progress=lambda v:emit({'case':case['id'],'fraction':v}))
    report['frozen_control_sha256']=SHA
    report['input_sha256']=hashlib.sha256((OUT/(case['id']+'-shared.npy')).read_bytes()).hexdigest()
    save(OUT/(case['id']+'-raw.json'),report)
    notes=[dict(n,start=n['start']+origin,end=n['end']+origin) for n in case['notes']]
    summary={}
    for policy in ('custom_onset_only','published_default','duration_matched'):
        result=report if policy=='custom_onset_only' else decode_saved(report,policy)
        events=result['events']
        for e in events:
            if not origin<=e['start']<e['end']<=origin+132300:raise AssertionError('Source origin/gate violation')
            e['local_start']=e['start']-origin;e['local_end']=e['end']-origin
        value=dict(events=len(events),unassigned_active_frame_pitches=result['unassigned_active_frame_pitch_count'],
                   strict20ms=matching(events,notes,882),within50ms=matching(events,notes,2205),
                   structure=structure(events,notes),source_origin_sample=origin,
                   nonfinite_augmented_onset_cells=result.get('nonfinite_augmented_onset_cells',0))
        # Keep complete event evidence, local/absolute mappings and raw matrix pointer.
        save(OUT/(case['id']+'-'+policy+'.json'),dict(summary=value,events=events,
            raw_report=case['id']+'-raw.json',settings=result.get('settings',result.get('policy'))))
        summary[policy]=value
    return summary


def main():
    import shutil
    import numpy as np
    from audio.render import render
    from audio.managed_process import run_worker
    spec=specification()
    if shutil.disk_usage(ROOT).free<8*1024**3+200*1024**2:raise RuntimeError('Disk reserve')
    OUT.mkdir(exist_ok=False)
    save(OUT/'frozen-controls.json',spec)
    save(OUT/'frozen-plan.json',dict(sha256=SHA,policies=['custom_onset_only','published_default','duration_matched'],
        thresholds='unchanged .5 onset/.3 frame; upstreamdefault11frame minimum vs durationmatched0',
        energy_window='first4410samples (100ms) after each command, stereo and mono RMS; before next attack',
        matching='exactpitch maximumcardinality then minimumsummed onseterror, no offsetfit',
        inference_limits=dict(wall_seconds=120,sampled_memory_bytes=2*1024**3)))
    raws={};renders={};peak=0
    for case in spec['cases']:
        path=OUT/(case['id']+'-render-fixed.wav')
        renders[case['id']]=render(dict(sample_rate=44100,notes=case['notes']),path,level_policy='legacy_fixed')
        data=pcm(path)
        if len(data)>132300:raise ValueError('Tail exceeds frozen duration; no trim permitted')
        data=np.pad(data,((0,132300-len(data)),(0,0)))
        mask=case.get('post_render_transform')
        if mask:
            i=np.arange(132300);gain=np.ones(132300)
            down=(i>=30032)&(i<30473);up=(i>=32237)&(i<32678)
            gain[down]=.5*(1+np.cos(np.pi*(i[down]-30032)/441))
            gain[(i>=30473)&(i<32237)]=0
            gain[up]=.5*(1-np.cos(np.pi*(i[up]-32237)/441))
            data=np.rint(data*gain[:,None]).astype('<i2')
        with wave.open(str(OUT/(case['id']+'-padded-masked-fixed.wav')),'wb') as w:
            w.setparams((2,2,44100,0,'NONE','not compressed'));w.writeframes(data.astype('<i2').tobytes())
        raws[case['id']]=data;peak=max(peak,int(np.abs(data.astype(np.int32)).max()))
    if peak==0:raise ValueError('All controls silent')
    shared=10**(-3/20)/(peak/32768);energy={}
    for case in spec['cases']:
        data=raws[case['id']].astype(np.float32)/32768*shared
        np.save(OUT/(case['id']+'-shared.npy'),data,allow_pickle=False)
        energy[case['id']]=[dict(command=n['id'],velocity=n['velocity'],
            stereo_rms=float(np.sqrt(np.mean(data[n['start']:n['start']+4410].astype(np.float64)**2))),
            mono_rms=float(np.sqrt(np.mean(data[n['start']:n['start']+4410].mean(axis=1).astype(np.float64)**2))))
            for n in case['notes']]
    save(OUT/'render-gain-energy.json',dict(render=renders,shared_gain=shared,raw_pcm_peak=peak,
        target_sample_peak_dbfs=-3,analysis_copy='float32 shared gain after fixedPCM16; not original-quality master',energy=energy))
    results={}
    for case in spec['cases']:
        result=run_worker('audio.research.run_basic_pitch_controls',dict(case=case['id']),
            python=ROOT/'mixed'/'env'/'Scripts'/'python.exe',wall_seconds=120,memory_bytes=2*1024**3)
        save(OUT/(case['id']+'-summary.json'),result);results[case['id']]=result
    save(OUT/'summary.json',results)
    print(json.dumps({k:{p:dict(events=v['events'],matched20=v['strict20ms']['matched'],matched50=v['within50ms']['matched'])
        for p,v in r['result'].items()} for k,r in results.items()},indent=2))


if __name__=='__main__':main()
