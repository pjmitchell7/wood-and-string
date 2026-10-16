"""Production integration boundary for the reviewed dry Electric adapter.

The underlying renderer and its private receipt keep their prototype identity.
This boundary only gates planner refusal and provides bounded preview metrics;
it adds no synthesis, sustain, effects or musical acceptance.
"""
from copy import deepcopy

from .electric_performance import VERSION as PLANNER_VERSION
from .electric_render import RENDER_VERSION as ADAPTER_VERSION, render as render_adapter
from .emilyguitar import PROFILE_VERSION

RENDER_VERSION = 'electric-preview-dry-emily-v1'


def render(arrangement, output, *, cancel=lambda: False, progress=lambda _: None, level_policy='peak'):
    if cancel():
        raise InterruptedError('Electric preview cancelled')
    if (type(arrangement) is not dict or arrangement.get('instrument') != 'electric'
            or arrangement.get('planner_version') != PLANNER_VERSION
            or arrangement.get('profile_version') != PROFILE_VERSION):
        raise ValueError('The Electric arrangement profile changed. Make a new preview.')
    if arrangement.get('renderable') is not True:
        raise ValueError('These notes could not be assigned to the requested Electric players. Review the listed omissions or choose another passage.')
    receipt = render_adapter(arrangement, output, cancel=cancel, progress=progress, level_policy=level_policy)
    # Preserve the original on-disk receipt. Its false prototype-scope acceptance
    # flags must not be rewritten into a musical or whole-app acceptance claim.
    fields = ('frames', 'sample_rate', 'source_origin_sample', 'peak', 'raw_peak',
        'gain_db', 'level_policy', 'level_measurement', 'target_sample_peak_dbfs',
        'clipped_samples', 'quantization', 'instrument', 'samples', 'profile_version',
        'instances', 'quality_by_instance', 'processing_mode', 'sample_limits',
        'round_robin_policy', 'dynamics', 'same_string_choke',
        'maximum_reattack_damping_seconds', 'render_tail_seconds', 'effects',
        'loops_added', 'original_audio_mixed', 'contours_not_rendered', 'physical_assumptions')
    metrics = {key: deepcopy(receipt[key]) for key in fields if key in receipt}
    metrics.update(renderer_version=RENDER_VERSION, dry_adapter_version=ADAPTER_VERSION,
        planner_version=PLANNER_VERSION, musical_acceptance=False,
        evidence_format='electric-render-evidence-v1',
        prototype_receipt_scope={key: receipt[key] for key in
            ('native_adapter_acceptance', 'musical_acceptance', 'app_mode_enabled')})
    return metrics
