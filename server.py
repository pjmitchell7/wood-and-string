"""Loopback development authority. LAN pairing is not enabled by this server."""
import asyncio
from functools import wraps
from contextlib import asynccontextmanager
import json
import hashlib
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import unquote
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from library import Library, MAX_IMPORT
from pairing import Pairing
from audio.preferences import normalize_guidance, guidance_is_default
from audio.render import RENDER_VERSION
from audio.preview_instruments import selection_fields, preview_guidance, BASS_RENDER_VERSION
from audio.mixed_analysis import PIPELINE_VERSION as MIXED_PIPELINE_VERSION, INPUT_MIMES

ROOT = Path(__file__).parent
MIMES = {'.mp3':'audio/mpeg', '.wav':'audio/wav', '.m4a':'audio/mp4', '.aac':'audio/aac', '.flac':'audio/flac', '.webm':'audio/webm'}

def mixed_runtime_status():
    try:
        from audio.analysis_runtime import runtime_status
        return runtime_status()
    except (ImportError, RuntimeError, OSError, ValueError):
        return {'ready':False,'reason':'The local mixed-preview runtime is not ready.'}

def bass_runtime_status():
    from audio.growlybass import bank_status
    return bank_status()

def instrumental_runtime_status():
    from audio.instrumental_runtime import runtime_status
    return runtime_status()

async def small_body(request, maximum):
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>maximum: raise HTTPException(413,'Request too large')
        body.extend(chunk)
    return bytes(body)

