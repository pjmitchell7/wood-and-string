"""Durable processing admission storage; this module never executes a job.

The caller authenticates and validates/normalizes the operation, source asset,
source digest, supported kind and runtime versions BEFORE submit. Revalidate
those immutable bytes and current resources before claim/launch. Requests are
opaque JSON here, not a new accepted processing protocol. job_id links the
existing jobs adapter; this store never rewrites that table or media.

All mutations use Library.connect() and BEGIN IMMEDIATE. A partial unique index
also permits only one running/cancel-requested claim across processes. Tokens
fence stale workers after recovery. Keep claim_token internal to the scheduler.
Only acknowledge finish/release of an active claim after its worker is reaped.
recover() is explicit startup recovery: call only after acquiring the existing
authority/worker ownership locks and ensuring previous workers have stopped.
No constructor recovery, timers, resource polling, HTTP or native processing.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import time
import uuid

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 32 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_REASON_CHARS = 1024
MAX_ACTIVE = 100
MAX_RECORDS = 10000  # Preserve history; refuse new work instead of deleting it.
MAX_DEPTH = 16
MAX_NODES = 8192
STATES = ('queued', 'waiting_resources', 'running', 'cancel_requested',
          'complete', 'failed', 'cancelled')
PENDING = ('queued', 'waiting_resources')
OWNED = ('running', 'cancel_requested')
TERMINAL = ('complete', 'failed', 'cancelled')


class QueueConflict(ValueError):
    """An operation ID was reused for different immutable work."""


class QueueFull(ValueError):
    """Admission capacity is exhausted; existing work/history is preserved."""


class QueueStateError(ValueError):
    """A stale claim or invalid state transition was refused."""


class QueueIntegrityError(ValueError):
    """Stored queue data does not match its canonical identity/schema."""


def _identity(value):
    if type(value) is not str or len(value) != 36:
        raise ValueError('Expected a canonical lowercase UUID')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValueError('Expected a canonical lowercase UUID') from error
    if str(parsed) != value:
        raise ValueError('Expected a canonical lowercase UUID')
    return value


def _json(value, maximum, *, request=False):
    if request and type(value) is not dict:
        raise ValueError('Expected a caller-validated normalized request object')
    nodes = 0
    ancestors = set()

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ValueError('Queue JSON exceeds its structural bound')
        kind = type(item)
        if kind is str:
            if len(item) > maximum:
                raise ValueError('Queue JSON string exceeds its byte bound')
            try:
                item.encode('utf-8', 'strict')
            except UnicodeError as error:
                raise ValueError('Queue JSON requires valid Unicode') from error
        elif kind is float:
            if not math.isfinite(item):
                raise ValueError('Queue JSON requires finite numbers')
        elif kind is int:
            if not -(2**63) <= item < 2**63:
                raise ValueError('Queue JSON integer exceeds its bounded representation')
        elif kind is bool or item is None:
            return
        elif kind in (dict, list):
            if id(item) in ancestors or len(item) > MAX_NODES:
                raise ValueError('Cyclic or oversized queue JSON')
            ancestors.add(id(item))
            try:
                if kind is dict:
                    for key, child in item.items():
                        if type(key) is not str:
                            raise ValueError('Queue JSON keys must be strings')
                        visit(key, depth + 1)
                        visit(child, depth + 1)
                else:
                    for child in item:
                        visit(child, depth + 1)
            finally:
                ancestors.remove(id(item))
        else:
            raise ValueError('Queue JSON requires plain JSON types')

    visit(value, 0)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True, allow_nan=False)
    if len(raw.encode('ascii')) > maximum:
        raise ValueError('Queue JSON exceeds its byte bound')
    return raw


def _reason(value):
    if value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > MAX_REASON_CHARS or '\0' in value:
        raise ValueError('Expected a short nonempty queue reason')
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError as error:
        raise ValueError('Queue reason requires valid Unicode') from error
    return value


class ProcessingQueue:
    def __init__(self, library, *, clock=time.time):
        self.library = library
        self.clock = clock
        with self._write() as db:
            db.execute('CREATE TABLE IF NOT EXISTS processing_queue_schema '
                       '(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)')
            version = db.execute('SELECT version FROM processing_queue_schema WHERE singleton=1').fetchone()
            if version is not None and version['version'] != SCHEMA_VERSION:
                raise QueueIntegrityError('Unsupported processing queue schema version')
            db.execute('''CREATE TABLE IF NOT EXISTS processing_queue (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL CHECK(length(request_json)<=32768),
                request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
                job_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('queued','waiting_resources','running','cancel_requested','complete','failed','cancelled')),
                reason TEXT CHECK(reason IS NULL OR length(reason)<=1024),
                result_json TEXT CHECK(result_json IS NULL OR length(result_json)<=65536),
                cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
                claim_token TEXT,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
                recoveries INTEGER NOT NULL DEFAULT 0 CHECK(recoveries>=0),
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                started_at REAL, finished_at REAL,
                CHECK ((state IN ('running','cancel_requested') AND claim_token IS NOT NULL)
                    OR (state NOT IN ('running','cancel_requested') AND claim_token IS NULL)),
                CHECK ((state IN ('complete','failed','cancelled') AND finished_at IS NOT NULL)
                    OR (state NOT IN ('complete','failed','cancelled') AND finished_at IS NULL)))''')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS processing_queue_one_owned "
                       "ON processing_queue((1)) WHERE state IN ('running','cancel_requested')")
            db.execute('CREATE INDEX IF NOT EXISTS processing_queue_pending ON processing_queue(state,sequence)')
            db.execute('''CREATE TRIGGER IF NOT EXISTS processing_queue_immutable
                BEFORE UPDATE OF sequence,operation_id,request_json,request_sha256,job_id,created_at ON processing_queue
                WHEN NEW.sequence IS NOT OLD.sequence OR NEW.operation_id IS NOT OLD.operation_id
                    OR NEW.request_json IS NOT OLD.request_json OR NEW.request_sha256 IS NOT OLD.request_sha256
                    OR NEW.job_id IS NOT OLD.job_id OR NEW.created_at IS NOT OLD.created_at
                BEGIN SELECT RAISE(ABORT,'immutable processing queue identity'); END''')
            if version is None:
                db.execute('INSERT INTO processing_queue_schema VALUES (1,?)', (SCHEMA_VERSION,))
            # Fail closed on a pre-existing partial/unrelated table, without touching jobs/assets.
            expected = {'sequence', 'operation_id', 'request_json', 'request_sha256', 'job_id', 'state',
                        'reason', 'result_json', 'cancel_requested', 'claim_token', 'attempts', 'recoveries',
                        'created_at', 'updated_at', 'started_at', 'finished_at'}
            if {row['name'] for row in db.execute('PRAGMA table_info(processing_queue)')} != expected:
                raise QueueIntegrityError('Unexpected processing queue table shape')

    @contextmanager
    def _write(self):
        with self.library.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    def _now(self, row=None):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('Expected a finite queue timestamp')
        return max(float(value), row['updated_at']) if row is not None else float(value)

    @staticmethod
    def _load(db, identity):
        row = db.execute('SELECT * FROM processing_queue WHERE operation_id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError('Queue item not found')
        return row

    @staticmethod
    def _row(row):
        result = dict(row)
        raw = result.pop('request_json')
        try:
            request = json.loads(raw)
            if (_json(request, MAX_REQUEST_BYTES, request=True) != raw
                    or hashlib.sha256(raw.encode('ascii')).hexdigest() != result['request_sha256']):
                raise ValueError('Request identity differs')
            encoded_result = result.pop('result_json')
            value = None if encoded_result is None else json.loads(encoded_result)
            if encoded_result is not None and _json(value, MAX_RESULT_BYTES) != encoded_result:
                raise ValueError('Result metadata is not canonical')
        except (TypeError, ValueError, RecursionError, UnicodeError) as error:
            raise QueueIntegrityError('Stored queue JSON failed identity validation') from error
        result.update(id=result['operation_id'], request=request, result=value,
                      cancel_requested=bool(result['cancel_requested']))
        return result

    @staticmethod
    def _claim(row, token):
        if row['claim_token'] is None:
            if token is not None:
                raise QueueStateError('This claim is no longer active')
        elif token is None or _identity(token) != row['claim_token']:
            raise QueueStateError('An exact current claim token is required')

    def submit(self, operation_id, request, job_id=None):
        """Idempotent byte identity; same ID with changed request/job raises conflict.

        Callers normalize their semantic numbers/versions first: 1 and 1.0 are
        distinct canonical requests. Returned dictionaries never alias inputs.
        The default job UUID matches the existing sorted-JSON jobs adapter.
        """
        identity = _identity(operation_id)
        raw = _json(request, MAX_REQUEST_BYTES, request=True)
        normalized = json.loads(raw)
        job_id = _identity(job_id) if job_id is not None else str(uuid.uuid5(
            uuid.NAMESPACE_URL, json.dumps(normalized, sort_keys=True, allow_nan=False)))
        digest = hashlib.sha256(raw.encode('ascii')).hexdigest()
        with self._write() as db:
            old = db.execute('SELECT * FROM processing_queue WHERE operation_id=?', (identity,)).fetchone()
            if old is not None:
                if old['request_json'] != raw or old['request_sha256'] != digest or old['job_id'] != job_id:
                    raise QueueConflict('Operation ID already belongs to different immutable work')
                return self._row(old)
            active = db.execute("SELECT count(*) FROM processing_queue WHERE state NOT IN "
                                "('complete','failed','cancelled')").fetchone()[0]
            total = db.execute('SELECT count(*) FROM processing_queue').fetchone()[0]
            if active >= MAX_ACTIVE:
                raise QueueFull('The processing queue already has 100 active requests')
            if total >= MAX_RECORDS:
                raise QueueFull('Processing history capacity reached; existing history is preserved')
            now = self._now()
            db.execute('''INSERT INTO processing_queue
                (operation_id,request_json,request_sha256,job_id,state,created_at,updated_at)
                VALUES (?,?,?,?,'queued',?,?)''', (identity, raw, digest, job_id, now, now))
            return self._row(self._load(db, identity))

    def get(self, operation_id):
        with self.library.connect() as db:
            return self._row(self._load(db, _identity(operation_id)))

    def list(self, states=None, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise ValueError('Expected bounded queue pagination')
        if states is None:
            selected = STATES
        elif type(states) is str:
            selected = (states,)
        elif type(states) in (tuple, list) and all(type(state) is str for state in states):
            selected = tuple(states)
        else:
            raise ValueError('Expected known queue state filters')
        if len(set(selected)) != len(selected) or any(state not in STATES for state in selected):
            raise ValueError('Expected known unique queue state filters')
        if not selected:
            return []
        placeholders = ','.join('?' for _ in selected)
        with self.library.connect() as db:
            return [self._row(row) for row in db.execute(
                f'SELECT * FROM processing_queue WHERE state IN ({placeholders}) '
                'ORDER BY sequence LIMIT ? OFFSET ?', (*selected, limit, offset))]

    def peek(self):
        """Inspect the FIFO pending head without claiming or skipping a waiter."""
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM processing_queue WHERE state IN ('queued','waiting_resources') "
                             'ORDER BY sequence LIMIT 1').fetchone()
            return None if row is None else self._row(row)

    def claim_next(self, expected_id=None):
        """Atomic single lane. Pass peek()['id'] after checking that exact item."""
        if expected_id is not None:
            _identity(expected_id)
        with self._write() as db:
            if db.execute("SELECT 1 FROM processing_queue WHERE state IN ('running','cancel_requested')").fetchone():
                return None
            row = db.execute("SELECT * FROM processing_queue WHERE state IN ('queued','waiting_resources') "
                             'ORDER BY sequence LIMIT 1').fetchone()
            if row is None or expected_id is not None and row['operation_id'] != expected_id:
                return None
            self._row(row)  # Verify the immutable request before changing its state.
            token, now = str(uuid.uuid4()), self._now(row)
            db.execute("UPDATE processing_queue SET state='running',claim_token=?,attempts=attempts+1,"
                       'reason=NULL,started_at=?,updated_at=? WHERE operation_id=?',
                       (token, now, now, row['operation_id']))
            return self._row(self._load(db, row['operation_id']))

    def wait(self, operation_id, reason, claim_token=None):
        """Persist a resource wait; release an owned lane only after cleanup."""
        identity, reason = _identity(operation_id), _reason(reason)
        if reason is None:
            raise ValueError('A resource wait needs an explanation')
        with self._write() as db:
            row = self._load(db, identity)
            if row['state'] not in (*PENDING, 'running'):
                raise QueueStateError('Only pending or stopped owned work can wait for resources')
            self._claim(row, claim_token)
            if row['state'] == 'waiting_resources' and row['reason'] == reason:
                # Resource polling must not force a journal flush for unchanged
                # state every two seconds or manufacture new progress timestamps.
                return self._row(row)
            db.execute("UPDATE processing_queue SET state='waiting_resources',reason=?,claim_token=NULL,"
                       'updated_at=? WHERE operation_id=?', (reason, self._now(row), identity))
            return self._row(self._load(db, identity))

    def finish(self, operation_id, state, reason=None, result=None, claim_token=None):
        """Retain a terminal outcome; caller verifies backend results/worker reap.

        Unclaimed pending work may finish for an already verified backend job.
        Active work needs its current token. A cancellation that wins the write
        race prevents a later complete transition: it is saved as cancelled.
        Existing terminal payloads cannot be replaced, including by a stale run.
        """
        identity = _identity(operation_id)
        if type(state) is not str or state not in TERMINAL:
            raise ValueError('Expected a terminal queue state')
        reason = _reason(reason)
        raw = None if result is None else _json(result, MAX_RESULT_BYTES)
        with self._write() as db:
            row = self._load(db, identity)
            if row['state'] in TERMINAL:
                if state == row['state'] and raw == row['result_json'] and (reason is None or reason == row['reason']):
                    return self._row(row)
                raise QueueStateError('Terminal queue history is immutable')
            self._claim(row, claim_token)
            if row['state'] == 'cancel_requested' and state == 'complete':
                state, reason = 'cancelled', 'Cancelled after the processing worker stopped.'
            if reason is None and state in ('failed', 'cancelled'):
                reason = 'Processing failed.' if state == 'failed' else 'Processing cancelled.'
            now = self._now(row)
            db.execute('UPDATE processing_queue SET state=?,reason=?,result_json=?,claim_token=NULL,'
                       'cancel_requested=?,finished_at=?,updated_at=? WHERE operation_id=?',
                       (state, reason, raw, int(row['cancel_requested'] or state == 'cancelled'), now, now, identity))
            return self._row(self._load(db, identity))

    def cancel(self, operation_id):
        """Durable cancellation; active work retains the lane until acknowledged."""
        identity = _identity(operation_id)
        with self._write() as db:
            row = self._load(db, identity)
            if row['state'] in (*TERMINAL, 'cancel_requested'):
                return self._row(row)
            now = self._now(row)
            pending = row['state'] in PENDING
            state = 'cancelled' if pending else 'cancel_requested'
            reason = 'Cancelled before processing started.' if pending else 'Cancellation requested; waiting for worker cleanup.'
            db.execute('UPDATE processing_queue SET state=?,reason=?,cancel_requested=1,updated_at=?,finished_at=? '
                       'WHERE operation_id=?', (state, reason, now, now if pending else None, identity))
            return self._row(self._load(db, identity))

    def recover(self):
        """Startup-only, after owning workers stop; fence old tokens and retain FIFO.

        Running work is queued again, never silently completed. Interrupted
        cancellation remains cancelled. Existing pending/terminal rows are kept.
        """
        with self._write() as db:
            rows = db.execute("SELECT * FROM processing_queue WHERE state IN ('running','cancel_requested')").fetchall()
            recovered = {'requeued': 0, 'cancelled': 0}
            for row in rows:
                self._row(row)
                cancelling = row['state'] == 'cancel_requested'
                state = 'cancelled' if cancelling else 'queued'
                reason = ('Interrupted processing remained cancelled after restart.' if cancelling else
                          'Processing was interrupted and queued again after restart.')
                now = self._now(row)
                db.execute('UPDATE processing_queue SET state=?,reason=?,claim_token=NULL,recoveries=recoveries+1,'
                           'updated_at=?,finished_at=? WHERE operation_id=?',
                           (state, reason, now, now if cancelling else None, row['operation_id']))
                recovered['cancelled' if cancelling else 'requeued'] += 1
            return recovered
