"""Resumable immutable snapshot copy between accessible folders.

This is the transfer integrity mechanism, not network discovery or Syncthing
installation. No live database, credentials or private keys are copied.
"""
import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
from snapshots import validate, digest, flush_file
from library import RESERVE

BLOCK=1024*1024

class TransferBusyError(RuntimeError):
    """Another process owns this destination; no copy was started."""

@contextmanager
def destination_ownership(destination):
    """Destination-wide, OS-released ownership; never put a lock in the snapshot.

    Keep the stable sibling lock file after release: unlinking it could let a
    third caller lock a different inode while a second caller holds the old one.
    Its open handle is outside incoming, so Windows can rename the generation.
    """
    destination.parent.mkdir(parents=True,exist_ok=True)
    lock_path=destination.with_name(destination.name+'.transfer.lock')
    with lock_path.open('a+b') as handle:
        if os.fstat(handle.fileno()).st_size==0:
            handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):
                raise TransferBusyError('Another transfer is using this destination. Wait for it to finish or stop it before resuming.') from error
            raise
        # The enclosing with closes/releases this handle on return or failure.
        yield

def same_prefix(source,partial):
    with source.open('rb') as src,partial.open('rb') as dst:
        while True:
            piece=dst.read(BLOCK)
            if not piece:return True
            if src.read(len(piece))!=piece:return False

def transfer_snapshot(source,destination,cancel=lambda:False,progress=lambda n,total:None):
    source=Path(source).resolve();destination=Path(destination).resolve()
    # Different generations targeting the same published folder also serialize.
    with destination_ownership(destination):
        return _transfer_owned(source,destination,cancel,progress)

def _transfer_owned(source,destination,cancel,progress):
    manifest=validate(source)
    # A generation ID selects an incoming directory but never supplies a path.
    identity=hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest()
    incoming=destination.with_name(destination.name+'.incoming-'+identity[:16])
    if destination.exists():
        existing=validate(destination)
        if existing==manifest:return {'already_complete':True,'copied_bytes':0,'resumed_bytes':0}
        raise FileExistsError('Destination contains a different snapshot; choose a new folder.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    incoming.mkdir(exist_ok=True);(incoming/'media').mkdir(exist_ok=True)
    blobs={row['sha256']:row['size'] for row in manifest['assets']}
    total=sum(blobs.values());done=0;copied=0;resumed=0
    for name,size in blobs.items():
        if cancel():raise InterruptedError('Transfer paused; verified progress can resume.')
        src=source/'media'/name;target=incoming/'media'/name;partial=target.with_suffix('.part')
        if target.exists():
            if target.stat().st_size==size and digest(target)==name:
                done+=size;resumed+=size;progress(done,total);continue
            raise ValueError('An incoming completed blob is damaged; no generation was published.')
        offset=partial.stat().st_size if partial.exists() else 0
        if offset>size or (offset and not same_prefix(src,partial)):
            raise ValueError('Partial transfer differs from the source; preserved for inspection.')
        if shutil.disk_usage(incoming).free-(total-done-offset)<RESERVE:
            raise ValueError('Transfer would use the 8 GB reserve; prior copies remain usable.')
        resumed+=offset;done+=offset
        with src.open('rb') as reader,partial.open('ab') as writer:
            reader.seek(offset)
            while chunk:=reader.read(BLOCK):
                if cancel():raise InterruptedError('Transfer paused; partial bytes are not playable.')
                writer.write(chunk);writer.flush();os.fsync(writer.fileno())
                copied+=len(chunk);done+=len(chunk);progress(done,total)
            writer.flush();os.fsync(writer.fileno())
        if partial.stat().st_size!=size or digest(partial)!=name:raise ValueError('Copied media failed verification.')
        os.replace(partial,target)
    if cancel():raise InterruptedError('Transfer paused before publication.')
    (incoming/'manifest.json').write_bytes((source/'manifest.json').read_bytes())
    flush_file(incoming/'manifest.json')
    validate(incoming)
    os.rename(incoming,destination)
    return {'already_complete':False,'copied_bytes':copied,'resumed_bytes':resumed,'total_bytes':total}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source');parser.add_argument('destination')
    args=parser.parse_args()
    try:
        print(json.dumps(transfer_snapshot(args.source,args.destination)))
    except TransferBusyError as error:
        print(json.dumps({'status':'busy','detail':str(error)}))
        raise SystemExit(2)
