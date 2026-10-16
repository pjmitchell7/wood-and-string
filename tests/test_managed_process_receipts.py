"""Independent, silent original-handle evidence tests; no acquired engines/models."""
import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager
import os
from pathlib import Path
import itertools
import json
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from audio import managed_process_receipts as managed

ACTUAL_FIXTURE_RESULTS = []
ACTUAL_EXTENSION_RESULTS = []
RECEIPT_JOB_CLASS = managed._ReceiptJob


def run(request, emit):
    """Fixed owned Python fixture, bounded even if its parent fails."""
    emit({'phase': 'ready', 'pid': os.getpid()})
    if request.get('mode') == 'error':
        raise ValueError('intentional owned fixture failure')
    if request.get('mode') == 'wait':
        time.sleep(5)
    return {'ok': True, 'pid': os.getpid()}


def _owned_supervisor_fixture():
    """Fixed short-lived supervisor; only the extension test selects this mode."""
    job = managed._ReceiptJob()
    def ready(event):
        created, exited, kernel, user = (W.FILETIME() for _ in range(4))
        if not job.k.GetProcessTimes(job.worker, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
            raise C.WinError(C.get_last_error())
        belongs = W.BOOL()
        if not job.k.IsProcessInJob(job.worker, job.handle, C.byref(belongs)):
            raise C.WinError(C.get_last_error())
        pid = job.k.GetProcessId(job.worker)
        if pid != event['pid'] or not belongs.value:
            raise AssertionError('Fixture worker ownership was not established')
        print(json.dumps({'type': 'owned_ready', 'pid': pid,
            'creation_time_100ns': (created.dwHighDateTime << 32) | created.dwLowDateTime,
            'owned_by_supervisor_job': True}), flush=True)
        # Prevent a natural success racing this explicit forced-loss check.
        # This trusted fixture callback and its child both stop within 5 seconds.
        time.sleep(5)
    with mock.patch.object(managed, '_ReceiptJob', return_value=job):
        managed.run_worker('tests.test_managed_process_receipts', {'mode': 'wait'},
                           python=sys._base_executable, progress=ready, wall_seconds=10,
                           memory_bytes=128*1024**2)


def _value(handle):
    return int(handle) if hasattr(handle, '__int__') else handle.value


class Kernel:
    """Minimal injectable kernel with observable handle identity and ordering."""
    def __init__(self):
        self.calls = []
        self.pid_for = {10: 101, 20: 202}
        self.created_for = {10: 0x12345678ABCDEF01, 20: 0x7654321012345678}
        self.next_handle = 100
        self.wait_status = 0
        self.wait_error = 6
        self.exit_code = 1
        self.active = [0]
        self.query_error = False
        self.terminate_ok = True
        self.close_fail = set()
        self.belongs = True
        self.times_ok = True
        self.duplicate_ok = True
        self.reported_pid = None

    def GetCurrentProcess(self):
        return -1

    def DuplicateHandle(self, current, source, target, output, rights, inherit, options):
        source = _value(source)
        self.calls.append(('duplicate', source, rights, inherit, options))
        if not self.duplicate_ok:
            C.set_last_error(5)
            return 0
        self.next_handle += 1
        handle = self.next_handle
        self.pid_for[handle] = self.pid_for[source]
        self.created_for[handle] = self.created_for[source]
        C.cast(output, C.POINTER(W.HANDLE)).contents.value = handle
        return 1

    def GetProcessId(self, handle):
        self.calls.append(('pid', handle))
        return self.pid_for[handle] if self.reported_pid is None else self.reported_pid

    def IsProcessInJob(self, handle, job, output):
        self.calls.append(('belongs', handle, job))
        C.cast(output, C.POINTER(W.BOOL)).contents.value = self.belongs
        return 1

    def GetProcessTimes(self, handle, created, *others):
        self.calls.append(('creation', handle))
        if not self.times_ok:
            C.set_last_error(87)
            return 0
        value = self.created_for[handle]
        target = C.cast(created, C.POINTER(W.FILETIME)).contents
        target.dwLowDateTime = value & 0xffffffff
        target.dwHighDateTime = value >> 32
        return 1

    def WaitForSingleObject(self, handle, milliseconds):
        self.calls.append(('wait', handle, milliseconds))
        C.set_last_error(self.wait_error)
        return self.wait_status

    def GetExitCodeProcess(self, handle, output):
        self.calls.append(('exit', handle))
        C.cast(output, C.POINTER(W.DWORD)).contents.value = self.exit_code
        return 1

    def QueryInformationJobObject(self, handle, info, output, size, needed):
        self.calls.append(('accounting', handle, info))
        if self.query_error:
            C.set_last_error(50)
            return 0
        value = self.active.pop(0) if len(self.active) > 1 else self.active[0]
        C.cast(output, C.POINTER(managed._Accounting)).contents.active = value
        return 1

    def TerminateJobObject(self, handle, code):
        self.calls.append(('terminate', handle, code))
        C.set_last_error(5)
        return int(self.terminate_ok)

    def CloseHandle(self, handle):
        self.calls.append(('close', handle))
        # Deliberately clobber last-error to test capture at the failed wait.
        C.set_last_error(123)
        return int(handle not in self.close_fail)

    def OpenProcess(self, rights, inherit, pid):
        self.calls.append(('open_pid', rights, inherit, pid))
        return 20


class Handle:
    def __init__(self, value=10):
        self.value = value
        self.closed = False
        self.failure = None

    def __int__(self):
        return self.value

    def Close(self):
        if self.failure:
            raise self.failure
        self.closed = True


class Pipe:
    def __init__(self):
        self.closed = False
        self.failure = None

    def close(self):
        if self.failure:
            raise self.failure
        self.closed = True


class Process:
    def __init__(self):
        self.pid = 101
        self._handle = Handle()
        self.stdin, self.stdout = Pipe(), Pipe()
        self.killed = False
        self.waited = False
        self.wait_failure = None

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        self.waited = True
        if self.wait_failure:
            raise self.wait_failure
        return 1


class Job:
    def __init__(self, kernel=None):
        self.k = kernel or Kernel()
        self.handle, self.worker, self.gate = 30, 40, 50
        self.gate_name = 'owned-test-gate'
        self.assignments = []
        self.released = False
        self.peak = 4096

    def assign(self, handle):
        self.assignments.append(handle)

    def attach_original_worker(self, source):
        return RECEIPT_JOB_CLASS.attach_original_worker(self, source)

    def attach_worker(self, pid):
        return RECEIPT_JOB_CLASS.attach_worker(self, pid)

    def release_worker(self):
        self.released = True

    def peak_memory(self):
        return self.peak


class Reader:
    def __init__(self):
        self.alive = False
        self.joined = False
        self.start_failure = None
        self.alive_failure = None

    def is_alive(self):
        if self.alive_failure:
            raise self.alive_failure
        return self.alive

    def join(self, timeout):
        self.joined = True


class ProtocolReader(Reader):
    def __init__(self, messages, events, start_failure=None):
        super().__init__()
        self.messages, self.events, self.start_failure = messages, events, start_failure

    def start(self):
        if self.start_failure:
            raise self.start_failure
        for event in self.events:
            self.messages.put(event)


@unittest.skipUnless(os.name == 'nt', 'Windows API receipt tests')
class ReceiptTests(unittest.TestCase):
    def setup_owned(self):
        kernel = Kernel()
        job, proc, reader = Job(kernel), Process(), Reader()
        witnesses = []
        managed._witness(job, proc._handle, proc.pid, 'launcher', witnesses)
        return kernel, job, proc, reader, witnesses

    def cleanup(self, owned, result=True):
        kernel, job, proc, reader, witnesses = owned
        return managed._cleanup(job, proc, reader, queue.Queue(), witnesses, result)

    def stages(self, proof):
        return [item['stage'] for item in proof['errors']]

    def test_identity_uses_duplicated_original_handle_and_full_creation_time(self):
        kernel, job, proc, reader, witnesses = self.setup_owned()
        witness = witnesses[0]
        self.assertEqual(witness['creation_time_100ns'], 0x12345678ABCDEF01)
        self.assertTrue(witness['identity_verified'])
        self.assertNotEqual(witness['_handle'], int(proc._handle))
        self.assertEqual(kernel.calls[0], ('duplicate', 10, managed.SYNC_QUERY, False, 0))
        self.assertFalse(any(call[0] == 'open_pid' for call in kernel.calls))

    def test_normal_cleanup_records_nonzero_supervised_exit_and_closes_all(self):
        owned = self.setup_owned()
        proof = self.cleanup(owned)
        kernel, job, proc, reader, witnesses = owned
        self.assertTrue(proof['proven'])
        self.assertEqual(proof['mode'], 'valid_result_then_owned_cleanup')
        self.assertEqual(proof['owned_job_active_processes'], 0)
        self.assertEqual(proof['processes'][0]['exit_code'], 1)
        self.assertEqual(proof['processes'][0]['wait_result'], 0)
        self.assertIsNone(proof['processes'][0]['wait_last_error'])
        self.assertNotIn('_handle', proof['processes'][0])
        self.assertTrue(proc.waited and proc._handle.closed and proc.stdin.closed and proc.stdout.closed)
        for field in ('job_handle_closed', 'worker_monitor_handle_closed', 'gate_handle_closed',
                      'launcher_original_handle_closed', 'reader_joined'):
            self.assertIs(proof[field], True, field)
        self.assertLess(kernel.calls.index(('accounting', 30, 1)), kernel.calls.index(('close', 30)))
        self.assertFalse(any(call[0] == 'open_pid' for call in kernel.calls))

    def test_timeout_and_wait_failed_keep_distinct_status_and_last_error(self):
        for status in (258, 0xffffffff):
            with self.subTest(status=status):
                owned = self.setup_owned()
                owned[0].wait_status = status
                proof = self.cleanup(owned)
                self.assertFalse(proof['proven'])
                row = proof['processes'][0]
                self.assertEqual(row['wait_result'], status)
                self.assertEqual(row['wait_last_error'], 6 if status == 0xffffffff else None)
                self.assertIsNone(row['exit_code'])
                self.assertTrue(row['handle_closed'])
                self.assertIn('original_handle_wait', self.stages(proof))
                if status == 0xffffffff:
                    error = next(e for e in proof['errors'] if e['stage'] == 'original_handle_wait')
                    self.assertEqual(error['winerror'], 6)

    def test_creation_or_pid_or_job_mismatch_keeps_acquired_handle_for_cleanup(self):
        for fault in ('pid', 'job', 'creation', 'zero_creation'):
            with self.subTest(fault=fault):
                kernel, job, proc, reader = Kernel(), Job(), Process(), Reader()
                job.k = kernel
                if fault == 'pid': kernel.reported_pid = 999
                elif fault == 'job': kernel.belongs = False
                elif fault == 'creation': kernel.times_ok = False
                else: kernel.created_for[10] = 0
                witnesses = []
                with self.assertRaises((managed.ManagedProcessError, OSError)):
                    managed._witness(job, proc._handle, proc.pid, 'launcher', witnesses)
                self.assertEqual(len(witnesses), 1)
                self.assertFalse(witnesses[0]['identity_verified'])
                proof = managed._cleanup(job, proc, reader, queue.Queue(), witnesses, False)
                self.assertFalse(proof['proven'])
                self.assertTrue(proof['processes'][0]['handle_closed'])
                self.assertEqual(proof['mode'], 'failure_owned_cleanup')

    def test_job_waits_for_active_descendants_before_close(self):
        owned = self.setup_owned()
        owned[0].active = [2, 1, 0]
        with mock.patch.object(managed.time, 'sleep'):
            proof = self.cleanup(owned)
        self.assertTrue(proof['proven'])
        calls = owned[0].calls
        self.assertEqual(sum(call[0] == 'accounting' for call in calls), 3)
        self.assertLess(max(i for i, c in enumerate(calls) if c[0] == 'accounting'), calls.index(('close', 30)))

    def test_job_still_active_or_query_failed_is_not_proven(self):
        for query_error in (False, True):
            owned = self.setup_owned()
            owned[0].active = [1]
            owned[0].query_error = query_error
            with self.subTest(query_error=query_error), mock.patch.object(managed, 'CLEANUP_SECONDS', 0):
                proof = self.cleanup(owned)
            self.assertFalse(proof['proven'])
            self.assertFalse(proof['owned_job_empty'])
            self.assertIn('owned_job_empty', self.stages(proof))
            self.assertTrue(owned[2]._handle.closed)

    def test_each_close_failure_stays_unproven_and_later_resources_close(self):
        for handle, stage in ((40, 'worker_monitor_handle_closed'), (50, 'gate_handle_closed'),
                              (30, 'job_handle_closed'), (101, 'witness_handle_close')):
            with self.subTest(handle=handle):
                owned = self.setup_owned()
                owned[0].close_fail = {handle}
                proof = self.cleanup(owned)
                self.assertFalse(proof['proven'])
                self.assertIn(stage, self.stages(proof))
                self.assertTrue(owned[2]._handle.closed)
                self.assertIn(('close', 30), owned[0].calls)
                self.assertIn(('close', 101), owned[0].calls)

    def test_popen_wait_and_original_close_failures_are_explicit(self):
        for fault in ('wait', 'close'):
            owned = self.setup_owned()
            if fault == 'wait': owned[2].wait_failure = subprocess.TimeoutExpired('owned fixture', 0)
            else: owned[2]._handle.failure = OSError('original close denied')
            with self.subTest(fault=fault):
                proof = self.cleanup(owned)
                self.assertFalse(proof['proven'])
                self.assertIn('original_popen_wait' if fault == 'wait' else 'launcher_original_handle_close', self.stages(proof))

    def test_failed_termination_uses_original_fallback_but_never_promotes(self):
        owned = self.setup_owned()
        owned[0].terminate_ok = False
        proof = self.cleanup(owned)
        self.assertFalse(proof['proven'])
        self.assertFalse(proof['terminate_job_succeeded'])
        self.assertTrue(owned[2].killed and owned[2].waited)
        self.assertIn('terminate_job', self.stages(proof))

    def test_reader_fault_does_not_skip_original_handle_close(self):
        owned = self.setup_owned()
        owned[3].alive_failure = RuntimeError('reader status failed')
        proof = self.cleanup(owned)
        self.assertFalse(proof['proven'])
        self.assertIn('reader_join', self.stages(proof))
        self.assertTrue(owned[2]._handle.closed)
        self.assertTrue(owned[2].waited)
        self.assertFalse(owned[2].stdout.closed)

    def test_unstarted_reader_is_not_joined(self):
        owned = self.setup_owned()
        owned[3].join = mock.Mock(side_effect=RuntimeError('cannot join thread before it is started'))
        proof = self.cleanup(owned)
        self.assertTrue(proof['proven'])
        owned[3].join.assert_not_called()
        self.assertTrue(owned[2].stdout.closed)

    @contextmanager
    def protocol(self, *, pid=101, events=None, start_failure=None):
        job, proc, readers = Job(), Process(), []
        events = events if events is not None else [
            ('message', {'type': 'hello', 'pid': pid}),
            ('message', {'type': 'result', 'value': {'ok': True}})]
        def make_reader(*args, **kwargs):
            reader = ProtocolReader(kwargs['args'][1], events, start_failure)
            readers.append(reader)
            return reader
        with mock.patch.object(managed, '_ReceiptJob', return_value=job), \
                mock.patch.object(managed.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(managed.threading, 'Thread', side_effect=make_reader):
            yield job, proc, readers

    def call(self, **kwargs):
        return managed.run_worker('tests.test_managed_process_receipts', {}, wall_seconds=10,
                                  memory_bytes=128*1024**2, **kwargs)

    def test_same_pid_uses_one_original_identity_for_both_roles_no_pid_open(self):
        with self.protocol() as (job, proc, readers):
            result = self.call()
        proof = result['cleanup']
        self.assertTrue(proof['proven'])
        self.assertEqual(result['metrics']['launcher_pid'], result['metrics']['worker_pid'])
        self.assertEqual(len(proof['processes']), 1)
        self.assertEqual(proof['processes'][0]['roles'], ['launcher', 'worker'])
        self.assertFalse(any(call[0] == 'open_pid' for call in job.k.calls))
        self.assertTrue(job.released)
        self.assertNotIn('cleanup', result['metrics'])

    def test_different_pid_opens_worker_once_at_handshake_then_uses_retained_handles(self):
        with self.protocol(pid=202) as (job, proc, readers):
            result = self.call()
        self.assertTrue(result['cleanup']['proven'])
        processes = result['cleanup']['processes']
        self.assertEqual([(p['pid'], p['roles']) for p in processes],
                         [(101, ['launcher']), (202, ['worker'])])
        opens = [c for c in job.k.calls if c[0] == 'open_pid']
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0][-1], 202)
        self.assertTrue(opens[0][1] & 0x100000)
        self.assertEqual(processes[1]['creation_time_100ns'], 0x7654321012345678)

    def test_start_failure_keeps_primary_and_closes_stdout_original_handle(self):
        with self.protocol(start_failure=RuntimeError('thread start sentinel')) as (job, proc, readers):
            with self.assertRaisesRegex(managed.ManagedProcessError, 'thread start sentinel') as caught:
                self.call()
        self.assertEqual(caught.exception.metrics['primary_error']['detail'], str(caught.exception))
        self.assertEqual(caught.exception.metrics['cleanup']['mode'], 'failure_owned_cleanup')
        self.assertTrue(proc.stdout.closed and proc._handle.closed and proc.waited)
        self.assertFalse(readers[0].joined)

    def test_primary_worker_error_survives_secondary_wait_and_close_faults(self):
        events = [('message', {'type': 'hello', 'pid': 101}),
                  ('message', {'type': 'error', 'message': 'primary worker sentinel'})]
        with self.protocol(events=events) as (job, proc, readers):
            job.k.wait_status = 0xffffffff
            job.k.close_fail = {50}
            with self.assertRaisesRegex(managed.ManagedProcessError, 'primary worker sentinel') as caught:
                self.call()
        metrics = caught.exception.metrics
        self.assertFalse(metrics['cleanup']['proven'])
        self.assertIn('primary worker sentinel', metrics['primary_error']['detail'])
        self.assertIn('original_handle_wait', self.stages(metrics['cleanup']))
        self.assertIn('gate_handle_closed', self.stages(metrics['cleanup']))
        self.assertTrue(proc._handle.closed)

    def test_valid_result_with_unproved_cleanup_cannot_return_success(self):
        with self.protocol() as (job, proc, readers):
            job.k.query_error = True
            with self.assertRaisesRegex(managed.ManagedProcessError, 'cleanup could not be proved') as caught:
                self.call()
        self.assertFalse(caught.exception.metrics['cleanup']['proven'])
        self.assertEqual(caught.exception.metrics['cleanup']['mode'], 'valid_result_then_owned_cleanup')

    def test_cancellation_remains_primary_with_cleanup_receipt(self):
        with self.protocol() as (job, proc, readers):
            with self.assertRaises(managed.Cancelled) as caught:
                self.call(cancel=lambda: job.released)
        self.assertEqual(caught.exception.metrics['primary_error']['type'], 'Cancelled')
        self.assertTrue(caught.exception.metrics['cleanup']['proven'])
        self.assertTrue(proc._handle.closed)

    def test_cancel_before_launch_has_no_process_and_no_false_cleanup_proof(self):
        with mock.patch.object(managed.subprocess, 'Popen') as spawn:
            with self.assertRaises(managed.Cancelled) as caught:
                self.call(cancel=lambda: True)
        spawn.assert_not_called()
        self.assertIsNone(caught.exception.metrics['launcher_pid'])
        self.assertFalse(caught.exception.metrics['cleanup']['proven'])

    def test_fallback_keeps_primary_continues_all_closes_and_never_proves(self):
        events = [('message', {'type': 'hello', 'pid': 101}),
                  ('message', {'type': 'error', 'message': 'original primary'})]
        with self.protocol(events=events) as (job, proc, readers), \
                mock.patch.object(managed, '_cleanup', side_effect=RuntimeError('unexpected cleanup fault')):
            # The first Job resource close fails; later ones must still run.
            job.k.close_fail = {job.worker}
            with self.assertRaisesRegex(managed.ManagedProcessError, 'original primary') as caught:
                self.call()
        proof = caught.exception.metrics['cleanup']
        self.assertFalse(proof['proven'])
        self.assertIn('unexpected_cleanup_exception', self.stages(proof))
        self.assertIn(('close', 50), job.k.calls)
        self.assertIn(('close', 30), job.k.calls)
        self.assertTrue(proc.waited and proc._handle.closed and proc.stdout.closed)

    def test_fallback_drains_full_queue_to_release_owned_reader(self):
        owned = self.setup_owned()
        kernel, job, proc, unused, witnesses = owned
        messages = queue.Queue(maxsize=128)
        for i in range(128): messages.put(('message', {'i': i}))
        entered = threading.Event()
        def finish_reader():
            entered.set()
            messages.put(('eof', None))
        reader = threading.Thread(target=finish_reader, daemon=True)
        reader.start()
        self.assertTrue(entered.wait(1))
        proof = managed._cleanup_fallback(job, proc, reader, messages, witnesses,
                                          RuntimeError('synthetic fallback'), False)
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive())
        self.assertFalse(proof['proven'])
        self.assertTrue(proc.stdout.closed and proc._handle.closed)

    def test_constructor_failure_never_launches_and_preserves_primary_without_proof(self):
        with mock.patch.object(managed, '_ReceiptJob', side_effect=OSError('constructor sentinel')), \
                mock.patch.object(managed.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(managed.ManagedProcessError, 'constructor sentinel') as caught:
                self.call()
        spawn.assert_not_called()
        self.assertFalse(caught.exception.metrics['cleanup']['proven'])
        self.assertIsNone(caught.exception.metrics['launcher_pid'])

    def test_spawn_failure_closes_created_job_without_claiming_process_proof(self):
        job = Job()
        with mock.patch.object(managed, '_ReceiptJob', return_value=job), \
                mock.patch.object(managed.subprocess, 'Popen', side_effect=OSError('spawn sentinel')):
            with self.assertRaisesRegex(managed.ManagedProcessError, 'spawn sentinel') as caught:
                self.call()
        self.assertFalse(caught.exception.metrics['cleanup']['proven'])
        self.assertIsNone(caught.exception.metrics['launcher_pid'])
        self.assertIn(('close', 30), job.k.calls)
        self.assertIn(('close', 50), job.k.calls)

    def test_duplicate_failure_does_not_invent_a_witness(self):
        kernel, witnesses = Kernel(), []
        kernel.duplicate_ok = False
        with self.assertRaises(OSError):
            managed._witness(Job(kernel), Handle(), 101, 'launcher', witnesses)
        self.assertEqual(witnesses, [])

    def test_gated_worker_assignment_failure_closes_acquired_monitor(self):
        with self.protocol(pid=202) as (job, proc, readers):
            job.assign = mock.Mock(side_effect=[None, OSError('worker assign sentinel')])
            with self.assertRaisesRegex(managed.ManagedProcessError, 'worker assign sentinel') as caught:
                self.call()
        self.assertFalse(job.released)
        self.assertIn(('close', 20), job.k.calls)
        self.assertTrue(proc._handle.closed)
        self.assertEqual(len(caught.exception.metrics['cleanup']['processes']), 1)

    def test_monitor_and_callback_failures_preserve_primary_and_close_owned_process(self):
        events = [('message', {'type': 'hello', 'pid': 101}),
                  ('message', {'type': 'progress', 'value': {'phase': 'ready'}})]
        for fault in ('monitor', 'callback'):
            with self.subTest(fault=fault), self.protocol(events=events) as (job, proc, readers):
                if fault == 'monitor':
                    job.peak_memory = mock.Mock(side_effect=OSError('monitor sentinel'))
                    kwargs = {}
                else:
                    kwargs = {'progress': mock.Mock(side_effect=RuntimeError('callback sentinel'))}
                with self.assertRaisesRegex(managed.ManagedProcessError, fault + ' sentinel') as caught:
                    self.call(**kwargs)
            self.assertTrue(caught.exception.metrics['cleanup']['proven'])
            self.assertTrue(proc.waited and proc._handle.closed)

    def test_malformed_hello_or_eof_never_releases_gate(self):
        for event in [('message', {'type': 'hello', 'pid': True}), ('eof', None)]:
            with self.subTest(event=event), self.protocol(events=[event]) as (job, proc, readers):
                with self.assertRaises(managed.ManagedProcessError) as caught:
                    self.call()
            self.assertFalse(job.released)
            self.assertTrue(proc.waited and proc._handle.closed)
            self.assertTrue(caught.exception.metrics['cleanup']['proven'])

    def test_sampled_memory_threshold_error_keeps_cleanup_receipt(self):
        with self.protocol() as (job, proc, readers):
            job.peak = 129*1024**2
            with self.assertRaisesRegex(managed.LimitExceeded, 'sampled memory') as caught:
                self.call()
        self.assertTrue(caught.exception.metrics['cleanup']['proven'])
        self.assertEqual(caught.exception.metrics['peak_working_set_bytes'], job.peak)

    def test_secondary_error_records_are_bounded(self):
        kernel, job, proc, reader, witnesses = self.setup_owned()
        # Synthetic repeated faults cannot make the retained receipt unbounded.
        witnesses = [dict(witnesses[0]) for _ in range(managed.MAX_CLEANUP_ERRORS + 10)]
        kernel.wait_status = 0xffffffff
        kernel.close_fail = {101}
        proof = managed._cleanup(job, proc, reader, queue.Queue(), witnesses, False)
        self.assertFalse(proof['proven'])
        self.assertEqual(len(proof['errors']), managed.MAX_CLEANUP_ERRORS)
        self.assertGreater(proof['additional_error_count'], 0)
        self.assertTrue(all(len(error['detail']) <= 512 for error in proof['errors']))


@unittest.skipUnless(os.name == 'nt', 'Owned Windows Python process fixtures')
class ActualOwnedProcessTests(unittest.TestCase):
    def call(self, mode='success', **kwargs):
        return managed.run_worker('tests.test_managed_process_receipts', {'mode': mode},
                                  python=kwargs.pop('python', sys._base_executable), wall_seconds=10,
                                  memory_bytes=128*1024**2, **kwargs)

    def record(self, name, result=None, failure=None, *, same_pid=True, python=None):
        metrics = result['metrics'] if result else failure.metrics
        proof = result['cleanup'] if result else metrics['cleanup']
        ACTUAL_FIXTURE_RESULTS.append({'fixture': name, 'python': str(python or sys._base_executable),
            'limits': {'wall_seconds': 10, 'sampled_worker_bytes': 128*1024**2},
            'metrics': metrics, 'cleanup': proof,
            'exception': type(failure).__name__ if failure else None})
        self.assertTrue(proof['proven'], proof)
        self.assertTrue(proof['owned_job_empty'])
        self.assertEqual(proof['owned_job_active_processes'], 0)
        if same_pid:
            self.assertEqual(metrics['worker_pid'], metrics['launcher_pid'])
            self.assertEqual(len(proof['processes']), 1)
            self.assertEqual(proof['processes'][0]['roles'], ['launcher', 'worker'])
        else:
            self.assertNotEqual(metrics['worker_pid'], metrics['launcher_pid'])
            self.assertEqual(len(proof['processes']), 2)
            self.assertEqual([(row['pid'], row['roles']) for row in proof['processes']],
                             [(metrics['launcher_pid'], ['launcher']), (metrics['worker_pid'], ['worker'])])
        for row in proof['processes']:
            self.assertGreater(row['creation_time_100ns'], 2**32)
            self.assertEqual(row['wait_result'], 0)
            self.assertIs(type(row['exit_code']), int)
            self.assertTrue(row['handle_closed'])
        self.assertLess(metrics['elapsed_seconds'], 10)
        self.assertLess(metrics['peak_working_set_bytes'], 128*1024**2)

    def test_short_real_python_success_without_any_openprocess_lookup(self):
        original = managed._ReceiptJob
        def owned_job():
            job = original()
            job.k.OpenProcess = mock.Mock(side_effect=AssertionError('No PID open for direct Python'))
            return job
        with mock.patch.object(managed, '_ReceiptJob', side_effect=owned_job):
            result = self.call()
        self.assertTrue(result['result']['ok'])
        self.record('real_direct_python_success_no_pid_open', result=result)

    def test_short_real_worker_failure_retains_original_error_and_proof(self):
        with self.assertRaisesRegex(managed.ManagedProcessError, 'intentional owned fixture failure') as caught:
            self.call('error')
        self.record('real_direct_python_worker_error', failure=caught.exception)

    def test_real_cancel_after_owned_progress_uses_retained_handle_proof(self):
        cancel = threading.Event()
        with self.assertRaises(managed.Cancelled) as caught:
            self.call('wait', cancel=cancel.is_set, progress=lambda _: cancel.set())
        self.record('real_direct_python_cancel_after_ownership', failure=caught.exception)

    @unittest.skipUnless((Path(__file__).resolve().parents[1]/'.venv/Scripts/python.exe').is_file(),
                         'Existing app venv Python is unavailable; no installation')
    def test_existing_venv_records_both_identities_and_only_one_gated_pid_open(self):
        python = Path(__file__).resolve().parents[1]/'.venv/Scripts/python.exe'
        original, opened = managed._ReceiptJob, []
        def owned_job():
            job = original()
            real_open = job.k.OpenProcess
            def captured_open(rights, inherit, pid):
                opened.append({'pid': pid, 'rights': rights, 'inherit': inherit})
                return real_open(rights, inherit, pid)
            job.k.OpenProcess = captured_open
            return job
        with mock.patch.object(managed, '_ReceiptJob', side_effect=owned_job):
            result = self.call(python=python)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]['pid'], result['metrics']['worker_pid'])
        self.assertTrue(opened[0]['rights'] & 0x100000)
        self.record('real_existing_venv_distinct_original_handles', result=result,
                    same_pid=False, python=python)


