"""Loopback development authority. LAN pairing is not enabled by this server."""
import asyncio
from functools import wraps
from contextlib import asynccontextmanager, ExitStack
import json
import hashlib
import math
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
from urllib.parse import unquote
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from library import Library, MAX_IMPORT
from pairing import Pairing
from processing_queue import ProcessingQueue, QueueConflict, QueueFull
from processing_resources import Admission
from audio.preferences import normalize_guidance, guidance_is_default
from audio.render import RENDER_VERSION
from audio.preview_instruments import selection_fields, preview_guidance, renderer_version
from audio.mixed_analysis import PIPELINE_VERSION as MIXED_PIPELINE_VERSION, INPUT_MIMES

ROOT = Path(__file__).parent
MIMES = {'.mp3':'audio/mpeg', '.wav':'audio/wav', '.m4a':'audio/mp4', '.aac':'audio/aac', '.flac':'audio/flac', '.webm':'audio/webm'}
PART_FILES = frozenset(('performer-1.wav', 'performer-2.wav', 'combined.wav'))


class _ProtectedPartsResponse(StreamingResponse):
    """Keep a verified companion's readers through ASGI completion/disconnect."""
    def __init__(self, stream, *, size, sha256, owner):
        self._parts_owner = owner
        def chunks():
            remaining = size
            stream.seek(0)
            while remaining:
                data = stream.read(min(65536, remaining))
                if not data:
                    raise OSError('Verified guitar part ended unexpectedly')
                remaining -= len(data)
                yield data
        super().__init__(chunks(), media_type='audio/wav', headers={
            'Content-Length': str(size), 'X-Content-SHA256': sha256,
            'Cache-Control': 'no-store', 'Accept-Ranges': 'none'})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._parts_owner.close()

def mixed_runtime_status():
    try:
        from audio.analysis_runtime import runtime_status
        return runtime_status()
    except (ImportError, RuntimeError, OSError, ValueError):
        return {'ready':False,'reason':'The local mixed-preview runtime is not ready.'}

def bass_runtime_status():
    from audio.growlybass import bank_status
    return bank_status()

