"""Immutable byte store with separate source/variant identities; local authority."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager

RESERVE = 8 * 1024**3
MAX_IMPORT = 250 * 1024**2

class Library:
    def __init__(self, root):
        self.root = Path(root)
        self.media = self.root / 'media'
        self.stage = self.root / 'staging'
        self.media.mkdir(parents=True, exist_ok=True)
        self.stage.mkdir(exist_ok=True)
        self.db = self.root / 'library.sqlite'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT NOT NULL,
              filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
              mime TEXT NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL,
              UNIQUE(sha256, source_id, kind));
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, state TEXT NOT NULL, progress REAL NOT NULL,
              request TEXT NOT NULL, result TEXT, error TEXT, created_at REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    @contextmanager
    def exclusive(self, name):
        """OS released lock prevents two authorities/worker overlap after crashes."""
        path=self.root/name
        handle=path.open('a+b')
        try:
            if path.stat().st_size==0: handle.write(b'0');handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally: handle.close()

    def space(self, needed=0):
        if shutil.disk_usage(self.root).free - needed < RESERVE:
            raise ValueError('Not enough free space: keeping an 8 GB safety reserve.')

    def reconcile_staging(self):
        """Only call after owning workers stop. Remove UUID-named incomplete scratch."""
        removed=0
        for path in self.stage.iterdir():
            suffix=path.name[36:]
            try: uuid.UUID(path.name[:36])
            except ValueError: continue
            if path.is_file() and suffix in ('.partial','.wav.partial','.wav.float.partial','.wav',
                    '.wav.dry.f32','.wav.receipt.json','.wav.receipt.partial',
                    '.cancel','.analysis.json','.analysis.partial','.analysis.decoded.wav','.analysis.decoded.wav.decode.partial'):
                removed+=path.stat().st_size
                path.unlink()
        return removed

    def cleanup_render_staging(self, identity):
        """After reaping this worker only; concurrent uploads remain untouched."""
        if type(identity) is not str or str(uuid.UUID(identity))!=identity:
            raise ValueError('Invalid render job identity')
        removed=0
        for suffix in ('.wav','.wav.partial','.wav.float.partial',
                       '.wav.dry.f32','.wav.receipt.json','.wav.receipt.partial'):
            path=self.stage/(identity+suffix)
            if path.is_file():
                removed+=path.stat().st_size
                path.unlink()
        return removed

    def items(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM assets ORDER BY created_at DESC')]

    def get(self, asset_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        if row is None: raise KeyError('Track not found')
        return dict(row)

    def verify(self, asset_id):
        row=self.get(asset_id)
        path=self.media/row['sha256']
        if not path.exists() or path.stat().st_size!=row['size']: return False
        with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()==row['sha256']

    def publish(self, staged, filename, mime, kind='original', source_id=None):
        staged = Path(staged)
        with staged.open('rb') as f: digest = hashlib.file_digest(f, 'sha256').hexdigest()
        size = staged.stat().st_size
        if not size: raise ValueError('The file is empty.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if kind == 'original':
                old = db.execute("SELECT * FROM assets WHERE sha256=? AND kind='original'", (digest,)).fetchone()
                if old:
                    stored = self.media/digest
                    valid = False
                    if stored.exists():
                        with stored.open('rb') as f:
                            valid = hashlib.file_digest(f,'sha256').hexdigest() == digest
                    if not valid:
                        # A verified retry repairs only the exact immutable byte identity.
                        if stored.exists(): os.replace(stored,self.stage/(str(uuid.uuid4())+'.corrupt'))
                        os.replace(staged,stored)
                        return dict(old)
                    staged.unlink()
                    return dict(old)
            identity = str(uuid.uuid4())
            source_id = source_id or identity
            old = db.execute('SELECT * FROM assets WHERE sha256=? AND source_id=? AND kind=?',(digest,source_id,kind)).fetchone()
            if old:
                stored=self.media/digest
                if stored.exists():
                    with stored.open('rb') as f:
                        if hashlib.file_digest(f,'sha256').hexdigest()==digest:
                            staged.unlink()
                            return dict(old)
                if stored.exists(): os.replace(stored,self.stage/(str(uuid.uuid4())+'.corrupt'))
                os.replace(staged,stored)
                return dict(old)
            path = self.media / digest
            if path.exists():
                with path.open('rb') as f:
                    if hashlib.file_digest(f, 'sha256').hexdigest() != digest:
                        raise ValueError('Stored media failed integrity verification; preserved upload for recovery.')
                staged.unlink()
            else: os.replace(staged, path)
            row = dict(id=identity, source_id=source_id, title=Path(filename).stem,
                       filename=filename, sha256=digest, size=size, mime=mime,
                       kind=kind, created_at=time.time())
            db.execute('INSERT INTO assets VALUES (:id,:source_id,:title,:filename,:sha256,:size,:mime,:kind,:created_at)', row)
        return row

    def job(self, identity):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
        if row is None: raise KeyError('Job not found')
        result = dict(row)
        result['request'] = json.loads(result['request'])
        result['result'] = json.loads(result['result']) if result['result'] else None
        return result

    def update_job(self, identity, state, progress=0, result=None, error=None):
        with self.connect() as db:
            db.execute('UPDATE jobs SET state=?,progress=?,result=?,error=? WHERE id=?',
                       (state, progress, json.dumps(result) if result else None, error, identity))
