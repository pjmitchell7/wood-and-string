"""Silent research-only separated-mixture melody+bass arrangement.

Run from app root with the existing pyin-env interpreter. This script never
separates audio, downloads, plays, or changes production behavior. Reference
stems are optional comparators, read only AFTER estimate-only renders exist.
All sample positions belong to the decoded fixture, whose local origin is 0.
"""
import os
os.environ['NUMBA_NUM_THREADS'] = '2'
os.environ['OMP_NUM_THREADS'] = '2'
import argparse
from collections import Counter
import ctypes
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import shutil
import time
import wave

import librosa
import numpy as np
from scipy.signal import resample_poly
from audio.performance import Note, Performance, arrange, OPEN, sampled_pitches
from audio.render import render

RESEARCH = Path(__file__).resolve().parent
SOURCE_RATE = 44100
ANALYSIS_RATE = 22050
HOP = 220
MIN_GROUP_FRAMES = 3
PROBABILITY_FLOOR = 0.10
SETTINGS = {
    'melody': {'fmin': 70.0, 'fmax': 1200.0, 'frame_length': 1024},
    'bass': {'fmin': 32.70319566257483, 'fmax': 400.0, 'frame_length': 2048},
}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def dump(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def read_audio(path):
    with wave.open(str(path), 'rb') as wav:
        frames = wav.getnframes()
        channels = wav.getnchannels()
        if (wav.getframerate() != SOURCE_RATE or wav.getsampwidth() != 2
                or wav.getcomptype() != 'NONE' or channels not in (1, 2)
                or not 0 < frames <= 30 * SOURCE_RATE):
            raise ValueError('Use a bounded 44100 Hz mono/stereo PCM16 research WAV')
        x = np.frombuffer(wav.readframes(frames), dtype='<i2').astype(np.float64)
    x = x.reshape(frames, channels).mean(axis=1) / 32768.0
    return x, {'path': str(Path(path).resolve()), 'sha256': sha(path),
               'sample_rate': SOURCE_RATE, 'frames': frames, 'channels': channels,
               'downmix': 'arithmetic stereo mean; no sample offset or gain matching'}


def extract_frames(path, role):
    x, input_info = read_audio(path)
    y = resample_poly(x, 1, 2)
    settings = SETTINGS[role]
    started = time.perf_counter()
    hz, voiced, probabilities = librosa.pyin(
        y, sr=ANALYSIS_RATE, hop_length=HOP, center=False, **settings)
    elapsed = time.perf_counter() - started
    rows = []
    for i, (frequency, is_voiced, probability) in enumerate(zip(hz, voiced, probabilities)):
        anchor = (i * HOP + settings['frame_length'] // 2) * 2
        candidate = (float(69 + 12 * math.log2(frequency / 440.0))
                     if np.isfinite(frequency) and frequency > 0 else None)
        accepted = bool(is_voiced and candidate is not None and probability >= PROBABILITY_FLOOR)
        rows.append({'sample': anchor, 'pitch': candidate if accepted else None,
                     'candidate_pitch': candidate, 'pyin_voiced': bool(is_voiced),
                     'confidence': float(probability),
                     'excluded_reason': None if accepted else (
                         'below-unvalidated-probability-floor' if is_voiced else 'pyin-unvoiced')})
    return {'role': role, 'input': input_info, 'analysis_seconds': elapsed,
            'frame_samples': settings['frame_length'] * 2, 'hop_samples': HOP * 2,
            'frame_anchor': 'window center in decoded fixture samples',
            'analysis_offset_samples': 0, 'frames': rows}


def segment(report):
    role = report['role']
    notes, discarded, current = [], [], []
    last = None

    def finish(key, group):
        if not group:
            return
        start = max(0, group[0]['sample'] - HOP)
        end = min(report['input']['frames'], group[-1]['sample'] + HOP)
        confidence = float(np.mean([row['confidence'] for row in group]))
        identity = f'{role}-candidate-{group[0]["sample"]}'
        if len(group) < MIN_GROUP_FRAMES:
            discarded.append({'id': identity, 'role': role, 'start': start, 'end': end,
                              'pitch': key, 'frames': len(group), 'confidence': confidence,
                              'reason': 'Fewer than three adjacent same-rounded-pitch frames'})
            return
        notes.append(Note(identity, start, end, key, 80, role, confidence,
                          phrase='unsegmented-fixture',
                          contour=tuple((row['sample'], row['pitch']) for row in group)))

    for row in report['frames']:
        key = round(row['pitch']) if row['pitch'] is not None else None
        if key != last:
            finish(last, current)
            current = []
            last = key
        if key is not None:
            current.append(row)
    finish(last, current)
    return notes, discarded


def validate_assignment(performance, arrangement):
    """Independent checks of declared timing/coverage/occupancy constraints."""
    sources = {note.id: note for note in performance.notes}
    notes = arrangement['notes']
    ids = [note['id'] for note in notes]
    omitted = [item['note_id'] for item in arrangement['adaptations'] if item['kind'] == 'omission']
    if len(ids) != len(set(ids)) or set(ids) & set(omitted) or set(ids) | set(omitted) != set(sources):
        raise AssertionError('Every extracted note must be assigned once or explicitly omitted')
    coverage = sampled_pitches()
    for note in notes:
        source = sources[note['id']]
        if (note['start'], note['end']) != (source.start, source.end):
            raise AssertionError('Arrangement moved source timing')
        expected_player = 1 if arrangement['performers'] == 1 or source.role == 'melody' else 2
        if note['player'] != expected_player or note['role'] != source.role:
            raise AssertionError('Performer roles are not distinct')
        if note['pitch'] not in coverage[note['string']] or note['pitch'] != OPEN[note['string']] + note['fret']:
            raise AssertionError('Assigned note is not covered by its guitar string')
        if note['pitch'] != source.pitch and not any(
                item['note_id'] == source.id and item['kind'] == 'octave'
                and item['semitones'] == note['pitch'] - source.pitch for item in arrangement['adaptations']):
            raise AssertionError('Undisclosed pitch adaptation')
    for player in range(1, arrangement['performers'] + 1):
        own = [note for note in notes if note['player'] == player]
        for sample in sorted({note['start'] for note in own}):
            active = [note for note in own if note['start'] <= sample < note['end']]
            if len(active) != len({note['string'] for note in active}):
                raise AssertionError('Overlapping notes assigned to one physical string')
            frets = [note['fret'] for note in active if note['fret']]
            if len(frets) > 4 or frets and max(frets) - min(frets) > 4:
                raise AssertionError('Conservative fretting bounds exceeded')
    return {'source_timing_preserved': True, 'sampled_string_coverage': True,
            'string_occupancy_and_four_fret_span': True,
            'assignment_or_omission_complete': True,
            'player_roles': {str(player): sorted({n['role'] for n in notes if n['player'] == player})
                            for player in range(1, arrangement['performers'] + 1)},
            'limit': 'Checks conservative baseline rules, not human playability certification'}


def compare_estimate_to_reference(estimate, reference):
    if estimate['input']['frames'] != reference['input']['frames']:
        raise ValueError('Comparator length differs; do not silently align or truncate')
    errors, reference_voiced, both, extra = [], 0, 0, 0
    for est, ref in zip(estimate['frames'], reference['frames'], strict=True):
        if est['sample'] != ref['sample']:
            raise AssertionError('Comparator frame centers differ')
        if ref['pitch'] is not None:
            reference_voiced += 1
            if est['pitch'] is not None:
                both += 1
                errors.append(abs(est['pitch'] - ref['pitch']) * 100)
        elif est['pitch'] is not None:
            extra += 1
    return {'reference_algorithm_voiced_frames': reference_voiced,
            'both_algorithm_voiced_frames': both,
            'agreement_within_50c_over_all_reference_algorithm_voiced':
                sum(error <= 50 for error in errors) / reference_voiced if reference_voiced else None,
            'median_cents_where_both_algorithm_voiced': float(np.median(errors)) if errors else None,
            'over_600c_where_both_algorithm_voiced': sum(error > 600 for error in errors),
            'estimate_only_voiced_frames': extra,
            'reference_only_voiced_frames': reference_voiced - both,
            'offset_fitted_or_applied': False,
            'meaning': 'Same-extractor agreement with a reference stem, NOT annotated musical accuracy'}


def peak_memory():
    if os.name != 'nt':
        return None
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
                'PagefileUsage', 'PeakPagefileUsage')]
    counters = Counters(); counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.windll.kernel32; kernel.GetCurrentProcess.restype = ctypes.c_void_p
    fn = ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    fn.restype = ctypes.c_int
    if not fn(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError('Cannot measure process memory')
    return counters.PeakWorkingSetSize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=RESEARCH / 'mixed')
    parser.add_argument('--output', type=Path, default=RESEARCH / 'mixed-arrangement-001')
    parser.add_argument('--compare-reference-stems', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(RESEARCH) or output.exists():
        raise ValueError('Choose a new ignored research output directory')
    if shutil.disk_usage(RESEARCH).free < 8 * 1024**3 + 128 * 1024**2:
        raise ValueError('Keep the 8 GiB reserve and 128 MiB output allowance')
    paths = {'melody': args.inputs / 'estimated-vocals.wav', 'bass': args.inputs / 'estimated-bass.wav'}
    mixture = args.inputs / 'mixed.wav'
    for path in [mixture, *paths.values()]:
        if not path.is_file():
            raise FileNotFoundError(f'Wait for the separation evidence: {path.name}')
    _, mixture_info = read_audio(mixture)
    output.mkdir()
    started = time.perf_counter()
    evidence = {'scope': 'Private noncommercial scientific research; no production adoption',
                'timebase': {'source': 'decoded mixed fixture', 'origin_sample': 0,
                             'sample_rate': SOURCE_RATE, 'original_MedleyDB_origin': None,
                             'separation_latency_correction_samples': 0},
                'mixture': mixture_info, 'settings': SETTINGS,
                'shared_settings': {'librosa': librosa.__version__, 'analysis_rate': ANALYSIS_RATE,
                                    'hop': HOP, 'center': False, 'probability_floor': PROBABILITY_FLOOR,
                                    'minimum_same_rounded_pitch_frames': MIN_GROUP_FRAMES},
                'confidence_status': 'Uncalibrated pYIN probability; threshold frozen before estimates arrived',
                'limitations': ['Reference stems do not generate arrangement notes',
                                'No chord or complete harmony inference; bass line only',
                                'Fixed velocity80; contours retained but not rendered',
                                'Repeated same-pitch attacks are not detected',
                                'Rounded-pitch groups can fragment vibrato; short groups explicitly discarded',
                                'Reference comparator is not human-checked melody ground truth',
                                'Train fixture may overlap separator training; no held-out claim',
                                'No audition, loudness matching, full-song or phone evidence']}
    reports, all_notes, dropped = {}, [], []
    for role, path in paths.items():
        print('Extracting ' + role + ' candidates from separated estimate', flush=True)
        report = extract_frames(path, role)
        if report['input']['frames'] != mixture_info['frames']:
            raise ValueError('Separated output length differs from mixture; no implicit alignment allowed')
        notes, discarded = segment(report)
        reports[role] = report; all_notes.extend(notes); dropped.extend(discarded)
        dump(output / (role + '-frames.json'), report)
    performance = Performance('mixed-fixture-sha256:' + mixture_info['sha256'], SOURCE_RATE, all_notes)
    performance.validate()
    dump(output / 'performance.json', asdict(performance))
    dump(output / 'discarded-candidate-groups.json', dropped)
    evidence['retained_note_candidates'] = dict(Counter(note.role for note in all_notes))
    evidence['discarded_short_groups'] = dict(Counter(item['role'] for item in dropped))
    evidence['extraction'] = {role: {key: report[key] for key in ('input', 'analysis_seconds', 'frame_samples', 'hop_samples')}
                              for role, report in reports.items()}
    evidence['renders'] = {}
    for performers, label in ((1, 'solo'), (2, 'duet')):
        print('Planning and silently rendering ' + label, flush=True)
        arrangement = arrange(performance, performers)
        checks = validate_assignment(performance, arrangement)
        if performers == 2 and not all(checks['player_roles'][str(player)] for player in (1, 2)):
            raise ValueError('A duet requires nonempty distinct-role assignments for both performers')
        dump(output / (label + '-arrangement.json'), arrangement)
        render_started = time.perf_counter()
        metrics = render(arrangement, output / (label + '.wav'))
        evidence['renders'][label] = {'metrics': metrics, 'seconds': time.perf_counter() - render_started,
                                     'sha256': sha(output / (label + '.wav')),
                                     'notes_by_role': dict(Counter(note['role'] for note in arrangement['notes'])),
                                     'adaptations_by_kind': dict(Counter(item['kind'] for item in arrangement['adaptations'])),
                                     'checks': checks}
    # Evaluation begins only after both estimate-only performance renders exist.
    if args.compare_reference_stems:
        evidence['reference_stem_comparison'] = {}
        for role, filename in (('melody', 'reference-vocals.wav'), ('bass', 'reference-bass.wav')):
            print('Comparing ' + role + ' against reference stem; no arrangement changes', flush=True)
            comparator = extract_frames(args.inputs / filename, role)
            dump(output / (role + '-reference-comparator-frames.json'), comparator)
            evidence['reference_stem_comparison'][role] = compare_estimate_to_reference(reports[role], comparator)
    evidence['total_seconds'] = time.perf_counter() - started
    evidence['peak_process_working_set_bytes'] = peak_memory()
    evidence['output_bytes'] = sum(path.stat().st_size for path in output.iterdir() if path.is_file())
    evidence['script_sha256'] = sha(__file__)
    dump(output / 'evidence.json', evidence)
    print(json.dumps(evidence, indent=2, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
