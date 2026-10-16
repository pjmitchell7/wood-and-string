"""Authored float/PCM observer tests. No bank, native renderer or playback."""
import array
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest

from audio.acoustic_stem_capture import AcousticStemCapture, forecast_capture, MAX_FRAMES
from audio.render import _write_pcm16, RENDER_VERSION
from stem_evidence import verify_bundle

RATE = 44100
FRAMES = RATE + 11
ORIGIN = 987654


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(performers=2):
    notes = [dict(id='lead-a', start=ORIGIN, end=ORIGIN+8, pitch=64, velocity=80,
                  role='melody', player=1, string=5, fret=0),
             dict(id='lead-b', start=ORIGIN+2, end=ORIGIN+11, pitch=59, velocity=80,
                  role='harmony', player=1, string=4, fret=0)]
    if performers == 2:
        notes.append(dict(id='backing-a', start=ORIGIN, end=ORIGIN+9, pitch=40, velocity=80,
                          role='bass', player=2, string=0, fret=0))
    source = {'id': 'authored-original', 'sha256': hashlib.sha256(b'authored unchanged source identity').hexdigest()}
    arrangement = dict(version=1, source_id=source['id'], sample_rate=RATE, performers=performers,
                       render_origin_sample=ORIGIN, notes=notes, adaptations=[],
                       source=dict(source_id=source['id'], sample_rate=RATE, notes=deepcopy(notes)))
    return source, arrangement


def start_capture(root, *, performers=2, arrangement=None, cancel=lambda: False, allowance=None):
    source, standard = fixture(performers)
    return AcousticStemCapture(root/'capture', source=source, arrangement=standard if arrangement is None else arrangement,
        revision='authored-revision-1', performers=list(range(1, performers+1)), origin_sample=ORIGIN,
        total_frames=FRAMES, max_work_bytes=forecast_capture(performers=performers, frames=FRAMES)['additional_disk_bytes'] if allowance is None else allowance,
        cancel=cancel)


def authored_render(root, capture=None, *, performers=2, opposed=False, policy='peak'):
    """Fake native instance outputs plus the exact existing master sum/quantizer.

    Deliberately leave most frames silent, with a nonzero last tail frame.
    Capture observes arrays before their reuse; the master path never consults it.
    """
    _, arrangement = fixture(performers)
    instances = sorted({(note['player'], note['string']) for note in arrangement['notes']})
    spool, partial = root/'master.f32', root/'master.wav'
    raw_peak = 0.
    with spool.open('wb') as target:
        for start in range(0, FRAMES, 256):
            count = min(256, FRAMES-start)
            if capture:
                capture.begin_block(start, count)
            mix = [0.] * (2*count)
            for player, string in instances:
                amplitude = {(1, 4):.02, (1, 5):.1, (2, 0):(-.11 if opposed else .05)}[(player, string)]
                left = array.array('f', [amplitude if start+i < 12 else amplitude*.1 if start+i == FRAMES-1 else 0. for i in range(count)])
                right = array.array('f', [-value*.75 for value in left])
                before = (left.tobytes(), right.tobytes())
                if capture:
                    capture.add_instance(player, string, left, right)
                assert before == (left.tobytes(), right.tobytes())
                for index in range(count):
                    mix[2*index] += left[index]
                    mix[2*index+1] += right[index]
            values = array.array('f', mix)
            before = values.tobytes()
            if capture:
                capture.end_block(values)
            assert before == values.tobytes()
            raw_peak = max(raw_peak, max(map(abs, values)))
            target.write(values.tobytes())
    levels = _write_pcm16(spool, partial, RATE, raw_peak, policy, lambda:False, lambda _:None)
    metrics = dict(frames=FRAMES, sample_rate=RATE, source_origin_sample=ORIGIN, **{k:v for k,v in levels.items() if k!='frames'},
                   renderer_version=RENDER_VERSION, processing_mode='freewheeling', instances=len(instances),
                   quality_by_instance=[{'sample':2,'oscillator':1} for _ in instances],
                   renderer='authored fake of existing native boundary', samples='none; authored floats')
    gain = 10**(-3/20)/raw_peak if policy=='peak' else .25
    return partial, metrics, raw_peak, gain


