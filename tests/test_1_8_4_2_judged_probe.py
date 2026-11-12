"""1.8.4.2 (S1e) - a start probe that lost the race is heard to its end.

The probe of the busy (or remembered-failing) best model used to be cut the moment the reply's own leg
brought a word - two seconds on the next model - long before a recovered best model could show its own first
word, so "the best model is back" could never be learned. Now a daemon judge waits for the probe's first
chunk, its end, its error or 30 s, then cuts it; the verdict is accounted for on the next call.

Real threads and real (short) time, on the fixtures of the start-probe tests.
"""
from __future__ import annotations

import threading
import time

import pytest

from tests import test_1_8_2_9_start_probe as S
from tests.test_1_8_2_9_start_probe import (  # noqa: F401  (fixture by name)
    PRIMARY, Refusal, Scripted, busy_and_due, fresh, pool)

R = S.R
transport = R.transport
EVENTS = __import__(f"{R.PACKAGE}.core.events", fromlist=["x"]).EVENTS


class Timed(Scripted):
    """Adds ('late503', delay): the stream fails with a 503 AFTER ``delay`` seconds, mid-stream."""

    def client_for(self, key, attempt):
        inner = super().client_for(key, attempt)
        create_inner = inner.chat.completions.create

        def create(**request):
            how = self.script.get(request.get("model"), ("silent",))
            if how[0] == "late503":
                self.models_asked.append(request.get("model"))
                self.asked_at.setdefault(request.get("model"), time.monotonic())
                return R.FakeStream([how[1], Refusal(503, "The model is overloaded. Please try again later.")])
            return create_inner(**request)

        inner.chat.completions.create = create
        return inner


def judges():
    return [t for t in threading.enumerate() if t.name == "kame-probe-judge"]


def wait_for(predicate, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def no_judges_left():
    return wait_for(lambda: not judges(), 8.0)


def test_a_probe_that_answers_after_the_reply_clears_the_window_and_leads_the_next_reply(fresh):
    busy_and_due(fresh)
    transport.HEALTH.note(PRIMARY, False, now=time.time() - 20)         # the record says it keeps failing too
    call = Timed(pool(), {"gemini-race": ("fast", 0.8), "gemini-race-two": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here", "the reply was never delayed by the probe"
    assert PRIMARY in fresh._busy_window, "not yet judged: the window still stands"
    assert wait_for(lambda: len(fresh._probe_verdicts) == 1), "the judge heard the probe's first word"
    EVENTS.clear()
    fresh._drain_probe_verdicts()
    assert PRIMARY not in fresh._busy_window and PRIMARY not in fresh._busy_until
    assert PRIMARY not in fresh._probed_at
    assert transport.HEALTH.summary(PRIMARY)[0] <= 2 and not transport.HEALTH.risky(PRIMARY)
    lines = [r["reason"] for r in EVENTS.recent(20) if "answered again beside the last reply" in r["reason"]]
    assert len(lines) == 1 and "the next reply starts on it" in lines[0]
    # the next reply starts on the best model, alone
    nxt = Timed(pool(), {"gemini-race": ("fast", 0.05)})
    list(fresh.stream(nxt))
    assert nxt.models_asked == ["gemini-race"]
    assert no_judges_left()


def test_a_probe_silent_to_the_deadline_opens_the_window_and_is_cut(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_PROBE_JUDGE_S", 1.0)
    busy_and_due(fresh)
    window = fresh._busy_window[PRIMARY]
    call = Timed(pool(), {"gemini-race-two": ("fast", 0.05)})              # the best model stays silent
    list(fresh.stream(call))
    assert wait_for(lambda: len(fresh._probe_verdicts) == 1, 6.0)
    assert fresh._probe_verdicts[0][2] == "silent"
    before = transport.HEALTH.summary(PRIMARY)
    fresh._drain_probe_verdicts()
    assert fresh._busy_window[PRIMARY] >= window * 2 - 0.01, "the window opened again, doubled"
    assert transport.HEALTH.summary(PRIMARY)[0] == before[0] + 1, "a failure in the record"
    assert [s for k, s in call.pool.opened if s.response.closed.is_set()], "the silent leg was cut"
    assert no_judges_left()


def test_a_probe_503_after_the_decision_rests_the_key_and_opens_the_window(fresh):
    busy_and_due(fresh)
    window = fresh._busy_window[PRIMARY]
    call = Timed(pool(), {"gemini-race": ("late503", 0.6), "gemini-race-two": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert wait_for(lambda: len(fresh._probe_verdicts) == 1)
    assert fresh._probe_verdicts[0][2] == "failed"
    before = transport.HEALTH.summary(PRIMARY)
    fresh._drain_probe_verdicts()
    assert fresh._busy_window[PRIMARY] >= window * 2 - 0.01
    assert transport.HEALTH.summary(PRIMARY)[0] == before[0] + 1
    assert no_judges_left()


def test_a_probe_that_won_the_race_is_not_judged(fresh):
    busy_and_due(fresh)
    call = Timed(pool(), {"gemini-race": ("fast", 0.05), "gemini-race-two": ("silent",)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    time.sleep(0.5)
    assert not judges() and not fresh._probe_verdicts, "it was the reply, not a probe to judge"


def test_a_stopped_turn_does_not_stop_the_judge(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_PROBE_JUDGE_S", 1.0)
    busy_and_due(fresh)
    call = Timed(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(call))
    call.closed = True                                                  # the user pressed stop
    assert wait_for(lambda: len(fresh._probe_verdicts) == 1, 6.0), "the deadline still ends the judge"
    assert no_judges_left()


def test_the_drain_runs_at_the_next_call_start(fresh):
    busy_and_due(fresh)
    call = Timed(pool(), {"gemini-race": ("fast", 0.6), "gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(call))
    assert wait_for(lambda: len(fresh._probe_verdicts) == 1)
    nxt = Timed(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(nxt))                                        # no explicit drain
    assert not fresh._probe_verdicts
    assert R.shown(out) == "gemini-race here" and nxt.models_asked == ["gemini-race"], \
        "the verdict was read before the start decision: the best model leads again"