def electric_runtime_status():
    from audio.electric_preview_runtime import bank_status
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
    processing = ProcessingQueue(library)
    admission = Admission()
    render_cleanup_attention = None

    def reconcile_private_renders():
        # Maintenance already runs under worker.lock. A damaged reference must
        # preserve evidence without preventing unrelated playback or startup.
        nonlocal render_cleanup_attention
        from render_evidence import reconcile_render_evidence
        try:
            reconcile_render_evidence(library)
            render_cleanup_attention = None
        except (ValueError,OSError,KeyError,TypeError,sqlite3.Error):
            render_cleanup_attention = 'Some saved Electric evidence needs review and has been preserved.'

    def require_instrument(spec):
        if spec.get('instrument')=='jazz-bass' and not bass_runtime_status().get('ready'):
            raise HTTPException(503,'The experimental Jazz Bass bank is not ready on this laptop.')
        if spec.get('instrument')=='electric' and not electric_runtime_status().get('ready'):
            raise HTTPException(503,'The experimental Electric bank is not ready on this laptop.')

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
                        library.cleanup_render_staging(identity)
                        from tone_evidence import cleanup_work as cleanup_tone
                        cleanup_tone(library,identity)
                        reconcile_private_renders()
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
                from tone_evidence import reconcile_work as reconcile_tone_work
                reconcile_tone_work(library)
                reconcile_evidence(library)
                reconcile_private_renders()
                processing.recover()
            scheduler = asyncio.create_task(processing_loop())
            try: yield
            finally:
                scheduler.cancel()
                try: await scheduler
                except asyncio.CancelledError: pass
                for p in processes.values():
                    if p.stdin: p.stdin.close()
                    if p.poll() is None:
                        try: p.wait(timeout=5)
                        except subprocess.TimeoutExpired: p.terminate();p.wait(timeout=5)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.library = library
    app.state.processing = processing

    def checked_job(identity):
        result=library.job(identity)
        if result['state']=='complete':
            try:
                if result['request'].get('kind')=='instrumental':
                    from instrumental_preview import checked_candidates
                    checked_candidates(library,result);valid=True
                else:
                    saved_asset=result['result']['asset']
                    asset=library.get(saved_asset['id'])
                    valid=(asset['sha256']==saved_asset.get('sha256',asset['sha256'])
                           and library.verify(asset['id']))
                    if valid and result['request'].get('instrument')=='electric':
                        from tone_evidence import verify_preview
                        from audio.preview_instruments import validate_saved_selection
                        if asset['sha256']!=result['result']['asset']['sha256']:
                            raise ValueError('Saved Electric WAV reference changed')
                        verify_preview(library,result['result'],validate_saved_selection(result['request']))
            except (KeyError,TypeError,ValueError,OSError): valid=False
            if not valid:
                retained=result['result'] if (result['request'].get('kind')=='instrumental'
                         or result['request'].get('instrument')=='electric') else None
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
        from acoustic_companion import VERSION as ACOUSTIC_COMPANION_VERSION
        runtime=mixed_runtime_status()
        bass=bass_runtime_status()
        electric=electric_runtime_status()
        instrumental=instrumental_runtime_status()
        return dict(ready=True, mode='this laptop only', conversion='experimental isolated melody and mixed melody/bass previews',
                    automatic_extraction=False, isolated_melody_ready=True,
                    mixed_ready=bool(runtime.get('ready')), mixed_reason=runtime.get('reason'),
                    bass_ready=bool(bass.get('ready')), bass_reason=bass.get('reason'),
                    electric_ready=bool(electric.get('ready')), electric_reason=electric.get('reason'),
                    render_evidence_attention=render_cleanup_attention,
                    instrumental_ready=bool(instrumental.get('ready')), instrumental_reason=instrumental.get('reason'),
                    acoustic_companion_version=ACOUSTIC_COMPANION_VERSION, acoustic_companion_supported=True)

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
        require_instrument(spec)
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

    def parts_job(identity):
        try:
            if str(uuid.UUID(identity)) != identity:
                raise ValueError('Invalid preview identity')
            result = library.job(identity)
        except (KeyError, ValueError, AttributeError):
            raise HTTPException(404, 'Preview not found')
        if result['state'] != 'complete':
            raise HTTPException(409, 'Finish the preview before opening its guitar parts.')
        saved = result.get('result')
        if type(saved) is not dict:
            raise HTTPException(409, 'Saved guitar parts are unavailable for this preview.')
        companion = saved.get('acoustic_companion')
        if companion is None or (type(companion) is dict and companion.get('status') == 'unavailable'):
            raise HTTPException(404, 'Separate guitar parts are unavailable for this saved preview.')
        return result

    @app.get('/api/jobs/{identity}/parts')
    def parts(identity: str):
        from acoustic_companion import VERSION, verify_saved
        current = parts_job(identity)
        try:
            checked = verify_saved(library, current)
            manifest, reference = checked['manifest'], checked['reference']
            media = {row['name']: {'url': f"/api/jobs/{identity}/parts/{row['name']}",
                                 'bytes': row['bytes'], 'sha256': row['sha256']}
                     for row in reference['audio']}
            return {'version': VERSION, 'manifest': manifest, 'manifest_sha256': reference['bundle_id'],
                    'binding': {key: manifest[key] for key in ('source', 'clock', 'arrangement', 'renderer')},
                    'media': media}
        except (ValueError, KeyError, TypeError, OSError):
            # Companion integrity cannot rewrite or invalidate an accepted job.
            raise HTTPException(409, 'Saved guitar parts failed verification. The master preview is preserved.')

    @app.get('/api/jobs/{identity}/parts/{name}')
    def part_audio(identity: str, name: str):
        from acoustic_companion import protect_saved
        if name not in PART_FILES:
            raise HTTPException(404, 'Guitar part not found')
        current = parts_job(identity)
        owner = ExitStack()
        try:
            checked = owner.enter_context(protect_saved(library, current))
            row = next((row for row in checked['reference']['audio'] if row['name'] == name), None)
            if row is None:
                raise HTTPException(404, 'Guitar part not found')
            return _ProtectedPartsResponse(checked['audio'][name], size=row['bytes'],
                                           sha256=row['sha256'], owner=owner)
        except (ValueError, KeyError, TypeError, OSError, StopIteration):
            owner.close()
            raise HTTPException(409, 'Saved guitar parts failed verification. The master preview is preserved.')
        except BaseException:
            owner.close()
            raise

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

    def mixed_spec(value, *, frozen=None, companion_frozen=None):
        try:
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
            from acoustic_companion import request_fields
            companion_selection = {**selection, **({'acoustic_companion_version': value['acoustic_companion_version']}
                                                   if 'acoustic_companion_version' in value else {})}
            companion = request_fields(companion_selection, frozen=frozen if frozen is not None else companion_frozen)
        except (ValueError,KeyError,TypeError,AttributeError):
            raise HTTPException(400,'Choose an imported original, a valid passage up to 30 seconds, and one or two guitarists.')
        runtime=mixed_runtime_status() if frozen is None else {'ready':True,'manifest':{'runtime_version':frozen['runtime_version']}}
        if not runtime.get('ready'): raise HTTPException(503,runtime.get('reason') or 'The local mixed-preview runtime is not ready.')
        version=runtime.get('manifest',{}).get('runtime_version')
        if not isinstance(version,str) or not version: raise HTTPException(503,'The local mixed-preview runtime needs verification.')
        if frozen is None: require_instrument(selection)
        spec={'kind':'mixed','asset_id':asset['id'],'sha256':asset['sha256'],
              'performers':performers,'start_seconds':start,'end_seconds':end,
              'pipeline_version':MIXED_PIPELINE_VERSION,'runtime_version':version,
              'renderer_version':renderer_version(selection),**selection,**companion}
        if not guidance_is_default(guidance): spec['guidance']=guidance
        if frozen is not None:
            for field in ('pipeline_version','renderer_version'): spec[field]=frozen[field]
            if selection.get('instrument','acoustic')==frozen.get('instrument','acoustic'):
                for field in ('bass_planner_version','bass_profile_version','electric_planner_version','electric_profile_version',
                              'electric_tone_version','electric_tone_window_version'):
                    if field in selection: spec[field]=frozen[field]
        return spec

    def processing_receipt(row):
        spec=row['request']
        job_id=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps(spec,sort_keys=True)))
        return {'operation_id':row['operation_id'],'job_id':job_id,
                'state':{'waiting_resources':'waiting','cancel_requested':'cancelling'}.get(row['state'],row['state']),
                'reason':row.get('reason'), 'request':spec}

    async def processing_tick():
        # The same admission lock/process registry serves immediate previews and
        # queued work. Never hold worker.lock while spawning its future owner.
        async with preview_lock:
            rows=processing.list(states=('queued','waiting_resources','running','cancel_requested'))
            if not rows: return
            for identity,child in list(processes.items()):
                if child.poll() is not None and library.job(identity)['state'] in ('queued','running'):
                    library.update_job(identity,'failed',error='The preview worker stopped unexpectedly.')
            try: await reap_terminal_workers()
            except HTTPException: return
            for row in rows:
                if row['state'] not in ('running','cancel_requested'): continue
                identity=processing_receipt(row)['job_id']
                try:
                    current=checked_job(identity)
                    child=processes.get(identity)
                    if child is None and current['state'] in ('queued','running'):
                        # A write/launch fault can leave a durable claim without
                        # a registered process. Do not strand the whole lane or
                        # infer that an unobserved worker is safe to replace.
                        try:
                            with library.exclusive('worker.lock'):
                                library.update_job(identity,'failed',error='The processing attempt could not start safely. Your original and settings are preserved.')
                        except OSError: continue
                        current=library.job(identity)
                    if child and child.poll() is not None and current['state'] in ('queued','running'):
                        library.update_job(identity,'failed',error='The preview worker stopped unexpectedly.')
                        current=library.job(identity)
                    if row['state']=='cancel_requested' and (not child or child.poll() is not None):
                        processing.finish(row['operation_id'],'cancelled',claim_token=row['claim_token'])
                    elif current['state'] in ('complete','failed','cancelled','interrupted'):
                        state='failed' if current['state']=='interrupted' else current['state']
                        if row['state']=='cancel_requested': state='cancelled'
                        processing.finish(row['operation_id'],state,reason=current.get('error'),claim_token=row['claim_token'])
                except KeyError:
                    processing.finish(row['operation_id'],'failed',reason='The saved processing attempt is missing. Your original is preserved.',claim_token=row['claim_token'])
            row=processing.peek()
            if row is None: return
            if processes:
                admission.reset()
                processing.wait(row['operation_id'],'Waiting for the current laptop preview to finish.')
                return
            identity=processing_receipt(row)['job_id']
            try:
                prior=checked_job(identity)
                if prior['state']=='complete':
                    claimed=processing.claim_next(expected_id=row['operation_id'])
                    if claimed: processing.finish(claimed['operation_id'],'complete',claim_token=claimed['claim_token'])
                    return
            except KeyError: pass
            ready,reason=admission.check()
            if not ready:
                processing.wait(row['operation_id'],reason)
                return
            try: library.space(256*1024**2)
            except ValueError:
                processing.wait(row['operation_id'],'Waiting for disk space while keeping the 8 GB reserve.')
                return
            try:
                spec=row['request']
                # Rebuild from the public choices and compare all pinned versions.
                choices={k:spec[k] for k in ('asset_id','start_seconds','end_seconds','performers','instrument','bass_part','guidance','electric_tone','acoustic_companion_version') if k in spec}
                if mixed_spec(choices,companion_frozen=spec)!=spec:
                    raise ValueError('The processing software changed. Review and add a new request.')
                if not await asyncio.to_thread(library.verify,spec['asset_id']):
                    raise ValueError('The saved original failed verification. Restore it before trying again.')
            except (HTTPException,ValueError,KeyError,OSError) as error:
                claimed=processing.claim_next(expected_id=row['operation_id'])
                if claimed: processing.finish(claimed['operation_id'],'failed',reason=getattr(error,'detail',str(error)),claim_token=claimed['claim_token'])
                return
            ready,reason=admission.check()
            if not ready:
                processing.wait(row['operation_id'],reason)
                return
            claimed=processing.claim_next(expected_id=row['operation_id'])
            if claimed is None: return
            with library.connect() as db:
                db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',
                    (identity,'queued',0,json.dumps(spec),None,None,time.time()))
            ready,reason=admission.before_start()
            if not ready:
                processing.wait(claimed['operation_id'],reason,claim_token=claimed['claim_token'])
                return
            try:
                processes[identity]=subprocess.Popen([sys.executable,str(ROOT/'worker.py'),str(library.root),identity],cwd=ROOT,
                    stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            except OSError:
                reason='The local worker could not start. Your request and original are preserved.'
                library.update_job(identity,'failed',error=reason)
                processing.finish(claimed['operation_id'],'failed',reason=reason,claim_token=claimed['claim_token'])
            admission.reset()

    async def processing_loop():
        while True:
            try:
                await processing_tick()
                app.state.processing_error=None
            except (OSError,ValueError,RuntimeError,sqlite3.Error):
                # Keep the durable request. Do not invent a completed receipt or
                # launch another process after an uncertain state transition.
                app.state.processing_error='The processing queue needs attention on the laptop.'
            await asyncio.sleep(2)

    app.state.processing_tick=processing_tick

    @app.get('/api/processing-queue')
    def processing_list(offset:int=0):
        if not 0<=offset<=10000: raise HTTPException(400,'Invalid history page.')
        active=processing.list(states=('queued','waiting_resources','running','cancel_requested'))
        history=processing.list(states=('complete','failed','cancelled'),limit=100,offset=offset)
        return {'items':[processing_receipt(row) for row in active],
                'history':[processing_receipt(row) for row in history], 'history_offset':offset,
                'reason':getattr(app.state,'processing_error',None)}

    @app.get('/api/processing-queue/{operation_id}')
    def processing_get(operation_id:str):
        try: return processing_receipt(processing.get(operation_id))
        except (KeyError,ValueError): raise HTTPException(404,'Processing request not found.')

    @app.post('/api/processing-queue')
    @serialized_preview
    async def processing_submit(request:Request):
        try:
            value=json.loads(await small_body(request,4096))
            if type(value) is not dict or set(value)!={'operation_id','request'}:
                raise ValueError('Choose a processing request and operation ID.')
            # Strict UUID validation also prevents this identity becoming a path.
            if str(uuid.UUID(value['operation_id']))!=value['operation_id']: raise ValueError('Invalid operation ID.')
            try: existing=processing.get(value['operation_id'])
            except KeyError: existing=None
            spec=mixed_spec(value['request'],frozen=existing['request'] if existing else None)
            if existing is None and not await asyncio.to_thread(library.verify,spec['asset_id']):
                raise ValueError('The laptop original failed verification.')
            job_id=str(uuid.uuid5(uuid.NAMESPACE_URL,json.dumps(spec,sort_keys=True)))
            row=processing.submit(value['operation_id'],spec,job_id=job_id)
        except QueueConflict as error: raise HTTPException(409,str(error))
        except QueueFull as error: raise HTTPException(429,str(error))
        except (ValueError,TypeError,KeyError,AttributeError) as error:
            raise HTTPException(400,str(error))
        return processing_receipt(row)

    @app.post('/api/processing-queue/{operation_id}/cancel')
    @serialized_preview
    async def processing_cancel(operation_id:str):
        try: row=processing.cancel(operation_id)
        except (KeyError,ValueError): raise HTTPException(404,'Processing request not found.')
        identity=processing_receipt(row)['job_id']
        interested=[item for item in processing.list(states=('queued','waiting_resources','running'))
                    if processing_receipt(item)['job_id']==identity and item['operation_id']!=operation_id]
        if not interested and identity in processes and processes[identity].poll() is None:
            (library.stage/(identity+'.cancel')).touch()
        return processing_receipt(row)

    @app.post('/api/arrange-preview')
    @serialized_preview
    async def arrange_preview(request: Request):
        try: value=json.loads(await small_body(request,4096))
        except ValueError: raise HTTPException(400,'Choose valid preview settings.')
        spec=mixed_spec(value)
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
        require_instrument(spec)
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
