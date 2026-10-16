import copy
import unittest
from unittest.mock import patch

from audio.arranger_baseline import make_baseline
from audio.arranger_contract import validate_proposal
from tests.test_arranger_contract import context, event, voice


class Baseline(unittest.TestCase):
    def test_deterministic_source_exact_and_role_preference(self):
        c=context();c['events'] += [event('again',11003,11457)]
        before=copy.deepcopy(c);p=make_baseline(c)
        self.assertEqual(p,make_baseline(c));self.assertEqual(c,before)
        r=validate_proposal(c,p);self.assertEqual(p['status'],'plan')
        self.assertEqual([(n['start'],n['end'],n['pitch'],n['velocity']) for n in r['notes']],
                         [(10003,10457,60,80),(11003,11457,60,80)])

    def test_optional_unplayable_is_omitted_not_essential(self):
        c=context();c['events'].append(event('too-low',12000,12500,1,'bass',False))
        p=make_baseline(c);self.assertEqual(p['status'],'plan')
        self.assertEqual([o['note_id'] for o in p['omissions']],['too-low'])
        c['events'][1]['essential']=True
        self.assertEqual(make_baseline(c)['status'],'abstain')

    def test_overlap_search_uses_distinct_strings(self):
        c=context();c['events'].append(event('harmony',10004,10450,48,'harmony',False))
        p=make_baseline(c);r=validate_proposal(c,p)
        self.assertEqual(p['status'],'plan');self.assertEqual(len(r['notes']),2)
        self.assertEqual(len({n['string'] for n in r['notes']}),2)
        self.assertEqual({n['pitch'] for n in r['notes']},{48,60})

    def test_bass_duo_octave_below_later_melody(self):
        c=context();c['request']=dict(mode='bass',performers=2,drums=False)
        c['capabilities']['voices']=[voice('lead','jazz-bass'),voice('low','jazz-bass','bass')]
        c['events']=[event(start=11000,end=12000,pitch=48),event('low',10000,13000,55,'bass')]
        p=make_baseline(c);r=validate_proposal(c,p)
        self.assertEqual(p['status'],'plan')
        notes={n['id']:n for n in r['notes']}
        self.assertLess(notes['low']['pitch'],notes['n1']['pitch'])
        self.assertEqual(notes['n1']['pitch'],48)
        self.assertEqual(notes['low']['pitch'],43)

    def test_all_modes_with_ensemble_and_real_drum_evidence(self):
        c=context();c['request']=dict(mode='ensemble',performers=3,drums=True)
        c['capabilities']['voices']=[voice(),voice('counter','electric','counterline'),
                                    voice('low','jazz-bass','bass'),voice('kit','drums','drums')]
        c['events'] += [event('counter',11000,11400,55,'counterline'),event('low',12000,12400,40,'bass'),
                        event('hit',13000,13200,42,'drums')]
        p=make_baseline(c);self.assertEqual(p['status'],'plan')
        r=validate_proposal(c,p)
        self.assertTrue(all(n['role']==n['source_role_hint'] for n in r['notes']))
        for mode,instrument in [('electric','electric'),('bass','jazz-bass')]:
            c=context();c['request']['mode']=mode;c['capabilities']['voices'][0]['instrument']=instrument
            self.assertEqual(make_baseline(c)['status'],'plan')

    def test_blocking_empty_voice_and_duplicate_stream_abstain(self):
        c=context();c['missing_facts']=[dict(id='missing',description='Unknown defining pitch',blocking=True)]
        self.assertEqual(make_baseline(c)['status'],'abstain')
        c=context();c['request']['performers']=2
        c['capabilities']['voices'].append(voice('other','acoustic','harmony'))
        self.assertEqual(make_baseline(c)['status'],'abstain')

    def test_limits_and_dynamics_never_silently_scaled(self):
        c=context();c['capabilities']['voices'][0]['velocity_range']=[1,79]
        self.assertEqual(make_baseline(c)['status'],'abstain')
        c=context()
        with patch('audio.arranger_baseline.MAX_EXPANSIONS',0):
            p=make_baseline(c)
        self.assertEqual(p['status'],'abstain');self.assertIn('budget',p['abstention'])
        validate_proposal(c,p)

    def test_invalid_context_fails_instead_of_becoming_abstention(self):
        c=context();c['events'][0]['start']=False
        with self.assertRaises(ValueError):make_baseline(c)

    def test_bass_snare_same_key_and_gates_never_force_octave(self):
        c=context();c['request']=dict(mode='bass',performers=1,drums=True)
        c['capabilities']['voices']=[voice('low','jazz-bass','bass'),voice('kit','drums','drums')]
        c['events']=[event('low',pitch=38,role='bass'),event('snare',pitch=38,role='drums')]
        before=copy.deepcopy(c);p=make_baseline(c);r=validate_proposal(c,p)
        self.assertEqual(p['status'],'plan');self.assertEqual(c,before)
        self.assertEqual({a['octave'] for a in p['assignments']},{0})
        self.assertEqual({(n['start'],n['end'],n['pitch']) for n in r['notes']},{(10003,10457,38)})
        self.assertEqual(p['omissions'],[])


if __name__=='__main__':unittest.main()
