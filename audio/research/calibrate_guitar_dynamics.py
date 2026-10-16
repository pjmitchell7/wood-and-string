"""Bounded silent calibration of existing samples; no bank copies or playback."""
import argparse
import ctypes as C
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import time

APP = Path(__file__).resolve().parents[2]
ROOT = APP/'audio/research'
SR = 44100
WINDOWS = {'100ms': 4410, '250ms': 11025, '750ms': 33075}
VELOCITIES = {1: (1, 24, 48), 2: (49, 80, 96), 3: (97, 112, 127)}


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(path, data):
    with path.open('x', encoding='utf-8') as f:
        json.dump(data, f, indent=2, allow_nan=False)


def run(request, emit):
    import numpy as np
    from audio.render import Sfizz, ASSETS
    from audio.dynamics import (POLICY, SCHEMA, LIMIT_DB, HEADER, regions,
        region_gain_db, measurement_region_sfz, text_hash, velocity_db, excluded_remaps)
    out = Path(request['output'])
    if out.parent != ROOT or not out.is_dir():
        raise ValueError('Owned calibration output required')
    plan = json.loads((out/'frozen-plan.json').read_text())
    if digest(out/'frozen-plan.json') != request['plan_sha256']:
        raise ValueError('Frozen plan changed')
    for path, key in ((APP/'audio/vendor/sfizz/bin/Release/sfizz.dll', 'sfizz_dll_sha256'),
                      (Path(__file__), 'script_sha256'),
                      (APP/'audio/dynamics.py', 'dynamics_source_sha256'),
                      (APP/'audio/render.py', 'render_adapter_sha256')):
        if digest(path) != plan[key]:
            raise ValueError('Frozen code or engine changed: '+key)
    source = ASSETS/'meta-steel-guitar.sfz'
    if digest(source) != plan['source_sfz_sha256']:
        raise ValueError('Source map changed')
    base = source.read_text(); inventory = regions(base)
    if len(inventory) != 375:
        raise ValueError('Expected exactly 375 explicit-string regions')
    if [(r['id'], r['sample']) for r in inventory] != [(r['id'], r['sample']) for r in plan['inventory']]:
        raise ValueError('Frozen inventory changed')
    sample_hashes = {r['id']: r['sample_sha256'] for r in plan['inventory']}
    for r in inventory:
        if digest(ASSETS/'ogg'/r['sample']) != sample_hashes[r['id']]:
            raise ValueError('Sample changed before calibration')
    api = Sfizz().lib
    buffers = [(C.c_float*256)(), (C.c_float*256)()]
    channels = (C.POINTER(C.c_float)*2)(*buffers)
    views = [np.ctypeslib.as_array(b) for b in buffers]
    began = time.monotonic()

    def measure(mapping, pitch, velocity):
        if time.monotonic()-began > 170:
            raise TimeoutError('Cooperative calibration deadline; retain partial ledger')
        synth = api.sfizz_create_synth()
        if not synth:
            raise RuntimeError('sfizz allocation failed')
        try:
            api.sfizz_set_sample_rate(synth, SR)
            api.sfizz_set_samples_per_block(synth, 256)
            if not api.sfizz_load_string(synth, str(ASSETS/'calibration-derived.sfz').encode(), mapping.encode()):
                raise RuntimeError('Cannot load calibration region')
            api.sfizz_send_note_on(synth, 0, pitch, velocity)
            sums = {key: 0. for key in WINDOWS}; peak = 0.; first = None; sha = hashlib.sha256()
            for start in range(0, WINDOWS['750ms'], 256):
                size = min(256, WINDOWS['750ms']-start)
                api.sfizz_render_block(synth, channels, 2, size)
                block = np.column_stack((views[0][:size], views[1][:size]))
                if not np.isfinite(block).all():
                    raise ValueError('Nonfinite raw float render')
                sha.update(block.astype('<f4', copy=False).tobytes())
                peak = max(peak, float(np.abs(block).max()))
                if first is None:
                    active = np.flatnonzero(np.any(block != 0, axis=1))
                    if len(active): first = start+int(active[0])
                for name, end in WINDOWS.items():
                    used = min(size, end-start)
                    if used > 0:
                        values = block[:used].astype(np.float64)
                        sums[name] += float(np.sum(values*values))
            return {'rms': {key: math.sqrt(sums[key]/(2*end)) for key, end in WINDOWS.items()},
                'peak': peak, 'first_nonzero_sample': first, 'raw_float_sha256': sha.hexdigest(),
                'frames': WINDOWS['750ms'], 'sample_rate': SR, 'channels': 2,
                'note_on_sample': 0, 'note_off': 'held beyond all measurement windows',
                'velocity': velocity}
        finally:
            api.sfizz_free(synth)

    references = {}; entries = []; measurements = 0
    with (out/'measurement-ledger.jsonl').open('x', encoding='utf-8') as ledger:
        def record(kind, identity, result):
            nonlocal measurements
            ledger.write(json.dumps({'kind': kind, 'id': identity, 'measurement': result}, allow_nan=False)+'\n')
            ledger.flush(); measurements += 1
            if measurements % 25 == 0:
                emit({'phase': kind, 'measurements': measurements, 'elapsed_seconds': time.monotonic()-began})
        for region in inventory:
            if region['layer'] == 2:
                result = measure(measurement_region_sfz(region, native=True), region['pitch'], 80)
                references[(region['string'], region['pitch'])] = result
                record('native-reference', region['id'], result)
        for region in inventory:
            unit = measure(measurement_region_sfz(region), region['pitch'], region['hivel'])
            record('unit-region', region['id'], unit)
            reference = references[(region['string'], region['pitch'])]
            entry = {**region, 'sample_sha256': sample_hashes[region['id']],
                'unit_measurement': unit, 'native_velocity80_reference': reference,
                'unit_rms': unit['rms']['250ms'], 'reference_rms': reference['rms']['250ms'],
                'status': 'pending-validation', 'validation': []}
            if entry['unit_rms'] > 1e-8 and entry['reference_rms'] > 1e-8:
                entry['unbounded_required_gain_db'] = 20*math.log10(entry['reference_rms']/entry['unit_rms'])+velocity_db(127)
            try:
                entry['gain_db'] = region_gain_db(entry['unit_rms'], entry['reference_rms'])
            except ValueError as exc:
                entry.update(status='rejected', reason=str(exc))
            entries.append(entry)
        for entry in entries:
            if entry['status'] == 'rejected':
                continue
            mapping = measurement_region_sfz(entry, gain_db=entry['gain_db'])
            for velocity in VELOCITIES[entry['layer']]:
                result = measure(mapping, entry['pitch'], velocity)
                record('calibrated-validation', entry['id'], result)
                expected_db = velocity_db(velocity)
                errors = {name: 20*math.log10(result['rms'][name]/entry['native_velocity80_reference']['rms'][name])-expected_db
                          if result['rms'][name] > 0 and entry['native_velocity80_reference']['rms'][name] > 0 else None
                          for name in WINDOWS}
                entry['validation'].append({'velocity': velocity, 'measurement': result,
                    'target_relative_db': expected_db, 'relative_level_error_db': errors})
            errors = [v['relative_level_error_db']['250ms'] for v in entry['validation']]
            entry['status'] = 'accepted' if all(x is not None and abs(x) <= .1 for x in errors) else 'rejected'
            if entry['status'] == 'rejected': entry['reason'] = 'Reference-window error exceeds fixed0.1dB tolerance'

    contradictions = []
    for pair in references:
        selected = [e for e in entries if (e['string'],e['pitch']) == pair]
        values = {v['velocity']: v for e in selected for v in e['validation']}
        for a, b in ((48,49),(96,97)):
            if a not in values or b not in values:
                contradictions.append({'string': pair[0], 'pitch': pair[1], 'boundary': [a,b],
                    'status': 'unmeasured-because-region-rejected'})
                continue
            for name in WINDOWS:
                ratio_db = 20*math.log10(values[b]['measurement']['rms'][name]/values[a]['measurement']['rms'][name])
                if ratio_db < -.05:
                    contradictions.append({'string': pair[0], 'pitch': pair[1], 'boundary': [a,b],
                        'window': name, 'change_db': ratio_db, 'status': 'level-inversion'})
    rejected = [e['id'] for e in entries if e['status'] != 'accepted']
    reference_inversions = [r for r in contradictions if r.get('window') == '250ms']
    manifest = {'schema': SCHEMA, 'policy': POLICY, 'status': 'rejected' if rejected or reference_inversions else 'accepted-reference-only',
        'source_sfz_sha256': plan['source_sfz_sha256'], 'source_sfz_text_sha256': text_hash(base),
        'sfizz_dll_sha256': plan['sfizz_dll_sha256'], 'reference_window_ms': 250, 'gain_limit_db': LIMIT_DB,
        'excluded_remaps': excluded_remaps(base),
        'velocity_curve': '18*(velocity-80)/126dB; all127 SFZ amplitude points; v127 normalized to1',
        'curve_normalization_offset_db': velocity_db(127), 'regions': entries,
        'rejected_region_ids': rejected, 'window_contradictions': contradictions,
        'summary': {'regions': len(entries), 'accepted_regions': len(entries)-len(rejected),
                    'measurements': measurements, 'elapsed_seconds': time.monotonic()-began,
                    'reference_inversions': len(reference_inversions),
                    'diagnostic_inversions': sum(r.get('window') in ('100ms','750ms') for r in contradictions)},
        'limits': ['No musical acceptance or source-velocity inference.',
                   'Only declared250ms RMS is calibrated; timbre, attacks and decay windows differ.',
                   'No source audio, new assets, effects, gate extension or timing shift.',
                   'Raw float renders were hashed and measured without PCM quantization or per-file normalization.',
                   'CPU/time/memory thresholds are sampled, not hard reservations.'],
        'provenance': plan}
    save(out/'manifest.json', manifest)
    return {'manifest': str(out/'manifest.json'), 'sha256': digest(out/'manifest.json'),
            'status': manifest['status'], **manifest['summary']}


