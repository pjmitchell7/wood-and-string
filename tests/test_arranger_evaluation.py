import copy
import unittest

from audio.arranger_evaluation import evaluate_case
from tests.test_arranger_contract import context, proposal


def case():
    return {'input':context(),'expectations':{'required_status':'plan','protected_ids':['n1'],
        'required_omissions':[],'allowed_assignments':[{'note_id':'n1','voice_ids':['lead'],'octaves':[0],'articulations':['sustain']}],
        'required_rest_windows':[{'voice_id':'lead','start':11000,'end':12000}],
        'distinct_voice_groups':[],'foundation_below':[],'preserve_all_source_gates':True,
        'preserve_source_velocity':True,'preserve_harmonic_alternatives':True,'diagnostics':[]}}


class ArrangerEvaluation(unittest.TestCase):
    def test_valid_contract_and_case_expectations_are_separate(self):
        c=case();before=copy.deepcopy(c);p=proposal()
        result=evaluate_case(c,p)
        self.assertIsNone(result['hard_violation']);self.assertEqual(result['expectation_failures'],[])
        self.assertEqual(result['protected_retained'],1);self.assertFalse(result['music_quality_assessed'])
        self.assertEqual(c,before)
        p['assignments'][0]['octave']=-1
        result=evaluate_case(c,p)
        self.assertIsNone(result['hard_violation']);self.assertTrue(result['expectation_failures'])
        self.assertEqual(result['octave_changes'],[{'note_id':'n1','semitones':-12}])

    def test_abstaining_does_not_pass_a_required_plan(self):
        p=proposal();p.update(status='abstain',assignments=[],abstention='No valid evidence claimed')
        r=evaluate_case(case(),p)
        self.assertIsNone(r['hard_violation']);self.assertTrue(r['expectation_failures'])
        self.assertEqual(r['protected_retained'],0);self.assertTrue(r['manual_text_review_required'])
        c=case();c['expectations']['required_status']='abstain'
        self.assertEqual(evaluate_case(c,p)['expectation_failures'],[])

    def test_malformed_proposal_is_a_hard_violation(self):
        p=proposal();p['assignments'][0]['note_id']='invented'
        r=evaluate_case(case(),p);self.assertIsNotNone(r['hard_violation'])
        self.assertEqual(r['assigned_events'],0)

    def test_required_rest_and_omission_have_explicit_failures(self):
        c=case();c['expectations']['required_rest_windows'][0].update(start=10000,end=12000)
        c['expectations']['required_omissions']=['n1']
        r=evaluate_case(c,proposal())
        self.assertIn('Required explicit omissions missing',r['expectation_failures'])
        self.assertIn('Source gate occupies required rest: lead',r['expectation_failures'])


if __name__=='__main__':unittest.main()
