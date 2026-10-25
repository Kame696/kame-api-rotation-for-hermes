"""V3 hedging tests: buckets, thresholds, budget, guards, 429 back-pressure.

The concurrent race mechanics (cancellation-at-first-token, loser cleanup)
are exercised against a real loopback server in the live smoke pass; what
this file pins is the decision layer — the part that decides whether a
hedge may ever fire.
"""
import os
import sys
import time
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))
sys.path.insert(0, _SRC)

# hedging reads settings via package-relative import ("from .. import settings"),
# so the whole test must run against ONE package instance — the installed name.
import importlib
import importlib.util as _ilu

_pkg_spec = _ilu.spec_from_file_location(
    "hermes_kame_api_rotation", os.path.join(_SRC, "__init__.py"),
    submodule_search_locations=[_SRC],
)
_pkg = _ilu.module_from_spec(_pkg_spec)
sys.modules.setdefault("hermes_kame_api_rotation", _pkg)
_pkg_spec.loader.exec_module(_pkg)
_hedging = importlib.import_module("hermes_kame_api_rotation.core.hedging")
HedgeTracker = _hedging.HedgeTracker
bucket_key = _hedging.bucket_key
reasoning_model = _hedging.reasoning_model


class TestBucketKeying(unittest.TestCase):
    def test_key_shape(self):
        self.assertEqual(bucket_key("g:m", True, False), "g:m|ttft|std")
        self.assertEqual(bucket_key("g:m", False, True), "g:m|total|reasoning")

    def test_reasoning_detection(self):
        self.assertTrue(reasoning_model("deepseek-r1"))
        self.assertTrue(reasoning_model("o3-mini"))
        self.assertTrue(reasoning_model("QwQ-32B"))
        self.assertTrue(reasoning_model("gemini-2.5-pro-thinking"))
        # 1.8.2.4 (owner decision D2): Gemini 2.5 Flash thinks by default;
        # only the -lite tiers do not.
        self.assertTrue(reasoning_model("gemini-2.5-flash"))
        self.assertFalse(reasoning_model("gemini-2.5-flash-lite"))
        self.assertFalse(reasoning_model(""))
        self.assertTrue(reasoning_model("gpt-5.1"))


class TestThresholds(unittest.TestCase):
    def setUp(self):
        self.t = HedgeTracker()

    def test_cold_std(self):
        self.assertEqual(self.t.threshold("g:m", stream=False, reasoning=False), 6.0)

    def test_cold_reasoning(self):
        self.assertEqual(self.t.threshold("g:m", stream=False, reasoning=True), 30.0)

    def test_buckets_do_not_mix(self):
        # 25 fast std samples must not give the reasoning bucket a threshold
        for _ in range(25):
            self.t.record_latency("g:m", 1.0, stream=False, reasoning=False)
        self.assertEqual(self.t.threshold("g:m", stream=False, reasoning=True), 30.0)
        # and the std bucket is learned (clamped to [cold/2, 4*cold])
        learned = self.t.threshold("g:m", stream=False, reasoning=False)
        self.assertLessEqual(learned, 24.0)
        self.assertGreaterEqual(learned, 3.0)

    def test_stale_falls_back_to_cold(self):
        now = time.monotonic()
        for i in range(25):
            self.t.record_latency("g:m", 1.0, stream=False, reasoning=False, now=now - 4000.0)
        # all samples older than the window -> cold again
        self.assertEqual(self.t.threshold("g:m", stream=False, reasoning=False), 6.0)

    def test_censored_sample_does_not_lower(self):
        # winner fast samples, then a loser lower bound far above them
        for _ in range(25):
            self.t.record_latency("g:m", 1.0, stream=False, reasoning=False)
        # a censored 20s loser: the threshold must not drop below what the
        # real samples alone would give
        before = self.t.threshold("g:m", stream=False, reasoning=False)
        self.t.record_latency("g:m", 20.0, stream=False, reasoning=False, censored=True)
        after = self.t.threshold("g:m", stream=False, reasoning=False)
        self.assertGreaterEqual(after, before)


