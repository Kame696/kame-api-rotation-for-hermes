"""1.8.4.3 second review (PLAN section 5.1): R1 first sign stamp, R2 rescue 2 after a failed rescue 1,
R3 cold bound 20 s, R4 same-scope judged against the provisional leg's key.
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_4_3_quiet_guard import (  # noqa: F401  (fixture by name)
    SeqCall, answered, guard, think, timings_mod, transport, settings, chunk, IDENTITY, KEYS)


def _sign_delay(guard):
    row = answered(guard)[-1]
    assert row.get("first_sign_at") is not None
    return row["first_sign_at"] - row["started_at"]


# R1 --------------------------------------------------------------------------------------------------

def test_r1_a_held_reply_first_sign_is_the_reasoning_chunk_not_the_text(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "30")      # guard on, no rescue in this test
    settings.forget()
    call = SeqCall([[0.02, think("a"), 0.3, chunk("answer", finish="stop")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "answer"
    delay = _sign_delay(guard)
    assert delay < 0.2, f"first sign stamped at the visible chunk ({delay:.2f}s), not at the thought"


def test_r1_guard_off_stamp_is_as_in_1842(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0")
    settings.forget()
    call = SeqCall([[0.02, think("a"), 0.3, chunk("answer", finish="stop")]])
    list(guard.stream(call))
    assert _sign_delay(guard) < 0.2


# R2 --------------------------------------------------------------------------------------------------

def test_r2_a_rescue_one_that_failed_does_not_make_rescue_two_wait(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0.5")
    settings.forget()
    call = SeqCall(by_model={
        "gemini-race": [[0.02, think("a"), 30.0], [0.02, R.per_minute()]],
        "gemini-race-two": [[0.02, chunk("next model", finish="stop")]],
    })
    started = time.monotonic()
    out = list(guard.stream(call))
    assert R.shown(out) == "next model"
    assert time.monotonic() - started < 1.2, "right after rescue 1 ended, not at twice the bound after it"
    assert call.models() == ["gemini-race", "gemini-race", "gemini-race-two"]


# R3 --------------------------------------------------------------------------------------------------

def test_r3_cold_bound_is_twenty_seconds():
    tracker = R.hedging_mod.HedgeTracker()
    assert tracker.quiet_bound(IDENTITY, False) == 20.0
    assert R.hedging_mod.HedgeTracker.quiet_bound.__doc__ is not None
    assert "_QUIET_MIN_SAMPLES" in R.hedging_mod.HedgeTracker.quiet_bound.__doc__


# R4 --------------------------------------------------------------------------------------------------

def test_r4_rescue_key_is_judged_against_the_provisional_legs_scope(guard):
    pacer = R.pacing_mod.PACER
    pacer._groups.update({KEYS[0]: "g1", KEYS[1]: "g1", KEYS[2]: "g2", KEYS[3]: "g2"})
    try:
        call = SeqCall([[30.0, chunk("silent primary")],              # the primary
                        [0.02, think("b"), 30.0],                      # a backup on the other group: provisional
                        [0.02, chunk("rescue answers", finish="stop")]])
        out = list(guard.stream(call))
        assert R.shown(out) == "rescue answers"
        assert len(call.opened) == 3
        primary_key, provisional_key, rescue_key = (o[1] for o in call.opened)
        assert not pacer.same_scope(primary_key, provisional_key)
        assert not pacer.same_scope(rescue_key, provisional_key), "never in the scope of the leg being rescued"
    finally:
        pacer._groups.clear()
