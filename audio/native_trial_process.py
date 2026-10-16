"""Bounded native execution for the application's private local-arranger trial.

INTERNAL ONLY: argv is a fixed application-owned vector, never a request field.
There is deliberately no command-line entry point or executable discovery here.
Call only from a module imported by audio.managed_process after its ownership
gate. The native process is created suspended, inherits that outer Windows Job,
and is attached to an inner kill-on-close Job before its primary thread resumes.
The inner Job observes its actual PID and owns all subsequently born descendants.

Memory is a sampled peak-working-set stop threshold, not a reservation. This
module does not parse model output or attest model/runtime integrity.
"""
import ctypes as C
from ctypes import wintypes as W
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from audio.managed_process import _WindowsJob


MAX_WALL_SECONDS = 120
MAX_MEMORY_BYTES = 6 * 1024**3
MAX_OUTPUT_BYTES = 1024**2
POLL_SECONDS = .025
READ_BYTES = 16384
CREATE_SUSPENDED = 0x00000004
JOB_DRAIN_SECONDS = 3
MAX_OWNED_PROCESSES = 1024


class NativeTrialError(RuntimeError):
    """A failed trial with terminal metrics; partial capture files are retained."""
    def __init__(self, reason, message, metrics=None):
        super().__init__(message)
        self.reason = reason
        self.metrics = dict(metrics or {})
        self.metrics['reason'] = reason


class NativeTrialCancelled(NativeTrialError):
    pass


class NativeTrialLimitExceeded(NativeTrialError):
    pass


class _ThreadEntry(C.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('thread_id', W.DWORD),
                ('owner_pid', W.DWORD), ('base_priority', W.LONG),
                ('delta_priority', W.LONG), ('flags', W.DWORD)]


class _PrimaryThread:
    """Documented Toolhelp lookup of the sole CREATE_SUSPENDED primary thread.

    Popen closes CreateProcess's initial thread handle. A read-only system thread
    snapshot locates exactly one thread of our still-suspended, owned PID. Open
    only SUSPEND_RESUME and QUERY_LIMITED_INFORMATION rights and verify its PID.
    No undocumented Nt* entry points, debugger or privilege adjustment is used.

    https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
    https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot
    https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-resumethread
    """
    def __init__(self, pid):
        self.handle = None
        self.thread_id = None
        self.k = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateToolhelp32Snapshot': (W.HANDLE, [W.DWORD, W.DWORD]),
            'Thread32First': (W.BOOL, [W.HANDLE, C.POINTER(_ThreadEntry)]),
            'Thread32Next': (W.BOOL, [W.HANDLE, C.POINTER(_ThreadEntry)]),
            'OpenThread': (W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            'GetProcessIdOfThread': (W.DWORD, [W.HANDLE]),
            'ResumeThread': (W.DWORD, [W.HANDLE]),
            'CloseHandle': (W.BOOL, [W.HANDLE]),
        }
        for name, (restype, args) in signatures.items():
            function = getattr(self.k, name); function.restype = restype; function.argtypes = args
        snapshot = self.k.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == C.c_void_p(-1).value or not snapshot:
            raise C.WinError(C.get_last_error())
        try:
            entry = _ThreadEntry(); entry.size = C.sizeof(entry)
            present = self.k.Thread32First(snapshot, C.byref(entry))
            if not present:
                raise C.WinError(C.get_last_error())
            matches = []
            while present:
                if entry.owner_pid == pid:
                    matches.append(entry.thread_id)
                entry.size = C.sizeof(entry)
                present = self.k.Thread32Next(snapshot, C.byref(entry))
            if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES is normal enumeration EOF.
                raise C.WinError(C.get_last_error())
            if len(matches) != 1:
                raise NativeTrialError('ownership', 'Expected exactly one suspended native primary thread')
            self.thread_id = matches[0]
            self.handle = self.k.OpenThread(0x0002 | 0x0800, False, self.thread_id)
            if not self.handle:
                raise C.WinError(C.get_last_error())
            if self.k.GetProcessIdOfThread(self.handle) != pid:
                raise NativeTrialError('ownership', 'Suspended native thread owner PID does not match')
        except BaseException:
            self.close()
            raise
        finally:
            if not self.k.CloseHandle(snapshot):
                error = C.WinError(C.get_last_error())
                self.close()
                raise error

    def resume(self):
        previous_count = self.k.ResumeThread(self.handle)
        if previous_count == 0xffffffff:
            raise C.WinError(C.get_last_error())
        if previous_count != 1:
            raise NativeTrialError('ownership', 'Native primary thread was not suspended exactly once')

    def close(self):
        if self.handle is not None:
            handle = self.handle
            if not self.k.CloseHandle(handle):
                raise C.WinError(C.get_last_error())
            self.handle = None


