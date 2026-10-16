"""One silent metadata phase and one separate Vulkan zero-generation probe."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time

from audio.arranger_prompt import canonical
from audio.gpu_trial_monitor import PROFILE
from audio.local_arranger_gpu_trial import TRIAL_ROOT, DEVICE_RESULT, parse_devices
from audio.managed_process import run_worker

APP=Path(__file__).resolve().parents[2]
CAP=8*1024**2
FILES=['audio/native_trial_process.py','audio/local_arranger_runtime.py','audio/local_arranger_gpu_runtime.py',
    'audio/local_arranger_gpu_trial.py','audio/gpu_memory.py','audio/gpu_trial_monitor.py',
    'audio/local_arranger_trial.py','audio/managed_process.py','audio/full_decode.py','audio/decoder.py',
    'audio/arranger_contract.py','audio/arranger_prompt.py','audio/research/probe_local_arranger_gpu.py',
    'audio/research/arranger_benchmark_v1.json']


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def size(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) if root.exists() else 0


def save(path,value):
    with path.open('xb') as stream:stream.write(canonical(value))


def cache_snapshot():
    """Read known shared NVIDIA cache lengths only; never delete/redirect them."""
    profile=Path(os.environ['USERPROFILE'])
    roots=[profile/'AppData/Local/NVIDIA/DXCache',profile/'AppData/Local/NVIDIA/GLCache',
           profile/'AppData/Local/NVIDIA Corporation/NV_Cache',profile/'AppData/LocalLow/NVIDIA/DXCache']
    result={}
    for root in roots:
        count=0;total=0
        if root.exists():
            for directory,dirs,files in os.walk(root,followlinks=False,onerror=lambda error:(_ for _ in ()).throw(error)):
                for name in [None,*dirs,*files]:
                    path=Path(directory) if name is None else Path(directory)/name
                    info=path.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                        raise ValueError('Cannot inventory a linked NVIDIA cache path')
                    if stat.S_ISREG(info.st_mode):
                        count+=1;total+=info.st_size
                        if count>50000 or total>128*1024**3:raise ValueError('Shared NVIDIA cache inventory exceeded read bound')
        result[str(root)]={'bytes':total,'files':count,'exists':root.exists()}
    return result


def check_space():
    allowance=2*1024**2+512*1024
    if size(TRIAL_ROOT)+allowance>CAP:raise ValueError('Keep previous GPU evidence; capture allowance exhausted')
    # Charge all owned work too. Reserve the unconsumed Vulkan scratch/output
    # envelope and the entire one-copy Emily proposal until its receipt exists.
    retained=size(APP/'audio/research')+size(APP/'audio/vendor/analysis')+size(APP/'work')
    emily=APP/'audio/research/emilyguitar-002/acquisition.json'
    reserve=72*1024**2
    if emily.exists() and json.loads(emily.read_text(encoding='utf-8')).get('state')=='complete':reserve+=64*1024**2
    else:reserve+=298228995
    # Root serializes acquisition/native phases. An incomplete/failed bank
    # consumes its actual bytes plus the full remaining acquisition envelope.
    if retained+reserve+allowance>7*1024**3:raise ValueError('Combined retained research/reservations exceed approved cap')
    if shutil.disk_usage(APP).free-reserve-allowance<8*1024**3:raise ValueError('Keep the approved 8 GiB disk reserve')
    dbpath=APP/'data/library.sqlite'
    if dbpath.is_file():
        with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]:
                raise ValueError('Wait for the app conversion before GPU testing')
    return {'retained_bytes':retained,'reserved_bytes':reserve,'next_capture_allowance_bytes':allowance}


def review_metadata():
    """Reinterpret successful native output; preserve the failed old parser run.

    This fixed correction addresses the independently reviewed invalid physical
    versus Vulkan heap-total equality. It never reruns native code or changes
    resource admission. Raw artifacts and original worker results remain intact.
    """
    check_space();source=TRIAL_ROOT/'metadata';results={}
    for op in ('help','version','devices'):
        folder=source/op;path=folder/(op+'.result.json')
        supervisor=json.loads((folder/'supervisor.json').read_text(encoding='utf-8'))['result']
        if sha(path)!=supervisor['sha256'] or path.stat().st_size!=supervisor['bytes']:
            raise ValueError('Original metadata receipt verification failed')
        value=json.loads(path.read_text(encoding='utf-8'));metrics=value.get('native_metrics',{})
        if metrics.get('returncode')!=0 or metrics.get('owned_job_empty') is not True:
            raise ValueError('Native metadata execution was not successful and fully reaped')
        for name,row in value['captures'].items():
            capture=folder/name
            if sha(capture)!=row['sha256'] or capture.stat().st_size!=row['bytes']:
                raise ValueError('Original native metadata capture verification failed')
        if op!='devices' and value.get('state')!='checked':raise ValueError('Original metadata checks failed')
        if op=='devices' and (value.get('state')!='failed' or value.get('error')!='ValueError: Unexpected NVIDIA device-list memory'):
            raise ValueError('This correction only reinterprets the reviewed heap-total parser error')
        results[op]=value
    if len({r['runtime_identity_sha256'] for r in results.values()})!=1:
        raise ValueError('Metadata checks used different runtime identities')
    value=dict(results['devices']);value.update(state='checked',selected_device=parse_devices((source/'devices/devices.stdout').read_bytes()),
        original_result_sha256=sha(source/'devices/devices.result.json'),original_error=value['error'],
        correction='Vulkan heap/budget counters are distinct from physical NVML VRAM; strict identity and bounded telemetry retained.',
        native_reexecuted=False)
    value.pop('error')
    folder=DEVICE_RESULT.parent;folder.mkdir(exist_ok=False)
    save(folder/'frozen.json',{'inputs':{name:sha(APP/name) for name in FILES},
        'original_metadata_frozen_sha256':sha(source/'frozen.json'),
        'original_results':{op:sha(source/op/(op+'.result.json')) for op in results},
        'resource_profile_unchanged':PROFILE,'native_executed_in_review':False})
    for name in ('devices.stdout','devices.stderr'):
        with (folder/name).open('xb') as stream:stream.write((source/'devices'/name).read_bytes())
    save(DEVICE_RESULT,value)
    save(folder/'complete.json',{'state':'metadata_reviewed','device_result_sha256':sha(DEVICE_RESULT),
        'device_result_bytes':DEVICE_RESULT.stat().st_size,'selected_device':value['selected_device'],
        'native_reexecuted':False,'generation_gate_released':False})
    print(json.dumps({'state':'metadata_reviewed','selected_device':value['selected_device'],'native_reexecuted':False}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['metadata','metadata-review','count'],required=True)
    parser.add_argument('--case',type=int,choices=range(1,9))
    args=parser.parse_args()
    if args.phase=='metadata-review':
        if args.case is not None:raise ValueError('Metadata review takes no public case')
        review_metadata();return
    if (args.phase=='count')!=(args.case is not None):raise ValueError('Count requires one public development case')
    device_hash=None
    if args.phase=='count':
        if not (DEVICE_RESULT.parent/'complete.json').is_file():raise ValueError('Finish the metadata interpretation review first')
        prior=json.loads(DEVICE_RESULT.read_text(encoding='utf-8'))
        reviewed=json.loads((DEVICE_RESULT.parent/'complete.json').read_text(encoding='utf-8'))
        if (sha(DEVICE_RESULT)!=reviewed['device_result_sha256'] or DEVICE_RESULT.stat().st_size!=reviewed['device_result_bytes']
                or prior.get('state')!='checked'):raise ValueError('Device listing receipt verification failed')
        device_hash=sha(DEVICE_RESULT)
        # This profile permits one first model-load attempt, not eight searches
        # across development cases or automatic reduction/retry after failure.
        if any(TRIAL_ROOT.glob('development-*')):raise ValueError('Preserve and review the first GPU model attempt before any new trial')
    admission=check_space();cache_before=cache_snapshot();TRIAL_ROOT.mkdir(parents=True,exist_ok=True)
    folder=TRIAL_ROOT/('metadata' if args.phase=='metadata' else f'development-{args.case:02d}')
    folder.mkdir(exist_ok=False)
    frozen={'version':1,'phase':args.phase,'public_case_number':args.case,
        'inputs':{name:sha(APP/name) for name in FILES},'profile':PROFILE,
        'device_receipt_sha256':device_hash,'resources_before':admission,
        'shared_cache_before':cache_before,
        'generated_tokens_requested':0,'audio_used':False,
        'claims':'No proposal generation, forward-kernel/performance proof, musical acceptance or supplied song processing.'}
    save(folder/'frozen.json',frozen)
    for name in FILES:
        target=folder/'code'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((APP/name).read_bytes())
    operations=['help','version','devices'] if args.phase=='metadata' else ['count']
    for op in operations:
        check_space();directory=folder/op;directory.mkdir()
        request={'operation':op,'work_dir':str(directory.resolve())}
        if op=='count':
            data=json.loads((APP/'audio/research/arranger_benchmark_v1.json').read_text(encoding='utf-8'))
            raw=canonical(data['cases'][args.case-1]['input']);(directory/'context.json').write_bytes(raw)
            request.update(context_sha256=hashlib.sha256(raw).hexdigest(),device_receipt_sha256=device_hash)
        save(directory/'request.json',request);began=time.monotonic()
        def progress(event):print(json.dumps({'operation':op,**event}),flush=True)
        try:
            result=run_worker('audio.local_arranger_gpu_trial',request,progress=progress,
                wall_seconds=150 if op=='count' else 60,memory_bytes=512*1024**2)
            save(directory/'supervisor.json',result)
            receipt=result['result'];path=directory/receipt['receipt']
            if path.stat().st_size!=receipt['bytes'] or sha(path)!=receipt['sha256']:raise ValueError('GPU result receipt verification failed')
            outcome=json.loads(path.read_text(encoding='utf-8'))
            print(json.dumps({'operation':op,'state':outcome['state'],'elapsed_seconds':time.monotonic()-began,
                'native_metrics':outcome.get('native_metrics'),'prompt_tokens':outcome.get('prompt_tokens'),
                'selected_device':outcome.get('selected_device'),'error':outcome.get('error')}),flush=True)
            if outcome['state']=='failed':raise ValueError('Inspect preserved GPU failure; no automatic retry')
        except BaseException as error:
            save(directory/'supervisor-error.json',{'error':repr(error),'elapsed_seconds':time.monotonic()-began});raise
        if any(sha(APP/name)!=digest for name,digest in frozen['inputs'].items()):raise ValueError('Source changed during GPU phase; preserve attempt')
        if size(TRIAL_ROOT)>CAP:raise ValueError('GPU evidence allowance exceeded')
    cache_after=cache_snapshot()
    baseline=cache_before if args.phase=='metadata' else json.loads((TRIAL_ROOT/'metadata/frozen.json').read_text(encoding='utf-8'))['shared_cache_before']
    growth=sum(max(0,row['bytes']-baseline[path]['bytes']) for path,row in cache_after.items())
    save(folder/'shared-cache.json',{'before':cache_before,'after':cache_after,'positive_growth_from_first_metadata_bytes':growth,
        'claim':'Shared known-cache net-size snapshots only; not peak writes or per-process attribution. Positive growth charged inside the GPU 64 MiB scratch reservation.'})
    if growth>64*1024**2:raise ValueError('Observed shared NVIDIA cache growth exceeded the GPU scratch reservation; review before further execution')
    save(folder/'complete.json',{'state':'complete','phase':args.phase,'trial_bytes':size(TRIAL_ROOT),
        'source_hashes_unchanged':True,'generated_tokens_requested':0})


if __name__=='__main__':main()
