"""Silent independent time-marker reproduction; NEVER reads pitch annotations.

Current native AAC substitutes historical libfdk_aac. Validates crop/trim
mechanism, not the ancestry of the acquired music fixture.
"""
from pathlib import Path
import hashlib,json,subprocess,wave
import numpy as np
from scipy.signal import correlate

P=Path(__file__).parent
OUT=P/'timebase';OUT.mkdir(exist_ok=True)
FFMPEG=next((P/'decoder').rglob('*.exe'))
SR=44100

def write(path,y):
    pcm=np.clip(y*32767,-32768,32767).astype('<i2')
    with wave.open(str(path),'wb') as w:
        w.setparams((2,2,SR,0,'NONE','not compressed'));w.writeframes(np.column_stack([pcm,pcm]).tobytes())

def read(path):
    with wave.open(str(path)) as w:return np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').reshape(-1,2)[:,0].astype(float)/32768

def run(args):
    result=subprocess.run([str(FFMPEG),'-hide_banner','-nostdin','-y',*map(str,args)],capture_output=True,text=True)
    with (OUT/'commands.log').open('a') as f:f.write(json.dumps(list(map(str,args)))+'\n'+result.stderr+'\n')
    result.check_returncode()

def encode_decode(source,target,trim,raw):
    m4a=target.with_suffix('.m4a')
    options=['-ss','00:00:00.200'] if trim else []
    if raw:options+=['-f','s16le','-acodec','pcm_s16le','-ar',SR,'-ac',2]
    run([*options,'-i',source,'-c:a','aac','-b:a','256k',m4a])
    run(['-i',m4a,'-c:a','pcm_s16le',target])
    return read(target)

def main():
    rng=np.random.default_rng(91327)
    # Aperiodic identifiers spread through time; no pitch reference is involved.
    original=np.convolve(rng.standard_normal(SR*37),np.ones(7)/7,mode='same')*.12
    write(OUT/'original-markers.wav',original);original=read(OUT/'original-markers.wav')
    results=[]
    for label,trim,raw in [('no-trim-container-control',False,False),('two-trims-container',True,False),('two-trims-raw-wav',True,True)]:
        first=OUT/(label+'-crop30.wav');write(first,original[4*SR:34*SR])
        decoded=encode_decode(first,OUT/(label+'-pass1.wav'),trim,raw)
        second=OUT/(label+'-crop7.wav');write(second,decoded[15*SR:22*SR])
        final=encode_decode(second,OUT/(label+'-pass2.wav'),trim,raw)
        windows=[]
        for seconds in [.7,2.7,4.7]:
            start=round(seconds*SR);chunk=final[start:start+SR//5]
            corr=correlate(original,chunk,mode='valid',method='fft');index=int(np.argmax(corr))
            norm=float(corr[index]/np.sqrt(np.dot(chunk,chunk)*np.dot(original[index:index+len(chunk)],original[index:index+len(chunk)])))
            windows.append({'output_window_sample':start,'source_match_sample':index,'origin_sample':index-start,'origin_seconds':(index-start)/SR,'normalized_correlation':norm})
        results.append({'case':label,'decoded_frames':len(final),'windows':windows})
    evidence={'method':'Three separated aperiodic waveform windows; no annotation or pitch-score access','historical_codec_reproduced':False,'current_codec':'FFmpeg7.1 native AAC, not FFmpeg3.4/libfdk_aac/GPAC','known_origin_seconds':19,'raw_wav_header_bytes':44,'expected_raw_two_trim_origin_samples':19*SR+2*(8820-11),'results':results,'music_fixture_sha256':hashlib.sha256((P/'fixture.stem.mp4').read_bytes()).hexdigest(),'music_fixture_mapping_validated':False}
    (OUT/'evidence.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(evidence,indent=2))

if __name__=='__main__':main()
