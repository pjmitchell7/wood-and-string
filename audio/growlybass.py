"""Research-only verified Growlybass clean sustain mapping; no mode activation."""
import hashlib
from pathlib import Path
import re

POLICY='growlybass-clean-first-rr-v1'
MAP_SHA='a912e7e142841e68f04fed16415f9294e5f8e8566cf6a442f9cd636deeeced25'
ALLOWED={'lokey','hikey','pitch_keycenter','ampeg_release','sample','lovel','hivel',
         'lorand','hirand','amp_velcurve_60','amp_velcurve_95','amp_velcurve_120'}

def clean_sustain_map(bank):
    """Preserve original keyboard centers; sounding-pitch offset is NOT assumed.

    Explicit RR1 only. All random selection opcodes are removed. No scrape,
    dirty release sample, staccato, fingering, or production readiness claim.
    """
    bank=Path(bank);raw=(bank/'growlybass_clean.sfz').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=MAP_SHA:raise ValueError('Unverified Growlybass clean map')
    text=raw.decode();opcodes=set(re.findall(r'(\w+)\s*=',text))
    if opcodes-ALLOWED:raise ValueError('Unexpected Growlybass opcode')
    output=[];inventory=[]
    for group in text.split('<group>')[1:]:
        head,*regions=group.split('<region>')
        selected=[]
        for region in regions:
            match=re.search(r'sample=([^\r\n]+)',region)
            if not match:raise ValueError('Missing sample')
            name=match.group(1).replace('\\','/')
            if not name.startswith('sustain/'):continue
            if not name.endswith('_rr1.wav'):continue
            path=(bank/name).resolve()
            if not path.is_relative_to(bank.resolve()) or not path.is_file():raise ValueError('Missing or invalid sample')
            region=re.sub(r'^(?:lo|hi)rand=.*\n?','',region,flags=re.M)
            region=re.sub(r'sample=[^\r\n]+','sample='+name,region)
            selected.append(region)
            with path.open('rb') as f:checksum=hashlib.file_digest(f,'sha256').hexdigest()
            inventory.append({'path':name,'sha256':checksum})
        if selected:output.append('<group>'+head+''.join('<region>'+r for r in selected))
    if len(inventory)!=56:raise ValueError('Unexpected sustain coverage')
    return '\n'.join(output),inventory


# Separate experimental playback profile. The frozen research helper above is
# deliberately unchanged; this profile does not admit the flagged ff layer.
PROFILE_VERSION='growlybass-moderate-rr1-v2'
ROOT=Path(__file__).resolve().parent
ACQUISITION_SHA='02140ed45b8a789e28f8d064caa57e27eed41647dfedcc3db7fc83298340d95a'
PROBE_SHA='23854c9032d882ec7911591cc9718bbf5b2df6f7c3e2a54caad012e7a6c52d96'

def _read_pinned(path,digest):
    import json
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Bass provenance manifest changed')
    return json.loads(raw)

def _configuration(root=ROOT):
    root=Path(root);research=root/'research/growlybass'
    acquired=_read_pinned(research/'acquisition.json',ACQUISITION_SHA)
    probe=_read_pinned(research/'probe-002/frozen.json',PROBE_SHA)
    engine=next(x for x in probe['native']['binaries'] if Path(x['path']).name.lower()=='sfizz.dll')
    return {'root':root,'bank':research/'bank/Growlybass','members':acquired['inventory'],
            'engine':root/'vendor/sfizz/bin/Release/sfizz.dll','engine_sha256':engine['sha256'],
            'profile_version':PROFILE_VERSION,'probe':probe}

def bank_status(root=ROOT):
    """Cheap availability, not an integrity attestation; worker must require_bank."""
    try:
        config=_configuration(root)
        if not config['engine'].is_file() or not (config['bank']/'growlybass_clean.sfz').is_file():raise ValueError('Bass bank or engine missing')
        for entry in config['members']:
            path=(config['bank'].parent/entry['path']).resolve()
            if not path.is_relative_to(config['bank'].parent.resolve()) or path.stat().st_size!=entry['bytes']:raise ValueError('Bass bank files are missing or incomplete')
        return {'ready':True,'reason':None,'profile_version':PROFILE_VERSION,'integrity_checked':False}
    except (OSError,ValueError,KeyError,StopIteration) as exc:
        return {'ready':False,'reason':str(exc),'profile_version':PROFILE_VERSION,'integrity_checked':False}

def require_bank(cancel=lambda:False,root=ROOT):
    """Verify every installed archive member and engine before native loading.

    Uses the already acquired research bank in place, without copying samples.
    The native probe pins engine dependencies; verify concrete PE import closure.
    Windows virtual API contracts are not files and remain OS-version dependent.
    """
    config=_configuration(root);base=config['bank'].parent.resolve()
    def check(path,digest,size=None):
        if cancel():raise InterruptedError('Bass verification cancelled')
        if size is not None and path.stat().st_size!=size:raise ValueError('Bass asset size changed')
        h=hashlib.sha256()
        with path.open('rb') as stream:
            while data:=stream.read(1024*1024):
                if cancel():raise InterruptedError('Bass verification cancelled')
                h.update(data)
        if h.hexdigest()!=digest:raise ValueError('Bass asset integrity verification failed: '+path.name)
    for entry in config['members']:
        path=(base/entry['path']).resolve()
        if not path.is_relative_to(base):raise ValueError('Invalid bass inventory path')
        check(path,entry['sha256'],entry['bytes'])
    check(config['engine'],config['engine_sha256'])
    native=config['probe']['native'];byname={Path(x['path']).name.lower():x for x in native['binaries']}
    imports={Path(k).name.lower():v for k,v in native['imports'].items()};pending=['sfizz.dll'];seen=set()
    while pending:
        name=pending.pop().lower()
        if name in seen or name.startswith(('api-ms-','ext-ms-')):continue
        seen.add(name);entry=byname[name]
        if name!='sfizz.dll':check(Path(entry['path']),entry['sha256'],entry['bytes'])
        pending.extend(imports[name])
    return {**config,'integrity_checked':True}

def moderate_map(bank):
    """RR1 sustained layers 1..120; renderer owns explicit string damping."""
    original,_=clean_sustain_map(bank);groups=[];count=0
    for group in original.split('<group>')[1:]:
        head,*regions=group.split('<region>');kept=[]
        for region in regions:
            if re.search(r'sample=[^\r\n]*_ff_rr1\.wav',region):continue
            if not re.search(r'hivel=(60|95|120)\b',region):raise ValueError('Unbounded moderate layer')
            kept.append(region);count+=1
        groups.append('<group>'+head+''.join('<region>'+r for r in kept))
    if count!=42:raise ValueError('Unexpected moderate region coverage')
    return '\n'.join(groups)
