import tempfile
import unittest
import httpx
from fastapi.testclient import TestClient
from gateway import create_gateway,COOKIE

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.forwarded=[]
        def authority(request):
            if request.url.path=='/session':return httpx.Response(200,json={'ready':True},headers={'Set-Cookie':'guitar_session=private-loopback; HttpOnly; Path=/; SameSite=Strict'})
            self.forwarded.append(request)
            return httpx.Response(200,stream=httpx.ByteStream(b'{"items":[]}'),headers={'Content-Type':'application/json'})
        self.app=create_gateway(self.temp.name,httpx.MockTransport(authority))
        self.origin=self.app.state.origin
        self.client=TestClient(self.app,base_url=self.origin,headers={'X-Forwarded-Proto':'https','Origin':self.origin})
        self.client.__enter__()
    def tearDown(self):self.client.__exit__(None,None,None);self.temp.cleanup()
    def pair(self):
        pending=self.client.post('/pair/request',json={'label':'Test phone'}).json()
        self.assertEqual(self.client.post('/pair/claim',json={'id':pending['id'],'secret':pending['secret']}).json()['state'],'pending')
        self.app.state.pairing.approve_local(pending['id'])
        response=self.client.post('/pair/claim',json={'id':pending['id'],'secret':pending['secret']})
        self.assertEqual(response.json()['state'],'paired');self.assertNotIn('token',response.json())
        for flag in ('Secure','HttpOnly','SameSite=strict','Path=/'):self.assertIn(flag,response.headers['set-cookie'])
        return response.json()['device_id']
    def test_unpaired_boundary_and_spoofed_identity(self):
        self.assertEqual(self.client.get('/api/library',headers={'Remote-User':'owner'}).status_code,401)
        self.assertEqual(self.client.get('/api/media/anything').status_code,401)
        self.assertEqual(self.forwarded,[])
        self.assertEqual(self.client.get('/',headers={'X-Forwarded-Proto':'http'}).status_code,403)
        self.assertEqual(self.client.post('/pair/request',json={'label':'phone'},headers={'Origin':'https://other.invalid'}).status_code,403)
    def test_paired_proxy_strips_identity_and_revocation_blocks(self):
        device=self.pair()
        response=self.client.get('/api/library',headers={'Remote-User':'caller','X-Authenticated-User':'caller','Authorization':'caller'})
        self.assertEqual(response.status_code,200,response.text)
        request=self.forwarded[-1]
        self.assertEqual(request.headers.get('cookie'),'guitar_session=private-loopback')
        for name in ('remote-user','x-authenticated-user','authorization'):self.assertNotIn(name,request.headers)
        self.assertEqual(self.client.post('/api/pairing/approve',json={}).status_code,404)
        self.app.state.pairing.revoke_local(device)
        self.assertEqual(self.client.get('/api/library').status_code,401)
    def test_instrumental_routes_and_module_require_pairing(self):
        for route in ('instrumental-candidates','arrange-instrumental'):
            self.assertEqual(self.client.post('/api/'+route,json={}).status_code,401)
        self.pair()
        for route in ('instrumental-candidates','arrange-instrumental'):
            self.assertEqual(self.client.post('/api/'+route,json={}).status_code,200)
            self.assertEqual(self.forwarded[-1].url.path,'/api/'+route)
            self.assertEqual(self.client.post('/api/'+route+'/extra',json={}).status_code,404)
        self.assertEqual(self.client.get('/instrumental.mjs?v=22').status_code,200)

    def test_processing_queue_routes_keep_pairing_body_and_path_boundaries(self):
        identity='12345678-1234-4234-9234-123456789abc'
        for route in ('/api/processing-queue','/api/processing-queue/'+identity):
            self.assertEqual(self.client.get(route).status_code,401)
        self.assertEqual(self.client.post('/api/processing-queue',json={}).status_code,401)
        self.pair()
        for route in ('/api/processing-queue','/api/processing-queue/'+identity):
            self.assertEqual(self.client.get(route).status_code,200)
            self.assertEqual(self.forwarded[-1].url.path,route)
        self.assertEqual(self.client.get('/api/processing-queue?offset=100').status_code,200)
        self.assertEqual(self.forwarded[-1].url.params.get('offset'),'100')
        for route in ('/api/processing-queue','/api/processing-queue/'+identity+'/cancel'):
            self.assertEqual(self.client.post(route,json={}).status_code,200)
            self.assertEqual(self.forwarded[-1].url.path,route)
        self.assertEqual(self.client.post('/api/processing-queue',content=b'x'*4097).status_code,413)
        self.assertEqual(self.client.post('/api/processing-queue/'+identity+'/extra',json={}).status_code,404)
        self.assertEqual(self.client.get('/processing-outbox.mjs?v=23').status_code,200)

    def test_v24_electric_and_diagnostics_shell_keeps_static_pairing_boundary(self):
        paths=('/app.js?v=24','/mixed-preview.mjs?v=24','/instrumental.mjs?v=24',
               '/processing-outbox.mjs?v=24','/sound-diagnostics.mjs?v=24')
        for path in paths:self.assertEqual(self.client.get(path).status_code,401)
        device=self.pair()
        for path in paths:self.assertEqual(self.client.get(path).status_code,200,path)
        self.assertEqual(self.forwarded,[],'Static shell files must not become authority requests')
        self.assertEqual(self.client.get('/render-evidence/private.dry.f32').status_code,404)
        self.assertEqual(self.client.get('/sound-diagnostics.mjs/extra').status_code,404)
        self.app.state.pairing.revoke_local(device)
        self.assertEqual(self.client.get('/sound-diagnostics.mjs?v=24').status_code,401)

    def test_instrumental_selection_body_cap_supports_long_ids_without_unbounding_other_routes(self):
        self.pair();large=b'x'*(256*1024)
        self.assertEqual(self.client.post('/api/arrange-instrumental',content=large).status_code,200)
        self.assertEqual(len(self.forwarded[-1].content),len(large))
        self.assertEqual(self.client.post('/api/arrange-instrumental',content=b'x'*(1024**2+1)).status_code,413)
        self.assertEqual(self.client.post('/api/instrumental-candidates',content=b'x'*4097).status_code,413)

    def test_lost_claim_response_can_retry_same_device(self):
        pending=self.app.state.pairing.request('Retry phone');self.app.state.pairing.approve_local(pending['id'])
        self.app.state.pairing.claim(pending['id'],pending['secret'])
        retry=self.client.post('/pair/claim',json={'id':pending['id'],'secret':pending['secret']})
        self.assertEqual(retry.status_code,200)
        self.assertEqual(len(self.app.state.pairing.devices()),1)

    def test_shell_headers_and_explicit_reconnect_do_not_grant_access(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['x-guitar-page'],'pairing')
        self.assertEqual(self.client.get('/reconnect').headers['x-guitar-page'],'pairing')
        self.assertEqual(self.client.get('/api/library').status_code,401)
        device=self.pair()
        self.assertEqual(self.client.get('/').headers['x-guitar-page'],'player')
        self.assertEqual(self.client.get('/index.html').headers['x-guitar-page'],'player')
        self.assertEqual(self.client.get('/transport.mjs?v=18').status_code,200)
        self.app.state.pairing.revoke_local(device)
        self.assertEqual(self.client.get('/transport.mjs?v=18').status_code,401)
        self.assertEqual(self.client.get('/').headers['x-guitar-page'],'pairing')
        reconnect=self.client.get('/reconnect')
        self.assertEqual(reconnect.status_code,200)
        self.assertEqual(reconnect.headers['cache-control'],'no-store')
        self.assertNotIn('set-cookie',reconnect.headers)
        self.assertEqual(self.client.get('/session').status_code,401)
        self.assertEqual(self.client.get('/api/media/anything').status_code,401)

if __name__=='__main__':unittest.main()