def main():
    from audio.dynamics import regions, text_hash, POLICY, excluded_remaps
    from audio.render import ASSETS
    from audio.managed_process import run_worker
    import re
    parser = argparse.ArgumentParser(); parser.add_argument('--output-name', default='guitar-dynamics-001')
    name = parser.parse_args().output_name
    if not re.fullmatch(r'guitar-dynamics-\d{3}', name):
        raise ValueError('Fresh guitar-dynamics-NNN directory required')
    used = sum(p.stat().st_size for tree in (ROOT, APP/'audio/vendor/analysis') for p in tree.rglob('*') if p.is_file())
    if used+16*1024**2 > 4*1024**3 or shutil.disk_usage(ROOT).free < 8*1024**3+16*1024**2:
        raise RuntimeError('Frozen research cap or disk reserve would be crossed')
    source = ASSETS/'meta-steel-guitar.sfz'; base = source.read_text(); inventory = regions(base)
    if len(inventory) != 375: raise ValueError('Expected375 regions')
    out = ROOT/name; out.mkdir(exist_ok=False)
    plan = {'policy': POLICY, 'frozen_before_measurements': True,
        'source_sfz_sha256': digest(source), 'source_sfz_text_sha256': text_hash(base),
        'sfizz_dll_sha256': digest(APP/'audio/vendor/sfizz/bin/Release/sfizz.dll'),
        'script_sha256': digest(Path(__file__)), 'dynamics_source_sha256': digest(APP/'audio/dynamics.py'),
        'render_adapter_sha256': digest(APP/'audio/render.py'), 'sample_rate': SR, 'channels': 2,
        'reference': 'native velocity80 of same string/pitch, first250ms stereo RMS',
        'windows_samples': WINDOWS, 'hold': '750ms, no note-off before measurement ends',
        'curve': '18*(velocity-80)/126dB; continuously declared, explicit integer velocity points',
        'region_volume_bound_db': [-12,12], 'reference_tolerance_db': .1,
        'boundary_inversion_tolerance_db': .05, 'diagnostic_windows_do_not_redefine_reference': True,
        'validation_velocities_by_layer': VELOCITIES, 'refuse_promotion_if_any_region_rejected': True,
        'wall_seconds': 180, 'sampled_memory_bytes': 1024**3, 'raw_audio_files': False,
        'excluded_remaps': excluded_remaps(base),
        'inventory': [dict(id=r['id'], sample=r['sample'], sample_sha256=digest(ASSETS/'ogg'/r['sample'])) for r in inventory]}
    save(out/'frozen-plan.json', plan)
    request = {'output': str(out), 'plan_sha256': digest(out/'frozen-plan.json')}
    try:
        result = run_worker('audio.research.calibrate_guitar_dynamics', request,
            python=ROOT/'mixed/env/Scripts/python.exe', wall_seconds=180, memory_bytes=1024**3,
            progress=lambda event: print(json.dumps(event), flush=True))
        save(out/'run.json', result); print(json.dumps(result, indent=2), flush=True)
    except Exception as exc:
        save(out/'failed.json', {'type': type(exc).__name__, 'error': str(exc), 'partial_ledger_preserved': True})
        raise


if __name__ == '__main__': main()
