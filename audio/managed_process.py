"""Bounded Windows analysis workers (standard library only).

run_worker('audio.owned_module', request, ...) starts a fixed internal module's
run(request, emit) function. Module/python/cwd are application configuration,
NEVER user-request commands. emit(dict) sends JSON progress; return a JSON dict.
Returns {'result': dict, 'metrics': {...}}. stdin is ONLY a liveness pipe.
Limits are sampled termination thresholds, not hard allocation reservations.
Memory covers the actual analysis process, not summed descendant memory.
Callbacks must return promptly; they run on the monitoring thread.
No shell, arbitrary script text, or external executable is accepted by the CLI.
Windows Job Objects kill owned processes on supervisor death; the worker also
exits on stdin EOF before importing analysis code. No privilege adjustment.
"""
import ctypes as C
from ctypes import wintypes as W
import importlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

MAX_LINE = 65536
MAX_EVENTS = 10000
POLL_SECONDS = .025


class ManagedProcessError(RuntimeError):
    pass


class Cancelled(ManagedProcessError):
    pass


class LimitExceeded(ManagedProcessError):
    pass


class _WindowsJob:
    """Non-inheritable kill-on-close job; retain actual worker process handle."""
    def __init__(self):
        if os.name != 'nt':
            raise ManagedProcessError('Managed analysis currently requires Windows')
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.p = C.WinDLL('psapi', use_last_error=True)
        signatures = {
            'CreateJobObjectW': (W.HANDLE, [C.c_void_p, W.LPCWSTR]),
            'SetInformationJobObject': (W.BOOL, [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]),
            'AssignProcessToJobObject': (W.BOOL, [W.HANDLE, W.HANDLE]),
            'IsProcessInJob': (W.BOOL, [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)]),
            'OpenProcess': (W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            'CloseHandle': (W.BOOL, [W.HANDLE]),
            'TerminateJobObject': (W.BOOL, [W.HANDLE, W.UINT]),
            'GetExitCodeProcess': (W.BOOL, [W.HANDLE, C.POINTER(W.DWORD)]),
            'CreateEventW': (W.HANDLE, [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR]),
            'SetEvent': (W.BOOL, [W.HANDLE]),
        }
        for name, (restype, args) in signatures.items():
            fn = getattr(self.k, name); fn.restype = restype; fn.argtypes = args
        class Basic(C.Structure):
            _fields_ = [('process_time', C.c_int64), ('job_time', C.c_int64),
                        ('flags', W.DWORD), ('min_ws', C.c_size_t), ('max_ws', C.c_size_t),
                        ('active', W.DWORD), ('affinity', C.c_size_t),
                        ('priority', W.DWORD), ('scheduling', W.DWORD)]
        class Extended(C.Structure):
            _fields_ = [('basic', Basic), ('io', C.c_uint64 * 6),
                        ('process_memory', C.c_size_t), ('job_memory', C.c_size_t),
                        ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]
        self.handle = self.k.CreateJobObjectW(None, None)
        self.worker = None
        self.gate = None
        self.gate_name = 'Local\\WoodStringAnalysis-' + uuid.uuid4().hex
        if not self.handle:
            raise C.WinError(C.get_last_error())
        limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)):
            error = C.WinError(C.get_last_error()); self.close(); raise error
        self.gate = self.k.CreateEventW(None, True, False, self.gate_name)
        if not self.gate:
            error = C.WinError(C.get_last_error()); self.close(); raise error

    def release_worker(self):
        if not self.k.SetEvent(self.gate):
            raise C.WinError(C.get_last_error())

    def assign(self, handle):
        belongs = W.BOOL()
        if not self.k.IsProcessInJob(handle, self.handle, C.byref(belongs)):
            raise C.WinError(C.get_last_error())
        if belongs.value:
            return
        if not self.k.AssignProcessToJobObject(self.handle, handle):
            raise C.WinError(C.get_last_error())

    def attach_worker(self, pid):
        # QUERY_INFORMATION | VM_READ | SET_QUOTA | TERMINATE
        handle = self.k.OpenProcess(0x100 | 0x400 | 0x10 | 1, False, pid)
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            self.assign(handle)
        except BaseException:
            self.k.CloseHandle(handle)
            raise
        self.worker = handle

    def peak_memory(self):
        class Counters(C.Structure):
            _fields_ = [('cb', W.DWORD), ('faults', W.DWORD)] + [
                (n, C.c_size_t) for n in ('peak', 'working', 'a', 'b', 'c', 'd', 'e', 'f')]
        counters = Counters(); counters.cb = C.sizeof(counters)
        fn = self.p.GetProcessMemoryInfo
        fn.argtypes = [W.HANDLE, C.POINTER(Counters), W.DWORD]; fn.restype = W.BOOL
        if not fn(self.worker, C.byref(counters), counters.cb):
            raise C.WinError(C.get_last_error())
        return counters.peak

    def close(self):
        if self.handle:
            # Close kills members even if TerminateJobObject itself reports error.
            self.k.TerminateJobObject(self.handle, 1)
            self.k.CloseHandle(self.handle); self.handle = None
        if self.worker:
            self.k.CloseHandle(self.worker); self.worker = None
        if self.gate:
            self.k.CloseHandle(self.gate); self.gate = None


