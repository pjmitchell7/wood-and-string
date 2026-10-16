"""Queue storage/restart/concurrency only. No server, media or native execution."""
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from library import Library
import processing_queue as storage
from processing_queue import ProcessingQueue, QueueConflict, QueueFull, QueueIntegrityError, QueueStateError


def identity():
    return str(uuid.uuid4())


def claim_process(root, barrier, results):
    """Only the SQLite API runs in these short-lived owned Python helpers."""
    try:
        queue = ProcessingQueue(Library(root))
        barrier.wait(timeout=10)
        row = queue.claim_next()
        results.put(None if row is None else {'id': row['id'], 'claim_token': row['claim_token']})
    except BaseException as error:
        results.put({'error': repr(error)})
        raise


def submit_process(root, operation_id, request, barrier, results):
    try:
        queue = ProcessingQueue(Library(root))
        barrier.wait(timeout=10)
        try:
            row = queue.submit(operation_id, request)
            results.put({'state': 'accepted', 'request': row['request']})
        except QueueConflict:
            results.put({'state': 'conflict'})
    except BaseException as error:
        results.put({'error': repr(error)})
        raise


class ProcessingQueueTests(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[1] / 'work'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work, prefix='processing-queue-test-')
        self.addCleanup(self.temp.cleanup)
        self.library = Library(self.temp.name)
        self.queue = ProcessingQueue(self.library)

    def submit(self, request=None):
        return self.queue.submit(identity(), {'opaque': 'caller normalized'} if request is None else request)

    def test_migration_is_separate_idempotent_and_preserves_library_tables(self):
        job = identity()
        with self.library.connect() as db:
            db.execute('PRAGMA user_version=37')
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (job, 'queued', 0, '{}', None, None, 1))
            before = tuple(db.execute('SELECT * FROM jobs').fetchone())
        first = self.submit()
        again = ProcessingQueue(self.library)
        self.assertEqual(again.get(first['id']), first)
        with self.library.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM jobs').fetchone()), before)
            self.assertEqual(db.execute('SELECT count(*) FROM assets').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 37)
            self.assertEqual(db.execute('SELECT version FROM processing_queue_schema').fetchone()[0], 1)

    def test_future_schema_refused_without_rewriting_data(self):
        row = self.submit()
        with self.library.connect() as db:
            db.execute('UPDATE processing_queue_schema SET version=2')
        with self.assertRaises(QueueIntegrityError):
            ProcessingQueue(self.library)
        self.assertEqual(self.queue.get(row['id']), row)

    def test_canonical_idempotence_and_detached_opaque_request(self):
        operation = identity()
        request = {'z': [1, {'text': 'café 🎸'}], 'a': True}
        first = self.queue.submit(operation, request)
        reordered = {'a': True, 'z': [1, {'text': 'café 🎸'}]}
        self.assertEqual(self.queue.submit(operation, reordered), first)
        expected = json.dumps(reordered, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
        self.assertEqual(first['request_sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(first['job_id'], str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(reordered, sort_keys=True))))
        request['z'].append('caller mutation')
        first['request']['z'].append('returned object mutation')
        self.assertEqual(self.queue.get(operation)['request'], reordered)

    def test_same_id_changed_request_numeric_type_or_backend_job_conflicts(self):
        row = self.submit({'n': 1})
        for request, job in [({'n': 2}, None), ({'n': 1.0}, None), ({'n': 1}, identity())]:
            with self.subTest(request=request, job=job), self.assertRaises(QueueConflict):
                self.queue.submit(row['id'], request, job)
        self.assertEqual(self.queue.get(row['id']), row)

    def test_noncanonical_ids_and_non_json_or_unbounded_requests_rejected(self):
        valid = identity()
        for bad in (valid.upper(), '{' + valid + '}', valid.replace('-', ''), 'path/elsewhere', 7, None):
            with self.subTest(identity=bad), self.assertRaises(ValueError):
                self.queue.submit(bad, {})
        cycle = []; cycle.append(cycle)
        deep = []
        for _ in range(storage.MAX_DEPTH + 2):
            deep = [deep]
        invalid = [[], {1: 'bad key'}, {'x': (1, 2)}, {'x': b'bytes'}, {'x': float('nan')},
                   {'x': float('inf')}, {'x': 2**1000}, {'x': '\ud800'}, {'x': cycle}, {'x': deep},
                   {'x': 'a' * storage.MAX_REQUEST_BYTES}, {'x': [0] * (storage.MAX_NODES + 1)}]
        for request in invalid:
            with self.subTest(type=type(request).__name__), self.assertRaises(ValueError):
                self.queue.submit(identity(), request)
        self.assertEqual(self.queue.list(), [])

    def test_source_and_kind_validation_are_an_explicit_caller_boundary(self):
        with (patch.object(self.library, 'get', side_effect=AssertionError('No asset lookup in opaque storage')),
              patch.object(self.library, 'verify', side_effect=AssertionError('No source file I/O'))):
            row = self.submit({'future_validator_owned_fields': {'version': 1}})
            self.assertEqual(self.queue.peek()['request'], row['request'])
        # Claim is only a durable store transition; no worker is started.
        self.assertEqual(self.queue.claim_next(expected_id=row['id'])['id'], row['id'])

    def test_wait_reason_persists_and_fifo_does_not_skip_waiting_head(self):
        first, second = self.submit(), self.submit()
        waiting = self.queue.wait(first['id'], 'Waiting for enough available memory.')
        self.assertEqual(waiting['state'], 'waiting_resources')
        self.assertEqual(ProcessingQueue(self.library).peek(), waiting)
        self.assertIsNone(self.queue.claim_next(expected_id=second['id']))
        claim = self.queue.claim_next(expected_id=first['id'])
        self.assertEqual(claim['id'], first['id'])
        self.assertEqual(claim['attempts'], 1)
        self.assertIsNone(self.queue.claim_next())

    def test_stale_peek_cannot_claim_unchecked_successor(self):
        first, second = self.submit(), self.submit()
        self.assertEqual(self.queue.peek()['id'], first['id'])
        self.queue.cancel(first['id'])
        self.assertIsNone(self.queue.claim_next(expected_id=first['id']))
        self.assertEqual(self.queue.get(second['id'])['state'], 'queued')
        self.assertEqual(self.queue.claim_next(expected_id=second['id'])['id'], second['id'])

    def test_unchanged_wait_does_not_rewrite_progress_or_flush_an_update(self):
        row = self.submit()
        waiting = self.queue.wait(row['id'], 'Waiting for laptop memory.')
        original = self.library.connect
        statements = []

        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.library, 'connect', traced):
            self.assertEqual(self.queue.wait(row['id'], 'Waiting for laptop memory.'), waiting)
        self.assertFalse(any(statement.lstrip().upper().startswith('UPDATE ') for statement in statements))
        changed = self.queue.wait(row['id'], 'Waiting for disk space.')
        self.assertEqual(changed['reason'], 'Waiting for disk space.')

    def test_active_claim_needs_exact_token_for_wait_or_finish(self):
        first, second = self.submit(), self.submit()
        claim = self.queue.claim_next()
        for token in (None, identity()):
            with self.subTest(token=token):
                with self.assertRaises(QueueStateError):
                    self.queue.finish(first['id'], 'complete', claim_token=token)
                with self.assertRaises(QueueStateError):
                    self.queue.wait(first['id'], 'Waiting after owned worker cleanup.', claim_token=token)
        waiting = self.queue.wait(first['id'], 'Memory changed before launch.', claim_token=claim['claim_token'])
        self.assertIsNone(waiting['claim_token'])
        self.assertEqual(self.queue.peek()['id'], first['id'])
        next_claim = self.queue.claim_next()
        self.assertNotEqual(next_claim['claim_token'], claim['claim_token'])
        self.assertEqual(next_claim['attempts'], 2)
        self.queue.finish(first['id'], 'complete', result={'job_id': first['job_id']}, claim_token=next_claim['claim_token'])
        self.assertEqual(self.queue.claim_next()['id'], second['id'])

    def test_cancel_pending_is_durable_terminal_and_idempotent(self):
        first = self.submit()
        self.queue.wait(first['id'], 'Waiting for RAM.')
        cancelled = self.queue.cancel(first['id'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertTrue(cancelled['cancel_requested'])
        self.assertIsNotNone(cancelled['finished_at'])
        self.assertEqual(self.queue.cancel(first['id']), cancelled)
        self.assertEqual(self.queue.submit(first['id'], first['request']), cancelled)
        self.assertIsNone(self.queue.peek())
        self.assertEqual(ProcessingQueue(self.library).get(first['id']), cancelled)

    def test_running_cancel_keeps_lane_until_ack_and_wins_completion_race(self):
        first, second = self.submit(), self.submit()
        claim = self.queue.claim_next()
        cancelled = self.queue.cancel(first['id'])
        self.assertEqual(cancelled['state'], 'cancel_requested')
        self.assertEqual(cancelled['claim_token'], claim['claim_token'])
        self.assertIsNone(self.queue.claim_next())
        with self.assertRaises(QueueStateError):
            self.queue.wait(first['id'], 'Do not discard cancellation.', claim_token=claim['claim_token'])
        final = self.queue.finish(first['id'], 'complete', claim_token=claim['claim_token'])
        self.assertEqual(final['state'], 'cancelled')
        self.assertTrue(final['cancel_requested'])
        self.assertEqual(self.queue.claim_next()['id'], second['id'])

    def test_terminal_payload_retained_and_cannot_be_replaced(self):
        row = self.submit()
        final = self.queue.finish(row['id'], 'complete', result={'verified_job_id': row['job_id']})
        self.assertEqual(self.queue.finish(row['id'], 'complete', result=final['result']), final)
        self.assertEqual(self.queue.cancel(row['id']), final)
        for state, value in [('failed', None), ('complete', {'different': 'result'})]:
            with self.subTest(state=state), self.assertRaises(QueueStateError):
                self.queue.finish(row['id'], state, result=value)
        with self.assertRaises(QueueStateError):
            self.queue.wait(row['id'], 'Cannot requeue saved terminal history.')
        self.assertEqual(self.queue.recover(), {'requeued': 0, 'cancelled': 0})
        self.assertEqual(self.queue.get(row['id']), final)

    def test_restart_is_explicit_and_old_claim_cannot_complete_reclaimed_work(self):
        first, second = self.submit(), self.submit()
        old = self.queue.claim_next()
        restarted = ProcessingQueue(Library(self.temp.name))
        self.assertEqual(restarted.get(first['id'])['state'], 'running')
        self.assertIsNone(restarted.claim_next())
        self.assertEqual(restarted.recover(), {'requeued': 1, 'cancelled': 0})
        restored = restarted.get(first['id'])
        self.assertEqual(restored['state'], 'queued')
        self.assertIsNone(restored['result'])
        self.assertEqual(restored['recoveries'], 1)
        with self.assertRaises(QueueStateError):
            restarted.finish(first['id'], 'complete', claim_token=old['claim_token'])
        new = restarted.claim_next()
        self.assertEqual(new['id'], first['id'])
        self.assertEqual(new['attempts'], 2)
        self.assertNotEqual(new['claim_token'], old['claim_token'])
        with self.assertRaises(QueueStateError):
            restarted.finish(first['id'], 'failed', claim_token=old['claim_token'])
        restarted.finish(first['id'], 'complete', claim_token=new['claim_token'])
        self.assertEqual(restarted.claim_next()['id'], second['id'])

    def test_restart_preserves_requested_cancellation_and_unrelated_waiter(self):
        first, second = self.submit(), self.submit()
        self.queue.wait(second['id'], 'Waiting for local storage.')
        waiter = self.queue.get(second['id'])
        self.queue.claim_next(); self.queue.cancel(first['id'])
        self.assertEqual(self.queue.recover(), {'requeued': 0, 'cancelled': 1})
        self.assertEqual(self.queue.get(first['id'])['state'], 'cancelled')
        self.assertEqual(self.queue.get(second['id']), waiter)
        self.assertEqual(self.queue.peek()['id'], second['id'])

    def test_capacity_bounds_preserve_idempotence_and_terminal_history(self):
        with patch.object(storage, 'MAX_ACTIVE', 2), patch.object(storage, 'MAX_RECORDS', 3):
            first, second = self.submit(), self.submit()
            with self.assertRaises(QueueFull): self.submit()
            self.assertEqual(self.queue.submit(first['id'], first['request']), first)
            self.queue.cancel(first['id'])
            third = self.submit()
            self.queue.cancel(second['id']); self.queue.cancel(third['id'])
            with self.assertRaises(QueueFull): self.submit()
            self.assertEqual(len(self.queue.list()), 3)
            self.assertTrue(all(row['state'] == 'cancelled' for row in self.queue.list()))

    def test_mid_transaction_exception_rolls_back_admission_and_releases_lock(self):
        operation = identity()
        original = self.library.connect

        @contextmanager
        def interrupted_commit():
            with original() as db:
                yield db
                raise OSError('Simulated interruption before transaction commit')

        with patch.object(self.library, 'connect', interrupted_commit), self.assertRaises(OSError):
            self.queue.submit(operation, {'kept': 'only after durable commit'})
        with self.assertRaises(KeyError): self.queue.get(operation)
        self.assertEqual(self.queue.submit(operation, {'kept': 'only after durable commit'})['state'], 'queued')

    def test_invalid_clock_and_result_bounds_leave_existing_state_unchanged(self):
        broken = ProcessingQueue(self.library, clock=lambda: float('nan'))
        with self.assertRaises(ValueError): broken.submit(identity(), {})
        self.assertIsNone(self.queue.peek())
        row = self.submit()
        for result in ({'x': 'a' * storage.MAX_RESULT_BYTES}, {'x': float('inf')}):
            with self.assertRaises(ValueError): self.queue.finish(row['id'], 'complete', result=result)
        for reason in ('', ' ', 'x' * 1025, '\0'):
            with self.assertRaises(ValueError): self.queue.wait(row['id'], reason)
        self.assertEqual(self.queue.get(row['id']), row)

    def test_fifo_uses_sequence_when_wall_clock_moves_backwards(self):
        ticks = iter([100, 50, 25, 20])
        queue = ProcessingQueue(self.library, clock=lambda: next(ticks))
        first = queue.submit(identity(), {'a': 1})
        second = queue.submit(identity(), {'a': 2})
        self.assertGreater(first['created_at'], second['created_at'])
        claim = queue.claim_next()
        self.assertEqual(claim['id'], first['id'])
        self.assertGreaterEqual(claim['updated_at'], first['updated_at'])
        queue.finish(first['id'], 'complete', claim_token=claim['claim_token'])
        self.assertEqual(queue.peek()['id'], second['id'])

    def test_database_immutable_identity_and_global_lane_constraints(self):
        first, second = self.submit(), self.submit()
        with self.library.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE processing_queue SET request_json=? WHERE operation_id=?', ('{}', first['id']))
        self.queue.claim_next()
        with self.library.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE processing_queue SET state='running',claim_token=? WHERE operation_id=?", (identity(), second['id']))
        self.assertEqual(self.queue.get(second['id'])['state'], 'queued')

    def test_corrupt_request_identity_refused_before_claim(self):
        operation = identity()
        with self.library.connect() as db:
            db.execute('''INSERT INTO processing_queue
                (operation_id,request_json,request_sha256,job_id,state,created_at,updated_at)
                VALUES (?,?,?,?,'queued',1,1)''', (operation, '{}', '0' * 64, identity()))
        with self.assertRaises(QueueIntegrityError): self.queue.get(operation)
        with self.assertRaises(QueueIntegrityError): self.queue.claim_next()
        with self.library.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM processing_queue').fetchone()[0], 'queued')

    def test_pagination_filters_and_unknown_items(self):
        first, second, third = self.submit(), self.submit(), self.submit()
        self.queue.cancel(first['id'])
        self.assertEqual([row['id'] for row in self.queue.list(states=['queued'])], [second['id'], third['id']])
        self.assertEqual([row['id'] for row in self.queue.list(limit=1, offset=1)], [second['id']])
        self.assertEqual(self.queue.list(states=[]), [])
        for arguments in ({'states': ['unknown']}, {'states': ['queued', 'queued']}, {'limit': 0}, {'limit': True}, {'limit': 1001}, {'offset': -1}):
            with self.assertRaises(ValueError): self.queue.list(**arguments)
        for operation in (self.queue.get, self.queue.cancel):
            with self.assertRaises(KeyError): operation(identity())

    def concurrent(self, target, arguments):
        context = multiprocessing.get_context('spawn')
        barrier, results = context.Barrier(2), context.Queue()
        processes = [context.Process(target=target, args=(*args, barrier, results)) for args in arguments]
        try:
            for process in processes: process.start()
            values = [results.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(timeout=15)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertFalse(any(isinstance(value, dict) and 'error' in value for value in values), values)
            return values
        finally:
            for process in processes:
                if process.is_alive(): process.terminate()
                process.join(timeout=5)
                process.close()
            results.close(); results.join_thread()

    def test_two_processes_cannot_claim_two_items(self):
        first = self.submit(); self.submit()
        values = self.concurrent(claim_process, [(self.temp.name,), (self.temp.name,)])
        claims = [value for value in values if value is not None]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['id'], first['id'])
        self.assertEqual(len(self.queue.list(states='running')), 1)

    def test_two_processes_conflicting_idempotency_cannot_replace_work(self):
        operation = identity()
        values = self.concurrent(submit_process, [(self.temp.name, operation, {'selected': 'a'}),
                                                 (self.temp.name, operation, {'selected': 'b'})])
        self.assertEqual(sorted(row['state'] for row in values), ['accepted', 'conflict'])
        accepted = next(row for row in values if row['state'] == 'accepted')
        self.assertEqual(self.queue.get(operation)['request'], accepted['request'])
        self.assertEqual(len(self.queue.list()), 1)


if __name__ == '__main__':
    unittest.main()
