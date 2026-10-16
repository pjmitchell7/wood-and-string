"""One actual silent saved-preview revision, in a fresh isolated library.

The scripted join tests the workflow. It is not a human-adjudicated correction
or a claimed improvement to the Music Delta training excerpt.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
import wave

from fastapi.testclient import TestClient
from server import create_app
from audio.render import RENDER_VERSION, ASSETS

APP = Path(__file__).resolve().parents[2]
ROOT = APP/'audio/research'
OUT = ROOT/'refinement-preview-001'
BUDGET = 32 * 1024 ** 2
MEMBERS = ['melody-104449','melody-143169','melody-148009','melody-151089']


def sha(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def size(tree):
    total = 0
    for path in tree.rglob('*'):
        try:
            if path.is_file(): total += path.stat().st_size
        except FileNotFoundError: pass
    return total


def main():
    used = size(ROOT) + size(APP/'audio/vendor/analysis')
    free = shutil.disk_usage(ROOT).free
    assert used + BUDGET <= 4*1024**3 and free >= 8*1024**3+BUDGET
    files = [Path(__file__), APP/'server.py', APP/'worker.py', APP/'library.py',
             APP/'preview_refinement.py', APP/'audio/refinement.py', APP/'audio/preferences.py',
             APP/'audio/performance.py', APP/'audio/render.py', APP/'audio/managed_process.py',
             ROOT/'mixed/mixed.wav', ROOT/'app-preview-003/evidence.json',
             ROOT/'app-preview-003/solo.wav', APP/'audio/vendor/sfizz/bin/Release/sfizz.dll',
             ASSETS/'meta-steel-guitar.sfz'] + sorted((ASSETS/'ogg').rglob('*.ogg'))
    OUT.mkdir(exist_ok=False)
    frozen = dict(files={str(p.relative_to(APP)):sha(p) for p in files},
                  parent='Exact retained app-preview-003 solo source evidence and note plan',
                  operations=[dict(kind='join',note_ids=MEMBERS)],
                  purpose='Scripted workflow validation, not a musical decision',
                  renderer_version=RENDER_VERSION, research_bytes_before=used,
                  free_bytes_before=free, additional_budget_bytes=BUDGET, wall_seconds=120,
                  no_analysis_or_playback=True)
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    evidence = dict(state='started',frozen_sha256=sha(OUT/'frozen.json'),audio_played=False)
    def save():
        partial = OUT/'evidence.partial'
        partial.write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        partial.replace(OUT/'evidence.json')
    began = time.monotonic(); save()
    app = create_app(OUT/'library'); library = app.state.library
    try:
        old = json.loads((ROOT/'app-preview-003/evidence.json').read_text())['cases'][0]
        source_stage = library.stage/'original-fixture'
        source_stage.write_bytes((ROOT/'mixed/mixed.wav').read_bytes())
        source = library.publish(source_stage,'Private training fixture.wav','audio/wav',
                                 source_id=old['result']['arrangement']['source_id'])
        parent_stage = library.stage/'parent-fixture'
        parent_stage.write_bytes((ROOT/'app-preview-003/solo.wav').read_bytes())
        parent_asset = library.publish(parent_stage,'Prior solo.wav','audio/wav',kind='mixed-preview-1',
                                       source_id=source['source_id'])
        parent_spec = {**old['request'],'asset_id':source['id'],'sha256':source['sha256']}
        parent_result = {**copy.deepcopy(old['result']),'asset':parent_asset}
        with library.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)',
                       (old['id'],'complete',1,json.dumps(parent_spec),json.dumps(parent_result),None,time.time()))
        parent_before = library.job(old['id'])
        request = dict(parent_job_id=old['id'],operations=frozen['operations'])
        with TestClient(app,base_url='http://127.0.0.1:8765') as client:
            assert client.get('/session').status_code == 200
            response = client.post('/api/refine-preview',json=request)
            assert response.status_code == 200, response.text
            identity = response.json()['id']; evidence.update(job_id=identity,request=request); save()
            previous = None
            while time.monotonic()-began < 120:
                job = client.get('/api/jobs/'+identity).json()
                evidence['job'] = job
                footprint = size(OUT)
                evidence['sampled_peak_directory_bytes'] = max(footprint,evidence.get('sampled_peak_directory_bytes',0))
                if footprint > BUDGET:
                    client.post('/api/jobs/'+identity+'/cancel')
                    raise RuntimeError('Output budget exceeded; cancellation requested')
                save()
                state = (job['state'],job['progress'])
                if state != previous: print(json.dumps(dict(state=state)),flush=True); previous = state
                if job['state'] not in ('queued','running'): break
                time.sleep(.25)
            else: raise TimeoutError('Inspect this same saved job; do not restart automatically')
            assert job['state']=='complete',job
            result = job['result']; arrangement = result['arrangement']; metrics = result['metrics']
            original = parent_result['arrangement']['source']
            assert arrangement['source'] == original
            assert result['analysis'] == parent_result['analysis']
            revised = arrangement['refinement_source']['notes']
            expected = [n for n in original['notes'] if n['id'] not in MEMBERS]
            assert [n for n in revised if not n['id'].startswith('joined-')] == sorted(expected,key=lambda n:(n['start'],n['end'],n['id']))
            joined = [n for n in revised if n['id'].startswith('joined-')]
            assert len(joined)==1 and (joined[0]['start'],joined[0]['end'],joined[0]['pitch'])==(104229,152189,62)
            assert len(original['notes'])==37 and len(revised)==34
            edits = arrangement['refinement']['edits']
            assert len(edits)==1 and len(edits[0]['members'])==4
            assert edits[0]['unobserved_gaps']==[
                {'start':141189,'end':142949},{'start':145589,'end':147789},{'start':149109,'end':150869}]
            assert arrangement['refinement']['parent_job_id']==old['id']
            assert arrangement['render_origin_sample']==11025
            assert metrics['renderer_version']==RENDER_VERSION and metrics['processing_mode']=='freewheeling'
            assert 'analysis_process' not in metrics and metrics['clipped_samples']==0
            assert library.job(old['id']) == parent_before
            assert library.verify(source['id']) and library.verify(parent_asset['id'])
            rendered = client.get('/api/media/'+result['asset']['id']).content
            assert hashlib.sha256(rendered).hexdigest()==result['asset']['sha256']
            (OUT/'revised-solo.wav').write_bytes(rendered)
            with wave.open(str(OUT/'revised-solo.wav')) as wav:
                geometry = dict(frames=wav.getnframes(),sample_rate=wav.getframerate(),channels=wav.getnchannels(),sample_width=wav.getsampwidth())
            assert geometry==dict(frames=307584,sample_rate=44100,channels=2,sample_width=2)
            retry = client.post('/api/refine-preview',json=request).json()
            assert retry['id']==identity and retry['state']=='complete'
            evidence.update(original_notes=37,corrected_notes=34,planned_notes=len(arrangement['notes']),
                            explicit_join_members=4,unobserved_gap_samples=5720,parent_unchanged=True,
                            source_unchanged=True,completed_retry_same_job=True,output_geometry=geometry,
                            output_sha256=sha(OUT/'revised-solo.wav'))
        assert not list(library.stage.iterdir())
        assert all(sha(APP/p)==h for p,h in frozen['files'].items())
        assert size(OUT)<BUDGET
        evidence.update(state='passed',staging_clean=True,elapsed_seconds=time.monotonic()-began,
                        output_directory_bytes=size(OUT),musical_acceptance='Not assessed; scripted edit only')
        save(); print(json.dumps(dict(state='passed',metrics=metrics,output=str(OUT))),flush=True)
    except BaseException as error:
        evidence.update(state='failed',error=repr(error),elapsed_seconds=time.monotonic()-began)
        save(); raise


if __name__=='__main__': main()
