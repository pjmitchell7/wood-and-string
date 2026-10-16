"""Silent fixed local runtime metadata / zero-generation token probes only."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

from audio.arranger_prompt import canonical
from audio.local_arranger_trial import TRIAL_ROOT
from audio.managed_process import run_worker

APP=Path(__file__).resolve().parents[2]
CAP=8*1024**2
FILES=['audio/native_trial_process.py','audio/local_arranger_runtime.py','audio/local_arranger_trial.py',
       'audio/managed_process.py','audio/full_decode.py','audio/decoder.py','audio/arranger_contract.py',
       'audio/arranger_prompt.py','audio/research/probe_local_arranger_runtime.py',
       'audio/research/arranger_benchmark_v1.json']


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def size(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) if root.exists() else 0


def save(path,value):
    with path.open('xb') as stream:stream.write(canonical(value))


def check_space():
    used=size(TRIAL_ROOT)
    if used+2*1024**2+256*1024>CAP:raise ValueError('Keep all existing trial evidence; insufficient remaining capture allowance')
    research=size(APP/'audio/research')+size(APP/'audio/vendor/analysis')+used
    if research+2*1024**2+256*1024>7*1024**3:raise ValueError('Approved retained research cap reached')
    if shutil.disk_usage(APP).free-2*1024**2-256*1024<8*1024**3:raise ValueError('Keep the 8 GiB disk reserve')
    dbpath=APP/'data/library.sqlite'
    if dbpath.is_file():
        with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]:
                raise ValueError('Wait for the app conversion before a local model probe')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['metadata','count'],required=True)
    parser.add_argument('--case',type=int,choices=range(1,9))
    parser.add_argument('--attempt',type=int,choices=range(1,10),default=1)
    parser.add_argument('--retry-reason')
    args=parser.parse_args()
    if (args.phase=='count')!=(args.case is not None):raise ValueError('Count requires one public development case; metadata takes no case')
    retry=None
    if args.attempt!=1:
        if args.phase!='count' or not args.retry_reason or not 1<=len(args.retry_reason)<=256:
            raise ValueError('Only an explicitly explained count admission retry is supported')
        prefix=f'development-{args.case:02d}'
        previous=TRIAL_ROOT/(prefix if args.attempt==2 else prefix+f'-attempt-{args.attempt-1:02d}')/'count'
        prior_path=previous/'count.result.json'
        prior=json.loads(prior_path.read_text(encoding='utf-8'))
        supervisor=json.loads((previous/'supervisor.json').read_text(encoding='utf-8'))['result']
        if sha(prior_path)!=supervisor['sha256'] or prior_path.stat().st_size!=supervisor['bytes']:
            raise ValueError('Previous admission receipt failed verification')
        if prior['state']!='failed' or prior.get('native_metrics') is not None or not prior.get('error','').startswith('ValueError: At least 8 GiB'):
            raise ValueError('Preserve native attempts; this retry is only for failed pre-launch RAM admission')
        retry={'previous_receipt':str(prior_path.relative_to(TRIAL_ROOT)),'previous_sha256':sha(prior_path),'reason':args.retry_reason}
    elif args.retry_reason is not None:raise ValueError('First attempts do not have a retry reason')
    check_space();TRIAL_ROOT.mkdir(parents=True,exist_ok=True)
    name='metadata' if args.phase=='metadata' else f'development-{args.case:02d}'
    if args.attempt>1:name+=f'-attempt-{args.attempt:02d}'
    folder=TRIAL_ROOT/name
    folder.mkdir(exist_ok=False)
    frozen={'version':1,'phase':args.phase,'public_case_number':args.case,'attempt':args.attempt,'retry_of':retry,
        'inputs':{name:sha(APP/name) for name in FILES},'generated_tokens_requested':0,'audio_used':False,
        'limits':{'capture_bytes_each':1024**2,'trial_evidence_bytes':CAP,'native_wall_seconds':120,
                  'native_memory_stop_bytes':6*1024**3,'minimum_available_ram_before_model':8*1024**3},
        'claims':'No proposal generation, native musical acceptance, installed-mode approval or song processing.'}
    save(folder/'frozen.json',frozen)
    for name in FILES:
        target=folder/'code'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((APP/name).read_bytes())
    operations=['help','version','tokenize-help'] if args.phase=='metadata' else ['count']
    for op in operations:
        check_space();directory=folder/op;directory.mkdir()
        request={'operation':op,'work_dir':str(directory.resolve())}
        if op=='count':
            data=json.loads((APP/'audio/research/arranger_benchmark_v1.json').read_text(encoding='utf-8'))
            # Expectations never enter the worker or its prompt.
            raw=canonical(data['cases'][args.case-1]['input']);(directory/'context.json').write_bytes(raw)
            request['context_sha256']=hashlib.sha256(raw).hexdigest()
        save(directory/'request.json',request)
        began=time.monotonic()
        def progress(event):print(json.dumps({'operation':op,**event}),flush=True)
        try:
            result=run_worker('audio.local_arranger_trial',request,progress=progress,
                              wall_seconds=150 if op=='count' else 60,memory_bytes=512*1024**2)
            save(directory/'supervisor.json',result)
            receipt=result['result'];path=directory/receipt['receipt']
            if path.stat().st_size!=receipt['bytes'] or sha(path)!=receipt['sha256']:raise ValueError('Probe receipt failed verification')
            outcome=json.loads(path.read_text(encoding='utf-8'))
            print(json.dumps({'operation':op,'state':outcome['state'],'elapsed_seconds':time.monotonic()-began,
                              'native_metrics':outcome.get('native_metrics'),'prompt_tokens':outcome.get('prompt_tokens')}),flush=True)
            if outcome['state']=='failed':raise ValueError('Inspect the preserved runtime failure; do not retry automatically')
        except BaseException as error:
            if not (directory/'supervisor-error.json').exists():save(directory/'supervisor-error.json',{'error':repr(error),'elapsed_seconds':time.monotonic()-began})
            raise
        if any(sha(APP/name)!=digest for name,digest in frozen['inputs'].items()):raise ValueError('Source changed during probe; retain this attempt')
        if size(TRIAL_ROOT)>CAP:raise ValueError('Runtime trial evidence cap exceeded')
    save(folder/'complete.json',{'state':'complete','phase':args.phase,'trial_bytes':size(TRIAL_ROOT),
                               'source_hashes_unchanged':True,'generated_tokens_requested':0})


if __name__=='__main__':main()
