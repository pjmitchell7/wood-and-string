"""One silent real API case; fresh isolated library, no retries on failure."""
import hashlib
import json
from pathlib import Path
import shutil
import time
from fastapi.testclient import TestClient
from server import create_app
from audio.analysis_runtime import runtime_status, MANIFEST_PATH
from audio.render import RENDER_VERSION

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT/'audio/research'
OUT = RESEARCH/'guided-preview-001'
RESERVE = 32*1024**2

def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def size(tree):
    total=0
    for p in tree.rglob('*'):
        try:
            if p.is_file(): total+=p.stat().st_size
        except FileNotFoundError: pass  # Owned staging files can finish between observations.
    return total

def main():
    used = size(RESEARCH)+size(ROOT/'audio/vendor/analysis')
    free = shutil.disk_usage(ROOT).free
    assert used+RESERVE <= 4*1024**3 and free >= 8*1024**3+RESERVE, (used,free)
    source = RESEARCH/'compressed-preview-001/fixture.mp3'
    source_hash = digest(source)
    runtime = runtime_status(); assert runtime['ready'], runtime['reason']
    OUT.mkdir(exist_ok=False)
    guidance = {'version':1,'source_notes':'The original mixes a vocal lead with bass and accompaniment.',
                'arrangement_notes':'Keep the estimated lead alone for this comparison.', 'accompaniment':'melody-only'}
    settings = {'start_seconds':.25,'end_seconds':6.25,'performers':1,'guidance':guidance}
    files = ['server.py','worker.py','library.py','audio/preferences.py','audio/performance.py',
             'audio/render.py','audio/managed_process.py','audio/mixed_analysis.py','audio/analysis_runtime.py',
             'audio/analysis-requirements.lock','audio/research/run_guided_preview.py',
             'audio/vendor/sfizz/bin/Release/sfizz.dll','audio/vendor/Meta-open-sound/meta-steel-guitar/meta-steel-guitar.sfz']
    frozen = {'source_sha256':source_hash,'source_path':str(source),'runtime_version':runtime['runtime_version'],
              'runtime_manifest_sha256':digest(MANIFEST_PATH),'code_sha256':{p:digest(ROOT/p) for p in files},
              'settings':settings,'settings_sha256':hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest(),
              'renderer_version':RENDER_VERSION,'research_bytes_before':used,'free_bytes_before':free,
              'additional_budget_bytes':RESERVE,'played_audio':False}
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    evidence = {'state':'started','frozen_sha256':digest(OUT/'frozen.json'),'played_audio':False}
    def save():
        (OUT/'evidence.partial').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        (OUT/'evidence.partial').replace(OUT/'evidence.json')
    save(); began=time.monotonic(); app=create_app(OUT/'library')
    try:
        with TestClient(app,base_url='http://127.0.0.1:8765') as client:
            assert client.get('/session').status_code==200
            response=client.post('/api/import',content=source.read_bytes(),headers={'X-Filename':'Private fixture.mp3'})
            assert response.status_code==200,response.text
            asset=response.json(); spec={'asset_id':asset['id'],**settings}
            response=client.post('/api/arrange-preview',json=spec)
            assert response.status_code==200,response.text
            identity=response.json()['id']; evidence.update(job_id=identity,request=spec); save()
            previous=None
            while time.monotonic()-began<250:
                job=client.get('/api/jobs/'+identity).json()
                observed_bytes=size(OUT)
                evidence['sampled_peak_directory_bytes']=max(observed_bytes,evidence.get('sampled_peak_directory_bytes',0))
                if observed_bytes>RESERVE:
                    client.post('/api/jobs/'+identity+'/cancel')
                    raise RuntimeError('Research output budget exceeded; cancellation requested')
                evidence['job']=job; save()
                state=(job['state'],job['progress'])
                if state!=previous: print(json.dumps({'state':state}),flush=True); previous=state
                if job['state'] not in ('queued','running'): break
                time.sleep(.25)
            else: raise TimeoutError('Inspect this same job; do not launch another case')
            assert job['state']=='complete',job
            result=job['result']; arrangement=result['arrangement']; metrics=result['metrics']
            bass={n['id'] for n in arrangement['source']['notes'] if n['role']=='bass'}
            assert bass, 'This fixture must exercise actual bass omission'
            assert arrangement['notes'] and all(n['role']!='bass' for n in arrangement['notes'])
            omissions=[a for a in arrangement['adaptations'] if a['kind']=='user-requested-omission']
            assert len(omissions)==len(bass) and {a['note_id'] for a in omissions}==bass
            assert set(arrangement['user_guidance']['omitted_note_ids'])==bass
            assert all(arrangement['user_guidance'][k]==v for k,v in guidance.items())
            assert metrics['renderer_version']==RENDER_VERSION and metrics['processing_mode']=='freewheeling'
            assert metrics['quality_by_instance'] and metrics['clipped_samples']==0
            assert result['analysis']['timebase']['decoded'] is True
            assert arrangement['render_origin_sample']==11025
            assert all(11025<=n['start']<n['end']<=275625 for n in arrangement['notes'])
            rendered=client.get('/api/media/'+result['asset']['id']).content
            assert hashlib.sha256(rendered).hexdigest()==result['asset']['sha256']
            (OUT/'melody-only.wav').write_bytes(rendered)
            assert hashlib.sha256(client.get('/api/media/'+asset['id']).content).hexdigest()==source_hash
            retry=client.post('/api/arrange-preview',json=spec)
            assert retry.status_code==200 and retry.json()['id']==identity and retry.json()['state']=='complete'
            evidence.update(bass_events_preserved=len(bass),bass_events_explicitly_omitted=len(omissions),
                            planned_notes=len(arrangement['notes']),retry_same_completed_job=True,
                            output_sha256=digest(OUT/'melody-only.wav'))
        assert not list(app.state.library.stage.iterdir())
        assert digest(source)==source_hash
        assert all(digest(ROOT/p)==value for p,value in frozen['code_sha256'].items())
        assert digest(MANIFEST_PATH)==frozen['runtime_manifest_sha256']
        assert size(OUT)<=RESERVE
        evidence.update(state='passed',staging_clean=True,original_unchanged=True,
                        elapsed_seconds=time.monotonic()-began,output_directory_bytes=size(OUT))
        save(); print(json.dumps({'state':'passed','metrics':metrics,'output':str(OUT)}),flush=True)
    except BaseException as exc:
        evidence.update(state='failed',error=repr(exc),elapsed_seconds=time.monotonic()-began)
        save(); raise

if __name__=='__main__': main()
