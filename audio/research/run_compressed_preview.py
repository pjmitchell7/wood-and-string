"""Silent real-fixture import/decode/analysis/render comparison through the API."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from fastapi.testclient import TestClient
from server import create_app
from audio.analysis_runtime import runtime_status

ROOT = Path(__file__).resolve().parent


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-name', default='compressed-preview-001')
    name = parser.parse_args().output_name
    if not re.fullmatch(r'compressed-preview-[0-9]{3}', name):
        raise ValueError('Choose a fresh compressed-preview-NNN directory')
    used = sum(p.stat().st_size for tree in (ROOT, ROOT.parent/'vendor/analysis')
               for p in tree.rglob('*') if p.is_file())
    if used + 100*1024**2 > 4*1024**3 or shutil.disk_usage(ROOT).free < 8*1024**3+100*1024**2:
        raise RuntimeError('Research budget/reserve would be exceeded')
    out = ROOT/name; out.mkdir(exist_ok=False)
    original = ROOT/'mixed/mixed.wav'; original_hash = digest(original)
    ffmpeg = ROOT/'decoder/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
    inputs = [('wav', original, 1)]
    for extension, codec, performers in [('flac', 'flac', 1), ('mp3', 'libmp3lame', 2)]:
        path = out/('fixture.'+extension)
        subprocess.run([str(ffmpeg), '-v', 'error', '-i', str(original), '-c:a', codec, str(path)],
                       check=True, timeout=30, capture_output=True)
        inputs.append((extension, path, performers))
    evidence = {'scope': 'Operational format integration; training fixture, no musical acceptance',
                'source_fixture_sha256': original_hash, 'runtime_version': runtime_status()['runtime_version'],
                'cases': [], 'played_audio': False}
    app = create_app(out/'library')
    def save():
        (out/'evidence.partial').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    def wait(client, identity):
        began = time.monotonic(); previous = None
        while time.monotonic()-began < 245:
            job = client.get('/api/jobs/'+identity).json()
            if (job['state'], job['progress']) != previous:
                previous = job['state'], job['progress']
                print(json.dumps({'id':identity, 'state':job['state'], 'progress':job['progress']}), flush=True)
            if job['state'] not in ('queued','running'):
                return job
            time.sleep(.2)
        raise TimeoutError('Observed job remains unfinished; inspect the same job before retrying')
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.get('/session').status_code == 200
        for extension, path, performers in inputs:
            data = path.read_bytes(); expected = hashlib.sha256(data).hexdigest()
            response = client.post('/api/import', content=data, headers={'X-Filename':'Private fixture.'+extension})
            assert response.status_code == 200, response.text
            asset = response.json()
            spec = {'asset_id':asset['id'], 'start_seconds':.25, 'end_seconds':6.25, 'performers':performers}
            response = client.post('/api/arrange-preview', json=spec)
            assert response.status_code == 200, response.text
            job = wait(client, response.json()['id'])
            evidence['cases'].append({'format':extension,'input_sha256':expected,'job':job}); save()
            assert job['state'] == 'complete', job
            result = job['result']; info = result['analysis']['timebase']
            assert info['decoded'] == (extension != 'wav')
            assert (info['origin_sample'], info['end_sample']) == (11025,275625)
            if info['decoded']:
                assert info['decoder']['original_sha256'] == expected
            assert result['arrangement']['render_origin_sample'] == 11025
            assert all(11025 <= n['start'] < n['end'] <= 275625 for n in result['arrangement']['notes'])
            assert result['asset']['source_id'] == asset['source_id']
            assert result['metrics']['clipped_samples'] == 0 and result['metrics']['peak'] > 0
            rendered = client.get('/api/media/'+result['asset']['id']).content
            assert hashlib.sha256(rendered).hexdigest() == result['asset']['sha256']
            assert hashlib.sha256(client.get('/api/media/'+asset['id']).content).hexdigest() == expected
            assert client.post('/api/arrange-preview',json=spec).json()['id'] == job['id']
            if performers == 2:
                assert {n['player'] for n in result['arrangement']['notes']} == {1,2}
        # The lossless encoded input must retain the exact downstream evidence.
        wav, flac = [case['job']['result'] for case in evidence['cases'][:2]]
        assert wav['arrangement']['notes'] == flac['arrangement']['notes']
        assert wav['asset']['sha256'] == flac['asset']['sha256']
        evidence['lossless_output_identical'] = True
        bad = client.post('/api/import',content=b'invalid local audio',headers={'X-Filename':'Broken.mp3'}).json()
        response = client.post('/api/arrange-preview',json={'asset_id':bad['id'],'start_seconds':0,'end_seconds':1,'performers':1})
        assert response.status_code == 200, response.text
        failed = wait(client,response.json()['id']); evidence['malformed_case'] = failed; save()
        assert failed['state'] == 'failed'
        assert client.get('/api/media/'+bad['id']).content == b'invalid local audio'
    assert not list(app.state.library.stage.iterdir())
    assert digest(original) == original_hash
    save(); (out/'evidence.partial').replace(out/'evidence.json')
    print(json.dumps({'completed':True,'output':str(out),'lossless_output_identical':True}),flush=True)


if __name__ == '__main__': main()
