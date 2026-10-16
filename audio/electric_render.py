"""Experimental dry Emilyguitar rendering; no app route or native self-test.

Fixed RR1 retains author note/release mappings. Each performer/string owns its
voices. Reattack applies an explicit <=5 ms outgoing fade and exact-onset reset.
This adapter does not change source events, infer fingering, or add effects.
"""
import array
import ctypes as C
import hashlib
import json
import math
import os
from pathlib import Path
import sys

from .emilyguitar import protect_bank, sample_limits, OPEN, MAX_FRET, PRELOAD, PROFILE_VERSION
from .render import Sfizz, _configure_offline, _write_pcm16

RENDER_VERSION = 'sfizz-emily-basic-rr1-string-damping-v1'
ARTICULATION = 'direct-sustain-first-variation'


def validate(arrangement):
    if type(arrangement) is not dict:
        raise ValueError('Expected an electric arrangement object')
    sr, origin = arrangement.get('sample_rate'), arrangement.get('render_origin_sample', 0)
    performers, notes = arrangement.get('performers'), arrangement.get('notes')
    if type(sr) is not int or sr != 44100 or type(origin) is not int or origin < 0:
        raise ValueError('Electric rendering requires the 44100 Hz integer source sample clock')
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose one or two electric performers')
    if type(notes) is not list or not 1 <= len(notes) <= 4000:
        raise ValueError('Invalid electric note count')
    seen, strings = set(), {}
    for note in notes:
        if type(note) is not dict or any(type(note.get(key)) is not int for key in ('start', 'end', 'pitch', 'velocity', 'player', 'string', 'fret')):
            raise ValueError('Invalid electric event values')
        if type(note.get('id')) is not str or not 1 <= len(note['id']) <= 200 or note['id'] in seen:
            raise ValueError('Invalid or repeated electric note identity')
        seen.add(note['id'])
        if not origin <= note['start'] < note['end'] <= origin+30*sr:
            raise ValueError('Electric notes must stay inside the bounded 30-second passage')
        if not 40 <= note['pitch'] <= 84 or not 1 <= note['velocity'] <= 127:
            raise ValueError('Electric pitch/velocity outside the declared profile; no clamping')
        if not 1 <= note['player'] <= performers or not 0 <= note['string'] < 6:
            raise ValueError('Invalid electric performer/string')
        if not 0 <= note['fret'] <= MAX_FRET or OPEN[note['string']]+note['fret'] != note['pitch']:
            raise ValueError('Electric string/fret differs from sounding pitch')
        if note.get('articulation') != ARTICULATION:
            raise ValueError('Unsupported electric articulation')
        strings.setdefault((note['player'], note['string']), []).append(note)
    if {note['player'] for note in notes} != set(range(1, performers+1)):
        raise ValueError('Each electric performer needs source material')
    for notes in strings.values():
        ordered = sorted(notes, key=lambda note: (note['start'], note['end'], note['id']))
        if any(a['end'] > b['start'] for a, b in zip(ordered, ordered[1:])):
            raise ValueError('Electric source gates overlap on one string')
    return sr, origin, strings


def _damping_gain(frame, next_attack, previous_attack, rate):
    if next_attack is None or previous_attack is None:
        return 1.
    duration = min(round(.005*rate), next_attack-previous_attack)
    if duration <= 0:
        return 1.
    phase = (frame-(next_attack-duration))/duration
    if phase <= 0:
        return 1.
    if phase >= 1:
        return 0.
    return .5*(1+math.cos(math.pi*phase))


def _configure(api, synth, sr):
    api.sfizz_set_sample_rate(synth, sr)
    api.sfizz_set_samples_per_block(synth, 256)
    quality = _configure_offline(api, synth)
    if quality != {'sample': 2, 'oscillator': 1}:
        raise RuntimeError('Unexpected electric engine quality defaults')
    for name, restype, args in [('sfizz_set_preload_size', None, [C.c_void_p, C.c_uint]),
                               ('sfizz_get_preload_size', C.c_uint, [C.c_void_p]),
                               ('sfizz_all_sound_off', None, [C.c_void_p])]:
        function = getattr(api, name)
        if hasattr(function, 'argtypes'):
            function.argtypes, function.restype = args, restype
    api.sfizz_set_preload_size(synth, PRELOAD)
    if api.sfizz_get_preload_size(synth) != PRELOAD:
        raise RuntimeError('Cannot set complete electric sample preload')
    return quality


