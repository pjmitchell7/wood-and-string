"""Silent refusal and receipt-preservation checks for the Electric boundary."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio.electric_performance import arrange_electric
from audio.electric_preview_render import ADAPTER_VERSION, RENDER_VERSION, render
from audio.performance import Note, Performance


class ElectricPreviewBoundaryTests(unittest.TestCase):
    def arrangement(self):
        return arrange_electric(Performance('known-notes', 44100,
            [Note('m1', 5336117 + 4410, 5336117 + 22050, 64, 80, 'melody')]),
            render_origin_sample=5336117)

    def test_refused_assignment_never_loads_native_adapter(self):
        arrangement = self.arrangement()
        arrangement['renderable'] = False
        with patch('audio.electric_preview_render.render_adapter') as native:
            with self.assertRaisesRegex(ValueError, 'could not be assigned'):
                render(arrangement, Path('unused.wav'))
            native.assert_not_called()

    def test_stale_profile_never_loads_native_adapter(self):
        for key in ('instrument', 'planner_version', 'profile_version'):
            with self.subTest(key=key):
                arrangement = self.arrangement()
                arrangement[key] = 'old-profile'
                with patch('audio.electric_preview_render.render_adapter') as native:
                    with self.assertRaisesRegex(ValueError, 'profile changed'):
                        render(arrangement, Path('unused.wav'))
                    native.assert_not_called()

    def test_cancellation_is_checked_before_loading_native_adapter(self):
        with patch('audio.electric_preview_render.render_adapter') as native:
            with self.assertRaises(InterruptedError):
                render(self.arrangement(), Path('unused.wav'), cancel=lambda: True)
            native.assert_not_called()

    def test_keeps_private_receipt_unchanged_and_distinguishes_wrapper_identity(self):
        arrangement = self.arrangement()
        before = deepcopy(arrangement)
        prototype = {'renderer_version': ADAPTER_VERSION, 'frames': 66150,
            'sample_rate': 44100, 'source_origin_sample': 5336117,
            'sample_limits': [{'gate_exceeds_stored_sample_estimate': True}],
            'effects': [], 'original_audio_mixed': False, 'loops_added': False,
            'native_adapter_acceptance': False, 'musical_acceptance': False,
            'app_mode_enabled': False, 'source_commands': [{'id': 'm1'}],
            'raw_float': {'file': 'private.dry.f32'}, 'bank': {'private': 'receipt'}}
        receipt_before = deepcopy(prototype)
        cancel, progress = lambda: False, lambda _: None
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'known.wav'
            receipt_path = output.with_suffix('.wav.receipt.json')
            saved = json.dumps(prototype, sort_keys=True).encode()
            receipt_path.write_bytes(saved)
            with patch('audio.electric_preview_render.render_adapter', return_value=prototype) as native:
                metrics = render(arrangement, output, cancel=cancel, progress=progress)
            native.assert_called_once_with(arrangement, output, cancel=cancel, progress=progress, level_policy='peak')
            self.assertEqual(receipt_path.read_bytes(), saved)
        self.assertEqual(arrangement, before)
        self.assertEqual(prototype, receipt_before)
        self.assertEqual(metrics['renderer_version'], RENDER_VERSION)
        self.assertEqual(metrics['dry_adapter_version'], ADAPTER_VERSION)
        self.assertFalse(metrics['musical_acceptance'])
        self.assertEqual(metrics['prototype_receipt_scope'], {
            'native_adapter_acceptance': False, 'musical_acceptance': False, 'app_mode_enabled': False})
        self.assertNotIn('source_commands', metrics)
        self.assertNotIn('raw_float', metrics)
        self.assertNotIn('bank', metrics)
        metrics['sample_limits'][0]['gate_exceeds_stored_sample_estimate'] = False
        self.assertTrue(prototype['sample_limits'][0]['gate_exceeds_stored_sample_estimate'])


if __name__ == '__main__':
    unittest.main()
