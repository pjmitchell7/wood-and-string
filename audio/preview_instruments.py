"""Explicit preview instrument selection; acoustic defaults keep their identity."""
from .bass_performance import VERSION as BASS_PLANNER_VERSION, arrange_bass
from .bass_render import RENDER_VERSION as BASS_RENDER_VERSION
from .growlybass import PROFILE_VERSION as BASS_PROFILE_VERSION
from .preferences import normalize_guidance, guidance_is_default, arrange_with_preferences


def selection_fields(value, performers):
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose one or two performers.')
    instrument = value.get('instrument', 'acoustic')
    if instrument == 'acoustic':
        if any(key in value for key in ('bass_part', 'bass_planner_version', 'bass_profile_version')):
            raise ValueError('Bass part choices require Jazz Bass.')
        return {}
    if instrument != 'jazz-bass':
        raise ValueError('Choose acoustic guitar or the experimental Jazz Bass preview.')
    part = value.get('bass_part', 'melody' if performers == 1 else 'melody-and-bass')
    if (performers == 1 and part not in ('melody', 'bass-line')) or (performers == 2 and part != 'melody-and-bass'):
        raise ValueError('Choose melody or bass line for solo, or melody and bass for two bass players.')
    return dict(instrument='jazz-bass', bass_part=part,
                bass_planner_version=BASS_PLANNER_VERSION, bass_profile_version=BASS_PROFILE_VERSION)


def validate_saved_selection(spec):
    fields = selection_fields(spec, spec['performers'])
    if any(spec.get(key) != value for key, value in fields.items()):
        raise ValueError('The bass arrangement profile changed. Start a new preview.')
    return fields


def preview_guidance(value, performers, selection):
    guidance = normalize_guidance(value, performers)
    if selection and guidance['accompaniment'] != 'melody-and-bass':
        raise ValueError('Use the Bass part choice for Jazz Bass previews.')
    return guidance


def required_roles(selection):
    if selection.get('bass_part') == 'bass-line':
        return ('bass',)
    if selection.get('bass_part') == 'melody-and-bass':
        return ('melody', 'bass')
    return ('melody',)


def arrange_selected(performance, performers, guidance, selection):
    normalized = preview_guidance(guidance, performers, selection)
    if not selection:
        return arrange_with_preferences(performance, performers, normalized)
    arrangement = arrange_bass(performance, performers,
                               solo_part=selection['bass_part'] if performers == 1 else 'melody')
    arrangement['profile_version'] = selection['bass_profile_version']
    if not guidance_is_default(normalized):
        arrangement['user_guidance'] = {**normalized,
            'text_effect':'Saved notes only; not supplied to the analyzer or interpreted as music instructions.',
            'omitted_note_ids':[n['note_id'] for n in arrangement['adaptations']
                                if n['kind'] == 'omission' and n['reason'] == 'Excluded by explicit bass part selection']}
    return arrangement
