"""Silent API identities, queued version replay and private-evidence refusal."""
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import server
from audio.preview_instruments import ELECTRIC_PROFILE_VERSION, ELECTRIC_RENDER_VERSION
from tests.test_processing_api import Process, RUNTIME


class ElectricAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gate = Mock()
        self.gate.check.return_value = (False, 'Waiting for memory.')
        self.gate.before_start.return_value = (True, None)
        self.children = []
        self.patches = [patch('server.Admission', return_value=self.gate),
            patch('server.mixed_runtime_status', return_value=RUNTIME),
            patch('server.bass_runtime_status', return_value={'ready': False}),
            patch('server.electric_runtime_status', return_value={'ready': True}),
            patch('server.instrumental_runtime_status', return_value={'ready': False}),
            patch('server.subprocess.Popen', side_effect=self.spawn)]
        for item in self.patches:
            item.start()
        self.app = server.create_app(self.temp.name)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.client.get('/session')
        self.asset = self.client.post('/api/import', content=b'original-unchanged',
            headers={'X-Filename': 'original.wav'}).json()
        self.request = dict(asset_id=self.asset['id'], start_seconds=90, end_seconds=91,
                            performers=1, instrument='electric')

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def spawn(self, *args, **kwargs):
        child = Process()
        self.children.append(child)
        return child

    def submit(self, operation, **changes):
        return self.client.post('/api/processing-queue', json={
            'operation_id': operation, 'request': {**self.request, **changes}})

    def test_electric_has_its_own_identity_and_availability_gate(self):
        response = self.client.post('/api/arrange-preview', json=self.request)
        self.assertEqual(response.status_code, 200, response.text)
        first = response.json()
        self.assertEqual(first['request']['renderer_version'], ELECTRIC_RENDER_VERSION)
        self.assertEqual(first['request']['electric_profile_version'], ELECTRIC_PROFILE_VERSION)
        self.assertNotIn('bass_part', first['request'])
        self.assertEqual(self.client.post('/api/arrange-preview', json=self.request).json()['id'], first['id'])
        status = self.client.get('/api/status').json()
        self.assertTrue(status['electric_ready'])
        self.assertFalse(status['bass_ready'])
        with patch('server.electric_runtime_status', return_value={'ready': False}):
            self.assertFalse(self.client.get('/api/status').json()['electric_ready'])
            self.assertEqual(self.client.post('/api/arrange-preview', json=self.request).status_code, 503)
        self.assertEqual(len(self.children), 1)

    def test_optional_tone_identity_keeps_dry_bypass_and_30_second_support(self):
        dry=self.client.post('/api/arrange-preview',json=self.request).json()
        bypass=self.client.post('/api/arrange-preview',json={**self.request,'electric_tone':{'mode':'bypass'}}).json()
        self.assertEqual(dry['id'],bypass['id']);self.assertEqual(dry['request'],bypass['request'])
        operation=str(uuid.uuid4())
        response=self.submit(operation,end_seconds=120,electric_tone={'mode':'clean','ambience':'room','ambience_mix':.1})
        self.assertEqual(response.status_code,200,response.text)
        spec=response.json()['request'];self.assertEqual(spec['end_seconds']-spec['start_seconds'],30)
        self.assertEqual(spec['renderer_version'],'electric-preview-tone-v1')
        self.assertEqual(spec['electric_tone']['overload'],'refuse')
        for tone in ({'mode':'clean','overload':'report'},{'mode':'clean','gain_db':True},{'unexpected':1}):
            self.assertEqual(self.submit(str(uuid.uuid4()),electric_tone=tone).status_code,400)
        self.assertEqual(self.submit(str(uuid.uuid4()),instrument='acoustic',electric_tone={}).status_code,400)

    def test_queued_tone_retry_preserves_old_versions_but_stale_dispatch_refuses(self):
        operation=str(uuid.uuid4());options={'electric_tone':{'mode':'clean','gain_db':-3}}
        first=self.submit(operation,**options).json()
        with patch('audio.electric_tone.VERSION','changed-component'):
            retry=self.submit(operation,**options)
            self.assertEqual(retry.status_code,200,retry.text);self.assertEqual(retry.json()['request'],first['request'])
            self.gate.check.return_value=(True,None)
            self.client.portal.call(self.app.state.processing_tick)
        self.assertEqual(self.client.get('/api/processing-queue/'+operation).json()['state'],'failed')
        self.assertFalse(self.children)

    def test_lost_reply_replays_frozen_profile_but_new_dispatch_rejects_stale_version(self):
        operation = str(uuid.uuid4())
        first = self.submit(operation).json()
        with patch('audio.preview_instruments.ELECTRIC_PROFILE_VERSION', 'changed-electric-profile'):
            retry = self.submit(operation)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(retry.json()['job_id'], first['job_id'])
            self.assertEqual(retry.json()['request'], first['request'])
            self.assertEqual(self.submit(operation, instrument='acoustic').status_code, 409)
            self.gate.check.return_value = (True, None)
            self.client.portal.call(self.app.state.processing_tick)
        receipt = self.client.get('/api/processing-queue/' + operation).json()
        self.assertEqual(receipt['state'], 'failed')
        self.assertFalse(self.children)

    def test_completed_preview_with_missing_dry_evidence_is_retained_and_not_reused(self):
        job = self.client.post('/api/arrange-preview', json=self.request).json()
        library = self.app.state.library
        staged = library.stage / 'test-preview'
        staged.write_bytes(b'saved-preview')
        asset = library.publish(staged, 'preview.wav', 'audio/wav',
            kind='electric-preview-1', source_id=self.asset['source_id'])
        retained = {'asset': asset, 'arrangement': {'instrument': 'electric'},
                    'notice': 'Original failed evidence remains available for diagnosis.'}
        library.update_job(job['id'], 'complete', 1, result=retained)
        self.children[-1].done = True
        checked = self.client.get('/api/jobs/' + job['id']).json()
        self.assertEqual(checked['state'], 'interrupted')
        self.assertEqual(checked['result'], retained)
        self.assertEqual((library.media / self.asset['sha256']).read_bytes(), b'original-unchanged')
        self.assertEqual((library.media / asset['sha256']).read_bytes(), b'saved-preview')

    def test_unverifiable_cleanup_reference_does_not_block_unrelated_startup(self):
        import json
        import time
        library=self.app.state.library
        directory=library.root/'render-evidence';directory.mkdir()
        preserved=directory/'unknown-research-data';preserved.write_bytes(b'preserve')
        with library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                (str(uuid.uuid4()),'complete',1,'{}',json.dumps({'render_evidence':{}}),None,time.time()))
        self.client.__exit__(None,None,None)
        self.app=server.create_app(self.temp.name)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765')
        self.client.__enter__();self.client.get('/session')
        status=self.client.get('/api/status').json()
        self.assertTrue(status['ready'])
        self.assertIn('preserved',status['render_evidence_attention'])
        self.assertEqual(preserved.read_bytes(),b'preserve')
        self.assertEqual((library.media/self.asset['sha256']).read_bytes(),b'original-unchanged')


if __name__ == '__main__':
    unittest.main()
