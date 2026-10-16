"""Admission for existing CPU mixed previews, without changing model trial gates.

This is a scheduling margin, not an allocation reservation or a measured peak.
The local AI experiment keeps its separately reviewed GPU/CPU profiles.
"""
import time

GiB = 1024**3
PROFILE = {
    'name': 'mixed-preview-cpu-queue-v1',
    'minimum_available_ram_bytes': 5 * GiB,
    'host_reserve_bytes': 2 * GiB,
    'analysis_child_limit_bytes': 2 * GiB,
    'parent_allowance_bytes': GiB,
    'stable_seconds': 2.,
    'maximum_sample_gap_seconds': 6.,
}


def available_ram():
    # Reuse the actual read-only Windows memory query, not a browser estimate.
    from audio.local_arranger_trial import available_ram as query
    return query()


class Admission:
    def __init__(self, query=available_ram, clock=time.monotonic):
        self.query = query
        self.clock = clock
        self.first_good = None
        self.last_good = None

    def check(self):
        now = self.clock()
        try:
            free = self.query()
            if type(free) is not int or free < 0:
                raise ValueError('Invalid available memory observation')
        except (OSError, ValueError, RuntimeError):
            self.first_good = self.last_good = None
            return False, 'Waiting for a reliable laptop memory reading.'
        if free < PROFILE['minimum_available_ram_bytes']:
            self.first_good = self.last_good = None
            return False, 'Waiting for laptop memory. Your request and original are saved.'
        if self.last_good is None or now - self.last_good > PROFILE['maximum_sample_gap_seconds']:
            self.first_good = now
        self.last_good = now
        if now - self.first_good < PROFILE['stable_seconds']:
            return False, 'Checking that laptop memory is stable before starting.'
        return True, None

    def reset(self):
        self.first_good = self.last_good = None

    def before_start(self):
        """Fresh threshold check after durable writes, with prior stability.

        This cannot reserve memory or prove what happened between samples.
        It prevents a slow database commit making the launch reading stale.
        """
        if self.first_good is None or self.last_good is None or self.last_good-self.first_good < PROFILE['stable_seconds']:
            return False, 'Waiting for stable laptop memory before starting.'
        try:
            free=self.query()
            if type(free) is not int or free < PROFILE['minimum_available_ram_bytes']:
                raise ValueError('Memory no longer meets the admission margin')
        except (OSError,ValueError,RuntimeError):
            self.reset()
            return False, 'Laptop memory changed before starting. The request is still saved.'
        return True, None
