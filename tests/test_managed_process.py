"""Silent lifecycle tests; explicit import_probe diagnostics never execute models."""
import ctypes
from ctypes import wintypes
import json
import os
import queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from audio import managed_process as managed


def run(request, emit):
    """Internal silent stub loaded by the real child bootstrap."""
    if request.get('pid_file'):
        Path(request['pid_file']).write_text(str(os.getpid()), encoding='utf-8')
    emit({'phase': 'ready', 'pid': os.getpid()})
    mode = request.get('mode', 'success')
    if mode == 'import_probe':
        # Explicit diagnostic only; normal lifecycle suite never imports models.
        import faulthandler
        with open(request['trace_path'], 'w', encoding='utf-8') as trace:
            faulthandler.dump_traceback_later(10, file=trace)
            try:
                import torch
                import librosa
                _ = librosa.pyin
                return {'ok': True, 'torch': torch.__version__, 'librosa': librosa.__version__}
            finally: faulthandler.cancel_dump_traceback_later()
    if mode == 'stdin_metadata':
        # Libraries can inspect standard-stream metadata during import.
        return {'ok': True, 'seekable': sys.stdin.buffer.seekable(),
                'isatty': os.isatty(sys.stdin.fileno())}
    if mode == 'descendant':
        child = subprocess.Popen([sys._base_executable,'-c','import time; time.sleep(60)'],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        Path(request['descendant_file']).write_text(str(child.pid), encoding='utf-8')
        time.sleep(60)
    if mode == 'error': raise ValueError('intentional stub failure')
    if mode == 'oversize': emit({'text': 'x' * managed.MAX_LINE})
    if mode == 'flood':
        for i in range(managed.MAX_EVENTS + 1): emit({'index': i})
    if mode == 'sleep': time.sleep(60)
    time.sleep(.05)
    return {'ok': True, 'pid': os.getpid()}


def alive(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally: kernel.CloseHandle(handle)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object lifecycle tests')
class ManagedProcessTests(unittest.TestCase):
    module = 'tests.test_managed_process'

    def call(self, request=None, **kwargs):
        return managed.run_worker(self.module, request or {}, wall_seconds=kwargs.pop('wall_seconds', 8), **kwargs)

    def gone(self, pid):
        deadline = time.monotonic() + 3
        while alive(pid) and time.monotonic() < deadline: time.sleep(.02)
        self.assertFalse(alive(pid), f'Owned process {pid} still alive')

    def capture_pids(self):
        pids = []
        original = managed._WindowsJob.attach_worker
        def attach(job, pid):
            pids.append(pid)
            return original(job, pid)
        return pids, mock.patch.object(managed._WindowsJob, 'attach_worker', attach)

    def test_success_real_venv_launcher_and_progress(self):
        self.assertNotEqual(sys.prefix, sys.base_prefix, 'Run tests with application venv Python')
        events = []
        result = self.call(progress=events.append)
        metrics = result['metrics']
        self.assertTrue(result['result']['ok'])
        self.assertNotEqual(metrics['worker_pid'], metrics['launcher_pid'])
        self.assertEqual(events[0]['pid'], metrics['worker_pid'])
        self.assertGreater(metrics['peak_working_set_bytes'], 0)
        self.gone(metrics['worker_pid']); self.gone(metrics['launcher_pid'])

    def test_standard_input_metadata_remains_available(self):
        result = self.call({'mode': 'stdin_metadata'})
        self.assertIsInstance(result['result']['seekable'], bool)
        self.assertFalse(result['result']['isatty'])
        self.gone(result['metrics']['worker_pid'])

    def test_cancel_before_launch(self):
        with mock.patch.object(managed.subprocess, 'Popen') as spawn:
            with self.assertRaises(managed.Cancelled): self.call(cancel=lambda: True)
            spawn.assert_not_called()

    def test_cancel_after_launch_before_hello(self):
        cancel = threading.Event(); launchers = []
        original_assign = managed._WindowsJob.assign
        original_spawn = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original_spawn(*args, **kwargs); launchers.append(process.pid); return process
        def assign(job, handle):
            original_assign(job, handle); cancel.set()
        with mock.patch.object(managed.subprocess, 'Popen', spawn), mock.patch.object(managed._WindowsJob, 'assign', assign):
            started = time.monotonic()
            with self.assertRaises(managed.Cancelled): self.call({'mode':'sleep'}, cancel=cancel.is_set)
        self.assertLess(time.monotonic()-started, 3)
        self.gone(launchers[0])

    def test_cancel_after_hello(self):
        cancel = threading.Event(); events = []
        def progress(event): events.append(event); cancel.set()
        started = time.monotonic()
        with self.assertRaises(managed.Cancelled): self.call({'mode':'sleep'}, cancel=cancel.is_set, progress=progress)
        self.assertLess(time.monotonic()-started, 3); self.gone(events[0]['pid'])

    def test_monitor_failure_cleans_actual_process(self):
        pids, capture = self.capture_pids()
        with capture, mock.patch.object(managed._WindowsJob, 'peak_memory', side_effect=OSError('injected monitor fault')):
            with self.assertRaisesRegex(managed.ManagedProcessError, 'monitor fault'): self.call({'mode':'sleep'})
        self.assertEqual(len(pids), 1); self.gone(pids[0])

    def test_analysis_waits_for_actual_ownership_and_denial_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory)/'module-started'
            original = managed._WindowsJob.attach_worker
            pids = []
            for deny in (False, True):
                if marker.exists(): marker.unlink()
                def attach(job, pid):
                    pids.append(pid)
                    time.sleep(.15)
                    self.assertFalse(marker.exists(), 'Analysis started before ownership acknowledgment')
                    if deny: raise OSError('actual ownership denied')
                    return original(job, pid)
                with mock.patch.object(managed._WindowsJob, 'attach_worker', attach):
                    if deny:
                        with self.assertRaisesRegex(managed.ManagedProcessError, 'actual ownership denied'):
                            self.call({'pid_file':str(marker)})
                        self.assertFalse(marker.exists())
                    else:
                        self.call({'pid_file':str(marker)})
                        self.assertTrue(marker.exists())
                self.gone(pids[-1])

    def test_job_assignment_failure_stops_launcher(self):
        launchers = []; original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs); launchers.append(process.pid); return process
        with mock.patch.object(managed.subprocess, 'Popen', spawn), mock.patch.object(managed._WindowsJob, 'assign', side_effect=OSError('ownership denied')):
            with self.assertRaisesRegex(managed.ManagedProcessError, 'ownership denied'): self.call({'mode':'sleep'})
        self.gone(launchers[0])

    def test_wall_and_memory_limits(self):
        for limits in ({'wall_seconds': .4}, {'memory_bytes': 1}):
            with self.subTest(limits=limits):
                pids, capture = self.capture_pids()
                with capture:
                    with self.assertRaises(managed.LimitExceeded): self.call({'mode':'sleep'}, **limits)
                for pid in pids: self.gone(pid)

    def test_callback_exception_cleans_worker(self):
        pids, capture = self.capture_pids()
        def progress(event): raise RuntimeError('callback fault')
        with capture:
            with self.assertRaisesRegex(managed.ManagedProcessError, 'callback fault'): self.call({'mode':'sleep'}, progress=progress)
        self.gone(pids[0])

    def test_worker_error_and_protocol_bounds(self):
        for mode in ('error', 'oversize', 'flood'):
            with self.subTest(mode=mode):
                pids, capture = self.capture_pids()
                with capture:
                    with self.assertRaises(managed.ManagedProcessError): self.call({'mode':mode})
                for pid in pids: self.gone(pid)
        with self.assertRaises(ValueError): self.call({'x':'x'*9000})
        with self.assertRaises(ValueError): managed.run_worker('os', {})

    def test_forced_supervisor_death_kills_actual_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory)/'actual-pid.txt'
            launcherfile = Path(directory)/'launcher-pid.txt'
            descendantfile = Path(directory)/'descendant-pid.txt'
            code = '''from audio import managed_process as m
import sys
from pathlib import Path
original = m.subprocess.Popen
def spawn(*args, **kwargs):
    child = original(*args, **kwargs)
    Path(sys.argv[3]).write_text(str(child.pid))
    return child
m.subprocess.Popen = spawn
m.run_worker("tests.test_managed_process", {"mode":"descendant","pid_file":sys.argv[1],"descendant_file":sys.argv[4]},python=sys.argv[2],wall_seconds=30)
'''
            # Base interpreter avoids a second venv redirector for the supervisor
            # so kill() targets the real parent. Its child still uses the venv.
            parent = subprocess.Popen([sys._base_executable,'-c',code,str(pidfile),sys.executable,str(launcherfile),str(descendantfile)],
                                      cwd=Path(__file__).resolve().parents[1],
                                      stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic()+5
                while not descendantfile.exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue(descendantfile.exists(), 'Stub descendant never started')
                pid = int(pidfile.read_text()); self.assertTrue(alive(pid))
                descendant = int(descendantfile.read_text()); self.assertTrue(alive(descendant))
                launcher = int(launcherfile.read_text())
                parent.kill(); parent.wait(timeout=3); self.gone(pid); self.gone(launcher)
                self.gone(descendant)
            finally:
                if parent.poll() is None: parent.kill()
                parent.wait(timeout=3)

    def test_parent_pipe_eof_without_job(self):
        # Independently prove the fallback guard, without a Job Object supervisor.
        gate = managed._WindowsJob()
        child = subprocess.Popen([sys._base_executable,'-m','audio.managed_process','--child',
                                  self.module,json.dumps({'mode':'sleep'}),gate.gate_name],
                                 cwd=Path(__file__).resolve().parents[1],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        received = queue.Queue()
        reader = threading.Thread(target=lambda: received.put(child.stdout.readline()),daemon=True)
        reader.start()
        try:
            hello = json.loads(received.get(timeout=3))
            self.assertEqual(hello, {'type':'hello','pid':child.pid})
            child.stdin.close()
            self.assertEqual(child.wait(timeout=3),73)
            self.gone(child.pid)
        finally:
            if child.poll() is None: child.kill()
            child.wait(timeout=3); child.stdout.close()
            if not child.stdin.closed: child.stdin.close()
            reader.join(timeout=1)
            gate.close()

    def test_parent_death_while_actual_worker_waits_at_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory)/'waiting-pid'
            marker = Path(directory)/'module-started'
            code = '''from audio import managed_process as m
import sys, time
from pathlib import Path
def attach(job, pid):
    Path(sys.argv[1]).write_text(str(pid))
    time.sleep(60)
m._WindowsJob.attach_worker = attach
m.run_worker("tests.test_managed_process", {"pid_file":sys.argv[2]},python=sys.argv[3],wall_seconds=30)
'''
            parent = subprocess.Popen([sys._base_executable,'-c',code,str(pidfile),str(marker),sys.executable],
                                      cwd=Path(__file__).resolve().parents[1],stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline=time.monotonic()+5
                while not pidfile.exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue(pidfile.exists()); pid=int(pidfile.read_text())
                self.assertFalse(marker.exists())
                parent.kill(); parent.wait(timeout=3); self.gone(pid)
                self.assertFalse(marker.exists())
            finally:
                if parent.poll() is None: parent.kill()
                parent.wait(timeout=3)


if __name__ == '__main__': unittest.main()
