"""1.8.4.3 (L11, PLAN section 7) - a model that said "busy" is raced by the next model from the retry's first instant.

Live 2026-11-13: a 503 takes the provider 2-4 s to send; the retry on the same model then sat silent and the next
model of the list was only asked 8-12 s into it (the cross timer: 3 x the usual first word, 8 s at least) - a timer
waiting to collect the evidence the 503 had already given. Now, once the model in use has answered busy on a key in
THIS reply, the attempt that follows asks the next model beside it at time 0.

Real time and real threads; no assertion on an absolute clock below 0.5 s (orders, counts, generous bounds).
Fakes: ``test_1_8_2_5_stream_race`` (``R``) and ``test_1_8_4_3_quiet_guard`` (``SeqCall``, the ``guard`` fixture).
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_4_3_quiet_guard import (  # noqa: F401  (guard is a fixture, picked up by name)
    EVENTS, SeqCall, answered, chunk, guard, kinds, reasons, think,
)

transport = R.transport
settings = R.settings

HOME = "gemini:gemini-race"
NEXT = "gemini:gemini-race-two"
THIRD = "gemini:gemini-race-three"
M_HOME, M_NEXT, M_THIRD = "gemini-race", "gemini-race-two", "gemini-race-three"
SILENT = 3.0


def overloaded():
    return R.Refusal(503, "The model is overloaded. Please try again later.")


def silent():
    return [SILENT, chunk("late", finish="stop")]


def reply(text, after=0.05):
    return [after, chunk(text + " ", ), chunk("answer", finish="stop")]


def fast_cross(monkeypatch, step=5.0):
    """The cross timer of a quiet PC: 0.3 s, then ``step`` s per further entry (the real floor is 8 s)."""
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", step)


def one_leg(monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MAX_LEGS", "1")
    settings.forget()


def run(t, call):
    started = time.monotonic()
    out = list(t.stream(call))
    return out, time.monotonic() - started


def race_requests(t):
    total = list(getattr(t, "_speed_total", []) or [])
    return total[transport._SPEED_RACE] if len(total) > transport._SPEED_RACE else 0


# 1 ---------------------------------------------------------------------------------------------------

def test_a_503_then_a_silent_retry_is_raced_by_the_next_model_at_once(guard):
    call = SeqCall(by_model={M_HOME: [[overloaded()]] + [silent()] * 4,
                             M_NEXT: [reply("next")]})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "next answer"
    assert elapsed < 3.0, "well under the 8 s floor of the cross timer"
    models = call.models()
    assert models[:3] == [M_HOME, M_HOME, M_NEXT], models
    said = [r for r in reasons() if "said it is busy" in r]
    assert said and "asked beside the retry" in said[0] and "(503)" in said[0], reasons()
    assert any("said it was busy" in r and "answered first" in r for r in reasons()), reasons()
    row = guard.replies[-1]
    assert row["model"] == transport.model_label(NEXT) and row.get("why") == "busy", row
    assert race_requests(guard) == 1
    assert guard._busy_until.get(HOME, 0.0) > time.time(), "the home model is inside a busy window"


# 2 ---------------------------------------------------------------------------------------------------

def test_the_retry_answering_first_wins_and_nothing_is_noted_against_either_model(guard):
    call = SeqCall(by_model={M_HOME: [[overloaded()], reply("home")] + [silent()] * 3,
                             M_NEXT: [silent()]})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "home answer"
    assert elapsed < 3.0
    assert M_NEXT in call.models()
    nxt = [s for m, _, s in call.opened if m == M_NEXT]
    assert nxt and nxt[0].response.closed.is_set(), "the next model's leg was cut"
    assert guard._busy_until.get(HOME, 0.0) <= time.time()
    assert guard._busy_until.get(NEXT, 0.0) <= time.time()
    assert not any("answered first" in r for r in reasons()), reasons()
    assert guard.replies[-1].get("why") in (None, ""), guard.replies[-1]


# 3 ---------------------------------------------------------------------------------------------------

def test_no_refusal_a_merely_slow_first_try_never_asks_the_next_model_before_its_timer(guard):
    call = SeqCall(by_model={M_HOME: [[1.0, chunk("ok", finish="stop")]] + [silent()] * 3})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "ok"
    assert set(call.models()) == {M_HOME}, call.models()
    assert not any("said it is busy" in r for r in reasons())


# 4 ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("first_failure", [R.per_minute, lambda: TimeoutError("timed out")],
                         ids=["a_429_per_minute_throttle", "a_timeout_of_ours"])
def test_a_failure_that_is_not_busy_does_not_race_the_retry(guard, first_failure):
    call = SeqCall(by_model={M_HOME: [[first_failure()], [0.4, chunk("ok", finish="stop")]] + [silent()] * 3,
                             M_NEXT: [reply("next")]})
    out, _ = run(guard, call)
    assert M_NEXT not in call.models()[:2], call.models()
    assert not any("said it is busy" in r for r in reasons()), reasons()


# 5 ---------------------------------------------------------------------------------------------------

def _retry_alone(call):
    assert M_NEXT not in call.models(), call.models()
    assert not any("said it is busy" in r for r in reasons()), reasons()


def _refused_then_ok():
    # the retry answers at once: before the fixture's 0.2 s backup-key trigger, so the reply is the retry's own
    return SeqCall(by_model={M_HOME: [[overloaded()], [0.02, chunk("ok", finish="stop")]], M_NEXT: [reply("next")]})


def test_hedging_off_after_a_503_the_retry_runs_alone(guard, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    call = _refused_then_ok()
    out, _ = run(guard, call)
    assert R.shown(out) == "ok"
    _retry_alone(call)


def test_never_switch_after_a_503_the_retry_runs_alone(guard, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    call = _refused_then_ok()
    out, _ = run(guard, call)
    assert R.shown(out) == "ok"
    _retry_alone(call)


def test_a_background_job_after_a_503_the_retry_runs_alone(guard):
    call = _refused_then_ok()
    call.channel, call.task, call.task_known = "secondary", "title_generation", True
    out, _ = run(guard, call)
    assert R.shown(out) == "ok"
    _retry_alone(call)


# 6 ---------------------------------------------------------------------------------------------------

def test_an_entry_already_refused_busy_as_a_race_leg_in_this_reply_is_not_the_one_moved(guard, monkeypatch):
    one_leg(monkeypatch)
    fast_cross(monkeypatch)
    # try 1: the home key is silent for 0.6 s then refused; at 0.3 s the next model joined and was refused busy
    # at once. Try 2: the home retry is silent; the next model is already known busy in this reply, so the
    # THIRD model is the one raced from the first instant.
    call = SeqCall(by_model={M_HOME: [[0.6, overloaded()], silent(), silent()],
                             M_NEXT: [[overloaded()], silent()],
                             M_THIRD: [reply("third")]})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "third answer"
    assert elapsed < 3.0
    models = call.models()
    assert models.count(M_NEXT) == 1, "the model refused busy as a leg was not asked a second time: %s" % models
    assert models.index(M_THIRD) > models.index(M_NEXT)
    assert any("said it is busy" in r for r in reasons()), reasons()


# 7 ---------------------------------------------------------------------------------------------------

def test_the_second_cross_entry_keeps_its_own_time(guard, monkeypatch):
    one_leg(monkeypatch)
    fast_cross(monkeypatch, step=0.5)
    call = SeqCall(by_model={M_HOME: [[overloaded()], silent(), silent()],
                             M_NEXT: [silent()],
                             M_THIRD: [reply("third")]})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "third answer"
    models = call.models()
    assert models[:4] == [M_HOME, M_HOME, M_NEXT, M_THIRD], models
    assert elapsed >= 0.6, "the third model was asked at its own timer, not at 0"
    assert elapsed < 4.0


# 8 ---------------------------------------------------------------------------------------------------

def test_under_the_quiet_watch_a_busy_cross_leg_that_thinks_then_goes_quiet_is_rescued(guard):
    call = SeqCall(by_model={M_HOME: [[overloaded()]] + [silent()] * 4,
                             M_NEXT: [[0.05, think("the next model thinks"), 30.0, chunk("never")],
                                      reply("rescued")]})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "rescued answer"
    assert elapsed < 4.0, (elapsed, call.models(), reasons())
    assert any("said it is busy" in r for r in reasons()), reasons()
    assert any("rescue" in r and "answered first" in r for r in reasons()), reasons()


# third review (2026-11-14), F5 ----------------------------------------------------------------------------

def test_a_rescue_that_wins_after_the_next_model_had_beaten_the_home_model_rests_the_home_model(guard, monkeypatch):
    # The home model stays silent, the next model is raced in by the cross timer and thinks (it is the provisional
    # winner), goes quiet, and a rescue on ANOTHER KEY OF THE NEXT MODEL answers. The home model lost the race to
    # the next model exactly as on a cross win, so its window opens; before, a rescue win skipped it.
    fast_cross(monkeypatch)
    call = SeqCall(by_model={M_HOME: [silent()] * 4,
                             M_NEXT: [[0.05, think("the next model thinks"), 30.0, chunk("never")],
                                      reply("rescued")]})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "rescued answer"
    assert elapsed < 3.0, (elapsed, call.models(), reasons())
    assert guard._busy_until.get(HOME, 0.0) > time.time(), "the home model lost to the next model: its window opens"
    assert guard._busy_until.get(NEXT, 0.0) <= time.time(), "the next model answered (through its rescue)"


def test_a_rescue_of_the_home_model_itself_does_not_rest_the_home_model(guard):
    # The home leg thinks, goes quiet, another key of the home model answers: nothing was slow, nothing rests.
    call = SeqCall(by_model={M_HOME: [[0.05, think("home thinks"), 30.0, chunk("never")], reply("rescued")]})
    out, elapsed = run(guard, call)
    assert R.shown(out) == "rescued answer"
    assert guard._busy_until.get(HOME, 0.0) <= time.time()