def create_app(root=None):
    library = Library(root or ROOT/'data')
    pairing = Pairing(library)
    session = secrets.token_urlsafe(32)
    processes = {}
    import_lock = asyncio.Lock()
    preview_lock = asyncio.Lock()

    def serialized_preview(handler):
        @wraps(handler)
        async def admission(*args, **kwargs):
            # Keep the admission slot through reaping and the next spawn.
            async with preview_lock:
                return await handler(*args, **kwargs)
        return admission

    async def reap_terminal_workers():
        for identity, process in list(processes.items()):
            if process.poll() is None and library.job(identity)['state'] not in ('queued','running'):
                # Terminal result is durable, but the worker still owns its
                # lock while finishing cleanup. Reap before admitting a successor.
                if process.stdin and not process.stdin.closed: process.stdin.close()
                try: await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    raise HTTPException(409,'The previous preview is finishing cleanup. Try again shortly.')
            if process.poll() is not None:
                if process.stdin and not process.stdin.closed: process.stdin.close()
                from analysis_evidence import cleanup_work,reconcile_evidence
                try:
                    with library.exclusive('worker.lock'):
                        cleanup_work(library,identity)
                        reconcile_evidence(library)
                except OSError:raise HTTPException(409,'The previous analysis is finishing cleanup. Try again shortly.')
                processes.pop(identity, None)
    @asynccontextmanager
    async def lifespan(app):
        with library.exclusive('authority.lock'):
            with library.exclusive('worker.lock'):
                with library.connect() as db:
                    db.execute("UPDATE jobs SET state='interrupted',error='The app stopped. Start a new preview.' WHERE state IN ('queued','running')")
                library.reconcile_staging()
                from analysis_evidence import reconcile_work, reconcile_evidence
                reconcile_work(library)
                reconcile_evidence(library)
            try: yield
            finally:
                for p in processes.values():
                    if p.stdin: p.stdin.close()
                    if p.poll() is None:
                        try: p.wait(timeout=5)
                        except subprocess.TimeoutExpired: p.terminate();p.wait(timeout=5)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.library = library

    def checked_job(identity):
        result=library.job(identity)
        if result['state']=='complete':
            try:
                if result['request'].get('kind')=='instrumental':
                    from instrumental_preview import checked_candidates
                    checked_candidates(library,result);valid=True
                else:valid=library.verify(result['result']['asset']['id'])
            except (KeyError,TypeError,ValueError,OSError): valid=False
            if not valid:
                retained=result['result'] if result['request'].get('kind')=='instrumental' else None
                library.update_job(identity,'interrupted',result=retained,error='Saved preview failed verification. Run the preview again to recover it.')
                result=library.job(identity)
        return result

    @app.middleware('http')
    async def local_boundary(request, call_next):
        # Exact hosts defeat rebinding; cross-origin and cross-site requests do not bootstrap sessions.
        host = request.headers.get('host', '')
        if host != '127.0.0.1:8765': return JSONResponse({'detail':'Use http://127.0.0.1:8765'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != 'http://127.0.0.1:8765':
            return JSONResponse({'detail':'Different origin denied'}, status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail':'Different site denied'}, status_code=403)
        if request.url.path.startswith('/api/') and not secrets.compare_digest(request.cookies.get('guitar_session',''), session):
            return JSONResponse({'detail':'Open the player on this laptop first.'}, status_code=401)
        response = await call_next(request)
        if request.url.path in ('/', '/session'):
            response.set_cookie('guitar_session',session,httponly=True,samesite='strict',path='/')
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        return response

    @app.get('/api/status')
    def status():
        runtime=mixed_runtime_status()
        bass=bass_runtime_status()
        instrumental=instrumental_runtime_status()
        return dict(ready=True, mode='this laptop only', conversion='experimental isolated melody and mixed melody/bass previews',
                    automatic_extraction=False, isolated_melody_ready=True,
                    mixed_ready=bool(runtime.get('ready')), mixed_reason=runtime.get('reason'),
                    bass_ready=bool(bass.get('ready')), bass_reason=bass.get('reason'),
                    instrumental_ready=bool(instrumental.get('ready')), instrumental_reason=instrumental.get('reason'))

    async def admit_instrumental(spec,space):
        # Never replace a saved candidate parent after failed verification.
        # A deterministic recovery chain preserves it and makes retries stable.
        for _ in range(32):
            identity=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps(spec,sort_keys=True)))
            try:
                prior=checked_job(identity)
                if prior['state']=='complete' or (identity in processes and processes[identity].poll() is None):return prior
                if prior['request'].get('kind')=='instrumental' and prior.get('result') is not None:
                    from instrumental_preview import canonical
                    spec={**spec,'recovery_of':identity,
                          'recovery_result_sha256':hashlib.sha256(canonical(prior['result'])).hexdigest()}
                    continue
            except KeyError:pass
            break
        else:raise HTTPException(409,'Too many saved recovery attempts. Keep this history and choose a different passage.')
        await reap_terminal_workers()
        if processes:raise HTTPException(409,'One preview is already running. Wait or cancel it first.')
        try:library.space(space)
        except ValueError as error:raise HTTPException(507,str(error))
        with library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (identity,'queued',0,json.dumps(spec),None,None,time.time()))
        try:
            processes[identity]=subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(library.root),identity],cwd=ROOT,
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except OSError:library.update_job(identity,'failed',error='The local analysis worker could not start. Your original is unchanged.')
        return library.job(identity)

    @app.post('/api/instrumental-candidates')
    @serialized_preview
    async def instrumental_candidates(request:Request):
        from instrumental_preview import VERSION,PIPELINE_VERSION
        try:
            value=json.loads(await small_body(request,4096))
            if type(value) is not dict or set(value)!={'asset_id','start_seconds','end_seconds'}:raise ValueError('Choose an original recording and passage.')
            source=library.get(value['asset_id']);start=value['start_seconds'];end=value['end_seconds']
            if source['kind']!='original' or source['mime'] not in INPUT_MIMES:raise ValueError('Choose a supported imported original.')
            if (any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end))
                or not 0<=start<end or end-start>10 or end-start<.1):raise ValueError('Choose a passage of0.1 to10seconds.')
        except (ValueError,KeyError,TypeError,AttributeError) as error:raise HTTPException(400,str(error))
        runtime=instrumental_runtime_status()
        if not runtime.get('ready'):raise HTTPException(503,runtime.get('reason') or 'Instrumental inspection is not ready on this laptop.')
        version=runtime.get('runtime_version')
        if type(version) is not str or not version:raise HTTPException(503,'Instrumental runtime needs verification.')
        spec=dict(kind='instrumental',asset_id=source['id'],sha256=source['sha256'],
                  start_seconds=float(start),end_seconds=float(end),review_version=VERSION,
                  runtime_version=version,pipeline_version=PIPELINE_VERSION)
        return await admit_instrumental(spec,64*1024**2)

    @app.post('/api/arrange-instrumental')
    @serialized_preview
    async def arrange_instrumental(request:Request):
        from instrumental_preview import prepare_arrangement
        try:
            prepared=prepare_arrangement(library,json.loads(await small_body(request,1024**2)))
        except (ValueError,KeyError,TypeError,AttributeError) as error:raise HTTPException(400,str(error))
        spec=prepared['spec']
        if spec.get('instrument')=='jazz-bass' and not bass_runtime_status().get('ready'):
            raise HTTPException(503,'The experimental Jazz Bass bank is not ready on this laptop.')
        return await admit_instrumental(spec,128*1024**2)

    @app.get('/session')
    def bootstrap(): return JSONResponse({'ready':True},headers={'Cache-Control':'no-store'})

    @app.get('/api/library')
    def listing(): return {'items':library.items()}

    @app.get('/api/pairing')
    def paired_devices(): return {'requests':pairing.pending(),'devices':pairing.devices()}

    @app.post('/api/pairing/approve')
    async def approve_pair(request:Request):
        try:
            value=json.loads(await small_body(request,1024))
            if type(value.get('approve')) is not bool:raise ValueError()
            pairing.approve_local(value['id'],value['approve'])
        except (ValueError,KeyError,AttributeError):raise HTTPException(400,'The request expired or was already handled.')
        return {'handled':True}

    @app.post('/api/pairing/revoke')
    async def revoke_device(request:Request):
        try:
            value=json.loads(await small_body(request,1024));pairing.revoke_local(value['id'])
        except (ValueError,KeyError,AttributeError):raise HTTPException(400,'Device not found.')
        return {'revoked':True}

    @app.post('/api/import')
    async def upload(request: Request):
        name = unquote(request.headers.get('x-filename','recording.wav')).replace('\\','/').split('/')[-1][:240]
        suffix = Path(name).suffix.lower()
        if suffix not in MIMES: raise HTTPException(415, 'Choose MP3, WAV, M4A, AAC, FLAC or WebM audio.')
        async with import_lock:
            staged = library.stage / (str(uuid.uuid4())+'.partial')
            try:
                library.space(MAX_IMPORT)
                count = 0
                with staged.open('xb') as f:
                    async for chunk in request.stream():
                        count += len(chunk)
                        if count > MAX_IMPORT: raise HTTPException(413,'Choose a file smaller than 250 MB for this preview.')
                        library.space(len(chunk))
                        f.write(chunk)
                    f.flush(); os.fsync(f.fileno())
                return library.publish(staged,name,MIMES[suffix])
            except ValueError as e: raise HTTPException(400,str(e))
            finally:
                if staged.exists(): staged.unlink()

    @app.get('/api/media/{identity}')
    def media(identity: str):
        try: item = library.get(identity)
        except KeyError: raise HTTPException(404,'Track not found')
        return FileResponse(library.media/item['sha256'], media_type=item['mime'], headers={'X-Content-SHA256':item['sha256']})

    @app.post('/api/diagnostic')
    @serialized_preview
    async def diagnostic(request: Request):
        raw = await small_body(request,1024)
        try:
            value = json.loads(raw)
            performers = value.get('performers',1)
            if type(performers) is not int or performers not in (1,2): raise ValueError()
        except (ValueError, AttributeError): raise HTTPException(400,'Choose one or two guitarists.')
        identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'wood-string:diagnostic-v4:'+RENDER_VERSION+':'+str(performers)))
        try:
            prior=checked_job(identity)
            if prior['state']=='complete' or (identity in processes and processes[identity].poll() is None): return prior
        except KeyError: pass
        await reap_terminal_workers()
        if processes: raise HTTPException(409,'One preview is already running. Wait or cancel it first.')
        try: library.space(64*1024**2)
        except ValueError as e: raise HTTPException(507,str(e))
        with library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',(identity,'queued',0,json.dumps({'performers':performers,'kind':'diagnostic','renderer_version':RENDER_VERSION}),None,None,time.time()))
        processes[identity]=subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(library.root),identity],cwd=ROOT,
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        return library.job(identity)

    @app.get('/api/jobs/{identity}')
    def job(identity: str):
        try:
            result=checked_job(identity)
            p=processes.get(identity)
            if p and p.poll() is not None and result['state'] in ('queued','running'):
                library.update_job(identity,'failed',error='The preview worker stopped unexpectedly.')
                result=library.job(identity)
            return result
        except KeyError: raise HTTPException(404,'Preview not found')

    @app.get('/api/previews/latest')
    def latest_preview():
        with library.connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE state='complete' AND json_extract(request,'$.kind') IN ('mixed','refine') ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(404, 'No saved mixed preview is available yet.')
        result = checked_job(row['id'])
        if result['state'] != 'complete':
            raise HTTPException(404, 'The latest saved preview needs to be generated again.')
        return result

    @app.post('/api/extract')
    @serialized_preview
    async def extract(request: Request):
        raw=await small_body(request,4096)
        try:
            value=json.loads(raw)
            if value.get('isolated_melody') is not True: raise ValueError('Confirm this is an isolated single melody.')
            asset=library.get(value['asset_id'])
            if asset['mime']!='audio/wav': raise ValueError('This first extraction supports PCM16 mono WAV only.')
            start=float(value.get('start_seconds',0));end=float(value['end_seconds'])
            performers=value.get('performers',1)
            if not math.isfinite(start) or not math.isfinite(end) or not 0<=start<end or end-start>30:
                raise ValueError('Choose a passage from 0 to 30 seconds long.')
            if type(performers) is not int or performers != 1: raise ValueError('Melody extraction currently supports one guitarist; accompaniment extraction is pending.')
        except (ValueError,KeyError,TypeError,AttributeError) as e:
            raise HTTPException(400,str(e))
        spec={'kind':'extract','asset_id':asset['id'],'sha256':asset['sha256'],'performers':performers,'start_seconds':start,'end_seconds':end,'pipeline_version':2,'renderer_version':RENDER_VERSION}
        identity=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps(spec,sort_keys=True)))
        try:
            prior=checked_job(identity)
            if prior['state']=='complete' or (identity in processes and processes[identity].poll() is None): return prior
        except KeyError: pass
        await reap_terminal_workers()
        if processes: raise HTTPException(409,'One preview is already running. Wait or cancel it first.')
        try: library.space(128*1024**2)
        except ValueError as e: raise HTTPException(507,str(e))
        with library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',(identity,'queued',0,json.dumps(spec),None,None,time.time()))
        processes[identity]=subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(library.root),identity],cwd=ROOT,
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        return library.job(identity)

    @app.post('/api/jobs/{identity}/cancel')
    def cancel(identity: str):
        try: result=library.job(identity)
        except KeyError: raise HTTPException(404,'Preview not found')
        if result['state'] in ('queued','running'):
            (library.stage/(identity+'.cancel')).touch()
        return {'requested':True}

    @app.post('/api/arrange-preview')
    @serialized_preview
    async def arrange_preview(request: Request):
        raw=await small_body(request,4096)
        try:
            value=json.loads(raw)
            asset=library.get(value['asset_id'])
            if asset['kind']!='original': raise ValueError('Choose an imported original recording.')
            if asset['mime'] not in INPUT_MIMES:
                raise HTTPException(415,'Choose a supported audio recording: MP3, WAV, M4A/AAC, FLAC or WebM.')
            start=value.get('start_seconds',0);end=value['end_seconds'];performers=value.get('performers',1)
            if type(start) not in (int,float) or type(end) not in (int,float):
                raise ValueError('Choose numeric passage times.')
            start=float(start);end=float(end)
            if not math.isfinite(start) or not math.isfinite(end) or not 0<=start<end or end-start>30:
                raise ValueError('Choose a passage longer than zero and no longer than 30 seconds.')
            if type(performers) is not int or performers not in (1,2): raise ValueError('Choose one or two guitarists.')
            selection=selection_fields(value,performers)
            guidance=preview_guidance(value.get('guidance'),performers,selection)
        except (ValueError,KeyError,TypeError,AttributeError):
            raise HTTPException(400,'Choose an imported original, a valid passage up to 30 seconds, and one or two guitarists.')
        runtime=mixed_runtime_status()
        if not runtime.get('ready'): raise HTTPException(503,runtime.get('reason') or 'The local mixed-preview runtime is not ready.')
        version=runtime.get('manifest',{}).get('runtime_version')
        if not isinstance(version,str) or not version: raise HTTPException(503,'The local mixed-preview runtime needs verification.')
        if selection and not bass_runtime_status().get('ready'):
            raise HTTPException(503,'The experimental Jazz Bass bank is not ready on this laptop.')
        spec={'kind':'mixed','asset_id':asset['id'],'sha256':asset['sha256'],
              'performers':performers,'start_seconds':start,'end_seconds':end,
              'pipeline_version':MIXED_PIPELINE_VERSION,'runtime_version':version,
              'renderer_version':BASS_RENDER_VERSION if selection else RENDER_VERSION,**selection}
        if not guidance_is_default(guidance): spec['guidance']=guidance
        identity=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps(spec,sort_keys=True)))
        try:
            prior=checked_job(identity)
            if prior['state']=='complete' or (identity in processes and processes[identity].poll() is None): return prior
        except KeyError: pass
        await reap_terminal_workers()
        if processes: raise HTTPException(409,'One preview is already running. Wait or cancel it first.')
        try: library.space(256*1024**2)
        except ValueError as e: raise HTTPException(507,str(e))
        with library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (identity,'queued',0,json.dumps(spec),None,None,time.time()))
        try:
            processes[identity]=subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(library.root),identity],cwd=ROOT,
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        except OSError:
            library.update_job(identity,'failed',error='The local preview worker could not start. Your original is unchanged.')
        return library.job(identity)

    @app.post('/api/refine-preview')
    @serialized_preview
    async def refine_preview(request: Request):
        from preview_refinement import prepare_refinement
        raw = await small_body(request, 65536)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'parent_job_id', 'operations'}:
                raise ValueError('Choose a saved preview and at least one edit.')
            prepared = prepare_refinement(library, value['parent_job_id'], value['operations'])
            spec = prepared['spec']
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise HTTPException(400, str(error))
        if spec.get('instrument') == 'jazz-bass' and not bass_runtime_status().get('ready'):
            raise HTTPException(503,'The experimental Jazz Bass bank is not ready on this laptop.')
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(spec, sort_keys=True)))
        try:
            prior = checked_job(identity)
            if prior['state'] == 'complete' or (identity in processes and processes[identity].poll() is None):
                return prior
        except KeyError:
            pass
        await reap_terminal_workers()
        if processes:
            raise HTTPException(409, 'One preview is already running. Wait or cancel it first.')
        try:
            library.space(128 * 1024 ** 2)
        except ValueError as error:
            raise HTTPException(507, str(error))
        with library.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (identity, 'queued', 0, json.dumps(spec), None, None, time.time()))
        try:
            processes[identity] = subprocess.Popen(
                [sys.executable, str(ROOT / 'worker.py'), str(library.root), identity], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except OSError:
            library.update_job(identity, 'failed', error='The local preview worker could not start. Your original is unchanged.')
        return library.job(identity)

    app.mount('/',StaticFiles(directory=ROOT/'web',html=True),name='player')
    return app

app=create_app()
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8765,proxy_headers=False)