@unittest.skipUnless(os.name == 'nt', 'Bundle verifier uses Windows protected readers')
class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def finish(self, capture, render):
        wav, metrics, peak, gain = render
        return capture.finish(wav, raw_peak=peak, global_gain=gain, renderer_metrics=metrics)

    def test_observer_preserves_existing_pcm_and_admits_complete_duo(self):
        baseline = self.root/'baseline';baseline.mkdir()
        observed = self.root/'observed';observed.mkdir()
        reference = authored_render(baseline)[0].read_bytes()
        capture = start_capture(observed)
        source, arrangement = fixture()
        snapshot = deepcopy(arrangement)
        rendered = authored_render(observed,capture)
        self.assertEqual(rendered[0].read_bytes(),reference)
        result = self.finish(capture,rendered)
        self.assertEqual(result['status'],'admitted',result)
        bundle = result['bundle'];manifest = bundle['manifest'];verification = bundle['verification']
        self.assertEqual((capture.folder/'combined.wav').read_bytes(),reference)
        self.assertEqual(verification['clock']['origin_sample'],ORIGIN)
        self.assertEqual(verification['clock']['tail_frames'],RATE)
        self.assertEqual(manifest['combined']['active_stem_ids'],['performer-1','performer-2'])
        self.assertEqual(manifest['source'],source)
        self.assertEqual(arrangement,snapshot)
        self.assertEqual(manifest['arrangement']['sha256'],hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest())
        receipt = bundle['renderer_receipt']
        self.assertEqual(manifest['renderer']['receipt_sha256'],digest(capture.folder/'renderer-receipt.json'))
        self.assertEqual(receipt['master_float']['sha256'],digest(observed/'master.f32'))
        for player in (1,2):
            self.assertEqual(receipt['performer_float'][str(player)]['sha256'],digest(capture.folder/f'performer-{player}.f64'))
            raw = (capture.folder/f'performer-{player}.wav').read_bytes()
            self.assertNotEqual(raw[-4:],b'\0'*4,'Complete nonzero tail must be retained')
        self.assertTrue(verification['all_mute_subsets_and_gain_reductions_within_pcm_range'])
        self.assertFalse(verification['source_or_vocal_content_exclusion_verified'])
        binding = {key:manifest[key] for key in ('source','clock','arrangement','renderer')}
        self.assertEqual(verify_bundle(capture.folder,manifest,expected_binding=binding,
            expected_manifest_sha256=verification['manifest_sha256'],max_frames=FRAMES,
            max_total_audio_bytes=bundle['forecast']['bundle_audio_bytes'])['verification'],verification)

    def test_solo_is_one_complete_polyphonic_performer(self):
        capture = start_capture(self.root,performers=1)
        result = self.finish(capture,authored_render(self.root,capture,performers=1))
        self.assertEqual(result['status'],'admitted',result)
        manifest = result['bundle']['manifest']
        self.assertEqual(len(manifest['stems']),1)
        self.assertEqual(manifest['stems'][0]['role'],'harmony and melody')
        self.assertEqual(manifest['arrangement']['performers'],1)

    def test_cancelling_parts_cannot_hide_overload_behind_safe_combined(self):
        capture = start_capture(self.root)
        render = authored_render(self.root,capture,opposed=True)
        before = render[0].read_bytes()
        result = self.finish(capture,render)
        self.assertEqual(result['status'],'refused')
        self.assertEqual(result['diagnostic']['stage'],'quantization')
        self.assertIn('headroom',result['diagnostic']['detail'])
        self.assertEqual(render[0].read_bytes(),before)
        self.assertEqual([path.name for path in capture.folder.iterdir()],['diagnostic.json'])

    def test_wrong_combined_mix_refuses_without_rewriting_it(self):
        capture = start_capture(self.root)
        render = authored_render(self.root,capture)
        raw = bytearray(render[0].read_bytes())
        raw[44:46] = struct.pack('<h',struct.unpack('<h',raw[44:46])[0]+4)
        render[0].write_bytes(raw)
        before = digest(render[0])
        result = self.finish(capture,render)
        self.assertEqual(result['status'],'refused')
        self.assertEqual(result['diagnostic']['stage'],'verification')
        self.assertIn('linear mix',result['diagnostic']['detail'])
        self.assertEqual(digest(render[0]),before)

    def test_float_tamper_is_not_relabelled_as_observed_native_capture(self):
        capture = start_capture(self.root)
        render = authored_render(self.root,capture)
        capture._close_streams()
        path = capture.folder/'performer-1.f64'
        raw = bytearray(path.read_bytes());raw[-8:] = struct.pack('<d',0.)
        path.write_bytes(raw)
        result = self.finish(capture,render)
        self.assertEqual(result['status'],'refused')
        self.assertIn('float bytes changed',result['diagnostic']['detail'])

    def test_saved_metadata_tamper_refuses_despite_valid_wavs(self):
        for filename in ('renderer-receipt.json','manifest.json','verification.json'):
            with self.subTest(filename=filename):
                root = self.root/filename;root.mkdir()
                capture = start_capture(root)
                render = authored_render(root,capture)
                before = digest(render[0])
                original = capture._write_json
                def tamper(name,value,maximum):
                    result = original(name,value,maximum)
                    if name == filename:
                        path = capture.folder/name
                        path.write_bytes(path.read_bytes()+b' ')
                    return result
                capture._write_json = tamper
                outcome = self.finish(capture,render)
                self.assertEqual(outcome['status'],'refused')
                self.assertIn('metadata bytes differ',outcome['diagnostic']['detail'])
                self.assertEqual(digest(render[0]),before)

    def test_cancelled_observer_is_sticky_and_normal_pcm_completes(self):
        calls = [0]
        def cancel():
            calls[0] += 1
            return calls[0] >= 10
        capture = start_capture(self.root,cancel=cancel)
        render = authored_render(self.root,capture)
        before = digest(render[0])
        result = self.finish(capture,render)
        self.assertEqual(result['status'],'cancelled')
        self.assertFalse(capture.begin_block(0,1))
        self.assertEqual(self.finish(capture,render),result)
        self.assertEqual(digest(render[0]),before)
        self.assertLessEqual((capture.folder/'diagnostic.json').stat().st_size,8192)

    def test_cancellation_with_metadata_and_wavs_held_releases_own_files(self):
        capture = start_capture(self.root)
        render = authored_render(self.root,capture)
        before = digest(render[0])
        checks = [0]
        def cancel():
            if capture._stage == 'verification':
                checks[0] += 1
            return checks[0] >= 10
        capture._cancel = cancel
        result = self.finish(capture,render)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['diagnostic']['stage'],'verification')
        self.assertEqual([path.name for path in capture.folder.iterdir()],['diagnostic.json'])
        self.assertEqual(digest(render[0]),before)

    def test_write_failure_cleanup_keeps_foreign_sibling_and_standalone(self):
        capture = start_capture(self.root)
        sibling = capture.folder/'unrelated.txt';sibling.write_text('keep')
        underlying = capture._streams[1]
        class BrokenWrite:
            def write(self,_):raise OSError('authored disk failure')
            def close(self):underlying.close()
        capture._streams[1] = BrokenWrite()
        render = authored_render(self.root,capture)
        before = digest(render[0])
        self.assertEqual(self.finish(capture,render)['status'],'refused')
        capture.cleanup(keep_diagnostic=False);capture.cleanup(keep_diagnostic=False)
        self.assertEqual(sibling.read_text(),'keep')
        self.assertEqual(digest(render[0]),before)
        self.assertEqual([p.name for p in capture.folder.iterdir()],['unrelated.txt'])

    def test_coverage_and_actual_roster_refuse_missing_or_reordered_instances(self):
        capture = start_capture(self.root)
        self.assertTrue(capture.begin_block(0,2))
        self.assertFalse(capture.add_instance(2,0,[0.,0.],[0.,0.]))
        self.assertEqual(capture.state,'refused')
        another = self.root/'other';another.mkdir()
        capture = start_capture(another)
        self.assertTrue(capture.begin_block(0,2))
        self.assertTrue(capture.add_instance(1,4,[0.,0.],[0.,0.]))
        self.assertFalse(capture.end_block(array.array('f',[0.]*4)))
        self.assertIn('Missing native',capture.outcome['diagnostic']['detail'])

    def test_absent_second_performer_and_unadmitted_resources_make_no_capture(self):
        source, arrangement = fixture()
        arrangement['notes'] = [note for note in arrangement['notes'] if note['player']==1]
        capture = start_capture(self.root,arrangement=arrangement)
        self.assertEqual(capture.state,'refused')
        self.assertIn('no assigned source',capture.outcome['diagnostic']['detail'])
        self.assertFalse((self.root/'capture').exists())
        capture = start_capture(self.root,allowance=forecast_capture(performers=2,frames=FRAMES)['additional_disk_bytes']-1)
        self.assertEqual(capture.state,'refused')
        self.assertFalse((self.root/'capture').exists())

    def test_original_identity_and_exact_master_gain_cannot_be_invented(self):
        _, arrangement = fixture()
        arrangement['source_id'] = 'other-original'
        capture = start_capture(self.root,arrangement=arrangement)
        self.assertEqual(capture.state,'refused')
        capture = start_capture(self.root)
        render = authored_render(self.root,capture)
        wav,metrics,peak,gain = render
        result = capture.finish(wav,raw_peak=peak,global_gain=gain*.99,renderer_metrics=metrics)
        self.assertEqual(result['status'],'refused')
        self.assertIn('Shared gain differs',result['diagnostic']['detail'])


class ForecastTests(unittest.TestCase):
    def test_exact_maximum_forecast_and_hard_bound(self):
        forecast = forecast_capture(performers=2,frames=MAX_FRAMES)
        self.assertEqual(forecast['performer_float_bytes'],MAX_FRAMES*32)
        self.assertEqual(forecast['bundle_audio_bytes'],3*(44+MAX_FRAMES*4))
        self.assertEqual(forecast['additional_disk_bytes'],MAX_FRAMES*32+2*(3*(44+MAX_FRAMES*4)+forecast['metadata_bytes']))
        self.assertEqual(forecast['capture_block_numeric_bytes'],10240)
        for count,frames in ((0,1),(3,1),(True,1),(1,MAX_FRAMES+1)):
            with self.assertRaises(ValueError):forecast_capture(performers=count,frames=frames)


if __name__=='__main__':
    unittest.main()
