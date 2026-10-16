"""Synthetic private render bytes for integration contracts; never a renderer.

The constant waveform is unrelated to the declared notes. It tests identity,
geometry and receipt plumbing only, never synthesis, musical accuracy or sound.
"""
import array
import hashlib
import json
import math
import struct
import sys
import wave

import render_evidence as evidence


def write_synthetic_render(library, identity, arrangement):
    wav = library.stage/(identity+'.wav')
    raw = library.stage/(identity+'.wav.dry.f32')
    receipt_path = library.stage/(identity+'.wav.receipt.json')
    origin = arrangement['render_origin_sample']
    frames = max(note['end'] for note in arrangement['notes'])-origin+44100
    values = array.array('f', [.125, -.0625])*frames
    if sys.byteorder != 'little': values.byteswap()
    raw.write_bytes(values.tobytes())
    gain = 10**(-3/20)/.125
    left, right = round(.125*gain*32768), round(-.0625*gain*32768)
    with wave.open(str(wav), 'wb') as stream:
        stream.setparams((2, 2, 44100, frames, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<hh', left, right)*frames)
    commands = [{key: note[key] for key in evidence.COMMAND_KEYS} for note in arrangement['notes']]
    instances = len({(note['player'], note['string']) for note in arrangement['notes']})
    bank = {'profile_version': evidence.PROFILE_VERSION, 'manifest_sha256': dict(evidence.PINS),
            'mapping_sha256': 'a'*64, 'bank_files_verified': 331, 'source_wav_git_identities_verified': 323,
            'wav_headers_verified': 323, 'integrity_checked': True, 'selected_attack_regions': 64,
            'selected_release_regions': 16, 'mode_enabled': False,
            'scope': 'Pinned bank and observed static engine closure; no sound-quality or OS attestation.',
            'native': [{'path': f'C:/synthetic/never-loaded-{i}.dll', 'bytes': i+1,
                        'sha256': hashlib.sha256(str(i).encode()).hexdigest(), 'held_read_share_only': i == 0} for i in range(4)]}
    limits = [{'note_id': note['id'], 'region_id': 'basic-001', 'sample': 'notes/synthetic_rr1.wav',
               'stored_frames': 100, 'sample_root_midi': note['pitch'], 'transposed_stored_frames_estimate': 100.,
               'gate_exceeds_stored_sample_estimate': note['end']-note['start'] > 100,
               'limit': evidence.LIMIT_TEXT} for note in arrangement['notes']]
    receipt = {
        'renderer_version': evidence.RENDER_VERSION, 'profile_version': evidence.PROFILE_VERSION,
        'instrument': 'Epiphone Emily the Strange direct electric samples', 'samples': 'Karoryfer Emilyguitar v1.001',
        'frames': frames, 'sample_rate': 44100, 'source_origin_sample': origin,
        'peak': max(abs(left), abs(right))/32768, 'raw_peak': .125, 'gain_db': 20*math.log10(gain),
        'level_policy': 'peak', 'level_measurement': 'sample_peak_not_lufs_or_true_peak',
        'target_sample_peak_dbfs': -3, 'clipped_samples': 0, 'quantization': 'PCM16 nearest, no dither',
        'source_commands': commands, 'source_commands_sha256': hashlib.sha256(json.dumps(commands, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'source_gates_pitch_velocity_changed': False, 'command_transpose_semitones': 0,
        'instances': instances, 'quality_by_instance': [{'sample': 2, 'oscillator': 1} for _ in range(instances)],
        'processing_mode': 'freewheeling', 'preload_floats_per_channel': evidence.PRELOAD,
        'raw_float': {'file': raw.name, 'bytes': frames*8, 'sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
                      'sample_peak': .125, 'rms': math.sqrt((.125**2+.0625**2)/2),
                      'format': 'stereo little-endian float32 interleaved',
                      'stage': 'dry adapter output after declared string damping, before constant gain'},
        'bank': bank, 'sample_limits': limits,
        'round_robin_policy': 'fixed first recorded variation only', 'random_amplitude': 0,
        'dynamics': 'Author velocity layers/curves retained; uncalibrated, measured energy inversions remain.',
        'same_string_choke': 'At most5ms half-cosine outgoing output fade followed by exact-onset all_sound_off',
        'maximum_reattack_damping_seconds': .005, 'render_tail_seconds': 1,
        'effects': [], 'loops_added': False, 'original_audio_mixed': False,
        'contours_not_rendered': [note['id'] for note in arrangement['notes'] if note.get('contour')],
        'physical_assumptions': {'open_midi': list(evidence.OPEN), 'max_fret': evidence.MAX_FRET,
                                 'source_string_identity_known': False, 'fingering_biomechanics_verified': False},
        'native_adapter_acceptance': False, 'musical_acceptance': False, 'app_mode_enabled': False,
        'wav_sha256': hashlib.sha256(wav.read_bytes()).hexdigest()}
    receipt_path.write_bytes(json.dumps(receipt, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
    return {'wav_path': wav, 'raw_path': raw, 'receipt_path': receipt_path, 'receipt': receipt}
