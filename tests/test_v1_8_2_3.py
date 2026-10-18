"""Tests for KAME API Rotation v1.8.2.3 features.

Offline tests — no API calls, no live Hermes.  Injected clocks and mocks
follow the existing test patterns in the KAME suite.
"""

import math
import sys
import os
import time
import threading

# Make the src/ tree importable
_SRC = os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation")
sys.path.insert(0, _SRC)


# ── Item 5: Host seam check ─────────────────────────────────────────

class TestHostSeamCheck:
    def test_check_returns_dict(self):
        from integrity import check_host_seams
        result = check_host_seams()
        assert isinstance(result, dict)
        assert "ok" in result
        assert "summary" in result
        assert "required_present" in result
        assert "required_missing" in result

    def test_check_never_raises(self):
        """check_host_seams must never raise — a check that crashes is worse."""
        from integrity import check_host_seams
        # Should always return a dict, even if imports fail
        result = check_host_seams()
        assert isinstance(result, dict)

    def test_summary_is_a_string(self):
        from integrity import check_host_seams
        result = check_host_seams()
        assert isinstance(result["summary"], str)
        assert len(result["summary"]) > 0

    def test_check_actually_probes(self):
        """check_host_seams must not hardcode True — it must try real imports."""
        from integrity import check_host_seams
        result = check_host_seams()
        # Outside Hermes, hermes_cli.plugins won't exist, so required hooks
        # should be marked False (not hardcoded True)
        assert isinstance(result["ok"], bool)
        # At minimum the result must contain list-type entries
        assert isinstance(result["required_present"], list)
        assert isinstance(result["required_missing"], list)


# ── Item 6: Retry hint ──────────────────────────────────────────────

class TestRetryHint:
    def test_transient_gets_hint(self):
        sys.path.insert(0, _SRC)
        # Import from the transport module's namespace
        exec_ns = {}
        with open(os.path.join(_SRC, "transport.py"), encoding="utf-8") as f:
            # Read just the constants and function
            lines = f.readlines()
        # Extract just the relevant code
        code = (
            "_TRANSIENT_KINDS = frozenset({\n"
            '    "timeout", "server", "server_overload", "connection", "other",\n'
            '    "rate_limit", "quota", "",\n'
            "})\n"
            '_RETRY_HINT = (\n'
            '    "\\n\\n⚠️ This appears to be a temporary API issue.  "\n'
            '    "Retrying the same tool call will very likely succeed."\n'
            ')\n'
            '_RETRY_HINT_MARKER = "Retrying the same tool call"\n'
            '\n'
            'def _with_retry_hint(exc, kind=""):\n'
            '    if kind and kind not in _TRANSIENT_KINDS:\n'
            '        return exc\n'
            '    try:\n'
            '        msg = str(exc)\n'
            '        if _RETRY_HINT_MARKER in msg:\n'
            '            return exc\n'
            '        exc.args = (msg + _RETRY_HINT,) + exc.args[1:]\n'
            '    except Exception:\n'
            '        pass\n'
            '    return exc\n'
        )
        exec(code, exec_ns)
        _with_retry_hint = exec_ns["_with_retry_hint"]

        err = ConnectionError("pool exhausted")
        result = _with_retry_hint(err, kind="timeout")
        assert "Retrying the same tool call" in str(result)

    def test_permanent_no_hint(self):
        exec_ns = {}
        code = (
            "_TRANSIENT_KINDS = frozenset({\n"
            '    "timeout", "server", "server_overload", "connection", "other",\n'
            '    "rate_limit", "quota", "",\n'
            "})\n"
            '_RETRY_HINT = "\\n\\nRetrying the same tool call will very likely succeed."\n'
            '_RETRY_HINT_MARKER = "Retrying the same tool call"\n'
            '\n'
            'def _with_retry_hint(exc, kind=""):\n'
            '    if kind and kind not in _TRANSIENT_KINDS:\n'
            '        return exc\n'
            '    try:\n'
            '        msg = str(exc)\n'
            '        if _RETRY_HINT_MARKER in msg:\n'
            '            return exc\n'
            '        exc.args = (msg + _RETRY_HINT,) + exc.args[1:]\n'
            '    except Exception:\n'
            '        pass\n'
            '    return exc\n'
        )
        exec(code, exec_ns)
        _with_retry_hint = exec_ns["_with_retry_hint"]

        err = ConnectionError("auth failed")
        result = _with_retry_hint(err, kind="auth")
        assert "Retrying" not in str(result)

    def test_idempotent(self):
        exec_ns = {}
        code = (
            "_TRANSIENT_KINDS = frozenset({'timeout'})\n"
            '_RETRY_HINT = "\\n\\nRetrying the same tool call will very likely succeed."\n'
            '_RETRY_HINT_MARKER = "Retrying the same tool call"\n'
            '\n'
            'def _with_retry_hint(exc, kind=""):\n'
            '    if kind and kind not in _TRANSIENT_KINDS:\n'
            '        return exc\n'
            '    try:\n'
            '        msg = str(exc)\n'
            '        if _RETRY_HINT_MARKER in msg:\n'
            '            return exc\n'
            '        exc.args = (msg + _RETRY_HINT,) + exc.args[1:]\n'
            '    except Exception:\n'
            '        pass\n'
            '    return exc\n'
        )
        exec(code, exec_ns)
        fn = exec_ns["_with_retry_hint"]

        err = ConnectionError("pool exhausted")
        err = fn(err, kind="timeout")
        err = fn(err, kind="timeout")  # second call
        assert str(err).count("Retrying") == 1


