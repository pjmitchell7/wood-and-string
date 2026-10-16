"""Explicit manual interpretation revisions; never mutate source evidence."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math

from .performance import Note, Performance

VERSION = 'manual-note-refinement-v1'


def _text(value, label):
    if type(value) is not str or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid ' + label)
    return value


def _finite_tree(value, depth=0):
    if depth > 8:
        raise ValueError('Metadata too deeply nested')
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) in (list, tuple) and len(value) <= 16000:
        for item in value: _finite_tree(item, depth + 1)
        return
    if type(value) is dict and len(value) <= 128 and all(type(k) is str for k in value):
        for item in value.values(): _finite_tree(item, depth + 1)
        return
    raise ValueError('Invalid or nonfinite metadata')


def performance_from_dict(value):
    """Validate serialized saved evidence and return a detached Performance."""
    if type(value) is not dict or set(value) - set(Performance.__dataclass_fields__):
        raise ValueError('Invalid performance fields')
    if not {'source_id', 'sample_rate', 'notes'} <= set(value):
        raise ValueError('Missing performance fields')
    _text(value['source_id'], 'source ID')
    for key, default in (('sample_rate', None), ('version', 1), ('analysis_offset_samples', 0)):
        if type(value.get(key, default)) is not int:
            raise ValueError('Performance integer required')
    if value.get('analysis_offset_samples', 0) < 0:
        raise ValueError('Invalid source origin')
    rows = value['notes']
    if type(rows) is not list or len(rows) > 4000:
        raise ValueError('Expected at most 4000 notes')
    notes = []
    contour_count = 0
    for row in rows:
        if type(row) is not dict or set(row) - set(Note.__dataclass_fields__) or not {'id','start','end','pitch'} <= set(row):
            raise ValueError('Invalid note fields')
        for key, default in (('id', None), ('role', 'melody'), ('phrase', 'phrase-1')):
            _text(row.get(key, default), key)
        confidence = row.get('confidence', 1.)
        if type(confidence) not in (int, float) or not math.isfinite(confidence):
            raise ValueError('Invalid confidence')
        contour = row.get('contour', ())
        if type(contour) not in (list, tuple) or len(contour) > 16000:
            raise ValueError('Invalid contour')
        contour_count += len(contour)
        if contour_count > 100000:
            raise ValueError('Too many contour points')
        previous = -1
        for point in contour:
            if (type(point) not in (list, tuple) or len(point) != 2 or type(point[0]) is not int
                    or type(point[1]) not in (int, float) or not math.isfinite(point[1]) or point[0] < previous):
                raise ValueError('Invalid contour point')
            previous = point[0]
        notes.append(Note(**{**row, 'contour': tuple(tuple(p) for p in contour)}))
    beats = value.get('beats', [])
    if type(beats) is not list:
        raise ValueError('Invalid beats')
    _finite_tree(beats)
    # Bound total metadata, not only each nested collection.
    if len(json.dumps(value, allow_nan=False)) > 8_000_000:
        raise ValueError('Performance evidence too large')
    result = Performance(value['source_id'], value['sample_rate'], notes,
                         value.get('version', 1), value.get('analysis_offset_samples', 0), deepcopy(beats))
    result.validate()
    return result


def normalize_operations(operations):
    """Canonical independent operations, suitable for versioned job identity."""
    if type(operations) is not list or len(operations) > 128:
        raise ValueError('Expected at most 128 operations')
    result, used = [], set()
    for op in operations:
        if type(op) is not dict or set(op) != {'kind', 'note_ids'} or op['kind'] not in ('join', 'omit'):
            raise ValueError('Invalid refinement operation')
        ids = op['note_ids']
        if type(ids) is not list or not 1 <= len(ids) <= 4000:
            raise ValueError('Invalid note IDs')
        for ident in ids: _text(ident, 'note ID')
        if len(set(ids)) != len(ids) or used.intersection(ids):
            raise ValueError('A note may be edited only once')
        if op['kind'] == 'join' and len(ids) < 2:
            raise ValueError('Join requires at least two notes')
        used.update(ids)
        result.append({'kind': op['kind'], 'note_ids': sorted(ids)})
    return sorted(result, key=lambda op: (op['kind'], op['note_ids']))


def refine(performance, operations, *, passage_start, passage_end):
    """Return (corrected Performance, ledger); all times remain source-absolute.

    Join is a user assertion, not detected legato. No pitch/confidence is
    fabricated inside gaps. Confidence is the minimum member confidence;
    first member velocity/phrase are retained and all members are audited.
    """
    if type(performance) is not Performance:
        raise ValueError('Expected Performance')
    source = performance_from_dict(asdict(performance))
    if (type(passage_start) is not int or type(passage_end) is not int
            or not 0 <= passage_start < passage_end):
        raise ValueError('Invalid passage bounds')
    if any(n.start < passage_start or n.end > passage_end for n in source.notes):
        raise ValueError('Source note outside passage')
    ops = normalize_operations(operations)
    lookup = {n.id: n for n in source.notes}
    removed, additions, entries = set(), [], []
    for op in ops:
        if any(i not in lookup for i in op['note_ids']):
            raise ValueError('Unknown note ID')
        members = sorted((lookup[i] for i in op['note_ids']), key=lambda n: (n.start, n.end, n.id))
        entry = {'kind': op['kind'], 'members': [asdict(n) for n in members]}
        if op['kind'] == 'join':
            first, last = members[0], members[-1]
            if any((n.pitch, n.role) != (first.pitch, first.role) for n in members):
                raise ValueError('Join requires identical pitch and role')
            if any(a.end > b.start for a, b in zip(members, members[1:])):
                raise ValueError('Cannot join overlapping notes')
            chosen = set(op['note_ids'])
            if any(n.id not in chosen and n.role == first.role and n.start < last.end and n.end > first.start for n in source.notes):
                raise ValueError('Unselected same-role note intervenes or overlaps')
            payload = [VERSION, source.source_id, source.sample_rate, [asdict(n) for n in members]]
            ident = 'joined-' + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if ident in lookup: raise ValueError('Joined note ID collision')
            joined = Note(ident, first.start, last.end, first.pitch, first.velocity, first.role,
                          min(n.confidence for n in members), first.phrase,
                          tuple(p for n in members for p in n.contour))
            additions.append(joined)
            entry.update(result_note_id=ident,
                         unobserved_gaps=[{'start': a.end, 'end': b.start} for a,b in zip(members,members[1:]) if a.end < b.start],
                         confidence_policy='minimum_member_no_gap_confidence',
                         velocity_policy='first_member', phrase_policy='first_member',
                         interpretation='user_asserted_join_not_detected_legato')
        removed.update(op['note_ids'])
        entries.append(entry)
    source.notes = sorted([n for n in source.notes if n.id not in removed] + additions, key=lambda n: (n.start, n.end, n.id)) if ops else source.notes
    source.validate()
    ledger = {'version': VERSION, 'source_id': source.source_id, 'sample_rate': source.sample_rate,
              'passage_start': passage_start, 'passage_end': passage_end,
              'operations': ops, 'changes': entries}
    return source, ledger
