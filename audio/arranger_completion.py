"""Strict stdout framing for the pinned Windows llama-completion trial.

The caller separately verifies native lifecycle, stop diagnostics, model/input
identities and then passes the entire returned body to parse_response. A suffix
alone does not authenticate the native stop branch or identify the sampled EOG
token. The pinned runtime warns that its EOG set overrides a model token type;
this module does not infer that the stop token was specifically <|im_end|>.
"""
import hashlib
import re

from .arranger_prompt import MAX_RESPONSE_BYTES

WINDOWS_EOG_TRAILER = b' [end of text]\r\n\r\n\r\n'
MAX_STDERR_BYTES = 1024**2


def extract_completion(raw_stdout):
    """Remove exactly one terminal transport frame, preserving all body bytes.

    No whitespace stripping, JSON search, repair, refusal classification or
    partial-output recovery. Missing framing (including token-budget exhaustion)
    fails. Prefix text and embedded frames remain for the strict JSON parser.
    """
    if (type(raw_stdout) is not bytes
            or not 0 < len(raw_stdout) <= MAX_RESPONSE_BYTES+len(WINDOWS_EOG_TRAILER)):
        raise ValueError('Expected bounded raw completion stdout bytes')
    if not raw_stdout.endswith(WINDOWS_EOG_TRAILER):
        raise ValueError('Missing exact pinned Windows completion trailer')
    body = raw_stdout[:-len(WINDOWS_EOG_TRAILER)]
    if not 0 < len(body) <= MAX_RESPONSE_BYTES:
        raise ValueError('Completion body is empty or exceeds the response bound')
    return body


def verify_eog(raw_stderr, *, prompt_count, token_budget=1024):
    """Check the fixed plain/verbosity5 native EOG and accepted-token counters.

    The caller supplies its separately audited prompt count and binds native
    argv/lifecycle/capture identity. n_remain decreases once per accepted output
    token, including the terminal EOG. The sampler perf count increments on
    accept for prompt AND generated tokens; grammar resampling applications do
    not increment that count. No specific EOG token ID is established here.
    """
    if type(raw_stderr) is not bytes or not 0<len(raw_stderr)<=MAX_STDERR_BYTES:
        raise ValueError('Expected bounded raw generation stderr bytes')
    if type(token_budget) is not int or token_budget!=1024:
        raise ValueError('Expected fixed first-generation token budget 1024')
    if type(prompt_count) is not int or not 1<=prompt_count<=4096-token_budget:
        raise ValueError('Expected separately verified prompt count with output reserve')
    eog=list(re.finditer(rb'(?m)^found an EOG token\r\n',raw_stderr))
    if len(eog)!=1:raise ValueError('Missing or ambiguous exact native EOG diagnostic')
    remaining=list(re.finditer(rb'(?m)^n_remain: (-?[0-9]{1,5})\r\n',raw_stderr))
    if not 1<=len(remaining)<=token_budget:
        raise ValueError('Accepted generated-token count is outside the fixed budget')
    values=[int(row[1]) for row in remaining]
    if (any(str(value).encode()!=row[1] for row,value in zip(remaining,values))
            or values!=list(range(token_budget-1,token_budget-len(values)-1,-1))):
        raise ValueError('Native remaining-token sequence is not exact and descending')
    if eog[0].start()<remaining[-1].end():
        raise ValueError('Native EOG diagnostic precedes final generated-token counter')
    perf=list(re.finditer(
        rb'(?m)^common_perf_print:    samplers time = +([0-9]{1,10}\.[0-9]{2}) ms / +([0-9]{1,7}) tokens\r\n',
        raw_stderr))
    if len(perf)!=1 or perf[0].start()<eog[0].end():
        raise ValueError('Missing or ambiguous final native sampler-accept counter')
    total=int(perf[0][2])
    if str(total).encode()!=perf[0][2] or total!=prompt_count+len(remaining):
        raise ValueError('Native total accepted tokens differ from prompt plus generated tokens')
    return {'version':'pinned-windows-completion-eog-v1',
            'capture_bytes':len(raw_stderr),'capture_sha256':hashlib.sha256(raw_stderr).hexdigest(),
            'verified_prompt_tokens':prompt_count,'token_budget':token_budget,
            'accepted_generated_tokens_including_eog':len(remaining),
            'remaining_output_budget':values[-1],'sampler_total_accepted_tokens':total,
            'native_eog_diagnostic_count':1,'native_eog_after_final_counter':True,
            'scope':'Native EOG-class stop and accepted-token counters; exact sampled EOG ID is unknown.'}
