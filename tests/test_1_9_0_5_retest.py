"""1.9.0.5 (E1, decisions/0036): the live re-test while every key rests on a label.

A fake clock and a fake provider, as in ``test_transport_1819``: nothing sleeps, nothing touches the network.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_retest_1905_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
events_mod = importlib.import_module(f"{PACKAGE}.core.events")
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"AIzaSyKEY{i}" + "0" * 29 for i in range(3)]
IDENTITY = "gemini:gemini-test"
REQUEST = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}
START = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = START
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def time_ns(self) -> int:
        return int(self.now * 1e9)

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    fake = SimpleNamespace(time=c.time, monotonic=c.monotonic, time_ns=c.time_ns, sleep=c.sleep,
                           strftime=__import__("time").strftime, localtime=__import__("time").localtime,
                           gmtime=__import__("time").gmtime)
    monkeypatch.setattr(carousel_mod, "time", fake)
    monkeypatch.setattr(transport, "time", fake)
    settings.forget()
    events_mod.EVENTS.clear()
    yield c
    settings.forget()


class Refusal(Exception):
    def __init__(self, status: int, message: str, details=()):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message, "status": "RESOURCE_EXHAUSTED",
                               "details": list(details)}}


def per_day() -> Refusal:
    """Google's daily label with no time of its own."""
    return Refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}])


def text(t: str, finish=None):
    delta = SimpleNamespace(role="assistant", content=t, tool_calls=None)
    return SimpleNamespace(id="c", model="m", choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)],
                           usage=None)


class Provider:
    """Answers (``healthy``) or refuses with the daily label; records when each key was asked."""

    def __init__(self, clock, healthy: bool, only_after: float = 0.0):
        self.clock = clock
        self.healthy = healthy
        self.only_after = only_after
        self.asked = []   # (seconds since start, key)

    def run(self, key, request):
        at = self.clock.now - START
        self.asked.append((at, key))
        if not self.healthy or at < self.only_after:
            raise per_day()

        def gen():
            yield text("back", "stop")
        return gen()


class FakeCall(transport.Call):
    def __init__(self, provider, clock, keys=KEYS, stop_after=None):
        self.provider_obj = provider
        self.clock = clock
        self._keys = list(keys)
        self.kwargs = dict(REQUEST)
        self.provider = "gemini"
        self.identity = IDENTITY
        self.stop_after = stop_after

    def keys(self):
        return list(self._keys)

    def client_for(self, key, attempt):
        def create(**request):
            return self.provider_obj.run(key, request)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return self.stop_after is not None and self.clock.now - START >= self.stop_after


def _transport(clock):
    return transport.KameTransport(engine=carousel_mod.Carousel(), sleep=clock.sleep)


def _rest_whole_pool_on_a_label(t, held_s=None):
    """Every key daily-labelled with no stated time; the doubt window long closed, so the hold is an hour."""
    t.engine._no_answer_since[IDENTITY] = START - 10_000.0
    for key in KEYS:
        t.engine.mark(IDENTITY, key, False, 0.0, "daily", now=START)
    assert t.engine.healthy_count(IDENTITY, KEYS, now=START) == 0
    assert t.engine.next_recovery_seconds(IDENTITY, KEYS, now=START) > 1000


def _shown(chunks):
    out = ""
    for c in chunks:
        for ch in getattr(c, "choices", None) or []:
            t = getattr(ch.delta, "content", None)
            if t:
                out += t
    return out


def test_a_healthy_provider_behind_a_daily_label_answers_at_about_twenty_seconds(clock):
    t = _transport(clock)
    _rest_whole_pool_on_a_label(t)
    provider = Provider(clock, healthy=True)
    out = list(t.stream(FakeCall(provider, clock)))
    assert _shown(out) == "back"
    assert len(provider.asked) == 1
    at, key = provider.asked[0]
    assert 19.0 <= at <= 22.0
    assert t.retests == 1
    kinds = [row["kind"] for row in events_mod.EVENTS.recent(20)]
    assert "retest" in kinds
    row = [r for r in events_mod.EVENTS.recent(20) if r["kind"] == "retest"][0]
    assert row["key"] and row["key"] not in KEYS and row["identity"] == IDENTITY
    # the answer ended the episode: the pool is usable again and the schedule is gone
    assert t.engine.healthy_count(IDENTITY, KEYS) >= 1
    assert IDENTITY not in t.engine._retest


def test_a_provider_that_really_refuses_is_asked_at_20_40_80_160_300_then_every_300(clock):
    t = _transport(clock)
    _rest_whole_pool_on_a_label(t)
    provider = Provider(clock, healthy=False)
    call = FakeCall(provider, clock, stop_after=1000.0)
    with pytest.raises(Exception):
        list(t.stream(call))
    times = [round(at) for at, _key in provider.asked]
    # intervals 20, 40, 80, 160, 300, 300: cumulative 20, 60, 140, 300, 600, 900 - and nothing in between
    assert times == [20, 60, 140, 300, 600, 900]
    assert t.retests == 6
    # one at a time, soonest first: the first three rounds cover three different keys
    assert len({key for _at, key in provider.asked[:3]}) == 3


def test_a_stated_deadline_is_never_re_tested(clock):
    t = _transport(clock)
    for key in KEYS:
        # the provider said when: 2 minutes
        t.engine.mark(IDENTITY, key, False, 120.0, "rate_limit", now=START, stated=True)
    assert t.engine.retest_due_in(IDENTITY, KEYS, now=START) is None
    assert t.engine.retest_candidate(IDENTITY, KEYS, now=START + 60) is None


