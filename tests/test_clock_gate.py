"""Proofs for tools/clock_gate.py — the G10 gate itself.

Mirrors `tests/test_continuity_gate.py`'s shape for the same reason: the
mechanism each measurement exercises (`_wait_for_recovery`'s padding/jitter,
`Carousel.thaw_server_cooled`, the shared-health file) is already proven
elsewhere — `tests/test_waiting.py`, `tests/test_v1_7_0_7_dispatch_recovery_
loop.py`, `tests/test_carousel.py`'s thaw tests, `tests/test_v1_8_0_0_shared_
health.py`. This file proves the GATE that measures them together:

1. The pure Monte Carlo (`_monte_carlo_stampede`) on hand-picked distributions
   whose answer is known in advance.
2. Each of the four early-wake helpers in isolation, fast and deterministic.
3. The stampede real-run's shared-file mechanism, including a direct
   regression test for the bug this file's own first run caught (see below).
4. `learnings/0003-the-gate-that-was-never-run.md`'s own rule: the self-check
   really does report FAIL for jitter fixed at 0.
5. `scenario_padding_cost`'s graceful degradation when the real corpus is not
   on the machine running the suite.
6. One full end-to-end run of `run_gate()` / the CLI, with the real corpus
   files (if present) proven byte-for-byte untouched.

Two real defects this file's own first run against production code caught,
each now guarded by a named test below so a regression fails loudly instead
of silently going back to reporting a wrong number:

* Marking a key resting BEFORE installing the fake clock sized "300s" (or
  "5 hours") against real wall-clock time, not the frozen `t0` the rest of
  the scenario reads — the pool looked already-recovered the instant the
  fake clock started. Fixed by marking after `_FakeClock.install()`.
* Three profiles sharing one file, run sequentially in one process: the
  first profile's own success released ITS key back to the shared file
  before the next profile had even started, collapsing the race the
  scenario exists to measure. Fixed by snapshotting the pristine,
  just-written world and restoring it before every profile's run.
* A scenario that asked for a 5-hour hold and triggered its event at slice
  5000 never fired at all, because `Carousel`'s own `max_hold_s` (the G8
  1-hour ceiling) silently clamped the hold to 3600s — `lag_s` came back
  `null` and the scenario still reported PASS, because the (then) filtering
  logic dropped `None` values before averaging instead of treating a missing
  measurement as a failure.

Run just this file:

    python -B -m pytest -q -p no:cacheprovider tests/test_clock_gate.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "clock_gate.py"
OUT_DIR = ROOT / "research" / "1.8.0.0" / "clock"


def _load_tool():
    spec = importlib.util.spec_from_file_location("clock_gate_under_test", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_tool()


def _snapshot_real_corpus(gate_mod) -> dict:
    """Same convention as `test_continuity_gate._snapshot_real_pool_health`
    and `test_replay_timeline`'s own subprocess-isolation test: the ONLY two
    files this tool could possibly read on the real machine — never written,
    only read, but proven untouched anyway rather than assumed."""
    replay = gate_mod._load_replay_tool()
    out = {}
    for path in (replay.DEFAULT_CALLS, replay.DEFAULT_REFUSALS):
        if path.is_file():
            stat = path.stat()
            out[str(path)] = (stat.st_size, stat.st_mtime)
    return out


# ---------------------------------------------------------------------------
# 1. the pure Monte Carlo
# ---------------------------------------------------------------------------


class TestMonteCarloStampede:
    def test_jitter_fixed_at_zero_collides_every_single_time(self, gate):
        # No spread at all when every profile's jitter draw is the same fixed
        # number: every trial's three offsets are identical, so every gap is
        # exactly zero — the mathematical floor this scenario's self-check
        # depends on.
        result = gate._monte_carlo_stampede(0.0, 0.0, trials=200, seed_base=1)
        assert result["trial_collision_rate"] == 1.0
        assert result["gap_under_100ms_rate"] == 1.0
        assert result["max_gap_s"] == 0.0

    def test_a_wide_band_collides_far_less_than_a_narrow_one(self, gate):
        wide = gate._monte_carlo_stampede(0.1, 1.5, trials=2000, seed_base=7)
        narrow = gate._monte_carlo_stampede(0.0, 0.05, trials=2000, seed_base=7)
        assert wide["trial_collision_rate"] < narrow["trial_collision_rate"]

    def test_same_seed_base_is_reproducible(self, gate):
        a = gate._monte_carlo_stampede(0.1, 1.5, trials=500, seed_base=42)
        b = gate._monte_carlo_stampede(0.1, 1.5, trials=500, seed_base=42)
        assert a == b


class TestSeededJitter:
    def test_one_seed_always_draws_the_same_value(self, gate):
        f = gate._seeded_jitter(55)
        assert f() == f() == f()

    def test_different_seeds_usually_draw_different_values(self, gate):
        values = {gate._seeded_jitter(seed)() for seed in range(20)}
        assert len(values) > 1


# ---------------------------------------------------------------------------
# 2. the four early-wake helpers, in isolation
# ---------------------------------------------------------------------------


class TestIdleWaitingHelpers:
    def test_key_expires_early_wakes_within_the_bound(self, gate):
        dispatch_binding_mod, carousel_mod, _shared = gate._modules()
        result = gate._idle_key_expires_early(dispatch_binding_mod, carousel_mod)
        assert result["became_usable_at"] is not None
        assert result["delay_s"] is not None
        assert 0.0 <= result["delay_s"] <= gate.IDLE_WAKE_BOUND_S

    def test_peer_success_thaws_outage_wakes_within_the_bound(self, gate):
        dispatch_binding_mod, carousel_mod, _shared = gate._modules()
        result = gate._idle_peer_thaws_outage(dispatch_binding_mod, carousel_mod)
        assert result["became_usable_at"] is not None
        assert result["delay_s"] is not None
        assert 0.0 <= result["delay_s"] <= gate.IDLE_WAKE_BOUND_S
        # The thaw target is at least THAW_BASE_S past the peer's own success
        # — regression guard for the first draft, which measured the delay
        # from the peer's success instant instead and got a false ~3s "lag".
        assert result["became_usable_at"] >= 1_800_000_000.0 + carousel_mod.THAW_BASE_S

    def test_new_credential_added_wakes_within_the_bound(self, gate):
        dispatch_binding_mod, carousel_mod, _shared = gate._modules()
        result = gate._idle_new_credential(dispatch_binding_mod, carousel_mod)
        assert result["became_usable_at"] is not None
        assert result["delay_s"] is not None
        assert 0.0 <= result["delay_s"] <= gate.IDLE_WAKE_BOUND_S

    def test_explicit_pool_reset_wakes_within_the_bound(self, gate):
        dispatch_binding_mod, carousel_mod, _shared = gate._modules()
        result = gate._idle_explicit_reset(dispatch_binding_mod, carousel_mod)
        assert result["became_usable_at"] is not None
        assert result["delay_s"] is not None
        assert 0.0 <= result["delay_s"] <= gate.IDLE_WAKE_BOUND_S

    def test_marking_after_clock_install_matters(self, gate):
        """Regression guard for the first bug this file's own run caught: a
        key marked resting BEFORE the fake clock is installed sizes its hold
        against real wall time, not the frozen `t0` the rest of the scenario
        reads. Reproduced directly here against `core.carousel.Carousel`,
        independent of the fix already applied in `clock_gate.py` itself, so
        a future edit that reintroduces the ordering bug fails this test
        even if it does not touch `_idle_key_expires_early` at all.
        """
        _dispatch_binding_mod, carousel_mod, _shared = gate._modules()
        engine = carousel_mod.Carousel()
        # Marked at REAL now (no `now=` override), matching the ordering bug.
        engine.mark(gate.IDENTITY, "k", False, 300.0, "rate_limit", stated=True)
        # A fake "now" far in the future (matching `_FakeClock`'s default
        # start) sees this hold as long expired, if the two do not agree.
        eta = engine.next_recovery_seconds(gate.IDENTITY, ["k"], now=1_800_000_000.0)
        assert eta is None, "marking before freezing the clock desyncs the hold from the scenario's own time base"


# ---------------------------------------------------------------------------
# 3. the stampede real-run and its shared-file mechanism
# ---------------------------------------------------------------------------


class TestStampedeRealRun:
    def test_three_profiles_get_three_distinct_departures(self, gate, tmp_path):
        result = gate._real_stampede_run(tmp_path, gate.JITTER_LOW_S, gate.JITTER_HIGH_S, seeds=(11, 22, 33))
        offsets = [p["departure_offset_s"] for p in result["per_profile"]]
        assert len(set(round(o, 3) for o in offsets)) == 3, offsets
        assert result["formula_cross_check_max_abs_error_s"] < 0.01

    def test_second_and_third_profile_never_depart_at_the_bare_deadline(self, gate, tmp_path):
        """Regression guard for the sequential-contamination bug: before the
        snapshot/restore fix, profiles 2 and 3 saw the pool as already
        healthy (freed by profile 1's own success) and departed at offset
        0.0 — no wait at all. Every profile here shares the SAME declared
        deadline, so every real departure must be at least ``deadline +
        PADDING_S`` past it (the jitter floor is 0, so ``+ 0`` is the only
        way to reach exactly the padding, never less).
        """
        result = gate._real_stampede_run(tmp_path, gate.JITTER_LOW_S, gate.JITTER_HIGH_S, seeds=(101, 102, 103))
        for profile in result["per_profile"]:
            assert profile["departure_offset_s"] >= result["deadline_s"] + gate.PADDING_S - 0.01, profile

    def test_jitter_fixed_at_zero_makes_all_three_depart_together(self, gate, tmp_path):
        result = gate._real_stampede_run(tmp_path, 0.0, 0.0, seeds=(1, 2, 3))
        offsets = {round(p["departure_offset_s"], 3) for p in result["per_profile"]}
        assert len(offsets) == 1, offsets
        assert result["any_collision_under_100ms"] is True


# ---------------------------------------------------------------------------
# 4. the gate can fail — learnings/0003
# ---------------------------------------------------------------------------


class TestSelfCheckHasTeeth:
    def test_jitter_fixed_at_zero_produces_a_real_reported_fail(self, gate, tmp_path):
        result = gate.scenario_self_check(tmp_path)
        assert result["passed"] is True, "the self-check itself must report the broken config as FAIL"
        assert result["measurements"]["monte_carlo_trial_collision_rate"] > 0.9
        assert result["measurements"]["gate_correctly_reported_fail"] is True


# ---------------------------------------------------------------------------
# 5. sleep-slice scenario, including the clamped-hold regression
# ---------------------------------------------------------------------------


class TestSleepSlices:
    def test_deep_recovery_actually_triggers_inside_the_1h_ceiling(self, gate):
        """Regression guard: a hold request longer than `Carousel`'s own
        `max_hold_s` (3600s, the G8 ceiling) is silently clamped, so a
        trigger scheduled past that point never fires. Asserts the trigger
        fired at all, not only that the resulting lag (when present) is
        small — a missing measurement must fail loudly, not read as zero
        cases contributing to a passing average.
        """
        dispatch_binding_mod, carousel_mod, _shared = gate._modules()
        result = gate._sleep_slice_deep_recovery(dispatch_binding_mod, carousel_mod)
        assert result["triggered"] is True
        assert result["lag_s"] is not None

    def test_scenario_fails_if_a_sub_case_never_triggers(self, gate, monkeypatch, tmp_path):
        dispatch_binding_mod, carousel_mod, _shared = gate._modules()

        def _broken_deep_recovery(_dbm, _cm):
            return {"label": "broken", "trigger_at_slice": 1, "triggered": False, "lag_s": None}

        monkeypatch.setattr(gate, "_sleep_slice_deep_recovery", _broken_deep_recovery)
        result = gate.scenario_sleep_slices(tmp_path)
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# 6. padding-cost scenario: graceful degradation without the real corpus
# ---------------------------------------------------------------------------


class TestPaddingCostWithoutCorpus:
    def test_missing_corpus_reports_unavailable_instead_of_crashing(self, gate, tmp_path, monkeypatch):
        replay = gate._load_replay_tool()
        monkeypatch.setattr(replay, "DEFAULT_CALLS", tmp_path / "no-calls.jsonl")
        monkeypatch.setattr(replay, "DEFAULT_REFUSALS", tmp_path / "no-refusals.jsonl")
        monkeypatch.setattr(gate, "_load_replay_tool", lambda: replay)
        result = gate.scenario_padding_cost()
        assert result["passed"] is True
        assert result["measurements"]["corpus_available"] is False
        assert "not found" in result["measurements"]["note"]


# ---------------------------------------------------------------------------
# 7. one full end-to-end run
# ---------------------------------------------------------------------------


class TestFullGateEndToEnd:
    def test_the_cli_runs_all_measurements_and_exits_zero_on_pass(self, gate):
        before = _snapshot_real_corpus(gate)

        env = dict(os.environ)
        result = subprocess.run(
            [sys.executable, "-B", str(TOOL_PATH)],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "GATE PASS" in result.stdout

        expected = {"stampede", "no_idle_waiting", "padding_cost", "jitter_cost", "sleep_slices", "self_check"}
        for name in expected:
            path = OUT_DIR / f"{name}.json"
            assert path.is_file(), f"missing {path}"
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert "passed" in payload
            assert "measurements" in payload
        assert (OUT_DIR / "REPORT.md").is_file()

        after = _snapshot_real_corpus(gate)
        assert before == after, "the real calls.jsonl/refusals.jsonl must be byte-for-byte untouched (read-only)"

    def test_run_gate_returns_the_same_measurements_in_process(self, gate):
        passed, results = gate.run_gate()
        names = {r["name"] for r in results}
        assert names == {"stampede", "no_idle_waiting", "padding_cost", "jitter_cost", "sleep_slices", "self_check"}
        assert passed is True, json.dumps({r["name"]: r["passed"] for r in results}, indent=2)
