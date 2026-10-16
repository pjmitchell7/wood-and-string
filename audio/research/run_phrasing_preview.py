"""One real production-context separation/pitch pass, two explicit event policies.

Silent private research: identical retained stems/frames, fixed velocity80,
unchanged planner and renderer. No candidate is selected as the app default.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import wave

ROOT = Path(__file__).resolve().parent


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(request, emit):
    """Managed child entry. Instrument the real production function locally."""
    from audio import mixed_analysis as mixed
    out = Path(request.pop('research_output'))
    original_extract = mixed.extract
    def compare(stem, role, origin):
        import numpy as np
        rows, report = mixed.pitch_frames(stem, role)
        np.save(out/(role+'-estimated-stem.npy'), stem, allow_pickle=False)
        save(out/(role+'-pitch-frames.json'), rows)
        baseline, baseline_events = mixed.interpret_frames(rows, stem, role, origin)
        candidate, candidate_events = mixed.interpret_frames(
            rows, stem, role, origin, interpretation='attack-aware-v1')
        save(out/(role+'-comparison.json'), {
            'source_origin': origin, 'source_frames': stem.shape[1],
            'stem_sha256': sha(out/(role+'-estimated-stem.npy')),
            'pitch_rows_sha256': sha(out/(role+'-pitch-frames.json')),
            'pitch_report': report,
            'baseline': {'notes': [asdict(n) for n in baseline], 'events': baseline_events},
            'candidate': {'notes': [asdict(n) for n in candidate], 'events': candidate_events}})
        return baseline, {**report, **baseline_events}
    mixed.extract = compare
    try:
        return mixed.run(request, emit)
    finally:
        mixed.extract = original_extract


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='phrasing-preview-001')
    args = parser.parse_args()
    if not re.fullmatch(r'phrasing-preview-[0-9]{3}', args.name):
        raise ValueError('Use a fresh phrasing-preview-NNN directory')
    budget = 40*1024**2
    used = sum(p.stat().st_size for tree in (ROOT, ROOT.parent/'vendor/analysis')
               for p in tree.rglob('*') if p.is_file())
    if used+budget > 4*1024**3 or shutil.disk_usage(ROOT).free < 8*1024**3+budget:
        raise RuntimeError('Research budget/reserve exceeded')
    out = ROOT/args.name
    out.mkdir(exist_ok=False)
    from audio.analysis_runtime import require_runtime
    from audio.managed_process import run_worker
    from audio.performance import Note, Performance, arrange
    from audio.render import render
    from audio.mixed_analysis import read_passage
    from worker import load_analysis
    source = ROOT/'mixed/mixed.wav'
    source_hash = sha(source)
    source_id = 'MusicDelta80sRock-private-training-excerpt'
    start, end, origin = .25, 6.25, 11025
    save(out/'frozen-plan.json', {
        'source_sha256': source_hash, 'start_seconds': start, 'end_seconds': end,
        'policies': ['baseline', 'attack-aware-v1'], 'fixed_velocity': 80,
        'source_context': 'Exact six-second input used by production mixed preview',
        'same_stems_and_pitch_rows': True, 'planner': 'greedy',
        'render_level_policy': 'peak; independent sample-peak scaling, not loudness matched',
        'acceptance': 'Operational comparison only; no real-song note truth or listening acceptance'})
    runtime = require_runtime()
    request = {'source_path': str(source.resolve()), 'source_sha256': source_hash,
               'source_id': source_id, 'start_seconds': start, 'end_seconds': end,
               'modeldir': runtime['modeldir'], 'result_path': str((out/'baseline.analysis.json').resolve()),
               'research_output': str(out.resolve())}
    began = time.monotonic()
    response = run_worker('audio.research.run_phrasing_preview', request,
        python=runtime['python'], wall_seconds=180, memory_bytes=2*1024**3,
        progress=lambda event: print(json.dumps(event), flush=True))
    baseline, report, checked_origin = load_analysis(
        out/'baseline.analysis.json', response['result'], source_id, start, end)
    assert checked_origin == origin
    candidate_notes = []
    summaries = {}
    for role in ('melody', 'bass'):
        value = json.loads((out/(role+'-comparison.json')).read_text())
        candidate_notes.extend(Note(**n) for n in value['candidate']['notes'])
        events = value['candidate']['events']['phrasing_audit']['events']
        summaries[role] = {
            'baseline_notes': len(value['baseline']['notes']),
            'baseline_discarded': len(value['baseline']['events']['discarded_short_groups']),
            'candidate_notes': len(value['candidate']['notes']),
            'associated_early_attacks': sum(e['attack_association'] is not None for e in events),
            'pitch_unobserved_intervals': [e['attack_association']['pitch_unobserved_interval']
                                         for e in events if e['attack_association']],
            'resume_ambiguities': sum('resume_uncertainty' in e for e in events)}
    candidate = Performance(source_id, 44100, candidate_notes)
    candidate.validate()
    save(out/'candidate-performance.json', asdict(candidate))
    evidence = {'source_sha256': source_hash, 'origin': origin, 'analysis': report,
                'analysis_process': response['metrics'], 'roles': summaries, 'renders': {},
                'audio_played': False, 'musical_acceptance': 'unverified candidate; existing user baseline failed'}
    for policy, performance in [('baseline', baseline), ('attack-aware-v1', candidate)]:
        for performers in (1, 2):
            label = policy+'-'+str(performers)
            arrangement = arrange(performance, performers)
            arrangement['render_origin_sample'] = origin
            save(out/(label+'-arrangement.json'), arrangement)
            if policy == 'baseline' and performers == 1:
                prior = json.loads((ROOT/'compressed-preview-001/evidence.json').read_text())
                expected = prior['cases'][0]['job']['result']['arrangement']['notes']
                assert arrangement['notes'] == expected, 'Baseline changed from actual WAV integration'
                evidence['prior_production_baseline_notes_identical'] = True
            metrics = render(arrangement, out/(label+'.wav'))
            evidence['renders'][label] = {'metrics': metrics, 'sha256': sha(out/(label+'.wav')),
                'notes': len(arrangement['notes']), 'adaptations': arrangement['adaptations']}
    raw, info = read_passage(source, source_hash, start, end)
    with wave.open(str(out/'original-reference.wav'), 'wb') as wav:
        wav.setparams((info['source_channels'], 2, 44100, 0, 'NONE', 'not compressed'))
        wav.writeframes(raw)
    evidence['original_reference_sha256'] = sha(out/'original-reference.wav')
    assert sha(source) == source_hash
    evidence['elapsed_seconds'] = time.monotonic()-began
    save(out/'evidence.json', evidence)
    print(json.dumps({'complete': True, 'out': str(out), 'roles': summaries,
                      'analysis_process': response['metrics']}), flush=True)


if __name__ == '__main__':
    main()
