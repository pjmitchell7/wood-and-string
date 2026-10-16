"""Frozen, silent sfizz mode comparison. No production settings are changed."""
import argparse
import ctypes as C
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

APP = Path(__file__).resolve().parents[2]
ROOT = APP/'audio/research'
SR, FRAMES, BLOCK = 44100, 44100, 256
REGIONS = ('3:73:2', '5:83:2', '5:84:3', '0:40:2')
MODES = ('stream', 'freewheel', 'preload', 'preload-freewheel')
PRELOAD = 1048576


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)


def bind(api):
    signatures = {
        'sfizz_get_preload_size': (C.c_uint, [C.c_void_p]),
        'sfizz_set_preload_size': (None, [C.c_void_p, C.c_uint]),
        'sfizz_enable_freewheeling': (None, [C.c_void_p]),
        'sfizz_get_sample_quality': (C.c_int, [C.c_void_p, C.c_int]),
        'sfizz_set_sample_quality': (None, [C.c_void_p, C.c_int, C.c_int]),
        'sfizz_get_oscillator_quality': (C.c_int, [C.c_void_p, C.c_int]),
        'sfizz_set_oscillator_quality': (None, [C.c_void_p, C.c_int, C.c_int]),
        'sfizz_get_num_bytes': (C.c_int, [C.c_void_p]),
    }
    for name, (restype, arguments) in signatures.items():
        method = getattr(api, name); method.restype = restype; method.argtypes = arguments


