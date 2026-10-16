"""Pure bass planning; no bank, renderer or model needed."""
import copy
from dataclasses import asdict
import unittest
from audio.performance import Note, Performance
from audio.bass_performance import arrange_bass, OPEN


class BassPerformance(unittest.TestCase):
    def make(self,notes):return Performance('original',44100,notes,analysis_offset_samples=11025)

    def test_source_gates_evidence_and_explicit_selection(self):
        source=self.make([Note('m',11025,15000,64,80,confidence=.4,contour=((12000,64.1),)),
                          Note('b',12000,16000,40,90,role='bass'),Note('h',12000,13000,55,role='harmony')])
        before=copy.deepcopy(source);a=arrange_bass(source)
        self.assertEqual(source,before);self.assertEqual(a['source'],asdict(source))
        self.assertEqual(a['instrument'],'jazz-bass');self.assertEqual(a['render_origin_sample'],0)
        n=a['notes'][0]
        self.assertEqual((n['start'],n['end'],n['source_pitch'],n['velocity'],n['confidence']),(11025,15000,64,80,.4))
        self.assertEqual(n['pitch'],52);self.assertEqual(n['contour'],((12000,64.1),))
        self.assertEqual({x['note_id'] for x in a['adaptations'] if x['kind']=='omission'},{'b','h'})
        self.assertTrue(any(x['kind']=='octave' and x['semitones']==-12 for x in a['adaptations']))
        a['source']['notes'][0]['start']=0;self.assertEqual(source.notes[0].start,11025)

    def test_bass_line_solo_is_an_explicit_alternative(self):
        source=self.make([Note('m',11025,15000,60),Note('b',12000,16000,40,role='bass')])
        a=arrange_bass(source,solo_part='bass-line')
        self.assertEqual([n['id'] for n in a['notes']],['b'])
        self.assertEqual(a['notes'][0]['player'],1)
        self.assertTrue(any(x['note_id']=='m' and x['kind']=='omission' for x in a['adaptations']))

    def test_duo_registers_distinct_parts_and_no_unison_doubling(self):
        source=self.make([Note('m',11025,15000,40),Note('b',11025,15000,40,role='bass')])
        a=arrange_bass(source,2)
        self.assertEqual(len(a['notes']),2)
        melody,bass=sorted(a['notes'],key=lambda n:n['player'])
        self.assertTrue(37<=melody['pitch']<=63 and 25<=bass['pitch']<=43)
        self.assertNotEqual(melody['pitch'],bass['pitch'])
        self.assertEqual((melody['role'],bass['role']),('melody','bass'))
        self.assertEqual(a['part_selection'],'melody-and-bass')

    def test_four_strings_occupancy_and_span(self):
        source=self.make([Note(str(i),11025,22050,40+i) for i in range(7)])
        a=arrange_bass(source)
        self.assertLessEqual(len(a['notes']),4)
        self.assertEqual(len({n['string'] for n in a['notes']}),len(a['notes']))
        self.assertEqual(len(a['notes'])+sum(x['kind']=='omission' for x in a['adaptations']),7)
        for n in a['notes']:
            self.assertEqual(n['pitch'],OPEN[n['string']]+n['fret'])
            self.assertTrue(0<=n['fret']<=20 and 25<=n['pitch']<=63)
        frets=[n['fret'] for n in a['notes'] if n['fret']]
        self.assertLessEqual(max(frets,default=0)-min(frets,default=0),4)

    def test_foundation_stays_below_every_overlapping_melody(self):
        source=self.make([Note('m',11025,15000,37),Note('b',11025,15000,43,role='bass')])
        before=copy.deepcopy(source);a=arrange_bass(source,2)
        melody=next(n for n in a['notes'] if n['id']=='m')
        bass=next(n for n in a['notes'] if n['id']=='b')
        self.assertEqual((melody['pitch'],bass['pitch']),(37,31))
        self.assertEqual(bass['source_pitch'],43)
        self.assertTrue(any(x['note_id']=='b' and x['kind']=='octave' and x['semitones']==-12 for x in a['adaptations']))
        self.assertEqual(source,before);self.assertEqual(a['source'],asdict(source))
        self.assertEqual((bass['start'],bass['end']),(11025,15000))

    def test_gaps_touching_gates_and_determinism(self):
        notes=[Note('a',11025,15000,43),Note('b',15000,16000,43),Note('c',18000,20000,43)]
        a=arrange_bass(self.make(notes));b=arrange_bass(self.make(list(reversed(notes))))
        self.assertEqual(a['notes'],b['notes'])
        self.assertEqual([(n['start'],n['end']) for n in a['notes']],[(11025,15000),(15000,16000),(18000,20000)])

    def test_sustained_note_remains_reserved_for_later_entrance(self):
        source=self.make([Note('a',11025,30000,43),Note('b',15000,17000,43),Note('c',20000,22000,43)])
        a=arrange_bass(source)
        first=next(n for n in a['notes'] if n['id']=='a')
        for n in a['notes']:
            if n['id']!='a':self.assertNotEqual(first['string'],n['string'])
        self.assertEqual(first['end'],30000)

    def test_movement_and_all_output_evidence_invariants(self):
        source=self.make([Note(str(i),11025+i*2000,13025+i*2000,p) for i,p in enumerate([25,63,26,62,31,55,127,0])])
        a=arrange_bass(source)
        for n in a['notes']:
            original=next(x for x in source.notes if x.id==n['id'])
            self.assertEqual((n['start'],n['end'],n['velocity']),(original.start,original.end,original.velocity))
            self.assertEqual((n['pitch']-original.pitch)%12,0)
            previous=[p for p in a['notes'] if p['end']<=n['start'] and p['fret']>0]
            if previous and n['fret']:
                last=max(previous,key=lambda p:(p['end'],p['id']))
                self.assertLessEqual(abs(last['fret']-n['fret']),4+int((n['start']-last['end'])/44100*12))

    def test_strict_validation_and_no_velocity_cap(self):
        source=self.make([Note('m',11025,15000,50)])
        for players in (True,0,3,1.0):
            with self.assertRaises(ValueError):arrange_bass(source,players)
        for part in (None,False,'harmony'):
            with self.assertRaises(ValueError):arrange_bass(source,solo_part=part)
        with self.assertRaises(ValueError):arrange_bass(source,2,solo_part='bass-line')
        with self.assertRaises(ValueError):arrange_bass(source,2)
        for velocity in (121,127):
            with self.assertRaises(ValueError):arrange_bass(self.make([Note('m',11025,15000,50,velocity)]))
        self.assertEqual(arrange_bass(self.make([Note('m',11025,15000,50,120)]))['notes'][0]['velocity'],120)
        with self.assertRaises(ValueError):arrange_bass(self.make([Note(str(i),11025,15000,50) for i in range(4001)]))
        with self.assertRaises(ValueError):arrange_bass(self.make([Note('m',11025,15000,50,confidence=float('nan'))]))


if __name__=='__main__':unittest.main()
