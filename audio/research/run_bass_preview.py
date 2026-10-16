"""Silent real import/analyze/bass-render/revise checks in an isolated library."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
import wave

from fastapi.testclient import TestClient
from server import create_app
from audio.preview_instruments import selection_fields, BASS_RENDER_VERSION
from audio.growlybass import require_bank, moderate_map

APP=Path(__file__).resolve().parents[2]
ROOT=APP/'audio/research'
OUT=ROOT/'bass-preview-001'
CAP=16*1024**2

def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def size(folder):
    total=0
    for path in folder.rglob('*'):
        try:
            if path.is_file():total+=path.stat().st_size
        except FileNotFoundError:pass
    return total

def main():
    used=size(ROOT)+size(APP/'audio/vendor/analysis');free=shutil.disk_usage(ROOT).free
    assert used+CAP<=4*1024**3 and free>=8*1024**3+CAP
    bank=require_bank()
    cases=[dict(performers=1,bass_part='melody'),dict(performers=1,bass_part='bass-line'),
           dict(performers=2,bass_part='melody-and-bass')]
    files=[Path(__file__),APP/'server.py',APP/'worker.py',APP/'library.py',APP/'preview_refinement.py',
           *[APP/'audio'/name for name in ('preview_instruments.py','bass_performance.py','bass_render.py',
              'growlybass.py','performance.py','refinement.py','preferences.py','render.py','mixed_analysis.py',
              'analysis_runtime.py','managed_process.py')],ROOT/'mixed/mixed.wav',ROOT/'app-preview-003/evidence.json',
           ROOT/'growlybass/adapter-002/frozen.json',ROOT/'growlybass/adapter-002/result.json',
           APP/'audio/vendor/analysis/runtime.json']
    operations=[dict(kind='join',note_ids=['melody-104449','melody-143169','melody-148009','melody-151089'])]
    OUT.mkdir(exist_ok=False)
    frozen=dict(code_inputs={str(p.relative_to(APP)):sha(p) for p in files},cases=cases,
                instrument='jazz-bass',start_seconds=.25,end_seconds=6.25,operations=operations,
                renderer_version=BASS_RENDER_VERSION,bank_profile=bank['profile_version'],
                moderate_map_sha256=hashlib.sha256(moderate_map(bank['bank']).encode()).hexdigest(),
                native_inventory_reference='audio/research/growlybass/adapter-002/frozen.json',
                research_bytes_before=used,free_bytes_before=free,additional_budget_bytes=CAP,
                maximum_wall_seconds=240,audio_played=False,
                acceptance='Operational only; no musical acceptance or quality improvement claim')
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    evidence=dict(state='started',frozen_sha256=sha(OUT/'frozen.json'),cases=[],audio_played=False)
    def save():
        p=OUT/'evidence.partial';p.write_text(json.dumps(evidence,indent=2),encoding='utf-8');p.replace(OUT/'evidence.json')
    start=time.monotonic();save()
    app=create_app(OUT/'library');library=app.state.library
    def wait(client,identity):
        last=None
        while time.monotonic()-start<240:
            job=client.get('/api/jobs/'+identity).json();footprint=size(OUT)
            evidence['peak_directory_bytes']=max(footprint,evidence.get('peak_directory_bytes',0))
            if footprint>CAP:
                client.post('/api/jobs/'+identity+'/cancel');raise RuntimeError('Storage cap reached')
            if (job['state'],job['progress'])!=last:
                last=(job['state'],job['progress']);print(json.dumps(dict(job=identity,state=last)),flush=True)
            if job['state'] not in ('queued','running'):return job
            time.sleep(.3)
        client.post('/api/jobs/'+identity+'/cancel');raise TimeoutError('Inspect retained job, do not automatically restart')
    try:
        with TestClient(app,base_url='http://127.0.0.1:8765') as client:
            client.get('/session')
            response=client.post('/api/import',content=(ROOT/'mixed/mixed.wav').read_bytes(),
                                 headers={'X-Filename':'Private development excerpt.wav'})
            assert response.status_code==200,response.text
            source=response.json();evidence['source']=source;save()
            parent=None;observed=None
            for case in cases:
                request=dict(asset_id=source['id'],instrument='jazz-bass',start_seconds=.25,end_seconds=6.25,**case)
                response=client.post('/api/arrange-preview',json=request);assert response.status_code==200,response.text
                job=wait(client,response.json()['id']);assert job['state']=='complete',job
                a=job['result']['arrangement'];m=job['result']['metrics'];asset=job['result']['asset']
                if observed is None:observed=copy.deepcopy(a['source']);parent=copy.deepcopy(job)
                assert a['source']==observed
                assert a['instrument']=='jazz-bass' and a['part_selection']==case['bass_part']
                assert len(a['source']['notes'])==37
                assert a['render_origin_sample']==11025
                assert m['renderer_version']==BASS_RENDER_VERSION and m['command_transpose_semitones']==12
                assert m['clipped_samples']==0 and m['processing_mode']=='freewheeling'
                assert {n['player'] for n in a['notes']}==set(range(1,case['performers']+1))
                for n in a['notes']:
                    original=next(x for x in observed['notes'] if x['id']==n['id'])
                    assert (n['start'],n['end'],n['velocity'],n['source_pitch'])==(original['start'],original['end'],original['velocity'],original['pitch'])
                path=library.media/asset['sha256'];assert sha(path)==asset['sha256']
                with wave.open(str(path)) as wav:
                    assert (wav.getframerate(),wav.getnchannels(),wav.getsampwidth(),wav.getnframes())==(
                        44100,2,2,max(n['end'] for n in a['notes'])-11025+44100)
                retry=client.post('/api/arrange-preview',json=request).json()
                assert retry['id']==job['id'] and retry['state']=='complete'
                evidence['cases'].append(job);save()
            before=copy.deepcopy(parent)
            request=dict(parent_job_id=parent['id'],operations=operations)
            response=client.post('/api/refine-preview',json=request);assert response.status_code==200,response.text
            revised=wait(client,response.json()['id']);assert revised['state']=='complete',revised
            a=revised['result']['arrangement']
            assert a['source']==observed and len(a['refinement_source']['notes'])==34
            assert a['part_selection']=='melody' and a['refinement']['parent_job_id']==parent['id']
            assert library.job(parent['id'])==before and library.verify(source['id'])
            retry=client.post('/api/refine-preview',json=request).json()
            assert retry['id']==revised['id'] and retry['state']=='complete'
            assert client.get('/api/previews/latest').json()['id']==revised['id']
            evidence.update(revision=revised,parent_unchanged=True,source_unchanged=True,completed_retries=True)
        assert not list(library.stage.iterdir())
        assert all(sha(APP/p)==h for p,h in frozen['code_inputs'].items())
        assert size(OUT)<CAP
        evidence.update(state='passed',staging_clean=True,elapsed_seconds=time.monotonic()-start,
                        output_directory_bytes=size(OUT))
        save();print(json.dumps(dict(state='passed',elapsed_seconds=evidence['elapsed_seconds'],bytes=size(OUT))),flush=True)
    except BaseException as error:
        evidence.update(state='failed',error=repr(error),elapsed_seconds=time.monotonic()-start);save();raise

if __name__=='__main__':main()