class _JobAccounting(C.Structure):
    _fields_ = [(name, C.c_int64) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        (name, W.DWORD) for name in ('page_faults', 'total_processes', 'active_processes', 'terminated_processes')]


class _JobProcessIds(C.Structure):
    _fields_ = [('assigned', W.DWORD), ('listed', W.DWORD),
                ('pids', C.c_size_t * MAX_OWNED_PROCESSES)]


def _active_job_processes(job):
    function = job.k.QueryInformationJobObject
    function.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    function.restype = W.BOOL
    accounting = _JobAccounting()
    if not function(job.handle, 1, C.byref(accounting), C.sizeof(accounting), None):
        raise C.WinError(C.get_last_error())
    return accounting.active_processes


def _stop_owned_job(job):
    """Terminate, wait owned process handles and observe zero active members.

    Retaining SYNCHRONIZE handles before termination lets Windows finish process
    rundown before we release ownership; exit-code/accounting changes alone can
    precede the final directory/file-handle release. A bounded PID inventory is
    queried only from this owned Job, never from unrelated system processes.
    """
    handles = []
    try:
        function = job.k.QueryInformationJobObject
        function.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
        function.restype = W.BOOL
        inventory = _JobProcessIds()
        if not function(job.handle, 3, C.byref(inventory), C.sizeof(inventory), None):
            raise C.WinError(C.get_last_error())
        if inventory.assigned > MAX_OWNED_PROCESSES or inventory.listed > MAX_OWNED_PROCESSES:
            raise NativeTrialError('cleanup_error', 'Owned native Job process inventory exceeds its bound')
        for pid in inventory.pids[:inventory.listed]:
            handle = job.k.OpenProcess(0x100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            if not handle:
                error = C.get_last_error()
                if error == 87: continue  # Already destroyed between the owned snapshot and OpenProcess.
                raise C.WinError(error)
            handles.append(handle)
            belongs = W.BOOL()
            if not job.k.IsProcessInJob(handle, job.handle, C.byref(belongs)):
                raise C.WinError(C.get_last_error())
            if not belongs.value:
                raise NativeTrialError('cleanup_error', 'Owned native Job PID changed ownership before teardown')
        if not job.k.TerminateJobObject(job.handle, 1):
            raise C.WinError(C.get_last_error())
        deadline = time.monotonic() + JOB_DRAIN_SECONDS
        job.k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
        job.k.WaitForSingleObject.restype = W.DWORD
        for handle in handles:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            status = job.k.WaitForSingleObject(handle, remaining_ms)
            if status == 0x102:
                raise NativeTrialError('cleanup_error', 'Owned native process did not finish termination')
            if status != 0:
                raise C.WinError(C.get_last_error())
        while _active_job_processes(job):
            if time.monotonic() >= deadline:
                raise NativeTrialError('cleanup_error', 'Owned native Job did not become empty')
            time.sleep(POLL_SECONDS)
    finally:
        try:
            for handle in handles:
                job.k.CloseHandle(handle)
        finally:
            job.close()


def _assert_owned_worker():
    """Require the existing managed bootstrap's signaled gate and Job ownership."""
    if os.name != 'nt':
        raise NativeTrialError('ownership', 'Native trial requires a Windows managed worker')
    if (len(sys.argv) != 5 or sys.argv[1] != '--child'
            or not re.fullmatch(r'(audio|tests)\.[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', sys.argv[2])
            or not re.fullmatch(r'Local\\WoodStringAnalysis-[0-9a-f]{32}', sys.argv[4])):
        raise NativeTrialError('ownership', 'Native trial must run inside the managed worker bootstrap')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []; kernel.GetCurrentProcess.restype = W.HANDLE
    kernel.IsProcessInJob.argtypes = [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)]
    kernel.IsProcessInJob.restype = W.BOOL
    kernel.OpenEventW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]; kernel.OpenEventW.restype = W.HANDLE
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]; kernel.WaitForSingleObject.restype = W.DWORD
    kernel.CloseHandle.argtypes = [W.HANDLE]; kernel.CloseHandle.restype = W.BOOL
    owned = W.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, C.byref(owned)):
        raise NativeTrialError('ownership', 'Cannot establish current managed worker Job ownership')
    if not owned.value:
        raise NativeTrialError('ownership', 'Current worker has no Windows Job ownership')
    gate = kernel.OpenEventW(0x100000, False, sys.argv[4])
    if not gate:
        raise NativeTrialError('ownership', 'Managed ownership acknowledgment is unavailable')
    try:
        if kernel.WaitForSingleObject(gate, 0) != 0:
            raise NativeTrialError('ownership', 'Managed ownership acknowledgment has not been released')
    finally:
        kernel.CloseHandle(gate)


