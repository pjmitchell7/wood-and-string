"""Finite-support overlap adapter for the unchanged optional tone component.

At most 31 seconds of dry audio (30-second passage plus its 1-second release).
Each component call is at most 30 seconds. Every assembled frame occurs once;
there is no crossfade, crop, gain fit or event rewrite. Mathematical finite-FIR
equivalence is subject to recurrence roundoff, not asserted byte equivalence.
"""
import array
import math
from . import electric_tone as tone

VERSION = 'electric-tone-overlap-v1'
MAX_FRAMES = 31*tone.SAMPLE_RATE
MAX_OUTPUT_FRAMES = MAX_FRAMES+tone.MAX_TAIL
MAX_ORIGIN = 2**53-MAX_FRAMES-tone.MAX_FRAMES-tone.MAX_TAIL
PCM_LIMIT = 32767/32768


def support(settings):
    settings = tone.normalize_settings(settings)
    if settings['mode'] == 'bypass': return 0
    taps = tone.TAPS[settings['ambience']] if settings['ambience_mix'] else ()
    return tone.FILTER_TAIL+(taps[-1][0] if taps else 0)


def process(samples, *, settings, source_origin_sample=0, cancel=lambda: False):
    config = tone.normalize_settings(settings)
    if config['overload'] != 'refuse': raise ValueError('Preview export requires overload refusal')
    if (type(samples) is not array.array or samples.typecode not in ('f','d')
            or len(samples)%2 or not 2 <= len(samples) <= MAX_FRAMES*2):
        raise ValueError('Expected bounded stereo dry audio, including its release tail')
    if type(source_origin_sample) is not int or not 0 <= source_origin_sample <= MAX_ORIGIN:
        raise ValueError('Invalid source origin')
    before = tone._levels(samples, cancel, amplitude_limit=tone.MAX_INPUT_AMPLITUDE)
    identity = tone._identity(samples)
    frames, tail = len(samples)//2, support(config)
    internal = dict(config, overload='report')
    windows = []
    if config['mode'] == 'bypass':
        output = samples
        windows.append(dict(input_start=0,input_end=frames,retained_start=0,retained_end=frames))
    elif frames <= tone.MAX_FRAMES:
        result = tone.process(samples, settings=internal, source_origin_sample=source_origin_sample, cancel=cancel)
        output = result['samples']
        windows.append(dict(input_start=0, input_end=frames, retained_start=0, retained_end=frames+tail))
    else:
        output = array.array('d', [0.])*(2*(frames+tail))
        step = tone.MAX_FRAMES-tail
        for start in range(0, frames, step):
            tone._check_cancel(cancel)
            end = min(frames, start+step)
            left = max(0, start-tail)
            result = tone.process(samples[left*2:end*2], settings=internal,
                                  source_origin_sample=source_origin_sample+left, cancel=cancel)
            retained_end = end+tail if end == frames else end
            # Slice assignment must preserve array type and exact frame count.
            output[start*2:retained_end*2] = array.array('d', result['samples'][(start-left)*2:(retained_end-left)*2])
            windows.append(dict(input_start=left, input_end=end, retained_start=start, retained_end=retained_end))
    levels = tone._levels(output, cancel)
    if tone._identity(samples) != identity: raise ValueError('Dry input changed')
    metadata = dict(version=VERSION, component_version=tone.VERSION, settings=config,
        sample_rate=tone.SAMPLE_RATE, source_origin_sample=source_origin_sample,
        input_frames=frames, output_frames=frames+tail, added_tail_samples=tail,
        dry_input=identity, output=tone._identity(output), input_levels=before, output_levels=levels,
        windows=windows, policy='Exact finite-support lookback; retain each frame once; no crossfade or alignment fit',
        inserted_alignment_delay_samples=0, phase=('unchanged' if config['mode']=='bypass' else 'Frequency-dependent; group delay not compensated or measured'),
        quantization='round(sample*32768), nearest ties-to-even, no dither, no clipping',
        pcm_sample_peak_limit=PCM_LIMIT, true_peak_measured=False, normalization=False,
        musical_acceptance=False, source_events_changed=False)
    if levels['sample_peak'] > PCM_LIMIT:
        metadata['export_refused'] = True
        raise tone.ToneOverload(metadata)
    metadata['export_refused'] = False
    return dict(samples=output, metadata=metadata)
