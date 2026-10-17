"""Policy-coherent recurrence and a bounded register-separation alternative.

This symbolic heuristic preserves exact candidate onsets and all observations.
A pitch cut is a competing explanation, never evidence of physical players.
"""

POLICIES = ('published', 'onset_only')
MAX_PITCH_CUTS_PER_POLICY = 127
MAX_SPLIT_COVERS_PER_POLICY = 16


def _overlap(left, right, search):
    """At least one pair of recurring occurrence spans strictly overlaps."""
    for a in left:
        for b in right:
            if not search.step('window'):
                return False
            if a[0][0] < b[-1][0] and b[0][0] < a[-1][0]:
                return True
    return False


def _split_patterns(keys, search, motifs):
    """Inspect every pitch gap, without an onset tolerance or register winner."""
    pitches = sorted({pitch for _, pitch in keys})
    found = []
    examined = 0
    split_covers = set()
    for cut in pitches[:-1]:
        search.check()
        if search.incomplete:
            break
        examined += 1
        low = tuple(key for key in keys if key[1] <= cut)
        high = tuple(key for key in keys if key[1] > cut)
        # Three attacks per occurrence, at least two occurrences per side.
        if min(len(low), len(high)) < 6:
            continue
        a = [row for row in motifs(low, search) if row[1]]
        b = [row for row in motifs(high, search) if row[1]]
        if search.incomplete:
            break
        accepted_a, accepted_b = set(), set()
        for x in a:
            for y in b:
                if _overlap(x[3], y[3], search):
                    accepted_a.add(x); accepted_b.add(y)
                if search.incomplete:
                    return found, examined
        if accepted_a:
            cover = tuple(sorted((low, high)))
            split_covers.add(cover)
            if len(split_covers) > MAX_SPLIT_COVERS_PER_POLICY:
                search.incomplete.append('pitch_cut_cover_bound')
                return found, examined
            for path, accepted in ((low, accepted_a), (high, accepted_b)):
                for row in sorted(accepted):
                    found.append((cover, path, row, cut))
    return found, examined


def policy_patterns(context, keys, search, stationary_core, path_covers, motifs):
    """Search complete interpretations separately and retain their support.

    All callbacks are bounded pure helpers from the validated proposer. No
    cross-policy union is used to construct a path or recurrence occurrence.
    """
    available = set(keys)
    records, covers, stationary_rows = [], set(), []
    cuts = 0
    for policy in POLICIES:
        search.check()
        policy_keys = sorted({(event['start'], event['pitch'])
                              for event in context['interpretations'][policy]} & available)
        stationary, cohorts, core = stationary_core(policy_keys)
        stationary_rows.append({'policy': policy, 'pitches': core,
                                'attack_keys': sorted(stationary)})
        for cover in path_covers(cohorts, search):
            covers.add(cover)
            for path in cover:
                for row in motifs(path, search):
                    records.append((policy, 'minimum_leap', cover, path, row, None))
        remaining = sorted(set(policy_keys)-stationary)
        alternatives, examined = _split_patterns(remaining, search, motifs)
        cuts += examined
        for cover, path, row, cut in alternatives:
            covers.add(cover)
            records.append((policy, 'pitch_cut_recurrence', cover, path, row, cut))
    ordered_covers = sorted(covers)
    cover_ids = {cover: index for index, cover in enumerate(ordered_covers)}
    hypotheses = {}
    for policy, method, cover, path, (score, moving, signature, occurrences), cut in records:
        search.check()
        key = moving, occurrences
        row = hypotheses.setdefault(key, {'signature': signature, 'score': score,
                                         'supports': [], 'supporting_policies': set()})
        support = {'policy': policy, 'method': method, 'cover': cover_ids[cover],
                   'path': cover.index(path)}
        if cut is not None:
            support['pitch_cut_after'] = cut
        if support not in row['supports']:
            row['supports'].append(support)
        row['supporting_policies'].add(policy)
    return hypotheses, ordered_covers, stationary_rows, cuts
