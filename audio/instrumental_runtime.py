"""Existing private UMX + Basic Pitch overlay discovery; never installs anything."""
import hashlib
import json
from pathlib import Path
from . import analysis_runtime

ROOT=Path(__file__).resolve().parent
VERSION='umxhq-basicpitch-candidates-v1'
PINS={
 'research/basic-pitch/runtime-inventory.json':'4d7989113bdd5df0e02cd4f58a5c147b9e3d0c3dca7bd733f34145b80329625f',
 'research/guitarset/recovery-001/frozen-plan.json':'6c99f917aaea188a519f627471356176168b8bc78a7de9015f09ed526a6277aa',
 'basic_pitch.py':'56263cb85ce43d708cedafc9fe0e5b95c70aa9d9ca5535eab8759818f24ad6cf',
 'basic_pitch_upstream.py':'6da446aa0dbb4b237f5f17ffb8b8f6b8a4fd4df316e7eaa584f48442c197e710',
 'research/basic-pitch/nmp.onnx':'2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec',
}

def _sha(path,cancel):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        while data:=stream.read(1024*1024):
            if cancel():raise InterruptedError('Instrumental runtime verification cancelled')
            h.update(data)
    return h.hexdigest()

def _inventory():
    path=ROOT/'research/basic-pitch/runtime-inventory.json'
    if _sha(path,lambda:False)!=PINS['research/basic-pitch/runtime-inventory.json']:raise ValueError('Basic Pitch inventory changed')
    value=json.loads(path.read_text());overlay=(ROOT/'research/basic-pitch/runtime').resolve()
    for entry in value:
        target=(overlay/entry['path']).resolve()
        if not target.is_relative_to(overlay):raise ValueError('Invalid Basic Pitch inventory path')
        if target.stat().st_size!=entry['bytes']:raise ValueError('Basic Pitch runtime incomplete')
    return value

def runtime_status():
    base=analysis_runtime.runtime_status()
    result={**base,'ready':False,'runtime_version':VERSION+'+'+base['runtime_version'],
            'basic_pitch_model':str(ROOT/'research/basic-pitch/nmp.onnx'),
            'basic_pitch_overlay':str(ROOT/'research/basic-pitch/runtime'),
            'manifest':{'analysis':base.get('manifest'),'basic_pitch_pins':dict(PINS)}}
    if not base['ready']:return result
    try:
        for name in PINS:
            if not (ROOT/name).is_file():raise ValueError('Private instrumental runtime is not installed')
        _inventory()
        result.update(ready=True,reason=None)
    except (OSError,ValueError,KeyError,TypeError) as exc:result['reason']=str(exc)
    return result

def require_runtime(cancel=lambda:False):
    if cancel():raise InterruptedError('Instrumental runtime verification cancelled')
    result=runtime_status()
    if not result['ready']:raise RuntimeError(result['reason'])
    analysis_runtime.require_runtime(cancel=cancel)
    for name,digest in PINS.items():
        if _sha(ROOT/name,cancel)!=digest:raise RuntimeError('Instrumental runtime pin changed: '+name)
    overlay=Path(result['basic_pitch_overlay']).resolve()
    for entry in _inventory():
        if _sha(overlay/entry['path'],cancel)!=entry['sha256']:raise RuntimeError('Basic Pitch runtime integrity failed: '+entry['path'])
    return result
