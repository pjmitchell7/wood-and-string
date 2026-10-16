"""Bounded, offline UMX-HQ melody/bass analysis in a managed subprocess.

PCM16 44.1 kHz WAV retains its exact original-sample path. Other supported
local files use a separately identified canonical decoded passage.
The caller supplies application-owned local paths, never commands or URLs.
Heavy imports happen in run(), after managed_process establishes ownership.
No separated audio is published or passed to the sampled-guitar renderer.
"""
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import wave

RATE = 44100
HOP = 220  # at 22050 Hz; source-domain step is 440 samples
MAX_RESULT = 4 * 1024**2
PIPELINE_VERSION = 'umxhq-pyin-melody-bass-decode-v3'
INPUT_MIMES = frozenset({'audio/wav', 'audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/flac', 'audio/webm'})
TARGETS = ('vocals', 'drums', 'bass', 'other')
WEIGHTS = {
    'vocals': ('vocals-b62c91ce.pth', 'b62c91cedbc7a066f1778ead5b5cecb377aa3a46a31af1cce7c5c8769339d083'),
    'drums': ('drums-9619578f.pth', '9619578f885c54737cb0234f9f9a4a679ee4f31438fd77fd1dbe02bb16c2da0a'),
    'bass': ('bass-8d85a5bd.pth', '8d85a5bd3f996a8867fca0e8442e077e5a3f5ec747a6112742452a8f347b39c8'),
    'other': ('other-b52fbbf7.pth', 'b52fbbf76479e752bd72e02304c602ac7802aa5bfbfb9cd12054b2695d5093ab'),
}
SETTINGS = {
    'melody': {'fmin': 70.0, 'fmax': 1200.0, 'frame_length': 1024},
    'bass': {'fmin': 32.70319566257483, 'fmax': 400.0, 'frame_length': 2048},
}
WARNINGS = [
    'Experimental melody and bass estimates; mistakes and missing notes are possible.',
    'No chord accompaniment, full harmony, expressive phrasing or full-song analysis yet.',
    'Confidence is an uncalibrated pYIN probability, not a measured accuracy score.',
    'Fixed note velocity; pitch bends and vibrato are retained but not rendered.',
    'Repeated attacks on the same pitch are not separated; vibrato can fragment notes.',
    'Short pitch groups are explicitly discarded. Octave changes and omissions are reported.',
    'Source sample timing is retained; detected note boundaries are not human-verified.',
]


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _validate_passage(path, expected_sha, start_seconds, end_seconds):
    if (not isinstance(expected_sha, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha)
            or not all(type(v) in (float, int) and math.isfinite(v) for v in (start_seconds, end_seconds))
            or not 0 <= start_seconds < end_seconds or end_seconds-start_seconds > 30):
        raise ValueError('Choose a finite passage longer than zero and at most 30 seconds.')
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 250*1024**2:
        raise ValueError('The saved source is unavailable or too large.')
    origin, end = round(start_seconds*RATE), round(end_seconds*RATE)
    if end-origin < 4096:
        raise ValueError('Choose at least 0.1 seconds for melody and bass analysis.')
    if origin > RATE*7200:
        raise ValueError('Choose a passage starting within the first two hours.')
    return path, origin, end


