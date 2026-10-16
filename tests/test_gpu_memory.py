import ctypes as C
from contextlib import ExitStack
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from audio import gpu_memory as gpu
from audio.gpu_memory import NvidiaMemory, Memory


class FakeLibrary:
    def __init__(self,total=6*1024**3,free=5*1024**3,used=0,code=0):
        self.values=(total,free,used);self.code=code;self.shutdowns=0
    def nvmlDeviceGetMemoryInfo(self,device,pointer):
        target=C.cast(pointer,C.POINTER(Memory)).contents
        target.total,target.free,target.used=self.values
        return self.code
    def nvmlShutdown(self):self.shutdowns+=1;return 0


class GpuMemory(unittest.TestCase):
    def query(self,**kwargs):
        query=NvidiaMemory();query.library=FakeLibrary(**kwargs);query.initialized=True
        return query

    def test_free_used_and_unreported_reservation_stay_distinct(self):
        query=self.query();snapshot=query.snapshot()
        self.assertEqual(snapshot['free_bytes'],5*1024**3)
        self.assertEqual(snapshot['reported_used_bytes'],0)
        self.assertEqual(snapshot['unreported_reserved_bytes'],1024**3)
        query.close();query.close();self.assertEqual(query.library.shutdowns,1)
        with self.assertRaises(RuntimeError):query.snapshot()

    def test_query_failure_is_not_reported_as_free_memory(self):
        query=self.query(code=999)
        with self.assertRaisesRegex(RuntimeError,'returned 999'):query.snapshot()

    def test_invalid_counters_fail_admission(self):
        for values in [{'total':0},{'free':7*1024**3},{'used':2*1024**3}]:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):self.query(**values).snapshot()


class ConstructorLibrary:
    """Callable mocked NVML exports support ctypes signature assignment."""
    def __init__(self, *, init_code=0, count=1, name=None, memory_code=0,
                 count_code=0, handle_code=0, name_code=0, total=6*1024**3):
        self.nvmlInit_v2=mock.Mock(return_value=init_code)
        self.nvmlShutdown=mock.Mock(return_value=0)
        def get_count(pointer):
            C.cast(pointer,C.POINTER(C.c_uint)).contents.value=count
            return count_code
        def get_handle(index,pointer):
            C.cast(pointer,C.POINTER(C.c_void_p)).contents.value=0x1234
            return handle_code
        def get_name(device,destination,size):
            destination.value=name if name is not None else gpu.EXPECTED_NAME.encode('utf-8')
            return name_code
        def get_memory(device,pointer):
            value=C.cast(pointer,C.POINTER(Memory)).contents
            value.total=total;value.free=5*1024**3;value.used=0
            return memory_code
        self.nvmlDeviceGetCount_v2=mock.Mock(side_effect=get_count)
        self.nvmlDeviceGetHandleByIndex_v2=mock.Mock(side_effect=get_handle)
        self.nvmlDeviceGetName=mock.Mock(side_effect=get_name)
        self.nvmlDeviceGetMemoryInfo=mock.Mock(side_effect=get_memory)


class GpuConstructorTests(unittest.TestCase):
    def mocked_driver(self, library):
        """Use a fake file and WinDLL object; never load the installed driver."""
        stack=ExitStack()
        directory=stack.enter_context(tempfile.TemporaryDirectory())
        path=Path(directory)/'nvml-test-only.dll'
        path.write_bytes(b'fake NVML identity bytes; not an executable')
        stack.enter_context(mock.patch.object(gpu,'NVML_PATH',path))
        stack.enter_context(mock.patch.object(gpu,'os',SimpleNamespace(name='nt')))
        loader=stack.enter_context(mock.patch.object(gpu.C,'WinDLL',return_value=library,create=True))
        return stack,path,loader

    def test_initialization_failure_does_not_shutdown_an_uninitialized_library(self):
        library=ConstructorLibrary(init_code=999)
        stack,path,loader=self.mocked_driver(library)
        query=NvidiaMemory()
        with stack:
            with self.assertRaisesRegex(RuntimeError,'nvmlInit_v2 returned 999'):query.__enter__()
            loader.assert_called_once_with(str(path.resolve()))
            library.nvmlInit_v2.assert_called_once_with()
            library.nvmlShutdown.assert_not_called()
            library.nvmlDeviceGetCount_v2.assert_not_called()
            query.close();library.nvmlShutdown.assert_not_called()
        self.assertFalse(query.initialized)

    def test_unexpected_count_name_and_query_errors_shutdown_once(self):
        cases=[({'count':0},ValueError,'exactly one NVIDIA'),
               ({'count':2},ValueError,'exactly one NVIDIA'),
               ({'name':b'NVIDIA Different GPU'},ValueError,'unexpected NVIDIA'),
               ({'name':b'\xff'},UnicodeDecodeError,None),
               ({'count_code':999},RuntimeError,'nvmlDeviceGetCount_v2 returned 999'),
               ({'handle_code':999},RuntimeError,'nvmlDeviceGetHandleByIndex_v2 returned 999'),
               ({'name_code':999},RuntimeError,'nvmlDeviceGetName returned 999')]
        for arguments,error,pattern in cases:
            with self.subTest(arguments=arguments):
                library=ConstructorLibrary(**arguments)
                stack,path,loader=self.mocked_driver(library)
                query=NvidiaMemory()
                with stack:
                    check=self.assertRaises(error) if pattern is None else self.assertRaisesRegex(error,pattern)
                    with check:query.__enter__()
                    self.assertFalse(query.initialized)
                    library.nvmlShutdown.assert_called_once_with()
                    library.nvmlDeviceGetMemoryInfo.assert_not_called()
                    query.close();library.nvmlShutdown.assert_called_once_with()

    def test_initial_snapshot_failure_balances_shutdown_exactly_once(self):
        for arguments,error in (({'memory_code':999},RuntimeError),({'total':0},ValueError)):
            with self.subTest(arguments=arguments):
                library=ConstructorLibrary(**arguments)
                stack,path,loader=self.mocked_driver(library)
                query=NvidiaMemory()
                with stack:
                    with self.assertRaises(error):query.__enter__()
                    library.nvmlDeviceGetMemoryInfo.assert_called_once()
                    library.nvmlShutdown.assert_called_once_with()
                    self.assertFalse(query.initialized)
                    query.close();library.nvmlShutdown.assert_called_once_with()

    def test_success_observes_identity_and_context_exit_shutdown_is_idempotent(self):
        library=ConstructorLibrary()
        stack,path,loader=self.mocked_driver(library)
        query=NvidiaMemory()
        with stack:
            with query as entered:
                self.assertIs(entered,query)
                self.assertTrue(query.initialized)
                self.assertEqual(query.device.value,0x1234)
                self.assertEqual(query.identity['name'],gpu.EXPECTED_NAME)
                self.assertEqual(query.identity['nvml_index'],0)
                self.assertEqual(query.identity['nvml_path'],str(path.resolve()))
                self.assertEqual(query.identity['nvml_observed_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(query.snapshot()['unreported_reserved_bytes'],1024**3)
                self.assertEqual(library.nvmlDeviceGetMemoryInfo.call_count,2)
                library.nvmlShutdown.assert_not_called()
            loader.assert_called_once_with(str(path.resolve()))
            library.nvmlShutdown.assert_called_once_with()
            self.assertFalse(query.initialized)
            query.close();library.nvmlShutdown.assert_called_once_with()
            with self.assertRaisesRegex(RuntimeError,'outside initialized'):query.snapshot()
            self.assertEqual(library.nvmlDeviceGetMemoryInfo.call_count,2)


if __name__=='__main__':unittest.main()
