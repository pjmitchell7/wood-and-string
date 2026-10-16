"""Explicit preview instrument selection; acoustic defaults keep their identity."""
from .bass_performance import VERSION as BASS_PLANNER_VERSION, arrange_bass
from .bass_render import RENDER_VERSION as BASS_RENDER_VERSION
from .growlybass import PROFILE_VERSION as BASS_PROFILE_VERSION
from .electric_performance import VERSION as ELECTRIC_PLANNER_VERSION, arrange_electric
from .electric_preview_render import RENDER_VERSION as ELECTRIC_RENDER_VERSION
from .emilyguitar import PROFILE_VERSION as ELECTRIC_PROFILE_VERSION
from .render import RENDER_VERSION as ACOUSTIC_RENDER_VERSION
from .preferences import normalize_guidance, guidance_is_default, arrange_with_preferences

BASS_FIELDS = ('bass_part', 'bass_planner_version', 'bass_profile_version')
ELECTRIC_FIELDS = ('electric_planner_version', 'electric_profile_version')


def selection_fields(value, performers):
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose one or two performers.')
    if type(value) is not dict:
        raise ValueError('Invalid preview instrument selection.')
    instrument = value.get('instrument', 'acoustic')
    if instrument == 'acoustic':
        if any(key in value for key in BASS_FIELDS):
            raise ValueError('Bass part choices require Jazz Bass.')
        if any(key in value for key in ELECTRIC_FIELDS):
            raise ValueError('Electric profile choices require Electric guitar.')
        return {}
    if instrument == 'electric':
        if any(key in value for key in BASS_FIELDS):
            raise ValueError('Bass part choices require Jazz Bass.')
        return dict(instrument='electric', electric_planner_version=ELECTRIC_PLANNER_VERSION,
                    electric_profile_version=ELECTRIC_PROFILE_VERSION)
    if instrument != 'jazz-bass':
        raise ValueError('Choose acoustic guitar, Electric guitar, or Jazz Bass.')
    if any(key in value for key in ELECTRIC_FIELDS):
        raise ValueError('Electric profile choices require Electric guitar.')
    part = value.get('bass_part', 'melody' if performers == 1 else 'melody-and-bass')
    if (performers == 1 and part not in ('melody', 'bass-line')) or (performers == 2 and part != 'melody-and-bass'):
        raise ValueError('Choose melody or bass line for solo, or melody and bass for two bass players.')
    return dict(instrument='jazz-bass', bass_part=part,
                bass_planner_version=BASS_PLANNER_VERSION, bass_profile_version=BASS_PROFILE_VERSION)


def validate_saved_selection(spec):
    fields = selection_fields(spec, spec['performers'])
    if any(spec.get(key) != value for key, value in fields.items()):
        if fields.get('instrument') == 'electric':
            raise ValueError('The Electric arrangement profile changed. Start a new preview.')
        raise ValueError('The bass arrangement profile changed. Start a new preview.')
    return fields


def renderer_version(selection):
    """Resolve explicit instrument identity; a truthy selection is not Bass."""
    if type(selection) is not dict:
        raise ValueError('Invalid preview instrument selection.')
    instrument = selection.get('instrument', 'acoustic')
    versions = {'acoustic': ACOUSTIC_RENDER_VERSION, 'jazz-bass': BASS_RENDER_VERSION,
                'electric': ELECTRIC_RENDER_VERSION}
    if type(instrument) is not str or instrument not in versions:
        raise ValueError('Unsupported preview instrument.')
    return versions[instrument]


def preview_guidance(value, performers, selection):
    guidance = normalize_guidance(value, performers)
    if selection and guidance['accompaniment'] != 'melody-and-bass':
        if selection.get('instrument') == 'electric':
            raise ValueError('Electric previews use the observed parts. The melody-only accompaniment switch is not supported.')
        raise ValueError('Use the Bass part choice for Jazz Bass previews.')
    return guidance


def required_roles(selection, *, performers=1):
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose one or two performers.')
    if selection.get('instrument') == 'electric' and performers == 2:
        return ('melody', 'bass')
    if selection.get('bass_part') == 'bass-line':
        return ('bass',)
    if selection.get('bass_part') == 'melody-and-bass':
        return ('melody', 'bass')
    return ('melody',)


def arrange_selected(performance, performers, guidance, selection, *, render_origin_sample=0):
    normalized = preview_guidance(guidance, performers, selection)
    if not selection:
        return arrange_with_preferences(performance, performers, normalized)
    if selection.get('instrument') == 'electric':
        validate_saved_selection({**selection, 'performers': performers})
        arrangement = arrange_electric(performance, performers, render_origin_sample=render_origin_sample)
        if not guidance_is_default(normalized):
            arrangement['user_guidance'] = {**normalized,
                'text_effect': 'Saved notes only; not supplied to the analyzer or interpreted as music instructions.',
                'omitted_note_ids': []}
        return arrangement
    if selection.get('instrument') != 'jazz-bass':
        raise ValueError('Unsupported preview instrument selection. Normalize the selection first.')
    arrangement = arrange_bass(performance, performers,
                               solo_part=selection['bass_part'] if performers == 1 else 'melody')
    arrangement['profile_version'] = selection['bass_profile_version']
    if not guidance_is_default(normalized):
        arrangement['user_guidance'] = {**normalized,
            'text_effect':'Saved notes only; not supplied to the analyzer or interpreted as music instructions.',
            'omitted_note_ids':[n['note_id'] for n in arrangement['adaptations']
                                if n['kind'] == 'omission' and n['reason'] == 'Excluded by explicit bass part selection']}
    return arrangement