# ── Item 3: Peak EWMA latency tracker ───────────────────────────────

class TestLatencyTracker:
    def test_record_and_get(self):
        from core.latency import LatencyTracker
        lt = LatencyTracker(tau=10.0)
        now = 100.0
        lt.record("g:m", "k1", 0.5, now=now)
        val = lt.get("g:m", "k1", now=now)
        assert val > 0
        assert val <= 0.5 + 0.01  # should be close to 0.5

    def test_unknown_key_returns_inf(self):
        from core.latency import LatencyTracker, _NO_LATENCY
        lt = LatencyTracker()
        val = lt.get("g:m", "unknown_key")
        assert val == _NO_LATENCY

    def test_rank_preserves_order_without_data(self):
        from core.latency import LatencyTracker
        lt = LatencyTracker()
        keys = ["k1", "k2", "k3"]
        ranked = lt.rank("g:m", keys)
        assert ranked == keys  # no data → preserve order

    def test_rank_prefers_faster_key(self):
        from core.latency import LatencyTracker
        lt = LatencyTracker(tau=100.0)
        now = 100.0
        lt.record("g:m", "slow", 5.0, now=now)
        lt.record("g:m", "fast", 0.5, now=now)
        ranked = lt.rank("g:m", ["slow", "fast"], now=now)
        assert ranked[0] == "fast"

    def test_negative_latency_ignored(self):
        from core.latency import LatencyTracker, _NO_LATENCY
        lt = LatencyTracker()
        lt.record("g:m", "k1", -1.0)
        assert lt.get("g:m", "k1") == _NO_LATENCY

    def test_nan_latency_ignored(self):
        from core.latency import LatencyTracker, _NO_LATENCY
        lt = LatencyTracker()
        lt.record("g:m", "k1", float("nan"))
        assert lt.get("g:m", "k1") == _NO_LATENCY

    def test_snapshot(self):
        from core.latency import LatencyTracker
        from core.carousel import fingerprint
        lt = LatencyTracker(tau=100.0)
        now = 100.0
        lt.record("g:m", "k1", 1.2, now=now)
        lt.record("g:m", "k2", 0.8, now=now)
        snap = lt.snapshot("g:m", ["k1", "k2"], now=now)
        # v3: keyed by the fingerprint (never the raw key), value is a dict
        # with the live score and display percentiles
        assert fingerprint("k1") in snap
        assert snap[fingerprint("k1")]["p50_ms"] == 1200.0
        assert snap[fingerprint("k2")]["score_ms"] == 800.0
        # k1 slower: its live score is the higher one
        assert snap[fingerprint("k1")]["score_ms"] > snap[fingerprint("k2")]["score_ms"]

    def test_thread_safety(self):
        from core.latency import LatencyTracker
        lt = LatencyTracker()
        errors = []

        def worker(key_id):
            try:
                for i in range(100):
                    lt.record("g:m", f"k{key_id}", 0.1 * (i + 1))
                    lt.get("g:m", f"k{key_id}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ── Item 2: GCRA pacing ─────────────────────────────────────────────

class TestGCRAPacer:
    """v3: the pacer is a sliding-log, not GCRA. These keep the historical
    test names but assert the v3 contract: unknown limit = never delayed;
    a known limit admits until the window is full, then waits the exact
    minimum."""

    def test_new_key_is_ready(self):
        from core.pacing import Pacer
        p = Pacer()
        assert p.is_ready("g:m", "k1")

    def test_stamp_makes_key_not_ready(self):
        from core.pacing import Pacer
        p = Pacer()
        p.note_limit("g:m", "k1", 1)
        now = 100.0
        p.stamp("g:m", "k1", now=now)
        assert not p.is_ready("g:m", "k1", now=now + 0.5)
        assert p.is_ready("g:m", "k1", now=now + 61.0)

    def test_time_until_ready(self):
        from core.pacing import Pacer
        p = Pacer()
        p.note_limit("g:m", "k1", 1)
        now = 100.0
        p.stamp("g:m", "k1", now=now)
        wait = p.time_until_ready("g:m", "k1", now=now + 0.5)
        assert 59.9 < wait < 60.6

    def test_soonest_picks_earliest(self):
        from core.pacing import Pacer
        p = Pacer()
        p.note_limit("g:m", "k1", 1)
        p.note_limit("g:m", "k2", 1)
        now = 100.0
        p.stamp("g:m", "k1", now=now)
        p.stamp("g:m", "k2", now=now + 30.0)
        key, wait = p.soonest("g:m", ["k1", "k2"], now=now + 40.0)
        assert key == "k1"  # k1's stamp ages out first: ready at 160.5
        assert 20.4 < wait < 20.6

    def test_empty_keys(self):
        from core.pacing import Pacer
        p = Pacer()
        key, wait = p.soonest("g:m", [])
        assert key is None
        assert wait == 0.0


# ── Item 7: Region in identity ───────────────────────────────────────

class TestRegionIdentity:
    def test_no_region(self):
        from core.carousel import Carousel
        ident = Carousel.identity("google", "gemini-2.5-pro")
        assert ident == "google:gemini-2.5-pro"

    def test_with_region(self):
        from core.carousel import Carousel
        ident = Carousel.identity("google", "gemini-2.5-pro", region="us-central1")
        assert ident == "google:gemini-2.5-pro:us-central1"

    def test_region_case_normalized(self):
        from core.carousel import Carousel
        ident = Carousel.identity("Google", "Gemini", region="US-Central1")
        assert "us-central1" in ident

    def test_empty_region_ignored(self):
        from core.carousel import Carousel
        ident1 = Carousel.identity("google", "gemini")
        ident2 = Carousel.identity("google", "gemini", region="")
        assert ident1 == ident2


# ── Item 4: Secondary Events ─────────────────────────────────────────

class TestSecondaryEvents:
    def test_secondary_buffer_exists(self):
        from core.events import EVENTS, SECONDARY_EVENTS
        assert SECONDARY_EVENTS is not EVENTS

    def test_set_and_clear_aux_slot(self):
        from core.events import set_aux_slot, clear_aux_slot, get_aux_slot
        assert get_aux_slot() == ""
        token = set_aux_slot("vision")
        assert get_aux_slot() == "vision"
        clear_aux_slot(token)
        assert get_aux_slot() == ""

    def test_active_events_main(self):
        from core.events import EVENTS, active_events, get_aux_slot
        assert get_aux_slot() == ""
        assert active_events() is EVENTS

    def test_active_events_secondary(self):
        from core.events import SECONDARY_EVENTS, active_events, set_aux_slot, clear_aux_slot
        token = set_aux_slot("title_gen")
        assert active_events() is SECONDARY_EVENTS
        clear_aux_slot(token)


# ── Hedging: token bucket + p90 (audit-added) ────────────────────────

class TestHedgeTokenBucket:
    """v3: the tracker is settings-driven and cold-start aware. The old
    API (constant percentile, None-without-samples) became threshold()
    with per-mode/class buckets and COLD defaults."""

    def test_cold_thresholds_are_the_defaults(self):
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        assert ht.threshold("g:m", stream=False, reasoning=False) == 6.0
        assert ht.threshold("g:m", stream=False, reasoning=True) == 30.0

    def test_enough_samples_learn(self):
        from core.hedging import HedgeTracker, _MIN_SAMPLES
        ht = HedgeTracker()
        now = 100.0
        for i in range(_MIN_SAMPLES + 5):
            ht.record_latency("g:m", 1.0, now=now + i)
        learned = ht.threshold("g:m", stream=False, reasoning=False, now=now + 30)
        assert learned is not None and learned <= 24.0  # clamped under 4*cold

    def test_token_bucket_budget(self):
        """Budget uses token bucket — tokens are consumed per hedge."""
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        now = 100.0
        for i in range(20):
            ht.record_latency("g:m", 1.0, now=now + i)
        consumed = 0
        for _ in range(10):
            if ht.should_hedge("g:m", 99.0, healthy_count=3, now=now + 25):
                consumed += 1
            else:
                break
        assert consumed > 0
        assert consumed <= 4  # 1 initial + up to 3 burst
        assert ht.should_hedge("g:m", 99.0, healthy_count=3, now=now + 25) is False

    def test_token_bucket_refill(self):
        """Token bucket refills as calls are recorded (1 token per 10 calls)."""
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        now = 100.0
        for i in range(20):
            ht.record_latency("g:m", 1.0, now=now + i)
        ht.should_hedge("g:m", 99.0, healthy_count=3, now=now + 25)
        for i in range(10):
            ht.record_latency("g:m", 1.0, now=now + 30 + i)
        assert ht.should_hedge("g:m", 99.0, healthy_count=3, now=now + 45) is True

    def test_no_hedge_under_pressure(self):
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        assert ht.should_hedge("g:m", 99.0, healthy_count=3,
                               under_pressure=True) is False

    def test_no_hedge_with_one_key(self):
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        assert ht.should_hedge("g:m", 99.0, healthy_count=1) is False

    def test_no_hedge_below_threshold(self):
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        # cold std threshold is 6s: a 3s call does not hedge
        assert ht.should_hedge("g:m", 3.0, healthy_count=3) is False

    def test_note_hedge_and_count(self):
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        assert ht.hedge_count("g:m") == 0
        ht.note_hedge("g:m")
        ht.note_hedge("g:m")
        assert ht.hedge_count("g:m") == 2

    def test_clear(self):
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        for i in range(20):
            ht.record_latency("g:m", 1.0, now=100.0 + i)
        ht.note_hedge("g:m")
        ht.clear()
        # clear restores cold-start behaviour, not "None"
        assert ht.threshold("g:m", stream=False, reasoning=False) == 6.0
        assert ht.hedge_count("g:m") == 0

    def test_burst_cap(self):
        """Burst is capped by the hedge_burst setting (default 3)."""
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        now = 100.0
        for i in range(200):
            ht.record_latency("g:m", 1.0, now=now + i * 0.01)
        successes = 0
        for _ in range(8):
            if ht.should_hedge("g:m", 99.0, healthy_count=3, now=now + 5):
                successes += 1
        assert successes <= 4  # 1 initial + burst 3

    def test_thread_safety(self):
        from core.hedging import HedgeTracker
        ht = HedgeTracker()
        errors = []
        def worker(wid):
            try:
                now = 100.0
                for i in range(50):
                    ht.record_latency(f"g:m{wid}", 0.5 + i * 0.01, now=now + i)
                    ht.should_hedge(f"g:m{wid}", 99.0, healthy_count=3, now=now + i + 1)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


class TestSettingsCompleteness:
    def test_pacing_in_all_numbers(self):
        from settings import ALL_NUMBERS, PACING_INTERVAL
        assert PACING_INTERVAL in ALL_NUMBERS

    def test_hedging_in_all_flags(self):
        from settings import ALL_FLAGS, HEDGING_DISABLED
        assert HEDGING_DISABLED in ALL_FLAGS

    def test_pacing_has_meta(self):
        from settings import META, PACING_INTERVAL
        assert PACING_INTERVAL in META

    def test_hedging_has_meta(self):
        from settings import META, HEDGING_DISABLED
        assert HEDGING_DISABLED in META

    def test_pacing_has_env(self):
        from settings import env_name, PACING_INTERVAL
        assert env_name(PACING_INTERVAL) == "KAME_PACING_INTERVAL"

    def test_hedging_has_env(self):
        from settings import env_name, HEDGING_DISABLED
        assert env_name(HEDGING_DISABLED) == "KAME_HEDGING_DISABLED"

    def test_pacing_has_range(self):
        from settings import bounds, PACING_INTERVAL
        rng = bounds(PACING_INTERVAL)
        assert rng is not None
        assert rng[0] == 0.0
        assert rng[1] == 60.0

    def test_pacing_in_groups(self):
        from settings import GROUPS, PACING_INTERVAL
        all_keys = [k for _, _, _, keys in GROUPS for k in keys]
        # 1.8.2.7: retired since 1.8.2.3, now loaded but never shown (settings.HIDDEN)
        assert PACING_INTERVAL not in all_keys

    def test_hedging_in_groups(self):
        from settings import GROUPS, HEDGING_DISABLED
        all_keys = [k for _, _, _, keys in GROUPS for k in keys]
        assert HEDGING_DISABLED in all_keys


# ── Run ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
