"""Freeze and evaluate one public symbolic baseline run. No model or audio use."""
import argparse
import hashlib
import json
from pathlib import Path
import time

from audio.arranger_baseline import make_baseline
from audio.arranger_contract import validate_proposal
from audio.arranger_evaluation import evaluate_case
from audio.arranger_prompt import build_prompt, canonical

APP=Path(__file__).resolve().parents[2]
FILES=['audio/arranger_contract.py','audio/arranger_baseline.py','audio/arranger_prompt.py',
       'audio/arranger_evaluation.py','audio/research/evaluate_arranger_benchmark.py',
       'audio/research/arranger_benchmark_v1.json','audio/research/plan_local_arranger.py']


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve()
    if out.exists():raise FileExistsError('Preserve previous benchmark run; choose a fresh output directory')
    data=json.loads((APP/'audio/research/arranger_benchmark_v1.json').read_text(encoding='utf-8'))
    if data['benchmark_version']!='arranger-benchmark-v1' or len(data['cases'])!=8:raise ValueError('Expected frozen eight development cases')
    out.mkdir(parents=True)
    frozen={'version':1,'kind':'symbolic-development-baseline-only','inputs':{n:sha(APP/n) for n in FILES},
            'audio_used':False,'model_used':False,'holdout_seen':False,
            'future_inference_settings':{'context_tokens':4096,'prompt_max_tokens':3072,'output_max_tokens':1024,
                'wall_seconds':120,'seed':1015,'temperature':.7,'top_p':.8,'top_k':20,'min_p':0,
                'presence_penalty':1.5,'reasoning':False,'repair_attempts':0},
            'limits':'No exact prompt/output token or CPU throughput claim before pinned runtime checks. Keep overflow/truncation failures visible.'}
    (out/'frozen.json').write_bytes(canonical(frozen))
    code=out/'code'
    for name in FILES:
        target=code/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((APP/name).read_bytes())
    rows=[]
    for number,case in enumerate(data['cases']):
        item=out/f'case-{number+1:02d}';item.mkdir()
        # Only the validated input enters build_prompt; expectations are evaluator-only.
        artifact=build_prompt(case['input'])
        (item/'prompt.txt').write_text(artifact['prompt'],encoding='utf-8',newline='\n')
        (item/'proposal.schema.json').write_bytes(canonical(artifact['schema']))
        (item/'prompt-receipt.json').write_bytes(canonical({k:v for k,v in artifact.items() if k not in ('prompt','schema')}))
        began=time.monotonic();proposal=make_baseline(case['input']);elapsed=time.monotonic()-began
        evaluation=evaluate_case(case,proposal);evaluation['baseline_wall_seconds']=elapsed
        if elapsed>data['protocol']['deterministic_baseline_wall_seconds_per_case']:
            evaluation['expectation_failures'].append('Baseline exceeded case wall allowance')
        (item/'proposal.json').write_bytes(canonical(proposal))
        try:(item/'resolved.json').write_bytes(canonical(validate_proposal(case['input'],proposal)))
        except ValueError as error:(item/'validation-error.json').write_bytes(canonical({'error':str(error)}))
        rows.append(evaluation)
    report={'version':1,'kind':'symbolic-development-baseline-only','cases':rows,
        'hard_violations':sum(r['hard_violation'] is not None for r in rows),
        'cases_with_expectation_failures':sum(bool(r['expectation_failures']) for r in rows),
        'unsupported_text_review':'Reasons remain unverified text requiring independent inspection.',
        'musical_fidelity':'Not assessed. No audio, rendering, installed-mode approval or model inference.'}
    (out/'report.json').write_bytes(canonical(report))
    if any(sha(APP/n)!=digest for n,digest in frozen['inputs'].items()):raise ValueError('Inputs changed during benchmark; retained run is not a stable freeze')
    total=sum(p.stat().st_size for p in out.rglob('*') if p.is_file())
    if total>data['protocol']['benchmark_incremental_disk_bytes']:raise ValueError('Symbolic benchmark exceeded storage allowance')
    print(json.dumps({'cases':len(rows),'hard_violations':report['hard_violations'],
                      'cases_with_expectation_failures':report['cases_with_expectation_failures'],
                      'retained_bytes':total,'frozen_sha256':sha(out/'frozen.json')}))


if __name__=='__main__':main()
