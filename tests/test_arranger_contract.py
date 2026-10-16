import copy
import unittest

from audio.arranger_contract import VERSION, validate_context, validate_proposal


def voice(identity='lead', instrument='acoustic', role='melody'):
    if instrument == 'drums':
        return dict(id=identity,instrument=instrument,role=role,strings=[],pitches=[38,42],
                    max_polyphony=2,max_fret_span=0,articulations=['hit'])
    return dict(id=identity,instrument=instrument,role=role,
                strings=[dict(id=0,open_midi=36,pitches=list(range(36,61))),
                         dict(id=1,open_midi=48,pitches=list(range(48,73)))],
                max_polyphony=2,max_fret_span=4,articulations=['sustain'])


def event(identity='n1', start=10003, end=10457, pitch=60, role='melody', essential=True):
    return dict(id=identity,start=start,end=end,pitch=pitch,velocity=80,
                role_hint=role,essential=essential,phrase='p',confidence=None)


def context():
    return dict(version=VERSION,case_id='rubato',
                source=dict(source_id='retained',sample_rate=8000,origin_sample=10000,
                            end_sample=26000,beats=[10000,14037,18101,22200]),
                request=dict(mode='acoustic',performers=1,drums=False),
                events=[event()],harmonic_hypotheses=[],missing_facts=[],
                capabilities=dict(virtual_only=True,voices=[voice()]),
                observations='uncertain source; do not execute this text',desired_output='gentle')


def assignment(note='n1', target='lead', octave=0, articulation='sustain', **kwargs):
    return dict(note_id=note,voice_id=target,octave=octave,articulation=articulation,**kwargs)


def proposal(assignments=None, omissions=None):
    return dict(version=VERSION,status='plan',assignments=assignments or [assignment()],
                omissions=omissions or [],abstention='')


