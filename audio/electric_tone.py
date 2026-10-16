"""Optional, offline clean Electric tone candidate; no renderer or file I/O.

``process`` accepts native-endian array('f'/'d') interleaved stereo at 44100 Hz.
The default is an exact, zero-copy bypass. Clean mode is *linear* gain and a
heuristic cabinet EQ, not a circuit/cabinet IR, a sustain repair, or an artist
preset. Its character has not been auditioned. No event data is accepted here.

Each low-pass is a finite exponential FIR: h[k] = (1-a)*a**k/(1-a**L),
0 <= k < L, a = exp(-2*pi*f/fs). The recurrence is the same convolution,
with floating-point roundoff. The frequency parameter places the underlying
pole; it is not a measured -3 dB frequency of the combined finite filter.
High-pass = input minus the first FIR; two short low-pass FIRs follow it.
Feed-forward ambience taps have finite support and no feedback. All full tails
are returned. Causal feedthrough is not zero phase/group delay; no alignment
compensation, limiting, normalization, compression or nonlinear drive is used.

The caller owns the input during the call (no concurrent mutation). Preserve
the dry render/evidence separately. Returned float samples are not PCM export;
overload must be resolved by explicit caller gain selection before export.
The 30-second limit includes any release tail already in the dry input. Peak
means sample peak, not oversampled true peak; RMS uses each complete buffer's
own duration and is not a loudness-match or an audibility measurement.
"""
import array
import hashlib
import math
import sys

VERSION = 'electric-tone-candidate-v1'
SAMPLE_RATE = 44100
MAX_FRAMES = 30*SAMPLE_RATE
MAX_INPUT_AMPLITUDE = 16.0
LOW_LENGTH = 2048
HIGH_LENGTH = 64
FILTER_TAIL = LOW_LENGTH-1+2*(HIGH_LENGTH-1)
TAPS = {
    'off': (),
    'room': ((882, .4), (1544, .3), (2867, .2), (4410, .1)),
    'delay': ((7938, 2/3), (15876, 1/3)),
}
DEFAULTS = dict(mode='bypass', gain_db=0., low_cut_hz=90., high_cut_hz=5500.,
                ambience='off', ambience_mix=0., overload='refuse')
MAX_TAIL = FILTER_TAIL+15876
BLOCK = 8192  # cancellation/validation chunks, not inserted signal buffering


class ToneOverload(ValueError):
    """No processed samples returned; metadata retains the measured overload."""
    def __init__(self, metadata):
        super().__init__('Tone output exceeds sample amplitude 1; select an explicit lower gain')
        self.metadata = metadata


def _number(value, lo, hi, name):
    if type(value) not in (int, float):
        raise ValueError(f'{name} must be a finite number, not a boolean')
    try:
        valid = math.isfinite(value) and lo <= value <= hi
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f'{name} outside [{lo}, {hi}]')
    return float(value)


def normalize_settings(settings=None):
    if settings is None:
        settings = {}
    if type(settings) is not dict or set(settings)-set(DEFAULTS):
        raise ValueError('Unknown tone setting')
    value = {**DEFAULTS, **settings}
    for key, choices in (('mode', ('bypass', 'clean')), ('ambience', tuple(TAPS)),
                         ('overload', ('refuse', 'report'))):
        if type(value[key]) is not str or value[key] not in choices:
            raise ValueError(f'Unsupported {key}')
    for key, lo, hi in (('gain_db', -24, 6), ('low_cut_hz', 60, 160),
                        ('high_cut_hz', 3500, 7500), ('ambience_mix', 0, .25)):
        value[key] = _number(value[key], lo, hi, key)
    if value['ambience'] == 'off' and value['ambience_mix'] != 0:
        raise ValueError('An ambience mix requires an explicit ambience setting')
    if value['mode'] == 'bypass' and any(value[key] != DEFAULTS[key] for key in
            ('gain_db', 'low_cut_hz', 'high_cut_hz', 'ambience', 'ambience_mix')):
        raise ValueError('Bypass does not silently ignore processing settings')
    return value


def _check_cancel(cancel):
    if cancel():
        raise InterruptedError('Tone processing cancelled')


def _levels(samples, cancel, *, amplitude_limit=None):
    peak = 0.
    energy = 0.
    nonzero = overloaded = 0
    for start in range(0, len(samples), BLOCK):
        _check_cancel(cancel)
        # Only this bounded chunk is copied; fsum avoids accumulating a long
        # low-level tail with a large leading energy using naive summation.
        chunk = samples[start:start+BLOCK]
        for value in chunk:
            if not math.isfinite(value):
                raise ValueError('Nonfinite tone sample')
            magnitude = abs(value)
            if amplitude_limit is not None and magnitude > amplitude_limit:
                raise ValueError('Input amplitude exceeds the declared numerical bound of 16')
            peak = max(peak, magnitude)
            nonzero += value != 0
            overloaded += magnitude > 1
        energy = math.fsum((energy, math.fsum(value*value for value in chunk)))
    return dict(sample_peak=peak, sample_peak_dbfs=20*math.log10(peak) if peak else None,
                rms=math.sqrt(energy/len(samples)), sum_squares=energy,
                nonzero_samples=nonzero, samples_above_full_scale=overloaded)


def _identity(samples):
    return dict(sha256=hashlib.sha256(memoryview(samples).cast('B')).hexdigest(),
                bytes=len(samples)*samples.itemsize, typecode=samples.typecode,
                byte_order=sys.byteorder, layout='interleaved-stereo')


