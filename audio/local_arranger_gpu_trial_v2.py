"""One fixed corrected-transport load/count worker; no generation."""
from pathlib import Path
from .local_arranger_gpu_trial import run_probe, DEVICE_RESULT
from .qwen_prompt_audit import NONTHINK_TEMPLATE

TRIAL_ROOT=Path(__file__).resolve().parents[1]/'work/local-arranger-gpu-trial-002'
VERSION='local-arranger-vulkan-exact-nonthinking-count-v2'


def run(request,emit):
    if type(request) is not dict or request.get('operation')!='count':
        raise ValueError('Corrected transport trial permits only the fixed zero-generation count')
    return run_probe(request,emit,trial_root=TRIAL_ROOT,device_result=DEVICE_RESULT,
        version=VERSION,transport_template=NONTHINK_TEMPLATE)
