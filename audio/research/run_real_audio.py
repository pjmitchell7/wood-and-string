"""Private noncommercial research fixture; no source audio in rendered output.
Run from app root: .venv/Scripts/python.exe -m audio.research.run_real_audio
Acquisition and licenses recorded in docs/REAL-AUDIO-001.md; fixture is ignored.
"""
import bisect
import csv
from dataclasses import asdict,replace
import json
import math
from pathlib import Path
import statistics
import time
import wave
from audio.extract import extract_wav
from audio.performance import arrange
from audio.render import render

ROOT=Path(__file__).parent

def score(report,reference,shift=19.):
    times=[r[0] for r in reference];pairs=[]
    for f in report['frames']:
        t=f['sample']/44100+shift;i=bisect.bisect_left(times,t)
        choices=[j for j in (i-1,i) if 0<=j<len(times)]
        j=min(choices,key=lambda j:abs(times[j]-t));hz=reference[j][1]
        pitch=69+12*math.log2(hz/440) if hz>0 else None
        pairs.append((pitch,f['pitch']))
    rv=[(r,p) for r,p in pairs if r is not None];both=[(r,p) for r,p in rv if p is not None]
    errors=[abs(r-p)*100 for r,p in both]
    return {'reference_voiced_frames':len(rv),'both_voiced_frames':len(both),
      'voiced_recall':len(both)/len(rv),
      'raw_pitch_accuracy_50c_all_reference_voiced':sum(e<=50 for e in errors)/len(rv),
      'median_absolute_cents_both_voiced':statistics.median(errors),
      'octave_sized_errors_over_600c':sum(e>600 for e in errors),
      'false_voiced_frames':sum(r is None and p is not None for r,p in pairs),
      'reference_unvoiced_frames':sum(r is None for r,p in pairs)}

def main():
    started=time.perf_counter()
    perf,report=extract_wav(ROOT/'reference-vocal-mono.wav','MusicDelta_80sRock-reference-vocal-excerpt')
    extract_time=time.perf_counter()-started
    reference=[tuple(map(float,row)) for row in csv.reader((ROOT/'reference.csv').open())]
    metrics=score(report,reference)
    origin=19*44100
    perf.notes=[replace(n,start=n.start+origin,end=n.end+origin,contour=tuple((s+origin,p) for s,p in n.contour)) for n in perf.notes]
    arrangement=arrange(perf,1);arrangement['render_origin_sample']=origin
    (ROOT/'extracted-performance.json').write_text(json.dumps(asdict(perf),indent=2))
    (ROOT/'extraction-report.json').write_text(json.dumps(report,indent=2))
    (ROOT/'arrangement.json').write_text(json.dumps(arrangement,indent=2))
    output=ROOT/'real-vocal-extracted-guitar.wav'
    if output.exists():raise FileExistsError('Keep prior experiment output; choose a new result path to repeat render')
    started=time.perf_counter();render_metrics=render(arrangement,output)
    with wave.open(str(ROOT/'reference-vocal-mono.wav')) as wav:frames=wav.getnframes()
    evidence={'fixture':'Music Delta - 80s Rock, MUSDB18 train test fixture','reference_origin_seconds':19,'alignment_status':'cutlist-derived; original-waveform correspondence not independently confirmed','source_decoded_frames':frames,'source_decoded_seconds':frames/44100,'aac_edit_list_media_time':2048,'aac_declared_duration_frames':299880,'extract_seconds':extract_time,'extracted_notes':len(perf.notes),'frame_metrics_provisional':metrics,'offset_sensitivity_not_fitted':{str(s):score(report,reference,19+s) for s in [-.05,0,.05]},'render_seconds':time.perf_counter()-started,'render':render_metrics,'adaptations':arrangement['adaptations'],'limitations':['Provided reference vocal, no source separation performed','Train fixture, not held-out generalization','Cutlist provenance supports19s; exact fixture trimming history unknown','No listening acceptance','Quantized plucks omit vocal pitch contours/dynamics and accompaniment']}
    (ROOT/'evidence.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(evidence,indent=2))

if __name__=='__main__':main()
