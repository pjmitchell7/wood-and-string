"""Real API/catalog and revision worker, with silent stubbed native rendering."""
import copy
from dataclasses import asdict
import io
import json
import tempfile
import time
import unittest
from unittest import mock
import uuid
import wave
import httpx

from fastapi.testclient import TestClient
from audio.performance import Note, Performance, arrange
import server
import worker
from gateway import create_gateway


class Child:
    def __init__(self, *args, **kwargs):
        self.done = False
        self.stdin = io.BytesIO()
    def poll(self): return 0 if self.done else None
    def wait(self, timeout=None): self.done = True; return 0
    def terminate(self): self.done = True


class RefinementPipeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = server.create_app(self.temp.name)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.client.get('/session')
        data = io.BytesIO()
        with wave.open(data, 'wb') as wav:
            wav.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\0' * 44100 * 2 * 2 * 2)
        self.original_bytes = data.getvalue()
        self.source = self.client.post('/api/import', content=self.original_bytes,
                                       headers={'X-Filename':'original.wav'}).json()
        self.library = self.app.state.library
        self.performance = Performance(self.source['source_id'], 44100, [
            Note('a', 12000, 18000, 64, confidence=.8),
            Note('b', 19000, 23000, 64, confidence=.3),
            Note('c', 30000, 40000, 67),
            Note('bass', 12000, 50000, 40, role='bass')])
        arrangement = arrange(self.performance, 1)
        arrangement['render_origin_sample'] = 11025
        stage = self.library.stage/'parent-fixture'
        stage.write_bytes(b'silent-parent-preview-fixture')
        asset = self.library.publish(stage, 'parent.wav', 'audio/wav', kind='mixed-preview-1',
                                     source_id=self.source['source_id'])
        self.parent_id = str(uuid.uuid4())
        self.parent_spec = dict(kind='mixed', asset_id=self.source['id'], sha256=self.source['sha256'],
                                performers=1, start_seconds=.25, end_seconds=1.25,
                                renderer_version=server.RENDER_VERSION)
        self.parent_result = dict(asset=asset, arrangement=arrangement,
                                  analysis={'warnings':['Uncertain source evidence']}, notice='Baseline')
        self.insert(self.parent_id, self.parent_spec, 'complete', self.parent_result)
        self.children = []
        def spawn(*args, **kwargs):
            child = Child(); self.children.append(child); return child
        self.patcher = mock.patch('server.subprocess.Popen', side_effect=spawn)
        self.spawn = self.patcher.start()
        self.ops = [{'kind':'join', 'note_ids':['b','a']}]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patcher.stop()
        self.temp.cleanup()

    def insert(self, identity, spec, state='queued', result=None):
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (identity, state, 1 if state=='complete' else 0, json.dumps(spec),
                        json.dumps(result) if result else None, None, time.time()))

    def post(self, **changes):
        return self.client.post('/api/refine-preview', json={
            'parent_job_id':self.parent_id, 'operations':self.ops, **changes})

    def execute(self, job, callback=None):
        def render(arrangement, path, **kwargs):
            if callback: callback(arrangement)
            path.write_bytes(b'silent-revised-preview-' + job['id'].encode())
            return {'renderer_version':server.RENDER_VERSION, 'clipped_samples':0}
        with mock.patch('worker.run_worker', side_effect=AssertionError('Analyzer must not run')) as analyzer, \
             mock.patch('audio.analysis_runtime.require_runtime', side_effect=AssertionError('Runtime not needed')), \
             mock.patch('worker.render', side_effect=render) as renderer:
            worker.run(self.temp.name, job['id'])
        self.children[-1].done = True
        analyzer.assert_not_called()
        self.assertEqual((self.library.media/self.source['sha256']).read_bytes(), self.original_bytes)
        self.assertFalse(any(self.library.stage.iterdir()))
        return self.library.job(job['id']), renderer

    def test_saved_revision_preserves_source_and_parent_without_analysis(self):
        before = copy.deepcopy(self.library.job(self.parent_id))
        response = self.post(); self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        def check(arrangement):
            self.assertEqual(arrangement['source'], asdict(self.performance))
            revised = arrangement['refinement_source']['notes']
            self.assertEqual(len(revised), 3)
            joined = next(n for n in revised if n['id'].startswith('joined-'))
            self.assertEqual((joined['start'],joined['end'],joined['confidence']), (12000,23000,.3))
            self.assertEqual(arrangement['render_origin_sample'], 11025)
            receipt = arrangement['refinement']
            self.assertEqual(receipt['parent_job_id'], self.parent_id)
            self.assertEqual(receipt['edits'][0]['unobserved_gaps'], [{'start':18000,'end':19000}])
        result, rendering = self.execute(job, check)
        self.assertEqual(result['state'], 'complete', result)
        rendering.assert_called_once()
        self.assertEqual(result['result']['analysis'], self.parent_result['analysis'])
        self.assertEqual(self.library.job(self.parent_id), before)
        self.assertEqual(result['result']['asset']['kind'], 'mixed-refinement-1')
        self.assertEqual(result['result']['asset']['source_id'], self.source['source_id'])

    def test_normalized_retries_and_completed_retry_do_not_spawn(self):
        first = self.post().json()
        self.assertEqual(self.post(operations=[{'kind':'join','note_ids':['a','b']}]).json()['id'], first['id'])
        self.assertEqual(self.spawn.call_count, 1)
        self.execute(first)
        self.assertEqual(self.post().json()['state'], 'complete')
        self.assertEqual(self.spawn.call_count, 1)

    def test_latest_saved_preview_excludes_running_diagnostic_and_failed_jobs(self):
        self.assertEqual(self.client.get('/api/previews/latest').json()['id'], self.parent_id)
        self.insert(str(uuid.uuid4()), {'kind':'diagnostic'}, 'complete', self.parent_result)
        self.insert(str(uuid.uuid4()), {**self.parent_spec}, 'failed')
        self.assertEqual(self.client.get('/api/previews/latest').json()['id'], self.parent_id)
        pending = self.post().json()
        self.assertEqual(self.client.get('/api/previews/latest').json()['id'], self.parent_id)
        finished, _ = self.execute(pending)
        self.assertEqual(self.client.get('/api/previews/latest').json()['id'], finished['id'])
        self.assertEqual(self.spawn.call_count, 1)

    def test_latest_saved_preview_missing_or_corrupt_is_unavailable(self):
        path = self.library.media/self.parent_result['asset']['sha256']
        path.write_bytes(b'corrupt')
        self.assertEqual(self.client.get('/api/previews/latest').status_code, 404)
        with self.library.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?',(self.parent_id,)).fetchone()[0], 'interrupted')
            db.execute('DELETE FROM jobs')
        self.assertEqual(self.client.get('/api/previews/latest').status_code, 404)
        self.spawn.assert_not_called()

    def test_revision_of_revision_keeps_original_and_uses_current_notes(self):
        first, _ = self.execute(self.post().json())
        joined = next(n['id'] for n in first['result']['arrangement']['refinement_source']['notes']
                      if n['id'].startswith('joined-'))
        response = self.post(parent_job_id=first['id'], operations=[{'kind':'omit','note_ids':['c']}])
        self.assertEqual(response.status_code, 200, response.text)
        second, _ = self.execute(response.json())
        self.assertEqual(second['state'], 'complete', second)
        arrangement = second['result']['arrangement']
        self.assertEqual(arrangement['source'], self.library.job(self.parent_id)['result']['arrangement']['source'])
        self.assertEqual({n['id'] for n in arrangement['refinement_source']['notes']}, {joined,'bass'})
        self.assertEqual(arrangement['refinement']['parent_job_id'], first['id'])
        self.assertEqual(self.library.job(first['id']), first)

    def test_invalid_empty_unknown_duplicate_and_all_omitted_rejected(self):
        for operations in ([], [{'kind':'omit','note_ids':['unknown']}],
                           [{'kind':'omit','note_ids':['a','a']}],
                           [{'kind':'join','note_ids':['a','c']}],
                           [{'kind':'omit','note_ids':['a','b','c','bass']}]):
            with self.subTest(operations=operations):
                self.assertEqual(self.post(operations=operations).status_code, 400)
        self.assertEqual(self.post(parent_job_id='missing').status_code, 400)
        self.assertEqual(self.client.post('/api/refine-preview', json={
            'parent_job_id':self.parent_id, 'operations':self.ops, 'extra':True}).status_code, 400)
        self.assertEqual(self.client.post('/api/refine-preview', content=b'x'*65537).status_code, 413)
        self.spawn.assert_not_called()

    def test_parent_change_after_admission_is_refused_before_render(self):
        job = self.post().json()
        changed = {**self.parent_result, 'notice':'Changed after admission'}
        self.library.update_job(self.parent_id, 'complete', 1, changed)
        result, rendering = self.execute(job)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('parent preview changed', result['error'])
        rendering.assert_not_called()

    def test_cancellation_before_start_does_not_render(self):
        job = self.post().json()
        self.client.post('/api/jobs/'+job['id']+'/cancel')
        result, rendering = self.execute(job)
        self.assertEqual(result['state'], 'cancelled')
        rendering.assert_not_called()

    def test_incomplete_or_corrupt_parent_and_original_are_refused(self):
        self.library.update_job(self.parent_id, 'failed', error='fixture')
        self.assertEqual(self.post().status_code, 400)
        self.insert(self.parent_id, self.parent_spec, 'complete', self.parent_result)
        parent_path = self.library.media/self.parent_result['asset']['sha256']
        parent_path.write_bytes(b'corrupt')
        self.assertEqual(self.post().status_code, 400)
        parent_path.write_bytes(b'silent-parent-preview-fixture')
        (self.library.media/self.source['sha256']).write_bytes(b'corrupt')
        self.assertEqual(self.post().status_code, 400)
        self.spawn.assert_not_called()

    def test_duet_and_melody_only_retain_required_roles(self):
        spec = {**self.parent_spec, 'performers':2}
        self.insert(self.parent_id, spec, 'complete', self.parent_result)
        self.assertEqual(self.post(operations=[{'kind':'omit','note_ids':['bass']}]).status_code, 400)
        spec = {**self.parent_spec, 'guidance':{'accompaniment':'melody-only'}}
        self.insert(self.parent_id, spec, 'complete', self.parent_result)
        self.assertEqual(self.post(operations=[{'kind':'omit','note_ids':['a','b','c']}]).status_code, 400)

    def test_serial_admission_space_and_spawn_failure(self):
        first = self.post().json()
        self.assertEqual(self.post(operations=[{'kind':'omit','note_ids':['c']}]).status_code, 409)
        self.children[-1].done = True
        self.library.update_job(first['id'], 'failed', error='fixture')
        with mock.patch.object(type(self.library), 'space', side_effect=ValueError('reserve')):
            self.assertEqual(self.post().status_code, 507)
        with mock.patch('server.subprocess.Popen', side_effect=OSError('fixture')):
            self.assertEqual(self.post().json()['state'], 'failed')


