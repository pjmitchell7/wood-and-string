"""Pure public-eight protocol checks. No files, native calls, model or execution API.

This is a planning/validation component, not authorization to run a batch.
Existing first-forward and count workers remain unchanged.
"""
import hashlib
import json

from .arranger_prompt import build_prompt, canonical
from .gpu_trial_monitor import PROFILE
from .qwen_prompt_audit import NONTHINK_TEMPLATE

VERSION = 'local-arranger-public8-unchanged-v1'
PUBLIC_SHA = 'e6d3a223ce78220c22916006d5097c151fad58a19a09d61b74ed17a288380469'
FORWARD_FREEZE_SHA = '21224c06f0c72169a4e4815ad0fbcd15fb08ca3e504e2fea125be05f3f0245bb'
BASELINE_FREEZE_SHA = '9b727e2514ad327c421f7f733d72650323c1a70e0c466108b1845513103a17df'
RUNTIME_ID = '68bf68acf01f7b88f37012c7dc64335bcdc7b3596edb3e984bede00a382bb6ac'
PUBLIC_IDS = tuple(f'{mode}-dev-0{number}' for mode in ('acoustic', 'electric', 'bass', 'ensemble') for number in (1, 2))
CAPTURE_CAP = 8*1024**2
# Both 1MiB native captures, temporary lossless archives, and bounded metadata.
# This is a conservative live-stage admission, not a new storage budget.
STAGE_RESERVE = 5*1024**2


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw, limit):
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        raise ValueError('Invalid bounded protocol JSON')
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise ValueError('Duplicate protocol field')
            out[key] = value
        return out
    def invalid(_): raise ValueError('Nonfinite protocol JSON')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)


def input_rows(context):
    artifact = build_prompt(context)
    values = {'context.json': canonical(context), 'prompt.txt': artifact['prompt'].encode(),
              'proposal.schema.json': canonical(artifact['schema']), 'chat-template.jinja': NONTHINK_TEMPLATE}
    if sha(values['context.json']) != artifact['context_sha256']:
        raise ValueError('Public source context is not the unchanged canonical evidence')
    return {name: {'bytes': len(raw), 'sha256': sha(raw)} for name, raw in values.items()}


def build_plan(public_raw, forward_freeze_raw, baseline_freeze_raw, current_sources):
    """Pin only the public benchmark and preserved factories; never open paths."""
    for raw, expected in ((public_raw, PUBLIC_SHA), (forward_freeze_raw, FORWARD_FREEZE_SHA),
                          (baseline_freeze_raw, BASELINE_FREEZE_SHA)):
        if sha(raw) != expected: raise ValueError('Unchanged benchmark/first-forward/baseline freeze differs')
    public = strict_json(public_raw, 256*1024)
    forward = strict_json(forward_freeze_raw, 65536)
    baseline = strict_json(baseline_freeze_raw, 65536)
    if tuple(case['input']['case_id'] for case in public['cases']) != PUBLIC_IDS:
        raise ValueError('Require exactly the eight public cases in their frozen order')
    pins = dict(forward['inputs'])
    historical = {}
    for name, expected in baseline['inputs'].items():
        if name.startswith('audio/research/') and name != 'audio/research/arranger_benchmark_v1.json':
            historical[name] = expected
            continue  # Preserve prior run identity; these baseline-only helpers are never invoked.
        if name in pins and pins[name] != expected: raise ValueError('Baseline and model factories differ')
        pins[name] = expected
    if any(current_sources.get(name) != expected for name, expected in pins.items()):
        raise ValueError('A frozen source/factory changed; do not run the unchanged comparison')
    cases = [{'ordinal': i, 'case_id': case['input']['case_id'], 'input_files': input_rows(case['input']),
              'action': 'reuse_preserved_actual_case1' if i == 1 else 'count_then_at_most_one_generation',
              'context_tokens': 4096, 'prompt_token_limit': 3072, 'output_token_limit': 1024,
              'token_count': 1888 if i == 1 else None}
             for i, case in enumerate(public['cases'], 1)]
    return {'version': VERSION, 'state': 'protocol_only_no_native_authorization',
        'public_benchmark_sha256': PUBLIC_SHA, 'first_forward_freeze_sha256': FORWARD_FREEZE_SHA,
        'baseline_freeze_sha256': BASELINE_FREEZE_SHA, 'runtime_identity_sha256': RUNTIME_ID,
        'unchanged_sources': pins, 'cases': cases, 'gpu_profile': dict(PROFILE),
        'historical_baseline_helpers_not_executed': historical,
        'sampler': dict(forward['sampler']), 'new_count_attempts_maximum': 7,
        'new_generation_attempts_maximum': 7, 'retry_or_repair_attempts': 0,
        'capture_cap_including_prior_gpu_evidence_bytes': CAPTURE_CAP,
        'live_stage_reservation_bytes': STAGE_RESERVE,
        'worker_inputs': 'Only canonical case input and its generated prompt/schema/template; no expectations or examples.',
        'holdout_read': False, 'native_execution_allowed': False, 'audio_used': False}