def read_passage(path, expected_sha, start_seconds, end_seconds):
    """Read exact selected PCM samples without decoding or loading a full song."""
    path, origin, end = _validate_passage(path, expected_sha, start_seconds, end_seconds)
    # Verify and read from the same handle; no second pathname lookup after verification.
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected_sha:
            raise ValueError('Original audio failed verification. Import the original again.')
        stream.seek(0)
        try:
            with wave.open(stream, 'rb') as wav:
                if (wav.getframerate() != RATE or wav.getsampwidth() != 2
                        or wav.getnchannels() not in (1, 2) or wav.getcomptype() != 'NONE'):
                    raise ValueError('Mixed previews currently need a 44.1 kHz PCM16 mono or stereo WAV.')
                if end > wav.getnframes():
                    raise ValueError(f'End is beyond this recording ({wav.getnframes()/RATE:.3f} seconds).')
                channels = wav.getnchannels()
                wav.setpos(origin)
                raw = wav.readframes(end-origin)
                if len(raw) != (end-origin)*channels*2:
                    raise ValueError('The WAV passage is incomplete.')
                info = {'sample_rate': RATE, 'source_channels': channels,
                        'source_frames': wav.getnframes(), 'origin_sample': origin,
                        'end_sample': end, 'selected_frames': end-origin,
                        'decoded': False,
                        'timebase': 'Original WAV sample positions; no source resampling or time fitting.'}
        except (wave.Error, EOFError) as exc:
            raise ValueError('Mixed previews currently need a valid PCM16 WAV recording.') from exc
    return raw, info


def read_input_passage(path, expected_sha, start_seconds, end_seconds, decoded_path):
    """Preserve native PCM timing or explicitly use the decoded sample timeline.

    Caller owns decoded_path and must clean it after a forced worker exit.
    Decoder output is a temporary analysis copy, never a replacement original.
    """
    path, origin, end = _validate_passage(path, expected_sha, start_seconds, end_seconds)
    direct = False
    try:
        with wave.open(str(path), 'rb') as wav:
            direct = (wav.getframerate() == RATE and wav.getsampwidth() == 2
                      and wav.getnchannels() in (1, 2) and wav.getcomptype() == 'NONE')
    except (wave.Error, EOFError):
        pass
    if direct:
        return read_passage(path, expected_sha, start_seconds, end_seconds)
    from audio.decoder import decode_passage
    decoded_path = Path(decoded_path)
    # Never remove or overwrite a pre-existing path when cleanup runs.
    if decoded_path.exists() or decoded_path.with_suffix(decoded_path.suffix+'.decode.partial').exists():
        raise FileExistsError('Decoded analysis staging path is occupied')
    try:
        metadata = decode_passage(path, decoded_path, origin, end-origin,
                                  expected_sha256=expected_sha)
        with wave.open(str(decoded_path), 'rb') as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) != (RATE, 2, 2, end-origin):
                raise ValueError('Decoded passage did not retain its canonical sample bounds')
            raw = wav.readframes(end-origin)
        if len(raw) != (end-origin)*4:
            raise ValueError('Decoded passage is incomplete')
        return raw, {'sample_rate': RATE, 'source_channels': 2, 'source_frames': None,
                     'origin_sample': origin, 'end_sample': end, 'selected_frames': end-origin,
                     'decoded': True, 'decoder': metadata,
                     'timebase': 'First decoder-emitted sample after codec skip is zero; sequential resampling to 44100Hz. Browser original cues may differ.'}
    finally:
        if decoded_path.exists():
            decoded_path.unlink()


def segment_frames(rows, role, origin, length):
    """Keep source timing and account explicitly for every short pitch group."""
    from audio.performance import Note
    notes, dropped, group = [], [], []
    previous = None

    def finish(pitch, members):
        if not members:
            return
        start = origin + max(0, members[0]['sample']-HOP)
        end = origin + min(length, members[-1]['sample']+HOP)
        confidence = sum(r['confidence'] for r in members)/len(members)
        identity = f'{role}-{origin+members[0]["sample"]}'
        if len(members) < 3:
            dropped.append({'id': identity, 'role': role, 'start': start, 'end': end,
                            'pitch': pitch, 'frames': len(members), 'confidence': confidence,
                            'reason': 'Fewer than three adjacent same-rounded-pitch frames'})
        else:
            notes.append(Note(identity, start, end, pitch, 80, role, confidence,
                              phrase='unsegmented-passage',
                              contour=tuple((origin+r['sample'], r['pitch']) for r in members)))

    for row in rows:
        key = round(row['pitch']) if row['pitch'] is not None else None
        if key != previous:
            finish(previous, group)
            group = []
            previous = key
        if key is not None:
            group.append(row)
    finish(previous, group)
    return notes, dropped


