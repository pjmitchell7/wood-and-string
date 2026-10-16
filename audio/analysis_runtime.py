"""Offline-only analysis runtime discovery and integrity verification.

Status performs cheap file/version checks. require_runtime hashes the installed
snapshot and model weights; call it off the HTTP event loop. Neither installs
packages, downloads data, nor imports the heavy analysis stack in the app process.
"""
from pathlib import Path
import hashlib,json
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parent
MANIFEST_PATH=ROOT/'vendor/analysis/runtime.json'
LOCK_PATH=ROOT/'analysis-requirements.lock'
MODELS={
 'bass-8d85a5bd.pth':'8d85a5bd3f996a8867fca0e8442e077e5a3f5ec747a6112742452a8f347b39c8',
 'drums-9619578f.pth':'9619578f885c54737cb0234f9f9a4a679ee4f31438fd77fd1dbe02bb16c2da0a',
 'other-b52fbbf7.pth':'b52fbbf76479e752bd72e02304c602ac7802aa5bfbfb9cd12054b2695d5093ab',
 'vocals-b62c91ce.pth':'b62c91cedbc7a066f1778ead5b5cecb377aa3a46a31af1cce7c5c8769339d083',
}
def sha256(path,cancel=lambda:False):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while True:
            if cancel():raise InterruptedError('Analysis runtime verification cancelled.')
            b=f.read(1024*1024)
            if not b:break
            h.update(b)
    return h.hexdigest()
def runtime_version():
    return 'umx-pyin-decode-v2-'+hashlib.sha256(LOCK_PATH.read_bytes()+json.dumps(MODELS,sort_keys=True).encode()).hexdigest()[:16]
def runtime_status():
    result={'ready':False,'reason':'Analysis runtime has not been set up.','python':None,'modeldir':None,'manifest':None,'runtime_version':runtime_version()}
    if (MANIFEST_PATH.parent/'setup.lock').exists():
        result['reason']='Analysis runtime setup is in progress.'
        return result
    try:
        m=json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
        if m['schema_version']!=1 or m['runtime_version']!=runtime_version():raise ValueError('Runtime setup is out of date.')
        python=Path(m['python']);modeldir=Path(m['modeldir'])
        if not python.is_file() or not all((modeldir/n).is_file() for n in MODELS):raise ValueError('Runtime files are missing.')
        result.update(ready=True,reason=None,python=str(python),modeldir=str(modeldir),manifest=m)
    except FileNotFoundError:pass
    except (KeyError,ValueError,TypeError,OSError) as e:result['reason']=str(e)
    return result
def require_runtime(cancel=lambda:False):
    if cancel():raise InterruptedError('Analysis runtime verification cancelled.')
    result=runtime_status()
    if not result['ready']:raise RuntimeError(result['reason'])
    m=result['manifest'];env=Path(m['environment']).resolve()
    def verify(item):
        if cancel():raise InterruptedError('Analysis runtime verification cancelled.')
        path=(env/item['path']).resolve()
        if not path.is_relative_to(env):raise RuntimeError('Invalid runtime manifest path.')
        if not path.is_file() or path.stat().st_size!=item['bytes'] or sha256(path,cancel)!=item['sha256']:raise RuntimeError('Analysis runtime file verification failed: '+item['path'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(verify,m['installed_files']):pass
    for n,h in MODELS.items():
        if sha256(Path(result['modeldir'])/n,cancel)!=h:raise RuntimeError('Model verification failed: '+n)
    if 'base_python' in m and sha256(m['base_python']['path'],cancel)!=m['base_python']['sha256']:raise RuntimeError('Base Python executable changed.')
    return result
