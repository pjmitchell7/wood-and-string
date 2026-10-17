"""Bounded source-onset focus proposals, separate from recognition and rendering.

This declared local preference compares coherent decoder policies and recurring
pitch paths, including register-separated alternatives after a stationary core.
It is not a
main-riff recognizer, a physical voice assignment, or a duration repair.
The pure proposer has no I/O. The separate saved-context loader reads an
authenticated parent through the existing protected phrase-evidence reader.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib
from itertools import permutations
import json

VERSION = 'instrumental-focus-proposal-v1'
POLICY = 'policy-coherent-local-leap-and-pitch-cut-recurrence-v2'
MAX_INPUT_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 768 * 1024
MAX_OBSERVATIONS = 1024
MAX_FAMILIES = 256
MAX_VOICES = 8
MAX_COVERS = 16
MAX_ASSIGNMENTS = 8192
MAX_MOTIF_WINDOWS = 32768
MIN_MOTIF = 3
MAX_MOTIF = 16
POLICIES = ('published', 'onset_only')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _validate(context):
    try:
        raw = _canonical(context)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError) as error:
        raise ValueError('Finite bounded JSON evidence required') from error
    if len(raw) > MAX_INPUT_BYTES or type(context) is not dict or set(context) != {
            'version', 'source', 'scope', 'interpretations', 'coverage', 'binding'}:
        raise ValueError('Exact bounded focus context required')
    if json.loads(raw) != context:
        raise ValueError('Evidence must retain exact JSON types and string object keys')
    if context['version'] != 'instrumental-focus-input-v1':
        raise ValueError('Unsupported focus evidence version')
    source, scope, coverage = context['source'], context['scope'], context['coverage']
    if (type(source) is not dict or set(source) != {'id', 'sha256', 'sample_rate', 'origin', 'end'}
            or type(source['id']) is not str or not 1 <= len(source['id']) <= 256
            or type(source['sha256']) is not str or len(source['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in source['sha256'])
            or source['sample_rate'] != 44100 or type(source['sample_rate']) is not int
            or not _integer(source['origin'], 0, 2**53)
            or not _integer(source['end'], source['origin'] + 1, source['origin'] + 600 * 44100)):
        raise ValueError('Explicit44.1kHz source clock and identity required')
    if (type(scope) is not dict or set(scope) != {'start', 'end'}
            or not _integer(scope['start'], source['origin'], source['end'] - 1)
            or not _integer(scope['end'], scope['start'] + 1, source['end'])):
        raise ValueError('Focus scope must be inside the declared source')
    if (type(coverage) is not dict or set(coverage) != {
            'start', 'end', 'pitches', 'left_censored', 'right_censored'}
            or not _integer(coverage['start'], source['origin'], source['end'] - 1)
            or not _integer(coverage['end'], coverage['start'] + 1, source['end'])
            or type(coverage['pitches']) is not list or len(coverage['pitches']) > 128
            or any(not _integer(p, 0, 127) for p in coverage['pitches'])
            or len(set(coverage['pitches'])) != len(coverage['pitches'])
            or type(coverage['left_censored']) is not bool
            or type(coverage['right_censored']) is not bool
            or type(context['binding']) is not dict):
        raise ValueError('Explicit bounded trace coverage required')
    inventories = context['interpretations']
    if type(inventories) is not dict or set(inventories) != set(POLICIES):
        raise ValueError('Both complete decoder inventories required')
    count = 0
    for policy in POLICIES:
        events = inventories[policy]
        if type(events) is not list:
            raise ValueError('Decoder inventory must be an event list')
        count += len(events)
        if count > MAX_OBSERVATIONS:
            raise ValueError('Observation bound exceeded; no silent truncation')
        ids = set()
        for event in events:
            if (type(event) is not dict or not {'id', 'start', 'end', 'pitch'} <= set(event)
                    or set(event) & {'role', 'role_hint', 'essential', 'track', 'focus'}
                    or type(event['id']) is not str or not 1 <= len(event['id']) <= 200
                    or event['id'] in ids
                    or not _integer(event['start'], source['origin'], source['end'] - 1)
                    or not _integer(event['end'], event['start'] + 1, source['end'])
                    or not _integer(event['pitch'], 0, 127)):
                raise ValueError('Unique observations with exact clock/pitch required; role labels are not evidence')
            ids.add(event['id'])
    return raw


class _Search:
    def __init__(self, cancel):
        self.cancel = cancel
        self.assignments = 0
        self.windows = 0
        self.incomplete = []

    def check(self):
        if self.cancel():
            raise InterruptedError('Focus proposal cancelled')

    def step(self, kind):
        self.check()
        field, limit = ('assignments', MAX_ASSIGNMENTS) if kind == 'assignment' else ('windows', MAX_MOTIF_WINDOWS)
        if getattr(self, field) >= limit:
            reason = field + '_bound'
            if reason not in self.incomplete:
                self.incomplete.append(reason)
            return False
        setattr(self, field, getattr(self, field) + 1)
        return True


def _families(context, search):
    grouped = defaultdict(list)
    for policy in POLICIES:
        for event in context['interpretations'][policy]:
            search.check()
            grouped[(event['start'], event['pitch'])].append((policy, event))
    if len(grouped) > MAX_FAMILIES:
        raise ValueError('Attack-family bound exceeded; split the scope explicitly')
    families = []
    for index, (key, observations) in enumerate(sorted(grouped.items())):
        endings = defaultdict(list)
        for policy, event in observations:
            endings[event['end']].append({'policy': policy, 'event_id': event['id']})
        variants = [{'end_sample': end, 'observations': sorted(refs, key=lambda r: (r['policy'], r['event_id']))}
                    for end, refs in sorted(endings.items())]
        families.append({'id': f'a{index:04d}', 'start_sample': key[0], 'pitch': key[1],
            'observations': sorted([{'policy': p, 'event': deepcopy(e)} for p, e in observations],
                                   key=lambda r: (r['policy'], r['event']['id'])),
            'gate_variants': variants,
            'exact_alias_observations': sum(max(0, len(v['observations']) - 1) for v in variants),
            'boundary_state': 'competing_decoder_endings' if len(variants) > 1 else 'one_decoder_ending',
            'physical_attack_and_release': 'unverified', 'source_instrument': 'unknown'})
    return families


def _stationary_core(keys):
    cohorts = defaultdict(list)
    for start, pitch in keys:
        cohorts[start].append(pitch)
    simultaneous = [set(pitches) for pitches in cohorts.values() if len(pitches) >= 2]
    core = set.intersection(*simultaneous) if len(simultaneous) >= 3 else set()
    if len(core) < 2:
        core = set()
    # Repeated simultaneous membership is only a stationary-core preference.
    # No arbitrary chord-tone weaving is admitted as evidence of a moving voice.
    stationary = {key for key in keys if key[1] in core and len(cohorts[key[0]]) >= 2}
    remainder = [(start, tuple((start, pitch) for pitch in sorted(pitches) if (start, pitch) not in stationary))
                 for start, pitches in sorted(cohorts.items())]
    return stationary, [(start, values) for start, values in remainder if values], sorted(core)


def _canonical_cover(paths):
    return tuple(sorted(tuple(path) for path in paths if path))


def _path_covers(cohorts, search):
    if any(len(values) > MAX_VOICES for _, values in cohorts):
        search.incomplete.append('simultaneous_voice_bound')
        return []
    states = {(): 0}
    for _, attacks in cohorts:
        search.check()
        candidates = {}
        for paths, prior_cost in states.items():
            if not paths:
                candidates[_canonical_cover([(key,) for key in attacks])] = 0
                continue
            n = max(len(paths), len(attacks))
            for placement in permutations(range(n), len(attacks)):
                if not search.step('assignment'):
                    return []
                cost = prior_cost
                updated = [list(path) for path in paths] + [[] for _ in range(n - len(paths))]
                for key, slot in zip(attacks, placement):
                    if updated[slot]:
                        cost += abs(key[1] - updated[slot][-1][1])
                    updated[slot].append(key)
                cover = _canonical_cover(updated)
                candidates[cover] = min(cost, candidates.get(cover, cost))
        best = min(candidates.values())
        states = {cover: cost for cover, cost in candidates.items() if cost == best}
        if len(states) > MAX_COVERS:
            search.incomplete.append('equally_economical_cover_bound')
            return []
    return sorted(states)


def _motifs(path, search):
    """Exact onset-spacing/pitch-interval recurrence, not tempo fitting."""
    groups = defaultdict(list)
    for length in range(MIN_MOTIF, min(MAX_MOTIF, len(path) // 2) + 1):
        for index in range(len(path) - length + 1):
            if not search.step('window'):
                return []
            window = path[index:index + length]
            signature = tuple((start - window[0][0], pitch - window[0][1]) for start, pitch in window)
            groups[signature].append((index, window))
    found = []
    for signature, windows in groups.items():
        # Equal-length windows: earliest finishing selection gives the maximum
        # number of disjoint occurrences. No attack counts twice as recurrence.
        occurrences = []
        next_index = 0
        for index, window in windows:
            if index >= next_index:
                occurrences.append(window)
                next_index = index + len(window)
        if len(occurrences) < 2:
            continue
        moving = any(pitch_delta for _, pitch_delta in signature)
        score = (len(signature) * len(occurrences), len(signature), len(occurrences))
        found.append((score, moving, signature, tuple(occurrences)))
    if not found:
        return []
    # Compare within each path, never select a louder, higher or denser voice
    # over a different moving path merely because it contains more notes.
    best = max(value[0] for value in found)
    return [value for value in found if value[0] == best]


def propose_focus(context, *, cancel=lambda: False):
    """Return source-supported local focus hypotheses and an exhaustive ledger.

    The input is detached JSON evidence from both decoders. This pure boundary
    does not authenticate a media/archive association; its caller owns that
    check. A selected focus never chooses or edits a candidate's ending.
    """
    if not callable(cancel):
        raise ValueError('Cancellation callback required')
    search = _Search(cancel)
    search.check()
    raw = _validate(context)
    families = _families(context, search)
    by_key = {(f['start_sample'], f['pitch']): f for f in families}
    scope = context['scope']
    keys = sorted(key for key in by_key if scope['start'] <= key[0] < scope['end'])
    from instrumental_focus_alternatives import policy_patterns, MAX_SPLIT_COVERS_PER_POLICY
    raw_hypotheses, covers, stationary_rows, cuts = policy_patterns(
        context, keys, search, _stationary_core, _path_covers, _motifs)
    stationary = {tuple(key) for row in stationary_rows for key in row['attack_keys']}
    core = sorted({pitch for row in stationary_rows for pitch in row['pitches']})
    hypotheses = {}
    for (moving, occurrences), value in raw_hypotheses.items():
        hypotheses[(moving, occurrences)] = {
            'kind': 'recurring_moving_line' if moving else 'recurring_single_pitch_rhythm',
            'relative_pattern': [{'onset_delta_samples': s, 'pitch_delta': p} for s, p in value['signature']],
            'occurrences': [[by_key[node]['id'] for node in window] for window in occurrences],
            'attack_ids': [by_key[node]['id'] for node in sorted({node for window in occurrences for node in window})],
            'supports': value['supports'], 'supporting_policies': sorted(value['supporting_policies']),
            'within_path_score': list(value['score']),
            'meaning': 'A policy-coherent local candidate, not verified author intent or physical voice identity.'}
    ordered = sorted(hypotheses.items(), key=lambda value: value[0])
    proposed = []
    for index, (_, row) in enumerate(ordered):
        proposed.append(dict(row, id=f'h{index:04d}'))
    moving = [h for h in proposed if h['kind'] == 'recurring_moving_line']
    # Equivalent hypotheses with the same chosen attacks are not competitors.
    groups = defaultdict(set)
    for h in moving:
        groups[tuple(h['attack_ids'])].update(h['supporting_policies'])
    agreed = len(groups) == 1 and next(iter(groups.values())) == set(POLICIES)
    chosen = list(next(iter(groups))) if agreed and not search.incomplete else []
    in_scope = {by_key[key]['id'] for key in keys}
    outside = {f['id'] for f in families} - in_scope
    selected = set(chosen)
    secondary = in_scope - selected if selected else set()
    unresolved = set() if selected else in_scope
    state = ('search_incomplete' if search.incomplete else 'proposed' if selected else
             'competing_moving_lines' if len(groups) > 1 else
             'policy_disagreement' if groups else 'no_unique_moving_focus')
    coverage = context['coverage']
    missing = sorted({key[1] for key in keys} - set(coverage['pitches']))
    result = {'version': VERSION, 'policy': POLICY, 'state': state,
        'input_sha256': hashlib.sha256(raw).hexdigest(),
        'source_evidence': {key: deepcopy(context[key]) for key in ('source', 'scope', 'coverage', 'binding')},
        'attack_families': families,
        'hypotheses': proposed,
        'path_covers': [[[by_key[key]['id'] for key in path] for path in cover] for cover in covers],
        'stationary_core': {'pitches': core, 'attack_ids': sorted(by_key[key]['id'] for key in stationary),
            'policy_evidence': [{'policy': row['policy'], 'pitches': row['pitches'],
                'attack_ids': [by_key[tuple(key)]['id'] for key in row['attack_keys']]} for row in stationary_rows],
            'meaning': 'Per-policy stationary-cohort preference; union shown for inspection only, not verified accompaniment or noise.'},
        'ledger': {'selected': sorted(selected), 'secondary': sorted(secondary),
                   'unresolved': sorted(unresolved), 'outside_scope': sorted(outside)},
        'trace_coverage': {'missing_pitches': missing,
            'scope_covered': coverage['start'] <= scope['start'] and coverage['end'] >= scope['end'],
            'context_censored': coverage['left_censored'] or coverage['right_censored'],
            'used_for_line_ranking': False, 'physical_rest_or_dropout': 'unresolved'},
        'search': {'assignments': search.assignments, 'motif_windows': search.windows,
            'pitch_cuts_examined': cuts,
            'incomplete_reasons': list(search.incomplete),
            'limits': {'families': MAX_FAMILIES, 'voices': MAX_VOICES, 'covers': MAX_COVERS,
                       'assignments': MAX_ASSIGNMENTS, 'motif_windows': MAX_MOTIF_WINDOWS,
                       'pitch_cut_covers_per_policy': MAX_SPLIT_COVERS_PER_POLICY,
                       'minimum_motif_attacks': MIN_MOTIF, 'maximum_motif_attacks': MAX_MOTIF},
            'scope': 'Separate complete policies: sequential minimum-leap covers plus pitch-cut recurring alternatives with overlapping occurrence spans. Motif windows and span comparisons share one budget. No cross-policy recurrence construction or exhaustive voice search.'},
        'unchanged': {'source_observations': True, 'gate_variants': True,
            'source_instrument': 'unknown', 'physical_attack_and_release': 'unverified',
            'newly_composed_notes': 0, 'duration_repairs': 0},
        'limitations': ['Local recurrence is not whole-song form or the true main riff.',
            'Exact rhythm matching can miss expressive variation or tempo changes.',
            'Minimum pitch leaps can swap or join real voices; selected paths are hypotheses.',
            'Pitch cuts can split one wide-register line and miss crossing or interlocking voices. They preserve alternatives, not physical players.',
            'Agreement between two decoders of the same model is not independent musical confirmation.',
            'Stationary chords may themselves be the defining hook.',
            'Neither shared model evidence nor a gap proves a physical rest or a new pluck.',
            'No source instrument, string, fret, articulation, dynamics or physical gate is inferred.']}
    if len(_canonical(result)) > MAX_OUTPUT_BYTES:
        raise ValueError('Focus proposal exceeds output bound; split scope or reduce uninterpreted metadata explicitly')
    search.check()
    return result


class FocusEvidenceError(ValueError):
    """Saved evidence was refused; no source, saved job or proposal is edited."""


def load_focus_context(library, job_id, expected_parent_result_sha256, scope, *, cancel=lambda: False):
    """Bind full inventories and automatically derived pitches to a saved parent.

    Each batch uses the protected phrase reader. Complete raw traces remain in
    the original NPZ; compact receipts bind their exact reports, not new copies
    of every model cell. No library is constructed or written by this function.
    """
    from bisect import bisect_left, bisect_right
    import math
    import uuid
    import instrumental_phrase as phrase
    from instrumental_preview import canonical as parent_canonical, validate_bundle, source_anchors, MAX_RESULT

    def need(ok, message):
        if not ok:
            raise FocusEvidenceError(message)

    def check():
        if cancel():
            raise InterruptedError('Saved focus evidence cancelled')

    def digest(value):
        return hashlib.sha256(_canonical(value)).hexdigest()

    def is_sha(value):
        return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

    need(callable(cancel), 'Cancellation callback required')
    check()
    try:
        need(type(job_id) is str and str(uuid.UUID(job_id)) == job_id
             and is_sha(expected_parent_result_sha256), 'Exact parent identity and trusted result hash required')
        need(type(scope) is dict and set(scope) == {'start_sample', 'end_sample'}, 'Exact source-sample focus scope required')
        with library.connect() as db:
            sizes = db.execute('SELECT length(CAST(request AS BLOB)),length(CAST(result AS BLOB)) FROM jobs WHERE id=?', (job_id,)).fetchone()
        need(sizes is not None and len(sizes) == 2 and all(_integer(size, 1, MAX_RESULT) for size in sizes),
             'Saved parent exceeds metadata bound')
        job = library.job(job_id)
        need(job['id'] == job_id and job['state'] == 'complete' and job['request'].get('kind') == 'instrumental',
             'Completed instrumental review required')
        need(hashlib.sha256(parent_canonical(job['result'])).hexdigest() == expected_parent_result_sha256,
             'Trusted parent result identity differs')
        spec = job['request']
        times = [spec.get(key) for key in ('start_seconds', 'end_seconds')]
        need(all(type(t) in (int, float) and math.isfinite(t) for t in times)
             and 0 <= times[0] < times[1] <= times[0] + 10, 'Saved source passage differs')
        origin, end = (round(t * 44100) for t in times)
        raw_bundle = job['result']['candidates']
        bundle = validate_bundle(raw_bundle, source_id=raw_bundle['source_id'], source_sha256=spec['sha256'], origin=origin, end=end)
        start, stop = scope['start_sample'], scope['end_sample']
        need(_integer(start, origin, end - 1) and _integer(stop, start + 1, end), 'Focus scope exceeds saved source')
        inventories = {name: deepcopy(bundle['interpretations'][name]['events']) for name in POLICIES}
        need(sum(len(events) for events in inventories.values()) <= MAX_OBSERVATIONS, 'Complete inventories exceed focus observation bound')
        pitches = sorted({event['pitch'] for events in inventories.values() for event in events
                          if event['start'] < stop and event['end'] > start})
        need(pitches, 'No observed pitch overlaps this scope; no focus evidence was collected')
        reference = job['result']['evidence']
        expected_binding = {'authority': 'verified_saved_instrumental_job',
            'parent': {'job_id': job_id, 'result_sha256': expected_parent_result_sha256, 'request_sha256': digest(spec)},
            'bundle_sha256': digest(bundle),
            'source': {'id': bundle['source_id'], 'sha256': bundle['source_sha256']},
            'npz': reference, 'model_sha256': bundle['provenance']['model_sha256'],
            'pipeline_version': bundle['provenance']['pipeline_version'],
            'policies': {name: {'interpretation_sha256': digest(value),
                'policy_sha256': digest({k: value[k] for k in ('version', 'policy', 'settings', 'model_sha256') if k in value}),
                'declaration': {k: deepcopy(value[k]) for k in ('version', 'policy', 'settings', 'model_sha256') if k in value}}
                for name, value in bundle['interpretations'].items()}}
        anchors = source_anchors(origin, end)
        count = len(anchors) - 1
        first, last = min(count, bisect_right(anchors, start) - 1), min(count, bisect_left(anchors, stop))
        low, high = max(0, first - 2), min(count, last + 2)
        expected_frames = {'indices': list(range(low, high)), 'samples': anchors[low:high],
            'end_samples': anchors[low+1:high+1], 'in_query': [anchors[i] < stop and anchors[i+1] > start for i in range(low, high)]}
        receipts, array_hashes, clock = [], None, None
        for offset in range(0, len(pitches), 8):
            check()
            request = dict(scope, pitches=pitches[offset:offset+8], context_frames=2)
            report = phrase.load_phrase_boundary_evidence(library, job_id, expected_parent_result_sha256, request, cancel=cancel)
            need(len(_canonical(report)) <= phrase.MAX_REPORT, 'Phrase report exceeds its bound')
            need(report['version'] == phrase.VERSION and report['request'] == request, 'Phrase request/version differs')
            actual = report['binding']
            need(all(actual.get(key) == value for key, value in expected_binding.items()), 'Phrase report parent/source/policy binding differs')
            hashes = actual['array_sha256']
            need(type(hashes) is dict and set(hashes) == {'note', 'onset', 'contour'} and all(is_sha(v) for v in hashes.values()),
                 'Complete raw matrix identities required')
            if array_hashes is None:
                array_hashes, clock = deepcopy(hashes), deepcopy(report['clock'])
            need(hashes == array_hashes and report['clock'] == clock, 'Evidence changed between pitch batches')
            need(report['frames'] == expected_frames and clock['sample_rate'] == 44100
                 and clock['origin_sample'] == origin and clock['end_sample'] == end and clock['model_frames'] == count
                 and clock['context']['samples'] == [anchors[low], anchors[high]], 'Phrase source geometry differs')
            need(report['source_part'] == bundle['source_part'] and set(report['traces']) == {str(p) for p in request['pitches']},
                 'Missing competitor trace or changed source identity')
            for name, events in inventories.items():
                expected = [event for event in events if event['start'] < max(stop, anchors[high]) and event['end'] > min(start, anchors[low])]
                need([row['event'] for row in report['interpretations'][name]['events']] == expected, 'Phrase omitted or changed competing observations')
            receipts.append({'request': request, 'report_sha256': digest(report),
                'binding_sha256': digest(actual), 'frames_sha256': digest(report['frames']),
                'returned_rows': high - low, 'trace_pitches': request['pitches']})
        check()
        need(library.job(job_id) == job, 'Saved parent changed during focus collection')
        context = {'version': 'instrumental-focus-input-v1',
            'source': {'id': bundle['source_id'], 'sha256': bundle['source_sha256'], 'sample_rate': 44100, 'origin': origin, 'end': end},
            'scope': {'start': start, 'end': stop}, 'interpretations': inventories,
            'coverage': {'start': anchors[low], 'end': anchors[high], 'pitches': pitches,
                'left_censored': bool(clock['context']['left_missing_frames'] or clock['censoring']['at_parent_start']),
                'right_censored': bool(clock['context']['right_missing_frames'] or clock['censoring']['at_parent_end'] or clock['censoring']['uncovered_query_samples'])},
            'binding': {'authority': 'verified_saved_focus_collection', **{k: deepcopy(v) for k, v in expected_binding.items() if k != 'authority'},
                'array_sha256': array_hashes, 'collection_reports': receipts,
                'source_part': deepcopy(bundle['source_part']), 'source_censoring': deepcopy(clock['censoring']),
                'trace_retention': 'Complete matrices remain in the bound NPZ. Exact reports are bound by hashes and requests; no extra full trace archive.'}}
        _validate(context)
        check()
        return context
    except InterruptedError:
        raise
    except FocusEvidenceError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OSError, OverflowError, RecursionError, UnicodeError):
        raise FocusEvidenceError('Saved focus evidence failed verification; no proposal was adopted.') from None


def load_focus_proposal(library, job_id, expected_parent_result_sha256, scope, *, cancel=lambda: False):
    """Read authenticated saved evidence and return a separate focus proposal."""
    context = load_focus_context(library, job_id, expected_parent_result_sha256, scope, cancel=cancel)
    return propose_focus(context, cancel=cancel)