def _environment():
    """Keep OS necessities; exclude model, accelerator, proxy and Python overrides."""
    allowed = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP', 'LOCALAPPDATA',
               'APPDATA', 'USERPROFILE', 'USERNAME', 'PROCESSOR_ARCHITECTURE',
               'NUMBER_OF_PROCESSORS', 'PATHEXT', 'OS', 'PROGRAMDATA',
               'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432'}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system_root = result.get('SystemRoot') or result.get('SYSTEMROOT') or os.environ.get('SystemRoot')
    if system_root:
        result['PATH'] = os.pathsep.join((str(Path(system_root) / 'System32'), str(system_root)))
    result.update({'HF_HUB_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
                   'TRANSFORMERS_OFFLINE': '1', 'CUDA_VISIBLE_DEVICES': '',
                   'HIP_VISIBLE_DEVICES': '', 'ROCR_VISIBLE_DEVICES': ''})
    return result


def _validate(argv, cwd, stdout_path, stderr_path, cancel, wall_seconds, memory_bytes, output_limit_each):
    if (not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= 128
            or any(not isinstance(value, str) or not value or '\0' in value for value in argv)
            or sum(map(len, argv)) > 30000):
        raise ValueError('Expected a bounded fixed internal argument vector')
    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('Internal native executable must be an existing absolute file')
    if not callable(cancel):
        raise ValueError('Cancellation callback required')
    if (isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float))
            or not math.isfinite(wall_seconds) or not 0 < wall_seconds <= MAX_WALL_SECONDS):
        raise ValueError('Native wall limit must be positive and at most 120 seconds')
    if type(memory_bytes) is not int or not 0 < memory_bytes <= MAX_MEMORY_BYTES:
        raise ValueError('Native memory stop threshold must be positive and at most 6 GiB')
    if type(output_limit_each) is not int or not 0 < output_limit_each <= MAX_OUTPUT_BYTES:
        raise ValueError('Each capture limit must be positive and at most 1 MiB')
    root = Path(cwd)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('Native working directory must be an existing absolute directory')
    root = root.resolve()
    outputs = []
    for value in (stdout_path, stderr_path):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError('Native capture paths must be absolute')
        if path.is_symlink():
            raise ValueError('Native capture paths cannot be links')
        path = path.resolve()
        if (not path.is_relative_to(root) or path == root or not path.parent.is_dir()
                or path.exists() or path.is_symlink()):
            raise ValueError('Native capture paths must be absent owned files beneath the working directory')
        outputs.append(path)
    if outputs[0] == outputs[1]:
        raise ValueError('Native captures must have distinct paths')
    return list(argv), root, outputs


