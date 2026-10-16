"""Run: python -m audio.diagnostic (from guitar-app). Original composed fixture."""
import json
import time
from pathlib import Path
from .performance import Note,Performance,arrange
from .render import render

def fixture():
    sr=48000
    # Deliberate non-grid melody, bass and inner voice; not a familiar-song test.
    notes=[Note(f'm{i}',start,start+duration,pitch,velocity,'melody') for i,(start,duration,pitch,velocity) in enumerate([
        (0,18000,64,79),(19201,11000,67,86),(32003,24000,69,94),
        (59007,18000,67,77),(80009,31000,64,72),(116013,42000,62,69),
        (163017,60000,64,75)])]
    notes += [Note('b1',0,56000,40,60,'bass'),Note('h1',0,56000,59,55,'harmony'),Note('b2',59007,53000+59007,45,60,'bass'),Note('b3',116013,46000+116013,50,58,'bass'),Note('b4',163017,60000+163017,40,55,'bass')]
    return Performance('original-composed-diagnostic-v1',sr,notes)

if __name__=='__main__':
    folder=Path(__file__).parent/'results'; folder.mkdir(exist_ok=True)
    evidence=[]
    for count in (1,2):
        a=arrange(fixture(),count)
        (folder/f'fixture-{count}.json').write_text(json.dumps(a,indent=2))
        out=folder/f'fixture-{count}.wav'
        if out.exists(): out.unlink()  # Only this generated diagnostic fixture.
        start=time.perf_counter(); result=render(a,out)
        result.update(performers=count,elapsed_seconds=time.perf_counter()-start,adaptations=a['adaptations'])
        evidence.append(result)
    (folder/'render-evidence.json').write_text(json.dumps(evidence,indent=2))
    print(json.dumps(evidence,indent=2))
