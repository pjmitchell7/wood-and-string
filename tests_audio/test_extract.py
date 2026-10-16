import array
import json
import math
from pathlib import Path
import random
import statistics
import tempfile
import unittest
import wave
from audio.extract import extract_wav

CASES={
 'clear':([60,64,67],[.30,.30,.40],False,0.),
 'harmonic-rich':([57,62,65],[.30,.30,.40],True,0.),
 'swing':([60,64,62,67],[.28,.14,.28,.14],True,0.),
 'rubato':([64,62,60],[.23,.39,.47],False,0.),
 'rapid-high':([76,79,81,79],[.12,.12,.12,.12],False,0.),
 'noisy-capture-proxy':([60,64,67],[.30,.30,.40],True,.015),
}

def write_fixture(path,pitches,durations,harmonics,noise,sr=16000):
    rng=random.Random(23); signal=[0.]*int(sr*.1); truth=[]
    for pitch,duration in zip(pitches,durations):
        start=len(signal);frequency=440*2**((pitch-69)/12);length=round(duration*sr)
        for j in range(length):
            phase=2*math.pi*frequency*j/sr
            value=.32*math.sin(phase)
            if harmonics:value+=.12*math.sin(phase*2)+.06*math.sin(phase*3)
            envelope=min(1,j/(sr*.005),(length-j)/(sr*.005))
            signal.append(value*envelope+rng.uniform(-noise,noise))
        truth.append((start,len(signal),pitch))
        signal.extend([0.]*int(sr*.06))
    with wave.open(str(path),'wb') as wav:
        wav.setparams((1,2,sr,0,'NONE','not compressed'))
        wav.writeframes(array.array('h',(round(v*32767) for v in signal)).tobytes())
    return truth

class ExtractTests(unittest.TestCase):
    def test_six_controlled_cases(self):
        measurements=[]
        with tempfile.TemporaryDirectory() as tmp:
            for name,options in CASES.items():
                path=Path(tmp)/f'{name}.wav';truth=write_fixture(path,*options)
                performance,report=extract_wav(path,'fixture-'+name)
                cents=[]
                for f in report['frames']:
                    actual=next((p for s,e,p in truth if s+320<=f['sample']<e-320),None)
                    if actual is not None and f['pitch'] is not None:cents.append(abs(f['pitch']-actual)*100)
                onset=[];offset=[]
                for start,end,pitch in truth:
                    found=min((n for n in performance.notes if n.pitch==pitch),key=lambda n:abs(n.start-start),default=None)
                    self.assertIsNotNone(found,name)
                    onset.append(abs(found.start-start)/16000)
                    offset.append(abs(found.end-end)/16000)
                result={'case':name,'median_pitch_error_cents':statistics.median(cents),'max_onset_error_ms':max(onset)*1000,'max_offset_error_ms':max(offset)*1000,'notes':len(performance.notes),'synthetic_only':True}
                measurements.append(result)
                self.assertLess(result['median_pitch_error_cents'],20,name)
                self.assertLessEqual(max(onset),.05,name)
                self.assertLessEqual(max(offset),.05,name)
        out=Path(__file__).parents[1]/'audio/results/extraction-evidence.json'
        out.write_text(json.dumps(measurements,indent=2))
    def test_passage_offset_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'x.wav';write_fixture(path,[64],[.5],False,0)
            p,r=extract_wav(path,'x',start_sample=800,end_sample=9600)
            self.assertTrue(p.notes);self.assertTrue(all(n.start>=800 for n in p.notes))
            self.assertEqual(r['frames'][0]['sample'],1120)
            with self.assertRaises(InterruptedError):extract_wav(path,'x',cancel=lambda:True)
            with wave.open(str(Path(tmp)/'stereo.wav'),'wb') as w:
                w.setparams((2,2,16000,0,'NONE','not compressed'));w.writeframes(b'\0'*6400)
            with self.assertRaises(ValueError):extract_wav(Path(tmp)/'stereo.wav','x')

if __name__=='__main__':unittest.main()
