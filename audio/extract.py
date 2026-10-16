"""Bounded monophonic WAV diagnostic, not dense-song melody separation.

YIN-style cumulative normalized difference. No model/download dependency.
The frame anchor is explicitly the window center in source sample coordinates.
"""
import array
import math
import statistics
import wave
from pathlib import Path
from .performance import Note,Performance

def pitch_frame(x,rate):
    energy=sum(v*v for v in x)/len(x)
    if energy<1e-6:return None,0.
    low=max(2,int(rate/1200)); high=min(int(rate/70),len(x)//2)
    width=len(x)-high
    differences=[0.]
    for lag in range(1,high+1):
        differences.append(sum((x[j]-x[j+lag])**2 for j in range(width)))
    cumulative=0.; normalized=[1.]
    for lag in range(1,high+1):
        cumulative+=differences[lag]
        normalized.append(differences[lag]*lag/cumulative if cumulative else 1.)
    lag=low
    while lag<high:
        if normalized[lag]<.18:
            while lag+1<=high and normalized[lag+1]<normalized[lag]:lag+=1
            delta=0.
            if 1<=lag<high:
                a,b,c=normalized[lag-1:lag+2];den=a-2*b+c
                if den:delta=.5*(a-c)/den
            frequency=rate/(lag+delta)
            return 69+12*math.log2(frequency/440),max(0.,1-normalized[lag])
        lag+=1
    return None,0.

def extract_wav(path,source_id,start_sample=0,end_sample=None,cancel=lambda:False,progress=lambda p:None):
    """Accept PCM16 MONO only, 70–1200 Hz, maximum 30-second passage.

    Caller must identify the source as isolated single-line audio. Mono encoding
    is NOT evidence of monophony. Reject stereo rather than claiming separation.
    """
    with wave.open(str(path),'rb') as wav:
        sr=wav.getframerate(); total=wav.getnframes()
        if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getcomptype()!='NONE':
            raise ValueError('Diagnostic extraction requires isolated monophonic PCM16 mono WAV; stereo songs need separation first')
        if not 8000<=sr<=96000:raise ValueError('Unsupported sample rate')
        end_sample=total if end_sample is None else end_sample
        if type(start_sample) is not int or type(end_sample) is not int or not 0<=start_sample<end_sample<=total:
            raise ValueError('Invalid source passage')
        if end_sample-start_sample>sr*30:raise ValueError('Diagnostic extraction is limited to 30 seconds')
        wav.setpos(start_sample)
        pcm=array.array('h',wav.readframes(end_sample-start_sample))
    factor=max(1,sr//8000); rate=sr/factor
    # Block averaging limits alias energy; not a production resampler.
    x=[sum(pcm[j:j+factor])/(32768*len(pcm[j:j+factor])) for j in range(0,len(pcm),factor)]
    frame=int(rate*.04); hop=int(rate*.01); frames=[]
    for pos in range(0,len(x)-frame+1,hop):
        if cancel():raise InterruptedError('Extraction cancelled')
        pitch,confidence=pitch_frame(x[pos:pos+frame],rate)
        anchor=start_sample+(pos+frame//2)*factor
        frames.append({'sample':anchor,'pitch':pitch,'confidence':confidence})
        if pos%(hop*20)==0:progress(pos/max(1,len(x)))
    groups=[]; current=[]; current_pitch=None
    for f in frames+[{'sample':end_sample,'pitch':None,'confidence':0}]:
        key=round(f['pitch']) if f['pitch'] is not None else None
        if key!=current_pitch:
            if current and len(current)>=3:groups.append((current_pitch,current))
            current=[];current_pitch=key
        if key is not None:current.append(f)
    notes=[]
    for i,(pitch,group) in enumerate(groups):
        start=max(start_sample,group[0]['sample']-hop*factor//2)
        end=min(end_sample,group[-1]['sample']+hop*factor//2)
        notes.append(Note(f'extracted-{i}',start,end,pitch,80,'melody',statistics.mean(f['confidence'] for f in group),contour=tuple((f['sample'],f['pitch']) for f in group)))
    performance=Performance(source_id,sr,notes)
    performance.validate();progress(1.)
    report={'method':'bounded-yin-difference-v1','scope':'isolated monophonic PCM16 WAV only; no separation or harmony extraction','frame_samples':frame*factor,'hop_samples':hop*factor,'frame_anchor':'window center, source-relative','analysis_offset_samples':0,'source_start_sample':start_sample,'source_end_sample':end_sample,'pitch_range_hz':[70,1200],'frames':frames,'warnings':['Mono file encoding does not establish musical monophony.','Fixed velocity; expressive dynamics extraction pending.','Notes shorter than 30ms and repeated same-pitch attacks may be lost.','Block averaging is not a production anti-alias resampler.','Window latency/uncertainty is retained, not corrected by quantizing the source.']}
    return performance,report
