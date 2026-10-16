import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
from audio import bass_render as render
from audio import growlybass as bank

def arrangement():
    return {'sample_rate':44100,'render_origin_sample':1000,'performers':1,'notes':[
        {'id':'a','start':1000,'end':1256,'pitch':28,'velocity':80,'player':1,'string':0,'fret':3,'articulation':'clean-sustain-first-variation'},
        {'id':'b','start':1256,'end':1512,'pitch':28,'velocity':72,'player':1,'string':0,'fret':3,'articulation':'clean-sustain-first-variation'}]}

class API:
    def __init__(self):self.next=0;self.freed=[];self.events=[];self.maps=[]
    def sfizz_create_synth(self):self.next+=1;return self.next
    def sfizz_free(self,synth):self.freed.append(synth)
    def sfizz_all_sound_off(self,synth):self.events.append(('reset',synth))
    def sfizz_set_sample_rate(self,*a):pass
    def sfizz_set_samples_per_block(self,*a):pass
    def sfizz_load_string(self,synth,path,mapping):self.maps.append(mapping);return True
    def sfizz_send_note_on(self,synth,at,pitch,velocity):self.events.append(('on',synth,at,pitch,velocity))
    def sfizz_send_note_off(self,synth,at,pitch,velocity):self.events.append(('off',synth,at,pitch,velocity))
    def sfizz_render_block(self,synth,channels,count,length):
        for i in range(length):
            channels[0][i]=.1*synth;channels[1][i]=-.1*synth

class BassRenderer(unittest.TestCase):
    def patches(self,api):
        from contextlib import ExitStack
        stack=ExitStack();stack.enter_context(patch.object(render,'require_bank',return_value={'bank':Path('.')}))
        stack.enter_context(patch.object(render,'moderate_map',return_value='mock verified map'))
        stack.enter_context(patch.object(render,'Sfizz',return_value=type('Engine',(),{'lib':api})()))
        stack.enter_context(patch.object(render,'_configure_offline',return_value={'sample':2,'oscillator':1}))
        return stack
    def test_source_preserved_transpose_order_and_real_pcm_policy(self):
        a=arrangement();before=copy.deepcopy(a);api=API()
        with tempfile.TemporaryDirectory() as tmp,self.patches(api):
            path=Path(tmp)/'bass.wav';result=render.render(a,path)
            self.assertEqual(a,before);self.assertEqual(result['command_transpose_semitones'],12)
            self.assertEqual(api.events[:4],[('on',1,0,40,80),('off',1,0,40,0),('reset',1),('on',1,0,40,72)])
            self.assertEqual(result['profile_version'],bank.PROFILE_VERSION);self.assertEqual(result['clipped_samples'],0)
            self.assertAlmostEqual(result['peak'],10**(-3/20),places=4)
            with wave.open(str(path)) as w:self.assertEqual((w.getsampwidth(),w.getframerate(),w.getnframes()),(2,44100,44612))
            self.assertEqual(api.freed,[1]);self.assertFalse(list(Path(tmp).glob('*.partial')))
            with self.assertRaises(FileExistsError):render.render(a,path)
    def test_independent_player_string_instances(self):
        a=arrangement();a['performers']=2;a['notes'].append({**a['notes'][0],'id':'c','player':2})
        a['notes'].append({**a['notes'][0],'id':'d','pitch':38,'fret':0,'string':2})
        api=API()
        with tempfile.TemporaryDirectory() as tmp,self.patches(api):
            result=render.render(a,Path(tmp)/'bass.wav');self.assertEqual(result['instances'],3);self.assertEqual(len(api.freed),3)
            self.assertEqual({e[1] for e in api.events},{1,2,3})
    def test_validation_rejects_loud_overlap_and_wrong_fret_without_loading(self):
        for mutation in ('velocity','overlap','fret','articulation','pitch'):
            a=arrangement()
            if mutation=='velocity':a['notes'][0]['velocity']=121
            if mutation=='overlap':a['notes'][0]['end']+=1
            if mutation=='fret':a['notes'][0]['fret']=4
            if mutation=='articulation':a['notes'][0]['articulation']='sampled-pluck'
            if mutation=='pitch':a['notes'][0]['pitch']=64
            with patch.object(render,'require_bank') as loading,self.assertRaises(ValueError):render.render(a,Path('unused.wav'))
            loading.assert_not_called()
    def test_cancel_and_native_failure_free_instances_and_partials(self):
        for failure in ('cancel','native'):
            api=API();calls=0
            def cancel():
                nonlocal calls;calls+=1;return calls>=6
            if failure=='native':api.sfizz_render_block=lambda *a:(_ for _ in ()).throw(RuntimeError('native fault'))
            with tempfile.TemporaryDirectory() as tmp,self.patches(api):
                path=Path(tmp)/'cancel.wav'
                with self.assertRaises((InterruptedError,RuntimeError)):render.render(arrangement(),path,cancel=cancel if failure=='cancel' else lambda:False)
                self.assertEqual(api.freed,[1]);self.assertFalse(list(Path(tmp).iterdir()))
    def test_verification_rejects_changed_installed_member_and_honors_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'bank/Growlybass';folder.mkdir(parents=True);sample=folder/'sample.wav';sample.write_bytes(b'original')
            engine=root/'sfizz.dll';engine.write_bytes(b'engine')
            digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
            config={'bank':folder,'engine':engine,'engine_sha256':digest(engine),'members':[{'path':'Growlybass/sample.wav','bytes':8,'sha256':digest(sample)}],
                    'probe':{'native':{'binaries':[{'path':str(engine),'sha256':digest(engine),'bytes':6}],'imports':{str(engine):[]}}}}
            with patch.object(bank,'_configuration',return_value=config):
                self.assertTrue(bank.require_bank()['integrity_checked'])
                with self.assertRaises(InterruptedError):bank.require_bank(cancel=lambda:True)
                sample.write_bytes(b'changed!')
                with self.assertRaises(ValueError):bank.require_bank()
    def test_moderate_map_removes_ff_and_random_selection(self):
        # Pure profile conversion; the original pinned helper remains untouched.
        groups=[]
        for i in range(14):
            groups.append('<group> lokey=37 hikey=38\n'+''.join(f'<region> sample=sustain/a_{layer}_rr1.wav\nhivel={high}\n' for layer,high in [('pp',60),('p',95),('f',120),('ff',127)]))
        with patch.object(bank,'clean_sustain_map',return_value=(''.join(groups),[])):
            result=bank.moderate_map(Path('.'));self.assertEqual(result.count('<region>'),42);self.assertNotIn('_ff_',result);self.assertNotIn('off_by=',result)
    def test_damping_is_limited_to_outgoing_five_ms(self):
        gain=render._damping_gain
        self.assertEqual(gain(779,1000,0,44100),1)
        self.assertAlmostEqual(gain(890,1000,0,44100),.5)
        self.assertEqual(gain(1000,1000,0,44100),0)
        self.assertEqual(gain(1000,None,1000,44100),1)

if __name__=='__main__':unittest.main()
