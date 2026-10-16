"""Paired browser gateway, loopback-only until a reviewed TLS proxy is started.

No owner approval route is forwarded. No client identity header is trusted.
"""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
import anyio
import httpx
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,StreamingResponse
from identity import library_identity
from library import Library,MAX_IMPORT
from pairing import Pairing

ROOT=Path(__file__).parent
COOKIE='__Host-guitar_device'
FORWARD_GET=re.compile(r'^/api/(?:status|library|previews/latest|media/[a-zA-Z0-9-]+|jobs/[a-zA-Z0-9-]+(?:/parts(?:/(?:performer-[12]|combined)\.wav)?)?|processing-queue(?:/[a-zA-Z0-9-]+)?)$')
FORWARD_POST=re.compile(r'^/api/(?:import|diagnostic|extract|arrange-preview|refine-preview|instrumental-candidates|arrange-instrumental|jobs/[a-zA-Z0-9-]+/cancel|processing-queue(?:/[a-zA-Z0-9-]+/cancel)?)$')
PUBLIC_FILES={'/pair.js':'pair.js','/pair.css':'pair.css','/icon.svg':'icon.svg'}


class _ForwardedResponse(StreamingResponse):
    """Release the authority stream even when a paired client disconnects."""
    def __init__(self, upstream, *, headers):
        self._upstream = upstream
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._upstream.aclose()

async def bounded_body(request,limit):
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>limit:raise HTTPException(413,'Request too large')
        data.extend(chunk)
    return bytes(data)

def create_gateway(root,transport=None):
    library=Library(root);pairing=Pairing(library);identity=library_identity(root)
    host=identity['hostname']+':8443';origin='https://'+host
    client=httpx.AsyncClient(base_url='http://127.0.0.1:8765',trust_env=False,follow_redirects=False,
                             timeout=httpx.Timeout(30,connect=5),transport=transport)
    @asynccontextmanager
    async def lifespan(app):
        with library.exclusive('gateway.lock'):
            try:yield
            finally:await client.aclose()
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.pairing=pairing;app.state.origin=origin

    @app.middleware('http')
    async def boundary(request,call_next):
        # Bound only to loopback. The TLS proxy must preserve Host and set proto.
        if request.headers.get('host')!=host or request.headers.get('x-forwarded-proto')!='https':
            return JSONResponse({'detail':'Use the saved secure library address.'},status_code=403)
        caller_origin=request.headers.get('origin')
        if caller_origin and caller_origin!=origin:return JSONResponse({'detail':'Different origin denied'},status_code=403)
        if request.method not in ('GET','HEAD') and caller_origin!=origin:
            return JSONResponse({'detail':'A same-origin request is required.'},status_code=403)
        if request.headers.get('sec-fetch-site')=='cross-site':return JSONResponse({'detail':'Different site denied'},status_code=403)
        device=pairing.authenticate(request.cookies.get(COOKIE))
        request.state.device=device
        public=request.url.path in ('/','/reconnect','/pair/request','/pair/claim') or request.url.path in PUBLIC_FILES
        if not public and not device:return JSONResponse({'detail':'Pair this device with the laptop first.'},status_code=401)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        if request.url.path.startswith(('/pair/','/api/')) or request.url.path=='/session':response.headers['Cache-Control']='no-store'
        return response

    @app.get('/')
    def index(request:Request):
        page='player' if request.state.device else 'pairing'
        return FileResponse(ROOT/'web'/('index.html' if request.state.device else 'pair.html'),headers={'Cache-Control':'no-store','X-Guitar-Page':page})

    @app.get('/reconnect')
    def reconnect():
        # Explicit public setup page; it grants no access to protected resources.
        return FileResponse(ROOT/'web'/'pair.html',headers={'Cache-Control':'no-store','X-Guitar-Page':'pairing'})

    @app.post('/pair/request')
    async def request_pair(request:Request):
        try:
            value=json.loads(await bounded_body(request,1024))
            return pairing.request(value.get('label',''))
        except (ValueError,AttributeError):raise HTTPException(400,'Use a short device name, then wait a few seconds before retrying.')

    @app.post('/pair/claim')
    async def claim(request:Request):
        try:
            value=json.loads(await bounded_body(request,2048));result=pairing.claim(value['id'],value['secret'])
        except (ValueError,KeyError,TypeError,PermissionError):raise HTTPException(403,'Pairing expired, was denied, or is invalid. Start again.')
        token=result.pop('token',None)
        response=JSONResponse(result)
        if token:response.set_cookie(COOKIE,token,secure=True,httponly=True,samesite='strict',path='/',max_age=365*24*3600)
        return response

    @app.get('/session')
    def session():return {'ready':True}

    @app.api_route('/api/{path:path}',methods=['GET','HEAD','POST'])
    async def proxy(request:Request,path:str):
        full='/api/'+path
        allowed=(FORWARD_GET.fullmatch(full) if request.method in ('GET','HEAD') else FORWARD_POST.fullmatch(full))
        if not allowed:raise HTTPException(404,'This action is available only on the library laptop.')
        headers={}
        for name in ('range','content-type','x-filename'):
            if name in request.headers:headers[name]=request.headers[name]
        # Only the gateway's loopback session cookie reaches the local authority.
        # Caller Cookie/Authorization/Remote-User/forwarding headers never do.
        try:
            boot=await client.get('/session')
            if boot.status_code!=200:raise HTTPException(503,'The library laptop service is unavailable.')
            if request.method=='POST' and path=='import':
                async def chunks():
                    size=0
                    async for chunk in request.stream():
                        size+=len(chunk)
                        if size>MAX_IMPORT:raise HTTPException(413,'Choose a file smaller than 250 MB.')
                        yield chunk
                content=chunks()
            elif request.method=='POST':content=await bounded_body(request,1024**2 if path=='arrange-instrumental' else 65536 if path=='refine-preview' else 4096)
            else:content=None
            query=request.query_params if path=='processing-queue' and request.method in ('GET','HEAD') else None
            if query is not None and len(request.url.query)>128:raise HTTPException(400,'History query too large.')
            outgoing=client.build_request(request.method,full,headers=headers,content=content,params=query)
            upstream=await client.send(outgoing,stream=True)
        except httpx.HTTPError:raise HTTPException(503,'The library laptop service is unavailable. Downloaded music still works.')
        kept={key:value for key,value in upstream.headers.items() if key.lower() in ('content-type','content-length','content-range','accept-ranges','etag','x-content-sha256')}
        return _ForwardedResponse(upstream,headers=kept)

    @app.get('/{path:path}')
    def static(path:str,request:Request):
        permitted={'index.html','app.js','style.css','sw.js','manifest.webmanifest','icon.svg','backup.mjs','capture.mjs','capture-ui.mjs','capture-journal.mjs','devices.mjs','mixed-preview.mjs','refinement.mjs','instrumental.mjs','transport.mjs','processing-outbox.mjs','sound-diagnostics.mjs','electric-tone.mjs','stem-player.js','acoustic-parts.mjs'}
        full='/'+path
        if full in PUBLIC_FILES:return FileResponse(ROOT/'web'/PUBLIC_FILES[full])
        if not request.state.device or path not in permitted:raise HTTPException(404,'Page not found')
        return FileResponse(ROOT/'web'/path,headers={'X-Guitar-Page':'player'} if path=='index.html' else None)
    return app

if __name__=='__main__':
    import uvicorn
    uvicorn.run(create_gateway(ROOT/'data'),host='127.0.0.1',port=8766,proxy_headers=False)
