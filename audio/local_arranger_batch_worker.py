"""Fixed public-case 2..8 wrapper; no controller, retry, tuning or CLI.

Only a managed owned worker may enter run(). The controller owns fresh stage
markers and source locks. This wrapper holds its exact evidence through the
unchanged shared worker's protected runtime and native cleanup boundary.
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat

from . import local_arranger_gpu_trial as gpu
from .arranger_prompt import canonical
from .full_decode import _protected_reader
from .gpu_trial_monitor import PROFILE
from .local_arranger_batch_plan import (VERSION, PUBLIC_SHA, PUBLIC_IDS, RUNTIME_ID,
    FORWARD_FREEZE_SHA, BASELINE_FREEZE_SHA, input_rows, strict_json,
    authenticate_count, validate_count)
from .local_arranger_trial import parse_prompt_count
from .native_trial_process import _assert_owned_worker
from .qwen_prompt_audit import NONTHINK_TEMPLATE, NONTHINK_TEMPLATE_SHA256

APP=Path(__file__).resolve().parents[1]
TRIAL_ROOT=APP/'work/local-arranger-public8-001'
DEVICE_RESULT=gpu.DEVICE_RESULT
HASH=re.compile('[0-9a-f]{64}')
INPUT_NAMES={'context.json','prompt.txt','proposal.schema.json','chat-template.jinja'}
AUDIT_IDENTITY=('count','token_ids_u32le_sha256','reconstructed_sha256','reconstructed_bytes',
    'exact_prompt_sha256','exact_prompt_bytes','reasoning_template_state',
    'entire_single_user_template_matches','metadata_sha256','metadata_bytes','required_model_sha256')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _plain(path):
    path=Path(path)
    if not path.is_absolute():raise ValueError('Expected absolute fixed evidence path')
    for parent in (path,*path.parents):
        info=parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Linked batch evidence is unsupported')
    return path


def _read(stack,path,limit,expected_sha=None):
    stream=stack.enter_context(_protected_reader(_plain(path)))
    info=os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or not 0<=info.st_size<=limit:
        raise ValueError('Expected bounded regular batch evidence')
    raw=stream.read(limit+1)
    if len(raw)!=info.st_size or (expected_sha is not None and _hash(raw)!=expected_sha):
        raise ValueError('Protected batch evidence size/hash differs')
    return raw


def validate_request(request):
    base={'operation','ordinal','context_sha256','device_receipt_sha256','freeze_sha256'}
    if type(request) is not dict or request.get('operation') not in ('count','generate'):
        raise ValueError('Only fixed public count/generate operations are supported')
    required=base|({'count_supervisor_sha256','capture_manifest_sha256'} if request['operation']=='generate' else set())
    if set(request)!=required:raise ValueError('Unexpected batch request fields')
    ordinal=request['ordinal']
    if type(ordinal) is not int or not 2<=ordinal<=8:raise ValueError('Only public ordinals 2..8 may run')
    for key in required-{'operation','ordinal'}:
        if type(request[key]) is not str or HASH.fullmatch(request[key]) is None:
            raise ValueError('Expected exact '+key)
    folder=TRIAL_ROOT/f'development-{ordinal:02d}'/request['operation']
    delegated={'operation':request['operation'],'work_dir':str(folder),
               'context_sha256':request['context_sha256'],'device_receipt_sha256':request['device_receipt_sha256']}
    # Existing path/ownership/reparse checks remain authoritative as well.
    gpu.validate_request(delegated,trial_root=TRIAL_ROOT,allow_generation=request['operation']=='generate')
    return folder,delegated


def _case(frozen,request):
    plan=frozen.get('plan',{})
    if (frozen.get('version')!=VERSION or plan.get('version')!=VERSION
        or frozen.get('device_receipt_sha256')!=request['device_receipt_sha256']
        or plan.get('public_benchmark_sha256')!=PUBLIC_SHA
        or plan.get('first_forward_freeze_sha256')!=FORWARD_FREEZE_SHA
        or plan.get('baseline_freeze_sha256')!=BASELINE_FREEZE_SHA
        or plan.get('runtime_identity_sha256')!=RUNTIME_ID or plan.get('gpu_profile')!=PROFILE):
        raise ValueError('Frozen public protocol/runtime/device identity differs')
    rows=plan.get('cases')
    if type(rows) is not list or len(rows)!=8:raise ValueError('Require exactly eight frozen public identities')
    for ordinal,row in enumerate(rows,1):
        if (type(row) is not dict or set(row)!={'ordinal','case_id','input_files','action','context_tokens',
                'prompt_token_limit','output_token_limit','token_count'}
            or row.get('ordinal')!=ordinal or type(row.get('ordinal')) is not int
            or row.get('case_id')!=PUBLIC_IDS[ordinal-1]):raise ValueError('Public case order/identity differs')
    row=rows[request['ordinal']-1]
    if (row.get('action')!='count_then_at_most_one_generation' or row.get('context_tokens')!=4096
        or row.get('prompt_token_limit')!=3072 or row.get('output_token_limit')!=1024
        or row.get('token_count') is not None):raise ValueError('Public case budgets/action differ')
    expected=row.get('input_files')
    if type(expected) is not dict or set(expected)!=INPUT_NAMES:raise ValueError('Exact four input identities required')
    for value in expected.values():
        if (type(value) is not dict or set(value)!={'bytes','sha256'} or type(value['bytes']) is not int
            or not 0<value['bytes']<=256*1024 or type(value['sha256']) is not str
            or HASH.fullmatch(value['sha256']) is None):raise ValueError('Invalid frozen input identity')
    if expected['context.json']['sha256']!=request['context_sha256']:raise ValueError('Wrong context for public ordinal')
    return row


def _preflight(stack,request,case,delegated,runtime,artifact,template):
    """Called inside unchanged run_probe's full protected runtime/model guard."""
    from .capture_archive import read_capture
    from .qwen_prompt_audit import audit_capture
    if runtime.get('identity_sha256')!=RUNTIME_ID:raise ValueError('Count/runtime identity differs')
    if template!=NONTHINK_TEMPLATE or _hash(template)!=NONTHINK_TEMPLATE_SHA256:
        raise ValueError('Unchanged nonthinking template required')
    folder=TRIAL_ROOT/f"development-{request['ordinal']:02d}"/'count'
    supervisor_raw=_read(stack,folder/'supervisor.json',65536,request['count_supervisor_sha256'])
    supervisor=strict_json(supervisor_raw,65536)
    raw=_read(stack,folder/'count.result.json',65536)
    prior=authenticate_count(raw,supervisor.get('result'))
    if supervisor['result'].get('state')!='counted':raise ValueError('Prior managed count did not finish counted')
    expected=case['input_files']
    generated={'context.json':artifact['context_sha256'],'prompt.txt':artifact['prompt_sha256'],
               'proposal.schema.json':artifact['schema_sha256'],'chat-template.jinja':_hash(template)}
    if generated!={name:row['sha256'] for name,row in expected.items()}:
        raise ValueError('Generation prompt/schema differs from the counted case')
    for name,pin in expected.items():
        value=_read(stack,folder/name,256*1024,pin['sha256'])
        if len(value)!=pin['bytes']:raise ValueError('Counted input length differs')
    manifest_raw=_read(stack,folder/'captures.json',65536,request['capture_manifest_sha256'])
    manifest=strict_json(manifest_raw,65536)
    if (type(manifest) is not dict or set(manifest)!={'version','captures'}
        or manifest['version']!='capture-archive-v1' or type(manifest['captures']) is not dict
        or set(manifest['captures'])!={'count.stdout','count.stderr'}):
        raise ValueError('Require exact authenticated archived count captures')
    captures={name:read_capture(stack,folder,name,expected=prior.get('captures',{}).get(name),
        binding=binding,freeze_sha256=request['freeze_sha256']) for name,binding in manifest['captures'].items()}
    selected=gpu.device_evidence(delegated,runtime,stack,device_result=DEVICE_RESULT)
    admitted=validate_count(prior,expected,gpu.count_argv(runtime,folder,selected['device'],folder/'chat-template.jinja'),selected,captures)
    metrics=prior['native_metrics'];resources=prior.get('resources',{})
    if (metrics.get('reason')!='exit' or metrics.get('primary_thread_resumed') is not True
        or metrics.get('primary_thread_handle_closed') is not True
        or type(metrics.get('native_pid')) is not int or metrics['native_pid']<=0
        or metrics.get('cleanup_errors') or resources.get('stop_reason') is not None
        or type(resources.get('fresh_pre_resume_samples')) is not int or resources['fresh_pre_resume_samples']<1
        or metrics.get('stdout_bytes')!=len(captures['count.stdout'])
        or metrics.get('stderr_bytes')!=len(captures['count.stderr'])):
        raise ValueError('Prior count lacks complete normal native lifecycle/capture evidence')
    # Stored assertions alone do not authenticate the complete token stream.
    model=stack.enter_context(_protected_reader(Path(runtime['model'])))
    audited=audit_capture(captures['count.stderr'],model,artifact['prompt'].encode('utf-8'))
    if (audited!=prior['prompt_audit'] or parse_prompt_count(captures['count.stderr'])!=admitted['prompt_tokens']
        or gpu.offload_evidence(captures['count.stderr'],selected)!=prior.get('offload')):
        raise ValueError('Recomputed count token/device evidence differs')
    return {**admitted,'prior_count_result_sha256':_hash(raw),
            'count_supervisor_sha256':request['count_supervisor_sha256'],
            'capture_manifest_sha256':request['capture_manifest_sha256'],
            'freeze_sha256':request['freeze_sha256'],'public_ordinal':request['ordinal'],
            'case_id':case['case_id'],'archived_captures_reaudited':True,
            'prompt_audit_identity':{key:audited[key] for key in AUDIT_IDENTITY},
            'scope':'Unchanged public symbolic case only; no private holdout, audio or musical-quality judgment.'}