def pitch_frames(stem, role):
    """Estimate once so alternative event policies can share identical evidence."""
    import librosa
    import numpy as np
    from scipy.signal import resample_poly
    settings = SETTINGS[role]
    y = resample_poly(stem.mean(axis=0).astype(np.float64), 1, 2)
    hz, voiced, probabilities = librosa.pyin(
        y, sr=22050, hop_length=HOP, center=False, **settings)
    rows = []
    excluded = Counter()
    for i, (frequency, is_voiced, probability) in enumerate(zip(hz, voiced, probabilities)):
        probability = float(probability)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('Pitch analysis returned an invalid probability.')
        pitch = float(69+12*math.log2(frequency/440)) if np.isfinite(frequency) and frequency > 0 else None
        accepted = bool(is_voiced and pitch is not None and probability >= .10)
        if not accepted:
            excluded['pyin-unvoiced' if not is_voiced else 'below-unvalidated-probability-floor'] += 1
        rows.append({'sample': (i*HOP+settings['frame_length']//2)*2,
                     'pitch': pitch if accepted else None, 'confidence': probability})
    report = {'settings': settings, 'analysis_rate': 22050, 'hop': HOP, 'center': False,
              'frame_anchor': 'Analysis window center mapped to original source samples',
              'probability_floor': .10,
              'total_frames': len(rows), 'excluded_frames': dict(excluded)}
    return rows, report


def interpret_frames(rows, stem, role, origin, *, interpretation='baseline'):
    """Keep baseline unchanged; make the bounded research comparison explicit.

    This helper does not select a new production default or infer source intent.
    Both policies consume the same pitch rows and separated waveform.
    """
    if interpretation == 'baseline':
        notes, dropped = segment_frames(rows, role, origin, stem.shape[1])
        return notes, {'minimum_group_frames': 3, 'retained_notes': len(notes),
                       'discarded_short_groups': dropped}
    if interpretation != 'attack-aware-v1':
        raise ValueError('Unknown note interpretation')
    from audio.phrasing import energy_evidence, segment_with_attack_association
    measured = energy_evidence(stem.mean(axis=0), rows, HOP*2)
    absolute = [{**row, 'sample': origin+row['sample']} for row in measured]
    notes, audit = segment_with_attack_association({
        'role': role, 'hop_samples': HOP*2, 'analysis_offset_samples': origin,
        'input': {'frames': origin+stem.shape[1]}, 'frames': absolute}, fixed_velocity=80)
    return notes, {'minimum_group_frames': None, 'retained_notes': len(notes),
                   'discarded_short_groups': [], 'interpretation': interpretation,
                   'phrasing_audit': audit}


def extract(stem, role, origin):
    rows, report = pitch_frames(stem, role)
    notes, events = interpret_frames(rows, stem, role, origin)
    report.update(events)
    return notes, report


def required_parts(request):
    """A Bass preview can follow foundation without requiring vocal melody."""
    roles = request.get('required_roles', ['melody'])
    if not isinstance(roles, list) or roles not in (['melody'], ['bass'], ['melody', 'bass']):
        raise ValueError('Invalid requested analysis parts.')
    return tuple(roles)


def run(request, emit):
    # These settings bound CPU parallelism; the parent owns memory/time termination.
    os.environ['OMP_NUM_THREADS'] = '2'
    os.environ['NUMBA_NUM_THREADS'] = '2'
    started = time.monotonic()
    required = required_parts(request)
    source_id = request.get('source_id')
    if not isinstance(source_id, str) or not source_id or len(source_id) > 512:
        raise ValueError('Missing source identity.')
    output = Path(request['result_path'])
    modeldir = Path(request['modeldir'])
    if not output.is_absolute() or not output.name.endswith('.analysis.json') or output.exists():
        raise ValueError('Invalid or occupied analysis staging path.')
    if not modeldir.is_absolute():
        raise ValueError('The local analysis model is unavailable.')
    emit({'phase': 'verifying', 'progress': 0})
    raw, timebase = read_input_passage(request['source_path'], request['source_sha256'],
                                     request['start_seconds'], request['end_seconds'],
                                     output.with_suffix('.decoded.wav'))
    import torch
    import openunmix
    import numpy as np
    import librosa
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    x = np.frombuffer(raw, dtype='<i2').reshape(-1, timebase['source_channels']).T.astype(np.float32)/32768
    if x.shape[0] == 1:
        x = np.repeat(x, 2, axis=0)
    if not np.any(x):
        raise ValueError('The selected passage is silent. Choose a passage with a clear melody.')
    del raw
    emit({'phase': 'loading_model', 'progress': .10})
    model = openunmix.umxhq(targets=list(TARGETS), pretrained=False, niter=1,
                          wiener_win_len=300, device='cpu')
    for name in TARGETS:
        filename, digest = WEIGHTS[name]
        path = modeldir/filename
        if sha(path) != digest:
            raise ValueError('Local analysis model failed verification. Repair its offline installation.')
        state = torch.load(path, map_location='cpu', weights_only=True)
        extras = set(state)-set(model.target_models[name].state_dict())
        if extras != {'sample_rate', 'stft.window', 'transform.0.window'} or float(state['sample_rate']) != RATE:
            raise ValueError('The installed model does not match the verified UMX-HQ checkpoint.')
        for key in extras:
            del state[key]
        model.target_models[name].load_state_dict(state, strict=True)
    model.eval()
    emit({'phase': 'separating', 'progress': .20})
    separation_start = time.monotonic()
    with torch.inference_mode():
        y = model(torch.from_numpy(x)[None])[0]
    if tuple(y.shape) != (4, 2, timebase['selected_frames']) or not torch.isfinite(y).all():
        raise ValueError('Separation did not preserve the selected source samples.')
    separation_seconds = time.monotonic()-separation_start
    estimates = y.numpy()
    del model, x
    notes, roles = [], {}
    for role, target, progress in (('melody', 0, .55), ('bass', 2, .75)):
        emit({'phase': 'estimating_'+role, 'progress': progress})
        role_notes, role_report = extract(estimates[target], role, timebase['origin_sample'])
        notes.extend(role_notes)
        roles[role] = role_report
    if required == ('melody',) and not roles['melody']['retained_notes']:
        raise ValueError('No stable melody was found in this passage. Try a clearer vocal passage.')
    if any(not roles[role]['retained_notes'] for role in required):
        raise ValueError('No stable notes were found for each requested part. Choose a clearer passage or a different Bass part.')
    from audio.performance import Performance
    performance = Performance(source_id, RATE, notes)
    performance.validate()
    warnings = list(WARNINGS)
    if timebase['decoded']:
        warnings.append('Analysis uses a decoded PCM16 copy; the original is unchanged. Original-player comparison timing may differ.')
    report = {'pipeline_version': PIPELINE_VERSION, 'source_sha256': request['source_sha256'],
              'timebase': timebase, 'roles': roles, 'warnings': warnings,
              'provenance': {'separator': 'Open-Unmix UMX-HQ', 'model_license': 'MIT',
                             'weights': {name: {'file': item[0], 'sha256': item[1]} for name, item in WEIGHTS.items()},
                             'openunmix': '1.3.0', 'torch': torch.__version__, 'librosa': librosa.__version__,
                             'device': 'cpu', 'niter': 1, 'wiener_win_len': 300,
                             'source_audio_in_guitar_render': False},
              'separation_seconds': separation_seconds, 'analysis_seconds': time.monotonic()-started}
    payload = json.dumps({'performance': asdict(performance), 'report': report,
                          'render_origin_sample': timebase['origin_sample']}, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_RESULT:
        raise ValueError('Analysis exceeded the bounded preview report size. Choose a shorter passage.')
    temporary = output.with_suffix('.partial')
    with temporary.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(output)
    emit({'phase': 'analyzed', 'progress': 1})
    return {'result_sha256': hashlib.sha256(payload).hexdigest(), 'result_bytes': len(payload)}