def _read_messages(stream, messages):
    try:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                messages.put(('eof', None)); return
            if len(line) > MAX_LINE or not line.endswith(b'\n'):
                raise ValueError('Worker message exceeds limit or is incomplete')
            def invalid_constant(value): raise ValueError('Nonfinite JSON value')
            value = json.loads(line, parse_constant=invalid_constant)
            if not isinstance(value, dict):
                raise ValueError('Worker messages must be JSON objects')
            messages.put(('message', value))
    except Exception as exc:
        messages.put(('error', str(exc)))


def run_worker(module, request, *, python=sys.executable, cancel=lambda: False,
               progress=lambda event: None, wall_seconds=120, memory_bytes=4*1024**3):
    """Run an application-owned module; clean up on all outcomes/callback errors.

    Callback events are worker-emitted dicts, never calculated fake progress.
    Cancellation raises Cancelled; wall/memory thresholds raise LimitExceeded;
    protocol/monitor/worker failures raise ManagedProcessError. Metrics include
    actual PID, launcher PID, sampled peak bytes and total elapsed seconds.
    Application must keep module and interpreter selection out of user input.
    """
    if not isinstance(module, str) or not re.fullmatch(r'(audio|tests)\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', module):
        raise ValueError('Expected a fixed internal audio/tests module')
    if not isinstance(request, dict):
        raise ValueError('Worker request must be a dict')
    if not math.isfinite(wall_seconds) or wall_seconds <= 0 or type(memory_bytes) is not int or memory_bytes <= 0:
        raise ValueError('Positive finite worker limits required')
    payload = json.dumps(request, allow_nan=False, ensure_ascii=True)
    if len(payload) > 8192:
        raise ValueError('Worker request too large')
    if cancel():
        raise Cancelled('Analysis cancelled before launch')
    job = _WindowsJob(); proc = None; reader = None
    started = time.monotonic(); actual = None; peak = 0; result = None; events = 0
    messages = queue.Queue(maxsize=128)
    try:
        proc = subprocess.Popen([str(python), '-m', 'audio.managed_process', '--child', module, payload, job.gate_name],
                                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(int(proc._handle))
        reader = threading.Thread(target=_read_messages, args=(proc.stdout, messages), daemon=True)
        reader.start()
        while True:
            if cancel():
                raise Cancelled('Analysis cancelled')
            if time.monotonic() - started > wall_seconds:
                raise LimitExceeded('Analysis wall-time limit exceeded')
            if actual is not None:
                peak = max(peak, job.peak_memory())  # Any observation failure fails closed.
                if peak > memory_bytes:
                    raise LimitExceeded('Analysis sampled memory threshold exceeded')
            try:
                kind, event = messages.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            if kind == 'error':
                raise ManagedProcessError('Invalid worker protocol: ' + event)
            if kind == 'eof':
                raise ManagedProcessError('Worker exited without a successful result')
            elif actual is None:
                if event.get('type') != 'hello' or type(event.get('pid')) is not int or event['pid'] <= 0:
                    raise ManagedProcessError('Missing actual worker PID handshake')
                actual = event['pid']; job.attach_worker(actual)
                if cancel(): raise Cancelled('Analysis cancelled before ownership acknowledgment')
                job.release_worker()
            elif event.get('type') == 'progress' and result is None:
                events += 1
                if events > MAX_EVENTS:
                    raise ManagedProcessError('Worker progress event limit exceeded')
                if not isinstance(event.get('value'), dict):
                    raise ManagedProcessError('Invalid worker progress')
                progress(event['value'])
            elif event.get('type') == 'result' and result is None and isinstance(event.get('value'), dict):
                result = event['value']
                return {'result': result, 'metrics': {'worker_pid': actual, 'launcher_pid': proc.pid,
                        'peak_working_set_bytes': peak, 'elapsed_seconds': time.monotonic()-started}}
            elif event.get('type') == 'error':
                raise ManagedProcessError('Analysis worker failed: ' + str(event.get('message', 'unknown')))
            else:
                raise ManagedProcessError('Unexpected worker message')
    except (Cancelled, LimitExceeded, ManagedProcessError):
        raise
    except Exception as exc:
        raise ManagedProcessError('Worker supervision failed: ' + str(exc)) from exc
    finally:
        try:
            if proc and proc.stdin:
                proc.stdin.close()
        finally:
            job.close()
        if proc:
            if proc.poll() is None:
                proc.kill()  # Also covers a failure before assignment to the job.
            proc.wait(timeout=5)
            if reader:
                # Drain bounded queue so the reader cannot remain blocked in put.
                deadline = time.monotonic() + 2
                while reader.is_alive() and time.monotonic() < deadline:
                    try: messages.get(timeout=.025)
                    except queue.Empty: pass
                reader.join(timeout=.1)
            proc.stdout.close()


def _wait_for_release(name):
    if not re.fullmatch(r'Local\\WoodStringAnalysis-[0-9a-f]{32}', name):
        raise ValueError('Invalid internal ownership gate')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.OpenEventW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]; kernel.OpenEventW.restype = W.HANDLE
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; kernel.WaitForSingleObject.restype = W.DWORD
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.OpenEventW(0x100000, False, name)  # SYNCHRONIZE only
    if not handle: raise C.WinError(C.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0xffffffff) != 0:
            raise C.WinError(C.get_last_error())
    finally: kernel.CloseHandle(handle)


