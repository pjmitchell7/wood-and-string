"""Hash-addressed non-playable analysis evidence, separate from music assets."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

MAX_BYTES=16*1024**2

def digest(path,cancel=lambda:False):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while data:=stream.read(1024**2):
            if cancel():raise InterruptedError('Analysis cancelled')
            h.update(data)
    return h.hexdigest()

def validate_reference(value):
    if (type(value) is not dict or set(value)!={'sha256','bytes','format'}
        or type(value['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',value['sha256'])
        or type(value['bytes']) is not int or not 0<value['bytes']<=MAX_BYTES
        or value['format']!='numpy-npz-v1'):
        raise ValueError('Invalid saved analysis reference')
    return value

class AnalysisEvidence:
    def __init__(self,library):
        self.library=library;self.root=library.root/'analysis-evidence'

    def verify(self,reference,cancel=lambda:False):
        validate_reference(reference)
        path=self.root/reference['sha256']
        if (self.root.is_symlink() or path.is_symlink() or not path.is_file()
            or path.stat().st_size!=reference['bytes'] or digest(path,cancel)!=reference['sha256']):
            raise ValueError('Saved instrumental evidence failed verification. Inspect this passage again.')
        return path

    def publish(self,staged,reference,cancel=lambda:False):
        validate_reference(reference);staged=Path(staged)
        if (staged.is_symlink() or not staged.resolve().is_relative_to(self.library.stage.resolve())
            or not staged.is_file() or staged.stat().st_size!=reference['bytes']
            or digest(staged,cancel)!=reference['sha256']):
            raise ValueError('Instrumental evidence receipt failed verification')
        self.library.space(reference['bytes']);self.root.mkdir(exist_ok=True)
        if self.root.is_symlink():raise ValueError('Invalid analysis evidence directory')
        target=self.root/reference['sha256']
        if target.exists():
            self.verify(reference,cancel)
        else:
            if cancel():raise InterruptedError('Analysis cancelled')
            os.replace(staged,target)
        self.verify(reference,cancel)
        return dict(reference)


def work_directory(library,identity):
    if type(identity) is not str or str(uuid.UUID(identity))!=identity:
        raise ValueError('Invalid analysis job identity')
    path=library.stage/(identity+'.instrumental')
    if path.is_symlink() or not path.resolve().is_relative_to(library.stage.resolve()):
        raise ValueError('Invalid analysis staging directory')
    return path

def cleanup_work(library,identity):
    # Verify resolved absolute target before recursive Windows deletion.
    path=work_directory(library,identity)
    if path.exists():
        if not path.is_dir():raise ValueError('Invalid analysis staging object')
        shutil.rmtree(path)

def reconcile_work(library):
    for path in library.stage.glob('*.instrumental'):
        try:
            identity=path.name[:-len('.instrumental')]
            if str(uuid.UUID(identity))!=identity:continue
        except ValueError:continue
        cleanup_work(library,identity)

def reconcile_evidence(library):
    """After owning worker.lock, discard only verified unreferenced app blobs.

    Unknown names, symlinks and corrupted bytes remain for diagnosis. Complete
    job results, including shared hashes, retain ownership across restarts.
    """
    store=AnalysisEvidence(library)
    if not store.root.exists():return
    if store.root.is_symlink():raise ValueError('Invalid analysis evidence directory')
    referenced=set()
    with library.connect() as db:
        for row in db.execute('SELECT result FROM jobs WHERE result IS NOT NULL'):
            try:
                value=json.loads(row[0]);checksum=value.get('evidence',{}).get('sha256')
                if type(checksum) is str and re.fullmatch('[0-9a-f]{64}',checksum):referenced.add(checksum)
            except (ValueError,AttributeError):continue
    for path in store.root.iterdir():
        if (re.fullmatch('[0-9a-f]{64}',path.name) and path.name not in referenced
            and not path.is_symlink() and path.is_file() and path.stat().st_size<=MAX_BYTES
            and digest(path)==path.name):path.unlink()
