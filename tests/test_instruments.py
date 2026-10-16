import tempfile
from pathlib import Path
import unittest
import hashlib
import zipfile
from dataclasses import replace
from audio.instruments import Bank, Event, mode, availability, installed_banks, validate_events, _verify_archive

class InstrumentContracts(unittest.TestCase):
    def setUp(self):
        self.banks={k:Bank(k,'test-only',True,'synthetic fixture',frozenset(range(128)),frozenset({'pluck'}))
                    for k in ('acoustic','electric','jazz-bass','drum-kit')}
    def event(self,**changes):
        return replace(Event('n1','acoustic-1',44100,45000,60,80,'pluck'),**changes)
    def validate(self,events,selection=None,**changes):
        return validate_events(selection or mode('acoustic',1),events,self.banks,
            **{'sample_rate':44100,'origin':44100,'end_sample':88200,**changes})
    def test_all_mode_combinations_and_stable_voices(self):
        for name in ('acoustic','electric','bass','ensemble'):
            for count in ((3,) if name=='ensemble' else (1,2)):
                for drums in (('none',) if name=='acoustic' else ('none','hats','snare','both')):
                    value=mode(name,count,drums)
                    self.assertEqual(value,mode(name,count,drums))
                    self.assertEqual(len([v for v in value.voices if v.instrument!='drum-kit']),count)
        self.assertEqual([v.instrument for v in mode('bass',2).voices],['jazz-bass']*2)
        self.assertEqual(mode('bass',1).voices[0].responsibility,'melody')
        self.assertEqual(mode('bass',1).voices[0].max_polyphony,4)
        self.assertEqual([v.responsibility for v in mode('bass',2).voices],['upper-counterline','foundation'])
        self.assertEqual({v.instrument for v in mode('ensemble',3).voices},{'acoustic','electric','jazz-bass'})
    def test_invalid_modes_and_no_acoustic_drums(self):
        for args in [('acoustic',1,'hats'),('ensemble',2),('bass',True),('electric',3),('unknown',1),('bass',1,'kick')]:
            with self.subTest(args=args),self.assertRaises(ValueError):mode(*args)
    def test_no_bank_and_no_adapter_cannot_claim_playable(self):
        with tempfile.TemporaryDirectory() as folder:
            banks=installed_banks(Path(folder))
            self.assertFalse(any(b.verified for b in banks.values()))
            self.assertFalse(availability(mode('acoustic',1),banks)['playable'])
        for name,n in [('electric',1),('bass',2),('ensemble',3)]:
            self.assertFalse(availability(mode(name,n),self.banks)['playable'])
        self.assertTrue(availability(mode('acoustic',2),self.banks)['playable'])

    def test_experimental_bass_adapter_requires_its_profile_and_moderate_velocity(self):
        from audio.growlybass import PROFILE_VERSION
        self.banks['jazz-bass']=replace(self.banks['jazz-bass'],bank_id=PROFILE_VERSION)
        self.assertTrue(availability(mode('bass',1),self.banks)['playable'])
        self.assertFalse(availability(mode('bass',1,'hats'),self.banks)['playable'])
        self.assertFalse(availability(mode('ensemble',3),self.banks)['playable'])
        note=self.event(voice_id='bass-1',pitch=33,velocity=120)
        self.assertTrue(self.validate([note],mode('bass',1)))
        with self.assertRaises(ValueError):self.validate([replace(note,velocity=121)],mode('bass',1))
    def test_source_clock_unchanged_and_overlap_not_quantized(self):
        events=[self.event(),self.event(id='n2',start=44101,end=45002,pitch=64)]
        before=list(events);self.assertTrue(self.validate(events));self.assertEqual(events,before)
        for changes in ({'start':44099},{'end':88201},{'start':True},{'velocity':0},{'pitch':128},{'articulation':'fake-slide'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.validate([self.event(**changes)])
        with self.assertRaises(ValueError):self.validate(list(reversed(events)))
        with self.assertRaises(ValueError):self.validate(events,end_sample=44100*32)
    def test_voice_identity_polyphony_and_doubled_duo(self):
        with self.assertRaises(ValueError):self.validate([self.event(voice_id='electric-1')])
        with self.assertRaises(ValueError):self.validate([self.event(id=str(i)) for i in range(7)])
        with self.assertRaises(ValueError):self.validate([self.event(),self.event(id='n2',voice_id='acoustic-2')],mode('acoustic',2))
        self.assertTrue(self.validate([self.event(),self.event(id='n2',voice_id='acoustic-2',pitch=48)],mode('acoustic',2)))
    def test_tampered_voice_contract_and_unverified_bank_rejected(self):
        value=mode('acoustic',1)
        with self.assertRaises(ValueError):availability(replace(value,voices=()),self.banks)
        self.banks['acoustic']=replace(self.banks['acoustic'],verified=False)
        with self.assertRaises(ValueError):self.validate([self.event()])

    def test_installed_bytes_must_match_pinned_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'provenance').mkdir();destination=root/'bank';destination.mkdir()
            archive=root/'provenance/test.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr('sample.bin',b'fixture')
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            (destination/'sample.bin').write_bytes(b'fixture')
            _verify_archive(root,'test.zip',digest,destination)
            (destination/'sample.bin').write_bytes(b'changed')
            with self.assertRaises(ValueError):_verify_archive(root,'test.zip',digest,destination)
            with self.assertRaises(ValueError):_verify_archive(root,'test.zip','0'*64,destination)

    def test_empty_part_and_wrong_drum_routing(self):
        with self.assertRaises(ValueError):self.validate([],mode('acoustic',1))
        self.banks['drum-kit']=replace(self.banks['drum-kit'],articulations=frozenset({'snare-center'}))
        notes=[self.event(voice_id='electric-1'),self.event(id='drum',voice_id='drums-hats',articulation='snare-center')]
        with self.assertRaises(ValueError):self.validate(notes,mode('electric',1,'hats'))

if __name__=='__main__': unittest.main()
