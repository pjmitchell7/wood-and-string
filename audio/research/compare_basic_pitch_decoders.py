"""Frozen matrix-only comparison; no ONNX import, audio read, render or inference."""
import hashlib
import json
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parent/'basic-pitch'
SOURCE=ROOT/'comparison-001'
OUT=ROOT/'comparison-002'


def save(path,obj):
    with path.open('x',encoding='utf-8') as f:json.dump(obj,f,indent=2,allow_nan=False)


def score(events,truth,tolerance):
    available=list(events);matched=[]
    for note in truth:
        candidates=[e for e in available if e['pitch']==note['pitch'] and abs(e['start']-note['start'])<=tolerance]
        if candidates:
            e=min(candidates,key=lambda e:abs(e['start']-note['start']));available.remove(e)
            matched.append(dict(id=note['id'],event=e['id'],onset_error=e['start']-note['start'],offset_error=e['end']-note['end']))
    return dict(matches=matched,matched=len(matched),missed=len(truth)-len(matched),extra=len(available))


def run(request,emit):
    from audio.basic_pitch_upstream import decode_saved,POLICIES
    began=time.monotonic();summaries={}
    truth=json.loads((SOURCE/'known-events.json').read_text())['notes']
    for case in ('silence','known-guitar','mixture','estimated-other','reference-other'):
        source=SOURCE/(case+'.json');before=hashlib.sha256(source.read_bytes()).hexdigest()
        raw=json.loads(source.read_text());results={}
        for policy in POLICIES:
            result=decode_saved(raw,policy)
            result['retained_input']={'path':str(source),'sha256':before,
                'source_origin_sample':raw['source_origin_sample'],'source_rate':raw['source_rate'],
                'source_length':raw['source_length'],'raw_matrices':'unchanged in retained_input file; contour unchanged',
                'timebase':'same exact window-derived source anchors as comparison001; official times separate'}
            save(OUT/(case+'-'+policy+'.json'),result)
            item=dict(events=len(result['events']),orphan_frame_pitches=result['unassigned_active_frame_pitch_count'],
                      starts_with_head_peak=sum(e['onset_head_peak_at_start'] for e in result['events']),
                      nonfinite_augmented_onset_cells=result['nonfinite_augmented_onset_cells'],warnings=result['warnings'])
            if case=='known-guitar':
                item.update(strict20ms=score(result['events'],truth,882),within50ms=score(result['events'],truth,2205))
            results[policy]=item
        assert hashlib.sha256(source.read_bytes()).hexdigest()==before
        summaries[case]=results;emit({'case':case,'done':True})
    save(OUT/'summary.json',summaries)
    return dict(seconds=time.monotonic()-began,summary=str(OUT/'summary.json'))


def main():
    from audio.basic_pitch_upstream import POLICIES
    from audio.managed_process import run_worker
    OUT.mkdir(exist_ok=False)
    # Written before loading any case outcomes.
    save(OUT/'frozen-plan.json',dict(policies=POLICIES,onset_threshold=.5,frame_threshold=.3,
        minimum_ms=127.7,default_min_frames=11,strict_length_comparison='end-start > min_frames',
        cases=['silence','known-guitar','mixture','estimated-other','reference-other'],
        no_model_inference=True,no_global_offset_fit=True,matching_samples=[882,2205],
        primary_timing='comparison001 window-derived source anchors',
        copied_source_revision='fa5997af0a8210982619003269994a1be25eddf3'))
    metrics=run_worker('audio.research.compare_basic_pitch_decoders',{},
        python=ROOT.parent/'pyin-env'/'Scripts'/'python.exe',wall_seconds=120,memory_bytes=1024**3)
    save(OUT/'run.json',metrics)
    print(json.dumps(metrics,indent=2))


if __name__=='__main__':main()
