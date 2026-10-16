"""Frozen silent candidate review and scripted selected-note app checks."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
import wave

from fastapi.testclient import TestClient
from server import create_app
from audio.instrumental_runtime import require_runtime
from instrumental_preview import checked_candidates,prepare_arrangement,arrange_review
from analysis_evidence import AnalysisEvidence

APP=Path(__file__).resolve().parents[2]
ROOT=APP/'audio/research'
OUT=ROOT/'instrumental-preview-001'
PRIOR=ROOT/'guitarset/recovery-001'
CAP=24*1024**2

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
    runtime=require_runtime()
    names=['server.py','worker.py','library.py','analysis_evidence.py','instrumental_preview.py',
           'audio/instrumental_analysis.py','audio/instrumental_runtime.py','audio/analysis_runtime.py',
           'audio/mixed_analysis.py','audio/managed_process.py','audio/basic_pitch.py','audio/basic_pitch_upstream.py',
           'audio/performance.py','audio/bass_performance.py','audio/render.py','audio/bass_render.py',
           'audio/growlybass.py','audio/preview_instruments.py','audio/preferences.py']
    inputs=[Path(__file__),*[APP/n for n in names],PRIOR/'raw-mixture.wav',PRIOR/'frozen-plan.json',
            PRIOR/'estimated-other-published_default.json',PRIOR/'estimated-other-custom_onset_only.json',
            ROOT/'growlybass/adapter-002/frozen.json',APP/'audio/vendor/analysis/runtime.json']
    OUT.mkdir(exist_ok=False)
    frozen=dict(inputs={str(p.relative_to(APP)):sha(p) for p in inputs},runtime_version=runtime['runtime_version'],
        source=str((PRIOR/'raw-mixture.wav').relative_to(APP)),start_seconds=0.,end_seconds=6.,
        interpretation='published',selection_rule='First five positive-duration events in saved published event order; no audition or outcome-based changes',
        accepted_role='Scripted lead-role workflow check only, not human/source musical acceptance',
        instruments=['acoustic','jazz-bass'],cancellation_passage=[.1,5.9],
        previous_decoder_events_must_match=True,maximum_wall_seconds=240,
        research_bytes_before=used,free_bytes_before=free,additional_budget_bytes=CAP,audio_played=False)
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    snapshots={}
    for name,digest in frozen['inputs'].items():
        if Path(name).suffix!='.py':continue
        target=OUT/'code'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((APP/name).read_bytes());snapshots[name]=digest
    (OUT/'code-snapshots.json').write_text(json.dumps(snapshots,indent=2),encoding='utf-8')
    evidence=dict(state='started',frozen_sha256=sha(OUT/'frozen.json'),audio_played=False,renders=[])
    began=time.monotonic()
    def save():
        p=OUT/'evidence.partial';p.write_text(json.dumps(evidence,indent=2),encoding='utf-8');p.replace(OUT/'evidence.json')
    save();app=create_app(OUT/'library');library=app.state.library
    def wait(client,identity):
        last=None
        while time.monotonic()-began<240:
            job=client.get('/api/jobs/'+identity).json();footprint=size(OUT)
            evidence['peak_directory_bytes']=max(footprint,evidence.get('peak_directory_bytes',0))
            if footprint>CAP:client.post('/api/jobs/'+identity+'/cancel');raise RuntimeError('Study storage cap reached')
            current=(job['state'],int(job['progress']*10))
            if current!=last:print(json.dumps(dict(id=identity,state=current)),flush=True);last=current
            if job['state'] not in ('queued','running'):return job
            time.sleep(.3)
        client.post('/api/jobs/'+identity+'/cancel');raise TimeoutError('Inspect this retained job; do not restart automatically')
    try:
        with TestClient(app,base_url='http://127.0.0.1:8765') as client:
            client.get('/session')
            imported=client.post('/api/import',content=(PRIOR/'raw-mixture.wav').read_bytes(),headers={'X-Filename':'Private controlled guitar mixture.wav'})
            assert imported.status_code==200,imported.text
            source=imported.json();evidence['source']=source;save()
            request=dict(asset_id=source['id'],start_seconds=0.,end_seconds=6.)
            response=client.post('/api/instrumental-candidates',json=request);assert response.status_code==200,response.text
            parent=wait(client,response.json()['id']);assert parent['state']=='complete',parent
            assert 'asset' not in parent['result'] and len(library.items())==1
            bundle,_=checked_candidates(library,parent);assert bundle['source_part']['instrument']=='unknown'
            assert set(bundle['interpretations'])=={'published','onset_only'}
            for key,old_name in [('published','published_default'),('onset_only','custom_onset_only')]:
                old=json.loads((PRIOR/('estimated-other-'+old_name+'.json')).read_text())['events']
                assert bundle['interpretations'][key]['events']==old,'Prior fixed events changed: '+key
            raw_path=AnalysisEvidence(library).verify(parent['result']['evidence'])
            assert sha(raw_path)==bundle['raw_activations']['sha256']
            selected=[e['id'] for e in bundle['interpretations']['published']['events'][:5]];assert len(selected)==5
            evidence.update(candidate_job=parent,selected_note_ids=selected,prior_decoder_events_equal=True);save()
            retry=client.post('/api/instrumental-candidates',json=request).json();assert retry['id']==parent['id'] and retry['state']=='complete'
            before=copy.deepcopy(parent)
            for instrument in frozen['instruments']:
                selection=dict(parent_job_id=parent['id'],interpretation='published',note_ids=selected,
                               instrument=instrument,accept_as_lead=True)
                response=client.post('/api/arrange-instrumental',json=selection);assert response.status_code==200,response.text
                job=wait(client,response.json()['id']);assert job['state']=='complete',job
                expected=arrange_review(prepare_arrangement(library,selection));a=job['result']['arrangement']
                assert a==json.loads(json.dumps(expected)) and len(a['source']['notes'])==5
                assert a['candidate_review']['source_part']['instrument']=='unknown'
                assert 'analysis_process' not in job['result']['metrics']
                for note in a['source']['notes']:
                    event=next(e for e in bundle['interpretations']['published']['events'] if e['id']==note['id'])
                    assert (note['start'],note['end'],note['pitch'],note['velocity'],note['confidence'])==(
                        event['start'],event['end'],event['pitch'],80,event['mean_note_activation'])
                asset=job['result']['asset'];path=library.media/asset['sha256'];assert sha(path)==asset['sha256']
                with wave.open(str(path)) as wav:assert wav.getsampwidth()==2 and wav.getnchannels()==2 and wav.getframerate()==44100
                assert job['result']['metrics']['clipped_samples']==0
                assert library.job(parent['id'])==before and library.verify(source['id'])
                retry=client.post('/api/arrange-instrumental',json=selection).json();assert retry['id']==job['id'] and retry['state']=='complete'
                evidence['renders'].append(job);save()
            cancelled=client.post('/api/instrumental-candidates',json={**request,'start_seconds':.1,'end_seconds':5.9})
            assert cancelled.status_code==200,cancelled.text
            identity=cancelled.json()['id'];assert client.post('/api/jobs/'+identity+'/cancel').status_code==200
            cancelled=wait(client,identity);assert cancelled['state']=='cancelled',cancelled
            evidence.update(cancelled_job=cancelled,parent_unchanged=True,source_unchanged=True,completed_retries=True)
        assert not list(library.stage.iterdir())
        assert all(sha(APP/name)==digest for name,digest in frozen['inputs'].items())
        evidence.update(state='passed',staging_clean=True,elapsed_seconds=time.monotonic()-began,retained_bytes=size(OUT))
        save();assert size(OUT)<CAP
        print(json.dumps(dict(state='passed',seconds=evidence['elapsed_seconds'],bytes=size(OUT))),flush=True)
    except BaseException as error:
        evidence.update(state='failed',error=repr(error),elapsed_seconds=time.monotonic()-began);save();raise

if __name__=='__main__':main()
