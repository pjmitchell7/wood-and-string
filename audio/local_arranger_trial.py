"""Fixed private runtime probes inside an already-owned managed worker.

No public API, executable selection, network acquisition, inference or rendering.
Count uses zero generated tokens and the same bounded completion configuration
planned for the first symbolic trial. Native output is retained, never executed.
"""
import ctypes
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .arranger_prompt import build_prompt, canonical
from .native_trial_process import run_native, NativeTrialError
from .local_arranger_runtime import protect_runtime
from .full_decode import _protected_reader

APP=Path(__file__).resolve().parents[1]
TRIAL_ROOT=APP/'work/local-arranger-trial-001'
VERSION='local-arranger-runtime-probe-v1'
OPERATIONS={'help','version','tokenize-help','count'}
MAX_CONTEXT_BYTES=256*1024


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def available_ram():
    if os.name!='nt':raise RuntimeError('Local runtime trial requires Windows')
    class Memory(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(name,ctypes.c_ulonglong) for name in
            ('total_phys','avail_phys','total_page','avail_page','total_virtual','avail_virtual','avail_extended')]
    value=Memory();value.length=ctypes.sizeof(value)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes=[ctypes.POINTER(Memory)];kernel.GlobalMemoryStatusEx.restype=ctypes.c_int
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(value)):raise ctypes.WinError(ctypes.get_last_error())
    return value.avail_phys


def validate_request(request):
    if type(request) is not dict or not {'operation','work_dir'}<=request.keys() or request.keys()-{'operation','work_dir','context_sha256'}:
        raise ValueError('Expected fixed local runtime probe request')
    op=request['operation']
    if type(op) is not str or op not in OPERATIONS:raise ValueError('Unsupported local runtime probe')
    if type(request['work_dir']) is not str:raise ValueError('Expected owned probe directory')
    directory=Path(request['work_dir'])
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():raise ValueError('Expected existing owned probe directory')
    original=directory
    root=TRIAL_ROOT.resolve();directory=directory.resolve()
    if directory==root or not directory.is_relative_to(root):raise ValueError('Probe directory escapes fixed private trial')
    for path in (original,*original.parents):
        if path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400:
            raise ValueError('Linked trial directories are unsupported')
        if path==TRIAL_ROOT:break
    if op=='count':
        if type(request.get('context_sha256')) is not str or not re.fullmatch('[0-9a-f]{64}',request['context_sha256']):
            raise ValueError('Expected exact symbolic context hash')
    elif 'context_sha256' in request:raise ValueError('Unexpected context for metadata probe')
    return op,directory


def count_argv(runtime,directory):
    """Application-owned CPU/offline vector; never accept request flags."""
    return [runtime['executable'],'--model',runtime['model'],'--offline','--gpu-layers','0',
        '--threads','4','--threads-batch','4','--ctx-size','4096','--n-predict','0',
        '--batch-size','256','--ubatch-size','128','--cache-type-k','f16','--cache-type-v','f16',
        '--no-context-shift','--jinja','--conversation','--single-turn','--reasoning','off',
        '--reasoning-budget','0','--json-schema-file',str(directory/'proposal.schema.json'),
        '--file',str(directory/'prompt.txt'),'--seed','1015','--temp','0.7','--top-p','0.8',
        '--top-k','20','--min-p','0','--presence-penalty','1.5','--simple-io',
        '--no-display-prompt','--no-warmup','--verbose-prompt']


def parse_prompt_count(raw):
    if type(raw) is not bytes or len(raw)>1024**2:raise ValueError('Invalid bounded native token diagnostics')
    text=raw.decode('utf-8',errors='strict')
    text=re.sub(r'\x1b\[[0-9;]*m','',text)
    # The pinned implementation uses __func__ inside llama_completion, not main.
    values=re.findall(r'(?m)^llama_completion: number of tokens in prompt = ([0-9]+)\s*$',text)
    if len(values)!=1:raise ValueError('Missing or ambiguous exact templated token count')
    count=int(values[0])
    if count<1:raise ValueError('Empty templated prompt')
    return count


def write_json(path,value):
    with path.open('xb') as stream:
        stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())


def _context(request,directory,stream):
    path=directory/'context.json'
    if path.is_symlink() or not path.is_file() or not 0<path.stat().st_size<=MAX_CONTEXT_BYTES:
        raise ValueError('Expected bounded symbolic evidence file')
    stream.seek(0);raw=stream.read(MAX_CONTEXT_BYTES+1)
    if not 0<len(raw)<=MAX_CONTEXT_BYTES:raise ValueError('Symbolic evidence exceeds bound')
    if hashlib.sha256(raw).hexdigest()!=request['context_sha256']:raise ValueError('Symbolic evidence hash changed')
    def invalid(value):raise ValueError('Invalid symbolic JSON constant')
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError('Duplicate symbolic JSON field')
            value[key]=item
        return value
    context=json.loads(raw,parse_constant=invalid,object_pairs_hook=pairs)
    artifact=build_prompt(context)
    if artifact['context_sha256']!=request['context_sha256']:raise ValueError('Expected canonical exact symbolic evidence')
    return artifact


