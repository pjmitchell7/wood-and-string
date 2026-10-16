"""Validated revisions of saved mixed previews; no analyzer or source audio edit."""
from dataclasses import asdict
import hashlib
import json
import math
import uuid

from audio.preferences import normalize_guidance, guidance_is_default
from audio.refinement import performance_from_dict, normalize_operations, refine, VERSION as POLICY_VERSION
from audio.preview_instruments import (validate_saved_selection, preview_guidance,
                                       required_roles, arrange_selected, renderer_version)

VERSION = 1
MAX_PARENT_BYTES = 12 * 1024 ** 2


def result_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > MAX_PARENT_BYTES:
        raise ValueError('This saved preview is too large to refine.')
    return hashlib.sha256(raw).hexdigest()


def prepare_refinement(library, parent_job_id, operations):
    """Revalidate immutable parent evidence on admission and again in the worker.

    The returned spec is also the complete new job identity input. A parent's
    result hash pins its notes, analysis, saved preferences and published asset.
    """
    try:
        if not isinstance(parent_job_id, str) or str(uuid.UUID(parent_job_id)) != parent_job_id:
            raise ValueError('Choose a saved mixed preview to refine.')
        parent = library.job(parent_job_id)
        request = parent['request']
        if parent['state'] != 'complete' or request.get('kind') not in ('mixed', 'refine'):
            raise ValueError('Choose a completed mixed preview to refine.')
        result = parent['result']
        digest = result_digest(result)
        asset = result['asset']
        if not library.verify(asset['id']):
            raise ValueError('The saved preview failed verification. Make a new preview first.')
        source = library.get(request['asset_id'])
        if source['kind'] != 'original' or source['sha256'] != request['sha256']:
            raise ValueError('The original recording no longer matches this preview.')
        if not library.verify(source['id']):
            raise ValueError('The original recording failed verification.')
        if asset['source_id'] != source['source_id']:
            raise ValueError('The saved preview does not belong to this original.')
        start, end = request['start_seconds'], request['end_seconds']
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end or end - start > 30):
            raise ValueError('The saved passage times are invalid.')
        origin, boundary = round(start * 44100), round(end * 44100)
        arrangement = result['arrangement']
        original = performance_from_dict(arrangement['source'])
        current = performance_from_dict(arrangement.get('refinement_source', arrangement['source']))
        if (original.source_id != source['source_id'] or current.source_id != original.source_id
                or original.sample_rate != 44100 or current.sample_rate != 44100
                or arrangement.get('render_origin_sample') != origin):
            raise ValueError('The saved source timing is invalid.')
        for performance in (original, current):
            if any(not origin <= n.start < n.end <= boundary for n in performance.notes):
                raise ValueError('The saved notes extend outside this passage.')
        performers = request['performers']
        selection = validate_saved_selection(request)
        if selection.get('instrument')=='jazz-bass' and (arrangement.get('instrument') != 'jazz-bass'
                          or arrangement.get('part_selection') != selection['bass_part']
                          or arrangement.get('profile_version') != selection['bass_profile_version']):
            raise ValueError('The saved bass preview has an invalid instrument or part choice.')
        if selection.get('instrument')=='electric':
            actual_asset=library.get(asset['id'])
            if (asset.get('sha256')!=actual_asset['sha256']
                    or asset.get('source_id')!=actual_asset['source_id']
                    or actual_asset['source_id']!=source['source_id']):
                raise ValueError('The saved Electric preview does not match its stored audio asset.')
            if (arrangement.get('instrument')!='electric' or arrangement.get('renderable') is not True
                    or arrangement.get('planner_version')!=selection['electric_planner_version']
                    or arrangement.get('profile_version')!=selection['electric_profile_version']):
                raise ValueError('The saved Electric preview has an invalid arrangement profile.')
            from tone_evidence import verify_preview
            try:
                verify_preview(library,result,selection)
            except (ValueError,OSError) as error:
                raise ValueError('The saved Electric dry evidence failed verification. Make a new preview before revising it.') from error
        guidance = preview_guidance(request.get('guidance'), performers, selection)
        normalized = normalize_operations(operations)
        if not normalized:
            raise ValueError('Choose at least one note edit before making a revision.')
        corrected, ledger = refine(current, normalized, passage_start=origin, passage_end=boundary)
        if not corrected.notes:
            raise ValueError('Keep at least one note in the revised passage.')
        roles = {n.role for n in corrected.notes}
        if selection and not set(required_roles(selection,performers=performers)) <= roles:
            raise ValueError('Keep at least one note in each selected Bass part.' if selection.get('instrument')=='jazz-bass'
                             else 'Keep at least one note in each selected Electric part.')
        if performers == 2 and not {'melody', 'bass'} <= roles:
            raise ValueError('Two guitarists need both melody and bass. Keep a note in each part.')
        if guidance['accompaniment'] == 'melody-only' and 'melody' not in roles:
            raise ValueError('Keep at least one melody note for a melody-only preview.')
        spec = dict(kind='refine', parent_job_id=parent_job_id, parent_result_sha256=digest,
                    asset_id=source['id'], sha256=source['sha256'], performers=performers,
                    start_seconds=float(start), end_seconds=float(end),
                    renderer_version=renderer_version(selection), refinement_version=VERSION,
                    refinement_policy_version=POLICY_VERSION,
                    operations=normalized, **selection)
        if not guidance_is_default(guidance):
            spec['guidance'] = guidance
        return dict(spec=spec, parent=parent, original=original, current=current,
                    corrected=corrected, ledger=ledger, source=source, origin=origin)
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as error:
        raise ValueError('This saved preview cannot be refined. Make a new mixed preview first.') from error


def arrange_refinement(prepared):
    spec = prepared['spec']
    selection = validate_saved_selection(spec)
    arrangement = arrange_selected(prepared['corrected'], spec['performers'], spec.get('guidance'), selection,
                                   render_origin_sample=prepared['origin'])
    # Never relabel a manual interpretation as the original analyzer evidence.
    arrangement['source'] = asdict(prepared['original'])
    arrangement['refinement_source'] = asdict(prepared['corrected'])
    arrangement['refinement'] = dict(version=VERSION, parent_job_id=spec['parent_job_id'],
                                     parent_result_sha256=spec['parent_result_sha256'],
                                     policy_version=POLICY_VERSION,
                                     edits=prepared['ledger']['changes'],
                                     passage_start=prepared['ledger']['passage_start'],
                                     passage_end=prepared['ledger']['passage_end'],
                                     evidence='Explicit user edits; not new analyzer observations')
    arrangement['render_origin_sample'] = prepared['origin']
    return arrangement