def run(request,emit):
    _assert_owned_worker()
    folder,delegated=validate_request(request)
    with ExitStack() as stack:
        raw=_read(stack,TRIAL_ROOT/'frozen.json',256*1024,request['freeze_sha256'])
        frozen=strict_json(raw,256*1024);case=_case(frozen,request)
        context_raw=_read(stack,folder/'context.json',256*1024,request['context_sha256'])
        context=strict_json(context_raw,256*1024)
        if (type(context) is not dict or context.get('case_id')!=case['case_id']
            or canonical(context)!=context_raw or input_rows(context)!=case['input_files']):
            raise ValueError('Stage context is not the exact unchanged public input')
        generating=request['operation']=='generate'
        admitted=[]
        def preflight(runtime,artifact,template):
            value=_preflight(stack,request,case,delegated,runtime,artifact,template)
            admitted.append(value)
            return value
        returned=gpu.run_probe(delegated,emit,trial_root=TRIAL_ROOT,device_result=DEVICE_RESULT,
            version=VERSION,transport_template=NONTHINK_TEMPLATE,generation_tokens=1024 if generating else 0,
            generation_preflight=preflight if generating else None)
        if (type(returned) is not dict or returned.get('receipt')!=request['operation']+'.result.json'
            or type(returned.get('bytes')) is not int or not 0<returned['bytes']<=65536
            or type(returned.get('sha256')) is not str or HASH.fullmatch(returned['sha256']) is None):
            raise ValueError('Shared worker returned an invalid bounded receipt binding')
        result_raw=_read(stack,folder/returned['receipt'],65536,returned['sha256'])
        result=strict_json(result_raw,65536)
        if (len(result_raw)!=returned['bytes'] or result.get('version')!=VERSION
            or result.get('operation')!=request['operation'] or result.get('state')!=returned.get('state')):
            raise ValueError('Shared worker result identity differs')
        if generating and result['state']=='proposed':
            if (len(admitted)!=1 or result.get('prior_count_admission')!=admitted[0]
                or result.get('prompt_tokens')!=admitted[0]['prompt_tokens']
                or {key:result.get('prompt_audit',{}).get(key) for key in AUDIT_IDENTITY}!=admitted[0]['prompt_audit_identity']):
                raise ValueError('Generated prompt differs from the authenticated complete count')
        return returned
