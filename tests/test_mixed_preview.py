"""Silent API/worker integration with stub analysis and renderer, no models."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid
import wave
import subprocess
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import server
import worker
from library import Library
from audio.managed_process import Cancelled, ManagedProcessError
import httpx
from gateway import create_gateway


RUNTIME={'ready':True,'reason':None,'python':'owned-python','modeldir':'owned-models',
         'manifest':{'runtime_version':'test-runtime-v1'}}

def original_bytes():
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(44100)
        wav.writeframes(b'\0\0'*44100)
    return buffer.getvalue()

class FakeProcess:
    def __init__(self,*args,**kwargs): self.done=False;self.stdin=io.BytesIO()
    def poll(self): return 0 if self.done else None
    def wait(self,timeout=None): self.done=True;return 0
    def terminate(self): self.done=True

class MixedAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=server.create_app(self.temp.name)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765');self.client.__enter__()
        self.client.get('/session')
        self.asset=self.client.post('/api/import',content=original_bytes(),headers={'X-Filename':'original.wav'}).json()
        self.spec={'asset_id':self.asset['id'],'start_seconds':0,'end_seconds':1,'performers':2}
        self.runtime=mock.patch('server.mixed_runtime_status',return_value=RUNTIME);self.runtime.start()
        self.children=[]
        def spawn(*args,**kwargs):
            child=FakeProcess();self.children.append(child);return child
        self.spawn=mock.patch('server.subprocess.Popen',side_effect=spawn);self.spawn_mock=self.spawn.start()
    def tearDown(self):
        self.client.__exit__(None,None,None);self.spawn.stop();self.runtime.stop();self.temp.cleanup()
    def post(self,**changes): return self.client.post('/api/arrange-preview',json={**self.spec,**changes})

    def test_guidance_identity_retry_and_legacy_default(self):
        initial=self.post().json()
        self.assertNotIn('guidance',initial['request'])
        self.assertEqual(self.post(guidance={'source_notes':'  ','accompaniment':'melody-and-bass'}).json()['id'],initial['id'])
        self.app.state.library.update_job(initial['id'],'failed',error='stub');self.children[-1].done=True
        guided=self.post(performers=1,guidance={'source_notes':' café ','arrangement_notes':'Sparse','accompaniment':'melody-only'}).json()
        self.assertNotEqual(guided['id'],initial['id'])
        self.assertEqual(guided['request']['guidance']['source_notes'],'café')
        self.assertEqual(self.post(performers=1,guidance={'source_notes':'cafe\u0301','arrangement_notes':'Sparse','accompaniment':'melody-only'}).json()['id'],guided['id'])
        self.assertEqual(self.app.state.library.job(guided['id'])['request'],guided['request'])
        self.assertEqual(self.post(guidance={'accompaniment':'melody-only'}).status_code,400)
        self.assertEqual(self.post(guidance={'source_notes':'x'*257}).status_code,400)
        self.assertEqual(self.post(guidance={'version':True}).status_code,400)
        self.app.state.library.update_job(guided['id'],'failed',error='stub');self.children[-1].done=True
        changed=self.post(performers=1,guidance={'source_notes':'Different observation','arrangement_notes':'Sparse','accompaniment':'melody-only'}).json()
        self.assertNotEqual(changed['id'],guided['id'])
        self.assertEqual(self.app.state.library.job(guided['id'])['request']['guidance']['source_notes'],'café')

    def test_idempotence_busy_and_versions(self):
        response=self.post();self.assertEqual(response.status_code,200,response.text)
        job=response.json();self.assertEqual(job['request']['kind'],'mixed')
        self.assertEqual(job['request']['pipeline_version'],server.MIXED_PIPELINE_VERSION)
        self.assertEqual(job['request']['runtime_version'],'test-runtime-v1')
        self.assertEqual(self.post().json()['id'],job['id']);self.assertEqual(self.spawn_mock.call_count,1)
        self.assertEqual(self.post(performers=1).status_code,409)
        library=self.app.state.library;stage=library.stage/'preview-fixture';stage.write_bytes(b'silent-result-fixture')
        asset=library.publish(stage,'preview.wav','audio/wav',kind='mixed-preview-2',source_id=self.asset['source_id'])
        library.update_job(job['id'],'complete',1,{'asset':asset})
        self.assertEqual(self.post().json()['state'],'complete');self.assertEqual(self.spawn_mock.call_count,1)

    def test_request_validation_and_unavailable_runtime(self):
        for changes in ({'performers':True},{'performers':3},{'start_seconds':-1},{'end_seconds':0},
                        {'end_seconds':31},{'end_seconds':True},{'start_seconds':'0'},{'asset_id':'missing'}):
            with self.subTest(changes=changes): self.assertEqual(self.post(**changes).status_code,400)
        raw=json.dumps({**self.spec,'end_seconds':float('nan')})
        self.assertEqual(self.client.post('/api/arrange-preview',content=raw).status_code,400)
        with mock.patch('server.mixed_runtime_status',return_value={'ready':False,'reason':'Not set up'}):
            self.assertEqual(self.post().status_code,503)
            status=self.client.get('/api/status').json();self.assertFalse(status['mixed_ready'])
            self.assertTrue(status['isolated_melody_ready']);self.assertNotIn('python',status)
        self.spawn_mock.assert_not_called()

    def test_terminal_worker_reaped_before_concurrent_successor_admission(self):
        job=self.post().json();old=self.children[-1]
        self.app.state.library.update_job(job['id'],'failed',error='stub terminal result')
        waits=[]
        def delayed_wait(timeout=None):
            waits.append(timeout);time.sleep(.1);old.done=True;return 0
        old.wait=delayed_wait
        original_spawn=self.spawn_mock.side_effect
        def ordered_spawn(*args,**kwargs):
            self.assertTrue(old.done,'Successor started before predecessor exited')
            return original_spawn(*args,**kwargs)
        self.spawn_mock.side_effect=ordered_spawn
        with ThreadPoolExecutor(2) as pool:
            calls=[pool.submit(self.post,end_seconds=end,performers=1) for end in (.5,.75)]
            codes=sorted(call.result().status_code for call in calls)
        self.assertEqual(codes,[200,409]);self.assertEqual(waits,[5])
        self.assertTrue(old.stdin.closed);self.assertEqual(len(self.children),2)

    def test_terminal_worker_cleanup_timeout_retains_admission_slot(self):
        job=self.post().json();old=self.children[-1]
        self.app.state.library.update_job(job['id'],'failed',error='stub terminal result')
        old.wait=mock.Mock(side_effect=subprocess.TimeoutExpired('owned-stub',5))
        response=self.post(performers=1)
        self.assertEqual(response.status_code,409);self.assertIn('cleanup',response.text)
        self.assertEqual(len(self.children),1)
        old.done=True

    def test_runtime_and_pipeline_versions_change_job_identity(self):
        first=self.post().json()
        self.children[-1].done=True
        updated={**RUNTIME,'manifest':{'runtime_version':'test-runtime-v2'}}
        with mock.patch('server.mixed_runtime_status',return_value=updated): second=self.post().json()
        self.assertNotEqual(first['id'],second['id'])
        self.children[-1].done=True
        with mock.patch('server.MIXED_PIPELINE_VERSION','next-pipeline'): third=self.post().json()
        self.assertNotIn(third['id'],(first['id'],second['id']))
        self.children[-1].done=True
        with mock.patch('audio.preview_instruments.ACOUSTIC_RENDER_VERSION','next-renderer'): fourth=self.post().json()
        self.assertNotIn(fourth['id'],(first['id'],second['id'],third['id']))
        self.assertEqual(fourth['request']['renderer_version'],'next-renderer')

    def test_original_and_supported_format_and_space(self):
        unsupported=self.app.state.library.stage/'unsupported';unsupported.write_bytes(b'unsupported-container')
        item=self.app.state.library.publish(unsupported,'recording.ogg','audio/ogg')
        self.assertEqual(self.post(asset_id=item['id']).status_code,415)
        library=self.app.state.library;path=library.stage/'variant';path.write_bytes(b'variant-fixture')
        item=library.publish(path,'variant.wav','audio/wav',kind='diagnostic-1')
        self.assertEqual(self.post(asset_id=item['id']).status_code,400)
        with mock.patch.object(Library,'space',side_effect=ValueError('reserve')):
            self.assertEqual(self.post().status_code,507)
        self.spawn_mock.assert_not_called()

    def test_supported_formats_enter_managed_analysis_not_inline_decoding(self):
        for extension in ('mp3','m4a','aac','flac','webm'):
            asset=self.client.post('/api/import',content=('fixture-'+extension).encode(),
                                   headers={'X-Filename':'recording.'+extension}).json()
            result=self.post(asset_id=asset['id'])
            self.assertEqual(result.status_code,200,result.text)
            job=result.json();self.assertEqual(job['request']['sha256'],asset['sha256'])
            self.assertEqual(job['state'],'queued')
            self.children[-1].done=True
            self.app.state.library.update_job(job['id'],'failed',error='stub terminal case')

    def test_spawn_failure_is_friendly_failed_job(self):
        with mock.patch('server.subprocess.Popen',side_effect=OSError('stub failure')):
            result=self.post().json()
        self.assertEqual(result['state'],'failed');self.assertIn('original',result['error'])
        self.assertTrue(self.app.state.library.verify(self.asset['id']))


class MixedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.library=Library(self.temp.name)
        source=self.library.stage/'input';source.write_bytes(original_bytes())
        self.asset=self.library.publish(source,'original.wav','audio/wav');self.raw=original_bytes()
        self.identity=str(uuid.uuid4());self.spec={'kind':'mixed','asset_id':self.asset['id'],'sha256':self.asset['sha256'],
            'performers':2,'start_seconds':0,'end_seconds':1,'pipeline_version':server.MIXED_PIPELINE_VERSION,'runtime_version':'test-runtime-v1','renderer_version':server.RENDER_VERSION}
        self.seed()
    def seed(self):
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                (self.identity,'queued',0,json.dumps(self.spec),None,None,time.time()))
    def tearDown(self): self.temp.cleanup()
    def envelope(self):
        return {'performance':{'source_id':self.asset['source_id'],'sample_rate':44100,'notes':[
            {'id':'melody1','start':1000,'end':20000,'pitch':64,'role':'melody'},
            {'id':'bass1','start':1000,'end':20000,'pitch':40,'role':'bass'}]},
            'report':{'warnings':['No harmony recovery']},'render_origin_sample':0}
    def analysis_stub(self,module,request,**kwargs):
        self.assertEqual(module,'audio.mixed_analysis');self.assertEqual(kwargs['memory_bytes'],2*1024**3)
        self.assertLessEqual(kwargs['wall_seconds'],180)
        self.assertEqual(Path(request['source_path']).read_bytes(),self.raw)
        kwargs['progress']({'progress':.5})
        path=Path(request['result_path']);data=json.dumps(self.envelope()).encode()
        path.with_suffix('.partial').write_bytes(b'leftover-fixture')
        path.with_suffix('.decoded.wav').write_bytes(b'owned decoded fixture')
        path.with_suffix('.decoded.wav.decode.partial').write_bytes(b'owned partial decode fixture')
        path.write_bytes(data)
        return {'result':{'result_bytes':len(data),'result_sha256':hashlib.sha256(data).hexdigest()},'metrics':{'peak_working_set_bytes':123}}
    def render_stub(self,arrangement,path,**kwargs):
        self.assertNotIn('source_path',arrangement)
        self.assertEqual({n['player'] for n in arrangement['notes']},{1,2})
        Path(path).write_bytes(b'silent-preview-fixture')
        return {'peak':0,'clipped_samples':0}
    def execute(self,analysis=None,render=None,parent_gone=lambda:False):
        with mock.patch('audio.analysis_runtime.require_runtime',return_value=RUNTIME), \
             mock.patch('worker.run_worker',side_effect=analysis or self.analysis_stub), \
             mock.patch('worker.render',side_effect=render or self.render_stub) as rendering:
            worker.run(self.temp.name,self.identity,parent_gone)
        self.assertEqual((self.library.media/self.asset['sha256']).read_bytes(),self.raw)
        self.assertFalse((self.library.stage/(self.identity+'.analysis.json')).exists())
        self.assertFalse((self.library.stage/(self.identity+'.analysis.partial')).exists())
        self.assertFalse((self.library.stage/(self.identity+'.analysis.decoded.wav')).exists())
        self.assertFalse((self.library.stage/(self.identity+'.analysis.decoded.wav.decode.partial')).exists())
        return self.library.job(self.identity),rendering

    def test_success_preserves_original_and_provenance(self):
        job,rendering=self.execute()
        self.assertEqual(job['state'],'complete',job);rendering.assert_called_once()
        result=job['result'];self.assertEqual(result['asset']['source_id'],self.asset['source_id'])
        self.assertEqual(result['asset']['kind'],'mixed-preview-2')
        self.assertIn('No chord',result['notice']);self.assertEqual(result['metrics']['analysis_process']['peak_working_set_bytes'],123)

    def test_guided_solo_retains_full_analysis_and_never_conditions_analyzer(self):
        self.spec['performers']=1
        self.spec['guidance']={'version':1,'source_notes':'Follow the quiet guitar','arrangement_notes':'Dreamy','accompaniment':'melody-only'}
        self.seed();requests=[]
        def analysis(module,request,**kwargs):
            requests.append(request.copy());return self.analysis_stub(module,request,**kwargs)
        def render(arrangement,path,**kwargs):
            self.assertEqual([n['id'] for n in arrangement['notes']],['melody1'])
            self.assertEqual([n['id'] for n in arrangement['source']['notes']],['melody1','bass1'])
            Path(path).write_bytes(b'silent-melody-only-fixture');return {'peak':0}
        job,_=self.execute(analysis=analysis,render=render)
        self.assertEqual(job['state'],'complete',job)
        self.assertNotIn('guidance',requests[0]);self.assertNotIn('Dreamy',json.dumps(requests[0]))
        result=job['result'];self.assertEqual(result['analysis']['warnings'],['No harmony recovery'])
        self.assertEqual(result['arrangement']['user_guidance']['omitted_note_ids'],['bass1'])
        self.assertEqual(result['asset']['kind'],'mixed-preview-1-melody-only')

    def test_invalid_receipt_or_schema_never_renders(self):
        for fault in ('hash','size','source','time','harmony','bass_only','duet_without_bass','malformed'):
            with self.subTest(fault=fault):
                self.seed()
                def broken(module,request,**kwargs):
                    response=self.analysis_stub(module,request,**kwargs);path=Path(request['result_path'])
                    value=json.loads(path.read_text());note=value['performance']['notes'][0]
                    if fault=='hash': response['result']['result_sha256']='0'*64;return response
                    if fault=='size': response['result']['result_bytes']=worker.MAX_ANALYSIS_BYTES+1;return response
                    if fault=='source': value['performance']['source_id']='other'
                    if fault=='time': note['end']=44101
                    if fault=='harmony': note['role']='harmony'
                    if fault=='bass_only': note['role']='bass'
                    if fault=='duet_without_bass': value['performance']['notes']=value['performance']['notes'][:1]
                    data=b'{' if fault=='malformed' else json.dumps(value).encode()
                    path.write_bytes(data);response['result']={'result_bytes':len(data),'result_sha256':hashlib.sha256(data).hexdigest()}
                    return response
                job,rendering=self.execute(analysis=broken)
                self.assertEqual(job['state'],'failed',job);rendering.assert_not_called()
                self.assertEqual(len(self.library.items()),1)

    def test_cancel_failure_and_parent_loss_preserve_source(self):
        for mode in ('cancel','error','after_result','parent_gone'):
            with self.subTest(mode=mode):
                self.seed()
                def stop(module,request,**kwargs):
                    response=self.analysis_stub(module,request,**kwargs)
                    if mode=='cancel': raise Cancelled('stub cancelled')
                    if mode=='error': raise ManagedProcessError('stub failed')
                    (self.library.stage/(self.identity+'.cancel')).touch()
                    return response
                job,rendering=self.execute(analysis=stop,parent_gone=lambda:mode=='parent_gone')
                self.assertEqual(job['state'],'failed' if mode=='error' else 'cancelled',job)
                rendering.assert_not_called();self.assertEqual(len(self.library.items()),1)

    def test_reconcile_only_owned_analysis_names(self):
        for suffix in ('.analysis.json','.analysis.partial'):
            (self.library.stage/(self.identity+suffix)).write_bytes(b'temporary')
        keep=self.library.stage/'notes.analysis.json';keep.write_bytes(b'keep')
        self.library.reconcile_staging();self.assertTrue(keep.exists())
        self.assertFalse((self.library.stage/(self.identity+'.analysis.json')).exists())

    def test_existing_diagnostic_remains_available_without_analysis(self):
        self.spec={'kind':'diagnostic','performers':2,'renderer_version':server.RENDER_VERSION};self.seed()
        job,rendering=self.execute()
        self.assertEqual(job['state'],'complete',job)
        self.assertEqual(job['result']['asset']['kind'],'diagnostic-2')
        self.assertNotIn('analysis_process',job['result']['metrics'])
        rendering.assert_called_once()

    def test_missing_or_changed_renderer_refuses_old_request_before_analysis(self):
        for value in (None, 'previous-renderer'):
            if value is None: self.spec.pop('renderer_version',None)
            else: self.spec['renderer_version']=value
            self.seed()
            def unexpected(*args,**kwargs):
                raise AssertionError('Analysis must not run for an old renderer request')
            job,rendering=self.execute(analysis=unexpected)
            self.assertEqual(job['state'],'failed')
            self.assertIn('renderer changed',job['error'])
            rendering.assert_not_called()
            self.assertTrue(self.library.verify(self.asset['id']))


class MixedGatewayTests(unittest.TestCase):
    def test_only_paired_requests_reach_new_endpoint(self):
        forwarded=[]
        def authority(request):
            if request.url.path=='/session':
                return httpx.Response(200,json={'ready':True},headers={'Set-Cookie':'guitar_session=local; Path=/'})
            forwarded.append(request)
            return httpx.Response(200,stream=httpx.ByteStream(b'{"state":"queued"}'),headers={'Content-Type':'application/json'})
        with tempfile.TemporaryDirectory() as root:
            app=create_gateway(root,httpx.MockTransport(authority));origin=app.state.origin
            with TestClient(app,base_url=origin,headers={'X-Forwarded-Proto':'https','Origin':origin}) as client:
                self.assertEqual(client.post('/api/arrange-preview',json={}).status_code,401)
                pending=client.post('/pair/request',json={'label':'silent-test'}).json()
                app.state.pairing.approve_local(pending['id'])
                client.post('/pair/claim',json={'id':pending['id'],'secret':pending['secret']})
                self.assertEqual(client.post('/api/arrange-preview',json={'performers':2}).status_code,200)
                self.assertEqual(forwarded[-1].url.path,'/api/arrange-preview')
                self.assertEqual(client.post('/api/arrange-preview/extra',json={}).status_code,404)
                self.assertEqual(client.get('/mixed-preview.mjs').status_code,200)


if __name__=='__main__': unittest.main()
