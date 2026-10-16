import unittest
from audio.performance import Note,Performance,arrange,sampled_pitches,OPEN
from audio.diagnostic import fixture
from audio.render import string_sfz

class ArrangementTests(unittest.TestCase):
    def test_future_melody_reserved_before_sustained_harmony(self):
        p=Performance('entering-melody',48000,[Note('h',0,96000,84,role='harmony'),Note('m',48000,72000,84)])
        a=arrange(p)
        melody=next(n for n in a['notes'] if n['id']=='m')
        self.assertEqual(melody['pitch'],84)
        self.assertEqual(melody['string'],5)
        self.assertFalse(any(x['note_id']=='m' for x in a['adaptations']))
        self.assertTrue(any(x['note_id']=='h' for x in a['adaptations']))
    def test_timing_and_all_constraints(self):
        for count in (1,2):
            a=arrange(fixture(),count); source={n.id:n for n in fixture().notes}
            for n in a['notes']:
                self.assertEqual((n['start'],n['end']),(source[n['id']].start,source[n['id']].end))
                self.assertIn(n['pitch'],sampled_pitches()[n['string']])
                self.assertEqual(n['pitch'],OPEN[n['string']]+n['fret'])
                peers=[q for q in a['notes'] if q['player']==n['player'] and q['start']<=n['start']<q['end']]
                self.assertEqual(len(peers),len({q['string'] for q in peers}))
                frets=[q['fret'] for q in peers if q['fret']]
                self.assertLessEqual(len(frets),4)
                if frets:self.assertLessEqual(max(frets)-min(frets),4)
    def test_duet_is_roles_not_duplicate(self):
        a=arrange(fixture(),2)
        self.assertEqual(len(a['notes']),len({n['id'] for n in a['notes']}))
        for n in a['notes']: self.assertEqual(n['player'],1 if n['role']=='melody' else 2)
    def test_octave_change_is_disclosed(self):
        a=arrange(Performance('x',48000,[Note('hi',17,1000,88)]))
        self.assertTrue(any(x['kind']=='octave' for x in a['adaptations']))
    def test_omissions_are_disclosed(self):
        p=Performance('x',48000,[Note(str(i),0,48000,64+i,role='harmony') for i in range(12)])
        a=arrange(p); self.assertEqual(len(a['notes'])+sum(x['kind']=='omission' for x in a['adaptations']),12)
        self.assertTrue(any(x['kind']=='omission' for x in a['adaptations']))
    def test_bad_inputs_rejected(self):
        for p in [Performance('x',48000,[Note('x',-1,2,64)]),Performance('x',48000,[Note('x',0,2,64,confidence=float('nan'))])]:
            with self.assertRaises(ValueError):arrange(p)
        for n in (0,3):
            with self.assertRaises(ValueError):arrange(fixture(),n)
    def test_string_maps_have_no_other_strings(self):
        from audio.performance import NAMES
        import re
        for i,name in enumerate(NAMES):
            s=string_sfz(i)
            self.assertEqual(set(re.findall(r'String(\w+)_Vel',s)),{name})
            self.assertNotIn('lochan=',s)
            self.assertIn('pitch_random=0',s)

if __name__=='__main__':unittest.main()

