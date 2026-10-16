"""Sampled resource admission/stops for one separate GTX1660Ti load probe.

These observations are not reservations or hard GPU allocation limits. Driver
queries have an independent outer worker timeout. No device controls are used.
"""
import time

GiB=1024**3
PROFILE={'name':'gtx1660ti-vulkan-load-count-v1',
    'minimum_host_available_bytes':6*GiB,'host_available_floor_bytes':2*GiB,
    'minimum_gpu_free_bytes':5*GiB,'gpu_free_floor_bytes':GiB,
    'gpu_used_delta_stop_bytes':int(4.5*GiB),'native_working_set_stop_bytes':int(3.5*GiB),
    'sample_interval_seconds':.2,'maximum_sample_query_seconds':1.,
    'maximum_sample_gap_seconds':1.,'native_wall_seconds':120,
    'scope':'Sampled OS available RAM and device-wide WDDM GPU counters; no reservation or per-model attribution.'}


class ResourceMonitor:
    def __init__(self,gpu,host_available,clock=time.monotonic):
        self.gpu=gpu;self.host_available=host_available;self.clock=clock
        self.started=clock();self.last=None;self.baseline=None;self.samples=0
        self.minimum_host=None;self.minimum_gpu=None;self.maximum_gpu_delta=0
        self.reason=None;self.pre_resume_samples=0

    def _sample(self,admission=False):
        began=self.clock()
        try:
            host=self.host_available();gpu=self.gpu.snapshot();ended=self.clock()
            if type(host) is not int or host<0:raise ValueError('Invalid OS available RAM counter')
            if ended-began>PROFILE['maximum_sample_query_seconds']:
                raise ValueError('GPU resource observation exceeded freshness bound')
            if gpu['total_bytes']!=6*GiB:raise ValueError('Unexpected total dedicated NVIDIA memory')
            if self.baseline is None:self.baseline=dict(gpu)
            delta=gpu['reported_used_bytes']-self.baseline['reported_used_bytes']
            self.last={'elapsed_seconds':ended-self.started,'observed_at':ended,
                       'query_seconds':ended-began,'host_available_bytes':host,**gpu}
            self.samples+=1
            self.minimum_host=host if self.minimum_host is None else min(host,self.minimum_host)
            self.minimum_gpu=gpu['free_bytes'] if self.minimum_gpu is None else min(gpu['free_bytes'],self.minimum_gpu)
            self.maximum_gpu_delta=max(self.maximum_gpu_delta,delta)
            if host<PROFILE['host_available_floor_bytes']:raise ValueError('Host available RAM crossed the 2 GiB reserve')
            if gpu['free_bytes']<PROFILE['gpu_free_floor_bytes']:raise ValueError('GPU free memory crossed the 1 GiB reserve')
            if delta>PROFILE['gpu_used_delta_stop_bytes']:raise ValueError('Device-wide GPU used-memory delta exceeded the stop bound')
            if admission and host<PROFILE['minimum_host_available_bytes']:
                raise ValueError('GPU profile requires at least 6 GiB available host RAM before native resume')
            if admission and gpu['free_bytes']<PROFILE['minimum_gpu_free_bytes']:
                raise ValueError('GPU profile requires at least 5 GiB free GPU memory before native resume')
        except Exception as error:
            self.reason=type(error).__name__+': '+str(error)
            raise

    def admit(self):
        """Always take a fresh observation, including while child is suspended."""
        self._sample(admission=True)

    def before_resume(self):
        self.admit();self.pre_resume_samples+=1

    def cancel(self):
        if self.reason:raise RuntimeError(self.reason)
        now=self.clock()
        if self.last is None:raise RuntimeError('GPU resource monitor was not admitted')
        gap=now-self.last['observed_at']
        if gap>PROFILE['maximum_sample_gap_seconds']:
            self.reason='Resource observation became stale before the next sample'
            raise RuntimeError(self.reason)
        if gap>=PROFILE['sample_interval_seconds']:self._sample()
        return False

    def receipt(self):
        return {'profile':dict(PROFILE),'gpu_identity':self.gpu.identity,'sample_count':self.samples,
            'fresh_pre_resume_samples':self.pre_resume_samples,'baseline_gpu':self.baseline,
            'minimum_host_available_bytes':self.minimum_host,'minimum_gpu_free_bytes':self.minimum_gpu,
            'maximum_device_used_delta_bytes':self.maximum_gpu_delta,'last':self.last,'stop_reason':self.reason}
