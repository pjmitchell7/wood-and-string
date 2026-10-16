"""Real candidate API/catalog/worker boundaries; inference/rendering are stubs."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import server
import worker
from analysis_evidence import AnalysisEvidence,reconcile_evidence,work_directory
from tests import test_instrumental_preview as fixtures
from tests.test_mixed_preview import FakeProcess

RUNTIME={'ready':True,'runtime_version':'test-candidates-v1','python':'unused-by-stub'}

class InstrumentalPipeline(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.InstrumentalPreview();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.library=self.fixture.library;self.app=server.create_app(self.library.root)
        self.children=[]
        def spawn(*args,**kwargs):
            child=FakeProcess();self.children.append(child);return child
        self.spawn=patch('server.subprocess.Popen',side_effect=spawn);self.spawn_mock=self.spawn.start();self.addCleanup(self.spawn.stop)
        self.runtime=patch('server.instrumental_runtime_status',return_value=RUNTIME);self.runtime.start();self.addCleanup(self.runtime.stop)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765');self.client.__enter__()
        self.addCleanup(lambda:self.client.__exit__(None,None,None));self.client.get('/session')
        self.calls=[]

    def post(self,**changes):
        return self.client.post('/api/instrumental-candidates',json={
            'asset_id':self.fixture.source['id'],'start_seconds':1.,'end_seconds':3.,**changes})

    def selection(self,**changes):
        return self.client.post('/api/arrange-instrumental',json=self.fixture.request(**changes))

    def analysis(self,module,request,**kwargs):
        self.calls.append(request['stage']);directory=Path(request['work_dir'])
        self.assertEqual(module,'audio.instrumental_analysis');self.assertEqual(kwargs['memory_bytes'],2*1024**3)
        def write(name,data,prefix):
            path=directory/name;path.write_bytes(data)
            return {prefix+'_bytes':len(data),prefix+'_sha256':hashlib.sha256(data).hexdigest()}
        kwargs['progress']({'progress':1})
        if request['stage']=='separate':
            receipt=write('separation.json',b'{"stub":"not inference"}','separation')
            receipt.update(write('other.npy',b'owned temporary array placeholder','other'))
        else:
            receipt=write('candidates.json',json.dumps(self.fixture.bundle).encode(),'candidates')
            receipt.update(write('activations.npz',self.fixture.raw,'activations'))
        return dict(result=receipt,metrics={'peak_working_set_bytes':123})

    def execute_analysis(self,job,analysis=None):
        with patch('audio.instrumental_runtime.require_runtime',return_value=RUNTIME), \
             patch('worker.run_worker',side_effect=analysis or self.analysis), \
             patch('worker.render',side_effect=AssertionError('Candidates must not render')):
            worker.run(self.library.root,job['id'])
        self.children[-1].done=True
        self.assertFalse(work_directory(self.library,job['id']).exists())
        self.assertEqual((self.library.media/self.fixture.source['sha256']).read_bytes(),self.fixture.original)
        return self.library.job(job['id'])

    def test_candidate_completion_without_playable_asset_and_completed_retry(self):
        job=self.post().json();self.assertEqual(self.post().json()['id'],job['id'])
        result=self.execute_analysis(job);self.assertEqual(result['state'],'complete',result)
        self.assertEqual(self.calls,['separate','recognize'])
        self.assertNotIn('asset',result['result']);self.assertEqual(len(self.library.items()),1)
        self.assertEqual(self.client.get('/api/jobs/'+job['id']).json()['state'],'complete')
        self.assertEqual(self.post().json()['id'],job['id']);self.assertEqual(self.spawn_mock.call_count,1)
        self.assertEqual(result['result']['candidates']['source_part']['instrument'],'unknown')
        self.assertEqual(self.client.get('/api/previews/latest').status_code,404)

    def test_request_bounds_runtime_and_busy(self):
        for changes in ({'start_seconds':True},{'end_seconds':1.},{'end_seconds':11.1},
                        {'end_seconds':1.01},{'asset_id':'missing'},{'extra':1}):
            with self.subTest(changes=changes):self.assertEqual(self.post(**changes).status_code,400)
        with patch('server.instrumental_runtime_status',return_value={'ready':False,'reason':'Unavailable'}):
            self.assertEqual(self.post().status_code,503)
        job=self.post().json();self.assertEqual(self.post(end_seconds=2.).status_code,409)
        self.assertEqual(self.library.job(job['id'])['state'],'queued')

    def test_receipt_tampering_stops_before_recognition_and_cleans_owned_directory(self):
        job=self.post().json()
        def corrupt(*args,**kwargs):
            value=self.analysis(*args,**kwargs);value['result']['other_sha256']='0'*64;return value
        result=self.execute_analysis(job,corrupt)
        self.assertEqual(result['state'],'failed');self.assertEqual(self.calls,['separate'])
        self.assertIn('receipt',result['error'])

    def test_forced_or_cancelled_stage_preserves_parent_evidence(self):
        from audio.managed_process import Cancelled
        job=self.post().json()
        def stop(module,request,**kwargs):
            self.analysis(module,request,**kwargs)
            raise Cancelled('Simulated native cancellation')
        result=self.execute_analysis(job,stop)
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(self.fixture.store.verify(self.fixture.reference).read_bytes(),self.fixture.raw)

    def test_stale_pipeline_never_launches_inference(self):
        job=self.post().json();spec={**job['request'],'pipeline_version':'old'}
        with self.library.connect() as db:db.execute('UPDATE jobs SET request=? WHERE id=?',(json.dumps(spec),job['id']))
        result=self.execute_analysis(job)
        self.assertEqual(result['state'],'failed');self.assertEqual(self.calls,[])

    def test_selected_render_preserves_parent_without_analysis(self):
        parent=copy.deepcopy(self.library.job(self.fixture.parent_id))
        response=self.selection();self.assertEqual(response.status_code,200,response.text);job=response.json()
        def render(arrangement,path,**kwargs):
            self.assertEqual([n['id'] for n in arrangement['source']['notes']],['common'])
            self.assertEqual(arrangement['source']['notes'][0]['confidence'],.17)
            self.assertEqual(arrangement['candidate_review']['source_part']['instrument'],'unknown')
            path.write_bytes(b'silent selected-note fixture');return {'clipped_samples':0}
        with patch('worker.run_worker',side_effect=AssertionError('Selection must not analyze')), \
             patch('worker.render',side_effect=render) as renderer:
            worker.run(self.library.root,job['id'])
        self.children[-1].done=True;result=self.library.job(job['id'])
        self.assertEqual(result['state'],'complete',result);renderer.assert_called_once()
        self.assertEqual(self.library.job(self.fixture.parent_id),parent)
        self.assertEqual(self.selection().json()['id'],job['id'])
        self.assertEqual(len(self.library.items()),2)
        self.assertEqual(self.client.post('/api/refine-preview',json={'parent_job_id':job['id'],
            'operations':[{'kind':'omit','note_ids':['common']}]}).status_code,400)

    def test_parent_change_after_selection_admission_rejects_before_render(self):
        job=self.selection().json();changed={**self.fixture.result,'notice':'Changed after admission'}
        self.library.update_job(self.fixture.parent_id,'complete',1,result=changed)
        with patch('worker.render') as renderer:worker.run(self.library.root,job['id'])
        self.children[-1].done=True;self.assertEqual(self.library.job(job['id'])['state'],'failed');renderer.assert_not_called()

    def test_verification_failure_preserves_evidence_and_creates_recovery_identity(self):
        first=self.execute_analysis(self.post().json());before=copy.deepcopy(first['result'])
        source=self.library.media/self.fixture.source['sha256'];original=source.read_bytes();source.write_bytes(b'broken')
        checked=self.client.get('/api/jobs/'+first['id']).json()
        self.assertEqual(checked['state'],'interrupted');self.assertEqual(checked['result'],before)
        with self.library.exclusive('worker.lock'):reconcile_evidence(self.library)
        self.assertTrue(AnalysisEvidence(self.library).verify(self.fixture.reference).is_file())
        source.write_bytes(original)
        recovery=self.post().json();self.assertNotEqual(recovery['id'],first['id'])
        self.assertEqual(recovery['request']['recovery_of'],first['id'])
        completed=self.execute_analysis(recovery);self.assertEqual(completed['state'],'complete',completed)
        self.assertEqual(self.post().json()['id'],recovery['id'])
        self.assertEqual(self.library.job(first['id'])['result'],before)

    def test_startup_cleans_only_owned_work_and_verified_unreferenced_artifacts(self):
        from analysis_evidence import reconcile_work,digest
        import uuid
        owned=work_directory(self.library,str(uuid.uuid4()));owned.mkdir();(owned/'decoded.wav').write_bytes(b'partial')
        unknown=self.library.stage/'keep.instrumental';unknown.mkdir();(unknown/'keep').write_bytes(b'untouched')
        with self.library.exclusive('worker.lock'):reconcile_work(self.library)
        self.assertFalse(owned.exists());self.assertEqual((unknown/'keep').read_bytes(),b'untouched')


if __name__=='__main__':unittest.main()
