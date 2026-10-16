"""Frozen real-guitar target recovery in one explicitly controlled mixture.

No playback, renderer, model acquisition, threshold search or production change.
The non-guitar components form an artificial interference test, not a new song.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import wave

APP = Path(__file__).resolve().parents[2]
ROOT = APP/'audio/research'
SOURCE = ROOT/'guitarset/case-001'
OUT = ROOT/'guitarset/recovery-001'
RATE, LENGTH = 44100, 264600
CASES = ('pure-guitar','raw-mixture','estimated-other')


def sha(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def save(path, value):
    with path.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,indent=2,allow_nan=False)
        stream.write('\n')


def tree_size(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())


def verified_plan(request):
    if request['plan_sha256'] != sha(OUT/'frozen-plan.json'):
        raise ValueError('Frozen plan changed')
    plan = json.loads((OUT/'frozen-plan.json').read_text())
    for name, digest in plan['inputs'].items():
        if sha(APP/name) != digest: raise ValueError('Frozen input changed: '+name)
    return plan


def references():
    value=json.loads((SOURCE/'00_SS3-98-C_comp.jams').read_text())
    result=[]
    for annotation in value['annotations']:
        if annotation['namespace'] != 'note_midi': continue
        string=annotation['annotation_metadata']['data_source']
        if string not in ('0','1','2','3','4','5'): raise ValueError('Unknown string annotation')
        for index,row in enumerate(annotation['data']):
            t,duration,pitch=row['time'],row['duration'],row['value']
            if (any(type(n) not in (int,float) or not math.isfinite(n) for n in (t,duration,pitch))
                    or t<0 or duration<=0 or not 0<=pitch<=127):
                raise ValueError('Invalid annotation')
            result.append(dict(id=f'string-{string}-note-{index}',string_id=string,
                start=round(t*RATE),end=round((t+duration)*RATE),pitch=pitch,
                time_seconds=t,duration_seconds=duration,confidence=row['confidence']))
    if len(result)!=271: raise ValueError('Unexpected complete annotation count')
    active=[n for n in result if n['start']<LENGTH and n['end']>0]
    if len(active)!=40: raise ValueError('Fixed first-six-second annotation gate changed')
    return result


def run(request,emit):
    plan=verified_plan(request)
    os.environ['OMP_NUM_THREADS']='2'; os.environ['NUMBA_NUM_THREADS']='2'
    import numpy as np
    def read(path,channels,start=0):
        with wave.open(str(path)) as wav:
            if (wav.getframerate(),wav.getsampwidth(),wav.getnchannels())!=(RATE,2,channels):
                raise ValueError('Unexpected source WAV format')
            wav.setpos(start); raw=wav.readframes(LENGTH)
        x=np.frombuffer(raw,dtype='<i2').reshape(-1,channels).astype(np.float32)/32768
        if len(x)!=LENGTH: raise ValueError('Truncated source passage')
        return x
    if request['stage']=='separate':
        from audio.mixed_analysis import TARGETS, WEIGHTS
        import torch, openunmix
        torch.set_num_threads(2); torch.set_num_interop_threads(1)
        guitar=np.repeat(read(SOURCE/'00_SS3-98-C_comp_mic.wav',1),2,axis=1)
        background=sum(read(ROOT/'mixed'/('reference-'+part+'.wav'),2,11025)
                       for part in ('vocals','drums','bass'))
        rms=lambda x:float(np.sqrt(np.mean(x.astype(np.float64)**2)))
        if min(rms(guitar),rms(background))<=1e-8: raise ValueError('Silent control component')
        interference_gain=rms(guitar)/rms(background)
        mixed=guitar+background*interference_gain
        common_gain=10**(-3/20)/max(float(abs(guitar).max()),float(abs(mixed).max()))
        inputs={}
        for label,data in (('pure-guitar',guitar),('raw-mixture',mixed)):
            pcm=np.rint(data.astype(np.float64)*common_gain*32768).astype('<i2')
            if np.max(abs(pcm.astype(np.int32)))>=32767: raise ValueError('Input clipping')
            path=OUT/(label+'.wav')
            with wave.open(str(path),'wb') as wav:
                wav.setparams((2,2,RATE,0,'NONE','not compressed'));wav.writeframes(pcm.tobytes())
            x=pcm.astype(np.float32)/32768
            inputs[label]=dict(sha256=sha(path),frames=LENGTH,channels=2,
                               rms=rms(x),sample_peak=float(abs(x).max()))
        model=openunmix.umxhq(targets=list(TARGETS),pretrained=False,niter=1,
                              wiener_win_len=300,device='cpu')
        for name in TARGETS:
            filename,digest=WEIGHTS[name];path=Path(plan['modeldir'])/filename
            if sha(path)!=digest: raise ValueError('UMX checkpoint changed')
            state=torch.load(path,map_location='cpu',weights_only=True)
            extras=set(state)-set(model.target_models[name].state_dict())
            if extras!={'sample_rate','stft.window','transform.0.window'} or float(state['sample_rate'])!=RATE:
                raise ValueError('Unexpected checkpoint structure')
            for key in extras: del state[key]
            model.target_models[name].load_state_dict(state,strict=True)
        model.eval();emit(dict(phase='separating',progress=.2))
        x=read(OUT/'raw-mixture.wav',2).T.copy()
        began=time.monotonic()
        with torch.inference_mode(): estimates=model(torch.from_numpy(x)[None])[0].numpy()
        if estimates.shape!=(4,2,LENGTH) or not np.isfinite(estimates).all():
            raise ValueError('Separation coordinate or finite check failed')
        other=estimates[list(TARGETS).index('other')].T.copy()
        np.save(OUT/'estimated-other.npy',other,allow_pickle=False)
        inputs['estimated-other']=dict(sha256=sha(OUT/'estimated-other.npy'),frames=LENGTH,
            channels=2,rms=rms(other),sample_peak=float(abs(other).max()),
            samples_outside_unit_range=int(np.sum(abs(other)>1)),separate_gain=1,
            normalized=False,clipped=False)
        evidence=dict(inputs=inputs,interference_rms_gain=interference_gain,
            common_gain=common_gain,prequantization_snr_db=0,
            separation_seconds=time.monotonic()-began,targets=list(TARGETS),
            downmix='arithmetic stereo mean for all three cases',
            guitar_sample_origin=0,background_fixture_origin=11025,
            production_model_settings=dict(niter=1,wiener_win_len=300,cpu=True))
        save(OUT/'input-evidence.json',evidence)
        return dict(input_evidence_sha256=sha(OUT/'input-evidence.json'))
    if request['stage']!='notes' or request.get('case') not in CASES:
        raise ValueError('Unknown fixed stage')
    case=request['case']
    sys.path.insert(0,str(ROOT/'basic-pitch/runtime'))
    from audio.basic_pitch import analyze_pcm
    from audio.basic_pitch_upstream import decode_saved
    from audio.reference_notes import score_events
    evidence=json.loads((OUT/'input-evidence.json').read_text())
    path=OUT/(case+('.npy' if case=='estimated-other' else '.wav'))
    if sha(path)!=evidence['inputs'][case]['sha256']: raise ValueError('Prepared input changed')
    stereo=np.load(path,allow_pickle=False) if case=='estimated-other' else read(path,2)
    if stereo.shape!=(LENGTH,2): raise ValueError('Input geometry changed')
    raw=analyze_pcm(stereo.mean(axis=1),RATE,0,ROOT/'basic-pitch/nmp.onnx',
                    progress=lambda p:emit(dict(case=case,progress=p)))
    save(OUT/(case+'-raw.json'),raw)
    refs=json.loads((OUT/'reference-notes.json').read_text())
    result={}
    for policy in plan['policies']:
        decoded=raw if policy=='custom_onset_only' else decode_saved(raw,'published_default')
        events=decoded['events']
        score=score_events(events,refs,start_sample=0,end_sample=LENGTH,sample_rate=RATE)
        save(OUT/(case+'-'+policy+'.json'),dict(events=events,score=score,
            settings=decoded.get('settings',decoded.get('policy')),raw_report=case+'-raw.json'))
        result[policy]=dict(events=len(events),matched20=score['onset_20ms']['matched'],
            matched50=score['onset_50ms']['matched'],full_notes=score['full_note_50ms']['matched'],
            full_reference_count=score['full_note_50ms']['reference_count'],
            onset_reference_count=score['onset_50ms']['reference_count'],
            unmatched_to_guitar_reference=score['onset_50ms']['unmatched_to_reference'],
            occupancy=score['occupancy']['totals_pitch_samples'],
            right_censored_reference_ids=score['right_censored_reference_ids'])
    return dict(summary=result,source_coverage=raw['source_coverage'],raw_sha256=sha(OUT/(case+'-raw.json')))


def main():
    from audio.analysis_runtime import require_runtime, MANIFEST_PATH
    from audio.managed_process import run_worker
    from audio.mixed_analysis import WEIGHTS
    allowance=64*1024**2
    used=tree_size(ROOT)+tree_size(APP/'audio/vendor/analysis');free=shutil.disk_usage(ROOT).free
    if used+allowance>4*1024**3 or free<8*1024**3+allowance: raise ValueError('Research budget/reserve')
    runtime=require_runtime()
    paths=[Path(__file__),APP/'audio/basic_pitch.py',APP/'audio/basic_pitch_upstream.py',
           APP/'audio/reference_notes.py',APP/'audio/mixed_analysis.py',APP/'audio/managed_process.py',
           MANIFEST_PATH,ROOT/'basic-pitch/nmp.onnx',ROOT/'basic-pitch/runtime-inventory.json',
           SOURCE/'manifest.json',SOURCE/'frozen-plan.json',SOURCE/'00_SS3-98-C_comp_mic.wav',
           SOURCE/'00_SS3-98-C_comp.jams']
    paths += [ROOT/'mixed'/('reference-'+part+'.wav') for part in ('vocals','drums','bass')]
    paths += [Path(runtime['modeldir'])/filename for filename,_ in WEIGHTS.values()]
    for item in json.loads((ROOT/'basic-pitch/runtime-inventory.json').read_text()):
        path=ROOT/'basic-pitch/runtime'/item['path']
        if path.stat().st_size!=item['bytes'] or sha(path)!=item['sha256']:
            raise ValueError('Pinned note runtime changed')
        paths.append(path)
    refs=references() # Annotation-only selection, before predictions.
    OUT.mkdir(exist_ok=False);save(OUT/'reference-notes.json',refs)
    paths.append(OUT/'reference-notes.json')
    plan=dict(inputs={str(p.relative_to(APP)):sha(p) for p in paths},
        frozen_before_inference=True,clip_start=0,clip_frames=LENGTH,sample_rate=RATE,
        annotation_gate=dict(complete_notes=271,selected_notes=40),cases=list(CASES),
        policies=['published_default','custom_onset_only'],primary_policy='published_default',
        primary_scope='Pinned published-default event functions with retained source-anchor timing, not complete upstream timing/MIDI equivalence',
        modeldir=runtime['modeldir'],runtime_version=runtime['runtime_version'],
        mixture='Three existing MusicDelta reference stems at11025..275625, equal RMS to guitar; fixed common joint -3dBFS gain',
        censoring='Right boundary notes retain onset/occupancy, excluded only from offset/full-note counts; report uncovered tail',
        score='Fractional pitch <=0.5semitone, onset20/50ms, full-note offset max50ms/20percent, no shift fit',
        useful_recovery_rule='Primary estimated-other vs raw-mixture: >=1 additional50ms guitar match, less missing pitch occupancy, no increased outside-annotation occupancy; otherwise no clean benefit/tradeoff',
        resource=dict(additional_bytes=allowance,research_bytes_before=used,free_bytes_before=free,
                      per_worker_seconds=120,sampled_worker_memory_bytes=2*1024**3),
        limits=['One development performance; possible training overlap',
                'Automatic string annotations have null confidence; note offsets not physical damping truth',
                'Cross-string unison multiplicity is ambiguous to pitch-only outputs',
                'Controlled mix is not a natural performance; interference key/tempo may differ',
                'No internal global rests>=50ms; no general rest acceptance',
                'Unmatched-to-guitar mixture events are not necessarily false musical notes',
                'Isolated mic is a recognition control, not a guaranteed score ceiling',
                'No production readiness or musical acceptance follows'])
    save(OUT/'frozen-plan.json',plan)
    for path in paths[:6]: (OUT/('frozen-'+path.name)).write_bytes(path.read_bytes())
    request=dict(plan_sha256=sha(OUT/'frozen-plan.json'))
    results={};began=time.monotonic()
    try:
        for stage,case in [('separate',None)]+[('notes',case) for case in CASES]:
            label=case or stage
            managed=run_worker('audio.research.run_guitarset_recovery',{**request,'stage':stage,'case':case},
                python=runtime['python'],wall_seconds=120,memory_bytes=2*1024**3,
                progress=lambda event:print(json.dumps(event),flush=True))
            save(OUT/(label+'-run.json'),managed);results[label]=managed
            if tree_size(OUT)>allowance: raise ValueError('Output budget exceeded; no successor started')
        primary=plan['primary_policy'];before=results['raw-mixture']['result']['summary'][primary]
        after=results['estimated-other']['result']['summary'][primary]
        decision=(after['matched50']>=before['matched50']+1
            and after['occupancy']['missing_samples']<before['occupancy']['missing_samples']
            and after['occupancy']['outside_annotation_samples']<=before['occupancy']['outside_annotation_samples'])
        assert all(sha(APP/p)==h for p,h in plan['inputs'].items())
        save(OUT/'summary.json',dict(results=results,primary_useful_recovery_rule_passed=decision,
            primary_policy=primary,elapsed_seconds=time.monotonic()-began,
            output_bytes_before_summary=tree_size(OUT),no_playback=True,no_production_change=True,
            limitations=plan['limits']))
        print(json.dumps(dict(complete=True,primary_rule_passed=decision,
            cases={c:results[c]['result']['summary'] for c in CASES})),flush=True)
    except BaseException as error:
        save(OUT/'failure.json',dict(error=repr(error),evidence_preserved=True));raise


if __name__=='__main__':main()
