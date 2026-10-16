"""Silent native lifecycle checks with owned Python helpers, never model/audio."""
import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from audio import managed_process as managed
from audio import native_trial_process as native


HELPERS = {
    'success': "import sys; sys.stdout.buffer.write(b'good\\n'); sys.stderr.buffer.write(b'note\\n')",
    'nonzero': "import sys; sys.stdout.buffer.write(b'partial'); sys.exit(7)",
    'stdout_flood': "import sys,time; sys.stdout.buffer.write(b'x'*2000000); sys.stdout.flush(); time.sleep(60)",
    'stderr_flood': "import sys,time; sys.stderr.buffer.write(b'x'*2000000); sys.stderr.flush(); time.sleep(60)",
    'sleep': "import os,sys,time; print(os.getpid(),flush=True); time.sleep(60)",
    'descendant': """import os,sys,time,subprocess
from pathlib import Path
child = subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],
                         creationflags=subprocess.CREATE_NO_WINDOW)
Path(sys.argv[1]).write_text(str(child.pid))
print(os.getpid(),flush=True)
time.sleep(60)
""",
    'descendant_exit': """import os,sys,subprocess
from pathlib import Path
child = subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
Path(sys.argv[1]).write_text(str(child.pid))
print(os.getpid(),flush=True)
""",
}


def run(request, emit):
    """Existing managed bootstrap imports this fixed internal test helper."""
    directory = Path(request['directory'])
    (directory / 'worker-pid').write_text(str(os.getpid()))
    mode = request.get('mode', 'success')
    marker = directory / 'native-executed'
    code = "import sys; from pathlib import Path; Path(sys.argv[2]).write_text('executed')\n" + HELPERS[mode]
    argv = [sys._base_executable, '-I', '-u', '-c', code, str(directory / 'descendant-pid'), str(marker)]
    started = time.monotonic()
    cancel_after = request.get('cancel_after')
    cancel = lambda: cancel_after is not None and time.monotonic() - started >= cancel_after
    capture = native._capture
    attach = native._WindowsJob.attach_worker
    close = native._PrimaryThread.close
    checks = {'executed_before_assignment': None, 'thread_handle_closures': []}
    def delayed_attach(job, pid):
        time.sleep(request.get('assignment_delay', 0))
        checks['executed_before_assignment'] = marker.exists()
        if marker.exists(): raise AssertionError('Native ran before inner Job assignment')
        return attach(job, pid)
    def checked_close(primary):
        handle = primary.handle
        close(primary)
        if handle is not None:
            # GetProcessIdOfThread requires the rights already held by this
            # handle and must now fail with ERROR_INVALID_HANDLE after close.
            owner = primary.k.GetProcessIdOfThread(handle)
            checks['thread_handle_closures'].append(owner == 0 and ctypes.get_last_error() == 6)
    def result_checks():
        checks['executed'] = marker.exists()
        descendant = directory / 'descendant-pid'
        checks['descendant_exists'] = descendant.exists()
        checks['descendant_alive_before_worker_result'] = alive(int(descendant.read_text())) if descendant.exists() else False
        return checks
    def reader_error(stream, destination, label, *args):
        class BadDestination:
            def write(self, data): raise OSError('injected disk write error')
            def flush(self): destination.flush()
            def close(self): destination.close()
        capture(stream, BadDestination() if label == 'stdout' else destination, label, *args)
    def before_resume():
        checks['pre_resume_observed'] = True
        checks['executed_at_pre_resume'] = marker.exists()
        if request.get('pre_resume_error'):raise OSError('injected fresh-resource observation failure')
    try:
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(native, '_capture', reader_error if request.get('reader_error') else capture))
            patches.enter_context(mock.patch.object(native._PrimaryThread, 'close', checked_close))
            patches.enter_context(mock.patch.object(native._WindowsJob, 'attach_worker', delayed_attach))
            if request.get('monitor_error'):
                patches.enter_context(mock.patch.object(native._WindowsJob, 'peak_memory', side_effect=OSError('injected monitor fault')))
            if request.get('assignment_error'):
                patches.enter_context(mock.patch.object(native._WindowsJob, 'attach_worker', side_effect=OSError('injected denial')))
            if request.get('reader_start_error'):
                patches.enter_context(mock.patch.object(native.threading.Thread, 'start', side_effect=OSError('injected thread failure')))
            if request.get('launch_error'):
                patches.enter_context(mock.patch.object(native.subprocess, 'Popen', side_effect=OSError('injected launch failure')))
            if request.get('lookup_error'):
                patches.enter_context(mock.patch.object(native._PrimaryThread, '__init__', side_effect=OSError('injected thread lookup failure')))
            if request.get('resume_error'):
                patches.enter_context(mock.patch.object(native._PrimaryThread, 'resume', side_effect=OSError('injected resume failure')))
            result = native.run_native(argv, cwd=directory, stdout_path=directory / 'stdout', stderr_path=directory / 'stderr',
                                       cancel=cancel, wall_seconds=request.get('wall_seconds', 3),
                                       memory_bytes=request.get('memory_bytes', native.MAX_MEMORY_BYTES),
                                       output_limit_each=request.get('output_limit_each', native.MAX_OUTPUT_BYTES),
                                       before_resume=before_resume if request.get('pre_resume_check') else None)
        return {'ok': True, 'metrics': result, 'checks': result_checks()}
    except native.NativeTrialError as exc:
        return {'ok': False, 'type': type(exc).__name__, 'reason': exc.reason,
                'metrics': exc.metrics, 'checks': result_checks()}


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