class RefinementGateway(unittest.TestCase):
    def test_paired_route_body_limit_and_static_module(self):
        forwarded = []
        def authority(request):
            if request.url.path == '/session':
                return httpx.Response(200, json={'ready':True}, headers={'Set-Cookie':'guitar_session=local; Path=/'})
            forwarded.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b'{"state":"queued"}'),
                                  headers={'Content-Type':'application/json'})
        with tempfile.TemporaryDirectory() as root:
            app = create_gateway(root, httpx.MockTransport(authority))
            origin = app.state.origin
            with TestClient(app, base_url=origin, headers={'X-Forwarded-Proto':'https','Origin':origin}) as client:
                self.assertEqual(client.post('/api/refine-preview', json={}).status_code, 401)
                self.assertEqual(client.get('/api/previews/latest').status_code, 401)
                pending = client.post('/pair/request', json={'label':'silent-refinement-check'}).json()
                app.state.pairing.approve_local(pending['id'])
                client.post('/pair/claim', json={'id':pending['id'],'secret':pending['secret']})
                content = json.dumps({'parent_job_id':str(uuid.uuid4()), 'operations':[
                    {'kind':'omit', 'note_ids':['note-'+str(i) for i in range(500)]}]}).encode()
                self.assertGreater(len(content), 4096)
                self.assertEqual(client.post('/api/refine-preview', content=content).status_code, 200)
                self.assertEqual(forwarded[-1].content, content)
                count = len(forwarded)
                self.assertEqual(client.post('/api/refine-preview', content=b'x'*65537).status_code, 413)
                self.assertEqual(len(forwarded), count)
                self.assertEqual(client.get('/refinement.mjs?v=20').status_code, 200)
                self.assertEqual(client.get('/api/previews/latest').status_code, 200)
                self.assertEqual(forwarded[-1].url.path, '/api/previews/latest')


if __name__ == '__main__': unittest.main()