def run(request, emit):
    import av
    import numpy as np
    from audio.render import Sfizz, ASSETS
    from audio.dynamics import regions, measurement_region_sfz
    out = Path(request['output'])
    if out.parent != ROOT or not out.is_dir():
        raise ValueError('Owned experiment directory required')
    plan = json.loads((out/'frozen-plan.json').read_text())
    if digest(out/'frozen-plan.json') != request['plan_sha256']:
        raise ValueError('Frozen plan changed')
    for item in plan['inputs']:
        if digest(APP/item['path']) != item['sha256']:
            raise ValueError('Frozen input changed: '+item['path'])
    inventory = {r['id']: r for r in regions((ASSETS/'meta-steel-guitar.sfz').read_text())}
    api = Sfizz().lib; bind(api)
    buffers = [(C.c_float*BLOCK)(), (C.c_float*BLOCK)()]
    channels = (C.POINTER(C.c_float)*2)(*buffers)
    views = [np.ctypeslib.as_array(buffer) for buffer in buffers]
    began = time.monotonic(); results = []; expected_defaults = None

    def rms(data):
        return float(np.sqrt(np.mean(np.square(data.astype(np.float64)))))

    def compare(left, right):
        delta = right.astype(np.float64)-left.astype(np.float64)
        indices = np.flatnonzero(np.any(delta != 0, axis=1))
        return dict(exact=not len(indices), first_difference_sample=int(indices[0]) if len(indices) else None,
                    max_absolute_error=float(np.abs(delta).max()),
                    relative_rms_error=rms(delta)/max(rms(left), 1e-20),
                    prefix100ms_max_error=float(np.abs(delta[:4410]).max()))

    source_envelopes = []
    for identity in REGIONS:
        sample = ASSETS/'ogg'/inventory[identity]['sample']
        with av.open(str(sample)) as container:
            stream = container.streams.audio[0]
            if stream.codec_context.channels != 1: raise ValueError('Expected known mono samples')
            rate = stream.codec_context.sample_rate
            pieces = []; decoded = 0
            for frame in container.decode(stream):
                decoded += frame.samples
                if decoded > PRELOAD: raise ValueError('Large preload does not cover complete source')
                pieces.append(frame.to_ndarray().reshape(-1))
            source = np.concatenate(pieces)
            if not np.isfinite(source).all() or rms(source) <= 1e-8:
                raise ValueError('Unusable native source sample')
            hop = rate//100
            source_envelopes.append(dict(id=identity, sample_rate=rate, decoded_frames=decoded,
                duration_seconds=decoded/rate, channels=1, complete_source_fits_preload=True,
                first_second_rms_10ms=[rms(source[p:p+hop]) for p in range(0, rate, hop)],
                decoded_float_sha256=hashlib.sha256(source.astype('<f4').tobytes()).hexdigest()))
    save(out/'source-envelopes.json', source_envelopes)

    with (out/'ledger.jsonl').open('x', encoding='utf-8') as ledger:
        for repeat in range(3):
            for identity in REGIONS:
                region = inventory[identity]
                velocity = 80 if region['layer'] == 2 else 112
                for mode in MODES[repeat:]+MODES[:repeat]:
                    for gain in ((0, -6) if repeat % 2 == 0 else (-6, 0)):
                        if time.monotonic()-began > 110:
                            raise TimeoutError('Cooperative experiment deadline')
                        synth = api.sfizz_create_synth()
                        if not synth: raise RuntimeError('Synth allocation failed')
                        try:
                            defaults = dict(preload=api.sfizz_get_preload_size(synth),
                                sample=[api.sfizz_get_sample_quality(synth, m) for m in (0, 1)],
                                oscillator=[api.sfizz_get_oscillator_quality(synth, m) for m in (0, 1)])
                            if expected_defaults is None: expected_defaults = defaults
                            if defaults != expected_defaults: raise ValueError('Engine defaults changed between cells')
                            api.sfizz_set_sample_rate(synth, SR); api.sfizz_set_samples_per_block(synth, BLOCK)
                            for m in (0, 1):
                                api.sfizz_set_sample_quality(synth, m, defaults['sample'][0])
                                api.sfizz_set_oscillator_quality(synth, m, defaults['oscillator'][0])
                            if 'preload' in mode: api.sfizz_set_preload_size(synth, PRELOAD)
                            if api.sfizz_get_preload_size(synth) != (PRELOAD if 'preload' in mode else defaults['preload']):
                                raise ValueError('Preload setter did not match intended value')
                            for m in (0, 1):
                                if (api.sfizz_get_sample_quality(synth, m) != defaults['sample'][0]
                                        or api.sfizz_get_oscillator_quality(synth, m) != defaults['oscillator'][0]):
                                    raise ValueError('Quality setters did not match intended values')
                            if 'freewheel' in mode: api.sfizz_enable_freewheeling(synth)
                            mapping = measurement_region_sfz(region, native=True).rstrip()+f' volume={gain}\n'
                            if not api.sfizz_load_string(synth, str(ASSETS/'repeatability-derived.sfz').encode(), mapping.encode()):
                                raise RuntimeError('Sample map load failed')
                            api.sfizz_send_note_on(synth, 0, region['pitch'], velocity)
                            data = np.empty((FRAMES, 2), dtype='<f4')
                            for start in range(0, FRAMES, BLOCK):
                                count = min(BLOCK, FRAMES-start)
                                api.sfizz_render_block(synth, channels, 2, count)
                                for channel in (0, 1): data[start:start+count, channel] = views[channel][:count]
                            if not np.isfinite(data).all() or rms(data) <= 1e-8:
                                raise ValueError('Nonfinite, silent or unusably quiet output')
                            label = f"{identity.replace(':','-')}-{mode}-r{repeat}-g{gain}"
                            np.save(out/(label+'.npy'), data, allow_pickle=False)
                            entry = dict(id=identity, mode=mode, repeat=repeat, gain_db=gain, velocity=velocity,
                                file=label+'.npy', file_sha256=digest(out/(label+'.npy')),
                                raw_sha256=hashlib.sha256(data.tobytes()).hexdigest(), mapping_sha256=hashlib.sha256(mapping.encode()).hexdigest(),
                                defaults=defaults, preload=api.sfizz_get_preload_size(synth),
                                sample_quality=[api.sfizz_get_sample_quality(synth, m) for m in (0, 1)],
                                oscillator_quality=[api.sfizz_get_oscillator_quality(synth, m) for m in (0, 1)],
                                engine_buffer_bytes=api.sfizz_get_num_bytes(synth),
                                rms={f'{ms}ms': rms(data[:int(SR*ms/1000)]) for ms in (100, 250, 750, 1000)},
                                rms_10ms=[rms(data[p:p+441]) for p in range(0, FRAMES, 441)])
                            ledger.write(json.dumps(entry, allow_nan=False)+'\n'); ledger.flush(); results.append(entry)
                        finally:
                            api.sfizz_free(synth)
                emit(dict(region=identity, repeat=repeat, measurements=len(results)))
    indexed = {(e['id'], e['mode'], e['repeat'], e['gain_db']): e for e in results}
    def data(identity, mode, repeat, gain):
        return np.load(out/indexed[identity, mode, repeat, gain]['file'], allow_pickle=False)
    repeats = []; gains = []; modes = []
    for identity in REGIONS:
        for mode in MODES:
            for gain in (0, -6):
                reference = data(identity, mode, 0, gain)
                for repeat in (1, 2):
                    repeats.append(dict(id=identity, mode=mode, gain_db=gain, repeat=repeat,
                                        **compare(reference, data(identity, mode, repeat, gain))))
            for repeat in range(3):
                target = data(identity, mode, repeat, 0).astype(np.float64)*10**(-6/20)
                result = compare(target, data(identity, mode, repeat, -6))
                gains.append(dict(id=identity, mode=mode, repeat=repeat,
                                  passes=result['relative_rms_error'] <= 1e-4, **result))
        for mode in MODES:
            for repeat in range(3):
                modes.append(dict(id=identity, mode=mode, repeat=repeat,
                    **compare(data(identity, 'preload-freewheel', 0, 0), data(identity, mode, repeat, 0))))
    summary = dict(measurements=len(results), defaults=expected_defaults,
        repeat_comparisons=repeats, scalar_gain_comparisons=gains, mode_comparisons=modes,
        counts={mode: dict(repeat_mismatches=sum(not x['exact'] for x in repeats if x['mode']==mode),
                          gain_failures=sum(not x['passes'] for x in gains if x['mode']==mode),
                          full_preload_freewheel_mismatches=sum(not x['exact'] for x in modes if x['mode']==mode)) for mode in MODES},
        audio_played=False, production_changed=False, elapsed_seconds=time.monotonic()-began,
        limits='Four selected regions, three fresh-synth repeats; no universal determinism or musical acceptance.')
    save(out/'summary.json', summary)
    return dict(output=str(out), counts=summary['counts'], measurements=len(results), defaults=expected_defaults)