class NativeValidationTests(unittest.TestCase):
    def call(self, directory, **kwargs):
        return native.run_native(kwargs.pop('argv', [sys._base_executable]),
                                 cwd=kwargs.pop('cwd', directory),
                                 stdout_path=kwargs.pop('stdout_path', Path(directory) / 'stdout'),
                                 stderr_path=kwargs.pop('stderr_path', Path(directory) / 'stderr'), **kwargs)

    def test_reject_invalid_limits_and_paths_before_ownership_or_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = [{'wall_seconds': 121}, {'wall_seconds': float('nan')}, {'wall_seconds': True},
                     {'memory_bytes': native.MAX_MEMORY_BYTES + 1}, {'memory_bytes': True},
                     {'output_limit_each': native.MAX_OUTPUT_BYTES + 1}, {'output_limit_each': 0},
                     {'argv': ['python']}, {'argv': [sys._base_executable, '\0']}, {'argv': []},
                     {'cwd': 'relative'}, {'stdout_path': root.parent / 'escaped'},
                     {'stdout_path': 'relative'}, {'stdout_path': root / 'stderr'}, {'cancel': None}]
            (root / 'existing').write_bytes(b'keep')
            cases.append({'stdout_path': root / 'existing'})
            for values in cases:
                with self.subTest(values=values), mock.patch.object(native, '_assert_owned_worker') as ownership, mock.patch.object(native.subprocess, 'Popen') as spawn:
                    with self.assertRaises(ValueError): self.call(directory, **values)
                    ownership.assert_not_called(); spawn.assert_not_called()
            self.assertEqual((root / 'existing').read_bytes(), b'keep')
            self.assertFalse((root / 'stdout').exists())

    def test_reject_normal_process_before_any_capture_or_spawn(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(native.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(native.NativeTrialError, 'managed worker'):
                self.call(directory)
            spawn.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_inherited_overrides_and_secrets_are_absent(self):
        source = {'SystemRoot': r'C:\Windows', 'TEMP': r'C:\Temp', 'PATH': 'untrusted',
                  'LLAMA_ARG_MODEL': 'remote', 'LLAMA_CACHE': 'untrusted',
                  'GGML_BACKEND_DL_PATH': 'untrusted', 'CUDA_VISIBLE_DEVICES': '0',
                  'HTTPS_PROXY': 'secret', 'HF_TOKEN': 'secret', 'PYTHONPATH': 'untrusted',
                  'UNRELATED_SECRET': 'secret'}
        with mock.patch.dict(os.environ, source, clear=True):
            result = native._environment()
        self.assertEqual(result['TEMP'], source['TEMP'])
        self.assertEqual(result['PATH'], os.pathsep.join((str(Path(source['SystemRoot']) / 'System32'), source['SystemRoot'])))
        self.assertEqual(result['CUDA_VISIBLE_DEVICES'], '')
        self.assertEqual(result['HF_HUB_OFFLINE'], '1')
        for key in ('LLAMA_ARG_MODEL', 'LLAMA_CACHE', 'GGML_BACKEND_DL_PATH', 'HTTPS_PROXY',
                    'HF_TOKEN', 'PYTHONPATH', 'UNRELATED_SECRET'):
            self.assertNotIn(key, result)

    def test_cancel_before_native_creates_no_logs(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(native, '_assert_owned_worker'), mock.patch.object(native.subprocess, 'Popen') as spawn:
            with self.assertRaises(native.NativeTrialCancelled) as caught:
                self.call(directory, cancel=lambda: True)
            self.assertIsNone(caught.exception.metrics['native_pid'])
            self.assertEqual(caught.exception.metrics['reason'], 'cancelled')
            self.assertEqual(list(Path(directory).iterdir()), []); spawn.assert_not_called()

    def test_exclusive_capture_race_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'stdout'
            def race(): destination.write_bytes(b'preserve')
            with mock.patch.object(native, '_assert_owned_worker', side_effect=race), mock.patch.object(native.subprocess, 'Popen') as spawn:
                with self.assertRaises(native.NativeTrialError): self.call(directory)
                spawn.assert_not_called()
            self.assertEqual(destination.read_bytes(), b'preserve')

    @unittest.skipUnless(os.name == 'nt', 'Windows resume return codes')
    def test_resume_requires_exactly_one_suspension(self):
        for previous in (0, 2, 0xffffffff):
            primary = object.__new__(native._PrimaryThread)
            primary.k = mock.Mock(); primary.k.ResumeThread.return_value = previous
            primary.handle = 123
            with self.subTest(previous=previous):
                with self.assertRaises((native.NativeTrialError, OSError)): primary.resume()


@unittest.skipUnless(os.name == 'nt', 'Windows managed native lifecycle')
class NativeLifecycleTests(unittest.TestCase):
    def call(self, directory, **kwargs):
        return managed.run_worker('tests.test_native_trial_process', {'directory': str(directory), **kwargs},
                                  wall_seconds=12, memory_bytes=1024**3)['result']

    def gone(self, pid):
        deadline = time.monotonic() + 3
        while alive(pid) and time.monotonic() < deadline: time.sleep(.02)
        self.assertFalse(alive(pid), 'Owned native process is still alive: ' + str(pid))

    def captures_released(self, root):
        # Windows termination may report the exit code before the OS releases
        # the process's final file handles. Verify release rather than assuming
        # immediate TemporaryDirectory deletion proves process ownership.
        for name in ('stdout', 'stderr'):
            source = root / name
            destination = root / (name + '.closed')
            deadline = time.monotonic() + 3
            while True:
                try:
                    source.rename(destination)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline: raise
                    time.sleep(.02)

    def test_success_actual_pid_bounded_silent_captures(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.call(directory)
            self.assertTrue(result['ok'], result)
            metrics = result['metrics']
            self.assertEqual(metrics['reason'], 'exit'); self.assertEqual(metrics['returncode'], 0)
            self.assertGreater(metrics['peak_working_set_bytes'], 0)
            self.assertTrue(metrics['primary_thread_resumed'])
            self.assertTrue(metrics['primary_thread_handle_closed'])
            self.assertTrue(metrics['owned_job_empty'])
            self.assertEqual(result['checks']['thread_handle_closures'], [True])
            self.assertFalse(result['checks']['executed_before_assignment'])
            self.assertEqual(metrics['stdout_bytes'], 5); self.assertEqual(metrics['stderr_bytes'], 5)
            self.assertEqual((Path(directory) / 'stdout').read_bytes(), b'good\n')
            self.assertEqual((Path(directory) / 'stderr').read_bytes(), b'note\n')
            self.gone(metrics['native_pid'])

    def test_nonzero_exit_is_failure_and_partial_log_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.call(directory, mode='nonzero')
            self.assertFalse(result['ok']); self.assertEqual(result['reason'], 'nonzero_exit')
            self.assertEqual(result['metrics']['returncode'], 7)
            self.assertEqual((Path(directory) / 'stdout').read_bytes(), b'partial')
            self.gone(result['metrics']['native_pid'])

    def test_fresh_pre_resume_observation_failure_never_executes_native(self):
        for fails in (False,True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as directory:
                result=self.call(directory,pre_resume_check=True,pre_resume_error=fails)
                self.assertEqual(result['ok'],not fails,result)
                self.assertTrue(result['checks']['pre_resume_observed'])
                self.assertFalse(result['checks']['executed_at_pre_resume'])
                self.assertEqual(result['metrics']['primary_thread_resumed'],not fails)
                self.assertEqual(result['checks']['executed'],not fails)
                self.assertTrue(result['metrics']['owned_job_empty'])
                self.gone(result['metrics']['native_pid'])

    def test_cancel_wall_memory_and_output_limits_reap_actual_pid(self):
        cases = [({'mode': 'sleep', 'cancel_after': .15}, 'cancelled'),
                 ({'mode': 'sleep', 'wall_seconds': .2}, 'wall_limit'),
                 ({'mode': 'sleep', 'memory_bytes': 1}, 'memory_limit'),
                 ({'mode': 'stdout_flood', 'output_limit_each': 8192}, 'output_limit'),
                 ({'mode': 'stderr_flood', 'output_limit_each': 8192}, 'output_limit')]
        for arguments, reason in cases:
            with self.subTest(reason=reason, arguments=arguments), tempfile.TemporaryDirectory() as directory:
                result = self.call(directory, **arguments)
                self.assertFalse(result['ok']); self.assertEqual(result['reason'], reason, result)
                limit = arguments.get('output_limit_each', native.MAX_OUTPUT_BYTES)
                self.assertLessEqual((Path(directory) / 'stdout').stat().st_size, limit)
                self.assertLessEqual((Path(directory) / 'stderr').stat().st_size, limit)
                self.gone(result['metrics']['native_pid'])

    def test_monitor_error_reaps_native(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.call(directory, mode='sleep', monitor_error=True)
            self.assertFalse(result['ok']); self.assertEqual(result['reason'], 'supervision_error')
            self.gone(result['metrics']['native_pid'])

    def test_reader_error_reaps_native_and_retains_bounded_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.call(directory, mode='sleep', reader_error=True)
            self.assertFalse(result['ok']); self.assertEqual(result['reason'], 'reader_error', result)
            self.assertEqual((Path(directory) / 'stdout').read_bytes(), b'')
            self.gone(result['metrics']['native_pid'])

    def test_inner_job_assignment_error_reaps_native(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.call(directory, mode='sleep', assignment_error=True)
            self.assertFalse(result['ok']); self.assertEqual(result['reason'], 'supervision_error')
            self.assertFalse(result['metrics']['primary_thread_resumed'])
            self.assertFalse(result['checks']['executed'])
            self.assertEqual(result['checks']['thread_handle_closures'], [True])
            self.gone(result['metrics']['native_pid'])

    def test_delayed_assignment_cannot_run_native_and_success_drains_descendant_before_return(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.call(directory, mode='descendant_exit', assignment_delay=.25)
            self.assertTrue(result['ok'], result)
            self.assertFalse(result['checks']['executed_before_assignment'])
            self.assertTrue(result['checks']['descendant_exists'])
            self.assertFalse(result['checks']['descendant_alive_before_worker_result'], result)
            self.assertTrue(result['metrics']['owned_job_empty'])
            self.gone(result['metrics']['native_pid'])

    def test_cancel_before_resume_prevents_descendants_and_after_resume_drains_them(self):
        for cancel_after in (.15, .7):
            with self.subTest(cancel_after=cancel_after), tempfile.TemporaryDirectory() as directory:
                result = self.call(directory, mode='descendant', assignment_delay=.25, cancel_after=cancel_after)
                self.assertFalse(result['ok']); self.assertEqual(result['reason'], 'cancelled', result)
                self.assertFalse(result['checks']['executed_before_assignment'])
                self.assertEqual(result['checks']['executed'], cancel_after > .25)
                self.assertEqual(result['checks']['descendant_exists'], cancel_after > .25)
                self.assertFalse(result['checks']['descendant_alive_before_worker_result'], result)
                self.assertTrue(result['metrics']['owned_job_empty'])
                self.assertEqual(result['checks']['thread_handle_closures'], [True])
                self.gone(result['metrics']['native_pid'])

    def test_lookup_and_resume_failures_never_execute_and_close_thread_handle(self):
        for fault in ('lookup_error', 'resume_error'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                result = self.call(directory, mode='descendant', **{fault: True})
                self.assertFalse(result['ok']); self.assertEqual(result['reason'], 'supervision_error', result)
                self.assertFalse(result['checks']['executed'])
                self.assertFalse(result['checks']['descendant_exists'])
                self.assertFalse(result['metrics']['primary_thread_resumed'])
                self.assertEqual(result['checks']['thread_handle_closures'], [True] if fault == 'resume_error' else [])
                self.gone(result['metrics']['native_pid'])

    def test_reader_start_error_reaps_native_and_launch_error_has_no_pid(self):
        for fault in ('reader_start_error', 'launch_error'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                result = self.call(directory, mode='sleep', **{fault: True})
                self.assertFalse(result['ok']); self.assertEqual(result['reason'], 'supervision_error', result)
                if fault == 'launch_error':
                    self.assertIsNone(result['metrics']['native_pid'])
                else:
                    self.gone(result['metrics']['native_pid'])

    def test_ownership_gate_and_missing_job_are_rejected_before_spawn(self):
        argv = ['managed_process.py', '--child', 'tests.test_native_trial_process', '{}',
                'Local\\WoodStringAnalysis-' + '0' * 32]
        for is_owned, gate_status in ((False, 0), (True, 258)):
            kernel = mock.Mock()
            def owned(process, job, pointer):
                pointer._obj.value = is_owned
                return 1
            kernel.IsProcessInJob.side_effect = owned
            kernel.WaitForSingleObject.return_value = gate_status
            with self.subTest(owned=is_owned, gate_status=gate_status), tempfile.TemporaryDirectory() as directory, mock.patch.object(native.sys, 'argv', argv), mock.patch.object(native.C, 'WinDLL', return_value=kernel), mock.patch.object(native.subprocess, 'Popen') as spawn:
                with self.assertRaises(native.NativeTrialError) as caught:
                    native.run_native([sys._base_executable], cwd=directory,
                                      stdout_path=Path(directory) / 'stdout', stderr_path=Path(directory) / 'stderr')
                self.assertEqual(caught.exception.reason, 'ownership')
                spawn.assert_not_called(); self.assertEqual(list(Path(directory).iterdir()), [])

    def test_forced_outer_supervisor_death_kills_native_and_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = '''from audio import managed_process as managed
import sys
managed.run_worker('tests.test_native_trial_process', {'directory':sys.argv[1], 'mode':'descendant', 'wall_seconds':30}, python=sys.argv[2], wall_seconds=40)
'''
            supervisor = subprocess.Popen([sys._base_executable, '-c', code, directory, sys.executable],
                                          cwd=Path(__file__).resolve().parents[1],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = time.monotonic() + 6
                while not (root / 'descendant-pid').exists() and time.monotonic() < deadline: time.sleep(.02)
                self.assertTrue((root / 'descendant-pid').exists(), 'Native descendant did not start')
                descendant = int((root / 'descendant-pid').read_text())
                worker_pid = int((root / 'worker-pid').read_text())
                self.assertTrue(alive(descendant))
                # Native stdout is buffered to the owned file. Wait until reader
                # has captured it before killing the process owning the outer Job.
                while not (root / 'stdout').stat().st_size and time.monotonic() < deadline: time.sleep(.02)
                native_pid = int((root / 'stdout').read_text())
                self.assertTrue(alive(native_pid))
                supervisor.kill(); supervisor.wait(timeout=3)
                self.gone(native_pid); self.gone(descendant); self.gone(worker_pid)
                self.captures_released(root)
            finally:
                if supervisor.poll() is None: supervisor.kill()
                supervisor.wait(timeout=3)


if __name__ == '__main__': unittest.main()