class TestDecision(unittest.TestCase):
    def setUp(self):
        self.t = HedgeTracker()
        self.ident = "g:m"

    def test_cold_start_hedge_only_when_stuck(self):
        # cold std = 6: a 7s call hedges, a 3s call does not
        self.assertTrue(self.t.should_hedge(self.ident, 7.0, healthy_count=3))
        self.assertFalse(self.t.should_hedge(self.ident, 3.0, healthy_count=3))

    def test_guard_single_key(self):
        self.assertFalse(self.t.should_hedge(self.ident, 99.0, healthy_count=1))

    def test_guard_pressure(self):
        self.assertFalse(self.t.should_hedge(self.ident, 99.0, healthy_count=3, under_pressure=True))

    def test_guard_prompt_size(self):
        self.assertFalse(self.t.should_hedge(self.ident, 99.0, healthy_count=3, est_prompt_tokens=50000))

    def test_guard_cooldown_after_429(self):
        self.assertTrue(self.t.should_hedge(self.ident, 99.0, healthy_count=3))
        self.t.note_hedge_429(self.ident)
        self.assertFalse(self.t.should_hedge(self.ident, 99.0, healthy_count=3))

    def test_budget_ratio(self):
        # Production interleaves: every call (slow or not) records its
        # latency, which refills the budget. 100 calls -> ~10 earned hedges
        # on top of the initial token and burst.
        hedges = 0
        for _ in range(100):
            self.t.record_latency(self.ident, 2.0)  # each call earns 0.1 token
            if self.t.should_hedge(self.ident, 99.0, healthy_count=50):
                hedges += 1
                self.t.note_hedge(self.ident)
        self.assertLessEqual(hedges, 14)  # 1 initial + 3 burst + ~10 earned
        self.assertGreaterEqual(hedges, 9)

    def test_mode_off(self):
        import importlib
        saved = os.environ.get("KAME_HEDGE_MODE")
        os.environ["KAME_HEDGE_MODE"] = "off"
        try:
            self.assertFalse(self.t.should_hedge(self.ident, 99.0, healthy_count=3))
        finally:
            if saved is None:
                os.environ.pop("KAME_HEDGE_MODE", None)
            else:
                os.environ["KAME_HEDGE_MODE"] = saved

    def test_mode_max_no_longer_fires_at_once_it_waits_a_notch_sooner_than_balanced(self):
        # 1.8.2.9 (decisions/0031): the old test pinned 'fires at t=0 with no samples'; that second
        # request on every reply burned a 14-key pool in four messages. Faster (stored 'max') now
        # hedges a late reply, at three quarters of the time Balanced would wait, from its own budget.
        saved = os.environ.get("KAME_HEDGE_MODE")
        os.environ["KAME_HEDGE_MODE"] = "max"
        try:
            self.assertFalse(self.t.should_hedge(self.ident, 0.0, healthy_count=3))     # nothing at t=0
            cold = self.t.threshold(self.ident, stream=False, reasoning=False)
            self.assertTrue(self.t.should_hedge(self.ident, cold + 0.01, healthy_count=3))
            # and still respects the saturation guard
            self.assertFalse(self.t.should_hedge(self.ident, cold + 0.01, healthy_count=1))
            # a hedge 429 steps Faster back to Balanced for 10 minutes: its threshold is longer again
            os.environ["KAME_HEDGE_MODE"] = "adaptive"
            balanced = self.t.threshold(self.ident, stream=False, reasoning=False)
            os.environ["KAME_HEDGE_MODE"] = "max"
            self.t.note_hedge_429(self.ident)
            self.assertAlmostEqual(self.t.threshold(self.ident, stream=False, reasoning=False), balanced)
        finally:
            if saved is None:
                os.environ.pop("KAME_HEDGE_MODE", None)
            else:
                os.environ["KAME_HEDGE_MODE"] = saved

    def test_snapshot_shape(self):
        for _ in range(5):
            self.t.record_latency(self.ident, 1.0)
        snap = self.t.snapshot()
        self.assertIn(bucket_key(self.ident, False, False), snap)


if __name__ == "__main__":
    unittest.main()
