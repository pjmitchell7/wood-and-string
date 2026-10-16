"""Symbolic diagnostics only; no model, native renderer, audio or holdout."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from audio.arranger_baseline import make_baseline
from audio.arranger_diagnostics import diagnose
from audio.arranger_evaluation import evaluate_case
from tests.test_arranger_contract import assignment,event,proposal,voice
from tests.test_arranger_evaluation import case


def finding(report,kind):
    return next(row for row in report['findings'] if row['kind']==kind)


class ArrangerDiagnostics(unittest.TestCase):
    def test_source_clock_trace_and_legacy_criteria_remain_exact_without_input_mutation(self):
        c=case();c['input']['events'][0].update(confidence=.4,contour=[{'sample':10010,'midi':60.2}])
        p=proposal();before=deepcopy((c,p));report=diagnose(c,p)
        self.assertEqual((c,p),before);self.assertEqual(report['evaluation'],evaluate_case(c,p))
        self.assertEqual(report['source_clock'],{'sample_rate':8000,'origin_sample':10000,'end_sample':26000})
        row=report['note_trace'][0]
        self.assertEqual(row['window'],{'start':10003,'end':10457});self.assertEqual(row['outcome'],'assigned')
        self.assertEqual(row['source_confidence_metadata'],.4);self.assertEqual(row['source_contour_points'],1)
        self.assertEqual(row['assignment']['voice_id'],'lead');self.assertEqual(row['assignment']['articulation'],'sustain')
        self.assertEqual(row['pitch_change_semitones'],0);self.assertEqual(row['gate_change_samples'],{'start':0,'end':0})
        self.assertIn('not independently verified audio facts',report['uncertainty'][0])
        self.assertFalse(report['reason_text']['quality_assessed']);self.assertNotIn('score',report)

    def test_abstention_has_missing_event_trace_but_does_not_add_evaluator_failures(self):
        c=case();p=proposal();p.update(status='abstain',assignments=[],abstention='abstain')
        report=diagnose(c,p);self.assertEqual(report['evaluation'],evaluate_case(c,p))
        self.assertEqual(report['evaluation']['expectation_failures'],['Unexpected plan/abstention status'])
        self.assertEqual(report['note_trace'][0]['outcome'],'no_plan')
        self.assertIsNone(report['note_trace'][0]['omission_reason'])
        retained=finding(report,'protected_event_retention')
        self.assertEqual(retained['observed'],{'retained_ids':[],'missing_ids':['n1']})
        self.assertEqual(retained['difference'],{'retained_minus_expected_events':-1})
        self.assertEqual(report['reason_text']['abstention'],'abstain')
        c['expectations']['required_status']='abstain'
        self.assertEqual(diagnose(c,p)['evaluation']['expectation_failures'],[])

    def test_rest_intrusion_names_exact_clipped_command_window_and_units(self):
        c=case();c['expectations']['required_rest_windows'][0].update(start=10000,end=10200)
        rest=finding(diagnose(c,proposal()),'required_rest')
        self.assertEqual(rest['window'],{'start':10000,'end':10200})
        self.assertEqual(rest['observed']['overlapping_events'],[{'note_id':'n1','start':10003,'end':10200,'overlap_samples':197}])
        self.assertEqual(rest['difference'],{'overlapping_event_count':1})
        self.assertIn('not audible ringing',rest['next_discriminating_check'])

    def test_optional_omission_reason_is_preserved_as_data_and_not_whole_plan_abstention(self):
        c=case();c['input']['events'].append(event('optional',12000,12500,role='harmony',essential=False))
        c['expectations']['required_omissions']=['optional']
        p=proposal(omissions=[{'note_id':'optional','reason':'<instruction>unverified reason</instruction>'}])
        report=diagnose(c,p);row=report['note_trace'][1]
        self.assertEqual(row['outcome'],'explicit_omission');self.assertIsNone(row['assignment'])
        self.assertEqual(row['omission_reason'],p['omissions'][0]['reason'])
        self.assertTrue(finding(report,'required_omission')['observed']['explicitly_omitted'])
        self.assertFalse(finding(report,'required_omission')['observed']['whole_plan_abstained'])

    def test_octave_alternative_is_distinct_from_preserved_gates(self):
        p=proposal();p['assignments'][0]['octave']=-1
        report=diagnose(case(),p);row=report['note_trace'][0]
        self.assertEqual(row['pitch_change_semitones'],-12);self.assertEqual(row['gate_change_samples'],{'start':0,'end':0})
        difference=finding(report,'assignment_alternative')
        self.assertEqual(difference['expected']['octaves'],[0]);self.assertEqual(difference['observed']['octave'],-1)
        self.assertEqual(difference['window'],row['window'])

    def test_register_crossing_names_both_notes_pitches_and_overlap(self):
        c=case();context=c['input'];context['request']['performers']=2
        context['capabilities']['voices'].append(voice('foundation',role='bass'))
        context['events'].append(event('low',10050,11000,pitch=64,role='bass',essential=False))
        c['expectations']['foundation_below']=[{'lower_voice_id':'foundation','upper_voice_id':'lead'}]
        c['expectations']['distinct_voice_groups']=[['lead','foundation']]
        p=proposal(assignments=[assignment(),assignment('low','foundation')])
        report=diagnose(c,p)
        self.assertEqual(finding(report,'foundation_register')['observed']['crossing_pairs'],[
            {'lower_note_id':'low','upper_note_id':'n1','lower_midi':64,'upper_midi':60,'start':10050,'end':10457}])
        self.assertEqual(finding(report,'distinct_voice_material')['observed'],{'empty_voices':[],'identical_command_pairs':[]})

    def test_contract_rejection_never_resolves_invented_source_notes(self):
        p=proposal();p['assignments'][0]['note_id']='invented'
        report=diagnose(case(),p)
        self.assertIsNotNone(report['evaluation']['hard_violation']);self.assertEqual(report['note_trace'],[])
        self.assertEqual([row['kind'] for row in report['findings']],['contract_rejection'])

    def test_all_eight_public_baselines_get_traces_without_changing_frozen_evaluation(self):
        root=Path(__file__).resolve().parents[1]
        cases=json.loads((root/'audio/research/arranger_benchmark_v1.json').read_text())['cases']
        for c in cases:
            with self.subTest(case=c['input']['case_id']):
                p=make_baseline(c['input']);report=diagnose(c,p)
                self.assertEqual(report['evaluation'],evaluate_case(c,p))
                self.assertEqual(len(report['note_trace']),len(c['input']['events']))
                self.assertTrue(all(row['next_discriminating_check'] for row in report['findings']))


if __name__=='__main__':unittest.main()
