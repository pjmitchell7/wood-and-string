import unittest
from audio.gpu_trial_monitor import ResourceMonitor, PROFILE, GiB


class FakeGpu:
    identity={'name':'fake observed NVIDIA'}
    def __init__(self):self.free=int(5.8*GiB);self.used=int(.2*GiB);self.error=None
    def snapshot(self):
        if self.error:raise self.error
        return {'total_bytes':6*GiB,'free_bytes':self.free,'reported_used_bytes':self.used,'unreported_reserved_bytes':0}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.gpu=FakeGpu();self.now=0.;self.host=int(6.5*GiB)
        self.monitor=ResourceMonitor(self.gpu,lambda:self.host,lambda:self.now)

    def test_admission_and_fresh_pre_resume_are_distinct_samples(self):
        self.monitor.admit();self.assertFalse(self.monitor.cancel())
        self.assertEqual(self.monitor.samples,1)
        self.monitor.before_resume();self.assertEqual(self.monitor.samples,2)
        self.now=.21;self.monitor.cancel();receipt=self.monitor.receipt()
        self.assertEqual(receipt['sample_count'],3);self.assertEqual(receipt['fresh_pre_resume_samples'],1)
        self.assertEqual(receipt['profile']['native_working_set_stop_bytes'],int(3.5*GiB))

    def test_initial_admission_is_stronger_than_runtime_reserve(self):
        self.host=6*GiB-1
        with self.assertRaisesRegex(ValueError,'at least 6 GiB'):self.monitor.admit()
        self.setUp();self.gpu.free=5*GiB-1
        with self.assertRaisesRegex(ValueError,'at least 5 GiB'):self.monitor.admit()

    def test_host_gpu_and_device_delta_stops_persist_reason(self):
        for mode in ('host','gpu','delta'):
            with self.subTest(mode=mode):
                self.setUp();self.monitor.admit();self.now=.21
                if mode=='host':self.host=2*GiB-1
                elif mode=='gpu':self.gpu.free=GiB-1
                else:self.gpu.used+=int(4.5*GiB)+1
                with self.assertRaises(ValueError):self.monitor.cancel()
                with self.assertRaises(RuntimeError):self.monitor.cancel()
                self.assertIsNotNone(self.monitor.receipt()['stop_reason'])

    def test_stale_or_failed_query_does_not_produce_an_acceptable_sample(self):
        self.monitor.admit();self.now=1.01
        with self.assertRaisesRegex(RuntimeError,'stale'):self.monitor.cancel()
        self.setUp();self.monitor.admit();self.now=.21;self.gpu.error=OSError('driver failed')
        with self.assertRaisesRegex(OSError,'driver failed'):self.monitor.cancel()
        self.assertEqual(self.monitor.samples,1)

    def test_slow_query_and_bad_identity_fail_admission(self):
        original=self.gpu.snapshot
        def slow():self.now+=1.01;return original()
        self.gpu.snapshot=slow
        with self.assertRaisesRegex(ValueError,'freshness'):self.monitor.admit()
        self.setUp();self.gpu.snapshot=lambda:{'total_bytes':8*GiB}
        with self.assertRaisesRegex(ValueError,'total dedicated'):self.monitor.admit()


if __name__=='__main__':unittest.main()
