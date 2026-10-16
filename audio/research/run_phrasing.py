"""Silent bounded phrasing probe; existing UMX estimates only, no separation."""
import os
os.environ['NUMBA_NUM_THREADS'] = '2'
os.environ['OMP_NUM_THREADS'] = '2'
import array
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import time
import wave

from audio.phrasing import energy_evidence, segment
from audio.performance import Performance, arrange
from audio.render import render
from audio.research.run_mixed_arrangement import extract_frames, segment as baseline_segment

ROOT = Path(__file__).parent
OUT = ROOT/'phrasing-001'
ORIGIN = 11025
END = 275625


def save(name, value):
    (OUT/name).write_text(json.dumps(value, indent=2, allow_nan=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--render-diagnostic', action='store_true',
                        help='Explicitly create unaccepted diagnostic renders after reviewing the hypothesis')
    args = parser.parse_args()
    if OUT.exists(): raise FileExistsError('Preserve prior evidence; choose a new run directory')
    if shutil.disk_usage(ROOT).free < 8*1024**3+64*1024**2: raise RuntimeError('Disk reserve')
    OUT.mkdir()
    start = time.monotonic(); notes = []; summaries = {}
    for role, target in [('melody', 'vocals'), ('bass', 'bass')]:
        source = ROOT/'umx'/f'estimated-{target}.wav'
        with wave.open(str(source)) as wav:
            assert (wav.getframerate(),wav.getnchannels(),wav.getsampwidth()) == (44100,2,2)
            wav.setpos(ORIGIN); raw = wav.readframes(END-ORIGIN)
        selected = OUT/f'{role}-selected-estimate.wav'
        with wave.open(str(selected), 'wb') as wav:
            wav.setparams((2,2,44100,0,'NONE','not compressed'));wav.writeframes(raw)
        report = extract_frames(selected, role)
        # Keep original local-frame report before adding source offsets/evidence.
        save(f'{role}-raw-pyin.json', report)
        old_notes, discarded = baseline_segment(report)
        data = array.array('h'); data.frombytes(raw)
        mono = [(data[i]+data[i+1])/65536 for i in range(0,len(data),2)]
        report['frames'] = energy_evidence(mono, report['frames'], report['hop_samples'])
        for row in report['frames']: row['sample'] += ORIGIN
        report['input']['frames'] = END
        report['analysis_offset_samples'] = ORIGIN
        new_notes, audit = segment(report)
        # Record overlap against all baseline accepted/discarded groups, not a
        # misleading count-only claim that all reductions are legitimate merges.
        baseline = [dict(asdict(n), discarded=False) for n in old_notes] + [dict(n,discarded=True) for n in discarded]
        for event in audit['events']:
            overlaps = [b for b in baseline if b['start']+ORIGIN < event['end'] and b['end']+ORIGIN > event['start']]
            event['baseline_groups'] = [{'id':b['id'], 'discarded':b['discarded'],
                'source_start':b['start']+ORIGIN, 'source_end':b['end']+ORIGIN,
                'start_delta':event['start']-(b['start']+ORIGIN),
                'end_delta':event['end']-(b['end']+ORIGIN)} for b in overlaps]
        save(f'{role}-evidence-frames.json', report);save(f'{role}-event-audit.json',audit)
        save(f'{role}-baseline-groups.json',baseline)
        notes += new_notes
        summaries[role] = {'estimated_source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'baseline_notes':len(old_notes), 'baseline_discarded_groups':len(discarded),
            'candidate_notes':len(new_notes), 'candidate_under_50ms':sum(n.end-n.start<2205 for n in new_notes),
            'velocity_range':[min(n.velocity for n in new_notes),max(n.velocity for n in new_notes)],
            'reattacks':sum(d['kind']=='reattack' for d in audit['decisions']),
            'retained_pitch_excursion_frames':sum(d['kind']=='pitch_excursion_retained_in_event' for d in audit['decisions'])}
    performance = Performance('umx-existing-estimates-phrasing-research',44100,notes,analysis_offset_samples=ORIGIN)
    performance.validate();save('performance.json',asdict(performance))
    renders = {}
    for count,label in [(1,'solo'),(2,'duet')]:
        arrangement = arrange(performance,count);arrangement['render_origin_sample']=ORIGIN
        save(f'{label}-arrangement.json',arrangement)
        if args.render_diagnostic:
            renders[label] = render(arrangement,OUT/f'{label}.wav')
    save('evidence.json', {'selection': {'source_start':ORIGIN,'source_end':END,'rate':44100},
        'comparison_limit':'Same selection as app003; different separation context (whole fixture) and PCM16 stored estimates. Not a controlled production-segmentation-only A/B.',
        'source_origin':'fixture-local; original MedleyDB origin unverified',
        'reference_audio_used':False,'playback':False,'quality_status':'User baseline FAILED; candidate not listened to or accepted',
        'roles':summaries,'renders':renders,'elapsed_seconds':time.monotonic()-start})
    print(json.dumps(summaries,indent=2))


if __name__ == '__main__': main()
