"""Separately frozen case1 transport correction; exactly one native attempt."""
import json
import time

from audio.arranger_prompt import canonical
from audio.gpu_trial_monitor import PROFILE
from audio.local_arranger_gpu_trial import DEVICE_RESULT
from audio.local_arranger_gpu_trial_v2 import TRIAL_ROOT,VERSION
from audio.managed_process import run_worker
from audio.qwen_prompt_audit import NONTHINK_TEMPLATE
from audio.research.probe_local_arranger_gpu import APP,FILES,CAP,sha,size,save,check_space,cache_snapshot

SOURCES=[*FILES,'audio/qwen_prompt_audit.py','audio/local_arranger_gpu_trial_v2.py',
    'audio/research/probe_local_arranger_gpu_v2.py']


def main():
    # The input and all semantic proposal/evaluation rules are unchanged. Only
    # file transport, explicit nonthinking template and diagnostic logging change.
    prior=APP/'work/local-arranger-gpu-trial-001/development-01/count'
    old=json.loads((prior/'count.result.json').read_text(encoding='utf-8'))
    receipt=json.loads((prior/'supervisor.json').read_text(encoding='utf-8'))['result']
    if (sha(prior/'count.result.json')!=receipt['sha256'] or (prior/'count.result.json').stat().st_size!=receipt['bytes']
            or old.get('state')!='failed' or old.get('error')!='ValueError: Missing or ambiguous exact templated token count'
            or old.get('native_metrics',{}).get('returncode')!=0 or old['native_metrics'].get('owned_job_empty') is not True):
        raise ValueError('Preserve and reconcile the first actual native count before this separate correction')
    for name,row in old['captures'].items():
        if sha(prior/name)!=row['sha256'] or (prior/name).stat().st_size!=row['bytes']:
            raise ValueError('First native capture changed')
    reviewed=json.loads((DEVICE_RESULT.parent/'complete.json').read_text(encoding='utf-8'))
    if sha(DEVICE_RESULT)!=reviewed['device_result_sha256'] or DEVICE_RESULT.stat().st_size!=reviewed['device_result_bytes']:
        raise ValueError('Verified NVIDIA device metadata changed')
    resources=check_space()
    if size(TRIAL_ROOT)+size(APP/'work/local-arranger-gpu-trial-001')+3*1024**2>CAP:
        raise ValueError('Combined GPU evidence allowance exhausted')
    before=cache_snapshot();TRIAL_ROOT.mkdir(exist_ok=False)
    directory=TRIAL_ROOT/'development-01';directory.mkdir()
    context=json.loads((APP/'audio/research/arranger_benchmark_v1.json').read_text(encoding='utf-8'))['cases'][0]['input']
    raw=canonical(context)
    if raw!=(prior/'context.json').read_bytes():raise ValueError('Public case1 semantic evidence changed')
    (directory/'context.json').write_bytes(raw)
    from hashlib import sha256
    request={'operation':'count','work_dir':str(directory.resolve()),'context_sha256':sha256(raw).hexdigest(),
        'device_receipt_sha256':sha(DEVICE_RESULT)}
    save(directory/'request.json',request)
    save(TRIAL_ROOT/'frozen.json',{'version':VERSION,'profile':PROFILE,'inputs':{name:sha(APP/name) for name in SOURCES},
        'resources_before':resources,'shared_cache_before':before,
        'template_sha256':sha256(NONTHINK_TEMPLATE).hexdigest(),'template_bytes':len(NONTHINK_TEMPLATE),
        'previous_count_result_sha256':sha(prior/'count.result.json'),
        'context_sha256':request['context_sha256'],'generated_tokens_requested':0,'generation_gate_released':False,
        'changes':['--binary-file --no-escape preserves exact prompt bytes',
            'protected tiny Qwen/ChatML wrapper pre-fills empty thinking block',
            'explicit trace4/plain log configuration exposes required backend records'],
        'claims':'Zero-generation initialization only; no actual forward kernels, proposal correctness or music-quality acceptance.'})
    for name in SOURCES:
        target=TRIAL_ROOT/'code'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((APP/name).read_bytes())
    began=time.monotonic()
    try:
        result=run_worker('audio.local_arranger_gpu_trial_v2',request,progress=lambda event:print(json.dumps(event),flush=True),
            wall_seconds=150,memory_bytes=512*1024**2)
        save(directory/'supervisor.json',result);receipt=result['result'];path=directory/receipt['receipt']
        if path.stat().st_size!=receipt['bytes'] or sha(path)!=receipt['sha256']:raise ValueError('Corrected count receipt changed')
        outcome=json.loads(path.read_text(encoding='utf-8'))
        print(json.dumps({'state':outcome['state'],'error':outcome.get('error'),'native_metrics':outcome.get('native_metrics'),
            'prompt_tokens':outcome.get('prompt_tokens'),'prompt_audit':outcome.get('prompt_audit'),'offload':outcome.get('offload')}),flush=True)
        if outcome['state']!='counted':raise ValueError('Preserve corrected attempt failure; no automatic retry')
        frozen=json.loads((TRIAL_ROOT/'frozen.json').read_text(encoding='utf-8'))
        if any(sha(APP/name)!=digest for name,digest in frozen['inputs'].items()):raise ValueError('Source changed during corrected trial')
        if size(TRIAL_ROOT)+size(APP/'work/local-arranger-gpu-trial-001')>CAP:raise ValueError('Combined GPU evidence cap exceeded')
    except BaseException as error:
        save(TRIAL_ROOT/'supervisor-error.json',{'error':repr(error),'elapsed_seconds':time.monotonic()-began});raise
    finally:
        after=cache_snapshot()
        baseline=json.loads((APP/'work/local-arranger-gpu-trial-001/metadata/frozen.json').read_text(encoding='utf-8'))['shared_cache_before']
        growth=sum(max(0,row['bytes']-baseline[path]['bytes']) for path,row in after.items())
        save(TRIAL_ROOT/'shared-cache.json',{'before':before,'after':after,'positive_growth_from_first_metadata_bytes':growth,
            'scope':'Shared known-cache net snapshots, not peak writes or process attribution; charged inside64MiB GPU scratch.'})
        if growth>64*1024**2:raise ValueError('Shared cache growth exceeded GPU scratch reservation; no further trial')
    save(TRIAL_ROOT/'complete.json',{'state':'counted','source_hashes_unchanged':True,'generation_gate_released':False})


if __name__=='__main__':main()
