"""1.8.2.9 (decisions/0031 J2, J3): the bounded "Faster" and its step back.

The stored value is still ``max``. A healthy reply sends ONE request (the old top mode sent two on
every reply); the first backup goes out at 1.5x the model's usual first word (Balanced 2x), from a
budget three times Balanced's; any quota refusal on a model steps Faster back to Balanced for it
for ten minutes, and it returns by itself. Balanced is pinned byte for byte.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_faster_mode.py -q
"""
from __future__ import annotations

import importlib
import os

import pytest

from tests import test_1_8_2_5_stream_race as R

settings = importlib.import_module(f"{R.PACKAGE}.settings")
hedging = R.hedging_mod
IDENT = "gemini:gemini-race"
OTHER = "gemini:gemini-other"


@pytest.fixture()
def tracker(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    t = hedging.HedgeTracker()
    yield t, monkeypatch
    settings.forget()


def mode(monkeypatch, value):
    monkeypatch.setenv("KAME_HEDGE_MODE", value)
    settings.forget()


def learn(t, median=2.0, count=40, identity=IDENT, reasoning=False, now=1000.0):
    for _ in range(count):
        t.record_latency(identity, median, stream=True, reasoning=reasoning, now=now)


def backups(t, calls, identity=IDENT, reasoning=False, start=1000.0):
    """How many backup legs the budget lets out over ``calls`` slow answered streams."""
    granted = 0
    for n in range(calls):
        now = start + n
        if t.may_start_backup(identity, healthy_count=14, under_pressure=False, stream=True,
                              reasoning=reasoning, now=now):
            granted += 1
        t.record_latency(identity, 5.0, stream=True, reasoning=reasoning, now=now)   # an answer earns tokens
    return granted


# --- the triggers ------------------------------------------------------------------------

def test_balanced_is_pinned_exactly_as_in_1828(tracker):
    t, mp = tracker
    mode(mp, "adaptive")
    assert t.stream_triggers(IDENT, reasoning=False, now=1000.0) == (6.0, 12.0)     # cold
    assert t.stream_triggers(IDENT, reasoning=True, now=1000.0) == (30.0, 60.0)
    learn(t, 2.0)
    assert t.stream_triggers(IDENT, reasoning=False, now=1000.0) == (4.0, 8.0)      # 2x the median
    assert t.fires_from_dispatch(IDENT) is False


def test_faster_goes_out_at_one_and_a_half_times_the_median_and_half_the_cold_number(tracker):
    t, mp = tracker
    mode(mp, "max")
    assert t.stream_triggers(IDENT, reasoning=False, now=1000.0) == (3.0, 6.0)      # cold 6 -> 3
    assert t.stream_triggers(IDENT, reasoning=True, now=1000.0) == (15.0, 30.0)     # cold 30 -> 15
    learn(t, 2.0)
    assert t.stream_triggers(IDENT, reasoning=False, now=1000.0) == (3.0, 6.0)      # 1.5 x 2.0
    assert t.fires_from_dispatch(IDENT) is False          # nothing fires at the first instant any more


def test_a_non_streamed_call_hedges_at_three_quarters_of_the_learned_time(tracker):
    t, mp = tracker
    for _ in range(40):
        t.record_latency(IDENT, 5.0, stream=False, reasoning=False, now=1000.0)
    mode(mp, "adaptive")
    base = t.threshold(IDENT, stream=False, reasoning=False, now=1000.0)
    mode(mp, "max")
    assert t.threshold(IDENT, stream=False, reasoning=False, now=1000.0) == pytest.approx(base * 0.75)


# --- the budget --------------------------------------------------------------------------

def test_saver_never_races(tracker):
    t, mp = tracker
    mode(mp, "off")
    assert backups(t, 50) == 0


def test_a_hundred_slow_answers_cost_at_most_the_budget_in_each_mode(tracker):
    t, mp = tracker
    mode(mp, "adaptive")
    balanced = backups(t, 100)
    t.clear()
    mode(mp, "max")
    faster = backups(t, 100)
    assert balanced <= 3 + 10 and faster <= 3 + 30       # burst + budget share of the 100 answers
    assert faster > balanced                              # and Faster really is the bigger budget


def test_a_mode_change_starts_with_the_other_modes_tokens_not_inherited(tracker):
    t, mp = tracker
    mode(mp, "adaptive")
    while t.may_start_backup(IDENT, healthy_count=14, under_pressure=False, stream=True, reasoning=False,
                             now=1000.0):
        pass                                              # Balanced's tokens are spent
    mode(mp, "max")
    assert t.may_start_backup(IDENT, healthy_count=14, under_pressure=False, stream=True, reasoning=False,
                              now=1000.0) is True         # Faster has its own full bucket


# --- the step back -----------------------------------------------------------------------

def test_a_quota_refusal_steps_faster_back_to_balanced_for_ten_minutes_then_it_returns(tracker):
    t, mp = tracker
    mode(mp, "max")
    learn(t, 2.0)
    assert t.stream_triggers(IDENT, reasoning=False, now=1000.0) == (3.0, 6.0)
    t.note_quota_refusal(IDENT, now=1000.0)
    assert t.stream_triggers(IDENT, reasoning=False, now=1001.0) == (4.0, 8.0)       # Balanced's
    assert t.faster_paused_for(IDENT, now=1001.0) == pytest.approx(599.0)
    t.note_quota_refusal(IDENT, now=1500.0)                                          # another one pushes it out
    assert t.stream_triggers(IDENT, reasoning=False, now=2000.0) == (4.0, 8.0)       # 1000+600 would have ended
    assert t.stream_triggers(IDENT, reasoning=False, now=2101.0) == (3.0, 6.0)       # 1500+600: Faster again


def test_the_step_back_is_per_model(tracker):
    t, mp = tracker
    mode(mp, "max")
    learn(t, 2.0)
    learn(t, 2.0, identity=OTHER)
    t.note_quota_refusal(IDENT, now=1000.0)
    assert t.stream_triggers(OTHER, reasoning=False, now=1001.0) == (3.0, 6.0)


def test_which_refusals_step_it_back():
    transport = R.transport
    assert transport._HEDGE_QUOTA_KINDS == {"per_minute", "rate_limit", "daily", "insufficient_quota"}
    for not_quota in ("server", "timeout", "other", "empty", "auth", "denied", "revoked"):
        assert not_quota not in transport._HEDGE_QUOTA_KINDS


def short_per_minute():
    """The per-minute refusal of the stream-race fixtures, with a one-second retry delay."""
    return R.Refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}])


def run_refusals(fresh, refusal):
    """Two keys, each refuses once and then answers: the refusal is certain to be seen."""
    keys = R.KEYS[:2]
    pool = R.Pool({key: [[refusal], [R.chunk("ok", finish="stop")]] for key in keys})
    out = list(fresh.stream(R.FakeCall(pool, keys=keys)))
    assert len(pool.opened) >= 2
    return R.shown(out)


@pytest.fixture()
def transport_fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_HEDGE_MODE", "max")
    settings.forget()
    hedging.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(hedging.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    t = R.transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    hedging.HEDGE_TRACKER.clear()


def test_a_per_minute_refusal_on_the_main_request_steps_faster_back(transport_fresh):
    assert run_refusals(transport_fresh, short_per_minute()) == "ok"
    assert hedging.HEDGE_TRACKER.faster_paused_for(R.IDENTITY) > 500


def test_an_overloaded_503_does_not_step_faster_back(transport_fresh):
    assert run_refusals(transport_fresh, R.Refusal(503, "The model is overloaded.")) == "ok"
    assert hedging.HEDGE_TRACKER.faster_paused_for(R.IDENTITY) == 0.0
