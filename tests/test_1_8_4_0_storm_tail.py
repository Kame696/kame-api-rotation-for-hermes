"""1.8.4.0 (E8) - the tail of a storm is raced, not knocked.

Replies a minute apart land in the TAIL of the busy window: after ``busy_until``, within
``max(first window, window)`` more. Since 1.8.3.1 one sequential knock on the best model decided there
whether the storm was still on, and the person waited for the 503. Now, where the start probe is allowed,
the reply starts on the first free model of the list at once and the best model is asked BESIDE it,
exactly as inside the window. Where the probe is not allowed the tail is the sequential knock, as before.

The clock is real; "a minute later" is made by moving the busy state back in time.
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_9_start_probe as S
from tests.test_1_8_2_9_start_probe import PRIMARY, Scripted, fresh, pool  # noqa: F401  (fixture by name)

R = S.R
settings = S.settings


def ended_ago(t, seconds=5.0, window=30.0):
    """The best model's window ended ``seconds`` ago; it was last asked when it was opened (long ago)."""
    now = time.time()
    t._busy_until[PRIMARY] = now - seconds
    t._busy_window[PRIMARY] = window
    t._probed_at[PRIMARY] = now - seconds - window


def advance(t, seconds):
    """A virtual clock: everything KAME stamped happened ``seconds`` earlier."""
    for table in (t._busy_until, t._probed_at):
        for name in list(table):
            table[name] -= seconds


# --- the rule -----------------------------------------------------------------------------------------

def test_a_reply_in_the_tail_starts_on_the_next_model_and_asks_the_best_beside_it(fresh):
    ended_ago(fresh)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert set(call.models_asked) == {"gemini-race", "gemini-race-two"}
    assert call.models_asked[0] == "gemini-race-two", "no request on the best model before the next model's"
    assert abs(call.asked_at["gemini-race"] - call.asked_at["gemini-race-two"]) < 0.15, "both go out at once"


def test_the_best_model_answering_first_is_shown_and_the_window_closes(fresh):
    ended_ago(fresh)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05), "gemini-race-two": ("silent",)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert PRIMARY not in fresh._busy_window, "one answer closes the window"
    assert fresh._busy_until.get(PRIMARY, 0.0) == 0.0
    assert fresh.handed_over == 0


def test_a_503_from_the_probe_in_the_tail_doubles_the_window_and_the_reply_is_unharmed(fresh):
    ended_ago(fresh, window=30.0)
    call = Scripted(pool(), {"gemini-race": ("503",), "gemini-race-two": ("fast", 0.4)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    fresh._drain_hedge_losers()
    assert fresh._busy_window.get(PRIMARY, 0) >= 60.0 - 0.01, fresh._busy_window     # 30 -> 60 (D5), once
    assert fresh._busy_until[PRIMARY] > time.time()
    assert call.models_asked[0] == "gemini-race-two", "the reply started on the next model, not on a knock"


def test_the_tail_asks_the_best_model_at_most_once_per_twenty_seconds(fresh):
    ended_ago(fresh)
    first = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(first))
    assert "gemini-race" in first.models_asked
    # the probe lost silently: the window is as it was, in its tail, and was asked a moment ago
    second = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    list(fresh.stream(second))
    assert second.models_asked == ["gemini-race"], "not due again: today's sequential knock, the best model alone"


# --- where nothing changes ------------------------------------------------------------------------------

def test_a_window_that_ended_longer_ago_than_the_tail_is_a_sequential_knock(fresh):
    ended_ago(fresh, seconds=40.0, window=30.0)             # the tail is 30 s: over
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"]


def test_a_doubled_window_has_a_longer_tail(fresh):
    ended_ago(fresh, seconds=40.0, window=120.0)            # the tail is 120 s: still inside
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    list(fresh.stream(call))
    assert set(call.models_asked) == {"gemini-race", "gemini-race-two"}


def test_a_model_that_never_had_a_window_has_no_tail(fresh):
    fresh._busy_window[PRIMARY] = 30.0                      # a window size, but busy_until is 0
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    list(fresh.stream(call))
    assert call.models_asked == ["gemini-race"]


def test_saver_keeps_the_sequential_knock_in_the_tail(fresh, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    ended_ago(fresh)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"], "no probe leg, no hand-over without a request"


def test_a_background_job_keeps_the_sequential_knock_in_the_tail(fresh):
    ended_ago(fresh)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    call.channel = "secondary"
    assert fresh._start_probe_plan(call, PRIMARY, True, R.KEYS) is None
    try:
        list(fresh.stream(call))
    except Exception:
        pass
    assert "gemini-race-two" not in call.models_asked


def test_a_non_stream_call_keeps_the_sequential_knock_in_the_tail(fresh):
    ended_ago(fresh)
    assert fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, False, R.KEYS) is None
    assert fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, True, R.KEYS) is not None


def test_never_keeps_the_sequential_knock_in_the_tail(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    ended_ago(fresh)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"]


# --- the 1.8.3.1 reopen, through the probe leg ---------------------------------------------------------

def test_the_reopen_of_1_8_3_1_still_happens_when_the_busy_answer_comes_through_the_probe_leg(fresh):
    ended_ago(fresh, window=30.0)
    seen = []
    original = fresh._note_busy

    def note(identity, **kwargs):
        original(identity, **kwargs)
        seen.append((identity, fresh._busy_window.get(identity, 0.0)))

    fresh._note_busy = note
    call = Scripted(pool(), {"gemini-race": ("503",), "gemini-race-two": ("fast", 0.4)})
    list(fresh.stream(call))
    fresh._drain_hedge_losers()
    assert (PRIMARY, 60.0) in seen, seen                    # reopened once, doubled, as for a sequential knock


# --- a five-minute storm, a reply a minute -------------------------------------------------------------

def test_replies_a_minute_apart_through_a_five_minute_storm_start_at_once_from_the_second_on(fresh):
    firsts = []
    for n in range(6):
        call = Scripted(pool(), {"gemini-race": ("503",), "gemini-race-two": ("fast", 0.4)})
        begun = time.monotonic()
        out = list(fresh.stream(call))
        fresh._drain_hedge_losers()
        assert R.shown(out) == "gemini-race-two here"
        firsts.append((n, call.asked_at["gemini-race-two"] - begun, call.models_asked.count("gemini-race"),
                       call.models_asked[0]))
        advance(fresh, 59.0)                                 # a minute between replies (the reply took the rest)
    # reply 0 meets a calm model (it knocks); from reply 1 on the next model starts at t = 0 beside one probe
    for n, took, asks, first_asked in firsts[1:]:
        assert took < 0.15, firsts
        assert first_asked == "gemini-race-two", firsts     # not a knock on the busy model first
        assert asks == 1, firsts