def render(arrangement, output, *, cancel=lambda: False, progress=lambda _: None, level_policy='peak'):
    """Write new WAV, sibling .dry.f32 and .receipt.json; preserve failures.

    Native callers must use the existing managed worker/controller. This pure
    module does not launch a process. The bank guard spans loading, rendering
    and all synth frees; external System32 dependencies remain hash-only.
    """
    if level_policy not in ('peak', 'legacy_fixed'):
        raise ValueError('Unknown electric level policy')
    sr, origin, strings = validate(arrangement)
    output = Path(output)
    raw_output = output.with_suffix(output.suffix+'.dry.f32')
    receipt_output = output.with_suffix(output.suffix+'.receipt.json')
    partial = output.with_suffix(output.suffix+'.partial')
    spool = output.with_suffix(output.suffix+'.float.partial')
    receipt_partial = output.with_suffix(output.suffix+'.receipt.partial')
    paths = (output, raw_output, receipt_output, partial, spool, receipt_partial)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError('Preserve occupied electric output/evidence')
    if cancel():
        raise InterruptedError('Electric render cancelled')
    total = max(note['end'] for note in arrangement['notes'])-origin+sr
    commands = [{key: note[key] for key in ('id', 'start', 'end', 'pitch', 'velocity', 'player', 'string', 'fret')}
                for note in arrangement['notes']]
    command_hash = hashlib.sha256(json.dumps(commands, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    states, qualities, published = [], [], []
    api = None
    try:
        with protect_bank(cancel=cancel) as config:
            limits = sample_limits(arrangement['notes'], config)
            if cancel():
                raise InterruptedError('Electric render cancelled')
            api = Sfizz().lib
            try:
                for (player, string), notes in sorted(strings.items()):
                    if cancel():
                        raise InterruptedError('Electric render cancelled')
                    synth = api.sfizz_create_synth()
                    if not synth:
                        raise RuntimeError('Electric instance allocation failed')
                    state = [synth, [], 0, None]
                    states.append(state)
                    qualities.append(_configure(api, synth, sr))
                    if not api.sfizz_load_string(synth, str(config['bank']/'fixed-basic-derived.sfz').encode(), config['mapping'].encode()):
                        raise RuntimeError('Cannot load verified electric mapping')
                    for note in notes:
                        state[1].extend([(note['start']-origin, 1, note['pitch'], note['velocity']),
                                         (note['end']-origin, 0, note['pitch'], 0)])
                    state[1].sort()  # Off before on at the same integer sample.
                buffers = [(C.c_float*256)(), (C.c_float*256)()]
                channels = (C.POINTER(C.c_float)*2)(*buffers)
                raw_hash, peak, energy = hashlib.sha256(), 0., 0.
                with spool.open('xb') as stream:
                    for start in range(0, total, 256):
                        if cancel():
                            raise InterruptedError('Electric render cancelled')
                        length = min(256, total-start)
                        values = [0.]*(length*2)
                        for state in states:
                            synth, events, index, previous = state
                            cursor = start
                            while cursor < start+length:
                                while index < len(events) and events[index][0] == cursor:
                                    at, on, pitch, velocity = events[index]
                                    if on:
                                        if previous is not None:
                                            api.sfizz_all_sound_off(synth)
                                        previous = at
                                    (api.sfizz_send_note_on if on else api.sfizz_send_note_off)(synth, 0, pitch, velocity)
                                    index += 1
                                stop = min(start+length, events[index][0] if index < len(events) else start+length)
                                count = stop-cursor
                                next_attack = next((event[0] for event in events[index:] if event[1]), None)
                                api.sfizz_render_block(synth, channels, 2, count)
                                for i in range(count):
                                    gain = _damping_gain(cursor+i, next_attack, previous, sr)
                                    out = (cursor-start+i)*2
                                    values[out] += buffers[0][i]*gain
                                    values[out+1] += buffers[1][i]*gain
                                cursor = stop
                            state[2], state[3] = index, previous
                        samples = array.array('f', values)
                        if not all(math.isfinite(value) for value in samples):
                            raise RuntimeError('Electric render contains nonfinite samples')
                        peak = max(peak, max(map(abs, samples)))
                        energy += math.fsum(value*value for value in samples)
                        if sys.byteorder != 'little':
                            samples.byteswap()
                        raw = samples.tobytes()
                        raw_hash.update(raw)
                        stream.write(raw)
                        progress(.8*(start+length)/total)
            finally:
                for synth, *_ in states:
                    api.sfizz_free(synth)
            levels = _write_pcm16(spool, partial, sr, peak, level_policy, cancel, progress)
            if cancel():
                raise InterruptedError('Electric render cancelled')
            receipt = {'renderer_version': RENDER_VERSION, 'profile_version': PROFILE_VERSION,
                'instrument': 'Epiphone Emily the Strange direct electric samples', 'samples': 'Karoryfer Emilyguitar v1.001',
                'frames': total, 'sample_rate': sr, 'source_origin_sample': origin, **levels,
                'source_commands': commands, 'source_commands_sha256': command_hash,
                'source_gates_pitch_velocity_changed': False, 'command_transpose_semitones': 0,
                'instances': len(states), 'quality_by_instance': qualities, 'processing_mode': 'freewheeling',
                'preload_floats_per_channel': PRELOAD, 'raw_float': {'file': raw_output.name,
                    'bytes': total*8, 'sha256': raw_hash.hexdigest(), 'sample_peak': peak,
                    'rms': math.sqrt(energy/(total*2)), 'format': 'stereo little-endian float32 interleaved',
                    'stage': 'dry adapter output after declared string damping, before constant gain'},
                'bank': config['receipt'], 'sample_limits': limits, 'round_robin_policy': 'fixed first recorded variation only',
                'random_amplitude': 0, 'dynamics': 'Author velocity layers/curves retained; uncalibrated, measured energy inversions remain.',
                'same_string_choke': 'At most5ms half-cosine outgoing output fade followed by exact-onset all_sound_off',
                'maximum_reattack_damping_seconds': .005, 'render_tail_seconds': 1,
                'effects': [], 'loops_added': False, 'original_audio_mixed': False,
                'contours_not_rendered': [note['id'] for note in arrangement['notes'] if note.get('contour')],
                'physical_assumptions': {'open_midi': list(OPEN), 'max_fret': MAX_FRET,
                    'source_string_identity_known': False, 'fingering_biomechanics_verified': False},
                'native_adapter_acceptance': False, 'musical_acceptance': False, 'app_mode_enabled': False}
            with partial.open('rb') as stream:
                receipt['wav_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            with receipt_partial.open('xb') as stream:
                stream.write(json.dumps(receipt, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
            # Windows rename refuses an existing destination; no replacement of prior artifacts.
            for source, destination in ((spool, raw_output), (partial, output), (receipt_partial, receipt_output)):
                os.rename(source, destination)
                published.append(destination)
            progress(1.)
            return receipt
    except BaseException:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in (partial, spool, receipt_partial):
            path.unlink(missing_ok=True)
