"""Experimental Squier Jazz Bass renderer, separate from acoustic rendering.

render(arrangement, output, *, cancel, progress, level_policy='peak') uses the
existing absolute-sample arrangement shape, source sounding pitch25..63 and
velocity1..120. Native availability and musical acceptance are separate checks.
"""
import array
import ctypes as C
import math
import os
from pathlib import Path
import sys
from .growlybass import require_bank,moderate_map,PROFILE_VERSION
from .render import Sfizz,_configure_offline,_write_pcm16

RENDER_VERSION='sfizz-growlybass-moderate-rr1-v2'

def _reset_voices(api,synth):
    # Exact documented API, sfizz.h: clear buffers/reset voices. Rendering is
    # split at the event sample, so this never resets the incoming note.
    reset=api.sfizz_all_sound_off
    if hasattr(reset,'argtypes'):reset.argtypes=[C.c_void_p];reset.restype=None
    reset(synth)

def _damping_gain(frame,next_attack,previous_attack,sr):
    if next_attack is None or previous_attack is None:return 1.
    duration=min(round(.005*sr),next_attack-previous_attack)
    if duration<=0:return 1.
    phase=(frame-(next_attack-duration))/duration
    if phase<=0:return 1.
    if phase>=1:return 0.
    return .5*(1+math.cos(math.pi*phase))

def validate(arrangement):
    sr=arrangement.get('sample_rate');origin=arrangement.get('render_origin_sample',0)
    performers=arrangement.get('performers');notes=arrangement.get('notes')
    if sr!=44100 or type(sr) is not int or type(origin) is not int or origin<0:raise ValueError('Bass renderer requires the 44100 Hz source sample clock')
    if type(performers) is not int or performers not in (1,2):raise ValueError('Choose one or two bass performers')
    if not isinstance(notes,list) or not 1<=len(notes)<=4000:raise ValueError('Invalid bass note count')
    seen=set();strings={}
    for n in notes:
        if any(type(n.get(k)) is not int for k in ('start','end','pitch','velocity','player','string')):raise ValueError('Invalid bass note values')
        if not origin<=n['start']<n['end']<=origin+30*sr:raise ValueError('Bass notes must stay inside a 30-second passage')
        if not 25<=n['pitch']<=63:raise ValueError('Bass pitch outside the experimental sounding range25..63')
        if not 1<=n['velocity']<=120:raise ValueError('Bass profile supports moderate velocities1..120; louder values are not clamped')
        if not 1<=n['player']<=performers or not 0<=n['string']<=3:raise ValueError('Invalid bass performer or string')
        fret=n['pitch']-(25,33,38,43)[n['string']]
        if not 0<=fret<=20 or ('fret' in n and (type(n['fret']) is not int or n['fret']!=fret)):raise ValueError('Bass string/fret does not match sounding pitch')
        if n.get('articulation')!='clean-sustain-first-variation':raise ValueError('Unsupported bass articulation')
        if not isinstance(n.get('id'),str) or not n['id'] or n['id'] in seen:raise ValueError('Invalid bass event identity')
        seen.add(n['id']);strings.setdefault((n['player'],n['string']),[]).append(n)
    if {n['player'] for n in notes}!=set(range(1,performers+1)):raise ValueError('Each bass performer needs notes')
    for events in strings.values():
        ordered=sorted(events,key=lambda n:n['start'])
        if any(a['end']>b['start'] for a,b in zip(ordered,ordered[1:])):raise ValueError('Bass notes overlap on one string')
    return sr,origin,strings

