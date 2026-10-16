import unittest
from processing_resources import Admission, PROFILE, GiB


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.free = 6 * GiB
        self.gate = Admission(lambda: self.free, lambda: self.now)

    def test_memory_must_remain_available_across_fresh_observations(self):
        self.assertFalse(self.gate.check()[0])
        self.now = 2.
        self.assertTrue(self.gate.check()[0])
        self.free = 4 * GiB
        self.assertFalse(self.gate.check()[0])
        self.free = 6 * GiB
        self.now = 3.
        self.assertFalse(self.gate.check()[0])
        self.now = 5.
        self.assertTrue(self.gate.check()[0])

    def test_stale_observation_and_explicit_reset_require_new_stability(self):
        self.gate.check()
        self.now = 10.
        self.assertFalse(self.gate.check()[0])
        self.now = 12.
        self.assertTrue(self.gate.check()[0])
        self.gate.reset()
        self.assertFalse(self.gate.check()[0])

    def test_invalid_or_unavailable_counter_waits_without_launching(self):
        for value in (None, True, -1, 5.1):
            self.free = value
            self.assertFalse(self.gate.check()[0])
        def missing(): raise OSError('No counter')
        self.assertFalse(Admission(missing).check()[0])

    def test_queue_cpu_margin_does_not_mutate_ai_gpu_profile(self):
        from audio.gpu_trial_monitor import PROFILE as gpu
        self.assertEqual(gpu['minimum_host_available_bytes'], 6 * GiB)
        self.assertEqual(PROFILE['minimum_available_ram_bytes'],
                         PROFILE['host_reserve_bytes'] + PROFILE['analysis_child_limit_bytes'] + PROFILE['parent_allowance_bytes'])

    def test_fresh_launch_observation_after_slow_durable_writes(self):
        self.assertFalse(self.gate.before_start()[0])
        self.gate.check();self.now=2.;self.assertTrue(self.gate.check()[0])
        self.now=35.;self.assertTrue(self.gate.before_start()[0])
        self.free=4*GiB
        self.assertFalse(self.gate.before_start()[0])
        self.free=6*GiB
        self.assertFalse(self.gate.before_start()[0])
