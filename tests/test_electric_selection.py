"""Selection/planning only: no native bank, analysis, synthesis or playback."""
from copy import deepcopy
from dataclasses import asdict
import unittest
from unittest.mock import patch

from audio import preview_instruments as p
from audio.bass_performance import arrange_bass
from audio.electric_performance import arrange_electric
from audio.performance import Note, Performance
from audio.preferences import arrange_with_preferences, normalize_guidance


def performance(origin=0):
    return Performance('source-immutable',44100,[
        Note('melody',origin+1000,origin+16000,64,81,'melody',.7,'p1',((origin+1000,64.1),)),
        Note('bass',origin+1300,origin+15000,45,63,'bass',.6,'p1')],
        analysis_offset_samples=17,beats=[origin,origin+22101,origin+44097])


def electric(performers=1):
    return p.selection_fields({'instrument':'electric'},performers)


class ElectricSelection(unittest.TestCase):
    def test_acoustic_request_identity_stays_empty(self):
        for players in (1,2):
            self.assertEqual(p.selection_fields({},players),{})
            self.assertEqual(p.selection_fields({'instrument':'acoustic'},players),{})
            self.assertEqual(p.validate_saved_selection({'performers':players}),{})
            self.assertEqual(p.required_roles({},performers=players),('melody',))

    def test_bass_canonical_fields_and_saved_identity_are_unchanged(self):
        for players,part in ((1,'melody'),(1,'bass-line'),(2,'melody-and-bass')):
            expected=dict(instrument='jazz-bass',bass_part=part,
                          bass_planner_version=p.BASS_PLANNER_VERSION,bass_profile_version=p.BASS_PROFILE_VERSION)
            value=dict(instrument='jazz-bass',bass_part=part)
            self.assertEqual(p.selection_fields(value,players),expected)
            self.assertEqual(p.validate_saved_selection({**expected,'performers':players}),expected)
        self.assertEqual(p.selection_fields({'instrument':'jazz-bass'},1)['bass_part'],'melody')
        self.assertEqual(p.selection_fields({'instrument':'jazz-bass'},2)['bass_part'],'melody-and-bass')

    def test_electric_has_exact_distinct_version_fields(self):
        expected=dict(instrument='electric',electric_planner_version=p.ELECTRIC_PLANNER_VERSION,
                      electric_profile_version=p.ELECTRIC_PROFILE_VERSION)
        for players in (1,2):
            self.assertEqual(electric(players),expected)
            self.assertEqual(p.validate_saved_selection({**expected,'performers':players}),expected)

    def test_cross_instrument_fields_and_unavailable_modes_reject(self):
        for instrument,field in [('acoustic','bass_part'),('acoustic','electric_profile_version'),
                                 ('electric','bass_part'),('electric','bass_planner_version'),
                                 ('jazz-bass','electric_profile_version'),('jazz-bass','electric_planner_version')]:
            with self.subTest(instrument=instrument,field=field),self.assertRaises(ValueError):
                p.selection_fields({'instrument':instrument,field:None},1)
        for value in (None,[],{'instrument':'ensemble'},{'instrument':'electric-with-drums'}):
            with self.assertRaises(ValueError): p.selection_fields(value,1)
        for players in (True,0,3,1.0):
            with self.assertRaises(ValueError): p.selection_fields({},players)

    def test_saved_electric_missing_or_stale_versions_reject_before_planner(self):
        for field in ('electric_planner_version','electric_profile_version'):
            for stale in ('old',None):
                selection=electric();selection[field]=stale
                with self.subTest(field=field,stale=stale),self.assertRaisesRegex(ValueError,'Electric arrangement profile changed'):
                    p.validate_saved_selection({**selection,'performers':1})
                with patch.object(p,'arrange_electric') as planner,self.assertRaises(ValueError):
                    p.arrange_selected(performance(),1,{},selection)
                planner.assert_not_called()
            selection=electric();del selection[field]
            with self.assertRaises(ValueError): p.validate_saved_selection({**selection,'performers':1})

    def test_renderer_choice_is_explicit_not_truthiness(self):
        self.assertEqual(p.renderer_version({}),p.ACOUSTIC_RENDER_VERSION)
        self.assertEqual(p.renderer_version({'instrument':'acoustic'}),p.ACOUSTIC_RENDER_VERSION)
        self.assertEqual(p.renderer_version(p.selection_fields({'instrument':'jazz-bass'},1)),p.BASS_RENDER_VERSION)
        self.assertEqual(p.renderer_version(electric()),p.ELECTRIC_RENDER_VERSION)
        self.assertEqual(p.ELECTRIC_RENDER_VERSION,'electric-preview-dry-emily-v1')
        for value in (None,{'instrument':'ensemble'},{'instrument':[]}):
            with self.assertRaises(ValueError): p.renderer_version(value)

    def test_required_roles_preserve_bass_and_require_both_for_electric_duo(self):
        self.assertEqual(p.required_roles(electric()),('melody',))
        self.assertEqual(p.required_roles(electric(2),performers=2),('melody','bass'))
        for part,roles in [('melody',('melody',)),('bass-line',('bass',)),('melody-and-bass',('melody','bass'))]:
            selection={'instrument':'jazz-bass','bass_part':part}
            self.assertEqual(p.required_roles(selection),roles)
            self.assertEqual(p.required_roles(selection,performers=2),roles)

    def test_origin_is_passed_before_planning_without_source_retiming(self):
        origin=90*44100;source=performance(origin);before=deepcopy(asdict(source))
        with self.assertRaises(ValueError): arrange_electric(source,2)
        result=p.arrange_selected(source,2,{},electric(2),render_origin_sample=origin)
        self.assertEqual(result,arrange_electric(source,2,render_origin_sample=origin))
        self.assertEqual(result['render_origin_sample'],origin);self.assertTrue(result['renderable'])
        self.assertEqual(result['source'],before);self.assertEqual(asdict(source),before)
        self.assertEqual({n['player'] for n in result['notes']},{1,2})
        for note in result['notes']:
            original=next(n for n in before['notes'] if n['id']==note['id'])
            self.assertEqual(tuple(note[k] for k in ('start','end','pitch','velocity','contour')),
                             tuple(original[k] for k in ('start','end','pitch','velocity','contour')))
        self.assertIn('contour-not-rendered',{a['kind'] for a in result['adaptations']})

    def test_text_is_metadata_only_and_unsupported_accompaniment_rejects(self):
        source=performance();selection=electric();plain=p.arrange_selected(source,1,{},selection)
        text={'source_notes':'An uncertain guitar part','arrangement_notes':'Dreamy, keep the rhythm'}
        described=p.arrange_selected(source,1,text,selection)
        metadata=described.pop('user_guidance');self.assertEqual(described,plain)
        self.assertEqual(metadata['source_notes'],text['source_notes']);self.assertEqual(metadata['omitted_note_ids'],[])
        self.assertIn('Saved notes only',metadata['text_effect'])
        with self.assertRaisesRegex(ValueError,'melody-only accompaniment switch is not supported'):
            p.preview_guidance({'accompaniment':'melody-only'},1,selection)
        with self.assertRaises(ValueError): p.preview_guidance({'accompaniment':'melody-only'},2,selection)
        with self.assertRaises(ValueError): p.preview_guidance({'accompaniment':'drums'},1,selection)

    def test_unrenderable_planner_receipt_is_preserved_for_caller(self):
        source=Performance('overlap',44100,[Note(str(i),100,1000,64) for i in range(7)])
        expected=arrange_electric(source,1)
        self.assertFalse(expected['renderable']);self.assertTrue(expected['issues'])
        actual=p.arrange_selected(source,1,{},electric())
        self.assertEqual(actual,expected);self.assertEqual(actual['source'],asdict(source))

    def test_legacy_arrangements_ignore_new_origin_kwarg_and_keep_outputs(self):
        source=performance();guidance=normalize_guidance({},1)
        self.assertEqual(p.arrange_selected(source,1,{}, {},render_origin_sample=999999),
                         arrange_with_preferences(source,1,guidance))
        selection=p.selection_fields({'instrument':'jazz-bass'},1)
        expected=arrange_bass(source,1,solo_part='melody');expected['profile_version']=selection['bass_profile_version']
        self.assertEqual(p.arrange_selected(source,1,{},selection,render_origin_sample=999999),expected)
        self.assertEqual(p.preview_guidance({'accompaniment':'melody-only'},1,{})['accompaniment'],'melody-only')
        with self.assertRaisesRegex(ValueError,'Use the Bass part choice'):
            p.preview_guidance({'accompaniment':'melody-only'},1,selection)


if __name__=='__main__': unittest.main()
