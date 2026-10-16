"""Optional managed parent with original-handle cleanup receipts.

Uses the unchanged managed_process child bootstrap, framing and Job limits.
No controller selects this module yet. Numeric PID reopens after execution are
not used as process-identity or termination authority.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import math
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from . import managed_process as old

VERSION = 'managed-process-receipts-v1'
ManagedProcessError, Cancelled, LimitExceeded = old.ManagedProcessError, old.Cancelled, old.LimitExceeded
CLEANUP_SECONDS = 3.0
MAX_CLEANUP_ERRORS = 32
SYNC_QUERY = 0x100000 | 0x400


class _Accounting(C.Structure):
    _fields_ = [(name, C.c_int64) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        (name, W.DWORD) for name in ('faults', 'total', 'active', 'terminated')]


class _ReceiptJob(old._WindowsJob):
    def __init__(self):
        super().__init__()
        signatures = {
            'GetCurrentProcess': (W.HANDLE, []),
            'DuplicateHandle': (W.BOOL, [W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]),
            'GetProcessId': (W.DWORD, [W.HANDLE]),
            'GetProcessTimes': (W.BOOL, [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4),
            'WaitForSingleObject': (W.DWORD, [W.HANDLE, W.DWORD]),
            'QueryInformationJobObject': (W.BOOL, [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]),
        }
        try:
            for name, (restype, args) in signatures.items():
                function = getattr(self.k, name); function.restype = restype; function.argtypes = args
        except BaseException:
            self.close()
            raise

    def attach_original_worker(self, source):
        self.worker = _duplicate(self.k, source, SYNC_QUERY | 0x100 | 0x10 | 1)
        self.assign(self.worker)

    def attach_worker(self, pid):
        # Capture once at the gated hello, extending the old monitor's rights
        # with SYNCHRONIZE. No subsequent PID-based process opens are needed.
        self.worker = self.k.OpenProcess(0x100000 | 0x100 | 0x400 | 0x10 | 1, False, pid)
        if not self.worker: raise C.WinError(C.get_last_error())
        self.assign(self.worker)


def _duplicate(kernel, source, rights=SYNC_QUERY):
    handle = W.HANDLE(); current = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(current, source, current, C.byref(handle), rights, False, 0):
        raise C.WinError(C.get_last_error())
    return handle.value


def _witness(job, source_handle, pid, role, witnesses):
    handle = _duplicate(job.k, source_handle)
    row = {'_handle': handle, 'roles': [role], 'pid': pid, 'creation_time_100ns': None,
           'identity_verified': False, 'wait_result': None, 'wait_last_error': None,
           'exit_code': None, 'handle_closed': False}
    witnesses.append(row)  # Every acquired handle is owned even if binding fails.
    observed = job.k.GetProcessId(handle)
    if observed != pid: raise ManagedProcessError('Original process handle/PID differs')
    belongs = W.BOOL()
    if not job.k.IsProcessInJob(handle, job.handle, C.byref(belongs)):
        raise C.WinError(C.get_last_error())
    if not belongs.value: raise ManagedProcessError('Process witness is outside the owned Job')
    created, exited, kernel, user = (W.FILETIME() for _ in range(4))
    if not job.k.GetProcessTimes(handle, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
        raise C.WinError(C.get_last_error())
    row['creation_time_100ns'] = (created.dwHighDateTime << 32) | created.dwLowDateTime
    if row['creation_time_100ns'] <= 0: raise ManagedProcessError('Missing process creation identity')
    row['identity_verified'] = True
    return row


def _reader_cleanup(reader, messages, error):
    if reader is None: return True
    try:
        # A failed Thread.start leaves an object that cannot be joined.
        alive = reader.is_alive(); deadline = time.monotonic() + 2
        while alive and time.monotonic() < deadline:
            try: messages.get(timeout=.025)
            except queue.Empty: pass
            alive = reader.is_alive()
        if alive: reader.join(timeout=.1)
        if reader.is_alive():
            raise ManagedProcessError('Protocol reader did not terminate')
        return True
    except BaseException as exc:
        error('reader_join', exc)
        return False


def _cleanup(job, proc, reader, messages, witnesses, result_received):
    began = time.monotonic(); deadline = began + CLEANUP_SECONDS
    proof = {'version': VERSION, 'mode': 'valid_result_then_owned_cleanup' if result_received else 'failure_owned_cleanup',
        'terminate_job_succeeded': False, 'owned_job_active_processes': None, 'owned_job_empty': False,
        'job_handle_closed': job is None, 'worker_monitor_handle_closed': job is None,
        'gate_handle_closed': job is None, 'launcher_original_handle_closed': proc is None,
        'reader_joined': reader is None, 'processes': [], 'errors': [], 'proven': False}
    def error(stage, exc):
        if len(proof['errors']) < MAX_CLEANUP_ERRORS:
            value = {'stage': stage, 'type': type(exc).__name__, 'detail': str(exc)[:512]}
            if getattr(exc, 'winerror', None) is not None: value['winerror'] = exc.winerror
            proof['errors'].append(value)
        else: proof['additional_error_count'] = proof.get('additional_error_count', 0) + 1
    if proc is not None and proc.stdin is not None:
        try: proc.stdin.close()
        except BaseException as exc: error('parent_pipe_close', exc)
    if job is not None and job.handle:
        try:
            if not job.k.TerminateJobObject(job.handle, 1): raise C.WinError(C.get_last_error())
            proof['terminate_job_succeeded'] = True
        except BaseException as exc: error('terminate_job', exc)
    if proc is not None and not proof['terminate_job_succeeded']:
        try: proc.kill()  # Original Popen handle only, including pre-assignment failure.
        except BaseException as exc: error('fallback_original_terminate', exc)
    for row in witnesses:
        try:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            status = job.k.WaitForSingleObject(row['_handle'], remaining)
            last_error = C.get_last_error() if status == 0xffffffff else None
            row.update(wait_result=status, wait_last_error=last_error)
            if status != 0:
                if status == 0xffffffff: raise C.WinError(last_error)
                raise ManagedProcessError('Original-handle wait did not signal: ' + str(status))
            exit_code = W.DWORD()
            if not job.k.GetExitCodeProcess(row['_handle'], C.byref(exit_code)):
                raise C.WinError(C.get_last_error())
            row['exit_code'] = exit_code.value  # Cleanup may intentionally exit1.
        except BaseException as exc: error('original_handle_wait', exc)
    if job is not None and job.handle:
        try:
            while True:
                value = _Accounting()
                if not job.k.QueryInformationJobObject(job.handle, 1, C.byref(value), C.sizeof(value), None):
                    raise C.WinError(C.get_last_error())
                proof['owned_job_active_processes'] = value.active
                if value.active == 0:
                    proof['owned_job_empty'] = True; break
                if time.monotonic() >= deadline: raise ManagedProcessError('Owned Job did not become empty')
                time.sleep(old.POLL_SECONDS)
        except BaseException as exc: error('owned_job_empty', exc)
    # Retain the Job through wait/accounting, then close every owned resource.
    if job is not None:
        for attribute, field in (('worker', 'worker_monitor_handle_closed'), ('gate', 'gate_handle_closed'),
                                 ('handle', 'job_handle_closed')):
            handle = getattr(job, attribute, None)
            if not handle: proof[field] = True; continue
            try:
                if not job.k.CloseHandle(handle): raise C.WinError(C.get_last_error())
                setattr(job, attribute, None); proof[field] = True
            except BaseException as exc: error(field, exc)
    for row in witnesses:
        try:
            if not job.k.CloseHandle(row['_handle']): raise C.WinError(C.get_last_error())
            row['handle_closed'] = True
        except BaseException as exc: error('witness_handle_close', exc)
    if proc is not None:
        try: proc.wait(timeout=max(0, deadline - time.monotonic()))
        except BaseException as exc: error('original_popen_wait', exc)
    proof['reader_joined'] = _reader_cleanup(reader, messages, error)
    if proc is not None:
        if proof['reader_joined']:
            try: proc.stdout.close()
            except BaseException as exc: error('stdout_close', exc)
        else: error('stdout_close', ManagedProcessError('Close skipped to avoid a live buffered-reader lock'))
        try:
            proc._handle.Close()  # CPython's checked original-handle close, idempotent on destruction.
            proof['launcher_original_handle_closed'] = True
        except BaseException as exc: error('launcher_original_handle_close', exc)
    proof['processes'] = [{key: value for key, value in row.items() if key != '_handle'} for row in witnesses]
    proof['elapsed_seconds'] = time.monotonic() - began
    proof['proven'] = bool(job is not None and proc is not None and witnesses and proof['owned_job_empty']
        and not proof['errors'] and all(row['identity_verified'] and row['wait_result'] == 0 and row['handle_closed']
                                      for row in witnesses))
    return proof


def _cleanup_fallback(job, proc, reader, messages, witnesses, exc, result_received):
    """Best effort on unforeseen cleanup faults; never a successful proof."""
    errors = [{'stage': 'unexpected_cleanup_exception', 'type': type(exc).__name__, 'detail': str(exc)[:512]}]
    def error(stage, failure):
        if len(errors) < MAX_CLEANUP_ERRORS:
            errors.append({'stage': stage, 'type': type(failure).__name__, 'detail': str(failure)[:512]})
    def attempt(stage, operation):
        try: operation()
        except BaseException as failure: error(stage, failure)
    if proc is not None and proc.stdin is not None: attempt('fallback_pipe_close', proc.stdin.close)
    if job is not None:
        if job.handle:
            def terminate():
                if not job.k.TerminateJobObject(job.handle, 1): raise C.WinError(C.get_last_error())
            attempt('fallback_terminate_job', terminate)
        for attribute in ('worker', 'gate', 'handle'):
            handle = getattr(job, attribute, None)
            if handle:
                def close(handle=handle, attribute=attribute):
                    if not job.k.CloseHandle(handle): raise C.WinError(C.get_last_error())
                    setattr(job, attribute, None)
                attempt('fallback_close_' + attribute, close)
    if proc is not None:
        attempt('fallback_original_terminate', proc.kill)
        attempt('fallback_original_wait', lambda: proc.wait(timeout=1))
    for row in witnesses:
        if not row['handle_closed'] and job is not None:
            def close(row=row):
                if not job.k.CloseHandle(row['_handle']): raise C.WinError(C.get_last_error())
                row['handle_closed'] = True
            attempt('fallback_witness_close', close)
    reader_joined = _reader_cleanup(reader, messages, error)
    if proc is not None:
        def stdout_close():
            if reader_joined: proc.stdout.close()
        attempt('fallback_stdout_close', stdout_close)
        attempt('fallback_original_handle_close', proc._handle.Close)
    return {'version': VERSION, 'mode': 'valid_result_then_owned_cleanup' if result_received else 'failure_owned_cleanup',
            'proven': False, 'owned_job_empty': False, 'reader_joined': reader_joined, 'errors': errors,
            'processes': [{key: value for key, value in row.items() if key != '_handle'} for row in witnesses]}


def run_worker(module, request, *, python=sys.executable, cancel=lambda: False,
               progress=lambda event: None, wall_seconds=120, memory_bytes=4*1024**3):
    """Old fixed-module API plus cleanup proof; never promote a failed cleanup."""
    if not isinstance(module, str) or not re.fullmatch(r'(audio|tests)\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', module):
        raise ValueError('Expected a fixed internal audio/tests module')
    if not isinstance(request, dict): raise ValueError('Worker request must be a dict')
    if not math.isfinite(wall_seconds) or wall_seconds <= 0 or type(memory_bytes) is not int or memory_bytes <= 0:
        raise ValueError('Positive finite worker limits required')
    payload = json.dumps(request, allow_nan=False, ensure_ascii=True)
    if len(payload) > 8192: raise ValueError('Worker request too large')
    job = proc = reader = None; actual = None; peak = events = 0; result = failure = None
    witnesses = []; messages = queue.Queue(maxsize=128); began = time.monotonic()
    try:
        if cancel(): raise Cancelled('Analysis cancelled before launch')
        job = _ReceiptJob()
        proc = subprocess.Popen([str(python), '-m', 'audio.managed_process', '--child', module, payload, job.gate_name],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(int(proc._handle))
        launcher = _witness(job, proc._handle, proc.pid, 'launcher', witnesses)
        reader = threading.Thread(target=old._read_messages, args=(proc.stdout, messages), daemon=True); reader.start()
        while True:
            if cancel(): raise Cancelled('Analysis cancelled')
            if time.monotonic() - began > wall_seconds: raise LimitExceeded('Analysis wall-time limit exceeded')
            if actual is not None:
                peak = max(peak, job.peak_memory())
                if peak > memory_bytes: raise LimitExceeded('Analysis sampled memory threshold exceeded')
            try: kind, event = messages.get(timeout=old.POLL_SECONDS)
            except queue.Empty: continue
            if kind == 'error': raise ManagedProcessError('Invalid worker protocol: ' + event)
            if kind == 'eof': raise ManagedProcessError('Worker exited without a successful result')
            if actual is None:
                if event.get('type') != 'hello' or type(event.get('pid')) is not int or event['pid'] <= 0:
                    raise ManagedProcessError('Missing actual worker PID handshake')
                actual = event['pid']
                if actual == proc.pid:
                    job.attach_original_worker(proc._handle); launcher['roles'].append('worker')
                else:
                    job.attach_worker(actual); _witness(job, job.worker, actual, 'worker', witnesses)
                if cancel(): raise Cancelled('Analysis cancelled before ownership acknowledgment')
                job.release_worker()
            elif event.get('type') == 'progress' and result is None:
                events += 1
                if events > old.MAX_EVENTS: raise ManagedProcessError('Worker progress event limit exceeded')
                if not isinstance(event.get('value'), dict): raise ManagedProcessError('Invalid worker progress')
                progress(event['value'])
            elif event.get('type') == 'result' and result is None and isinstance(event.get('value'), dict):
                result = event['value']; break
            elif event.get('type') == 'error':
                raise ManagedProcessError('Analysis worker failed: ' + str(event.get('message', 'unknown')))
            else: raise ManagedProcessError('Unexpected worker message')
    except BaseException as exc:
        failure = exc if isinstance(exc, (ManagedProcessError, KeyboardInterrupt, SystemExit)) else ManagedProcessError(
            'Worker supervision failed: ' + type(exc).__name__ + ': ' + str(exc))
    finally:
        elapsed = time.monotonic() - began
        try: cleanup = _cleanup(job, proc, reader, messages, witnesses, result is not None)
        except BaseException as exc:
            cleanup = _cleanup_fallback(job, proc, reader, messages, witnesses, exc, result is not None)
    metrics = {'version': VERSION, 'worker_pid': actual, 'launcher_pid': proc.pid if proc is not None else None,
               'peak_working_set_bytes': peak, 'elapsed_seconds': elapsed, 'cleanup': cleanup}
    if failure is not None:
        metrics['primary_error'] = {'type': type(failure).__name__, 'detail': str(failure)[:2048]}
        failure.metrics = metrics
        raise failure
    if result is None or not cleanup['proven']:
        error = ManagedProcessError('Managed worker cleanup could not be proved'); error.metrics = metrics
        raise error
    return {'result': result, 'metrics': {key: value for key, value in metrics.items() if key != 'cleanup'},
            'cleanup': cleanup}