def _lowpass(samples, frequency, length, cancel, *, highpass=False):
    """Finite FIR via O(n) recurrence; zero initial conditions, complete tail."""
    coefficient = math.exp(-2*math.pi*frequency/SAMPLE_RATE)
    oldest_weight = coefficient**length
    scale = (1-coefficient)/(1-oldest_weight)
    frames = len(samples)//2
    output = array.array('d', [0.])*(2*(frames+length-1))
    state = [0., 0.]
    for frame in range(frames+length-1):
        if frame % (BLOCK//2) == 0:
            _check_cancel(cancel)
        for channel in (0, 1):
            current = samples[2*frame+channel] if frame < frames else 0.
            old = samples[2*(frame-length)+channel] if length <= frame < frames+length else 0.
            state[channel] = coefficient*state[channel]+current-oldest_weight*old
            filtered = scale*state[channel]
            output[2*frame+channel] = current-filtered if highpass else filtered
    return output


def process(samples, *, sample_rate=SAMPLE_RATE, settings=None,
            source_origin_sample=0, cancel=lambda: False):
    """Return {'samples': array, 'metadata': JSON-safe receipt}; never play/write.

    Validation scans before any duration-sized output allocation. Bypass returns
    the same array (including signed zeros/typecode/bytes). Clean output is
    float64. 'report' permits finite float overload with an explicit export
    refusal flag; default 'refuse' raises ToneOverload carrying metadata.
    """
    config = normalize_settings(settings)
    if type(sample_rate) is not int or sample_rate != SAMPLE_RATE:
        raise ValueError('Tone requires the unchanged 44100 Hz source clock')
    if type(source_origin_sample) is not int or not 0 <= source_origin_sample <= 2**53-MAX_FRAMES-MAX_TAIL:
        raise ValueError('Invalid bounded source origin')
    if type(samples) is not array.array or samples.typecode not in ('f', 'd'):
        raise ValueError('Expected array of interleaved float32/float64 stereo samples')
    if len(samples) % 2 or not 2 <= len(samples) <= 2*MAX_FRAMES:
        raise ValueError('Expected stereo frames: one frame through 30 seconds')
    _check_cancel(cancel)
    before = _levels(samples, cancel, amplitude_limit=MAX_INPUT_AMPLITUDE)
    original_identity = _identity(samples)
    frames = len(samples)//2
    taps = TAPS[config['ambience']] if config['ambience_mix'] else ()
    if config['mode'] == 'bypass':
        output, filter_tail = samples, 0
    else:
        output = _lowpass(samples, config['low_cut_hz'], LOW_LENGTH, cancel, highpass=True)
        output = _lowpass(output, config['high_cut_hz'], HIGH_LENGTH, cancel)
        output = _lowpass(output, config['high_cut_hz'], HIGH_LENGTH, cancel)
        filter_tail = FILTER_TAIL
        if taps:
            wet = array.array('d', [0.])*(len(output)+2*taps[-1][0])
            for start in range(0, len(output), BLOCK):
                _check_cancel(cancel)
                for i in range(start, min(start+BLOCK, len(output))):
                    value = output[i]
                    wet[i] += value  # dry coefficient is 1, not 1-mix
                    for delay, weight in taps:
                        wet[i+2*delay] += value*config['ambience_mix']*weight
            output = wet
        gain = 10**(config['gain_db']/20)
        for start in range(0, len(output), BLOCK):
            _check_cancel(cancel)
            for i in range(start, min(start+BLOCK, len(output))):
                output[i] *= gain
    after = _levels(output, cancel)
    # Detect ordinary concurrent mutation; this is not a synchronized ownership
    # or ABA-proof protocol. The documented caller-owned-input rule still holds.
    if _identity(samples) != original_identity:
        raise ValueError('Dry input changed during tone processing')
    tail = filter_tail+(taps[-1][0] if taps else 0)
    metadata = dict(version=VERSION, stage='effects', settings=config,
        dry_input=original_identity, output=_identity(output), input_levels=before,
        output_levels=after, sample_rate=SAMPLE_RATE, input_frames=frames,
        output_frames=len(output)//2, source_origin_sample=source_origin_sample,
        input_end_sample=source_origin_sample+frames,
        output_end_sample=source_origin_sample+len(output)//2,
        level_measurement='Each complete buffer in digital amplitude units; sum_squares in squared amplitude; no perceived loudness or true-peak measurement',
        added_tail_samples=tail, added_tail_seconds=tail/SAMPLE_RATE,
        filter_support_tail_samples=filter_tail,
        ambience_taps=[dict(delay_samples=d, weight=w, effective_gain=w*config['ambience_mix']) for d, w in taps],
        filter=dict(kind='finite-exponential-FIR-highpass-plus-two-lowpass' if filter_tail else 'bypass',
                    low_length=LOW_LENGTH if filter_tail else 0, high_length=HIGH_LENGTH if filter_tail else 0,
                    zero_initial_conditions=True, feedback=False),
        latency=dict(inserted_alignment_delay_samples=0,
                     phase='frequency-dependent; group delay not compensated or measured' if filter_tail else 'unchanged',
                     causal_feedthrough=True),
        level_policy='explicit constant gain only; no limiter, normalization or clipping',
        sample_overload=after['samples_above_full_scale'] > 0,
        export_requires_gain_selection=after['samples_above_full_scale'] > 0,
        true_peak_measured=False, pcm_quantized=False, event_data_received=False,
        dry_evidence_replaced=False, musical_acceptance=False,
        scope='Synthetic DSP behavior only; not a recovered sustain, physical amp model or artist rig')
    if metadata['sample_overload'] and config['overload'] == 'refuse':
        raise ToneOverload(metadata)
    return dict(samples=output, metadata=metadata)
