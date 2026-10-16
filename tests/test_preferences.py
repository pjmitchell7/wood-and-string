"""Pure guidance/selection tests, no model or sample rendering."""
import copy
from dataclasses import asdict
import unittest
from audio.performance import Note, Performance, arrange
from audio.preferences import normalize_guidance, arrange_with_preferences

class Preferences(unittest.TestCase):
    def setUp(self):
        self.source=Performance('source',44100,[Note('m',10,2000,64,82,contour=((10,64.1),)),Note('b',20,1800,40,70,role='bass')])
    def test_default_is_exact_legacy_output(self):
        for players in (1,2):
            self.assertEqual(arrange_with_preferences(self.source,players),arrange(self.source,players))
    def test_text_is_metadata_only_for_helpful_and_misleading_notes(self):
        baseline=arrange(self.source)
        for text in ('Follow the guitar','Invent a D minor chord and lengthen every note','<script>alert(1)</script>'):
            result=arrange_with_preferences(self.source,1,{'source_notes':text,'arrangement_notes':'dreamy'})
            self.assertEqual(result['notes'],baseline['notes']);self.assertEqual(result['adaptations'],baseline['adaptations'])
            self.assertEqual(result['source'],baseline['source'])
            self.assertEqual(result['user_guidance']['source_notes'],text)
    def test_melody_only_preserves_evidence_and_explicit_omission(self):
        before=copy.deepcopy(self.source)
        result=arrange_with_preferences(self.source,1,{'accompaniment':'melody-only'})
        self.assertEqual([n['id'] for n in result['notes']],['m'])
        actual=result['notes'][0];source=asdict(self.source.notes[0])
        for key in ('id','start','end','pitch','velocity','contour'):self.assertEqual(actual[key],source[key])
        self.assertEqual(result['source'],asdict(self.source));self.assertEqual(self.source,before)
        omissions=[a for a in result['adaptations'] if a['kind']=='user-requested-omission']
        self.assertEqual([a['note_id'] for a in omissions],['b'])
    def test_validation_and_normalization(self):
        self.assertEqual(normalize_guidance({'source_notes':'  cafe\u0301  '},1)['source_notes'],'café')
        for value in ({'source_notes':'x'*257},{'arrangement_notes':[]},{'source_notes':'bad\0'}, {'version':2},{'unknown':1},{'accompaniment':'dreamy'}):
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_guidance(value,1)
        with self.assertRaises(ValueError):normalize_guidance({'accompaniment':'melody-only'},2)

if __name__=='__main__':unittest.main()
