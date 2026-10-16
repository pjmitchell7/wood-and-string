"""Silent actual-mixture app integration; retained private fixture evidence only."""
import hashlib
import argparse
import json
from pathlib import Path
import time
import re
from fastapi.testclient import TestClient
from server import create_app
from audio.analysis_runtime import runtime_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-name', default='app-preview-001')
    name = parser.parse_args().output_name
    if not re.fullmatch(r'app-preview-[0-9]{3}', name):
        raise ValueError('Choose a fresh app-preview-NNN evidence directory.')
    research = Path(__file__).resolve().parent
    output = research/name
    output.mkdir(exist_ok=False)
    fixture = research/'mixed/mixed.wav'
    raw = fixture.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert runtime_status()['ready']
    app = create_app(output/'library')
    evidence = {'scope': 'Actual training-fixture mixture; no human listening or fidelity acceptance',
                'source_sha256': digest, 'cases': [], 'played_audio': False}
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.get('/session').status_code == 200
        response = client.post('/api/import', content=raw, headers={'X-Filename': 'private-research-mixture.wav'})
        assert response.status_code == 200, response.text
        original = response.json()
        for performers in (1, 2):
            spec = {'asset_id': original['id'], 'start_seconds': .25, 'end_seconds': 6.25, 'performers': performers}
            began = time.monotonic()
            response = client.post('/api/arrange-preview', json=spec)
            assert response.status_code == 200, response.text
            identity = response.json()['id']
            last_progress = -1
            while time.monotonic()-began < 245:
                job = client.get('/api/jobs/'+identity).json()
                if job['progress'] != last_progress:
                    print(json.dumps({'performers': performers, 'state': job['state'], 'progress': job['progress']}), flush=True)
                    last_progress = job['progress']
                if job['state'] not in ('queued', 'running'):
                    break
                time.sleep(.15)
            assert job['state'] == 'complete', job
            result = job['result']
            arrangement = result['arrangement']
            assert arrangement['render_origin_sample'] == 11025
            assert all(11025 <= note['start'] < note['end'] <= 275625 for note in arrangement['notes'])
            if performers == 2:
                assert {note['player'] for note in arrangement['notes']} == {1, 2}
                assert all(note['player'] == (1 if note['role'] == 'melody' else 2) for note in arrangement['notes'])
            assert result['asset']['source_id'] == original['source_id']
            assert result['metrics']['clipped_samples'] == 0 and result['metrics']['peak'] > 0
            media = client.get('/api/media/'+result['asset']['id']).content
            assert hashlib.sha256(media).hexdigest() == result['asset']['sha256']
            (output/('solo.wav' if performers == 1 else 'duet.wav')).write_bytes(media)
            retried = client.post('/api/arrange-preview', json=spec).json()
            assert retried['id'] == identity and retried['state'] == 'complete'
            evidence['cases'].append(job)
            (output/'evidence.partial').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        spec = {'asset_id': original['id'], 'start_seconds': .1, 'end_seconds': 6.4, 'performers': 1}
        response = client.post('/api/arrange-preview', json=spec)
        assert response.status_code == 200, response.text
        identity = response.json()['id']
        deadline = time.monotonic()+120
        while time.monotonic() < deadline:
            job = client.get('/api/jobs/'+identity).json()
            assert job['state'] in ('queued', 'running'), job
            if job['progress'] >= .13:
                break
            time.sleep(.05)
        assert job['progress'] >= .13
        began = time.monotonic()
        assert client.post('/api/jobs/'+identity+'/cancel').status_code == 200
        while time.monotonic()-began < 10:
            job = client.get('/api/jobs/'+identity).json()
            if job['state'] not in ('queued', 'running'):
                break
            time.sleep(.05)
        assert job['state'] == 'cancelled', job
        evidence['cancel_seconds'] = time.monotonic()-began
        assert len(client.get('/api/library').json()['items']) == 3
        assert hashlib.sha256(client.get('/api/media/'+original['id']).content).hexdigest() == digest
    assert not list(app.state.library.stage.glob('*.partial'))
    assert not list(app.state.library.stage.glob('*.analysis.json'))
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == digest
    (output/'evidence.partial').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    (output/'evidence.partial').replace(output/'evidence.json')
    print(json.dumps({'complete': True, 'output': str(output), 'cancel_seconds': evidence['cancel_seconds']}), flush=True)


if __name__ == '__main__':
    main()