def _capture(stream, destination, label, limit, messages, counts):
    """Only bounded bytes reach disk; one terminal message per reader."""
    failure = None
    try:
        while True:
            data = stream.read1(READ_BYTES)
            if not data:
                break
            remaining = limit - counts[label]
            accepted = data[:remaining]
            if accepted:
                destination.write(accepted)
                counts[label] += len(accepted)
                destination.flush()
            if len(data) > remaining:
                failure = ('output_limit', label)
                break
        destination.flush()
    except BaseException as exc:
        failure = ('reader_error', label + ': ' + type(exc).__name__ + ': ' + str(exc))
    finally:
        for resource in (destination, stream):
            try:
                resource.close()
            except BaseException as exc:
                failure = ('reader_error', label + ' close: ' + type(exc).__name__ + ': ' + str(exc))
        messages.put(failure or ('eof', label))


def run_native(argv, *, cwd, stdout_path, stderr_path, cancel=lambda: False,
               wall_seconds=MAX_WALL_SECONDS, memory_bytes=MAX_MEMORY_BYTES,
               output_limit_each=MAX_OUTPUT_BYTES, before_resume=None):
    """Run a fixed internal native vector, capture silently and always kill/reap.

    Returns terminal metrics on exit code zero. Nonzero exit, ownership, launch,
    observation or capture failure raises NativeTrialError. Cancellation and
    sampled wall/memory/output limits have specific subclasses. Partial logs are
    retained with exclusive creation and a strict per-stream byte ceiling.
    Callbacks must return promptly. No audio/model/executable is selected here.
    """
    argv, root, outputs = _validate(argv, cwd, stdout_path, stderr_path, cancel,
                                    wall_seconds, memory_bytes, output_limit_each)
    if before_resume is not None and not callable(before_resume):
        raise TypeError('Expected an internal pre-resume observation callback')
    started = time.monotonic()
    metrics = {'native_pid': None, 'returncode': None, 'elapsed_seconds': 0.,
               'peak_working_set_bytes': 0, 'stdout_bytes': 0, 'stderr_bytes': 0,
               'primary_thread_id': None, 'primary_thread_resumed': False,
               'primary_thread_handle_closed': False, 'owned_job_empty': False}
    counts = {'stdout': 0, 'stderr': 0}
    messages = queue.SimpleQueue(); readers = []; destinations = []
    job = None; proc = None; primary = None; failure = None; finished = set()
    try:
        _assert_owned_worker()
        if cancel():
            raise NativeTrialCancelled('cancelled', 'Native trial cancelled before launch')
        # Exclusive creation is the authority, even if a file appears after validation.
        for path in outputs:
            destinations.append(path.open('xb'))
        job = _WindowsJob()
        proc = subprocess.Popen(argv, cwd=root, env=_environment(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, close_fds=True, shell=False,
                                creationflags=subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED)
        metrics['native_pid'] = proc.pid
        # No native instruction executes before inner ownership. Inheriting the
        # outer Job alone is insufficient: a pre-assignment descendant would not
        # join the subsequently attached inner Job. CREATE_SUSPENDED closes that
        # race; lookup/assignment/capture/resume failures kill the suspended PID.
        primary = _PrimaryThread(proc.pid)
        metrics['primary_thread_id'] = primary.thread_id
        job.attach_worker(proc.pid)
        for label, stream, destination in zip(('stdout', 'stderr'), (proc.stdout, proc.stderr), destinations):
            reader = threading.Thread(target=_capture,
                                      args=(stream, destination, label, output_limit_each, messages, counts),
                                      name='local-arranger-' + label, daemon=True)
            reader.start(); readers.append(reader)
        if cancel():
            raise NativeTrialCancelled('cancelled', 'Native trial cancelled before native execution')
        if time.monotonic() - started > wall_seconds:
            raise NativeTrialLimitExceeded('wall_limit', 'Native trial wall-time limit exceeded before execution')
        metrics['peak_working_set_bytes'] = job.peak_memory()
        if metrics['peak_working_set_bytes'] > memory_bytes:
            raise NativeTrialLimitExceeded('memory_limit', 'Native trial sampled memory threshold exceeded before execution')
        if before_resume is not None:
            before_resume()
        if time.monotonic() - started > wall_seconds:
            raise NativeTrialLimitExceeded('wall_limit', 'Native trial wall-time limit exceeded during pre-resume observation')
        primary.resume()
        metrics['primary_thread_resumed'] = True
        primary.close()
        metrics['primary_thread_handle_closed'] = True
        primary = None
        while True:
            while True:
                try:
                    kind, value = messages.get_nowait()
                except queue.Empty:
                    break
                if kind == 'output_limit':
                    raise NativeTrialLimitExceeded('output_limit', 'Native ' + value + ' capture limit exceeded')
                if kind == 'reader_error':
                    raise NativeTrialError('reader_error', 'Native capture failed: ' + value)
                finished.add(value)
            if cancel():
                raise NativeTrialCancelled('cancelled', 'Native trial cancelled')
            if time.monotonic() - started > wall_seconds:
                raise NativeTrialLimitExceeded('wall_limit', 'Native trial wall-time limit exceeded')
            if job is not None:
                metrics['peak_working_set_bytes'] = max(metrics['peak_working_set_bytes'], job.peak_memory())
                if metrics['peak_working_set_bytes'] > memory_bytes:
                    raise NativeTrialLimitExceeded('memory_limit', 'Native trial sampled memory threshold exceeded')
            code = proc.poll()
            if code is not None:
                metrics['returncode'] = code
                # Close ownership of any remaining descendants; this also releases
                # pipes held by a descendant after the direct executable exits.
                if job is not None:
                    closing = job; job = None
                    _stop_owned_job(closing)
                    metrics['owned_job_empty'] = True
                if len(finished) == 2:
                    if code != 0:
                        raise NativeTrialError('nonzero_exit', 'Native trial exited with code ' + str(code))
                    break
            time.sleep(POLL_SECONDS)
    except NativeTrialError as exc:
        failure = exc
    except BaseException as exc:
        failure = NativeTrialError('supervision_error', 'Native trial supervision failed: '
                                   + type(exc).__name__ + ': ' + str(exc))
    finally:
        # Closing the inner job kills descendants even when observation failed.
        # The inherited outer job independently kills them on managed parent loss.
        cleanup_errors = []
        if job is not None:
            try:
                _stop_owned_job(job)
                metrics['owned_job_empty'] = True
            except BaseException as exc: cleanup_errors.append(str(exc))
        if primary is not None:
            try:
                primary.close()
                metrics['primary_thread_handle_closed'] = True
            except BaseException as exc: cleanup_errors.append(str(exc))
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()  # Also covers failure before inner assignment.
                proc.wait(timeout=5)
                metrics['returncode'] = proc.returncode
            except BaseException as exc:
                cleanup_errors.append(str(exc))
        for reader in readers:
            reader.join(timeout=2)
            if reader.is_alive(): cleanup_errors.append('capture reader did not terminate')
        for resource in destinations + ([proc.stdout, proc.stderr] if proc is not None else []):
            try:
                if resource is not None and not resource.closed: resource.close()
            except BaseException as exc: cleanup_errors.append(str(exc))
        # A reader failure that arrived during normal process teardown also fails.
        while True:
            try: kind, value = messages.get_nowait()
            except queue.Empty: break
            if failure is None and kind != 'eof':
                error_type = NativeTrialLimitExceeded if kind == 'output_limit' else NativeTrialError
                failure = error_type(kind, 'Native capture terminated: ' + str(value))
        metrics.update(elapsed_seconds=time.monotonic() - started,
                       stdout_bytes=counts['stdout'], stderr_bytes=counts['stderr'])
        if cleanup_errors:
            metrics['cleanup_errors'] = cleanup_errors
            if failure is None:
                failure = NativeTrialError('cleanup_error', 'Native trial cleanup failed')
    if failure is not None:
        failure.metrics = dict(metrics, reason=failure.reason)
        raise failure
    return dict(metrics, reason='exit')
