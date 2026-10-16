"""Portable immutable media/catalog snapshots; not a live database copy.

Snapshots omit jobs, browser state and protected service identity. They are not
yet the full product recovery bundle. Restore creates a new directory only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from library import Library, RESERVE

def digest(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def flush_file(path):
    with Path(path).open('r+b') as f: os.fsync(f.fileno())

def export_snapshot(library, destination):
    destination=Path(destination).resolve()
    if destination.exists(): raise FileExistsError('Choose a new snapshot directory.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    items=library.items()
    required=sum({x['sha256']:x['size'] for x in items}.values())
    if shutil.disk_usage(destination.parent).free-required<RESERVE: raise ValueError('Snapshot would use the 8 GB reserve.')
    staging=destination.with_name(destination.name+'.partial-'+uuid.uuid4().hex)
    staging.mkdir();(staging/'media').mkdir()
    try:
        for row in items:
            name=row['sha256'];source=library.media/name;target=staging/'media'/name
            if not target.exists(): shutil.copyfile(source,target)
            if target.stat().st_size!=row['size'] or digest(target)!=name: raise ValueError('Source media integrity failed; existing library preserved.')
            flush_file(target)
        manifest={'format':'wood-string-snapshot','version':1,'generation':str(uuid.uuid4()),'assets':items,
                  'scope':'Media and laptop asset relationships only. No playlists, browser state, jobs or private keys.'}
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        flush_file(staging/'manifest.json')
        # Publication of directory happens only after all copied media verify.
        os.rename(staging,destination)
    except BaseException:
        # Only this newly created, owned partial directory is removed.
        shutil.rmtree(staging)
        raise
    return manifest

def validate(snapshot):
    snapshot=Path(snapshot).resolve()
    manifest_path=snapshot/'manifest.json'
    if manifest_path.stat().st_size>16*1024**2: raise ValueError('Manifest exceeds preview limit.')
    data=json.loads(manifest_path.read_text(encoding='utf-8'))
    if data.get('format')!='wood-string-snapshot' or data.get('version')!=1 or not isinstance(data.get('assets'),list):
        raise ValueError('Unsupported snapshot.')
    seen=set()
    keys={'id','source_id','title','filename','sha256','size','mime','kind','created_at'}
    for row in data['assets']:
        if not isinstance(row,dict) or set(row)!=keys: raise ValueError('Invalid asset record.')
        if not isinstance(row['id'],str) or not row['id'] or row['id'] in seen: raise ValueError('Duplicate or missing asset identity.')
        seen.add(row['id'])
        name=row['sha256']
        if not isinstance(name,str) or len(name)!=64 or any(c not in '0123456789abcdef' for c in name): raise ValueError('Invalid media hash.')
        if type(row['size']) is not int or row['size']<1: raise ValueError('Invalid media length.')
        for key in ('source_id','title','filename','mime','kind'):
            if not isinstance(row[key],str): raise ValueError('Invalid text metadata.')
        path=snapshot/'media'/name
        if path.is_symlink() or path.resolve().parent!=(snapshot/'media').resolve(): raise ValueError('External media references rejected.')
        if path.stat().st_size!=row['size'] or digest(path)!=name: raise ValueError('Snapshot media is incomplete or corrupted.')
    return data

def restore_snapshot(snapshot, destination):
    destination=Path(destination).resolve()
    if destination.exists(): raise FileExistsError('Restore only into a new directory; existing libraries stay untouched.')
    data=validate(snapshot)
    destination.parent.mkdir(parents=True,exist_ok=True)
    required=sum({x['sha256']:x['size'] for x in data['assets']}.values())
    if shutil.disk_usage(destination.parent).free-required<RESERVE: raise ValueError('Restore would use the 8 GB reserve.')
    staging=destination.with_name(destination.name+'.partial-'+uuid.uuid4().hex)
    try:
        library=Library(staging)
        for row in data['assets']:
            target=library.media/row['sha256']
            if not target.exists(): shutil.copyfile(Path(snapshot)/'media'/row['sha256'],target)
            if digest(target)!=row['sha256']: raise ValueError('Copied media verification failed.')
            flush_file(target)
        with library.connect() as db:
            db.executemany('INSERT INTO assets VALUES (:id,:source_id,:title,:filename,:sha256,:size,:mime,:kind,:created_at)',data['assets'])
        flush_file(library.db)
        os.rename(staging,destination)
    except BaseException:
        if staging.exists(): shutil.rmtree(staging)
        raise
    return data

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['export','verify','restore'])
    parser.add_argument('source');parser.add_argument('destination',nargs='?')
    args=parser.parse_args()
    if args.action=='verify': result=validate(args.source)
    elif not args.destination: parser.error('Destination required.')
    elif args.action=='export': result=export_snapshot(Library(args.source),args.destination)
    else: result=restore_snapshot(args.source,args.destination)
    print(json.dumps({'generation':result['generation'],'assets':len(result['assets']),'scope':result['scope']}))
