import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import nullcontext
import numpy as np
from audio import instrumental_analysis as ia
from audio.basic_pitch import decode_events,STRIDE,FRAMES,OVERLAP

class InstrumentalAnalysis(unittest.TestCase):
    def fixture(self,root,stage='recognize'):
        work=root/'owned';work.mkdir();source=root/'original.wav';source.write_bytes(b'original')
        request={'stage':stage,'source_path':str(source.resolve()),'source_sha256':ia.sha(source),'source_id':'source',
                 'start_seconds':2.,'end_seconds':3.,'work_dir':str(work.resolve())}
        stereo=np.column_stack([np.full(44100,.2,np.float32),np.full(44100,-.1,np.float32)])
        np.save(work/'other.npy',stereo,allow_pickle=False)
        report={'version':1,'source_id':'source','source_sha256':request['source_sha256'],'sample_rate':44100,'origin':88200,'end':132300,
                'source_part':ia.PART,'timebase':{'decoded':False},'provenance':{'niter':1},
                'other':{'bytes':(work/'other.npy').stat().st_size,'sha256':ia.sha(work/'other.npy')}}
        ia.write_json(work/'separation.json',report)
        return request,work,{'basic_pitch_overlay':str(root/'overlay'),'basic_pitch_model':str(root/'unused-model')}
    def raw(self):
        count=int(22050/STRIDE*(FRAMES-OVERLAP));arrays={key:np.zeros((count,width),np.float32) for key,width in [('note',88),('onset',88),('contour',264)]}
        arrays['note'][4:35,39]=.8;arrays['onset'][4,39]=.9;arrays['contour'][4:35,117]=.8
        raw=decode_events(arrays,source_rate=44100,source_origin_sample=88200,source_length=44100)
        raw.update(source_rate=44100,source_origin_sample=88200,source_length=44100,raw_predictions={k:v.tolist() for k,v in arrays.items()},metadata={'input_resampler':'mock no inference'},model_sha256='f'*64)
        return raw
    def test_both_real_decoders_retained_raw_arrays_mean_and_source_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            request,work,runtime=self.fixture(Path(tmp));seen=[]
            def analyze(samples,rate,origin,*args,**kwargs):seen.append((samples.copy(),rate,origin));return self.raw()
            with patch('audio.instrumental_runtime.require_runtime',return_value=runtime),patch('audio.basic_pitch.analyze_pcm',side_effect=analyze):
                receipt=ia.run(request,lambda _:None)
            self.assertTrue(np.allclose(seen[0][0],.05));self.assertEqual(seen[0][1:],(44100,88200))
            result=json.loads((work/'candidates.json').read_text());self.assertEqual(result['source_part']['instrument'],'unknown')
            self.assertEqual(set(result['interpretations']),{'published','onset_only'})
            for value in result['interpretations'].values():
                self.assertTrue(value['events']);self.assertEqual(value['source_clock']['origin'],88200)
                self.assertTrue(all(88200<=e['start']<e['end']<=132300 for e in value['events']))
                self.assertIn('unassigned_active_frame_pitch_count',value);self.assertNotIn('raw_predictions',value)
            self.assertIn('attacks',result['interpretations']['onset_only']);self.assertIn('rejected_attacks',result['interpretations']['onset_only'])
            self.assertIn('nonfinite_augmented_onset_cells',result['interpretations']['published'])
            self.assertEqual(ia.sha(work/'activations.npz'),receipt['activations_sha256'])
            with np.load(work/'activations.npz',allow_pickle=False) as arrays:self.assertEqual(arrays['contour'].shape[1],264)
            self.assertEqual((Path(tmp)/'original.wav').read_bytes(),b'original')
    def test_zero_boundary_events_are_receipted_not_silently_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            request,work,runtime=self.fixture(Path(tmp))
            decoded={'events':[{'id':'terminal','start':132300,'end':132300,'pitch':60}],'warnings':[],'settings':{'test':True},'nonfinite_augmented_onset_cells':3}
            with patch('audio.instrumental_runtime.require_runtime',return_value=runtime),patch('audio.basic_pitch.analyze_pcm',return_value=self.raw()),patch('audio.basic_pitch_upstream.decode_saved',return_value=decoded):ia.run(request,lambda _:None)
            result=json.loads((work/'candidates.json').read_text())['interpretations']['published']
            self.assertEqual(result['events'],[]);self.assertEqual(result['rejected_boundary_events'][0]['id'],'terminal')
            self.assertEqual(result['nonfinite_augmented_onset_cells'],3);self.assertTrue(result['warnings'])
    def test_raw_invalid_frame_count_or_nonfinite_fails_without_outputs(self):
        for kind in ('count','nonfinite','origin'):
            with tempfile.TemporaryDirectory() as tmp:
                request,work,runtime=self.fixture(Path(tmp));raw=self.raw()
                if kind=='count':raw['raw_predictions']={k:v[:-1] for k,v in raw['raw_predictions'].items()}
                elif kind=='nonfinite':raw['raw_predictions']['onset'][0][0]=float('nan')
                else:raw['source_origin_sample']=0
                with patch('audio.instrumental_runtime.require_runtime',return_value=runtime),patch('audio.basic_pitch.analyze_pcm',return_value=raw),self.assertRaises(ValueError):ia.run(request,lambda _:None)
                self.assertFalse((work/'candidates.json').exists());self.assertFalse((work/'activations.npz').exists());self.assertTrue((work/'other.npy').exists())
    def test_integrity_geometry_and_preexisting_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            request,work,runtime=self.fixture(Path(tmp));(work/'other.npy').write_bytes(b'changed')
            with patch('audio.instrumental_runtime.require_runtime',return_value=runtime),self.assertRaises(ValueError):ia.run(request,lambda _:None)
            (work/'candidates.json').write_bytes(b'keep')
            with self.assertRaises(FileExistsError):ia.run(request,lambda _:None)
            self.assertEqual((work/'candidates.json').read_bytes(),b'keep')
    def test_stage_exception_cleans_only_owned_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);request,work,runtime=self.fixture(root,'separate');(work/'other.npy').unlink();(work/'separation.json').unlink();keep=work/'unrelated';keep.write_bytes(b'keep')
            def fail(*args):
                (work/'other.npy').write_bytes(b'partial');(work/'decoded.wav.decode.partial').write_bytes(b'partial');raise InterruptedError('cancel')
            with patch('audio.instrumental_runtime.require_runtime',return_value=runtime),patch.object(ia,'_separate',side_effect=fail),self.assertRaises(InterruptedError):ia.run(request,lambda _:None)
            self.assertEqual(list(work.iterdir()),[keep])
    def test_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            request,work,_=self.fixture(Path(tmp));request['end_seconds']=12.01
            with self.assertRaises(ValueError):ia.validate_request(request)
        with self.assertRaises(ValueError):ia.validate_events([{'id':str(i),'start':0,'end':1,'pitch':60} for i in range(4001)],0,2)
        with self.assertRaises(ValueError):ia.validate_events([{'id':'bad','start':0,'end':1,'pitch':60,'mean_note_activation':float('nan')}],0,2)
    def test_separation_cpu_settings_geometry_no_normalization_and_other_selection(self):
        from audio import mixed_analysis
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);request,work,_=self.fixture(root,'separate');(work/'other.npy').unlink();(work/'separation.json').unlink()
            pcm=np.column_stack([np.full(44100,3276,np.int16),np.full(44100,-6553,np.int16)])
            weights={}
            for target in mixed_analysis.TARGETS:
                p=root/(target+'.pth');p.write_bytes(b'weights');weights[target]=(p.name,ia.sha(p))
            observed={}
            class Tensor:
                def __init__(self,value):self.value=value
                def __getitem__(self,key):return Tensor(self.value[key])
                def numpy(self):return self.value
            class Target:
                def state_dict(self):return {'weight':0}
                def load_state_dict(self,state,strict):assert set(state)=={'weight'} and strict
            class Model:
                target_models={target:Target() for target in mixed_analysis.TARGETS}
                def eval(self):pass
                def __call__(self,x):
                    observed['input']=x.value.copy();output=np.zeros((1,4,2,44100),np.float32)
                    output[0,3,0,:]=2.;output[0,3,1,:]=-3.
                    return Tensor(output)
            def factory(**kwargs):observed['settings']=kwargs;return Model()
            torch=SimpleNamespace(set_num_threads=lambda n:None,set_num_interop_threads=lambda n:None,from_numpy=Tensor,
                inference_mode=nullcontext,load=lambda *a,**kw:{'weight':0,'sample_rate':44100,'stft.window':0,'transform.0.window':0})
            timebase={'origin_sample':88200,'end_sample':132300,'source_channels':2,'selected_frames':44100,'decoded':False}
            with patch.dict('sys.modules',{'torch':torch,'openunmix':SimpleNamespace(umxhq=factory)}),patch.object(mixed_analysis,'WEIGHTS',weights),patch.object(mixed_analysis,'read_input_passage',return_value=(pcm.tobytes(),timebase)):
                receipt=ia._separate(request,{'modeldir':str(root),'runtime_version':'test'},work,88200,132300,lambda _:None)
            other=np.load(work/'other.npy',allow_pickle=False)
            self.assertEqual(other.shape,(44100,2));self.assertTrue(np.all(other[:,0]==2));self.assertTrue(np.all(other[:,1]==-3))
            self.assertTrue(np.array_equal(observed['input'][0],pcm.T.astype(np.float32)/32768))
            self.assertEqual((observed['settings']['niter'],observed['settings']['wiener_win_len'],observed['settings']['device']),(1,300,'cpu'))
            self.assertEqual(receipt['other_sha256'],ia.sha(work/'other.npy'))

if __name__=='__main__':unittest.main()
