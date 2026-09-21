"""Proofs for tools/continuity_gate.py — the G4 gate itself.

`tests/test_v1_8_0_0_shared_health.py` already proves the file format and
the reconciliation rule with threads standing in for processes; its own
header says a genuine multi-process run was never executed. This file does
not re-derive that proof — it proves the GATE that runs the real thing:

1. The pure helpers (`_count_double_burns`, `_validate_pool_health_json`,
   `_appdata_snapshot`/`_assert_appdata_untouched`, `_new_shared_root`) on
   hand-made data, fast and deterministic.
2. The worker CLI (`--worker <spec.json>`) as a real subprocess, on a tiny
   scripted case, so a change to the spec format or the worker dispatch
   breaks a test here before it breaks a 30-second full run.
3. `learnings/0003-the-gate-that-was-never-run.md`'s own rule, proven
   directly: `scenario_self_check` really does drive real subprocesses with
   sharing deliberately off, and really does come back reporting FAIL for
   that broken configuration — a gate that cannot fail is not a gate.
4. One full end-to-end run of `run_gate()`, the same thing `python -B
   tools/continuity_gate.py` runs, with the same narrow AppData check
   `tests/test_replay_timeline.py::TestSubprocessIsolation` already
   established as the right scope (the whole-tree version flags the
   owner's OWN separately-running Hermes cron ticker and kanban DB, which
   this gate cannot reach and must not report against).

Slow by the standard of this suite (the end-to-end test alone is real
subprocess trees, ~20-40s) — that is the whole point: this is what proves
the thing that only threads proved before. Run just this file:

    python -B -m pytest -q -p no:cacheprovider tests/test_continuity_gate.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
TOOL_PATH = ROOT / "tools" / "continuity_gate.py"
OUT_DIR = ROOT / "research" / "1.8.0.0" / "continuity"


def _load_tool():
    spec = importlib.util.spec_from_file_location("continuity_gate_under_test", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_tool()


def _snapshot_real_pool_health() -> dict:
    """Same convention as `test_replay_timeline._snapshot_real_evidence`:
    only the artifact this module could possibly reach in the real install,
    never the whole AppData tree (the owner's own live Hermes republishes
    unrelated files, like `state.json` and the cron ticker, on its own
    clock — that is not this gate's business, see `gate._appdata_snapshot`'s
    own docstring for the same reasoning).
    """
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "plugin-data" / "hermes-kame-api-rotation"
    out = {}
    for name in ("pool-health.json", "pool-health.json.lock"):
        candidate = base / name
        if candidate.is_file():
            stat = candidate.stat()
            out[name] = (stat.st_size, stat.st_mtime)
    return out


# ---------------------------------------------------------------------------
# 1. pure helpers
# ---------------------------------------------------------------------------


class TestCountDoubleBurns:
    def test_same_profile_repeated_refusal_is_not_a_double_burn(self, gate):
        events = [
            {"t": 100.0, "key": "k1", "refused": True, "profile": "base"},
            {"t": 100.5, "key": "k1", "refused": True, "profile": "base"},
        ]
        assert gate._count_double_burns(events, hold_s=2.0) == 0

    def test_a_different_profile_inside_the_hold_is_a_double_burn(self, gate):
        events = [
            {"t": 100.0, "key": "k1", "refused": True, "profile": "base"},
            {"t": 100.5, "key": "k1", "refused": True, "profile": "k"},
        ]
        assert gate._count_double_burns(events, hold_s=2.0) == 1

    def test_a_different_profile_after_the_hold_expires_is_not_a_double_burn(self, gate):
        events = [
            {"t": 100.0, "key": "k1", "refused": True, "profile": "base"},
            {"t": 102.5, "key": "k1", "refused": True, "profile": "k"},
        ]
        assert gate._count_double_burns(events, hold_s=2.0) == 0

    def test_a_success_event_is_never_counted_as_a_burn(self, gate):
        events = [
            {"t": 100.0, "key": "k1", "refused": True, "profile": "base"},
            {"t": 100.2, "key": "k1", "refused": False, "profile": "k"},
        ]
        assert gate._count_double_burns(events, hold_s=2.0) == 0

    def test_out_of_order_input_is_sorted_before_counting(self, gate):
        events = [
            {"t": 100.5, "key": "k1", "refused": True, "profile": "k"},
            {"t": 100.0, "key": "k1", "refused": True, "profile": "base"},
        ]
        assert gate._count_double_burns(events, hold_s=2.0) == 1

    def test_separate_keys_never_interact(self, gate):
        events = [
            {"t": 100.0, "key": "k1", "refused": True, "profile": "base"},
            {"t": 100.1, "key": "k2", "refused": True, "profile": "k"},
        ]
        assert gate._count_double_burns(events, hold_s=2.0) == 0


class TestValidatePoolHealthJson:
    def test_a_missing_file_is_ok_nothing_shared_yet(self, gate, tmp_path):
        ok, detail = gate._validate_pool_health_json(tmp_path / "pool-health.json")
        assert ok is True
        assert "no file yet" in detail

    def test_a_valid_document_is_ok(self, gate, tmp_path):
        path = tmp_path / "pool-health.json"
        path.write_text(json.dumps({"schema": 1, "model": {}, "account": {}}), encoding="utf-8")
        ok, _detail = gate._validate_pool_health_json(path)
        assert ok is True

    def test_corrupt_json_is_reported_not_raised(self, gate, tmp_path):
        path = tmp_path / "pool-health.json"
        path.write_text("{not json", encoding="utf-8")
        ok, detail = gate._validate_pool_health_json(path)
        assert ok is False
        assert "corrupt" in detail.lower()

    def test_wrong_shape_is_reported(self, gate, tmp_path):
        path = tmp_path / "pool-health.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        ok, _detail = gate._validate_pool_health_json(path)
        assert ok is False


class TestAppDataSnapshot:
    def test_untouched_snapshot_matches_itself(self, gate, tmp_path, monkeypatch):
        monkeypatch.setattr(gate, "REAL_HERMES_APPDATA", tmp_path / "nonexistent-hermes")
        before = gate._appdata_snapshot()
        ok, _detail = gate._assert_appdata_untouched(before)
        assert ok is True

    def test_a_write_to_the_watched_file_is_detected(self, gate, tmp_path, monkeypatch):
        fake_home = tmp_path / "fake-hermes"
        monkeypatch.setattr(gate, "REAL_HERMES_APPDATA", fake_home)
        pool_dir = fake_home / "plugin-data" / "hermes-kame-api-rotation"
        pool_dir.mkdir(parents=True)
        pool_file = pool_dir / "pool-health.json"
        pool_file.write_text("{}", encoding="utf-8")
        before = gate._appdata_snapshot()
        time.sleep(0.01)
        pool_file.write_text('{"changed": true}', encoding="utf-8")
        ok, detail = gate._assert_appdata_untouched(before)
        assert ok is False
        assert "pool-health.json" in detail

    def test_unrelated_files_in_the_same_install_are_not_watched(self, gate, tmp_path, monkeypatch):
        """The whole-tree version of this check false-positived on the
        owner's live Hermes cron ticker and `state.json` — proving the
        narrow scope explicitly so nobody widens it back by "obvious"
        refactor. See `gate._appdata_snapshot`'s own docstring.
        """
        fake_home = tmp_path / "fake-hermes"
        (fake_home / "plugin-data" / "hermes-kame-api-rotation").mkdir(parents=True)
        monkeypatch.setattr(gate, "REAL_HERMES_APPDATA", fake_home)
        before = gate._appdata_snapshot()
        unrelated = fake_home / "plugin-data" / "hermes-kame-api-rotation" / "state.json"
        unrelated.write_text('{"live": true}', encoding="utf-8")
        (fake_home / "cron").mkdir(parents=True)
        (fake_home / "cron" / "ticker_heartbeat").write_text("x", encoding="utf-8")
        ok, _detail = gate._assert_appdata_untouched(before)
        assert ok is True


class TestNewSharedRoot:
    def test_three_profiles_resolve_to_the_same_shared_file(self, gate, tmp_path, monkeypatch):
        homes = gate._new_shared_root(tmp_path)
        assert set(homes) == {"base", "k", "lo1"}
        assert homes["k"] == homes["base"] / "profiles" / "k"
        assert homes["lo1"] == homes["base"] / "profiles" / "lo1"

        _carousel_mod, shared_health_mod, _quota_mod = gate._modules()
        paths = set()
        for label, home in homes.items():
            monkeypatch.setenv("HERMES_HOME", str(home))
            paths.add(shared_health_mod.default_path())
        assert len(paths) == 1, f"all three profiles must derive one shared path, got {paths}"


# ---------------------------------------------------------------------------
# 2. the worker CLI, as a real subprocess
# ---------------------------------------------------------------------------


class TestWorkerSubprocess:
    def test_one_shot_refuse_writes_the_expected_result_and_the_shared_file(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        out_path = tmp_path / "result.json"
        spec_path = tmp_path / "spec.json"
        spec = {
            "mode": "one_shot", "home": str(home), "share_pool_health": True,
            "identity": "sim:test-model", "keys": ["only-key"], "hold_s": 3.0,
            "max_hold_s": 10.0, "daily_cooldown_s": 5.0, "action": "refuse",
            "out": str(out_path), "profile_label": "base",
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "-B", str(TOOL_PATH), "--worker", str(spec_path)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert out_path.is_file()
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["key"] == "only-key"
        assert payload["marked"] == "refuse"

        shared_file = home / "plugin-data" / "hermes-kame-api-rotation" / "pool-health.json"
        assert shared_file.is_file(), "a shared write must land under the given temp HERMES_HOME"
        document = json.loads(shared_file.read_text(encoding="utf-8"))
        assert document["schema"] == 1

    def test_an_unknown_worker_mode_exits_nonzero_instead_of_hanging(self, tmp_path):
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps({"mode": "not-a-real-mode"}), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", str(TOOL_PATH), "--worker", str(spec_path)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert "unknown worker mode" in result.stderr


# ---------------------------------------------------------------------------
# 3. the gate can fail — learnings/0003
# ---------------------------------------------------------------------------


class TestSelfCheckHasTeeth:
    def test_sharing_off_produces_real_cross_process_double_burns(self, gate, tmp_path):
        """Direct proof, not a mock: three real subprocesses, sharing
        deliberately off, and the storm really does show a different
        profile calling into a hold another profile just recorded. If this
        ever comes back zero, decisions/0006's whole premise (a bench in
        one profile is invisible to another without sharing) has stopped
        being true, or the scenario stopped exercising it, and either one is
        real news.
        """
        result = gate.scenario_self_check(tmp_path)
        assert result["passed"] is True, "the self-check itself must report the broken config as FAIL"
        assert result["measurements"]["double_burns_observed"] > 0
        assert result["measurements"]["gate_correctly_reported_fail"] is True


# ---------------------------------------------------------------------------
# 4. one full end-to-end run
# ---------------------------------------------------------------------------


class TestFullGateEndToEnd:
    def test_the_cli_runs_all_scenarios_and_exits_zero_on_pass(self):
        before = _snapshot_real_pool_health()

        env = dict(os.environ)
        env.pop("HERMES_HOME", None)  # prove the tool never inherits a real HERMES_HOME by accident
        result = subprocess.run(
            [sys.executable, "-B", str(TOOL_PATH)],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "GATE PASS" in result.stdout

        expected = {
            "three_processes", "double_burn", "stale_lock", "restart",
            "pool_sizes", "idle", "self_check",
        }
        for name in expected:
            path = OUT_DIR / f"{name}.json"
            assert path.is_file(), f"missing {path}"
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert "passed" in payload
            assert "measurements" in payload
        assert (OUT_DIR / "REPORT.md").is_file()

        after = _snapshot_real_pool_health()
        assert before == after, "the real install's shared pool-health file must be byte-for-byte untouched"

    def test_run_gate_returns_the_same_scenarios_in_process(self, gate):
        passed, results = gate.run_gate()
        names = {r["name"] for r in results}
        assert names == {
            "three_processes", "double_burn", "stale_lock", "restart",
            "pool_sizes", "idle", "self_check",
        }
        assert passed is True, json.dumps(
            {r["name"]: r["passed"] for r in results}, indent=2
        )
        # scenario 2's own strict numeric requirement (sharing's median
        # double-burn count must be measurably lower than not sharing's) is
        # asserted ONCE, inside `scenario_double_burn` itself — see that
        # function's own docstring for why a single storm's raw count is not
        # a stable enough measurement to assert on twice. Re-deriving the
        # same inequality here, on the same one-off sample, would only be a
        # second chance for machine noise to flip a real effect; it would
        # not make the check stronger. What IS worth asserting here,
        # independently of the gate's own verdict, is that the measurement
        # was actually taken at the sample size this suite expects, on both
        # sides, with the shape a reader of the JSON report can trust.
        double_burn = next(r for r in results if r["name"] == "double_burn")
        assert double_burn["measurements"]["repeats"] == gate.DOUBLE_BURN_REPEATS
        for side in ("off", "on"):
            stats = double_burn["measurements"][side]
            assert stats["n"] == gate.DOUBLE_BURN_REPEATS
            assert len(stats["samples"]) == gate.DOUBLE_BURN_REPEATS
            assert {"median", "mean", "min", "max", "stdev"} <= set(stats)
        assert double_burn["passed"] is True, json.dumps(double_burn["measurements"], indent=2, default=str)
        # scenario 6: zero, everywhere, every time.
        idle = next(r for r in results if r["name"] == "idle")
        assert idle["measurements"]["idle_violations_total"] == 0

        # RED_TEAM.md F11: scenario 1's single-storm double-burn count is a
        # diagnostic, never a gate metric -- the key name says so, and this
        # pins that it stays that way. `scenario_double_burn`'s medians
        # above are the only asserted, repeated measurement of this claim;
        # a name change back to something that reads as authoritative
        # (e.g. dropping "_single_run_diagnostic_only") without also wiring
        # it into `passed` would silently reopen F11.
        three_processes = next(r for r in results if r["name"] == "three_processes")
        measurements = three_processes["measurements"]
        assert "double_burns_with_sharing_on_single_run_diagnostic_only" in measurements
        assert "double_burns_with_sharing_on" not in measurements