def parent_pipe_lost(stop=None):
    """Wait without blocking stdio locks; return False on requested shutdown.

    EOF, unexpected data, or an inspection error means parent loss. The pipe
    carries no requests. Polling bounds liveness detection to about 50 ms.
    """
    if os.name != 'nt':
        os.read(sys.stdin.fileno(), 1)
        return True
    import msvcrt
    parent_handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.PeekNamedPipe.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.DWORD), C.c_void_p]
    kernel.PeekNamedPipe.restype = W.BOOL
    count = W.DWORD()
    while stop is None or not stop.is_set():
        if not kernel.PeekNamedPipe(parent_handle, None, 0, None, C.byref(count), None):
            return True
        if count.value: return True
        if stop is None: time.sleep(.05)
        else: stop.wait(.05)
    return False


def _child(module, request, gate_name):
    def parent_watch():
        try: parent_pipe_lost()
        finally: os._exit(73)
    threading.Thread(target=parent_watch, daemon=True).start()
    if not re.fullmatch(r'(audio|tests)\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', module) or not isinstance(request, dict):
        return 1
    wire = sys.stdout
    lock = threading.Lock()
    def send(value):
        data = json.dumps(value, allow_nan=False, ensure_ascii=True)
        if len(data.encode('utf-8')) + 1 > MAX_LINE:
            raise ValueError('Worker message too large')
        with lock:
            wire.write(data+'\n'); wire.flush()
    # Keep accidental analysis prints away from the structured protocol.
    sys.stdout = sys.stderr
    send({'type': 'hello', 'pid': os.getpid()})
    try:
        # The helper must not import analysis code or allow its descendants
        # before the supervisor confirms actual-PID Job Object ownership.
        _wait_for_release(gate_name)
        result = importlib.import_module(module).run(request, lambda event: send({'type':'progress','value':event}))
        if not isinstance(result, dict): raise TypeError('Worker result must be a dict')
        send({'type': 'result', 'value': result})
        # Retain the actual process until the parent has read its result and
        # memory counters. Parent closes its liveness pipe on every outcome.
        threading.Event().wait()
    except BaseException as exc:
        send({'type': 'error', 'message': type(exc).__name__ + ': ' + str(exc)})
        return 1
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--child':
        raise SystemExit('Internal managed worker entry point only')
    raise SystemExit(_child(sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]))