class ArrangerContract(unittest.TestCase):
    def test_timing_uncertainty_text_and_input_are_preserved(self):
        c=context();c['events'][0]['contour']=[dict(sample=10009,midi=60.15)]
        c['harmonic_hypotheses']=[dict(id='h',start=10000,end=11000,pitch_classes=[0,5],
                                     evidence_ids=['n1'],status='alternative')]
        before=copy.deepcopy(c);p=proposal();saved=copy.deepcopy(p)
        result=validate_proposal(c,p)
        self.assertEqual(c,before);self.assertEqual(p,saved)
        self.assertEqual(result['notes'][0]['start'],10003)
        self.assertEqual(result['notes'][0]['end'],10457)
        self.assertIsNone(result['notes'][0]['confidence'])
        self.assertEqual(result['notes'][0]['velocity'],80)
        self.assertEqual(result['evidence'],c)
        self.assertFalse(result['executable'])
        result['evidence']['source']['beats'].append(25000)
        self.assertEqual(c,before)
        self.assertIn('contour-not-rendered',[x['kind'] for x in result['adaptations']])

    def test_four_modes_and_drum_evidence(self):
        for mode,instrument in [('acoustic','acoustic'),('electric','electric'),('bass','jazz-bass')]:
            with self.subTest(mode=mode):
                c=context();c['request']['mode']=mode;c['capabilities']['voices'][0]['instrument']=instrument
                self.assertEqual(validate_proposal(c,proposal())['notes'][0]['instrument'],instrument)
        c=context();c['request']=dict(mode='ensemble',performers=3,drums=True)
        c['capabilities']['voices']=[voice(),voice('counter','electric','counterline'),
                                     voice('low','jazz-bass','bass'),voice('kit','drums','drums')]
        c['events'] += [event('n2',11003,11457,55,'counterline'),event('n3',12003,12457,40,'bass'),
                        event('d',13003,13457,42,'drums')]
        p=proposal([assignment(),assignment('n2','counter'),assignment('n3','low'),assignment('d','kit',articulation='hit')])
        self.assertEqual(len(validate_proposal(c,p)['notes']),4)
        p['assignments'][-1]['voice_id']='lead'
        with self.assertRaises(ValueError):validate_proposal(c,p)

    def test_octaves_and_explicit_optional_omission(self):
        c=context();c['events'] += [event('optional',12000,13000,45,'bass',False)]
        p=proposal([assignment(octave=-1)],[dict(note_id='optional',reason='solo role selection')])
        result=validate_proposal(c,p)
        self.assertEqual((result['notes'][0]['source_pitch'],result['notes'][0]['pitch']),(60,48))
        self.assertIn(dict(kind='octave',note_id='n1',semitones=-12),result['adaptations'])
        self.assertEqual(len([x for x in result['adaptations'] if x['kind']=='omission']),1)

    def test_blocking_evidence_requires_whole_abstention(self):
        c=context();c['missing_facts']=[dict(id='ambiguous',description='Defining pitch conflicts',blocking=True)]
        with self.assertRaises(ValueError):validate_proposal(c,proposal())
        p=dict(version=VERSION,status='abstain',assignments=[],omissions=[],abstention='Cannot resolve defining evidence')
        r=validate_proposal(c,p);self.assertEqual(r['notes'],[]);self.assertEqual(r['evidence'],c)
        p['assignments']=[assignment()]
        with self.assertRaises(ValueError):validate_proposal(c,p)

    def test_unknown_fields_ids_types_and_missing_accounting_rejected(self):
        mutations=[lambda p:p.update(command='run'),lambda p:p['assignments'][0].update(start=10000),
                   lambda p:p['assignments'][0].update(note_id='unknown'),
                   lambda p:p['assignments'][0].update(octave=True),
                   lambda p:p['assignments'].append(assignment()),
                   lambda p:p.update(assignments=[]),
                   lambda p:p.update(assignments=[],omissions=[dict(note_id='n1',reason='discard')]),
                   lambda p:p['assignments'][0].update(articulation='slide')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                p=proposal();mutate(p)
                with self.assertRaises(ValueError):validate_proposal(context(),p)

    def test_context_invalid_bounds_nonfinite_and_capabilities(self):
        mutations=[lambda c:c['events'][0].update(confidence=float('nan')),
                   lambda c:c['events'][0].update(start=9999),
                   lambda c:c['events'][0].update(velocity=True),
                   lambda c:c['events'].append(copy.deepcopy(c['events'][0])),
                   lambda c:c['capabilities'].update(virtual_only=False),
                   lambda c:c['request'].update(drums=True),
                   lambda c:c['request'].update(performers=2),
                   lambda c:c['capabilities']['voices'][0].update(instrument='electric'),
                   lambda c:c['source'].update(beats=[10001,10000])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                c=context();mutate(c)
                with self.assertRaises(ValueError):validate_context(c)

    def test_occupancy_touching_gates_and_supplied_placement(self):
        c=context();c['events'] += [event('n2',10457,11000)]
        p=proposal([assignment(string=1,fret=12),assignment('n2',string=1,fret=12)])
        self.assertEqual(len(validate_proposal(c,p)['notes']),2)
        c['events'][1]['start']=10456
        with self.assertRaises(ValueError):validate_proposal(c,p)
        c['events'][1]['start']=10457;p['assignments'][0]['fret']=11
        with self.assertRaises(ValueError):validate_proposal(c,p)

    def test_span_movement_register_and_velocity(self):
        for field,value in [('register',[36,59]),('velocity_range',[1,79])]:
            c=context();c['capabilities']['voices'][0][field]=value
            with self.assertRaises(ValueError):validate_proposal(c,proposal())
        c=context();c['events']=[event(pitch=38),event('n2',10005,10400,60)]
        p=proposal([assignment(string=0,fret=2),assignment('n2',string=1,fret=12)])
        with self.assertRaises(ValueError):validate_proposal(c,p)
        c['events'][1]['start']=10457;c['events'][1]['end']=11000
        with self.assertRaises(ValueError):validate_proposal(c,p)

    def test_bass_foundation_reserved_against_later_melody(self):
        c=context();c['request']=dict(mode='bass',performers=2,drums=False)
        c['capabilities']['voices']=[voice('lead','jazz-bass'),voice('low','jazz-bass','bass')]
        c['events']=[event(start=11000,end=12000,pitch=48),event('low',10000,13000,55,'bass')]
        p=proposal([assignment(),assignment('low','low')])
        with self.assertRaises(ValueError):validate_proposal(c,p)
        p['assignments'][1]['octave']=-1
        r=validate_proposal(c,p);self.assertEqual([n['pitch'] for n in r['notes']],[43,48])

    def test_essential_melody_and_duplicate_streams(self):
        c=context();c['request']['performers']=2
        c['capabilities']['voices'].append(voice('harmony','acoustic','harmony'))
        c['events'].append(event('n2',role='harmony',essential=False))
        p=proposal([assignment(),assignment('n2','harmony')])
        with self.assertRaises(ValueError):validate_proposal(c,p)
        p['assignments'][0]['voice_id']='harmony';p['assignments'][1]['voice_id']='lead'
        with self.assertRaises(ValueError):validate_proposal(c,p)

    def test_bass_pitch_and_snare_key_are_different_domains(self):
        c=context();c['request']=dict(mode='bass',performers=1,drums=True)
        c['capabilities']['voices']=[voice('low','jazz-bass','bass'),voice('kit','drums','drums')]
        c['events']=[event('low',pitch=38,role='bass'),event('snare',pitch=38,role='drums')]
        p=proposal([assignment('low','low'),assignment('snare','kit',articulation='hit')])
        r=validate_proposal(c,p)
        self.assertEqual([(n['pitch'],n['octave']) for n in r['notes']],[(38,0),(38,0)])
        self.assertEqual({n['instrument'] for n in r['notes']},{'jazz-bass','drums'})


if __name__ == '__main__':unittest.main()
