"""Read-only guard for one private, pinned local-arranger trial package.

No installation, downloads, native imports or execution. Cheap status is not
attestation. Use protect_runtime across native execution to retain protected
owned-file handles; require_runtime alone is a completed verification snapshot.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile
import zlib

ROOT=Path(__file__).resolve().parent/'research/local-arranger-001'
SYSTEM32=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32'
VERSION='local-arranger-qwen3-4b-b11669-v1'
MODEL={'file':'Qwen3-4B-Q4_K_M.gguf','bytes':2497280256,
       'sha256':'7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5',
       'revision':'bc640142c66e1fdd12af0bd68f40445458f3869b'}
RUNTIME={'file':'llama-b11669-bin-win-cpu-x64.zip','bytes':19537886,
         'sha256':'37b3efcf962847ca869745fd4ccf7f5e7e8dacfeff9fa3523b4ca8cdc66d632b',
         'expanded_bytes':49893149,'members':51,'tag':'b11669',
         'commit':'620d2225cc436ab0b001e3200e590e3b286ef9ab',
         'executable':'llama-completion.exe','tokenize':'llama-tokenize.exe',
         # Local acquisition snapshots are pinned evidence, not publisher
         # attestations. Installed members are also checked against ZIP data.
         'metadata':{
             'frozen.json':(2718,'d1506bae8f629a9d365e069180afa74e7f364998628a13de5bab7b0c8217dfc7'),
             'acquisition.json':(6729,'74efd8f15df725cb7bc8592d3a2c4c33542e0595ffb6654041da862dde0b618d'),
             'runtime-inventory.json':(9210,'fe2ed569b174655d732d5c1301d5708e6dbca3fbb0d82d1ab2c1470e8cf8d4df'),
             'static-dependencies.json':(79945,'1a2e569904e0dcb00899cfabaa62aeb633d1e4cbdf44522969b8e0a57c5521e1'),
             'notices/LICENSE-LLVM-OpenMP':(19741,'fdad1758a9e1f9d5a81e18879b3406772115edc92c24bfa36b70c654f325e8e4'),
             'notices/llama-LICENSE':(1078,'94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d'),
             'notices/llama-zip-inventory.json':(6265,'729db48de36b2aeba525396628d45f7d7b7f52511a6df94e9ca1ff039ecc1216'),
             'notices/metadata-files.json':(3403,'0369624f75df1c8ca87ac7c6f77532c9dc865aaa7051702edac0d397543fce7e'),
             'notices/qwen-LICENSE':(11544,'5de36594c10839788a8c589443a8ef9d8b8d17c65a1b5807206ae037fc36c6bd'),
             'notices/qwen-README.md':(7576,'f7b7cbdbeab2a25b55e92cf160c035287374253b13af7ea2d5178847f6f8537e')}}
MAX_METADATA=1024**2
CHUNK=1024**2


def _check(cancel):
    if cancel():raise InterruptedError('Local arranger verification cancelled')


def _name(value):
    if (type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}',value)
            or value.endswith('.') or value.split('.')[0].upper() in
            {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}):
        raise ValueError('Unsafe package member name')
    return value


def _regular(path,directory=False,owned=True):
    s=path.lstat()
    if (stat.S_ISLNK(s.st_mode) or getattr(s,'st_file_attributes',0)&0x400
            or not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode))
            or (owned and not directory and s.st_nlink!=1)):
        raise ValueError('Package links or nonregular entries are unsupported')
    return s


def _root():
    root=ROOT.absolute()
    for p in reversed((root,*root.parents)):_regular(p,True)
    return root


def _json(data):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate metadata key')
            result[key]=value
        return result
    return json.loads(data.decode('utf-8'),object_pairs_hook=pairs,
                      parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite metadata')))


def _small(path):
    if _regular(path).st_size>MAX_METADATA:raise ValueError('Oversize package metadata')
    with path.open('rb') as stream:return stream.read(MAX_METADATA+1)


def _closure(folder,expected):
    _regular(folder,True);found={}
    for p in folder.iterdir():
        _name(p.name);_regular(p)
        if p.name.casefold() in found:raise ValueError('Case-colliding package entries')
        found[p.name.casefold()]=p.name
    if set(found.values())!=set(expected):raise ValueError('Package directory has missing or extra entries')


def _layout(root,inventory):
    expected={MODEL['file'],RUNTIME['file'],'runtime','notices',
              *(n for n in RUNTIME['metadata'] if '/' not in n)}
    actual=set()
    for p in root.iterdir():
        _name(p.name);_regular(p,p.name in ('runtime','notices'));actual.add(p.name)
    if actual!=expected:raise ValueError('Private package has missing or extra entries')
    _closure(root/'runtime',inventory)
    _closure(root/'notices',[n.split('/')[1] for n in RUNTIME['metadata'] if n.startswith('notices/')])


def _snapshot(read=_small,cancel=lambda:False):
    _check(cancel);root=_root();metadata={};digests={}
    for name,(size,digest) in RUNTIME['metadata'].items():
        _check(cancel)
        parts=name.split('/')
        if len(parts)>2 or (len(parts)==2 and parts[0]!='notices'):raise ValueError('Invalid fixed metadata path')
        for part in parts:_name(part)
        data=read(root/name)
        if len(data)!=size or hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Package metadata pin changed: '+name)
        metadata[name]=data;digests[name]=digest
    frozen=_json(metadata['frozen.json']);receipt=_json(metadata['acquisition.json'])
    if receipt.get('state')!='complete' or receipt.get('native_executed') is not False:raise ValueError('Acquisition is not the completed pinned attempt')
    for section,pins,keys in [('model',MODEL,('file','bytes','sha256','revision')),
                              ('runtime',RUNTIME,('file','bytes','sha256','expanded_bytes','members','tag','commit'))]:
        if any(frozen.get(section,{}).get(k)!=pins[k] for k in keys):raise ValueError('Frozen acquisition identity differs')
    artifacts=receipt.get('artifacts')
    if type(artifacts) is not list or len(artifacts)!=2:raise ValueError('Incomplete acquisition receipt')
    if {a.get('file') for a in artifacts}!={MODEL['file'],RUNTIME['file']}:raise ValueError('Unexpected acquisition artifacts')
    for pins in (MODEL,RUNTIME):
        a=next(a for a in artifacts if a['file']==pins['file'])
        if (a.get('state')!='verified' or a.get('expected_bytes')!=pins['bytes'] or a.get('transferred_bytes')!=pins['bytes']
                or a.get('expected_sha256')!=pins['sha256'] or a.get('sha256')!=pins['sha256']):raise ValueError('Stale acquisition artifact receipt')
        if _regular(root/pins['file']).st_size!=pins['bytes']:raise ValueError('Artifact size differs')
    expected_notices={n.split('/')[1]:{'bytes':s,'sha256':h} for n,(s,h) in RUNTIME['metadata'].items() if n.startswith('notices/')}
    if receipt.get('notices')!=expected_notices:raise ValueError('Required notice evidence differs')
    rows=_json(metadata['runtime-inventory.json']);inventory={};lower=set()
    if type(rows) is not list or len(rows)!=RUNTIME['members']:raise ValueError('Runtime inventory count differs')
    for row in rows:
        if type(row) is not dict or set(row)!={'path','bytes','sha256','zip_crc32'}:raise ValueError('Invalid runtime inventory')
        name=_name(row['path'])
        if name.casefold() in lower:raise ValueError('Case-colliding inventory names')
        lower.add(name.casefold())
        if (type(row['bytes']) is not int or not 0<row['bytes']<=RUNTIME['expanded_bytes']
                or not re.fullmatch('[0-9a-f]{64}',row['sha256']) or not re.fullmatch('[0-9a-f]{8}',row['zip_crc32'])):raise ValueError('Invalid inventory measurement')
        inventory[name]=row
    if sum(r['bytes'] for r in rows)!=RUNTIME['expanded_bytes']:raise ValueError('Expanded runtime size differs')
    if not {RUNTIME['executable'],RUNTIME['tokenize']}<=inventory.keys():raise ValueError('Fixed native entry points missing')
    _layout(root,inventory)
    for name,row in inventory.items():
        if _regular(root/'runtime'/name).st_size!=row['bytes']:raise ValueError('Installed runtime member size differs')
    deps=_json(metadata['static-dependencies.json'])
    systems=_dependencies(root,inventory,deps)
    return root,inventory,systems,digests


def _dependencies(root,inventory,deps):
    if (type(deps) is not dict or set(deps)!={'binaries','imports','windows_api_set_contracts','unresolved','limitations'}
            or deps['unresolved']!=[] or type(deps['limitations']) is not str or not deps['limitations']):raise ValueError('Unresolved or invalid static dependencies')
    binaries=deps['binaries'];edges=deps['imports'];contracts=deps['windows_api_set_contracts']
    if type(binaries) is not list or not 1<=len(binaries)<=400 or type(edges) is not dict or type(contracts) is not list:raise ValueError('Invalid static dependency bounds')
    paths={};local={};system={}
    for b in binaries:
        if type(b) is not dict or set(b)!={'path','bytes','sha256'}:raise ValueError('Invalid dependency record')
        p=Path(b['path'])
        if not p.is_absolute() or _name(p.name)!=p.name:raise ValueError('Invalid dependency path')
        key=str(p).casefold()
        if key in paths or type(b['bytes']) is not int or b['bytes']<=0 or not re.fullmatch('[0-9a-f]{64}',b['sha256']):raise ValueError('Invalid dependency identity')
        paths[key]=b
        if p.parent==root/'runtime':
            item=inventory.get(p.name)
            if item is None or any(item[k]!=b[k] for k in ('bytes','sha256')):raise ValueError('Local dependency differs from ZIP inventory')
            local[p.name.casefold()]=b
        elif p.parent==SYSTEM32.absolute():
            if _regular(p,owned=False).st_size!=b['bytes']:raise ValueError('Observed System32 dependency changed')
            system[p.name.casefold()]=b
        else:raise ValueError('Dependency lies outside fixed runtime and System32')
    native={n.casefold() for n in inventory if Path(n).suffix.lower() in ('.exe','.dll')}
    if set(local)!=native or {p.casefold() for p in edges}!=set(paths):raise ValueError('Static normal import closure incomplete')
    contracts_lower=set()
    for name in contracts:
        _name(name)
        if not name.lower().startswith(('api-ms-','ext-ms-')) or name.lower() in contracts_lower:raise ValueError('Invalid Windows API-set contract')
        contracts_lower.add(name.lower())
    observed=set()
    for names in edges.values():
        if type(names) is not list or len(names)>1000:raise ValueError('Invalid static import list')
        for name in names:
            _name(name);key=name.lower()
            if key.startswith(('api-ms-','ext-ms-')):observed.add(key)
            elif key not in local and key not in system:raise ValueError('Static import has no observed resolution')
    if observed!=contracts_lower:raise ValueError('API-set evidence differs from normal imports')
    return list(system.values())


def _receipt(root,inventory,systems,digests):
    identity={'model':MODEL,'runtime':RUNTIME,'manifest_sha256':digests}
    return {'ready':True,'reason':None,'version':VERSION,'runtime_version':VERSION,
            'model':str(root/MODEL['file']),'executable':str(root/'runtime'/RUNTIME['executable']),
            'tokenize':str(root/'runtime'/RUNTIME['tokenize']),'runtime_root':str(root/'runtime'),
            'identity_sha256':hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'manifest_sha256':dict(digests),'model_sha256':MODEL['sha256'],'runtime_archive_sha256':RUNTIME['sha256'],
            'native_inputs':[{'path':str(root/MODEL['file']),'bytes':MODEL['bytes'],'sha256':MODEL['sha256']},
                *[{'path':str(root/'runtime'/name),'bytes':r['bytes'],'sha256':r['sha256']} for name,r in sorted(inventory.items())]],
            'observed_system_dependencies':systems,'full_verified':False,'protection_active':False,
            'limits':'Static normal import closure only; no delay-load/dynamic-load, CPU compatibility, native execution or musical-quality claim. System32 files are never locked. Directory closure is checked, not protected by an ACL.'}


def runtime_status():
    result={'ready':False,'reason':'Private local-arranger trial package unavailable','version':VERSION,'runtime_version':VERSION,
            'model':str(ROOT.absolute()/MODEL['file']),'executable':str(ROOT.absolute()/'runtime'/RUNTIME['executable']),
            'tokenize':str(ROOT.absolute()/'runtime'/RUNTIME['tokenize']),'runtime_root':str(ROOT.absolute()/'runtime'),
            'full_verified':False,'protection_active':False}
    try:result=_receipt(*_snapshot())
    except (OSError,ValueError,KeyError,TypeError,StopIteration) as exc:result['reason']=str(exc)
    return result


def _digest(stream,cancel):
    stream.seek(0);h=hashlib.sha256();crc=0;count=0
    while True:
        _check(cancel);data=stream.read(CHUNK)
        if not data:break
        h.update(data);crc=zlib.crc32(data,crc);count+=len(data)
    return count,h.hexdigest(),f'{crc:08x}'


@contextmanager
def protect_runtime(cancel=lambda:False):
    """Fully verify then hold every owned package file read-share-only.

    Caller must keep this context open through native child termination. No
    handle is returned in the receipt. This cannot prohibit new directory
    entries; closure is checked before and after the protected interval.
    """
    from .full_decode import _protected_reader
    _check(cancel);root=_root()
    with ExitStack() as stack:
        handles={}
        def owned(path):
            _check(cancel)
            if path not in handles:
                before=_regular(path);h=stack.enter_context(_protected_reader(path));after=os.fstat(h.fileno())
                if (before.st_dev,before.st_ino,before.st_size)!=(after.st_dev,after.st_ino,after.st_size):raise ValueError('Owned file changed while opening')
                handles[path]=h
            return handles[path]
        def read(path):
            h=owned(path)
            if os.fstat(h.fileno()).st_size>MAX_METADATA:raise ValueError('Oversize package metadata')
            h.seek(0);return h.read(MAX_METADATA+1)
        root,inventory,systems,digests=_snapshot(read,cancel)
        for name in (MODEL['file'],RUNTIME['file']):owned(root/name)
        for name in inventory:owned(root/'runtime'/name)
        _layout(root,inventory)
        for pins in (MODEL,RUNTIME):
            count,digest,_=_digest(owned(root/pins['file']),cancel)
            if (count,digest)!=(pins['bytes'],pins['sha256']):raise ValueError('Pinned model or runtime archive failed verification')
        archive_handle=owned(root/RUNTIME['file']);archive_handle.seek(0)
        with zipfile.ZipFile(archive_handle) as archive:
            entries=archive.infolist();names=[]
            if len(entries)!=RUNTIME['members'] or sum(i.file_size for i in entries)!=RUNTIME['expanded_bytes']:raise ValueError('ZIP geometry differs')
            for info in entries:
                _check(cancel);name=_name(info.filename);names.append(name)
                mode=info.external_attr>>16
                if (info.is_dir() or info.flag_bits&1 or info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED)
                        or info.external_attr&0x10 or (mode and stat.S_IFMT(mode) not in (0,stat.S_IFREG))):raise ValueError('Unsupported ZIP member type')
                row=inventory.get(name)
                if row is None or row['bytes']!=info.file_size or row['zip_crc32']!=f'{info.CRC:08x}':raise ValueError('ZIP member and inventory differ')
                with archive.open(info) as original:
                    # ZipExtFile is sequential; its complete read also checks CRC.
                    h=hashlib.sha256();crc=0;count=0
                    while True:
                        _check(cancel);data=original.read(CHUNK)
                        if not data:break
                        count+=len(data);h.update(data);crc=zlib.crc32(data,crc)
                        if count>row['bytes']:raise ValueError('ZIP member exceeds declared bound')
                expected=(row['bytes'],row['sha256'],row['zip_crc32'])
                if (count,h.hexdigest(),f'{crc:08x}')!=expected or _digest(owned(root/'runtime'/name),cancel)!=expected:raise ValueError('Installed member differs from authenticated ZIP bytes')
            if len({n.casefold() for n in names})!=len(names) or set(names)!=set(inventory):raise ValueError('ZIP member identity collision')
        # Read only: never acquire protected/share-restricting system handles.
        for item in systems:
            _check(cancel);p=Path(item['path']);before=_regular(p,owned=False)
            with p.open('rb') as stream:count,digest,_=_digest(stream,cancel)
            after=_regular(p,owned=False)
            if (count,digest)!=(item['bytes'],item['sha256']) or (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('Observed static System32 dependency changed')
        _check(cancel);_layout(root,inventory)
        receipt=_receipt(root,inventory,systems,digests)
        receipt.update(full_verified=True,protection_active=True,
                       owned_file_protection='Windows GENERIC_READ / FILE_SHARE_READ, held until context exit; no System32 locks')
        try:yield receipt
        finally:_layout(root,inventory)


def require_runtime(cancel=lambda:False):
    with protect_runtime(cancel=cancel) as receipt:
        result=dict(receipt);result['protection_active']=False
    return result