def authenticate_count(raw, supervisor_result):
    if (type(supervisor_result) is not dict or supervisor_result.get('receipt') != 'count.result.json'
            or type(supervisor_result.get('bytes')) is not int
            or supervisor_result.get('bytes') != len(raw) or supervisor_result.get('sha256') != sha(raw)):
        raise ValueError('Prior count result size/hash binding differs')
    return strict_json(raw, 65536)


def validate_count(prior, expected_inputs, expected_argv, expected_device, captures):
    """Admission component for a future fixed worker's case-bound closure.

    The caller must supply protected bytes authenticated by its exact stage
    receipt. This pure check cannot establish filesystem/process ownership.
    """
    if type(prior) is not dict:raise ValueError('Expected count object')
    tokens = prior.get('prompt_tokens')
    audit, metrics = prior.get('prompt_audit', {}), prior.get('native_metrics', {})
    if (prior.get('version') != VERSION or prior.get('state') != 'counted' or prior.get('operation') != 'count'
            or prior.get('runtime_identity_sha256') != RUNTIME_ID
            or type(prior.get('model_generated_tokens_requested')) is not int
            or prior['model_generated_tokens_requested'] != 0
            or prior.get('fits_prompt_budget') is not True or type(tokens) is not int or not 1 <= tokens <= 3072
            or prior.get('context_tokens') != 4096 or prior.get('required_output_reserve') != 1024
            or prior.get('profile') != PROFILE or prior.get('resources', {}).get('profile') != PROFILE
            or prior.get('argv') != expected_argv or prior.get('selected_device') != expected_device
            or prior.get('input_files') != expected_inputs
            or prior.get('generation_gate_released') is not False
            or type(metrics.get('returncode')) is not int or metrics['returncode'] != 0
            or metrics.get('owned_job_empty') is not True
            or type(audit.get('count')) is not int or audit.get('count') != tokens or audit.get('entire_single_user_template_matches') is not True
            or audit.get('reasoning_template_state') != 'empty_think_block_prefilled'):
        raise ValueError('Count did not admit this exact case/runtime/transport/profile')
    for field, filename in (('context_sha256','context.json'),('prompt_sha256','prompt.txt'),
                            ('schema_sha256','proposal.schema.json')):
        if prior.get(field) != expected_inputs[filename]['sha256']:
            raise ValueError('Count evidence field differs from the bound input')
    if (audit.get('exact_prompt_sha256') != expected_inputs['prompt.txt']['sha256']
            or audit.get('exact_prompt_bytes') != expected_inputs['prompt.txt']['bytes']):
        raise ValueError('Count token audit does not bind the complete prompt')
    if set(captures) != {'count.stdout', 'count.stderr'} or set(prior.get('captures', {})) != set(captures):
        raise ValueError('Exact count capture closure required')
    for name, raw in captures.items():
        if type(raw) is not bytes or len(raw) > 1024**2 or prior['captures'][name] != {'bytes': len(raw), 'sha256': sha(raw)}:
            raise ValueError('Count capture bytes differ')
    if audit.get('capture_sha256') != sha(captures['count.stderr']):
        raise ValueError('Count token audit capture differs')
    return {'prompt_tokens': tokens, 'all_four_inputs_match': True, 'output_token_budget': 1024,
            'context_tokens': 4096, 'runtime_identity_sha256': RUNTIME_ID}


def next_action(ordinal, *, count_attempted=False, count_admitted=False, generation_attempted=False):
    """Consumed stages never retry, including pre-launch admission failures."""
    if type(ordinal) is not int or not 1 <= ordinal <= 8:
        raise ValueError('Public ordinal must be1..8')
    if any(type(v) is not bool for v in (count_attempted, count_admitted, generation_attempted)):
        raise ValueError('Exact persisted stage flags required')
    if ordinal == 1:
        if count_attempted or count_admitted or generation_attempted: raise ValueError('Case1 cannot be rerun')
        return 'reuse_preserved_actual_case1'
    if (count_admitted and not count_attempted) or (generation_attempted and not count_admitted):
        raise ValueError('Invalid stage order')
    if generation_attempted: return 'terminal_no_retry'
    if not count_attempted: return 'count'
    return 'generate' if count_admitted else 'terminal_count_not_admitted'


def admit_storage(prior_bytes, batch_bytes):
    if any(type(value) is not int or value < 0 for value in (prior_bytes, batch_bytes)):
        raise ValueError('Storage ledger requires nonnegative integer bytes')
    if prior_bytes+batch_bytes+STAGE_RESERVE > CAPTURE_CAP:
        raise ValueError('Preserve evidence; current stage lacks the existing GPU capture allowance')
    return {'combined_existing_bytes': prior_bytes+batch_bytes, 'live_stage_reserve_bytes': STAGE_RESERVE,
            'cap_bytes': CAPTURE_CAP, 'new_budget_created': False}
