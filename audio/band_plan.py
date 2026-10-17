"""Pure, unactivated band roster and declared source-clock plan.

Lineage, effects, role labels and complete endpoints are caller declarations,
not proof of recognition, musical independence, renderer history or release.
"""
import hashlib
import json
import re

VERSION = 'band-plan-v1'
RATE = 44100
MAX_BODY_FRAMES = 30 * RATE
MAX_FRAMES = 33 * RATE
MAX_EVENTS = 4000
MAX_PLAN_BYTES = 1024 * 1024
SUPPORT = {36: 109593, 38: 90913, 42: 34444}
GUARD = 4410
KIT_VERSION = 'virtuosity-basic-kit-render-v1'
MAP_SHA256 = '2230b2e37efa6faab2a77759e7389f7a335ebed406fd0303b59262323b825fd0'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')


def need(value, message):
    if not value:
        raise ValueError(message)


def check_cancel(cancel):
    need(callable(cancel), 'Cancellation callback required')
    if cancel():
        raise InterruptedError('Band operation cancelled')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def is_sha(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def shape(value, keys):
    need(type(value) is dict and set(value) == set(keys), 'Exact band object fields required')


def identity(value):
    need(type(value) is str and _ID.fullmatch(value) is not None, 'Bounded symbolic identity required')
    return value


def _provenance(value, budget):
    shape(value, ('source_refs', 'newly_composed'))
    refs = value['source_refs']
    need(type(refs) is list and len(refs) <= 8 and type(value['newly_composed']) is bool,
         'Typed bounded lineage declaration required')
    copied = [identity(ref) for ref in refs]
    need(len(set(copied)) == len(copied) and (copied or value['newly_composed']),
         'Source references or explicit newly composed declaration required')
    budget[0] += len(copied)
    need(budget[0] <= 8192, 'Band source-reference limit exceeded')
    return dict(source_refs=copied, newly_composed=value['newly_composed'])


def validate_plan(value, *, cancel=lambda: False):
    """Return a detached bounded plan. Nothing here chooses or renders notes."""
    check_cancel(cancel)
    shape(value, ('version', 'source', 'revision', 'mode', 'sample_rate',
                  'origin_sample', 'body_end_sample', 'parts'))
    need(value['version'] == VERSION and value['sample_rate'] == RATE
         and type(value['sample_rate']) is int, 'Band version or sample rate differs')
    shape(value['source'], ('id', 'sha256'))
    source = dict(id=identity(value['source']['id']), sha256=value['source']['sha256'])
    need(is_sha(source['sha256']), 'Original source SHA256 required')
    revision = identity(value['revision'])
    mode = value['mode']
    need(type(mode) is str and mode in ('acoustic', 'electric', 'bass', 'ensemble'),
         'Unsupported band mode')
    origin, end = value['origin_sample'], value['body_end_sample']
    need(integer(origin, 0, 2**53-1-MAX_FRAMES)
         and integer(end, origin+1, origin+MAX_BODY_FRAMES), 'Invalid absolute band passage')
    need(type(value['parts']) is list and 1 <= len(value['parts']) <= 4, 'Expected one to four band parts')
    parts, seen, budget = [], set(), [0]
    for supplied in value['parts']:
        check_cancel(cancel)
        need(type(supplied) is dict, 'Band part object required')
        drum = supplied.get('instrument') == 'drums'
        keys = ('id', 'instrument', 'role', 'content_end_sample', 'complete_end_sample', 'effects', 'provenance')
        shape(supplied, (*keys, 'events', 'event_provenance') if drum else keys)
        name = identity(supplied['id'])
        need(name not in seen, 'Duplicate band part identity')
        seen.add(name)
        instrument, role = supplied['instrument'], supplied['role']
        need(type(instrument) is str and instrument in ('acoustic', 'electric', 'bass', 'drums'),
             'Unsupported band instrument')
        need(type(role) is str and 0 < len(role) <= 64 and role.strip() == role
             and all(ord(c) >= 32 for c in role) and (role == 'drums') == drum,
             'Explicit melodic or percussion role required')
        content, complete = supplied['content_end_sample'], supplied['complete_end_sample']
        need(integer(content, origin+1, end) and integer(complete, content, origin+MAX_FRAMES),
             'Invalid declared content or complete-tail endpoint')
        effects = supplied['effects']
        need(type(effects) is list and len(effects) <= 8, 'Bounded per-instrument effect declarations required')
        effects = [identity(effect) for effect in effects]
        need(len(set(effects)) == len(effects) and (not drum or not effects),
             'Basic kit is dry; shared or repeated effects unavailable')
        part = dict(id=name, instrument=instrument, role=role, content_end_sample=content,
                    complete_end_sample=complete, effects=effects,
                    provenance=_provenance(supplied['provenance'], budget))
        if drum:
            events, refs = supplied['events'], supplied['event_provenance']
            need(type(events) is list and 1 <= len(events) <= MAX_EVENTS and type(refs) is dict,
                 'Bounded explicit kit events and lineage required')
            copied, ids, attacks, previous = [], set(), set(), None
            for index, event in enumerate(events):
                if index % 32 == 0:
                    check_cancel(cancel)
                shape(event, ('id', 'start', 'end', 'note', 'velocity'))
                event_id = identity(event['id'])
                need(event_id not in ids and integer(event['start'], origin, end-1)
                     and integer(event['end'], event['start']+1, end)
                     and type(event['note']) is int and event['note'] in SUPPORT
                     and integer(event['velocity'], 1, 127), 'Invalid basic-kit event')
                attack = (event['start'], event['note'])
                order = (*attack, event_id)
                need(attack not in attacks and (previous is None or previous < order),
                     'Kit events must sort by attack/key/ID without duplicate attack keys')
                previous = order
                ids.add(event_id)
                attacks.add(attack)
                copied.append(dict(event))
            need(set(refs) == ids, 'Every kit event needs exactly one lineage declaration')
            event_refs = {event['id']: _provenance(refs[event['id']], budget) for event in copied}
            need(part['provenance']['newly_composed'] == any(p['newly_composed'] for p in event_refs.values()),
                 'Kit summary and event composition declarations differ')
            minimum = max(max(e['end'], e['start']+SUPPORT[e['note']]) for e in copied)+GUARD
            need(content == max(e['end'] for e in copied) and complete >= minimum,
                 'Kit plan must retain every possible variant support and guard')
            part.update(events=copied, event_provenance=event_refs)
        parts.append(part)
    melodic = [p['instrument'] for p in parts if p['instrument'] != 'drums']
    kits = sum(p['instrument'] == 'drums' for p in parts)
    need(kits <= 1, 'Only one persistent basic kit is supported')
    if mode == 'ensemble':
        need(sorted(melodic) == ['acoustic', 'bass', 'electric'], 'Ensemble needs exactly one acoustic, Electric and Bass')
    else:
        need(1 <= len(melodic) <= 2 and all(i == mode for i in melodic), 'Mode requires one or two matching melodic performers')
        roles = [part['role'].casefold() for part in parts if part['instrument'] != 'drums']
        need(len(set(roles)) == len(roles), 'Two same-family performers need distinct declared roles')
    need(mode != 'acoustic' or kits == 0, 'Acoustic mode cannot contain drums')
    result = dict(version=VERSION, source=source, revision=revision, mode=mode,
                  sample_rate=RATE, origin_sample=origin, body_end_sample=end, parts=parts)
    need(len(canonical(result)) <= MAX_PLAN_BYTES, 'Complete band plan exceeds byte limit')
    check_cancel(cancel)
    return result


def plan_sha256(value, *, cancel=lambda: False):
    return sha256(canonical(validate_plan(value, cancel=cancel)))


def drum_request(value, *, cancel=lambda: False):
    """Exact renderer request; one caller-owned persistent kit, no inferred beat."""
    plan = validate_plan(value, cancel=cancel)
    kit = next((part for part in plan['parts'] if part['instrument'] == 'drums'), None)
    if kit is None:
        return None
    return dict(sample_rate=RATE, render_origin_sample=plan['origin_sample'], events=kit['events'])