def test_a_daily_label_holding_a_stated_long_deadline_is_not_re_tested(clock):
    t = _transport(clock)
    for key in KEYS:
        t.engine.mark(IDENTITY, key, False, 3600.0, "daily", now=START, stated=True)
    assert t.engine.retest_candidate(IDENTITY, KEYS, now=START + 60) is None


def test_nothing_is_re_tested_when_a_key_is_back_within_the_first_interval(clock):
    t = _transport(clock)
    for key in KEYS:
        t.engine.mark(IDENTITY, key, False, 0.0, "daily", now=START)   # 300 s re-probe (pool alive)
    t.engine._pools[IDENTITY][KEYS[0]]["sick_until"] = START + 15.0
    assert t.engine.retest_due_in(IDENTITY, KEYS, now=START) is None


def test_at_most_one_re_test_is_in_flight_per_identity(clock):
    t = _transport(clock)
    _rest_whole_pool_on_a_label(t)
    engine = t.engine
    assert engine.retest_due_in(IDENTITY, KEYS, now=START) == pytest.approx(20.0)
    assert engine.retest_candidate(IDENTITY, KEYS, now=START + 19.0) is None
    first = engine.retest_candidate(IDENTITY, KEYS, now=START + 20.0)
    assert first in KEYS
    # a second reply asking at the same moment, or while the first has not come back, gets nothing
    assert engine.retest_candidate(IDENTITY, KEYS, now=START + 20.0) is None
    assert engine.retest_candidate(IDENTITY, KEYS, now=START + 59.0) is None
    # the first one came back refused: the next is on the schedule, not before
    engine.mark(IDENTITY, first, False, 0.0, "daily", now=START + 21.0)
    assert engine.retest_candidate(IDENTITY, KEYS, now=START + 59.0) is None
    second = engine.retest_candidate(IDENTITY, KEYS, now=START + 60.0)
    assert second in KEYS and second != first


def test_any_answer_resets_the_schedule(clock):
    t = _transport(clock)
    _rest_whole_pool_on_a_label(t)
    engine = t.engine
    engine.retest_due_in(IDENTITY, KEYS, now=START)
    first = engine.retest_candidate(IDENTITY, KEYS, now=START + 20.0)
    engine.mark(IDENTITY, first, True, now=START + 21.0)
    assert IDENTITY not in engine._retest
    # a fresh episode starts its first interval again
    engine.mark(IDENTITY, first, False, 0.0, "daily", now=START + 100.0)
    for key in KEYS:
        engine.mark(IDENTITY, key, False, 0.0, "daily", now=START + 100.0)
    assert engine.retest_due_in(IDENTITY, KEYS, now=START + 100.0) == pytest.approx(20.0)


def test_money_is_never_re_tested(clock):
    t = _transport(clock)
    for key in KEYS:
        t.engine.mark(IDENTITY, key, False, 0.0, "insufficient_quota", now=START)
    assert t.engine.retest_due_in(IDENTITY, KEYS, now=START) is None


def test_a_re_test_that_is_refused_goes_through_the_normal_mark(clock):
    t = _transport(clock)
    _rest_whole_pool_on_a_label(t)
    provider = Provider(clock, healthy=False)
    with pytest.raises(Exception):
        list(t.stream(FakeCall(provider, clock, stop_after=30.0)))
    asked_key = provider.asked[0][1]
    state = t.engine._pools[IDENTITY][asked_key]
    assert state["failures"] >= 2 and state["kind"] == "daily"
    assert t.engine.healthy_count(IDENTITY, KEYS) == 0


def test_the_chat_line_says_it(clock):
    chat_line = importlib.import_module(f"{PACKAGE}.chat_line")
    assert "retest" in chat_line._ACTING
    text_ = chat_line._acting_text("retest", "every key rests - asking one again now", {"identity": IDENTITY})
    assert "asking one again" in text_


def test_a_503_or_a_stated_per_minute_refusal_during_a_re_test_keeps_the_key_in_the_re_testing(clock):
    t = _transport(clock)
    _rest_whole_pool_on_a_label(t)
    engine = t.engine
    # the key's last error kind is no longer "daily", but the label hold it rests on still stands
    engine.mark(IDENTITY, KEYS[0], False, 5.0, "server", now=START + 20.0)
    engine.mark(IDENTITY, KEYS[1], False, 30.0, "per_minute", now=START + 20.0, stated=True)
    held = [key for _until, key in engine._label_held(IDENTITY, KEYS, START + 21.0)]
    assert sorted(held) == sorted(KEYS)
    assert engine.retest_due_in(IDENTITY, KEYS, now=START + 21.0) == pytest.approx(20.0)
    assert engine.retest_candidate(IDENTITY, KEYS, now=START + 41.0) in KEYS


def test_an_answer_or_a_stated_quota_deadline_ends_the_label_hold(clock):
    t = _transport(clock)
    _rest_whole_pool_on_a_label(t)
    engine = t.engine
    engine.mark(IDENTITY, KEYS[0], False, 3600.0, "daily", now=START + 5.0, stated=True)   # the provider named the time
    engine.mark(IDENTITY, KEYS[1], True, now=START + 6.0)                                  # an answer
    held = [key for _until, key in engine._label_held(IDENTITY, KEYS, START + 7.0)]
    assert KEYS[0] not in held and KEYS[1] not in held and KEYS[2] in held
