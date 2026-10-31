"""1.8.3.1 (PLAN_1.8.3.1 D5, L7) - the first busy answer after a busy window ended reopens it.

The same scenario as Agent Zero's ``test_l7_the_first_busy_answer_after_a_window_reopens_it_once...``:
a window that ended five seconds ago, one busy (503) answer from the first key, and the reply goes on.
Hermes used to wait for a second busy key before it noted the busy model again, so every reply during
the half minute after a window knocked once more on a model that was still busy. Now one knock decides.
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_8_adaptive as A

transport = A.transport
fresh = A.fresh
ID = A.PRIMARY


def _storm_ended_five_seconds_ago(t, window=30.0):
    t._busy_until[ID] = time.time() - 5.0
    t._busy_window[ID] = window


def _first_key_busy_then_ok():
    return A.Plan({A.KEYS[0]: [A.overloaded()], A.KEYS[1]: ["ok"], A.KEYS[2]: ["ok"], A.KEYS[3]: ["ok"]})


def _spy(t, monkeypatch):
    """What each ``_note_busy`` left behind at the moment it ran (an answer clears the window afterwards)."""
    seen = []
    original = t._note_busy

    def note(identity, **kwargs):
        original(identity, **kwargs)
        seen.append({"identity": identity, "until": t._busy_until.get(identity, 0.0),
                     "window": t._busy_window.get(identity, 0.0)})

    monkeypatch.setattr(t, "_note_busy", note)
    return seen


def _reply(t, call=None):
    plan = _first_key_busy_then_ok()
    call = call or A.FakeCall(plan)
    return t.complete(call), plan


def test_the_first_busy_answer_after_a_window_reopens_it(fresh, monkeypatch):
    _storm_ended_five_seconds_ago(fresh)
    seen = _spy(fresh, monkeypatch)
    result, plan = _reply(fresh)
    assert result.choices[0].message.content == "ok"
    assert len(seen) == 1, "one busy knock after the window must reopen it, once"
    assert seen[0]["until"] > time.time()


def test_the_window_doubles_as_it_does_for_two_busy_keys(fresh, monkeypatch):
    _storm_ended_five_seconds_ago(fresh, window=30.0)
    seen = _spy(fresh, monkeypatch)
    _reply(fresh)
    assert seen[0]["window"] == pytest.approx(60.0)           # caught again within a window of its end


def test_a_busy_answer_inside_a_running_window_does_not_reopen_it_again(fresh, monkeypatch):
    fresh._busy_until[ID] = time.time() + 20.0               # still open
    fresh._busy_window[ID] = 30.0
    seen = _spy(fresh, monkeypatch)
    _reply(fresh)
    assert seen == []


def test_a_model_that_was_never_busy_is_not_marked_by_one_busy_key(fresh, monkeypatch):
    # No window ever: the first busy answer of a calm model is just one key's answer.
    seen = _spy(fresh, monkeypatch)
    _reply(fresh)
    assert seen == []


def test_the_key_that_answered_busy_still_rests_only_a_moment(fresh):
    _storm_ended_five_seconds_ago(fresh)
    _reply(fresh)
    rest = fresh.engine.next_recovery_seconds(ID, [A.KEYS[0]])
    assert rest is None or rest <= 1.5, rest


def test_never_switch_keeps_the_old_rule(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    transport.settings.forget()
    _storm_ended_five_seconds_ago(fresh)
    seen = _spy(fresh, monkeypatch)
    _reply(fresh)
    assert seen == []


def test_a_background_job_does_not_reopen_the_window(fresh, monkeypatch):
    _storm_ended_five_seconds_ago(fresh)
    seen = _spy(fresh, monkeypatch)
    call = A.FakeCall(_first_key_busy_then_ok())
    call.channel = "secondary"
    try:
        fresh.complete(call)
    except Exception:
        pass
    assert seen == []
