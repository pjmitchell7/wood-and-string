"""Explicit bounded acquisition only; no engine loading or audio execution."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import time
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'audio/research/growlybass'
URL='https://github.com/sfzinstruments/karoryfer.growlybass/releases/download/v1.002/Karoryfer.Growlybass.v1.002.zip'
SIZE=167454256
DOWNLOAD_CAP=170*1024**2
EXPANDED_CAP=210*1024**2
OTHER_RESERVE=(20+32)*1024**2
API='https://api.github.com/repos/sfzinstruments/karoryfer.growlybass/'

def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def tree_size(path):
    total=0
    for p in path.rglob('*'):
        try:
            if p.is_file():total+=p.stat().st_size
        except FileNotFoundError:pass
    return total

def budget(extra):
    used=tree_size(ROOT/'audio/research')+tree_size(ROOT/'audio/vendor/analysis')
    free=shutil.disk_usage(ROOT).free
    if used+extra+OTHER_RESERVE>=4*1024**3:raise RuntimeError('Research budget exceeded')
    if free<8*1024**3+extra+OTHER_RESERVE:raise RuntimeError('Disk reserve would be crossed')
    return {'research_bytes':used,'disk_free_bytes':free,'other_reserved_bytes':OTHER_RESERVE,'additional_bytes':extra}

def main():
    before=budget(SIZE+EXPANDED_CAP)
    OUT.mkdir(exist_ok=False)
    redirects=[]
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            redirects.append({'from':req.full_url,'status':code,'to':newurl})
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    opener=urllib.request.build_opener(Redirect())
    def get(url):return opener.open(urllib.request.Request(url,headers={'User-Agent':'Private-local-instrument-setup'}),timeout=45)
    with get(API+'releases/tags/v1.002') as response:release=json.load(response)
    asset=next(a for a in release['assets'] if a['browser_download_url']==URL)
    assert asset['size']==SIZE
    with get('https://raw.githubusercontent.com/sfzinstruments/karoryfer.growlybass/v1.002/LICENSE') as response:license_bytes=response.read(100000)
    assert b'CC0 1.0 Universal' in license_bytes
    with get(API+'git/ref/tags/v1.002') as response:tag=json.load(response)
    frozen={'url':URL,'release_id':release['id'],'tag':'v1.002','tag_object':tag['object'],
            'asset_id':asset['id'],'expected_bytes':SIZE,'publisher_digest':asset.get('digest'),
            'license':'CC0-1.0','license_sha256':hashlib.sha256(license_bytes).hexdigest(),
            'download_cap_bytes':DOWNLOAD_CAP,'expanded_cap_bytes':EXPANDED_CAP,'budget_before':before,
            'created_unix':time.time()}
    (OUT/'frozen.json').write_text(json.dumps(frozen,indent=2),encoding='utf-8')
    (OUT/'LICENSE.source').write_bytes(license_bytes)
    partial=OUT/'Growlybass.zip.partial';archive=OUT/'Growlybass.zip'
    total=0;began=time.monotonic()
    with get(URL) as response,partial.open('xb') as output:
        final_url=response.url
        announced=response.headers.get('Content-Length')
        if announced is not None and int(announced)!=SIZE:raise RuntimeError('Unexpected Content-Length')
        while data:=response.read(1024*1024):
            total+=len(data)
            if total>min(SIZE,DOWNLOAD_CAP):raise RuntimeError('Download cap exceeded')
            if time.monotonic()-began>240:raise TimeoutError('Download deadline')
            output.write(data)
    assert total==SIZE
    archive_hash=sha(partial)
    if asset.get('digest'):assert asset['digest']=='sha256:'+archive_hash
    partial.replace(archive)
    receipt={'archive_sha256':archive_hash,'archive_bytes':total,'redirects':redirects,'final_url':final_url,
             'download_seconds':time.monotonic()-began,'state':'downloaded'}
    (OUT/'acquisition.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    with zipfile.ZipFile(archive) as z:
        entries=z.infolist();expanded=sum(i.file_size for i in entries)
        if expanded>EXPANDED_CAP:raise RuntimeError('Expanded size cap exceeded')
        names=set()
        for item in entries:
            path=PurePosixPath(item.filename)
            if '\\' in item.filename or path.is_absolute() or any(p in ('..','.') or ':' in p or p.rstrip(' .')!=p for p in path.parts):raise ValueError('Unsafe ZIP path')
            if any(p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))} for p in path.parts):raise ValueError('Reserved ZIP path')
            if item.filename.casefold() in names:raise ValueError('Duplicate ZIP path')
            names.add(item.filename.casefold())
            if stat.S_ISLNK(item.external_attr>>16) or item.flag_bits&1:raise ValueError('Link/encrypted member unsupported')
        budget(expanded)
        destination=OUT/'bank';destination.mkdir()
        inventory=[];actual=0
        for item in entries:
            path=destination.joinpath(*PurePosixPath(item.filename).parts)
            if not path.resolve().is_relative_to(destination.resolve()):raise ValueError('Escaped destination')
            if item.is_dir():path.mkdir(parents=True,exist_ok=True);continue
            path.parent.mkdir(parents=True,exist_ok=True);member_hash=hashlib.sha256();count=0
            with z.open(item) as source,path.open('xb') as output:
                while data:=source.read(1024*1024):
                    count+=len(data);actual+=len(data)
                    if count>item.file_size or actual>EXPANDED_CAP:raise RuntimeError('Extraction cap exceeded')
                    output.write(data);member_hash.update(data)
            assert count==item.file_size and sha(path)==member_hash.hexdigest()
            inventory.append({'path':item.filename,'bytes':count,'sha256':member_hash.hexdigest()})
    receipt.update(state='verified',expanded_bytes=actual,files=len(inventory),inventory=inventory,budget_after=budget(0))
    (OUT/'acquisition.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k not in ('inventory','redirects','final_url')}),flush=True)

if __name__=='__main__':main()
