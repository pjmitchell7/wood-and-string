"""Explicit arrangement selection; free text is local metadata, never analysis."""
from dataclasses import asdict, replace
import unicodedata
from .performance import arrange

VERSION = 1


def normalize_guidance(value, performers):
    if type(performers) is not int or performers not in (1, 2):
        raise ValueError('Choose one or two guitarists.')
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value)-{'version', 'source_notes', 'arrangement_notes', 'accompaniment'}:
        raise ValueError('Invalid preview notes or arrangement choice.')
    version = value.get('version', VERSION)
    if type(version) is not int or version != VERSION:
        raise ValueError('Unsupported preview preference version.')
    result = {'version': VERSION}
    for field in ('source_notes', 'arrangement_notes'):
        text = value.get(field, '')
        if not isinstance(text, str): raise ValueError('Preview notes must be text.')
        text = unicodedata.normalize('NFC', text).strip()
        if len(text)>256 or len(text.encode('utf-8'))>1024 or any(ord(c)<32 and c not in '\n\t' for c in text):
            raise ValueError('Each preview note is limited to 256 characters without control codes.')
        result[field] = text
    accompaniment = value.get('accompaniment', 'melody-and-bass')
    if accompaniment not in ('melody-and-bass', 'melody-only'):
        raise ValueError('Choose melody with bass, or melody only.')
    if accompaniment == 'melody-only' and performers != 1:
        raise ValueError('Melody only supports one guitarist. Two guitarists require melody and bass.')
    result['accompaniment'] = accompaniment
    return result


def guidance_is_default(guidance):
    return not guidance['source_notes'] and not guidance['arrangement_notes'] and guidance['accompaniment']=='melody-and-bass'


def arrange_with_preferences(performance, performers, value=None):
    performance.validate()
    guidance = normalize_guidance(value, performers)
    selected = performance
    omitted = []
    if guidance['accompaniment']=='melody-only':
        if not any(n.role=='melody' for n in performance.notes):
            raise ValueError('No estimated melody is available for a melody-only preview.')
        omitted = [n for n in performance.notes if n.role=='bass']
        selected = replace(performance, notes=[n for n in performance.notes if n.role!='bass'])
    arrangement = arrange(selected, performers)
    # Retain complete original evidence, including deliberately omitted events.
    arrangement['source'] = asdict(performance)
    arrangement['adaptations'].extend({'note_id':n.id, 'kind':'user-requested-omission',
        'reason':'Estimated bass omitted by the explicit melody-only choice.',
        'role':n.role, 'essential':False} for n in omitted)
    if not guidance_is_default(guidance):
        arrangement['user_guidance'] = {**guidance,
            'text_effect':'Saved notes only; not supplied to the analyzer or interpreted as music instructions.',
            'omitted_note_ids':[n.id for n in omitted]}
    return arrangement
