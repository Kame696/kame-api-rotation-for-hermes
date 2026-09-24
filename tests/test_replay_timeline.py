"""Proofs for tools/replay_timeline.py, on synthetic tiny timelines only.

No real evidence file is read here (agent.log boundaries are always injected
directly except in the one subprocess-isolation test, whose synthetic call
timestamps are chosen far outside the real log's range so it never actually
matches a real registration). Pinned:

1. Clock control — a failure replayed at a fake ``at`` far from wall-clock
   time sizes its hold relative to that ``at``, not to ``time.time()``.
2. The two cross-checks (``contradicted_holds``, ``idle_with_key_available``)
   compute correctly on hand-made event sequences.
3. Subprocess isolation — invoking the tool as a real subprocess redirects
   HERMES_HOME to a fresh temp directory and never touches the owner's real
   AppData evidence, even though the tool loads the real plugin package.
4. agent.log parsing (build-registration lines -> epoch, close-registration
   flagging, per-hash window computation) is correct.
5. A restart between two failures resets the carousel's memory — reproducing,
   with the real plugin's own ``_escalate`` daily-quota logic, the exact
   300s-vs-3600s divergence measured against production.
6. A non-marking verdict (e.g. a terminal "raise") records ``hold_s = null``
   and is excluded from the holds aggregate.

Run: python -B -m pytest -q -p no:cacheprovider tests/test_replay_timeline.py
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
TOOL_PATH = ROOT / "tools" / "replay_timeline.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("replay_timeline_under_test", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = _load_tool()


# ---------------------------------------------------------------------------
# Matching a failed call to its refusal payload
# ---------------------------------------------------------------------------


class TestMatchRefusals:
    def test_nearest_within_window_same_status_and_model_wins(self):
        calls = [
            {
                "at": 100.0, "identity": "google:gemini-3.7-flash", "key": "key:aaa",
                "outcome": "rotate", "status": 429, "kind": "rate_limit", "attempt": 1,
            }
        ]
        refusals = [
            {"at": 90.0, "model": "google:gemini-3.7-flash", "status": 429, "message": "too far", "body": "", "response": ""},
            {"at": 101.2, "model": "google:gemini-3.7-flash", "status": 429, "message": "near one", "body": "", "response": ""},
            {"at": 100.1, "model": "some:other-model", "status": 429, "message": "wrong model", "body": "", "response": ""},
        ]
        stats = rt.match_refusals(calls, refusals)
        assert stats == {"matched": 1, "unmatched": 0}
        assert calls[0]["_refusal"]["message"] == "near one"

    def test_bare_model_name_matches_identity_suffix(self):
        calls = [
            {
                "at": 50.0, "identity": "openai-codex:gpt-5.6-luna", "key": "key:bbb",
                "outcome": "raise", "status": 401, "kind": "auth", "attempt": 1,
            }
        ]
        refusals = [
            {"at": 50.5, "model": "gpt-5.6-luna", "status": 401, "message": "bare name", "body": "", "response": ""},
        ]
        stats = rt.match_refusals(calls, refusals)
        assert stats["matched"] == 1
        assert calls[0]["_refusal"]["message"] == "bare name"

    def test_outside_window_or_wrong_status_is_unmatched(self):
        calls = [
            {"at": 100.0, "identity": "x:y", "key": "key:a", "outcome": "raise", "status": 500, "kind": "server", "attempt": 1},
        ]
        refusals = [
            {"at": 200.0, "model": "x:y", "status": 500, "message": "too far away", "body": "", "response": ""},
            {"at": 100.1, "model": "x:y", "status": 503, "message": "wrong status", "body": "", "response": ""},
        ]
        stats = rt.match_refusals(calls, refusals)
        assert stats == {"matched": 0, "unmatched": 1}
        assert calls[0]["_refusal"] is None

    def test_matching_is_without_replacement(self):
        calls = [
            {"at": 100.0, "identity": "x:y", "key": "key:a", "outcome": "rotate", "status": 429, "kind": "rate_limit", "attempt": 1},
            {"at": 100.5, "identity": "x:y", "key": "key:b", "outcome": "rotate", "status": 429, "kind": "rate_limit", "attempt": 1},
        ]
        refusals = [
            {"at": 100.2, "model": "x:y", "status": 429, "message": "only one candidate", "body": "", "response": ""},
        ]
        stats = rt.match_refusals(calls, refusals)
        assert stats == {"matched": 1, "unmatched": 1}
        matched_rows = [c for c in calls if c["_refusal"] is not None]
        assert len(matched_rows) == 1

    def test_answered_rows_are_left_untouched(self):
        calls = [{"at": 1.0, "identity": "x:y", "key": "key:a", "outcome": "answered", "status": None, "kind": ""}]
        refusals = []
        rt.match_refusals(calls, refusals)
        assert "_refusal" not in calls[0]


# ---------------------------------------------------------------------------
# ReplayState — contradicted_holds and idle_with_key_available
# ---------------------------------------------------------------------------


class TestReplayState:
    def test_answer_inside_the_hold_window_is_contradicted_once(self):
        state = rt.ReplayState()
        state.register_hold("id1", "key:a", at=100.0, hold_s=50.0)  # held until 150
        state.check_idle_before_success("id1", "key:a", at=120.0)
        state.register_success("id1", "key:a", at=120.0)
        assert state.contradicted_count == 1
        assert state.contradicted_seconds_lost == pytest.approx(30.0)  # 150 - 120

        # A second answer after the hold already closed must not double-count.
        state.register_hold("id1", "key:a", at=200.0, hold_s=10.0)
        state.register_success("id1", "key:a", at=205.0)
        assert state.contradicted_count == 2  # the second hold's own contradiction
        assert state.contradicted_seconds_lost == pytest.approx(30.0 + 5.0)

    def test_answer_after_the_hold_ends_is_not_contradicted(self):
        state = rt.ReplayState()
        state.register_hold("id1", "key:a", at=100.0, hold_s=10.0)  # until 110
        state.check_idle_before_success("id1", "key:a", at=200.0)
        state.register_success("id1", "key:a", at=200.0)
        assert state.contradicted_count == 0
        assert state.contradicted_seconds_lost == 0.0

    def test_answer_on_a_different_key_does_not_contradict_another_keys_hold(self):
        state = rt.ReplayState()
        state.register_hold("id1", "key:a", at=100.0, hold_s=1000.0)
        state.register_success("id1", "key:b", at=150.0)
        assert state.contradicted_count == 0

    def test_idle_fires_when_every_previously_seen_key_is_held(self):
        state = rt.ReplayState()
        state.register_hold("id1", "key:a", at=100.0, hold_s=1000.0)  # held until 1100
        # A key never seen before answers while the only known key is held.
        idle = state.check_idle_before_success("id1", "key:b", at=150.0)
        assert idle is not None
        assert idle["keys_checked"] == 1
        assert idle["time_until_release"] == pytest.approx(1100.0 - 150.0)

    def test_idle_does_not_fire_when_a_known_key_is_free(self):
        state = rt.ReplayState()
        state.register_hold("id1", "key:a", at=100.0, hold_s=10.0)  # until 110
        state.register_success("id1", "key:a", at=115.0)  # key:a now known and free
        idle = state.check_idle_before_success("id1", "key:b", at=120.0)
        assert idle is None

    def test_idle_does_not_fire_on_the_very_first_call(self):
        state = rt.ReplayState()
        idle = state.check_idle_before_success("id1", "key:a", at=1.0)
        assert idle is None


# ---------------------------------------------------------------------------
# Hold statistics
# ---------------------------------------------------------------------------


class TestHoldStats:
    def test_percentiles_and_thresholds(self):
        from collections import Counter

        stats = rt.hold_stats([10.0, 400.0, 2000.0, 4000.0], Counter({"rate_limit": 2, "daily": 2}), Counter({429: 3, None: 1}))
        assert stats["count"] == 4
        assert stats["over_300s"] == 3
        assert stats["over_1800s"] == 2
        assert stats["over_3600s"] == 1
        assert stats["max"] == 4000.0
        assert stats["by_kind"] == {"rate_limit": 2, "daily": 2}


# ---------------------------------------------------------------------------
# Clock control — the real plugin, in-process (safe: tests/conftest.py has
# already redirected HERMES_HOME for this whole pytest session before this
# module's imports even ran).
# ---------------------------------------------------------------------------


class TestClockControl:
    def test_on_failure_sizes_the_hold_relative_to_record_at_not_wall_clock(self):
        plugin = rt.load_plugin(PLUGIN_DIR, package_name="kame_replay_clock_control_test")
        dispatch_binding_mod = importlib.import_module("kame_replay_clock_control_test.dispatch_binding")
        carousel_mod = importlib.import_module("kame_replay_clock_control_test.core.carousel")

        engine = carousel_mod.Carousel()
        binding = dispatch_binding_mod.DispatchBinding(engine=engine)

        fake_at = 1_700_000_000.0  # a fixed point far from wall-clock "now"
        real_wall_now = dispatch_binding_mod.time.time()
        assert abs(real_wall_now - fake_at) > 1_000_000, "the fixture assumption needs updating"

        exc = Exception("429 Too Many Requests")
        exc.status_code = 429
        exc.retry_after = 12.0

        clock = {"now": fake_at}
        real_time_fn = dispatch_binding_mod.time.time
        real_monotonic_fn = dispatch_binding_mod.time.monotonic
        dispatch_binding_mod.time.time = lambda: clock["now"]
        dispatch_binding_mod.time.monotonic = lambda: clock["now"]
        try:
            binding._on_failure(
                "prov:model", "key:zzz", exc, "test", 1, False, credential_id="key:zzz",
            )
        finally:
            dispatch_binding_mod.time.time = real_time_fn
            dispatch_binding_mod.time.monotonic = real_monotonic_fn

        sick_until = engine._pools["prov:model"]["key:zzz"]["sick_until"]
        assert sick_until > fake_at, "a 429 must set a hold ending after the record's own time"
        assert sick_until - fake_at < 3600.0 * 24, "the hold must be a plausible offset from fake_at"
        assert abs(sick_until - real_wall_now) > 1_000_000, (
            "the hold landed near real wall-clock time — clock control did not work"
        )


# ---------------------------------------------------------------------------
# Subprocess isolation
# ---------------------------------------------------------------------------


def _snapshot_real_evidence() -> dict:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "plugin-data" / "hermes-kame-api-rotation"
    out = {}
    if not base.is_dir():
        return out
    # state.json deliberately excluded: it is a live snapshot the real,
    # separately-running Hermes process republishes on its own schedule,
    # unrelated to whether this test's subprocess is isolated. The files
    # that actually matter — the ones a leak would silently grow — are the
    # append-only evidence logs.
    for name in ("calls.jsonl", "refusals.jsonl", "settings-changes.jsonl"):
        candidate = base / name
        if candidate.is_file():
            stat = candidate.stat()
            out[name] = (stat.st_size, stat.st_mtime)
    return out


class TestSubprocessIsolation:
    def test_replay_tool_isolates_hermes_home_and_never_touches_real_appdata(self, tmp_path):
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        out_path = tmp_path / "out.json"

        calls_rows = [
            {
                "at": 1_800_000_000.0, "identity": "google:gemini-3.7-flash", "key": "key:aaa",
                "attempt": 1, "outcome": "rotate", "kind": "server", "status": 503,
            },
            {
                "at": 1_800_000_060.0, "identity": "google:gemini-3.7-flash", "key": "key:bbb",
                "attempt": 1, "outcome": "answered", "kind": "", "status": None,
            },
        ]
        refusal_rows = [
            {
                "at": 1_800_000_000.2, "provider": "google", "model": "google:gemini-3.7-flash",
                "status": 503, "type": "Boom", "code": "", "message": "503 Service Unavailable",
                "body": "", "response": "",
            }
        ]
        calls_path.write_text("\n".join(json.dumps(r) for r in calls_rows) + "\n", encoding="utf-8")
        refusals_path.write_text("\n".join(json.dumps(r) for r in refusal_rows) + "\n", encoding="utf-8")

        before = _snapshot_real_evidence()

        env = dict(os.environ)
        env.pop("HERMES_HOME", None)  # prove the TOOL sets it, not an inherited value
        result = subprocess.run(
            [
                sys.executable, "-B", str(TOOL_PATH),
                "--plugin-dir", str(PLUGIN_DIR),
                "--calls", str(calls_path),
                "--refusals", str(refusals_path),
                "--out", str(out_path),
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert out_path.is_file()

        metrics = json.loads(out_path.read_text(encoding="utf-8"))
        hermes_home = Path(metrics["isolation"]["hermes_home"])
        real_home = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"

        assert hermes_home != real_home
        assert real_home not in hermes_home.parents
        assert tempfile.gettempdir().lower() in str(hermes_home).lower()
        assert "kame-replay-home-" in hermes_home.name

        assert metrics["counts"]["successes_replayed"] == 1
        assert metrics["counts"]["failures_replayed"] == 1
        assert metrics["matching"]["matched"] == 1

        after = _snapshot_real_evidence()
        assert before == after, "the real AppData evidence files must be byte-for-byte untouched"


# ---------------------------------------------------------------------------
# agent.log parsing: build-registration lines, close registrations, windows
# ---------------------------------------------------------------------------


class TestAgentLogBoundaries:
    def test_parses_build_lines_and_converts_local_time_with_mktime(self, tmp_path):
        log = tmp_path / "agent.log"
        log.write_text(
            "2026-01-01 00:00:00,000 INFO hermes_plugins.hermes_kame_api_rotation: "
            "hermes-kame-api-rotation: build aaaa1111 � complete\n"
            "2026-01-01 00:00:01,000 INFO some.other.module: unrelated line\n"
            "2026-01-01 00:05:00,500 INFO hermes_plugins.hermes_kame_api_rotation: "
            "hermes-kame-api-rotation: build bbbb2222 � complete\n",
            encoding="utf-8",
        )
        boundaries = rt.parse_agent_log_boundaries([log])
        assert [h for _, h in boundaries] == ["aaaa1111", "bbbb2222"]
        expected_first = time.mktime(time.strptime("2026-01-01 00:00:00", "%Y-%m-%d %H:%M:%S"))
        expected_second = time.mktime(time.strptime("2026-01-01 00:05:00", "%Y-%m-%d %H:%M:%S")) + 0.5
        assert boundaries[0][0] == pytest.approx(expected_first)
        assert boundaries[1][0] == pytest.approx(expected_second)

    def test_reads_across_several_rotated_files_and_sorts_by_time(self, tmp_path):
        older = tmp_path / "agent.log.1"
        newer = tmp_path / "agent.log"
        older.write_text(
            "2026-01-01 00:00:00,000 INFO x hermes-kame-api-rotation: build 1a2b3c4d complete\n",
            encoding="utf-8",
        )
        newer.write_text(
            "2026-01-02 00:00:00,000 INFO x hermes-kame-api-rotation: build 5e6f7a8b complete\n",
            encoding="utf-8",
        )
        # Passed newest-first, as the real DEFAULT_AGENT_LOGS list is — output
        # must still come back sorted by time, not by file order.
        boundaries = rt.parse_agent_log_boundaries([newer, older])
        assert [h for _, h in boundaries] == ["1a2b3c4d", "5e6f7a8b"]

    def test_duplicate_lines_across_files_are_deduped(self, tmp_path):
        a = tmp_path / "agent.log"
        b = tmp_path / "agent.log.1"
        line = "2026-01-01 00:00:00,000 INFO x hermes-kame-api-rotation: build deadbeef complete\n"
        a.write_text(line, encoding="utf-8")
        b.write_text(line, encoding="utf-8")
        boundaries = rt.parse_agent_log_boundaries([a, b])
        assert len(boundaries) == 1

    def test_missing_files_are_skipped_without_error(self, tmp_path):
        assert rt.parse_agent_log_boundaries([tmp_path / "does-not-exist.log"]) == []

    def test_close_registrations_flagged_regardless_of_matching_hash(self):
        boundaries = [(1000.0, "a"), (1030.0, "a"), (2000.0, "b")]
        close = rt.find_close_registrations(boundaries, threshold=60.0)
        assert len(close) == 1
        assert close[0]["gap_s"] == pytest.approx(30.0)
        assert close[0]["hash_a"] == "a" and close[0]["hash_b"] == "a"

    def test_registrations_far_apart_are_not_flagged(self):
        boundaries = [(1000.0, "a"), (2000.0, "b")]
        assert rt.find_close_registrations(boundaries, threshold=60.0) == []

    def test_hash_windows_span_to_the_next_registration_of_any_hash(self):
        boundaries = [(1000.0, "a"), (1500.0, "a"), (2000.0, "b"), (3000.0, "a")]
        windows = rt.compute_hash_windows(boundaries, "a")
        assert windows == [(1000.0, 1500.0), (1500.0, 2000.0), (3000.0, math.inf)]

    def test_hash_windows_for_an_unseen_hash_is_empty(self):
        boundaries = [(1000.0, "a")]
        assert rt.compute_hash_windows(boundaries, "never-seen") == []

    def test_in_windows_is_half_open(self):
        windows = [(100.0, 200.0)]
        assert rt._in_windows(100.0, windows) is True
        assert rt._in_windows(199.999, windows) is True
        assert rt._in_windows(200.0, windows) is False
        assert rt._in_windows(99.0, windows) is False


# ---------------------------------------------------------------------------
# Process-boundary resets change what the carousel remembers
# ---------------------------------------------------------------------------

#: The exact recorded-PerDay body shape from tools/live_daily.py, proven
#: there to classify as kind="daily" with a small, provider-stated delay via
#: the evidence-first classifier (core.classify) — which is what puts this
#: scenario on the ``_no_answer_since`` / "is the pool still alive" branch in
#: core/carousel.py's ``_escalate``, the exact mechanism the coordinator
#: identified as the source of the 1.7.0.5 mismatches.
_DAILY_BODY = {
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and billing "
            "details. For more information on this error, head to: "
            "https://ai.google.dev/gemini-api/docs/rate-limits. \n* Quota exceeded "
            "for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 20, model: "
            "gemini-3.8-flash\nPlease retry in 5.0s."
        ),
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{
                    "quotaMetric": (
                        "generativelanguage.googleapis.com/"
                        "generate_content_free_tier_requests"
                    ),
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "quotaDimensions": {"location": "global", "model": "gemini-3.8-flash"},
                    "quotaValue": "20",
                }],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5s"},
        ],
    }
}


#: A stand-in "now" comfortably above MIN_AT (1.75e9) so these rows survive
#: load_calls'/load_refusals' timestamp filter, like every other synthetic
#: fixture in this file.
_BASE_AT = 1_800_000_000.0


def _daily_refusal_row(at: float) -> dict:
    body_text = json.dumps(_DAILY_BODY)
    return {
        "at": at, "provider": "gemini", "model": "gemini:gemini-3.8-flash",
        "status": 429, "type": "GeminiAPIError", "code": "",
        "message": "Gemini HTTP 429 (RESOURCE_EXHAUSTED): " + _DAILY_BODY["error"]["message"],
        "body": body_text, "response": "",
    }


def _daily_call_row(at: float) -> dict:
    return {
        "at": at, "identity": "gemini:gemini-3.8-flash", "key": "key:aaa",
        "attempt": 1, "outcome": "rotate", "kind": "daily", "status": 429, "rest_s": None,
    }


class TestProcessBoundaryReset:
    """Reproduces the exact bug: two "daily" refusals 1300s apart (past
    core.carousel.POOL_SILENCE_BEFORE_THE_DAY_S = 1200s). A continuous engine
    remembers the first refusal's ``_no_answer_since`` and concludes on the
    second one that "the pool has been silent, believe the label" -> the full
    hour (3600s, ``daily_cooldown_s``). A restart in between forgets that
    memory, so the second refusal is judged fresh -> the flat re-probe
    (min(RL_BACKOFF_CAP_S, daily_cooldown_s) = 300s) — which is what
    production's own calls.jsonl actually shows for this shape of mismatch.
    """

    def _run(self, tmp_path, label, boundaries):
        calls_path = tmp_path / f"calls_{label}.jsonl"
        refusals_path = tmp_path / f"refusals_{label}.jsonl"
        out_path = tmp_path / f"out_{label}.json"
        calls = [_daily_call_row(_BASE_AT + 1000.0), _daily_call_row(_BASE_AT + 2300.0)]
        refusals = [_daily_refusal_row(_BASE_AT + 1000.1), _daily_refusal_row(_BASE_AT + 2300.1)]
        calls_path.write_text("\n".join(json.dumps(r) for r in calls) + "\n", encoding="utf-8")
        refusals_path.write_text("\n".join(json.dumps(r) for r in refusals) + "\n", encoding="utf-8")
        metrics = rt.run_replay(PLUGIN_DIR, calls_path, refusals_path, out_path, boundaries=boundaries)
        decisions = rt._read_jsonl(Path(metrics["decisions_file"]))
        return metrics, decisions

    def test_without_a_restart_the_second_daily_refusal_escalates_on_stale_memory(self, tmp_path):
        metrics, decisions = self._run(tmp_path, "continuous", boundaries=[])
        assert len(decisions) == 2
        assert [d["kind_returned"] for d in decisions] == ["daily", "daily"]
        assert decisions[0]["hold_s"] == pytest.approx(300.0), "first label on a fresh pool: flat re-probe"
        assert decisions[1]["hold_s"] == pytest.approx(3600.0), (
            "stale _no_answer_since makes the second label look like a dead pool -> the full hour"
        )
        assert decisions[0]["segment"] == 0
        assert decisions[1]["segment"] == 0
        assert metrics["process_boundaries"]["applied_during_replay"] == 0
        assert metrics["process_boundaries"]["segments"] == 1

    def test_a_restart_between_the_two_refusals_resets_the_carousel(self, tmp_path):
        metrics, decisions = self._run(
            tmp_path, "restarted", boundaries=[(_BASE_AT + 1500.0, "test-build")]
        )
        assert len(decisions) == 2
        assert [d["kind_returned"] for d in decisions] == ["daily", "daily"]
        assert decisions[0]["hold_s"] == pytest.approx(300.0)
        assert decisions[1]["hold_s"] == pytest.approx(300.0), (
            "a fresh process forgot the first refusal -> re-probe again, not the hour"
        )
        assert decisions[0]["segment"] == 0
        assert decisions[1]["segment"] == 1
        assert metrics["process_boundaries"]["applied_during_replay"] == 1
        assert metrics["process_boundaries"]["segments"] == 2

    def test_a_restart_before_the_first_call_still_counts_but_changes_nothing_here(self, tmp_path):
        # A boundary before either call starts a fresh segment 1 before row 1
        # is even processed — same behaviour as segment 0 would have been,
        # since nothing preceded it either way.
        metrics, decisions = self._run(
            tmp_path, "early-boundary", boundaries=[(_BASE_AT + 500.0, "test-build")]
        )
        assert decisions[0]["hold_s"] == pytest.approx(300.0)
        assert decisions[0]["segment"] == 1
        assert metrics["process_boundaries"]["applied_during_replay"] == 1


# ---------------------------------------------------------------------------
# Null holds for verdicts that never called mark()
# ---------------------------------------------------------------------------


class TestNullHoldsForNonMarkingVerdicts:
    def test_a_terminal_failure_records_a_null_hold_and_is_excluded_from_stats(self, tmp_path):
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        out_path = tmp_path / "out.json"
        calls_path.write_text(
            json.dumps(
                {
                    "at": 1_800_000_000.0, "identity": "some:model", "key": "key:aaa",
                    "attempt": 1, "outcome": "raise", "kind": "other", "status": 404, "rest_s": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        refusals_path.write_text(
            json.dumps(
                {
                    "at": 1_800_000_000.1, "provider": "some", "model": "some:model",
                    "status": 404, "type": "NotFoundError", "code": "",
                    "message": "Error: model not found", "body": "", "response": "",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        metrics = rt.run_replay(PLUGIN_DIR, calls_path, refusals_path, out_path, boundaries=[])
        decisions = rt._read_jsonl(Path(metrics["decisions_file"]))

        assert len(decisions) == 1
        assert decisions[0]["verdict"] == "raise"
        assert decisions[0]["hold_s"] is None
        assert decisions[0]["sick_until_delta"] == 0.0
        assert metrics["holds"]["count"] == 0
        assert metrics["counts"]["marked_failures"] == 0
        assert metrics["counts"]["non_marking_failures"] == 1


# ---------------------------------------------------------------------------
# The contaminated-window filter (test doubles inside the "clean" logs)
# ---------------------------------------------------------------------------


class TestContaminatedWindowFilter:
    def test_rows_inside_the_window_are_excluded_from_calls_and_refusals(self, tmp_path):
        inside = rt.CONTAMINATED_WINDOW_START + 5.0
        outside = rt.CONTAMINATED_WINDOW_END + 5.0
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        calls_path.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"at": inside, "identity": "some-gateway:x", "key": "key:a", "outcome": "answered"},
                    {"at": outside, "identity": "gemini:model", "key": "key:b", "outcome": "answered"},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        refusals_path.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {"at": inside, "provider": "a-provider-from-2029", "model": "", "status": 503, "type": "Boom", "code": "", "message": "503 Service Unavailable", "body": "", "response": ""},
                    {"at": outside, "provider": "gemini", "model": "gemini:model", "status": 429, "type": "GeminiAPIError", "code": "", "message": "429", "body": "", "response": ""},
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        calls, calls_report = rt.load_calls(calls_path)
        refusals, refusals_report = rt.load_refusals(refusals_path)

        assert len(calls) == 1 and calls[0]["at"] == outside
        assert len(refusals) == 1 and refusals[0]["at"] == outside
        assert calls_report["excluded_contaminated_window"] == 1
        assert refusals_report["excluded_contaminated_window"] == 1

    def test_boundary_endpoints_are_inclusive(self, tmp_path):
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        calls_path.write_text(
            "\n".join(
                json.dumps({"at": at, "identity": "x:y", "key": "key:a", "outcome": "answered"})
                for at in (rt.CONTAMINATED_WINDOW_START, rt.CONTAMINATED_WINDOW_END)
            )
            + "\n",
            encoding="utf-8",
        )
        refusals_path.write_text("", encoding="utf-8")
        calls, report = rt.load_calls(calls_path)
        assert calls == []
        assert report["excluded_contaminated_window"] == 2

    def test_the_real_window_matches_the_measured_counts(self):
        # Pins the window itself against the exact figures the coordinator
        # reported and this session independently re-verified against the
        # real files: 376 of 1,763 refusals fall in the window, and 348 of
        # 2,094 calls. load_refusals applies the some-gateway filter first,
        # and all 4 some-gateway rows happen to sit inside this same window
        # — so of the 376, 4 are already gone by the time the window count
        # is taken, leaving 372 counted here plus the 4 counted separately.
        calls_path = rt.DEFAULT_CALLS
        refusals_path = rt.DEFAULT_REFUSALS
        if not calls_path.is_file() or not refusals_path.is_file():
            pytest.skip("real installed evidence not present on this machine")
        _, calls_report = rt.load_calls(calls_path)
        _, refusals_report = rt.load_refusals(refusals_path)
        assert calls_report["excluded_contaminated_window"] == 348
        assert refusals_report["excluded_some_gateway"] == 4
        assert refusals_report["excluded_contaminated_window"] == 372
        assert refusals_report["excluded_some_gateway"] + refusals_report["excluded_contaminated_window"] == 376


# ---------------------------------------------------------------------------
# The host-guidance-block patch (removing the "...billing..." false trigger)
# ---------------------------------------------------------------------------


#: The phrase this whole fix is about: it lives only in the Hermes-appended
#: guidance paragraph (never in Google's own message, which separately and
#: legitimately says "...check your plan and billing details" — that phrase
#: is NOT what needs stripping, and neither test below claims otherwise).
_HERMES_APPENDED_PHRASE = "billing-enabled project"


class TestGuidanceBlockPatch:
    def test_full_free_tier_paragraph_is_stripped_so_it_does_not_read_as_billing(self):
        plugin = rt.load_plugin(PLUGIN_DIR, package_name="kame_replay_guidance_patch_test")
        rt.patch_guidance_blocks("kame_replay_guidance_patch_test")
        evidence_mod = importlib.import_module("kame_replay_guidance_patch_test.core.evidence")
        host_text_mod = importlib.import_module("kame_replay_guidance_patch_test.host_text")

        raw_message = (
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current quota, "
            "please check your plan and billing details.\nPlease retry in 30.2s.\n\n"
            + rt._KNOWN_HOST_GUIDANCE_BLOCKS[0]
        )
        assert _HERMES_APPENDED_PHRASE in raw_message
        ev = evidence_mod.harvest(
            Exception(raw_message), message=raw_message, guidance_blocks=host_text_mod.guidance_blocks(),
        )
        assert _HERMES_APPENDED_PHRASE not in ev.message
        # Google's own sentence (never Hermes' to strip) survives untouched.
        assert "check your plan and billing details" in ev.message.lower()
        assert "exceeded your current quota" in ev.message.lower()

    def test_without_the_patch_the_fallback_still_strips_the_whole_paragraph(self):
        # This test used to characterise the bug the patch works around:
        # host_text.py's fallback (the opening clause only) was too short for
        # evidence.strip_trailing_blocks's exact-substring removal to take the
        # whole paragraph off the end, so the phrase that trips
        # core.classify's strict billing pattern (`billing...enabled`)
        # survived. 1.8.1.4 fixed the plugin itself: the strip now cuts from
        # the block's start, so the opening clause is enough and the patch is
        # no longer needed (it stays, harmless, for older plugin dirs).
        plugin = rt.load_plugin(PLUGIN_DIR, package_name="kame_replay_guidance_unpatched_test")
        evidence_mod = importlib.import_module("kame_replay_guidance_unpatched_test.core.evidence")
        host_text_mod = importlib.import_module("kame_replay_guidance_unpatched_test.host_text")

        raw_message = (
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current quota, "
            "please check your plan and billing details.\nPlease retry in 30.2s.\n\n"
            + rt._KNOWN_HOST_GUIDANCE_BLOCKS[0]
        )
        ev = evidence_mod.harvest(
            Exception(raw_message), message=raw_message, guidance_blocks=host_text_mod.guidance_blocks(),
        )
        assert _HERMES_APPENDED_PHRASE not in ev.message
        # Google's own sentence (never Hermes' to strip) survives untouched.
        assert "check your plan and billing details" in ev.message.lower()
        assert "please retry in 30.2s" in ev.message.lower()


# ---------------------------------------------------------------------------
# Fidelity gate: rest_s comparison semantics
# ---------------------------------------------------------------------------


class TestFidelityRestComparison:
    def test_none_and_zero_are_the_same_value(self):
        # calls.jsonl's rest_s is next_recovery_seconds(), which returns None
        # specifically to mean "already available" — the same fact hold_s=0.0
        # represents. They must agree, not be flagged as a mismatch.
        assert rt._rest_close(None, 0.0, tolerance=1.0) is True
        assert rt._rest_close(0.0, None, tolerance=1.0) is True
        assert rt._rest_close(None, None, tolerance=1.0) is True

    def test_none_versus_a_real_hold_is_not_close(self):
        assert rt._rest_close(None, 300.0, tolerance=1.0) is False

    def test_within_tolerance_agrees(self):
        assert rt._rest_close(300.0, 300.4, tolerance=1.0) is True
        assert rt._rest_close(300.0, 302.0, tolerance=1.0) is False


# ---------------------------------------------------------------------------
# over_ceiling cause classification (pure)
# ---------------------------------------------------------------------------


class TestOverCeilingClassification:
    def test_retained_wins_over_everything(self):
        assert rt.classify_over_ceiling_cause(
            retained=True, calendar_reset=True, stated=True, escalation_produced=True,
        ) == "retained_prior_hold"

    def test_calendar_reset_before_stated_or_escalation(self):
        assert rt.classify_over_ceiling_cause(
            retained=False, calendar_reset=True, stated=True, escalation_produced=True,
        ) == "calendar_reset"

    def test_stated_before_escalation(self):
        assert rt.classify_over_ceiling_cause(
            retained=False, calendar_reset=False, stated=True, escalation_produced=True,
        ) == "stated_deadline"

    def test_escalation_when_nothing_else_applies(self):
        assert rt.classify_over_ceiling_cause(
            retained=False, calendar_reset=False, stated=False, escalation_produced=True,
        ) == "escalation"

    def test_default_when_nothing_applies(self):
        assert rt.classify_over_ceiling_cause(
            retained=False, calendar_reset=False, stated=False, escalation_produced=False,
        ) == "default"


# ---------------------------------------------------------------------------
# Escalation observation: core.journal.short_streak + core.escalate.stretch
# ---------------------------------------------------------------------------


class TestEscalationMechanismDirect:
    """Exercises the real plugin's own journal/escalate modules directly —
    no replay machinery — to pin down what the mechanism does with two
    consecutive same-window, same-reason, on-time refusals: it widens,
    matching escalate.py's own ``factor_for(2) == 2.0``. True whether the
    source is a provider-timed one (``header``, ``retryinfo``, ...) or not.

    1.7.0.7 added a blanket ``if provider_timed(source): ...`` guard to both
    ``short_streak`` and ``stretch`` that zeroed/blocked exactly the
    provider-timed case. Decision 0004 D3 (measured on the owner's corpus:
    the guard blocked 31 of ~530 widenings, all of them the case R13
    authorizes — a stated deadline served in full, refused again) removed
    it in 1.8.0.0. See ``tests/test_v1_8_0_0_evidence.py`` for the fuller
    coverage of that removal and of the two narrower chain breaks that stay.
    """

    def _load(self, package_name: str):
        rt.load_plugin(PLUGIN_DIR, package_name=package_name)
        journal_mod = importlib.import_module(f"{package_name}.core.journal")
        escalate_mod = importlib.import_module(f"{package_name}.core.escalate")
        vocabulary_mod = importlib.import_module(f"{package_name}.core.vocabulary")
        return journal_mod, escalate_mod, vocabulary_mod

    def test_two_on_time_same_window_refusals_produce_a_widening(self):
        journal_mod, escalate_mod, vocabulary_mod = self._load("kame_escalation_fires_test")
        book = journal_mod.Journal()
        # Two prior blocks, same (credential, model, window, reason), each
        # landing right after the previous one's own deadline lapsed.
        book.record_block(
            at=1000.0, provider="gemini", model="gemini:model", credential_id="key:a",
            status_code=429, window="per_minute", source="table", reset_at=1020.0,
            sized_by=journal_mod.SIZED_BY_KAME, reason="rate_limit",
        )
        book.record_block(
            at=1025.0, provider="gemini", model="gemini:model", credential_id="key:a",
            status_code=429, window="per_minute", source="table", reset_at=1045.0,
            sized_by=journal_mod.SIZED_BY_KAME, reason="rate_limit",
        )
        # The refusal "in flight" — the third in the run.
        strikes = journal_mod.short_streak(
            book, credential_id="key:a", model="gemini:model", window="per_minute",
            at=1046.0, source="table", reason="rate_limit",
        )
        assert strikes == 2
        assert vocabulary_mod.provider_timed("table") is False

        stretched = escalate_mod.stretch(
            reset_at=1066.0, now=1046.0, strikes=strikes, window="per_minute",
            reason="rate_limit", source="table",
        )
        assert stretched is not None
        # factor_for(2) == 2.0, capped at MAX_PER_MINUTE_HOLD_SECONDS (300s) —
        # 20s * 2.0 = 40s, well under the cap.
        assert stretched - 1046.0 == pytest.approx(40.0)

    def test_provider_timed_source_no_longer_zeroes_the_streak_or_blocks_stretch(self):
        """Old name/behaviour (pre-1.8.0.0):
        ``test_provider_timed_source_zeroes_the_streak_and_blocks_stretch`` —
        asserted ``strikes == 0`` and ``stretched is None`` for this exact
        sequence, because 1.7.0.7's blanket guard fired on the provider-timed
        ``source``. Decision 0004 D3 removed that guard: the same sequence
        that already widens under ``source="table"``
        (``test_two_on_time_same_window_refusals_produce_a_widening`` above)
        must now widen identically under ``source="header"`` — the source
        being fresh is not evidence against the two deadlines it labeled
        both having been served in full and refused again.
        """
        journal_mod, escalate_mod, vocabulary_mod = self._load("kame_escalation_guard_test")
        assert vocabulary_mod.provider_timed("header") is True
        assert vocabulary_mod.provider_timed("body.retryDelay") is True
        book = journal_mod.Journal()
        book.record_block(
            at=1000.0, provider="gemini", model="gemini:model", credential_id="key:a",
            status_code=429, window="per_minute", source="header", reset_at=1020.0,
            sized_by=journal_mod.SIZED_BY_KAME, reason="rate_limit",
        )
        book.record_block(
            at=1025.0, provider="gemini", model="gemini:model", credential_id="key:a",
            status_code=429, window="per_minute", source="header", reset_at=1045.0,
            sized_by=journal_mod.SIZED_BY_KAME, reason="rate_limit",
        )
        strikes = journal_mod.short_streak(
            book, credential_id="key:a", model="gemini:model", window="per_minute",
            at=1046.0, source="header", reason="rate_limit",
        )
        assert strikes == 2, "two deadlines served in full and refused again is a strike"

        stretched = escalate_mod.stretch(
            reset_at=1066.0, now=1046.0, strikes=strikes, window="per_minute",
            reason="rate_limit", source="header",
        )
        assert stretched is not None
        assert stretched - 1046.0 == pytest.approx(40.0)  # factor_for(2) == 2.0, 20s * 2

    def test_a_success_in_the_gap_breaks_the_streak(self):
        journal_mod, escalate_mod, vocabulary_mod = self._load("kame_escalation_worked_between_test")
        book = journal_mod.Journal()
        book.record_block(
            at=1000.0, provider="gemini", model="gemini:model", credential_id="key:a",
            status_code=429, window="per_minute", source="table", reset_at=1020.0,
            sized_by=journal_mod.SIZED_BY_KAME, reason="rate_limit",
        )
        # The key answered during the stretch it was supposed to be resting.
        book.record_success(at=1021.0, provider="gemini", model="gemini:model", credential_id="key:a")
        strikes = journal_mod.short_streak(
            book, credential_id="key:a", model="gemini:model", window="per_minute",
            at=1030.0, source="table", reason="rate_limit",
        )
        assert strikes == 0


# ---------------------------------------------------------------------------
# Escalation + over_ceiling wired through run_replay end to end
# ---------------------------------------------------------------------------


#: A real, stated, long (3.5h) deadline — verbatim shape from the owner's own
#: corpus (refusals.jsonl, openai-codex, `at=1788896543.872`): a structured
#: `resets_in_seconds` field the classifier reads directly, kept whole
#: rather than clamped. Reused here (with `resets_in_seconds` recomputed
#: relative to the test's own `at`) rather than guessing at a shape from
#: scratch, so this test exercises a real, already-observed code path.
def _usage_limit_reached_refusal_row(at: float, resets_in_seconds: float = 12660.0) -> dict:
    body = {
        "type": "usage_limit_reached", "message": "The usage limit has been reached",
        "plan_type": "plus", "resets_at": int(at + resets_in_seconds),
        "eligible_promo": None, "resets_in_seconds": int(resets_in_seconds),
    }
    message = (
        "Error code: 429 - {'error': {'type': 'usage_limit_reached', 'message': "
        "'The usage limit has been reached', 'plan_type': 'plus', 'resets_at': "
        f"{int(at + resets_in_seconds)}, 'eligible_promo': None, "
        f"'resets_in_seconds': {int(resets_in_seconds)}}}"
    )
    return {
        "at": at, "provider": "openai-codex", "model": "openai-codex:gpt-5.6-luna",
        "status": 429, "type": "RateLimitError", "code": "",
        "message": message, "body": json.dumps(body), "response": "",
    }


def _usage_limit_reached_call_row(at: float) -> dict:
    return {
        "at": at, "identity": "openai-codex:gpt-5.6-luna", "key": "key:bbb",
        "attempt": 1, "outcome": "rotate", "kind": "rate_limit", "status": 429, "rest_s": None,
    }


class TestEscalationAndOverCeilingWiredThroughRunReplay:
    def test_escalation_section_is_populated_and_well_formed(self, tmp_path):
        # Three consecutive daily refusals, each landing ~10s after the
        # PREVIOUS one's own 300s deadline lapsed — the exact sequence
        # short_streak's `_landed_right_after` requires to count a strike
        # (arriving mid-bench, as ten seconds apart would, breaks the chain
        # instead of building it). Reuses the proven daily-quota body from
        # TestProcessBoundaryReset so the classification path is known.
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        out_path = tmp_path / "out.json"
        t0 = _BASE_AT + 1000.0
        t1 = t0 + 300.0 + 10.0   # 10s after refusal 1's 300s reprobe deadline
        t2 = t1 + 300.0 + 10.0   # 10s after refusal 2's own deadline
        ats = [t0, t1, t2]
        calls = [_daily_call_row(a) for a in ats]
        refusals = [_daily_refusal_row(a + 0.1) for a in ats]
        calls_path.write_text("\n".join(json.dumps(r) for r in calls) + "\n", encoding="utf-8")
        refusals_path.write_text("\n".join(json.dumps(r) for r in refusals) + "\n", encoding="utf-8")

        metrics = rt.run_replay(PLUGIN_DIR, calls_path, refusals_path, out_path, boundaries=[])

        esc = metrics["escalation"]
        assert esc["checked"] == 3
        assert set(esc["by_identity"].keys()) == {"gemini:gemini-3.8-flash"}
        bucket = esc["by_identity"]["gemini:gemini-3.8-flash"]
        assert bucket["checked"] == 3
        # Only the third refusal (two proven-short deadlines behind it)
        # reaches the 2-strike threshold.
        assert esc["strikes_ge_2"] == 1
        assert esc["produced"] <= esc["strikes_ge_2"]
        assert esc["blocked_by_provider_timed_guard"] == 0

        oc = metrics["over_ceiling"]
        assert oc["threshold_s"] == rt.OVER_CEILING_THRESHOLD_S
        assert oc["count"] == len(oc["events"])
        for event in oc["events"]:
            assert event["hold_s"] > rt.OVER_CEILING_THRESHOLD_S
            assert event["cause"] in (
                "retained_prior_hold", "calendar_reset", "stated_deadline", "escalation", "default",
            )

    def test_a_real_stated_long_deadline_is_now_capped_at_the_owners_ceiling(self, tmp_path):
        # 1.8.0.0 (PLAN_1.8.0.0.md G8): this is `research/1.8.0.0/gate/1707.json`'s
        # `real-17` in miniature — the exact shape (a real Codex
        # `usage_limit_reached` 429 whose `resets_in_seconds` sizes a hold near
        # 12,660s) this release's ceiling exists to fix. Before the fix,
        # `carousel_mod.Carousel()` (this replay's engine, constructed with no
        # override) applied that number whole and this test pinned it, under
        # `over_ceiling`, at OVER_CEILING_THRESHOLD_S = 3600.0. After the fix
        # the engine's own default `max_hold_s` is that same 3600.0, so the
        # hold this scenario now produces is capped there too — no longer
        # *strictly greater* than the threshold, so it no longer appears under
        # `over_ceiling` at all. That is the tool correctly reporting that the
        # bug it was built to catch is gone; `metrics["holds"]["max"]` is
        # where the actual, now-bounded number still shows up.
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        out_path = tmp_path / "out.json"
        at = _BASE_AT + 2000.0
        calls_path.write_text(json.dumps(_usage_limit_reached_call_row(at)) + "\n", encoding="utf-8")
        refusals_path.write_text(
            json.dumps(_usage_limit_reached_refusal_row(at + 0.1)) + "\n", encoding="utf-8"
        )

        metrics = rt.run_replay(PLUGIN_DIR, calls_path, refusals_path, out_path, boundaries=[])

        oc = metrics["over_ceiling"]
        assert oc["count"] == 0
        assert oc["events"] == []
        assert metrics["holds"]["max"] == pytest.approx(3600.0)
        assert metrics["holds"]["over_3600s"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