@unittest.skipUnless(os.name == 'nt', 'Windows original-handle extension controls')
class LifecycleExtensionTests(unittest.TestCase):
    def test_wall_time_expiry_preserves_limit_error_and_cleanup_proof(self):
        helper = ReceiptTests('test_unstarted_reader_is_not_joined')
        clock = itertools.count(0, 20)
        with helper.protocol() as (job, proc, readers), \
                mock.patch.object(managed.time, 'monotonic', side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(managed.LimitExceeded, 'wall-time') as caught:
                helper.call()
        metrics = caught.exception.metrics
        self.assertEqual(metrics['primary_error']['type'], 'LimitExceeded')
        self.assertTrue(metrics['cleanup']['proven'])
        self.assertEqual(metrics['cleanup']['mode'], 'failure_owned_cleanup')
        self.assertFalse(job.released)
        self.assertTrue(proc.waited and proc._handle.closed)
        ACTUAL_EXTENSION_RESULTS.append({'fixture': 'fake_wall_time_expiry',
            'metrics': metrics, 'scope': 'Simulated clock and kernel; no real child or elapsed-time claim.'})

    def test_forced_supervisor_loss_waits_retained_owned_child_handle(self):
        """No late PID reopen: acquire once before killing the known supervisor."""
        began = time.monotonic()
        deadline = began + 10
        kernel = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenProcess': (W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            'GetProcessId': (W.DWORD, [W.HANDLE]),
            'GetProcessTimes': (W.BOOL, [W.HANDLE]+[C.POINTER(W.FILETIME)]*4),
            'IsProcessInJob': (W.BOOL, [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)]),
            'WaitForSingleObject': (W.DWORD, [W.HANDLE, W.DWORD]),
            'GetExitCodeProcess': (W.BOOL, [W.HANDLE, C.POINTER(W.DWORD)]),
            'CloseHandle': (W.BOOL, [W.HANDLE]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(kernel, name)
            function.restype, function.argtypes = restype, argtypes
        def identity(handle):
            created, exited, system, user = (W.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(system), C.byref(user)):
                raise C.WinError(C.get_last_error())
            pid = kernel.GetProcessId(handle)
            self.assertGreater(pid, 0)
            return {'pid': pid, 'creation_time_100ns': (created.dwHighDateTime << 32)|created.dwLowDateTime}
        def memory(handle):
            monitor = type('OwnedMemoryMonitor', (), {})()
            monitor.worker = handle
            monitor.p = C.WinDLL('psapi', use_last_error=True)
            return managed.old._WindowsJob.peak_memory(monitor)
        def remaining():
            return max(.001, deadline-time.monotonic())
        supervisor = child = reader = None
        proof = {'fixture': 'forced_owned_python_supervisor_loss',
            'limits': {'whole_fixture_seconds': 10, 'managed_worker_sampled_bytes': 128*1024**2},
            'child_handle_opened_before_supervisor_kill': False, 'late_pid_reopens': 0,
            'child_handle_closed': False, 'supervisor_original_handle_closed': False,
            'scope': 'Termination after forced supervisor loss; Job kill versus stdin-EOF fallback is not isolated.'}
        try:
            supervisor = subprocess.Popen([sys._base_executable, '-m', 'tests.test_managed_process_receipts',
                                           '--owned-supervisor-fixture'],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            proof['supervisor'] = identity(supervisor._handle)
            messages = queue.Queue(maxsize=1)
            def read_ready():
                try: messages.put(supervisor.stdout.readline(2049))
                except BaseException as exc: messages.put(exc)
            reader = threading.Thread(target=read_ready, daemon=True)
            reader.start()
            line = messages.get(timeout=min(3, remaining()))
            if isinstance(line, BaseException): raise line
            self.assertLessEqual(len(line), 2048)
            ready = json.loads(line)
            self.assertEqual(ready['type'], 'owned_ready')
            self.assertIs(ready['owned_by_supervisor_job'], True)
            self.assertIs(type(ready['pid']), int)
            child = kernel.OpenProcess(0x100000|0x400|0x10, False, ready['pid'])
            if not child: raise C.WinError(C.get_last_error())
            proof['child_handle_opened_before_supervisor_kill'] = True
            proof['child'] = identity(child)
            self.assertEqual(proof['child'], {key: ready[key] for key in ('pid', 'creation_time_100ns')})
            belongs = W.BOOL()
            if not kernel.IsProcessInJob(child, None, C.byref(belongs)):
                raise C.WinError(C.get_last_error())
            self.assertTrue(belongs.value)
            proof['child_in_job_before_supervisor_kill'] = True
            proof['child_wait_before_kill'] = kernel.WaitForSingleObject(child, 0)
            self.assertEqual(proof['child_wait_before_kill'], 258)
            self.assertIsNone(supervisor.poll())
            proof['sampled_child_peak_bytes'] = memory(child)
            proof['sampled_supervisor_peak_bytes'] = memory(supervisor._handle)
            self.assertLess(proof['sampled_child_peak_bytes'], 128*1024**2)
            self.assertLess(proof['sampled_supervisor_peak_bytes'], 128*1024**2)
            supervisor.kill()  # Uses only the retained original Popen handle.
            proof['supervisor_exit_code'] = supervisor.wait(timeout=remaining())
            proof['supervisor_original_wait_result'] = kernel.WaitForSingleObject(supervisor._handle, 0)
            self.assertEqual(proof['supervisor_original_wait_result'], 0)
            self.assertNotEqual(proof['supervisor_exit_code'], 0)
            proof['child_wait_result'] = kernel.WaitForSingleObject(child, max(1, int(remaining()*1000)))
            self.assertEqual(proof['child_wait_result'], 0)
            code = W.DWORD()
            if not kernel.GetExitCodeProcess(child, C.byref(code)):
                raise C.WinError(C.get_last_error())
            proof['child_exit_code'] = code.value
            self.assertNotEqual(code.value, 259)
            # Re-read identity using the held handle, even after termination.
            self.assertEqual(identity(child), proof['child'])
        finally:
            if supervisor is not None:
                if supervisor.poll() is None:
                    supervisor.kill()
                supervisor.wait(timeout=remaining())
                if reader is not None:
                    reader.join(timeout=min(1, remaining()))
                    self.assertFalse(reader.is_alive())
                supervisor.stdout.close()
                supervisor._handle.Close()
                proof['supervisor_original_handle_closed'] = True
            if child:
                if not kernel.CloseHandle(child): raise C.WinError(C.get_last_error())
                proof['child_handle_closed'] = True
            proof['elapsed_seconds'] = time.monotonic()-began
            ACTUAL_EXTENSION_RESULTS.append(proof)
        self.assertLess(proof['elapsed_seconds'], 10)


if __name__ == '__main__':
    if sys.argv[1:] == ['--owned-supervisor-fixture']:
        _owned_supervisor_fixture()
    else:
        unittest.main()
