"""Read-only NVML memory queries for this laptop's bounded GPU experiment.

No context creation, allocation, clock/power/driver changes or workload launch.
Device-wide WDDM memory counters are observations, not per-model allocations.
"""
import ctypes as C
import hashlib
import os
from pathlib import Path

EXPECTED_NAME='NVIDIA GeForce GTX 1660 Ti'
NVML_PATH=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/nvml.dll'


class Memory(C.Structure):
    _fields_=[('total',C.c_ulonglong),('free',C.c_ulonglong),('used',C.c_ulonglong)]


class NvidiaMemory:
    """One balanced NVML initialization; only query entry points are bound."""
    def __init__(self):
        self.library=None;self.initialized=False;self.device=C.c_void_p();self.identity=None

    def _call(self,name,*args):
        code=getattr(self.library,name)(*args)
        if code!=0:raise RuntimeError('NVML query failed: '+name+' returned '+str(code))

    def __enter__(self):
        if os.name!='nt':raise RuntimeError('This GPU probe requires the observed Windows driver')
        path=NVML_PATH.resolve(strict=True)
        if path!=NVML_PATH.absolute() or NVML_PATH.is_symlink():raise ValueError('Unexpected NVML driver path')
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        self.library=C.WinDLL(str(path))
        signatures={
            'nvmlInit_v2':[], 'nvmlShutdown':[],
            'nvmlDeviceGetCount_v2':[C.POINTER(C.c_uint)],
            'nvmlDeviceGetHandleByIndex_v2':[C.c_uint,C.POINTER(C.c_void_p)],
            'nvmlDeviceGetName':[C.c_void_p,C.c_char_p,C.c_uint],
            'nvmlDeviceGetMemoryInfo':[C.c_void_p,C.POINTER(Memory)],
        }
        for name,args in signatures.items():
            fn=getattr(self.library,name);fn.argtypes=args;fn.restype=C.c_int
        try:
            self._call('nvmlInit_v2');self.initialized=True
            count=C.c_uint();self._call('nvmlDeviceGetCount_v2',C.byref(count))
            if count.value!=1:raise ValueError('Revalidate device selection: expected exactly one NVIDIA GPU')
            self._call('nvmlDeviceGetHandleByIndex_v2',0,C.byref(self.device))
            name=C.create_string_buffer(256);self._call('nvmlDeviceGetName',self.device,name,len(name))
            actual=name.value.decode('utf-8',errors='strict')
            if actual!=EXPECTED_NAME:raise ValueError('Revalidate GPU profile: unexpected NVIDIA device')
            self.identity={'name':actual,'nvml_index':0,'nvml_path':str(path),'nvml_observed_sha256':digest,
                           'scope':'Read-only device-wide counters; no per-process attribution or publisher attestation.'}
            self.snapshot()
            return self
        except BaseException:
            self.close();raise

    def snapshot(self):
        if not self.initialized:raise RuntimeError('NVML query outside initialized interval')
        value=Memory();self._call('nvmlDeviceGetMemoryInfo',self.device,C.byref(value))
        total,free,used=value.total,value.free,value.used
        if not 0<total<=128*1024**3 or not 0<=free<=total or not 0<=used<=total or free+used>total:
            raise ValueError('Invalid device-wide NVML memory counters')
        return {'total_bytes':total,'free_bytes':free,'reported_used_bytes':used,
                'unreported_reserved_bytes':total-free-used}

    def close(self):
        if self.initialized:
            self.initialized=False
            self._call('nvmlShutdown')

    def __exit__(self,*args):self.close()
