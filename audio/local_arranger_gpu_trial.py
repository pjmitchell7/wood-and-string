"""Fixed private Vulkan probes and a separately admitted symbolic forward trial.

No app endpoint, supplied songs, playback or downloads.
The CPU experiment stays unchanged; this uses its own reviewed resource profile.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .arranger_prompt import canonical
from .full_decode import _protected_reader
from .gpu_memory import NvidiaMemory, EXPECTED_NAME
from .gpu_trial_monitor import ResourceMonitor, PROFILE
from .local_arranger_trial import available_ram, _context, digest, write_json, parse_prompt_count
from .native_trial_process import run_native, NativeTrialError

APP=Path(__file__).resolve().parents[1]
TRIAL_ROOT=APP/'work/local-arranger-gpu-trial-001'
DEVICE_RESULT=TRIAL_ROOT/'metadata-reviewed/devices.result.json'
VERSION='local-arranger-vulkan-load-count-v1'
OPERATIONS={'help','version','devices','count'}


def runtime_guard():
    # Import only in the managed worker; missing acquisition/guard fails closed.
    from .local_arranger_gpu_runtime import protect_runtime
    return protect_runtime()


def validate_request(request,trial_root=None,allow_generation=False):
    if type(request) is not dict or not {'operation','work_dir'}<=request.keys() or request.keys()-{
            'operation','work_dir','context_sha256','device_receipt_sha256'}:
        raise ValueError('Expected fixed private GPU probe request')
    op=request['operation']
    operations=OPERATIONS|{'generate'} if allow_generation else OPERATIONS
    if type(op) is not str or op not in operations:raise ValueError('Unsupported GPU probe operation')
    if type(request['work_dir']) is not str:raise ValueError('Expected owned GPU probe directory')
    original=Path(request['work_dir'])
    if not original.is_absolute() or not original.is_dir():raise ValueError('Expected existing absolute GPU probe directory')
    base=TRIAL_ROOT if trial_root is None else trial_root
    directory=original.resolve();root=base.resolve()
    if directory==root or not directory.is_relative_to(root):raise ValueError('GPU probe directory escapes fixed trial')
    for path in (original,*original.parents):
        if path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400:
            raise ValueError('Linked GPU trial directory is unsupported')
        if path==base:break
    for key in ('context_sha256','device_receipt_sha256'):
        if op in ('count','generate'):
            if type(request.get(key)) is not str or not re.fullmatch('[0-9a-f]{64}',request[key]):
                raise ValueError('Expected exact frozen '+key)
        elif key in request:raise ValueError('Metadata probe has unexpected model inputs')
    return op,directory


def parse_devices(raw):
    if type(raw) is not bytes or len(raw)>1024**2:raise ValueError('Invalid bounded device listing')
    text=raw.decode('utf-8',errors='strict')
    if text.count('Available devices:')!=1:raise ValueError('Missing or ambiguous native device-list heading')
    rows=re.findall(r'(?m)^  (Vulkan[0-9]+): (.+) \(([0-9]+) MiB, ([0-9]+) MiB free\)\s*$',text)
    candidates=[r for r in rows if r[1]==EXPECTED_NAME]
    if len(candidates)!=1:raise ValueError('Expected exactly one explicitly named NVIDIA Vulkan device')
    device,name,total,free=candidates[0]
    if not 0<int(total)<=128*1024 or not 0<=int(free)<=int(total):raise ValueError('Unexpected NVIDIA device-list memory')
    if len({r[0] for r in rows})!=len(rows):raise ValueError('Duplicate Vulkan device names')
    return {'device':device,'name':name,'listed_total_mib':int(total),'listed_free_mib':int(free),
            'memory_scope':'Vulkan device-local heap total and estimated budget available; not physical VRAM or admission.',
            'binding':'Exact native Vulkan device name and NVIDIA description; NVML index is separate.'}


def device_evidence(request,runtime,stack,device_result=None):
    target=DEVICE_RESULT if device_result is None else device_result
    stream=stack.enter_context(_protected_reader(target));raw=stream.read(256*1024+1)
    if len(raw)>256*1024 or hashlib.sha256(raw).hexdigest()!=request['device_receipt_sha256']:
        raise ValueError('Device metadata receipt changed')
    result=json.loads(raw)
    if (result.get('state')!='checked' or result.get('operation')!='devices'
            or result.get('runtime_identity_sha256')!=runtime['identity_sha256']):
        raise ValueError('Device listing does not bind this verified Vulkan runtime')
    path=target.parent/'devices.stdout'
    capture=stack.enter_context(_protected_reader(path));raw=capture.read(1024**2+1)
    expected=result.get('captures',{}).get('devices.stdout',{})
    if len(raw)!=expected.get('bytes') or hashlib.sha256(raw).hexdigest()!=expected.get('sha256'):
        raise ValueError('Actual device listing capture changed')
    selected=parse_devices(raw)
    if selected!=result.get('selected_device'):raise ValueError('Device selection differs from native listing')
    return selected


def count_argv(runtime,directory,device,template=None,generation_tokens=0):
    # Frozen application vector; no caller flags, automatic fitting or layer retry.
    argv=[runtime['executable'],'--model',runtime['model'],'--offline',
        '--device',device,'--split-mode','none','--gpu-layers','all','--fit','off',
        '--threads','4','--threads-batch','4','--ctx-size','4096','--n-predict',str(generation_tokens),
        '--batch-size','256','--ubatch-size','128','--cache-type-k','f16','--cache-type-v','f16',
        '--no-context-shift','--jinja','--conversation','--single-turn','--reasoning','off',
        '--reasoning-budget','0','--json-schema-file',str(directory/'proposal.schema.json'),
        '--file',str(directory/'prompt.txt'),'--seed','1015','--temp','0.7','--top-p','0.8',
        '--top-k','20','--min-p','0','--presence-penalty','1.5','--simple-io',
        '--no-display-prompt','--no-warmup','--verbose-prompt']
    if template is not None:
        argv[argv.index('--file')]='--binary-file'
        argv.extend(['--no-escape','--chat-template-file',str(template),'--verbosity','4',
            '--no-log-prefix','--no-log-timestamps','--log-colors','off','--no-log-jsonl'])
        if generation_tokens:argv[argv.index('--verbosity')+1]='5'
    return argv


def offload_evidence(raw,selected):
    text=raw.decode('utf-8',errors='strict')
    devices=re.findall(r'(?m)^llama_prepare_model_devices: using device (Vulkan[0-9]+) \((.+)\) \(([^\r\n]*)\) - ([0-9]+) MiB free\s*$',text)
    if (len(devices)!=1 or devices[0][0]!=selected['device'] or devices[0][1]!=EXPECTED_NAME
            or not devices[0][2] or not 0<=int(devices[0][3])<=128*1024):
        raise ValueError('Actual count-process GPU selection differs from the frozen NVIDIA device')
    rows=re.findall(r'(?m)^load_tensors: offloaded ([0-9]+)/([0-9]+) layers to GPU\s*$',text)
    if len(rows)!=1 or int(rows[0][0])!=int(rows[0][1]) or not 1<int(rows[0][0])<1000:
        raise ValueError('Actual logs do not establish complete requested layer offload')
    if 'no usable GPU found' in text or 'option will be ignored' in text:
        raise ValueError('Actual logs report an unusable GPU or ignored offload')
    return {'offloaded_layers':int(rows[0][0]),'total_layers':int(rows[0][1]),
            'current_device':devices[0][0],'current_description':devices[0][1],
            'current_device_id':devices[0][2],'vulkan_budget_available_mib_at_selection':int(devices[0][3]),
            'claim':'These initialization log lines establish requested layer offload, not forward performance or musical quality.'}


def run_probe(request,emit,*,trial_root=None,device_result=None,version=None,transport_template=None,
              generation_tokens=0,generation_preflight=None):
    """Internal shared worker; config is supplied only by fixed owned modules."""
    if type(generation_tokens) is not int or generation_tokens not in (0,1024):raise ValueError('Unsupported internal generation allowance')
    generating=generation_tokens==1024
    op,directory=validate_request(request,trial_root=trial_root,allow_generation=generating)
    if (op=='generate')!=generating:raise ValueError('Generation requires the separate fixed trial worker')
    model_probe=op in ('count','generate')
    if generating and (transport_template is None or not callable(generation_preflight)):
        raise ValueError('Generation requires an exact audited transport and prior-count preflight')
    if transport_template is not None and (not model_probe or type(transport_template) is not bytes or not 0<len(transport_template)<=1024):
        raise ValueError('Expected bounded application-owned count transport')
    paths=[directory/(op+suffix) for suffix in ('.result.json','.attempt.json','.stdout','.stderr')]
    if model_probe:paths.extend(directory/name for name in ('prompt.txt','proposal.schema.json','prompt-receipt.json'))
    if transport_template is not None:paths.append(directory/'chat-template.jinja')
    if generating:paths.extend(directory/name for name in ('completion.raw','proposal.json','resolved.json'))
    if any(p.exists() or p.is_symlink() for p in paths):raise FileExistsError('Preserve occupied GPU probe evidence')
    started=time.monotonic();result={'version':VERSION if version is None else version,'operation':op,'state':'started',
        'model_generated_tokens_requested':generation_tokens,'generation_gate_released':False,
        'one_controlled_generation_trial':generating,'profile':dict(PROFILE)}
    write_json(directory/(op+'.attempt.json'),result)
    monitor=None
    try:
        with ExitStack() as evidence:
            artifact=None
            if model_probe:
                stream=evidence.enter_context(_protected_reader(directory/'context.json'))
                artifact=_context(request,directory,stream)
                stream.seek(0);source_context=json.loads(stream.read(256*1024+1))
                with (directory/'prompt.txt').open('xb') as output:output.write(artifact['prompt'].encode('utf-8'))
                write_json(directory/'proposal.schema.json',artifact['schema'])
                write_json(directory/'prompt-receipt.json',{k:v for k,v in artifact.items() if k not in ('prompt','schema')})
                if transport_template is not None:
                    with (directory/'chat-template.jinja').open('xb') as output:output.write(transport_template)
                result['input_files']={}
                for name,key in [('context.json','context_sha256'),('prompt.txt','prompt_sha256'),('proposal.schema.json','schema_sha256')]:
                    handle=stream if name=='context.json' else evidence.enter_context(_protected_reader(directory/name))
                    handle.seek(0);actual=hashlib.file_digest(handle,'sha256').hexdigest()
                    if actual!=artifact[key]:raise ValueError('Protected symbolic GPU input changed')
                    result['input_files'][name]={'bytes':os.fstat(handle.fileno()).st_size,'sha256':actual}
                if transport_template is not None:
                    handle=evidence.enter_context(_protected_reader(directory/'chat-template.jinja'))
                    raw=handle.read(1025)
                    if raw!=transport_template:raise ValueError('Protected transport template changed')
                    result['input_files']['chat-template.jinja']={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
            emit({'phase':'verifying portable GPU package and existing model','progress':0})
            with runtime_guard() as runtime:
                result['runtime_identity_sha256']=runtime['identity_sha256']
                if model_probe:
                    selected=device_evidence(request,runtime,evidence,device_result=device_result);result['selected_device']=selected
                    if generating:result['prior_count_admission']=generation_preflight(runtime,artifact,transport_template)
                    gpu=evidence.enter_context(NvidiaMemory())
                    monitor=ResourceMonitor(gpu,available_ram);monitor.admit()
                    argv=count_argv(runtime,directory,selected['device'],directory/'chat-template.jinja' if transport_template is not None else None,generation_tokens=generation_tokens)
                else:argv=[runtime['executable'],'--list-devices' if op=='devices' else '--'+op]
                result['argv']=argv
                emit({'phase':'generating first bounded symbolic proposal' if generating else 'loading model for zero-generation GPU check' if model_probe else 'checking GPU runtime metadata','progress':.1})
                try:
                    metrics=run_native(argv,cwd=directory,stdout_path=directory/(op+'.stdout'),stderr_path=directory/(op+'.stderr'),
                        wall_seconds=PROFILE['native_wall_seconds'] if monitor else 20,
                        memory_bytes=PROFILE['native_working_set_stop_bytes'] if monitor else 512*1024**2,
                        cancel=monitor.cancel if monitor else lambda:False,
                        before_resume=monitor.before_resume if monitor else None)
                except NativeTrialError as error:
                    result['native_metrics']=error.metrics;result['native_error_reason']=error.reason;raise
                result['native_metrics']=metrics
                if monitor:monitor._sample()
                if transport_template is not None:
                    from .qwen_prompt_audit import audit_capture
                    with _protected_reader(Path(runtime['model'])) as model_stream:
                        audit=audit_capture((directory/(op+'.stderr')).read_bytes(),model_stream,artifact['prompt'].encode('utf-8'))
                    result['prompt_audit']=audit
                    if (audit.get('entire_single_user_template_matches') is not True
                            or audit.get('reasoning_template_state')!='empty_think_block_prefilled'):
                        raise ValueError('Actual tokenized prompt did not preserve exact evidence and explicit nonthinking transport')
            if model_probe:
                raw=(directory/(op+'.stderr')).read_bytes();tokens=parse_prompt_count(raw)
                if 'prompt_audit' in result and result['prompt_audit'].get('count')!=tokens:
                    raise ValueError('Native count and full token-piece audit disagree')
                result.update(prompt_tokens=tokens,context_tokens=4096,required_output_reserve=1024,
                    fits_prompt_budget=tokens<=3072,offload=offload_evidence(raw,selected),
                    context_sha256=artifact['context_sha256'],prompt_sha256=artifact['prompt_sha256'],schema_sha256=artifact['schema_sha256'],
                    actual_reasoning_prompt_review=('Exact user bytes and nonthinking assistant prefix verified from actual token pieces; generation still gated.'
                        if 'prompt_audit' in result else 'Pending inspection of actual verbose tokenized prompt; no generation gate released.'))
                result['state']='counted' if tokens<=3072 else 'rejected_prompt_budget'
                if generating:
                    if tokens>3072:raise ValueError('Generation process violated prior prompt budget')
                    from .arranger_completion import extract_completion,verify_eog
                    from .arranger_prompt import parse_response
                    result['termination']=verify_eog(raw,prompt_count=tokens,token_budget=generation_tokens)
                    completion=extract_completion((directory/'generate.stdout').read_bytes())
                    with (directory/'completion.raw').open('xb') as output:output.write(completion)
                    proposal,resolved=parse_response(source_context,completion)
                    write_json(directory/'proposal.json',proposal);write_json(directory/'resolved.json',resolved)
                    result.update(state='proposed',normal_eog_trailer_observed=True,
                        proposal_status=proposal['status'],resolved_note_count=len(resolved['notes']),
                        completion_bytes=len(completion),completion_sha256=hashlib.sha256(completion).hexdigest(),
                        musical_quality_assessed=False)
            else:
                if op=='devices':result['selected_device']=parse_devices((directory/'devices.stdout').read_bytes())
                result['state']='checked'
    except Exception as error:result.update(state='failed',error=type(error).__name__+': '+str(error))
    finally:
        if monitor:result['resources']=monitor.receipt()
    result['elapsed_seconds']=time.monotonic()-started
    result['captures']={p.name:{'bytes':p.stat().st_size,'sha256':digest(p)} for p in (directory/(op+'.stdout'),directory/(op+'.stderr')) if p.is_file()}
    if generating:
        result['output_files']={p.name:{'bytes':p.stat().st_size,'sha256':digest(p)} for p in
            (directory/'completion.raw',directory/'proposal.json',directory/'resolved.json') if p.is_file()}
    path=directory/(op+'.result.json');write_json(path,result)
    return {'state':result['state'],'receipt':path.name,'bytes':path.stat().st_size,'sha256':digest(path)}


def run(request,emit):
    return run_probe(request,emit)
