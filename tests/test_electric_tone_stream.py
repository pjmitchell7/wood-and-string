"""Authored arrays only. No native renderer/model, media files or listening."""
import array
import math
import unittest
from unittest.mock import patch
from audio import electric_tone as component
from audio import electric_tone_stream as stream


class ToneOverlap(unittest.TestCase):
    def test_short_input_is_exact_unchanged_component_output(self):
        samples=array.array('d',[.1,-.2,.3,.1]*30)
        settings=dict(mode='clean',ambience='room',ambience_mix=.1,gain_db=-3)
        expected=component.process(samples,settings=settings,source_origin_sample=800)['samples']
        actual=stream.process(samples,settings=settings,source_origin_sample=800)
        self.assertEqual(actual['samples'].tobytes(),expected.tobytes())
        self.assertEqual(actual['metadata']['source_origin_sample'],800)

    def test_join_impulses_all_parameter_corners_and_complete_tail(self):
        for low,high,mix,ambience in [(60,3500,0,'off'),(160,7500,.25,'room'),(60,7500,.25,'delay'),(160,3500,.25,'delay')]:
            settings=dict(mode='clean',low_cut_hz=low,high_cut_hz=high,ambience=ambience,ambience_mix=mix,gain_db=-6)
            support=stream.support(settings);window=support+500
            samples=array.array('d',[0.])*(2*(window+800))
            for frame,value in [(499,.1),(500,-.2),(501,.15),(1000,.1),(len(samples)//2-1,.2)]:
                samples[frame*2]=value;samples[frame*2+1]=-value*.3
            expected=component.process(samples,settings=settings)['samples'];before=samples.tobytes()
            with self.subTest(settings=settings),patch.object(component,'MAX_FRAMES',window):
                result=stream.process(samples,settings=settings)
            self.assertEqual(len(result['samples']),len(expected))
            self.assertLess(max(abs(a-b) for a,b in zip(result['samples'],expected)),3e-14)
            self.assertEqual(samples.tobytes(),before)
            prior=0
            for row in result['metadata']['windows']:
                self.assertEqual(row['retained_start'],prior);prior=row['retained_end']
                self.assertLessEqual(row['input_end']-row['input_start'],window)
            self.assertEqual(prior,len(expected)//2)

    def test_31_seconds_preserves_all_dry_frames_and_full_tail(self):
        samples=array.array('f',[0.,0.])*stream.MAX_FRAMES
        result=stream.process(samples,settings=dict(mode='clean',ambience='delay',ambience_mix=.25),source_origin_sample=5336117)
        self.assertEqual(len(result['samples'])//2,stream.MAX_FRAMES+component.MAX_TAIL)
        self.assertEqual(len(result['metadata']['windows']),2)
        self.assertTrue(all(value==0 for value in result['samples']))
        bypass=stream.process(samples,settings={})
        self.assertIs(bypass['samples'],samples)
        self.assertEqual(bypass['metadata']['phase'],'unchanged')

    def test_source_origin_boundary_prevalidates_all_later_windows(self):
        source=array.array('d',[.01,-.01])
        result=stream.process(source,settings={'mode':'clean'},source_origin_sample=stream.MAX_ORIGIN)
        self.assertEqual(result['metadata']['source_origin_sample'],stream.MAX_ORIGIN)
        with patch.object(component,'process') as process,self.assertRaises(ValueError):
            stream.process(source,settings={'mode':'clean'},source_origin_sample=stream.MAX_ORIGIN+1)
        process.assert_not_called()

    def test_full_assembly_refuses_overload_without_window_refusal_or_clipping(self):
        source=array.array('d',[16.,-16.])*40
        with self.assertRaises(component.ToneOverload) as error:
            stream.process(source,settings=dict(mode='clean',gain_db=6))
        self.assertTrue(error.exception.metadata['export_refused'])
        with self.assertRaisesRegex(ValueError,'refusal'):
            stream.process(source,settings=dict(mode='clean',overload='report'))

    def test_invalid_finite_bound_and_cancel_before_signal_allocation(self):
        with patch.object(component,'process') as process:
            for samples in (array.array('d',[1e308,0]),array.array('d',[float('nan'),0])):
                with self.assertRaises(ValueError):stream.process(samples,settings={'mode':'clean'})
            with self.assertRaises(InterruptedError):stream.process(array.array('d',[0,0]),settings={},cancel=lambda:True)
            process.assert_not_called()


if __name__=='__main__':unittest.main()
