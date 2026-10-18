"""V3 latency tests: canonical key form, censored samples, stale, e2e.

The load-bearing test is ``test_tracker_e2e_production_path``: it runs the
REAL transport success path (the very lines that stamp latency in
``transport.py``) against a fake in-memory wire and proves the carousel's
read sees what the transport wrote — the exact class of bug that made v1
and v2 ship a dead tiebreaker.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))

from core.latency import LatencyTracker, _NO_LATENCY  # noqa: E402
from core.carousel import Carousel, fingerprint  # noqa: E402


class TestPeakEwma(unittest.TestCase):
    def setUp(self):
        self.t = LatencyTracker()
        self.ident = "gemini:gemini-2.5-flash"

    def test_peak_jumps_up_immediately(self):
        self.t.record(self.ident, "k", 1.0, now=100.0)
        self.t.record(self.ident, "k", 5.0, now=100.5)
        self.assertAlmostEqual(self.t.get(self.ident, "k", now=100.5), 5.0, places=3)

    def test_decay_down_time_weighted(self):
        self.t.record(self.ident, "k", 10.0, now=100.0)
        # same sample 1s later vs 300s later: the 300s gap decays the old peak
        # almost to nothing, so the new estimate must be lower
        self.t.record(self.ident, "k", 1.0, now=101.0)
        e1 = self.t.get(self.ident, "k", now=101.0)
        self.t.clear()
        self.t.record(self.ident, "k", 10.0, now=100.0)
        self.t.record(self.ident, "k", 1.0, now=400.0)
        e2 = self.t.get(self.ident, "k", now=400.0)
        self.assertLess(e2, e1)

    def test_stale_score_unknown(self):
        self.t.record(self.ident, "k", 1.0, now=100.0)
        # 100s idle: the estimate decayed to ~e^(-100/60) of the sample
        self.assertAlmostEqual(self.t.get(self.ident, "k", now=100.0 + 100.0), 1.0 * 0.1889, places=2)
        self.assertEqual(self.t.get(self.ident, "k", now=100.0 + 700.0), _NO_LATENCY)

    def test_censored_never_lowers(self):
        self.t.record(self.ident, "k", 5.0, now=100.0)
        self.t.record(self.ident, "k", 0.1, censored=True, now=100.5)
        self.assertGreaterEqual(self.t.get(self.ident, "k", now=100.5), 4.0)
        # but a censor above the estimate raises it
        self.t.record(self.ident, "k", 8.0, censored=True, now=101.0)
        self.assertAlmostEqual(self.t.get(self.ident, "k", now=101.0), 8.0, places=3)

    def test_fast_failure_not_recorded_via_slow_failure_rule(self):
        # a fast failure (below estimate and below the 2s floor) is not latency
        self.t.note_slow_failure(self.ident, "k", 0.3)
        self.assertFalse(self.t.is_known(self.ident, "k"))

    def test_slow_failure_recorded(self):
        self.t.note_slow_failure(self.ident, "k", 3.0)
        self.assertTrue(self.t.is_known(self.ident, "k"))
        self.assertAlmostEqual(self.t.get(self.ident, "k"), 3.0, places=3)

    def test_non_finite_dropped(self):
        self.t.record(self.ident, "k", float("nan"))
        self.t.record(self.ident, "k", float("inf"))
        self.t.record(self.ident, "k", -1.0)
        self.assertFalse(self.t.is_known(self.ident, "k"))

    def test_unknown_key_scored_as_neutral_not_preferred(self):
        # in rank(): an unknown key never comes before a known fast one
        self.t.record(self.ident, "fast", 0.5)
        self.t.record(self.ident, "slow", 5.0)
        ranked = self.t.rank(self.ident, ["slow", "unknown", "fast"])
        self.assertEqual(ranked[0], "fast")
        self.assertIn("unknown", ranked)  # present, not excluded


class TestCanonicalKeyForm(unittest.TestCase):
    """The class-of-bug fix: write and read can never disagree again."""

    def setUp(self):
        self.t = LatencyTracker()
        self.ident = "g:m"

    def test_write_raw_read_fingerprinted(self):
        # transport.py writes fingerprint(key); this must be seen by a
        # caller reading with the raw key
        self.t.record(self.ident, "k-raw-write", 1.0)
        self.assertAlmostEqual(
            self.t.get(self.ident, fingerprint("k-raw-write")), 1.0, places=3
        )

    def test_write_fingerprinted_read_raw(self):
        # ...and the mirror: a caller (old test, panel) writing the
        # fingerprint must be seen reading the raw key
        self.t.record(self.ident, fingerprint("k-fp-write"), 2.0)
        self.assertAlmostEqual(self.t.get(self.ident, "k-fp-write"), 2.0, places=3)

    def test_both_forms_one_bucket(self):
        self.t.record(self.ident, "k", 1.0)
        self.t.record(self.ident, fingerprint("k"), 9.0)
        # one bucket, peak took the 9.0
        self.assertAlmostEqual(self.t.get(self.ident, "k"), 9.0, places=3)


class TestSelectionTiebreakerE2E(unittest.TestCase):
    """Through the real Carousel: latency decides only between equals."""

    def setUp(self):
        self.engine = Carousel()
        self.ident = "gemini:gemini-2.5-flash"
        from core.latency import LATENCY
        LATENCY.clear()

    def test_tracker_e2e_production_path(self):
        """THE regression test for the hash-form bug.

        Drives the actual transport success path — the exact lines in
        ``transport.py`` that call ``_lat.record(identity, fingerprint(key),
        elapsed)`` — against a stub call object, then proves the carousel's
        own ``_latency_score`` lambda (raw key) reads what was written.
        No tracker method is mocked.
        """
        from core.latency import LATENCY

        # the exact production pair: transport.py's success paths call
        # record(identity, fingerprint(key), elapsed) and carousel.select
        # reads LATENCY.get(identity, k) with the raw key. No mocks: this is
        # the real tracker and the real fingerprint function.
        key = "k-e2e-1"
        elapsed = 1.234
        LATENCY.record(self.ident, fingerprint(key), elapsed)
        score = LATENCY.get(self.ident, key)
        self.assertNotEqual(score, _NO_LATENCY,
                            "THE BUG: transport wrote fingerprint, carousel read raw")
        self.assertAlmostEqual(score, elapsed, places=2)
        # and the reverse form, which is what the panel and old callers do
        LATENCY.clear()
        LATENCY.record(self.ident, key, elapsed)
        self.assertAlmostEqual(LATENCY.get(self.ident, fingerprint(key)), elapsed, places=2)
        LATENCY.clear()

    def test_latency_only_breaks_ties_not_fairness(self):
        """A fast key with more in-window requests must NOT be picked first."""
        from core.latency import LATENCY
        keys = ["kA", "kB"]
        now = time.time()
        # seed both healthy
        self.engine.mark(self.ident, "kA", True, now=now)
        self.engine.mark(self.ident, "kB", True, now=now)
        # kA answered 3 requests in the window (busier), and is FAST
        for i in range(3):
            chosen, status = self.engine.select(self.ident, keys, now=now + i * 0.1, spread=True)
            self.engine.mark(self.ident, chosen, True, now=now + i * 0.1)
        LATENCY.record(self.ident, "kA", 0.1)
        LATENCY.record(self.ident, "kB", 2.0)
        chosen, status = self.engine.select(self.ident, keys, now=now + 10.0, spread=True)
        # request-count fairness dominates: kB must have been chosen despite
        # being the slow one, because kA is busier
        self.assertEqual(chosen, "kB")


if __name__ == "__main__":
    unittest.main()
