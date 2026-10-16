"""Bass selection through real API/catalog/worker, without native sound output."""
import copy
from dataclasses import asdict
import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient
import server
import worker
from audio.mixed_analysis import required_parts
from audio.preview_instruments import selection_fields, BASS_RENDER_VERSION
from tests.test_mixed_preview import FakeProcess, RUNTIME


class BassPreview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = server.create_app(self.temp.name)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.client.__enter__(); self.client.get('/session')
        raw = io.BytesIO()
        with wave.open(raw, 'wb') as wav:
            wav.setparams((1, 2, 44100, 0, 'NONE', 'uncompressed')); wav.writeframes(b'\0'*88200)
        self.original = raw.getvalue()
        self.asset = self.client.post('/api/import', content=self.original,
                                     headers={'X-Filename':'song.wav'}).json()
        self.library = self.app.state.library
        self.children = []
        def spawn(*args, **kwargs):
            child = FakeProcess(); self.children.append(child); return child
        self.spawn = patch('server.subprocess.Popen', side_effect=spawn); self.spawn.start()
        self.runtime = patch('server.mixed_runtime_status', return_value=RUNTIME); self.runtime.start()
        self.bank = patch('server.bass_runtime_status', return_value={'ready':True}); self.bank.start()
        self.requests = []

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.spawn.stop(); self.runtime.stop(); self.bank.stop(); self.temp.cleanup()

    def post(self, **changes):
        return self.client.post('/api/arrange-preview', json={
            'asset_id':self.asset['id'], 'start_seconds':0, 'end_seconds':1,
            'performers':1, 'instrument':'jazz-bass', **changes})

    def execute(self, job, *, bass_only=False, revision=False):
        def analysis(module, request, **kwargs):
            self.requests.append(copy.deepcopy(request))
            notes = [dict(id='b',start=1000,end=20000,pitch=33,role='bass')]
            if not bass_only:
                notes += [dict(id='m1',start=1000,end=9000,pitch=64,role='melody'),
                          dict(id='m2',start=10000,end=18000,pitch=64,role='melody')]
            envelope = dict(performance=dict(source_id=self.asset['source_id'], sample_rate=44100, notes=notes),
                            report=dict(warnings=['Test estimates']), render_origin_sample=0)
            raw = json.dumps(envelope).encode(); path = worker.Path(request['result_path']); path.write_bytes(raw)
            return dict(result=dict(result_bytes=len(raw),result_sha256=hashlib.sha256(raw).hexdigest()),metrics={})
        def render(arrangement, path, **kwargs):
            self.assertEqual(arrangement['instrument'], 'jazz-bass')
            path.write_bytes(b'silent-bass-output-'+job['id'].encode())
            return dict(renderer_version=BASS_RENDER_VERSION,clipped_samples=0)
        with patch('audio.analysis_runtime.require_runtime',return_value=RUNTIME), \
             patch('worker.run_worker', side_effect=analysis) as analyzer, \
             patch('worker.render_bass',side_effect=render) as bass, \
             patch('worker.render',side_effect=AssertionError('Acoustic renderer selected for Bass')) as acoustic:
            worker.run(self.temp.name, job['id'])
        self.children[-1].done = True
        if revision: analyzer.assert_not_called()
        acoustic.assert_not_called()
        self.assertEqual((self.library.media/self.asset['sha256']).read_bytes(),self.original)
        self.assertFalse(list(self.library.stage.iterdir()))
        return self.library.job(job['id']),bass

    def test_explicit_selection_and_acoustic_identity(self):
        self.assertEqual(selection_fields({},1),{})
        self.assertEqual(selection_fields({'instrument':'acoustic'},2),{})
        for value in ({'instrument':'electric'},{'bass_part':'melody'},
                      {'instrument':'jazz-bass','bass_part':'melody-and-bass'}):
            with self.subTest(value=value),self.assertRaises(ValueError): selection_fields(value,1)
        for value in (None,[],['other'],['bass','melody'],['melody','melody']):
            with self.subTest(value=value),self.assertRaises(ValueError):required_parts({'required_roles':value})
        self.assertEqual(required_parts({}),('melody',))
        self.assertEqual(required_parts({'required_roles':['bass']}),('bass',))

    def test_api_versions_retry_and_unavailable_mode(self):
        job = self.post().json(); spec = job['request']
        self.assertEqual(spec['instrument'],'jazz-bass'); self.assertEqual(spec['bass_part'],'melody')
        self.assertEqual(spec['renderer_version'],BASS_RENDER_VERSION)
        self.assertEqual(self.post(bass_part='melody').json()['id'],job['id'])
        self.library.update_job(job['id'],'failed',error='Test terminal'); self.children[-1].done=True
        duo = self.post(performers=2).json()
        self.assertNotEqual(duo['id'],job['id']);self.assertEqual(duo['request']['bass_part'],'melody-and-bass')
        for changes in ({'instrument':'electric'},{'bass_part':'unknown'},{'performers':2,'bass_part':'bass-line'},
                        {'guidance':{'accompaniment':'melody-only'}}):
            self.assertEqual(self.post(**changes).status_code,400)
        with patch('server.bass_runtime_status',return_value={'ready':False}):
            self.assertFalse(self.client.get('/api/status').json()['bass_ready'])
            self.assertEqual(self.post().status_code,503)

    def test_bass_line_works_without_melody_and_keeps_analysis_unconditioned(self):
        job = self.post(bass_part='bass-line',guidance={'source_notes':'Focus on the groove'}).json()
        result,rendered = self.execute(job,bass_only=True)
        self.assertEqual(result['state'],'complete',result);rendered.assert_called_once()
        self.assertEqual(self.requests[0]['required_roles'],['bass'])
        self.assertNotIn('guidance',self.requests[0])
        arrangement=result['result']['arrangement']
        self.assertEqual({n['role'] for n in arrangement['notes']},{'bass'})
        self.assertEqual(arrangement['source']['notes'][0]['pitch'],33)
        self.assertEqual(result['result']['asset']['kind'],'bass-preview-1-bass-line')

    def test_solo_duo_and_stale_profile_fail_before_analysis(self):
        job = self.post(performers=2).json()
        result,rendered = self.execute(job)
        self.assertEqual(result['state'],'complete',result);rendered.assert_called_once()
        a=result['result']['arrangement'];self.assertEqual({n['player'] for n in a['notes']},{1,2})
        self.assertEqual({n['pitch'] for n in a['source']['notes']},{33,64})
        stale = self.post().json(); spec=stale['request'];spec['bass_profile_version']='stale'
        with self.library.connect() as db:
            db.execute('UPDATE jobs SET request=? WHERE id=?',(json.dumps(spec),stale['id']))
        result,rendered = self.execute(stale)
        self.assertEqual(result['state'],'failed');rendered.assert_not_called()
        self.assertIn('profile changed',result['error'])

    def test_bass_revision_keeps_instrument_source_parent_and_selected_part(self):
        first,_ = self.execute(self.post().json())
        self.assertEqual(first['state'],'complete',first)
        before=copy.deepcopy(first)
        response=self.client.post('/api/refine-preview',json={'parent_job_id':first['id'],
            'operations':[{'kind':'join','note_ids':['m1','m2']}]})
        self.assertEqual(response.status_code,200,response.text)
        revised,renderer=self.execute(response.json(),revision=True)
        self.assertEqual(revised['state'],'complete',revised);renderer.assert_called_once()
        a=revised['result']['arrangement']
        self.assertEqual(a['source'],first['result']['arrangement']['source'])
        self.assertEqual(a['part_selection'],'melody')
        self.assertEqual(len(a['refinement_source']['notes']),2)
        self.assertEqual(self.library.job(first['id']),before)
        self.assertEqual(revised['result']['asset']['kind'],'bass-refinement-1-melody')
        denied=self.client.post('/api/refine-preview',json={'parent_job_id':first['id'],
            'operations':[{'kind':'omit','note_ids':['m1','m2']}]})
        self.assertEqual(denied.status_code,400)


if __name__=='__main__':unittest.main()
