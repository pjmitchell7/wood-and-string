"""Authored fake-sfizz boundary checks; no DLL, bank, browser or playback."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audio import render as renderer
from tests.test_acoustic_stem_capture import fixture, start_capture, FRAMES, ORIGIN


class FakeApi:
    def __init__(self):
        self.states = {}
        self.freed = []
        self.commands = []
        self.next_id = 0

    def sfizz_create_synth(self):
        self.next_id += 1
        self.states[self.next_id] = dict(cursor=0, onset=None, string=None)
        return self.next_id

    def sfizz_free(self, synth):
        self.freed.append(synth)
        del self.states[synth]

    def sfizz_set_sample_rate(self, synth, value):
        assert value == 44100

    def sfizz_set_samples_per_block(self, synth, value):
        assert value == 256

    def sfizz_get_sample_quality(self, *args): return 2
    def sfizz_get_oscillator_quality(self, *args): return 1
    def sfizz_set_sample_quality(self, *args): pass
    def sfizz_set_oscillator_quality(self, *args): pass
    def sfizz_enable_freewheeling(self, *args): pass

    def sfizz_load_string(self, synth, path, text):
        self.states[synth]['string'] = int(text.decode().split('=')[1])
        return True

    def sfizz_send_note_on(self, synth, offset, pitch, velocity):
        state = self.states[synth]
        state['onset'] = state['cursor'] + offset
        self.commands.append((state['string'], state['cursor'] + offset, 1, pitch, velocity))

    def sfizz_send_note_off(self, synth, offset, pitch, velocity):
        state = self.states[synth]
        self.commands.append((state['string'], state['cursor'] + offset, 0, pitch, velocity))

    def sfizz_render_block(self, synth, channels, channel_count, size):
        state = self.states[synth]
        amplitude = {0: .05, 4: .02, 5: .1}[state['string']]
        for index in range(size):
            position = state['cursor'] + index
            age = -1 if state['onset'] is None else position - state['onset']
            value = amplitude if 0 <= age < 12 else amplitude * .1 if position == FRAMES-1 else 0.
            channels[0][index] = value
            channels[1][index] = -value * .75
        state['cursor'] += size


def fake_boundary(module=renderer):
    api, stack = FakeApi(), ExitStack()
    stack.enter_context(patch.object(module, 'Sfizz', return_value=SimpleNamespace(lib=api)))
    stack.enter_context(patch.object(module, 'string_sfz', side_effect=lambda string: f'string={string}'))
    return stack, api


class RenderBridge(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def render_case(self, path, arrangement, **kwargs):
        boundary, api = fake_boundary()
        with boundary:
            metrics = renderer.render(arrangement, path, **kwargs)
        self.assertFalse(api.states)
        self.assertEqual(len(api.freed), 3)
        return metrics, api.commands

    def test_default_and_captured_master_preserve_frozen_legacy_bytes(self):
        _, arrangement = fixture()
        baseline = self.root/'baseline.wav'
        metrics, commands = self.render_case(baseline, arrangement)
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(),
                         '0d4994ae324b5e55f6e65d79886a235b2c838817e29bb95d78566a74a071fe6d')
        self.assertEqual(commands, [(4,2,1,59,80), (4,11,0,59,0), (5,0,1,64,80),
                                    (5,8,0,64,0), (0,0,1,40,80), (0,9,0,40,0)])
        capture = start_capture(self.root)
        observed = self.root/'observed.wav'
        observed_metrics, observed_commands = self.render_case(observed, arrangement, stem_capture=capture)
        self.assertEqual(observed.read_bytes(), baseline.read_bytes())
        self.assertEqual(observed_metrics, metrics)
        self.assertEqual(observed_commands, commands)
        gain = 10**(-3/20)/metrics['raw_peak']
        result = capture.finish(observed, raw_peak=metrics['raw_peak'], global_gain=gain,
                                renderer_metrics=metrics)
        self.assertEqual(result['status'], 'admitted', result)
        manifest = result['bundle']['manifest']
        self.assertEqual(manifest['clock']['origin_sample'], ORIGIN)
        self.assertEqual(manifest['clock']['frames'], FRAMES)
        self.assertEqual(len(manifest['stems']), 2)
        self.assertNotEqual((capture.folder/'performer-1.wav').read_bytes()[-4:], b'\0'*4)
        capture.cleanup(keep_diagnostic=False)

    def test_capture_write_refusal_preserves_current_master_block(self):
        class FailedWriter:
            def __init__(self, stream): self.stream = stream
            def write(self, data): raise OSError('Authored capture disk failure')
            def close(self): self.stream.close()
        _, arrangement = fixture()
        capture = start_capture(self.root)
        capture._streams[1] = FailedWriter(capture._streams[1])
        baseline, observed = self.root/'baseline.wav', self.root/'observed.wav'
        metrics, commands = self.render_case(baseline, arrangement)
        observed_metrics, observed_commands = self.render_case(observed, arrangement, stem_capture=capture)
        self.assertEqual(observed.read_bytes(), baseline.read_bytes())
        self.assertEqual(observed_metrics, metrics)
        self.assertEqual(observed_commands, commands)
        self.assertEqual(capture.outcome['status'], 'refused')
        self.assertIn('disk failure', capture.outcome['diagnostic']['detail'])
        self.assertEqual(capture.outcome['diagnostic']['captured_frames'], 0)
        capture.cleanup(keep_diagnostic=False)

    def test_changed_arrangement_refuses_only_the_companion(self):
        _, arrangement = fixture()
        capture = start_capture(self.root)
        arrangement['notes'][0]['velocity'] += 1
        baseline, observed = self.root/'baseline.wav', self.root/'observed.wav'
        metrics, commands = self.render_case(baseline, arrangement)
        observed_metrics, observed_commands = self.render_case(observed, arrangement, stem_capture=capture)
        self.assertEqual(observed.read_bytes(), baseline.read_bytes())
        self.assertEqual(observed_metrics, metrics)
        self.assertEqual(observed_commands, commands)
        self.assertEqual(capture.outcome['status'], 'refused')
        self.assertEqual(capture.outcome['diagnostic']['stage'], 'capture')
        self.assertEqual(capture.outcome['diagnostic']['captured_frames'], 0)
        self.assertIn('differs', capture.outcome['diagnostic']['detail'])
        capture.cleanup(keep_diagnostic=False)

    def test_cancelled_master_and_caller_owned_capture_cleanup(self):
        _, arrangement = fixture()
        capture = start_capture(self.root)
        boundary, api = fake_boundary()
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 1
        try:
            with boundary, self.assertRaises(InterruptedError):
                renderer.render(arrangement, self.root/'cancelled.wav', cancel=cancelled, stem_capture=capture)
        finally:
            capture.cleanup(keep_diagnostic=False)
        self.assertFalse(api.states)
        self.assertFalse(list(self.root.iterdir()))


if __name__ == '__main__': unittest.main()
