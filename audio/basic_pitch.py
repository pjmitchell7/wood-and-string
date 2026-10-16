"""Standalone research adapter; never called by production analysis.

Windowing/output names and optional comparison timing follow Spotify Basic Pitch commit
fa5997af0a8210982619003269994a1be25eddf3 (Apache-2.0, Spotify AB).
https://github.com/spotify/basic-pitch/tree/fa5997af0a8210982619003269994a1be25eddf3
Event decoding here is deliberately NOT Spotify's default note_creation:
only independent onset-head peaks start events; no inferred onsets, Melodia,
duration suppression, or low-activation gap bridging. Activations are not
calibrated confidence, physical key releases, or articulation labels.
"""
import hashlib
import math
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

RATE, WINDOW, HOP, OVERLAP, FRAMES = 22050, 43844, 256, 30, 172
STRIDE = WINDOW - OVERLAP * HOP
MODEL_SHA256 = '2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec'
VERSION = 'basic-pitch-onset-only-research-v1'


def frame_samples(count, source_rate, origin=0):
    """Exact retained-window anchors; model attack latency remains unverified."""
    i = np.arange(count, dtype=np.int64)
    analysis_samples = (i // 142)*STRIDE + (i % 142)*HOP
    return origin + np.rint(analysis_samples * (source_rate/RATE)).astype(np.int64)


def official_frame_samples(count, source_rate, origin=0):
    """Upstream model_frames_to_time convention, rounded once at source rate.

    The empirical 1.8ms term is upstream, not a fitted correction for our audio.
    Retain this convention for comparison; it does not certify attack accuracy.
    """
    i = np.arange(count, dtype=np.float64)
    correction = (FRAMES * HOP - WINDOW) / RATE + .0018
    return origin + np.rint(source_rate * (i * HOP / RATE -
                                            np.floor(i / FRAMES) * correction)).astype(np.int64)


def decode_events(predictions, *, source_rate, source_origin_sample, source_length,
                  onset_threshold=.5, frame_threshold=.3):
    """Preserve polyphony and each independent onset-head local maximum.

    End = first following note-activation frame below threshold, next onset of
    that pitch, or passage end. A start with no support survives as an audited
    rejection. No pitch/role restriction or minimum duration silently hides it.
    """
    if source_rate <= 0 or source_length <= 0 or source_origin_sample < 0:
        raise ValueError('Invalid source coordinates')
    if not 0 < onset_threshold <= 1 or not 0 < frame_threshold <= 1:
        raise ValueError('Thresholds must be in (0,1]')
    arrays = {k: np.asarray(predictions[k], dtype=np.float32) for k in ('note', 'onset', 'contour')}
    n = len(arrays['note'])
    for k, width in [('note', 88), ('onset', 88), ('contour', 264)]:
        a = arrays[k]
        if a.shape != (n, width) or not np.isfinite(a).all() or (a < 0).any() or (a > 1).any():
            raise ValueError('Invalid activation matrix: ' + k)
    times = frame_samples(n + 1, source_rate, source_origin_sample)
    limit = source_origin_sample + source_length
    times = np.minimum(times, limit)
    events, attacks, rejected = [], [], []
    for p in range(88):
        head = arrays['onset'][:, p]
        # Plateau policy: first frame; boundary peaks are retained explicitly.
        starts = [i for i in range(n) if head[i] >= onset_threshold
                  and (i == 0 or head[i] > head[i-1])
                  and (i == n-1 or head[i] >= head[i+1])]
        for j, start in enumerate(starts):
            next_start = starts[j+1] if j+1 < len(starts) else n
            attack = {'frame': start, 'sample': int(times[start]), 'pitch': p+21,
                      'activation': float(head[start]), 'evidence': 'onset_head_local_peak'}
            attacks.append(attack)
            end = start
            while end < next_start and arrays['note'][end, p] >= frame_threshold:
                end += 1
            if end == start or times[end] <= times[start]:
                rejected.append({**attack, 'reason': 'no_positive_duration_note_support'})
                continue
            center = p * 3
            lo, hi = max(0, center-6), min(264, center+7)
            contour = []
            for f in range(start, end):
                row = arrays['contour'][f, lo:hi]
                b = lo + int(np.argmax(row))
                contour.append({'frame': f, 'sample': int(times[f]),
                                'midi': 21+b/3, 'activation': float(arrays['contour'][f, b])})
            events.append({'id': f'bp-{p+21}-{start}', 'pitch': p+21,
                           'start': int(times[start]), 'end': int(times[end]),
                           'start_frame': start, 'end_frame_exclusive': end,
                           'onset_activation': float(head[start]),
                           'mean_note_activation': float(arrays['note'][start:end, p].mean()),
                           'end_reason': 'next_same_pitch_onset' if end == next_start and end < n
                           else 'model_frame_coverage_end' if end == n else 'note_activation_below_threshold',
                           'contour_candidates': contour})
    covered = np.zeros((n,88), dtype=bool)
    for e in events:
        covered[e['start_frame']:e['end_frame_exclusive'],e['pitch']-21] = True
    orphan = (arrays['note'] >= frame_threshold) & ~covered
    return {'events': sorted(events, key=lambda e: (e['start'], e['pitch'])),
            'unassigned_active_frame_pitch_count': int(orphan.sum()),
            'unassigned_active_counts_by_midi': {str(p+21):int(orphan[:,p].sum()) for p in range(88) if orphan[:,p].any()},
            'attacks': sorted(attacks, key=lambda e: (e['sample'], e['pitch'])),
            'rejected_attacks': rejected, 'frame_samples': times[:-1].tolist(),
            'official_frame_samples': official_frame_samples(n, source_rate, source_origin_sample).tolist(),
            'source_coverage': {'first_anchor': int(times[0]), 'last_boundary': int(times[-1]),
                                'passage_end': limit, 'uncovered_tail_samples': max(0,limit-int(times[-1]))},
            'policy': {'onset_threshold': onset_threshold, 'frame_threshold': frame_threshold,
                       'minimum_duration_frames': 0, 'gap_tolerance_frames': 0,
                       'infer_onsets': False, 'melodia_trick': False,
                       'velocity': 'not inferred; note activation is not measured loudness',
                       'contour': 'local +/-2 semitone candidates; overlapping sources may contaminate'}}


def analyze_pcm(samples, source_rate, source_origin_sample, model_path, *,
                cancel=lambda: False, progress=lambda value: None):
    """Mono finite float PCM <=30s; explicit verified local ONNX; CPU only.

    Cancellation checks between windows; caller must manage process timeout to
    interrupt an in-flight native inference. Returns raw matrices as JSON lists.
    Resampling uses scipy polyphase, a declared difference from librosa.load.
    """
    x = np.asarray(samples, dtype=np.float32)
    if (x.ndim != 1 or source_rate not in (22050, 44100, 48000)
            or not 0 < len(x) <= source_rate*30 or not np.isfinite(x).all()
            or source_origin_sample < 0):
        raise ValueError('Expected finite mono PCM <=30s at 22050/44100/48000Hz')
    def check():
        if cancel():
            raise InterruptedError('Basic Pitch cancelled')
    check()
    path = Path(model_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        raise ValueError('Model hash differs from pinned official artifact')
    import onnxruntime as ort
    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])
    gcd = math.gcd(RATE, source_rate)
    mono = resample_poly(x, RATE//gcd, source_rate//gcd).astype(np.float32) if source_rate != RATE else x
    padded = np.pad(mono, (OVERLAP*HOP//2, 0))
    rows = {k: [] for k in ('note', 'onset', 'contour')}
    starts = list(range(0, len(padded), STRIDE))
    for idx, start in enumerate(starts):
        check()
        window = np.zeros((1, WINDOW, 1), np.float32)
        chunk = padded[start:start+WINDOW]
        window[0, :len(chunk), 0] = chunk
        values = session.run(['StatefulPartitionedCall:1', 'StatefulPartitionedCall:2',
                              'StatefulPartitionedCall:0'], {'serving_default_input_2:0': window})
        for (key, width), value in zip([('note',88),('onset',88),('contour',264)], values):
            if value.shape != (1, FRAMES, width):
                raise ValueError('Unexpected ONNX output shape')
            rows[key].append(value[0, 15:-15, :])
        progress((idx+1)/len(starts))
    check()
    expected = int(len(mono)/STRIDE*(FRAMES-OVERLAP))
    predictions = {k: np.concatenate(v)[:expected] for k,v in rows.items()}
    result = decode_events(predictions, source_rate=source_rate,
                           source_origin_sample=source_origin_sample, source_length=len(x))
    result.update(version=VERSION, model_sha256=digest, source_rate=source_rate,
                  source_origin_sample=source_origin_sample, source_length=len(x),
                  raw_predictions={k: v.tolist() for k,v in predictions.items()},
                  metadata={'input_resampler': 'scipy.signal.resample_poly default Kaiser5; no normalization',
                            'timing': 'origin+round(rate/22050*(floor(i/142)*36164+(i%142)*256))',
                            'official_timing': 'separate comparison field; upstream empirical correction not applied to source anchors',
                            'time_accuracy': 'window-derived anchors; physical attack latency unverified',
                            'runtime': ort.__version__, 'providers': session.get_providers(),
                            'production': False})
    return result
