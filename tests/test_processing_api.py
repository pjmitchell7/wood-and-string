"""Durable dispatcher integration, silent fake workers and real SQLite/media."""
import io
import tempfile
import unittest
import uuid
from unittest import mock
from fastapi.testclient import TestClient
import server

RUNTIME={'ready':True,'manifest':{'runtime_version':'queue-test-v1'}}


class Process:
    def __init__(self): self.stdin=io.BytesIO();self.done=False
    def poll(self): return 0 if self.done else None
    def wait(self,timeout=None): self.done=True;return 0
    def terminate(self): self.done=True


class ProcessingAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.gate=mock.Mock()
        self.gate.check.return_value=(False,'Waiting for laptop memory.')
        self.gate.before_start.return_value=(True,None)
        self.patches=[mock.patch('server.Admission',return_value=self.gate),
                      mock.patch('server.mixed_runtime_status',return_value=RUNTIME),
                      mock.patch('server.subprocess.Popen',side_effect=lambda *a,**kw:self.spawn())]
        self.mocks=[patch.start() for patch in self.patches]
        self.children=[]
        self.open()
        self.asset=self.client.post('/api/import',content=b'silent-test-original',headers={'X-Filename':'original.wav'}).json()
        self.request={'asset_id':self.asset['id'],'start_seconds':0,'end_seconds':1,'performers':1}

    def spawn(self):
        child=Process();self.children.append(child);return child

    def open(self):
        self.app=server.create_app(self.temp.name)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765')
        self.client.__enter__();self.client.get('/session')

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for patch in reversed(self.patches): patch.stop()
        self.temp.cleanup()

    def submit(self,operation=None,**changes):
        return self.client.post('/api/processing-queue',json={'operation_id':operation or str(uuid.uuid4()),'request':{**self.request,**changes}})

    def tick(self): self.client.portal.call(self.app.state.processing_tick)

    def complete(self,receipt):
        library=self.app.state.library
        staged=library.stage/'fake-result';staged.write_bytes(b'silent-result')
        asset=library.publish(staged,'result.wav','audio/wav',kind='mixed-preview-1',source_id=self.asset['source_id'])
        library.update_job(receipt['job_id'],'complete',1,{'asset':asset})
        self.children[-1].done=True

    def test_memory_wait_survives_restart_and_same_operation_is_idempotent(self):
        receipt=self.submit().json();identity=receipt['operation_id']
        self.tick()
        self.assertEqual(self.client.get('/api/processing-queue/'+identity).json()['state'],'waiting')
        self.assertEqual(len(self.children),0)
        self.client.__exit__(None,None,None);self.open()
        retry=self.submit(identity).json()
        self.assertEqual(retry['job_id'],receipt['job_id'])
        self.assertEqual(len(self.app.state.processing.list()),1)
        self.assertEqual(len(self.children),0)
        self.gate.check.return_value=(True,None);self.tick()
        self.assertEqual(len(self.children),1)
        self.complete(receipt);self.tick()
        self.assertEqual(self.client.get('/api/processing-queue/'+identity).json()['state'],'complete')
        self.assertEqual(self.submit(identity).json()['state'],'complete')
        self.assertEqual(len(self.children),1)

    def test_single_lane_with_immediate_preview_and_fifo_queue(self):
        immediate=self.client.post('/api/arrange-preview',json=self.request).json()
        first=self.submit(end_seconds=.5).json();second=self.submit(end_seconds=.7).json()
        self.gate.check.return_value=(True,None);self.tick()
        self.assertEqual(len(self.children),1)
        self.app.state.library.update_job(immediate['id'],'failed',error='stub ended')
        self.children[0].done=True;self.tick()
        self.assertEqual(len(self.children),2)
        self.assertEqual(self.client.get('/api/processing-queue/'+first['operation_id']).json()['state'],'running')
        self.assertIn(self.client.get('/api/processing-queue/'+second['operation_id']).json()['state'],('queued','waiting'))
        self.complete(first);self.tick()
        self.assertEqual(len(self.children),3)
        self.assertEqual(self.client.get('/api/processing-queue/'+second['operation_id']).json()['state'],'running')

    def test_queued_cancel_never_spawns_and_running_cancel_is_acknowledged(self):
        receipt=self.submit().json()
        route='/api/processing-queue/'+receipt['operation_id']
        self.assertEqual(self.client.post(route+'/cancel').json()['state'],'cancelled')
        self.gate.check.return_value=(True,None);self.tick();self.assertFalse(self.children)
        running=self.submit(end_seconds=.5).json();self.tick()
        route='/api/processing-queue/'+running['operation_id']
        self.assertEqual(self.client.post(route+'/cancel').json()['state'],'cancelling')
        self.assertTrue((self.app.state.library.stage/(running['job_id']+'.cancel')).exists())
        self.app.state.library.update_job(running['job_id'],'cancelled')
        self.tick()
        self.assertTrue(self.children[-1].done)
        self.assertEqual(self.client.get(route).json()['state'],'cancelled')

    def test_restart_recovers_unfinished_attempt_and_reuses_verified_completion(self):
        receipt=self.submit().json();self.gate.check.return_value=(True,None);self.tick()
        self.gate.check.return_value=(False,'Waiting for laptop memory.')
        self.client.__exit__(None,None,None);self.open();self.tick()
        self.assertIn(self.client.get('/api/processing-queue/'+receipt['operation_id']).json()['state'],('queued','waiting'))
        self.gate.check.return_value=(True,None);self.tick()
        self.assertEqual(len(self.children),2)
        self.complete(receipt)
        self.client.__exit__(None,None,None);self.open();self.tick()
        self.assertEqual(self.client.get('/api/processing-queue/'+receipt['operation_id']).json()['state'],'complete')
        self.assertEqual(len(self.children),2)

    def test_bad_inputs_changed_identity_and_source_are_not_admitted(self):
        self.assertEqual(self.submit('bad').status_code,400)
        self.assertEqual(self.submit(end_seconds=31).status_code,400)
        first=self.submit().json()
        self.assertIn(self.submit(first['operation_id'],end_seconds=.5).status_code,(400,409))
        self.assertEqual(len(self.app.state.processing.list()),1)
        (self.app.state.library.media/self.asset['sha256']).write_bytes(b'damaged')
        self.assertEqual(self.submit().status_code,400)
        self.assertFalse(self.children)

    def test_unknown_memory_and_changed_runtime_wait_or_fail_without_launch(self):
        receipt=self.submit().json();self.tick();self.assertFalse(self.children)
        self.gate.check.return_value=(True,None)
        with mock.patch('server.mixed_runtime_status',return_value={'ready':True,'manifest':{'runtime_version':'changed'}}):
            self.assertEqual(self.submit(receipt['operation_id']).json()['job_id'],receipt['job_id'])
            self.tick()
        self.assertEqual(self.client.get('/api/processing-queue/'+receipt['operation_id']).json()['state'],'failed')
        self.assertFalse(self.children)

    def test_memory_drop_after_durable_claim_returns_to_waiting_without_spawn(self):
        receipt=self.submit().json()
        self.gate.check.return_value=(True,None)
        self.gate.before_start.return_value=(False,'Memory changed before launch.')
        self.tick()
        self.assertEqual(self.client.get('/api/processing-queue/'+receipt['operation_id']).json()['state'],'waiting')
        self.assertFalse(self.children)
        self.gate.before_start.return_value=(True,None)
        self.tick();self.assertEqual(len(self.children),1)

    def test_unexpected_launch_fault_does_not_strand_the_owned_lane(self):
        receipt=self.submit().json();self.gate.check.return_value=(True,None)
        with mock.patch('server.subprocess.Popen',side_effect=RuntimeError('injected launch boundary fault')):
            with self.assertRaises(RuntimeError): self.tick()
        self.tick()
        row=self.client.get('/api/processing-queue/'+receipt['operation_id']).json()
        self.assertEqual(row['state'],'failed')
        self.assertFalse(self.children)
        successor=self.submit(end_seconds=.5).json();self.tick()
        self.assertEqual(self.client.get('/api/processing-queue/'+successor['operation_id']).json()['state'],'running')
        self.assertEqual(len(self.children),1)
