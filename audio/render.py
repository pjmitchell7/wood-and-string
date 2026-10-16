"""Actual sfizz/Meta OGG diagnostic render with optional passive part capture.

sfizz has no MIDI-channel filtering. Each performer/string gets an isolated
instance and a derived exact-string map, instead of loading all channel maps.
"""
import ctypes as C
import os
from pathlib import Path
import wave
import array
import math
import sys

ROOT=Path(__file__).parent
ASSETS=ROOT/'vendor/Meta-open-sound/meta-steel-guitar'
RENDER_VERSION = 'sfizz-freewheel-live-quality-v1'

def string_sfz(string):
    text=(ASSETS/'meta-steel-guitar.sfz').read_text()
    section=text.split(f'<master> lochan={string+2} hichan={string+2}')[1].split('<master>')[0]
    return '<control> default_path=ogg/\n<global> ampeg_release=0.05 pitch_random=0 amp_random=0 off_mode=normal\n'+section

class Sfizz:
    def __init__(self):
        self.lib=C.CDLL(str(ROOT/'vendor/sfizz/bin/Release/sfizz.dll'))
        signatures={
            'sfizz_create_synth':(C.c_void_p,[]),
            'sfizz_free':(None,[C.c_void_p]),
            'sfizz_load_string':(C.c_bool,[C.c_void_p,C.c_char_p,C.c_char_p]),
            'sfizz_set_sample_rate':(None,[C.c_void_p,C.c_float]),
            'sfizz_set_samples_per_block':(None,[C.c_void_p,C.c_int]),
            'sfizz_send_note_on':(None,[C.c_void_p,C.c_int,C.c_int,C.c_int]),
            'sfizz_send_note_off':(None,[C.c_void_p,C.c_int,C.c_int,C.c_int]),
            'sfizz_render_block':(None,[C.c_void_p,C.POINTER(C.POINTER(C.c_float)),C.c_int,C.c_int]),
            'sfizz_enable_freewheeling':(None,[C.c_void_p]),
            'sfizz_get_sample_quality':(C.c_int,[C.c_void_p,C.c_int]),
            'sfizz_set_sample_quality':(None,[C.c_void_p,C.c_int,C.c_int]),
            'sfizz_get_oscillator_quality':(C.c_int,[C.c_void_p,C.c_int]),
            'sfizz_set_oscillator_quality':(None,[C.c_void_p,C.c_int,C.c_int]),
        }
        for name,(restype,args) in signatures.items():
            f=getattr(self.lib,name); f.restype=restype; f.argtypes=args


def _configure_offline(api, synth):
    """Wait for sample streaming without changing the existing quality defaults.

    The pinned engine has distinct live/offline quality defaults. Preserve its
    live qualities for this repair so the only changed behavior is offline
    loading. All calls run sequentially before loading or sending note events.
    """
    sample = api.sfizz_get_sample_quality(synth, 0)
    oscillator = api.sfizz_get_oscillator_quality(synth, 0)
    if not 0 <= sample <= 10 or not 0 <= oscillator <= 10:
        raise RuntimeError('Invalid sampled engine quality settings')
    api.sfizz_set_sample_quality(synth, 1, sample)
    api.sfizz_set_oscillator_quality(synth, 1, oscillator)
    if (api.sfizz_get_sample_quality(synth, 1) != sample
            or api.sfizz_get_oscillator_quality(synth, 1) != oscillator):
        raise RuntimeError('Cannot preserve sampled engine quality settings')
    api.sfizz_enable_freewheeling(synth)
    return {'sample': sample, 'oscillator': oscillator}

def _write_pcm16(spool, partial, sr, raw_peak, level_policy, cancel, progress):
    """Second pass: constant gain on float samples, then one PCM16 quantization."""
    if not math.isfinite(raw_peak) or raw_peak <= 1e-8:
        raise ValueError('Sampled render is silent or below the usable peak threshold')
    gain = 10 ** (-3 / 20) / raw_peak if level_policy == 'peak' else .25
    peak = 0
    frames = 0
    length = spool.stat().st_size
    with spool.open('rb') as source, wave.open(str(partial), 'wb') as wav:
        wav.setnchannels(2); wav.setsampwidth(2); wav.setframerate(sr)
        while data := source.read(65536):
            if cancel(): raise InterruptedError('Render cancelled')
            values = array.array('f'); values.frombytes(data)
            if sys.byteorder != 'little': values.byteswap()
            pcm = array.array('h')
            for value in values:
                scaled = value * gain
                if not math.isfinite(scaled) or abs(scaled) >= 1:
                    raise RuntimeError('Render exceeds PCM16 headroom')
                sample = round(scaled * 32768)
                if not -32768 < sample < 32767:
                    raise RuntimeError('Render reaches PCM16 full scale')
                peak = max(peak, abs(sample)); pcm.append(sample)
            if sys.byteorder != 'little': pcm.byteswap()
            wav.writeframesraw(pcm.tobytes()); frames += len(values) // 2
            progress(.8 + .2 * source.tell() / length)
    return {'peak': peak / 32768, 'raw_peak': raw_peak, 'gain_db': 20 * math.log10(gain),
            'level_policy': level_policy, 'level_measurement': 'sample_peak_not_lufs_or_true_peak', 'target_sample_peak_dbfs': -3 if level_policy == 'peak' else None,
            'clipped_samples': 0, 'quantization': 'PCM16 nearest, no dither', 'frames': frames}


