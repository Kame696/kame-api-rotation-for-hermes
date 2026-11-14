"""1.8.4.3 designer review (F1-F4): blame, rescue model, rescue-2 timing, learning across restarts."""
from __future__ import annotations

import json
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_4_3_quiet_guard import (  # noqa: F401  (fixture by name)
    SeqCall, answered, guard, think, timings_mod, transport, settings, chunk, IDENTITY, KEYS)


# F1 --------------------------------------------------------------------------------------------------

def test_a_provisional_that_errors_is_blamed_not_the_innocent_primary(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "30")      # no rescue: the provisional errors first
    settings.forget()
    call = SeqCall([[30.0, chunk("silent primary")],
                    [0.02, think("a"), 0.1, R.per_minute()],
                    [0.02, chunk("next key answers", finish="stop")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "next key answers"
    primary_key, backup_key = call.opened[0][1], call.opened[1][1]
    assert primary_key != backup_key
    assert guard.engine.healthy_count(IDENTITY, [backup_key]) == 0, "the key that failed is rested"
    assert guard.engine.healthy_count(IDENTITY, [primary_key]) == 1, "the cut, silent primary is not"


# F2 --------------------------------------------------------------------------------------------------

def test_rescue_one_asks_the_provisional_legs_model(guard, monkeypatch):
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    monkeypatch.setattr(transport, "_SLOW_S", {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    call = SeqCall(by_model={
        "gemini-race": [[30.0, chunk("silent home")], [0.02, chunk("WRONG model", finish="stop")]],
        "gemini-race-two": [[0.02, think("c"), 30.0], [0.02, chunk("right model", finish="stop")]],
    })
    out = list(guard.stream(call))
    assert R.shown(out) == "right model"
    assert call.models() == ["gemini-race", "gemini-race-two", "gemini-race-two"]


# F3 --------------------------------------------------------------------------------------------------

def test_rescue_two_is_due_at_once_when_rescue_one_could_not_open(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0.6")
    settings.forget()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "may_start_backup", lambda *a, **k: False)
    call = SeqCall(by_model={
        "gemini-race": [[0.02, think("a"), 30.0]],
        "gemini-race-two": [[0.02, chunk("next model", finish="stop")]],
    })
    started = time.monotonic()
    out = list(guard.stream(call))
    assert R.shown(out) == "next model"
    assert time.monotonic() - started < 1.4, "at the first bound, not at twice the bound after it"
    assert call.models() == ["gemini-race", "gemini-race-two"]


def test_rescue_two_keeps_twice_the_bound_after_a_rescue_one_that_opened(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0.4")
    settings.forget()
    call = SeqCall(by_model={
        "gemini-race": [[0.02, think("a"), 30.0], [0.02, think("b"), 30.0]],
        "gemini-race-two": [[0.02, chunk("next model", finish="stop")]],
    })
    started = time.monotonic()
    out = list(guard.stream(call))
    assert R.shown(out) == "next model"
    assert time.monotonic() - started >= 0.75
    assert call.models() == ["gemini-race", "gemini-race", "gemini-race-two"]


# F4 --------------------------------------------------------------------------------------------------

def test_the_quiet_bound_survives_a_restart(guard, monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    now = time.time()
    rows = [{"identity": IDENTITY, "outcome": "answered", "ms_longest_quiet": 2000, "at": now - 60}
            for _ in range(10)]
    rows.append({"identity": IDENTITY, "outcome": "answered", "ms_longest_quiet": None, "at": now})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    monkeypatch.setattr(timings_mod, "_destination", lambda: str(path))
    assert R.hedging_mod.HEDGE_TRACKER.quiet_bound(IDENTITY, False) == 20.0
    guard._seed_race_history(IDENTITY, False)
    assert R.hedging_mod.HEDGE_TRACKER.quiet_bound(IDENTITY, False) == 8.0
    R.hedging_mod.HEDGE_TRACKER.record_quiet(IDENTITY, 0.1)
    guard._race_seeded.discard((IDENTITY, False))
    guard._seed_race_history(IDENTITY, False)       # live samples win: seeding never adds twice
    assert len(R.hedging_mod.HEDGE_TRACKER._quiet[IDENTITY]) == 11