def main():
    from audio.render import ASSETS
    from audio.dynamics import regions
    from audio.managed_process import run_worker
    parser = argparse.ArgumentParser(); parser.add_argument('--output-name', default='engine-repeatability-001')
    name = parser.parse_args().output_name
    if not re.fullmatch(r'engine-repeatability-\d{3}', name): raise ValueError('Fresh numbered output required')
    used = sum(p.stat().st_size for root in (ROOT, APP/'audio/vendor/analysis') for p in root.rglob('*') if p.is_file())
    reserve = 64*1024**2
    if used+reserve > 4*1024**3 or shutil.disk_usage(ROOT).free < 8*1024**3+reserve:
        raise RuntimeError('Research or disk budget would be crossed')
    inventory = {r['id']: r for r in regions((ASSETS/'meta-steel-guitar.sfz').read_text())}
    paths = [Path(__file__), APP/'audio/render.py', APP/'audio/dynamics.py', APP/'audio/provenance/sfizz.h',
             APP/'audio/vendor/sfizz/bin/Release/sfizz.dll', ASSETS/'meta-steel-guitar.sfz']
    paths += [ASSETS/'ogg'/inventory[identity]['sample'] for identity in REGIONS]
    out = ROOT/name; out.mkdir(exist_ok=False)
    for source in paths[:4]: (out/('source-'+source.name)).write_bytes(source.read_bytes())
    plan = dict(frozen_before_measurements=True, sample_rate=SR, frames=FRAMES, block=BLOCK,
        regions=list(REGIONS), modes=list(MODES), repeats=3, gains_db=[0,-6], measurements=96,
        inputs=[dict(path=str(p.relative_to(APP)), sha256=digest(p)) for p in paths],
        quality='Capture fresh live defaults, pin both processing modes to those same sample/oscillator qualities',
        preload_control_frames_per_channel=PRELOAD, source_native_velocity='80 for medium layer,112 for high',
        onset=0, gate='Held past one-second output', new_synth_per_cell=True, no_sleeps=True,
        deterministic_order='Repeat, region, modes rotated by repeat, gain order reversed for odd repeat',
        exact_repeat_rule='Bit-identical float32 samples; report all differences', scalar_relative_rms_tolerance=1e-4,
        no_fitted_time_shift=True, duration_seconds=120, sampled_memory_bytes=1024**3,
        source_check='Decode immutable native samples before measurements; require complete mono frames fit large preload; retain native-rate10ms energy',
        no_production_changes=True, source_commit='946dc4b',
        hypothesis='Offline default streaming can reach unavailable sample frames; test freewheel and independent large preload.')
    save(out/'frozen-plan.json', plan)
    try:
        result = run_worker('audio.research.check_engine_repeatability',
            dict(output=str(out), plan_sha256=digest(out/'frozen-plan.json')),
            python=ROOT/'mixed/env/Scripts/python.exe', wall_seconds=120, memory_bytes=1024**3,
            progress=lambda event: print(json.dumps(event), flush=True))
        save(out/'run.json', result); print(json.dumps(result), flush=True)
    except Exception as exc:
        save(out/'failed.json', dict(type=type(exc).__name__, error=str(exc), partial_evidence_preserved=True))
        raise


if __name__ == '__main__': main()