def render(arrangement, output, cancel=lambda:False, progress=lambda p:None, level_policy="peak", *, stem_capture=None):
    """Render sampled notes to a generated listening rendition, not a source master.

    Default peak policy applies constant gain to a float32 intermediate to reach
    -3 dBFS sample peak before PCM16 conversion. No LUFS or true-peak guarantee.
    legacy_fixed preserves the former 0.25 gain for diagnostic comparison.
    Both passes stream bounded blocks; temporary files are removed on failure.
    Optional performer capture observes the same native calls and master values.
    The caller owns its admission, finish, publication and cleanup lifecycle.
    """
    if level_policy not in ('peak', 'legacy_fixed'):
        raise ValueError('Unknown render level policy')
    notes=arrangement['notes']; sr=arrangement['sample_rate']; output=Path(output)
    if not notes: raise ValueError('Arrangement contains no playable notes')
    origin=arrangement.get('render_origin_sample',0)
    if type(origin) is not int or origin<0 or any(n['start']<origin for n in notes):
        raise ValueError('Render origin must precede all notes in source sample coordinates')
    total=max(n['end'] for n in notes)-origin+sr
    if total>sr*120: raise ValueError('Diagnostic render is limited to 120 seconds')
    if stem_capture is not None:
        from .acoustic_stem_capture import AcousticStemCapture
        if not isinstance(stem_capture, AcousticStemCapture):
            raise TypeError('Expected an admitted acoustic performer capture object')
        stem_capture.bind_render(arrangement, origin_sample=origin, total_frames=total)
    output.parent.mkdir(parents=True,exist_ok=True)
    partial=output.with_suffix(output.suffix+'.partial')
    spool=output.with_suffix(output.suffix+'.float.partial')
    if output.exists() or partial.exists() or spool.exists(): raise FileExistsError('Refusing to overwrite render')
    api=Sfizz().lib; instances=[]; block=256; qualities=[]
    try:
        pairs=sorted({(n['player'],n['string']) for n in notes})
        for player,string in pairs:
            synth=api.sfizz_create_synth()
            if not synth: raise RuntimeError('sfizz allocation failed')
            instances.append([synth,[],0])
            api.sfizz_set_sample_rate(synth,sr); api.sfizz_set_samples_per_block(synth,block)
            qualities.append(_configure_offline(api, synth))
            if not api.sfizz_load_string(synth,str(ASSETS/'derived.sfz').encode(),string_sfz(string).encode()):
                raise RuntimeError('Cannot load sampled guitar')
            events=[]
            for n in notes:
                if (n['player'],n['string'])==(player,string):
                    events.extend([(n['start']-origin,1,n['pitch'],n['velocity']),(n['end']-origin,0,n['pitch'],0)])
            instances[-1][1]=sorted(events)
        buffers=[(C.c_float*block)(),(C.c_float*block)()]
        channels=(C.POINTER(C.c_float)*2)(*buffers)
        peak=0.
        with spool.open('xb') as floats:
            for start in range(0,total,block):
                if cancel(): raise InterruptedError('Render cancelled')
                size=min(block,total-start); mix=[0.]*(size*2)
                if stem_capture is not None: stem_capture.begin_block(start,size)
                for instance_index,state in enumerate(instances):
                    synth,events,index=state
                    while index<len(events) and events[index][0]<start+size:
                        sample,on,pitch,velocity=events[index]
                        (api.sfizz_send_note_on if on else api.sfizz_send_note_off)(synth,sample-start,pitch,velocity)
                        index+=1
                    state[2]=index
                    api.sfizz_render_block(synth,channels,2,size)
                    if stem_capture is not None:
                        stem_capture.add_instance(*pairs[instance_index],buffers[0],buffers[1])
                    for j in range(size):
                        mix[2*j]+=buffers[0][j]; mix[2*j+1]+=buffers[1][j]
                values=array.array('f',mix)
                if any(not math.isfinite(x) for x in values):
                    raise RuntimeError('Non-finite sampled render')
                peak=max(peak,max(abs(x) for x in values))
                if stem_capture is not None: stem_capture.end_block(values)
                if sys.byteorder != 'little': values.byteswap()
                floats.write(values.tobytes())
                if start%(block*100)==0: progress(.8*start/total)
        levels=_write_pcm16(spool,partial,sr,peak,level_policy,cancel,progress)
        if cancel(): raise InterruptedError('Render cancelled')
        os.replace(partial,output); progress(1.)
        return {'frames':total,'sample_rate':sr,'source_origin_sample':origin,**levels,'renderer':'sfizz 1.2.3','samples':'Meta steel OGG v1.0.0','instances':len(instances),
                'renderer_version':RENDER_VERSION,'processing_mode':'freewheeling','quality_by_instance':qualities}
    finally:
        for synth,_,_ in instances: api.sfizz_free(synth)
        if partial.exists(): partial.unlink()
        if spool.exists(): spool.unlink()
