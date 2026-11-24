"""1.9.0.6 - the owner's day spent (live session 2026-11-24 11:09): every key of every flash model refused for the DAY
(Google's ``GenerateRequestsPerDayPerProjectPerModel-FreeTier``, 20 a day), the main model 503 "high demand".

Seen live: the reply waited (right: no request is spent on a day that is gone) but its wait woke every ~0.6 s - a new
"every key resting" event, a spinner write and a state publish each time, for an hour and a half - with no request.
"""
from __future__ import annotations

import importlib
import time
from types import SimpleNamespace

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_9_0_6_walk_the_list as W
from tests.test_1_9_0_6_walk_the_list import fresh  # noqa: F401  (the fixture)

transport = R.transport


def daily(model):
    exc = P.Refusal(429, "You exceeded your current quota, please check your plan and billing details.")
    exc.body = {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                "quotaDimensions": {"model": model, "location": "global"}, "quotaValue": "20"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "58317s"}]}}
    return exc


class DaySpent(W.Owner):
    def __init__(self):
        super().__init__({})
        self.sent = []
        self.t0 = time.monotonic()

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            self.sent.append((round(time.monotonic() - self.t0, 2), model))
            if model == "gemini-4-hydrogen":
                raise P.Refusal(503, "This model is currently experiencing high demand.")
            raise daily(model)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class MainSpent(W.Owner):
    """The main model's day is spent on every key; the next model of the list answers."""

    def __init__(self):
        super().__init__({})
        self.sent = []

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            self.sent.append(model)
            if model == "gemini-4-hydrogen":
                raise daily(model)
            return R.FakeStream([0.05, R.chunk(f"{model} "), R.chunk("here", finish="stop")])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_every_key_resting_and_no_switch_left_kame_walks_the_list_itself(fresh, monkeypatch):
    """r3 of the live comparison: Hermes' switches for the answer were spent ("budget held") and KAME's own stand-in
    declined, so nothing handed the reply on; the circle back also declined, because a model after Hermes' position
    was free - for a hand-over that could not come. The reply sat on a model whose every key was out for the day,
    waking every half second, for 90 minutes, while the next model answered."""
    sb = importlib.import_module(f"{R.PACKAGE}.core.switch_budget")
    sb.reset_all()
    for _ in range(sb.allowed() + 1):
        sb.note_switch(sb.conversation_key())
    assert not sb.may_hand_over(sb.conversation_key())
    monkeypatch.setattr(transport.KameTransport, "_stand_in_for", lambda self, *a, **k: None)
    call = MainSpent()
    try:
        out, err, stuck, took = W.run(fresh, call, limit=15.0)
    finally:
        sb.reset_all()
    assert not stuck, f"the reply was still waiting after {took:.0f}s: {call.sent}"
    assert not err, err
    assert R.shown(out) == "gemini-3.8-flash here", call.sent


def test_a_spent_day_waits_quietly(fresh, monkeypatch):
    waits = []
    real = transport.KameTransport._wait_for_recovery

    def counted(self, *a, **k):
        waits.append(time.monotonic())
        return (yield from real(self, *a, **k))
    monkeypatch.setattr(transport.KameTransport, "_wait_for_recovery", counted)
    call = DaySpent()
    out, err, stuck, took = W.run(fresh, call, limit=20.0)
    late = [w for w in waits if w - call.t0 > 8.0]          # after the first lap
    print("requests", len(call.sent), call.sent[-6:], "waits", len(waits), "late waits", len(late), "err", err[:1])
    assert len(late) <= 4, f"the wait woke {len(late)} times in 12 s with nothing to do"
