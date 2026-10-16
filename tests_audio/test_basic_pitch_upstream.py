import ast
import inspect
import hashlib
from pathlib import Path
import unittest
import warnings
import numpy as np
import scipy.signal
from audio import basic_pitch_upstream as bp


def report(n=100):
    return dict(source_rate=44100,source_origin_sample=5336117,source_length=441000,
                raw_predictions={'note':np.zeros((n,88),np.float32),'onset':np.zeros((n,88),np.float32)})


class UpstreamTests(unittest.TestCase):
    def test_pinned_source_ast_and_output_equivalence(self):
        source=Path('audio/research/basic-pitch/spotify-note_creation-fa5997.py')
        self.assertTrue(source.is_file(),'Pinned reference required for equivalence test')
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         '83346193f834edfc37aeadbed91a4092b574727dd2b4535e03e5ad89d2e89506')
        names=('get_infered_onsets','constrain_frequency','output_to_notes_polyphonic')
        tree=ast.parse(source.read_text()); nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        for node in nodes:
            actual=ast.parse(inspect.getsource(getattr(bp,node.name))).body[0]
            self.assertEqual(ast.dump(node),ast.dump(actual))
        # Execute only the reviewed three function definitions, with same numpy/scipy.
        scope=dict(np=np,scipy=scipy,List=list,Tuple=tuple,Optional=__import__('typing').Optional,
                   MIDI_OFFSET=21,MAX_FREQ_IDX=87)
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),scope)
        rng=np.random.default_rng(43844)
        for policy in bp.POLICIES.values():
            frames=rng.random((70,88),dtype=np.float32)*.8
            onsets=rng.random((70,88),dtype=np.float32)*.9
            args=dict(onset_thresh=.5,frame_thresh=.3,max_freq=None,min_freq=None,**policy)
            expected=scope['output_to_notes_polyphonic'](frames.copy(),onsets.copy(),**args)
            actual=bp.output_to_notes_polyphonic(frames.copy(),onsets.copy(),**args)
            self.assertEqual(actual,expected)

    def test_silence_records_upstream_nan_warning(self):
        r=bp.decode_saved(report())
        self.assertEqual(r['events'],[])
        self.assertEqual(r['nonfinite_augmented_onset_cells'],8800)
        self.assertTrue(r['warnings'])

    def test_minimum_duration_strict_and_rest_tolerance(self):
        r=report();f=r['raw_predictions']['note'];o=r['raw_predictions']['onset']
        f[10:21,39]=.8;o[10,39]=.9
        short=bp.decode_saved(r,'published_default')
        self.assertEqual(short['events'],[])
        self.assertTrue(bp.decode_saved(r,'duration_matched')['events'])
        f[10:40,39]=.8;f[20:24,39]=0
        events=bp.decode_saved(r,'onset_head_ablation')['events']
        self.assertEqual(len(events),1)
        self.assertGreater(events[0]['end_frame_exclusive'],24)

    def test_melodia_recovery_is_not_independent_attack(self):
        r=report();r['raw_predictions']['note'][10:50,39]=.8
        a=bp.decode_saved(r,'onset_head_ablation');b=bp.decode_saved(r,'duration_matched')
        self.assertFalse(a['events']);self.assertTrue(b['events'])
        self.assertFalse(any(e['onset_head_peak_at_start'] for e in b['events']))

    def test_preserves_arrays_origin_and_polyphony(self):
        r=report();r['raw_predictions']['note'][10:50,39]=.8;r['raw_predictions']['note'][20:60,43]=.8
        r['raw_predictions']['onset'][10,39]=.9;r['raw_predictions']['onset'][20,43]=.9
        before={k:v.copy() for k,v in r['raw_predictions'].items()}
        result=bp.decode_saved(r,'onset_head_ablation')
        self.assertEqual(len(result['events']),2)
        self.assertGreater(result['events'][0]['start'],5336117)
        self.assertGreater(result['events'][0]['end'],result['events'][1]['start'])
        for k,v in before.items():np.testing.assert_array_equal(v,r['raw_predictions'][k])


if __name__=='__main__':unittest.main()