def run(request,emit):
    op,directory=validate_request(request)
    result_path=directory/(op+'.result.json');attempt_path=directory/(op+'.attempt.json')
    paths=[result_path,attempt_path,directory/(op+'.stdout'),directory/(op+'.stderr')]
    if op=='count':paths.extend([directory/'prompt.txt',directory/'proposal.schema.json',directory/'prompt-receipt.json'])
    if any(path.exists() or path.is_symlink() for path in paths):raise FileExistsError('Preserve occupied local runtime attempt')
    started=time.monotonic();result={'version':VERSION,'operation':op,'state':'started','model_generated_tokens_requested':0}
    write_json(attempt_path,result)
    evidence=ExitStack()
    try:
        artifact=None
        if op=='count':
            context_path=directory/'context.json'
            if context_path.is_symlink():raise ValueError('Linked symbolic evidence is unsupported')
            context_stream=evidence.enter_context(_protected_reader(context_path))
            artifact=_context(request,directory,context_stream)
            with (directory/'prompt.txt').open('xb') as stream:stream.write(artifact['prompt'].encode('utf-8'))
            write_json(directory/'proposal.schema.json',artifact['schema'])
            write_json(directory/'prompt-receipt.json',{k:v for k,v in artifact.items() if k not in ('prompt','schema')})
            result['input_files']={}
            for name,expected in [('context.json',request['context_sha256']),('prompt.txt',artifact['prompt_sha256']),
                                  ('proposal.schema.json',artifact['schema_sha256'])]:
                stream=context_stream if name=='context.json' else evidence.enter_context(_protected_reader(directory/name))
                stream.seek(0);actual=hashlib.file_digest(stream,'sha256').hexdigest()
                if actual!=expected:raise ValueError('Protected symbolic input differs from frozen artifact')
                result['input_files'][name]={'bytes':os.fstat(stream.fileno()).st_size,'sha256':actual}
            free=available_ram();result['available_ram_before_native']=free
            if free<8*1024**3:raise ValueError('At least 8 GiB available RAM required before model loading')
        emit({'phase':'verifying local arranger package','progress':0})
        with protect_runtime() as runtime:
            result['runtime_identity_sha256']=runtime['identity_sha256']
            if op=='count':
                free=available_ram();result['available_ram_immediately_before_native']=free
                if free<8*1024**3:raise ValueError('At least 8 GiB available RAM required immediately before model loading')
                argv=count_argv(runtime,directory)
            elif op=='tokenize-help':argv=[runtime['tokenize'],'--help']
            else:argv=[runtime['executable'],'--'+op]
            result['argv']=argv
            emit({'phase':'counting complete templated prompt' if op=='count' else 'checking portable runtime','progress':.1})
            try:
                metrics=run_native(argv,cwd=directory,stdout_path=directory/(op+'.stdout'),stderr_path=directory/(op+'.stderr'),
                    wall_seconds=120 if op=='count' else 15,memory_bytes=6*1024**3 if op=='count' else 512*1024**2)
            except NativeTrialError as error:
                result['native_metrics']=error.metrics;result['native_error_reason']=error.reason
                raise
            result['native_metrics']=metrics
        if op=='count':
            count=parse_prompt_count((directory/'count.stderr').read_bytes())
            result.update(prompt_tokens=count,context_tokens=4096,required_output_reserve=1024,
                          admitted_for_generation=count<=3072,context_sha256=artifact['context_sha256'],
                          prompt_sha256=artifact['prompt_sha256'],schema_sha256=artifact['schema_sha256'])
            result['state']='counted' if count<=3072 else 'rejected_prompt_budget'
        else:result['state']='checked'
    except Exception as error:
        result.update(state='failed',error=type(error).__name__+': '+str(error))
    finally:evidence.close()
    result['elapsed_seconds']=time.monotonic()-started
    result['captures']={p.name:{'bytes':p.stat().st_size,'sha256':digest(p)} for p in (directory/(op+'.stdout'),directory/(op+'.stderr')) if p.is_file()}
    write_json(result_path,result)
    return {'state':result['state'],'receipt':result_path.name,'bytes':result_path.stat().st_size,'sha256':digest(result_path)}