def render(arrangement,output,*,cancel=lambda:False,progress=lambda _:None,level_policy='peak'):
    if level_policy not in ('peak','legacy_fixed'):raise ValueError('Unknown render level policy')
    sr,origin,strings=validate(arrangement)
    output=Path(output);partial=output.with_suffix(output.suffix+'.partial');spool=output.with_suffix(output.suffix+'.float.partial')
    if any(p.exists() for p in (output,partial,spool)):raise FileExistsError('Refusing to overwrite bass render')
    if cancel():raise InterruptedError('Bass render cancelled')
    config=require_bank(cancel=cancel);mapping=moderate_map(config['bank'])
    if cancel():raise InterruptedError('Bass render cancelled')
    output.parent.mkdir(parents=True,exist_ok=True);api=Sfizz().lib;states=[];qualities=[];block=256
    total=max(n['end'] for n in arrangement['notes'])-origin+sr
    try:
        for (player,string),notes in sorted(strings.items()):
            if cancel():raise InterruptedError('Bass render cancelled')
            synth=api.sfizz_create_synth()
            if not synth:raise RuntimeError('Bass instance allocation failed')
            state=[synth,[],0,None];states.append(state)
            api.sfizz_set_sample_rate(synth,sr);api.sfizz_set_samples_per_block(synth,block)
            quality=_configure_offline(api,synth)
            if quality!={'sample':2,'oscillator':1}:raise RuntimeError('Unexpected bass renderer quality defaults')
            qualities.append(quality)
            if not api.sfizz_load_string(synth,str(config['bank']/'moderate-derived.sfz').encode(),mapping.encode()):raise RuntimeError('Cannot load verified bass mapping')
            for n in notes:state[1].extend([(n['start']-origin,1,n['pitch']+12,n['velocity']),(n['end']-origin,0,n['pitch']+12,0)])
            state[1].sort() # Same-sample note-offs precede repeated attacks.
        buffers=[(C.c_float*block)(),(C.c_float*block)()];channels=(C.POINTER(C.c_float)*2)(*buffers);peak=0.
        with spool.open('xb') as stream:
            for start in range(0,total,block):
                if cancel():raise InterruptedError('Bass render cancelled')
                length=min(block,total-start);values=[0.]*(length*2)
                for state in states:
                    synth,events,index,previous_attack=state;cursor=start
                    while cursor<start+length:
                        while index<len(events) and events[index][0]==cursor:
                            at,on,pitch,velocity=events[index]
                            if on:
                                if previous_attack is not None:_reset_voices(api,synth)
                                previous_attack=at
                            (api.sfizz_send_note_on if on else api.sfizz_send_note_off)(synth,0,pitch,velocity);index+=1
                        stop=min(start+length,events[index][0] if index<len(events) else start+length)
                        count=stop-cursor
                        next_attack=next((event[0] for event in events[index:] if event[1]),None)
                        api.sfizz_render_block(synth,channels,2,count)
                        for i in range(count):
                            gain=_damping_gain(cursor+i,next_attack,previous_attack,sr);out=(cursor-start+i)*2
                            values[out]+=buffers[0][i]*gain;values[out+1]+=buffers[1][i]*gain
                        cursor=stop
                    state[2]=index;state[3]=previous_attack
                if not all(math.isfinite(v) for v in values):raise RuntimeError('Bass render contains nonfinite samples')
                peak=max(peak,max(map(abs,values)));samples=array.array('f',values)
                if sys.byteorder!='little':samples.byteswap()
                stream.write(samples.tobytes());progress(.8*(start+length)/total)
        levels=_write_pcm16(spool,partial,sr,peak,level_policy,cancel,progress)
        if cancel():raise InterruptedError('Bass render cancelled')
        os.replace(partial,output);progress(1.)
        return {'frames':total,'sample_rate':sr,'source_origin_sample':origin,**levels,
                'renderer':'sfizz 1.2.3','renderer_version':RENDER_VERSION,'instrument':'Squier Jazz Bass',
                'samples':'Karoryfer Growlybass v1.002','profile_version':PROFILE_VERSION,
                'command_transpose_semitones':12,'source_pitch_changed':False,'round_robin_policy':'first-recorded-variation-only',
                'map_release_seconds':.3,'render_tail_seconds':1,'same_string_choke':'half-cosine output fade then exact-onset voice reset',
                'maximum_reattack_damping_seconds':.005,
                'instances':len(states),'processing_mode':'freewheeling','quality_by_instance':qualities}
    finally:
        for synth,*_ in states:api.sfizz_free(synth)
        spool.unlink(missing_ok=True);partial.unlink(missing_ok=True)
