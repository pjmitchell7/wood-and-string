"""Silent temporary PCM/API, pairing and fake-dispatch checks. No native work."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid

import httpx
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import gateway
import server

VERSION = 'acoustic-companion-v1'
RUNTIME = {'ready': True, 'manifest': {'runtime_version': 'authored-api-runtime'}}


def job_id(spec):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(spec, sort_keys=True)))


class FakeProcess:
    def __init__(self):
        self.stdin = io.BytesIO()
        self.done = False
    def poll(self): return 0 if self.done else None
    def wait(self, timeout=None): self.done = True; return 0
    def terminate(self): self.done = True


class CompanionPartsAPI(unittest.TestCase):
    def setUp(self):
        from acoustic_companion import AcousticCompanion
        from audio import render as renderer
        from tests.test_acoustic_stem_capture import fixture, ORIGIN, FRAMES
        from tests.test_acoustic_stem_render import fake_boundary
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        stack = self.enterContext(ExitStack())
        gate = Mock()
        gate.check.return_value = (False, 'Authored resource wait')
        stack.enter_context(patch('server.Admission', return_value=gate))
        stack.enter_context(patch('server.subprocess.Popen', side_effect=AssertionError('No real worker allowed')))
        self.app = server.create_app(self.temp.name)
        self.client = self.enterContext(TestClient(self.app, base_url='http://127.0.0.1:8765'))
        self.client.get('/session')
        self.library = self.app.state.library
        staged = self.library.stage / 'authored-original.partial'
        staged.write_bytes(b'authored source identity; never decoded or played')
        self.source = self.library.publish(staged, 'authored.wav', 'audio/wav')
        _, arrangement = fixture()
        arrangement['source_id'] = self.source['source_id']
        arrangement['source']['source_id'] = self.source['source_id']
        self.spec = dict(kind='mixed', asset_id=self.source['id'], sha256=self.source['sha256'], performers=2,
            start_seconds=ORIGIN / 44100, end_seconds=(ORIGIN + FRAMES - 44100) / 44100,
            renderer_version=server.RENDER_VERSION, acoustic_companion_version=VERSION)
        self.identity = job_id(self.spec)
        self.insert(self.spec, None, state='queued')
        output = self.library.stage / (self.identity + '.wav')
        with self.library.exclusive('worker.lock'):
            producer = AcousticCompanion(self.library, self.identity, self.spec, self.source, arrangement)
            try:
                self.assertIsNotNone(producer.capture)
                boundary, api = fake_boundary()
                with boundary:
                    metrics = renderer.render(arrangement, output, stem_capture=producer.capture)
                self.assertFalse(api.states)
                companion = producer.finish(output, metrics)
                self.assertEqual(companion['status'], 'available', companion)
                self.master = self.library.publish(output, 'authored-master.wav', 'audio/wav',
                    kind='mixed-preview-2', source_id=self.source['source_id'])
            finally:
                producer.cleanup()
        self.result = {'asset': self.master, 'arrangement': arrangement, 'metrics': metrics,
                       'analysis': {'warnings': []}, 'notice': 'Authored test fixture', 'acoustic_companion': companion}
        self.insert(self.spec, self.result)
        self.prefix = '/api/jobs/' + self.identity + '/parts'
        self.bundle = self.library.root / 'stem-bundles' / companion['reference']['bundle_id']
        self.original_bytes = (self.library.media / self.source['sha256']).read_bytes()
        self.master_bytes = (self.library.media / self.master['sha256']).read_bytes()

    def insert(self, spec, result, state='complete'):
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                (self.identity, state, 1 if state == 'complete' else 0, json.dumps(spec),
                 json.dumps(result) if result is not None else None, None, 1234.0))

    def assert_unchanged(self, before):
        self.assertEqual(self.library.job(self.identity), before)
        self.assertEqual((self.library.media / self.master['sha256']).read_bytes(), self.master_bytes)
        self.assertEqual((self.library.media / self.source['sha256']).read_bytes(), self.original_bytes)

    def test_metadata_and_fixed_wav_responses_bind_exact_saved_job_and_hashes(self):
        before = self.library.job(self.identity)
        response = self.client.get(self.prefix)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(set(value), {'version', 'manifest', 'manifest_sha256', 'binding', 'media'})
        self.assertEqual(value['version'], VERSION)
        self.assertEqual(value['manifest_sha256'], self.result['acoustic_companion']['reference']['bundle_id'])
        self.assertEqual(value['binding'], {key: value['manifest'][key] for key in ('source', 'clock', 'arrangement', 'renderer')})
        self.assertEqual(value['binding']['arrangement']['revision'], self.identity)
        self.assertEqual(value['binding']['source'], {'id': self.source['source_id'], 'sha256': self.source['sha256']})
        self.assertNotIn(str(self.library.root), response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(set(value['media']), {'performer-1.wav', 'performer-2.wav', 'combined.wav'})
        for name, member in value['media'].items():
            self.assertEqual(member['url'], self.prefix + '/' + name)
            audio = self.client.get(member['url'])
            self.assertEqual(audio.status_code, 200, audio.text[:200])
            self.assertEqual(audio.content, (self.bundle / name).read_bytes())
            self.assertEqual(hashlib.sha256(audio.content).hexdigest(), member['sha256'])
            self.assertEqual(audio.headers['x-content-sha256'], member['sha256'])
            self.assertEqual(audio.headers['content-length'], str(member['bytes']))
            self.assertEqual(audio.headers['content-type'], 'audio/wav')
            self.assertEqual(audio.headers['cache-control'], 'no-store')
            self.assertEqual(audio.headers['accept-ranges'], 'none')
            self.assertNotEqual(audio.content[-4:], b'\0' * 4)
        self.assert_unchanged(before)

    def test_authentication_completion_legacy_and_unavailable_controls(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get(self.prefix).status_code, 401)
        self.assertEqual(self.client.get(self.prefix + '/performer-1.wav').status_code, 401)
        self.client.get('/session')
        self.assertEqual(self.client.get('/api/jobs/not-a-uuid/parts').status_code, 404)
        self.assertEqual(self.client.get('/api/jobs/' + str(uuid.uuid4()) + '/parts').status_code, 404)
        self.insert(self.spec, self.result, state='running')
        before = self.library.job(self.identity)
        self.assertEqual(self.client.get(self.prefix).status_code, 409)
        self.assert_unchanged(before)
        legacy = {key: value for key, value in self.spec.items() if key != 'acoustic_companion_version'}
        result = {key: value for key, value in self.result.items() if key != 'acoustic_companion'}
        self.insert(legacy, result)
        before = self.library.job(self.identity)
        self.assertEqual(self.client.get(self.prefix).status_code, 404)
        self.assert_unchanged(before)
        result = deepcopy(self.result)
        result['acoustic_companion'] = {'version': VERSION, 'status': 'unavailable',
                                       'diagnostic': {'stage': 'publication', 'reason': 'Authored refusal'}}
        self.insert(self.spec, result)
        before = self.library.job(self.identity)
        self.assertEqual(self.client.get(self.prefix).status_code, 404)
        self.assertEqual(self.client.get(self.prefix + '/performer-1.wav').status_code, 404)
        self.assert_unchanged(before)

    def test_companion_corruption_refuses_controls_while_master_and_job_stay_complete(self):
        for name in ('performer-1.wav', 'manifest.json', 'renderer-receipt.json', 'verification.json'):
            path = self.bundle / name
            raw = path.read_bytes()
            path.write_bytes(raw + b'authored corruption')
            before = self.library.job(self.identity)
            with self.subTest(name=name):
                self.assertEqual(self.client.get(self.prefix).status_code, 409)
                self.assertEqual(self.client.get(self.prefix + '/performer-1.wav').status_code, 409)
                self.assertEqual(self.client.get('/api/jobs/' + self.identity).json()['state'], 'complete')
                self.assert_unchanged(before)
                self.assertEqual(path.read_bytes(), raw + b'authored corruption')
            path.write_bytes(raw)

    def test_source_master_reference_arrangement_and_revision_tampering_refuse_without_job_writes(self):
        changes = [lambda spec, result: spec.update(sha256='f' * 64),
                   lambda spec, result: result['asset'].update(source_id='foreign-source'),
                   lambda spec, result: result['acoustic_companion']['reference']['manifest'].update(sha256='f' * 64),
                   lambda spec, result: result['acoustic_companion'].update(arrangement_sha256='f' * 64),
                   lambda spec, result: result['arrangement']['notes'][0].update(velocity=1)]
        for change in changes:
            spec, result = deepcopy(self.spec), deepcopy(self.result)
            change(spec, result)
            self.insert(spec, result)
            before = self.library.job(self.identity)
            self.assertEqual(self.client.get(self.prefix).status_code, 409)
            self.assertEqual(self.client.get(self.prefix + '/combined.wav').status_code, 409)
            self.assert_unchanged(before)
        self.insert(self.spec, self.result)
        other_id = str(uuid.uuid4())
        with self.library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (other_id, 'complete', 1, json.dumps(self.spec), json.dumps(self.result), None, 1234.0))
        self.assertEqual(self.client.get('/api/jobs/' + other_id + '/parts').status_code, 409)
        self.assertEqual(self.library.job(other_id)['state'], 'complete')
        for asset in (self.source, self.master):
            path = self.library.media / asset['sha256']
            raw = path.read_bytes()
            path.write_bytes(raw + b'changed asset')
            before = self.library.job(self.identity)
            self.assertEqual(self.client.get(self.prefix).status_code, 409)
            self.assertEqual(self.library.job(self.identity), before)
            path.write_bytes(raw)
        self.assert_unchanged(self.library.job(self.identity))

    def test_unknown_traversal_and_private_metadata_files_are_never_media(self):
        for suffix in ('/performer-3.wav', '/original.wav', '/manifest.json', '/renderer-receipt.json',
                       '/verification.json', '/performer-1.f64', '/%2e%2e%5coriginal.wav', '/combined.wav/extra'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.get(self.prefix + suffix).status_code, 404)
        self.assertEqual(self.client.get('/api/jobs/' + self.identity.upper() + '/parts').status_code, 404)

    def test_real_protected_readers_live_through_response_and_close_on_disconnect(self):
        route = next(route for route in self.app.routes if getattr(route, 'path', None) == '/api/jobs/{identity}/parts/{name}')
        path = self.bundle / 'performer-1.wav'
        async def run(fail):
            response = route.endpoint(self.identity, 'performer-1.wav')
            with self.assertRaises(OSError):
                path.open('r+b')
            chunks = []
            async def send(message):
                with self.assertRaises(OSError):
                    path.open('r+b')
                if message['type'] == 'http.response.body':
                    chunks.append(message.get('body', b''))
                    if fail:
                        raise OSError('Authored disconnect during protected stream')
            async def receive():
                await asyncio.Event().wait()
            if fail:
                with self.assertRaises(ClientDisconnect):
                    await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
            else:
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
                self.assertEqual(b''.join(chunks), path.read_bytes())
            with path.open('r+b'):
                pass  # Every held reader has closed, including after disconnect.
        for fail in (False, True):
            with self.subTest(fail=fail):
                asyncio.run(run(fail))


class CompanionQueueAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.children = []
        self.gate = Mock()
        self.gate.check.return_value = (False, 'Authored resource wait')
        self.gate.before_start.return_value = (True, None)
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch('server.Admission', return_value=self.gate))
        stack.enter_context(patch('server.mixed_runtime_status', return_value=RUNTIME))
        stack.enter_context(patch('server.bass_runtime_status', return_value={'ready': True}))
        stack.enter_context(patch('server.electric_runtime_status', return_value={'ready': True}))
        stack.enter_context(patch('server.instrumental_runtime_status', return_value={'ready': False}))
        stack.enter_context(patch('server.subprocess.Popen', side_effect=self.spawn))
        self.app = server.create_app(self.temp.name)
        self.client = self.enterContext(TestClient(self.app, base_url='http://127.0.0.1:8765'))
        self.client.get('/session')
        self.library = self.app.state.library
        self.source = self.client.post('/api/import', content=b'authored unchanged source',
                                       headers={'X-Filename': 'original.wav'}).json()
        self.choices = {'asset_id': self.source['id'], 'start_seconds': 0, 'end_seconds': 1, 'performers': 1}

    def spawn(self, *args, **kwargs):
        child = FakeProcess()
        self.children.append(child)
        return child

    def submit(self, operation=None, **changes):
        return self.client.post('/api/processing-queue', json={'operation_id': operation or str(uuid.uuid4()),
                                                            'request': {**self.choices, **changes}})

    def tick(self):
        self.client.portal.call(self.app.state.processing_tick)

    def legacy(self, **changes):
        fresh = self.submit().json()
        self.app.state.processing.cancel(fresh['operation_id'])
        spec = {key: value for key, value in fresh['request'].items() if key != 'acoustic_companion_version'}
        spec.update(changes)
        operation = str(uuid.uuid4())
        self.app.state.processing.submit(operation, spec, job_id=job_id(spec))
        return operation, spec

    def test_new_acoustic_requests_pin_capability_without_changing_operation_identity(self):
        status = self.client.get('/api/status').json()
        self.assertEqual(status['acoustic_companion_version'], VERSION)
        self.assertIs(status['acoustic_companion_supported'], True)
        operation = str(uuid.uuid4())
        response = self.submit(operation)
        self.assertEqual(response.status_code, 200, response.text)
        first = response.json()
        self.assertEqual(first['operation_id'], operation)
        self.assertEqual(first['request']['acoustic_companion_version'], VERSION)
        self.assertEqual(first['job_id'], job_id(first['request']))
        self.assertEqual(self.submit(operation).json()['job_id'], first['job_id'])
        old = {key: value for key, value in first['request'].items() if key != 'acoustic_companion_version'}
        self.assertNotEqual(first['job_id'], job_id(old))
        immediate = self.client.post('/api/arrange-preview', json=self.choices)
        self.assertEqual(immediate.status_code, 200, immediate.text)
        self.assertEqual(immediate.json()['request']['acoustic_companion_version'], VERSION)

    def test_legacy_retry_and_dispatch_preserve_absence_and_old_job_identity(self):
        operation, spec = self.legacy()
        expected = job_id(spec)
        retry = self.submit(operation)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()['request'], spec)
        self.assertEqual(retry.json()['job_id'], expected)
        # An already saved operation remains frozen even if latest selection is supplied.
        explicit = self.submit(operation, acoustic_companion_version=VERSION)
        self.assertEqual(explicit.status_code, 200, explicit.text)
        self.assertEqual(explicit.json()['request'], spec)
        self.gate.check.return_value = (True, None)
        self.tick()
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.library.job(expected)['request'], spec)
        self.assertNotIn('acoustic_companion_version', self.library.job(expected)['request'])

    def test_wrong_or_nonacoustic_versions_refuse_and_dispatch_never_upgrades(self):
        for changes in ({'acoustic_companion_version': 'future'}, {'acoustic_companion_version': True},
                        {'instrument': 'jazz-bass', 'acoustic_companion_version': VERSION},
                        {'instrument': 'electric', 'acoustic_companion_version': VERSION}):
            with self.subTest(changes=changes):
                self.assertEqual(self.submit(**changes).status_code, 400)
        operation, spec = self.legacy(acoustic_companion_version='future')
        self.gate.check.return_value = (True, None)
        self.tick()
        self.assertEqual(self.app.state.processing.get(operation)['state'], 'failed')
        self.assertEqual(self.children, [])
        self.assertEqual(self.app.state.processing.get(operation)['request'], spec)

    def test_legacy_companion_freeze_keeps_current_runtime_check(self):
        operation, spec = self.legacy()
        self.gate.check.return_value = (True, None)
        with patch('server.mixed_runtime_status', return_value={'ready': True, 'manifest': {'runtime_version': 'changed'}}):
            self.tick()
        self.assertEqual(self.app.state.processing.get(operation)['state'], 'failed')
        self.assertEqual(self.children, [])
        self.assertNotIn('acoustic_companion_version', self.app.state.processing.get(operation)['request'])


class CompanionGatewayAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.forwarded = []
        def authority(request):
            if request.url.path == '/session':
                return httpx.Response(200, json={'ready': True},
                    headers={'Set-Cookie': 'guitar_session=owned-loopback; Path=/; HttpOnly'})
            self.forwarded.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(b'authored response'),
                headers={'Content-Type': 'audio/wav', 'X-Content-SHA256': 'a' * 64})
        self.app = gateway.create_gateway(self.temp.name, httpx.MockTransport(authority))
        self.origin = self.app.state.origin
        self.client = self.enterContext(TestClient(self.app, base_url=self.origin,
            headers={'X-Forwarded-Proto': 'https', 'Origin': self.origin}))
        self.identity = str(uuid.uuid4())
        self.prefix = '/api/jobs/' + self.identity + '/parts'

    def pair(self):
        request = self.app.state.pairing.request('Authored phone')
        self.app.state.pairing.approve_local(request['id'])
        response = self.client.post('/pair/claim', json={'id': request['id'], 'secret': request['secret']})
        self.assertEqual(response.status_code, 200, response.text)

    def test_parts_routes_require_pairing_and_forward_only_fixed_names(self):
        for path in (self.prefix, self.prefix + '/performer-1.wav', '/stem-player.js', '/acoustic-parts.mjs'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertFalse(self.forwarded)
        self.pair()
        for path in (self.prefix, self.prefix + '/performer-1.wav', self.prefix + '/performer-2.wav', self.prefix + '/combined.wav'):
            response = self.client.get(path, headers={'Authorization': 'untrusted', 'Remote-User': 'untrusted'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['x-content-sha256'], 'a' * 64)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            forwarded = self.forwarded[-1]
            self.assertEqual(forwarded.url.path, path)
            self.assertEqual(forwarded.headers.get('cookie'), 'guitar_session=owned-loopback')
            self.assertNotIn('authorization', forwarded.headers)
            self.assertNotIn('remote-user', forwarded.headers)
        count = len(self.forwarded)
        for suffix in ('/manifest.json', '/verification.json', '/performer-3.wav', '/original.wav',
                       '/performer-1.wav/extra', '/%2e%2e%5coriginal.wav', '//combined.wav'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.get(self.prefix + suffix).status_code, 404)
        self.assertEqual(len(self.forwarded), count)

    def test_parts_static_modules_keep_pairing_and_exact_static_boundary(self):
        self.pair()
        for name in ('stem-player.js', 'acoustic-parts.mjs'):
            path = server.ROOT / 'web' / name
            self.assertTrue(path.is_file(), 'The paired player module must exist')
            self.assertEqual(self.client.get('/' + name + '?v=26').status_code, 200)
            self.assertEqual(self.client.get('/' + name + '/extra').status_code, 404)
        self.assertFalse(self.forwarded)


class ResponseOwnership(unittest.TestCase):
    def test_authority_response_closes_protected_owner_after_success_send_failure_or_disconnect(self):
        async def run(mode):
            stream = io.BytesIO(b'a' * 100000)
            owner = ExitStack()
            owner.callback(stream.close)
            response = server._ProtectedPartsResponse(stream, size=100000, sha256='a' * 64, owner=owner)
            chunks = []
            async def send(message):
                self.assertFalse(stream.closed)
                if message['type'] == 'http.response.body':
                    chunks.append(message.get('body', b''))
                    if mode == 'send failure':
                        raise OSError('Authored client disconnect')
            async def receive():
                if mode == 'receive disconnect':
                    return {'type': 'http.disconnect'}
                await asyncio.Event().wait()
            scope = {'type': 'http', 'asgi': {'spec_version': '2.3' if mode == 'receive disconnect' else '2.4'}}
            if mode == 'send failure':
                with self.assertRaises(ClientDisconnect):
                    await response(scope, receive, send)
            else:
                await response(scope, receive, send)
            self.assertTrue(stream.closed)
            if mode == 'success':
                self.assertEqual(b''.join(chunks), b'a' * 100000)
                self.assertTrue(all(len(chunk) <= 65536 for chunk in chunks))
        for mode in ('success', 'send failure', 'receive disconnect'):
            with self.subTest(mode=mode):
                asyncio.run(run(mode))

    def test_gateway_response_closes_authority_on_send_failure(self):
        class Upstream:
            status_code = 200
            closed = False
            async def aiter_raw(self):
                yield b'authored'
            async def aclose(self):
                self.closed = True
        async def run():
            upstream = Upstream()
            response = gateway._ForwardedResponse(upstream, headers={})
            async def send(message):
                self.assertFalse(upstream.closed)
                if message['type'] == 'http.response.body':
                    raise OSError('Authored paired client disconnect')
            async def receive():
                await asyncio.Event().wait()
            with self.assertRaises(ClientDisconnect):
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
            self.assertTrue(upstream.closed)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
