"""Offline dry-run plan only: never fetches, installs, imports a model, or spawns.

Run from the app with: python -m audio.research.plan_local_arranger
Optional --output writes the JSON plan to an explicitly named local file.
Package bytes/digests are publisher metadata, not local acquisition verification.
"""
import argparse
import json
import shutil
from pathlib import Path

APP=Path(__file__).resolve().parents[2]
GiB=1024**3
MODEL_REV='bc640142c66e1fdd12af0bd68f40445458f3869b'
RUNTIME_REV='620d2225cc436ab0b001e3200e590e3b286ef9ab'
MODEL=dict(repository='Qwen/Qwen3-4B-GGUF',revision=MODEL_REV,file='Qwen3-4B-Q4_K_M.gguf',
           bytes=2497280256,sha256='7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5',
           license='Apache-2.0',public=True,gated=False,
           url=f'https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/{MODEL_REV}/Qwen3-4B-Q4_K_M.gguf')
RUNTIME=dict(repository='ggml-org/llama.cpp',tag='b11669',commit=RUNTIME_REV,asset_id=644417050,
             file='llama-b11669-bin-win-cpu-x64.zip',bytes=19537886,
             sha256='37b3efcf962847ca869745fd4ccf7f5e7e8dacfeff9fa3523b4ca8cdc66d632b',
             expanded_bytes=49893149,members=51,license='MIT; bundled LLVM OpenMP Apache-2.0 WITH LLVM-exception',
             expanded_evidence='ZIP central directory from explicit 65536-byte HTTP range; full archive not verified',
             url='https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-win-cpu-x64.zip')

def tree_size(root):
    total=0
    for p in root.rglob('*'):
        try:
            if p.is_file():total+=p.stat().st_size
        except FileNotFoundError:pass
    return total

def plan():
    used=sum(tree_size(APP/p) for p in ('audio/research','audio/vendor/analysis'))
    free=shutil.disk_usage(APP).free
    components={'model_single_copy':MODEL['bytes'],'retained_runtime_zip':RUNTIME['bytes'],
                'runtime_expanded_metadata':RUNTIME['expanded_bytes'],'metadata_notices_allowance':2*1024**2,
                'scratch_allowance':64*1024**2,'output_allowance':8*1024**2}
    incremental=sum(components.values())
    argv=['<verified-runtime>/llama-completion.exe','--model','<verified-model>/'+MODEL['file'],
          '--offline','--gpu-layers','0','--threads','4','--threads-batch','4','--ctx-size','4096',
          '--n-predict','1024','--batch-size','256','--ubatch-size','128','--cache-type-k','f16','--cache-type-v','f16',
          '--no-context-shift','--jinja','--conversation','--single-turn','--reasoning','off','--reasoning-budget','0',
          '--json-schema-file','<owned-job>/proposal.schema.json','--file','<owned-job>/prompt.txt',
          '--seed','1015','--temp','0.7','--top-p','0.8','--top-k','20','--min-p','0','--presence-penalty','1.5',
          '--simple-io','--no-display-prompt','--no-warmup']
    return {'version':1,'state':'plan_only_not_authorized_to_acquire_or_execute','model':MODEL,'runtime':RUNTIME,
            'current':{'research_bytes':used,'research_cap':4*GiB,'research_headroom':4*GiB-used,
                       'free_disk':free,'existing_cumulative_acquisition_bytes':'must reconcile prior ledger before acquisition'},
            'disk':{'components':components,'incremental_estimate':incremental,
                    'new_package_acquisition_bytes':MODEL['bytes']+RUNTIME['bytes'],
                    'fits_current_research_cap':used+incremental<=4*GiB,
                    'reserve_after_estimate':free-incremental,'required_reserve':8*GiB,
                    'proposed_additional_allowance':3*GiB,'proposed_total_research_cap':7*GiB,
                    'proposed_total_cumulative_acquisition_cap':5*GiB},
            'run':{'argv_proposal_not_executed':argv,'prompt_tokens_max_including_template':3072,
                   'output_tokens_max':1024,'context_tokens':4096,'thinking':False,'threads':4,
                   'f16_kv_estimate_bytes':2*36*8*128*4096*2,'sampled_process_memory_stop_bytes':6*GiB,
                   'minimum_available_ram_before_launch':8*GiB,'wall_seconds':120,'stdout_stderr_cap_each':1024**2,
                   'launch':'owned supervised hidden ordinary process, shell=False, no server, no downloads, sanitized LLAMA_ARG_* env',
                   'ownership':'Windows Job before execution; parent-loss kill; cancellation and terminal reaping required',
                   'failure':'retain receipt; no automatic rerun or silent fallback presented as AI',
                   'limits':'RAM/throughput/template/schema behavior unmeasured; KV estimate excludes buffers and allocator overhead'},
            'gates':['Explicit budget approval and heavy-slot release','Verify exact bytes/hash before any GGUF load',
                     'Validate archive paths, links, entry counts and expanded sum before extraction; hash all members',
                     'Retain license/notices; inspect local executable/DLL dependency closure before launch',
                     'Check --help/--version from pinned executable; no assumption that source docs guarantee binary behavior',
                     'Count templated prompt tokens with the pinned tokenizer; reject overflow, never truncate source evidence',
                     'Reject invalid/extra JSON, invented source IDs, unsupported roles/ranges, undeclared timing edits',
                     'Benchmark existing deterministic baseline vs proposal on fixed symbolic cases; no claim model hears music']}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args()
    result=json.dumps(plan(),indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(result,encoding='utf-8')
    else:print(result)

if __name__=='__main__':main()
