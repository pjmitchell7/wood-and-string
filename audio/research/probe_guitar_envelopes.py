"""Silent installed-bank envelope measurements. Writes new files; never plays."""
import array
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import wave

APP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))
from audio.render import render, ASSETS, string_sfz

SR = 44100


def note(identity, start, end, pitch=64, velocity=80, string=5):
    return dict(id=identity, start=round(start*SR), end=round(end*SR),
                pitch=pitch, velocity=velocity, string=string, player=1)


def rms(values):
    return math.sqrt(sum(x*x for x in values)/len(values)) if values else 0


def segment(data, start, end):
    return data[round(start*SR)*2:round(end*SR)*2]


def main():
    out = Path(sys.argv[1]) if len(sys.argv)>1 else APP/'audio/research/guitar-envelopes-001'
    if out.exists(): raise FileExistsError('Use a fresh probe directory')
    if shutil.disk_usage(APP).free < 8*1024**3+32*1024**2:
        raise RuntimeError('Insufficient reserve for bounded probe')
    out.mkdir(parents=True)
    report = {'sample_rate':SR, 'silent_file_render_only':True,
              'level_policy':'peak', 'runs':{}, 'measurements':{},
              'hashes':{'render.py':hashlib.sha256((APP/'audio/render.py').read_bytes()).hexdigest(),
                        'installed_sfz':hashlib.sha256((ASSETS/'meta-steel-guitar.sfz').read_bytes()).hexdigest()}}
    data = {}
    def run(name, notes):
        path = out/(name+'.wav')
        metrics = render({'notes':notes,'sample_rate':SR}, path)
        with wave.open(str(path),'rb') as wav:
            pcm=array.array('h');pcm.frombytes(wav.readframes(wav.getnframes()))
        if sys.byteorder!='little':pcm.byteswap()
        gain=10**(metrics['gain_db']/20)
        # Recover approximate pre-normalization amplitude; quantization remains.
        data[name]=[x/32768/gain for x in pcm]
        report['runs'][name]={'commands':notes,'metrics':metrics,
                             'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    velocities=[1,48,49,96,97,127]
    run('velocity', [note(str(v), .25+i*2, 1.25+i*2, velocity=v) for i,v in enumerate(velocities)])
    rows=[]
    for i,v in enumerate(velocities):
        start=.25+i*2; samples=segment(data['velocity'],start,start+.25)
        peak=max(map(abs,samples)); threshold=peak*.01
        first=next(j//2 for j,x in enumerate(samples) if abs(x)>=threshold)
        rows.append({'velocity':v,'first_250ms_raw_peak':peak,'first_250ms_raw_rms':rms(samples),
                     'first_1percent_peak_after_command_ms':first/SR*1000})
    report['measurements']['velocity_same_output']=rows
    run('held', [note('a',.25,1.75)])
    run('short', [note('a',.25,.5)])
    run('second_same', [note('b',1,1.5)])
    run('second_other', [note('b',1,1.5,string=4)])
    # Deliberately bypass planner to isolate engine choking; same-string
    # overlapping gates below are physically conflicting commands, not an
    # arrangement recommendation. Different-string control permits overlap.
    run('overlap_same', [note('a',.25,1.75),note('b',1,1.5)])
    run('overlap_other', [note('a',.25,1.75),note('b',1,1.5,string=4)])
    run('touching', [note('a',.25,1),note('b',1,1.5)])
    run('rest', [note('a',.25,.5),note('b',1,1.5)])
    run('second_changed', [note('b',1,1.5,pitch=65)])
    run('overlap_changed', [note('a',.25,1.75),note('b',1,1.5,pitch=65)])
    windows=[(.5,.55),(.55,.6),(.6,.7),(.7,.9),(1.05,1.4)]
    report['measurements']['window_raw_rms']={name:{f'{a}-{b}':rms(segment(values,a,b))
        for a,b in windows} for name,values in data.items() if name!='velocity'}
    for name,second in [('overlap_same','second_same'),('overlap_other','second_other'),
                        ('overlap_changed','second_changed'),('touching','second_same')]:
        a=segment(data[name],1.1,1.4);b=segment(data[second],1.1,1.4);held=segment(data['held'],1.1,1.4)
        residual=[x-y for x,y in zip(a,b)]
        report['measurements'][name+'_residual']={'rms':rms(residual),
            'held_rms':rms(held), 'difference_from_held_rms':rms([x-y for x,y in zip(residual,held)])}
    report['derived_map_example']=string_sfz(5).splitlines()[:4]
    report['output_bytes']=sum(p.stat().st_size for p in out.glob('*.wav'))
    (out/'evidence.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'output':str(out),'output_bytes':report['output_bytes'],
                      'measurements':report['measurements']},indent=2))


if __name__=='__main__':main()
