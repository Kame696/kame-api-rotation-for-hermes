"""1.9.0.6 (failures/0034, the owner's test of 2026-11-24 07:43) - one reply never floods the provider with one request.

Google answered "429: The query-per-second (QPS) limit for identical or near-identical request payloads has been
exceeded" on every key and every model. KAME rotated into it: 184 copies of the same request in two minutes, each one
renewing the limit, and no answer. Here a fake provider keeps that rule (a payload asked again sooner than GAP after the
last ask of it is refused, on any key and any model) and the reply must answer, with few requests.
"""
from __future__ import annotations

import threading
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_9_0_6_walk_the_list as W
from tests.test_1_9_0_6_walk_the_list import fresh  # noqa: F401  (the fixture)

import importlib

pace_mod = importlib.import_module(f"{R.PACKAGE}.core.payload_pace")
transport = R.transport

IDENTICAL = ("The query-per-second (QPS) limit for identical or near-identical request payloads has been exceeded. "
             "See https://ai.dev/rate-limit to manage your rate limits.")


class Google(W.Owner):
    """Every model of the owner's list, one rule for the payload: asked again within ``gap`` of its last ask (any key,
    any model) -> 429 identical. ``blocked_until``: the limit is already on when the reply starts (the owner's 07:43)."""

    def __init__(self, gap=1.5, blocked_s=0.0, plain_429_s=0.0):
        super().__init__({})
        self.gap = gap
        self.t0 = time.monotonic()
        self.blocked_until = self.t0 + blocked_s
        self.plain_until = self.t0 + plain_429_s
        self.last = None
        self.sent = []
        self._lock = threading.Lock()

    def client_for(self, key, attempt):
        outer = self

        def create(**request):
            now = time.monotonic()
            with outer._lock:
                outer.sent.append((now - outer.t0, request.get("model"), key))
                last, outer.last = outer.last, now
            if now < outer.plain_until:
                raise P.Refusal(429, "Resource has been exhausted (e.g. check quota).")
            if now < outer.blocked_until or (last is not None and now - last < outer.gap):
                raise P.Refusal(429, IDENTICAL)
            return R.FakeStream([0.05, R.chunk(f"{request.get('model')} "), R.chunk("here", finish="stop")])
        from types import SimpleNamespace
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture()
def quick_holds(monkeypatch):
    monkeypatch.setattr(pace_mod, "HOLD_STEPS", (2.0, 4.0, 8.0, 16.0, 30.0))
    yield


def test_the_owners_0743_the_limit_is_on_and_the_reply_waits_instead_of_rotating(fresh, quick_holds):
    """The limit is already on (the storm before it): the first ask is refused "identical"; the reply holds the
    payload and asks again after the gap, and answers. 1.9.0.6 before this fix rotated into it forever."""
    call = Google(gap=1.5, blocked_s=1.0)
    out, err, stuck, took = W.run(fresh, call, limit=30.0)
    assert not stuck, f"the reply was still going after {took:.0f}s with {len(call.sent)} requests"
    assert not err, err
    assert R.shown(out).endswith("here"), call.sent
    assert len(call.sent) <= 6, f"{len(call.sent)} requests: {call.sent}"
    assert took < 10.0


def test_a_hold_is_never_a_rotation_no_key_rests_and_no_model_is_written_busy(fresh, quick_holds):
    call = Google(gap=1.5, blocked_s=1.0)
    before = transport.HEALTH.summary("gemini:gemini-4-hydrogen")
    W.run(fresh, call, limit=30.0)
    assert transport.HEALTH.summary("gemini:gemini-4-hydrogen")[0] == before[0], "no failure in its record"
    assert "gemini:gemini-4-hydrogen" not in fresh._busy_until
    rested = [k for k in W.KEYS14 if fresh.engine.healthy_count("gemini:gemini-4-hydrogen", [k]) == 0]
    assert rested == [], "the identical-payload refusal rests no key"


def test_a_storm_of_plain_429s_is_paced_not_hammered(fresh):
    """Every key of every model 429 for 6 s, then the provider is back. The first eight refusals go at once (the
    speed of every version), then at most about one ask a second - not dozens."""
    carousel = R.carousel_mod
    call = Google(gap=0.0, plain_429_s=6.0)
    out, err, stuck, took = W.run(fresh, call, limit=40.0)
    in_storm = [s for s in call.sent if s[0] < 6.0]
    assert not stuck, f"still going after {took:.0f}s, {len(call.sent)} requests"
    assert not err or isinstance(err[0], transport.KamePoolResting), err
    assert len(in_storm) <= 16, f"{len(in_storm)} requests in 6 s of 429s: {in_storm}"


def test_two_refusals_then_an_answer_is_as_fast_as_before(fresh):
    """The common case: a key or two throttled, the next answers. No pause at all."""
    call = Google(gap=0.0, plain_429_s=0.0)
    seq = iter([True, True])

    real = call.client_for

    def first_two_refused(key, attempt):
        client = real(key, attempt)
        create = client.chat.completions.create

        def wrapped(**request):
            if next(seq, False):
                call.sent.append((time.monotonic() - call.t0, request.get("model"), key))
                raise P.Refusal(429, "Resource has been exhausted (e.g. check quota).")
            return create(**request)
        client.chat.completions.create = wrapped
        return client
    call.client_for = first_two_refused
    out, err, stuck, took = W.run(fresh, call, limit=20.0)
    assert not err and not stuck
    assert R.shown(out).endswith("here")
    assert took < 2.0, f"{took:.1f}s for two refusals and an answer"


# --- the pace itself ------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_payload_is_the_messages_not_the_model_or_the_key():
    a = {"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "oi"}], "stream": True}
    b = {"model": "gemini-3.5-flash", "messages": [{"role": "user", "content": "oi"}], "timeout": 30}
    c = {"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "ola"}]}
    assert pace_mod.fingerprint(a) == pace_mod.fingerprint(b) != pace_mod.fingerprint(c)
    assert pace_mod.fingerprint({"model": "x"}) == "" and pace_mod.fingerprint(None) == ""


def test_the_hold_grows_and_two_legs_at_once_are_one_step():
    clock = Clock()
    pace = pace_mod.PayloadPace(clock)
    assert pace.note_identical("p") == 2.0
    clock.t += 0.3
    assert pace.note_identical("p") == pytest.approx(1.7), "a second leg refused at the same instant: same hold"
    clock.t += 2.0
    assert pace.wait_s("p") == 0.0
    assert pace.note_identical("p") == 4.0
    for _ in range(10):
        clock.t += 40.0
        pace.note_identical("p")
    assert pace.held_s("p") <= 30.0
    pace.note_answered("p")
    assert pace.wait_s("p") == 0.0 and pace.note_identical("p") == 2.0, "an answer starts the ladder again"


def test_the_streak_gap_spares_the_first_eight_refusals():
    clock = Clock()
    pace = pace_mod.PayloadPace(clock)
    for n in range(8):
        pace.note_sent("p")
        pace.note_refusal("p", "rate_limit")
        assert pace.wait_s("p") == (0.0 if n < 7 else 1.0)
    pace.note_refusal("p", "timeout")
    pace.note_refusal("p", "server")
    assert pace.streak("p") == 8, "a timeout or a 503 is not a throttle of the payload"
    for _ in range(24):
        pace.note_refusal("p", "per_minute")
    assert pace.wait_s("p") == 4.0
    clock.t += 5.0
    assert pace.wait_s("p") == 0.0, "a refusal slower than the gap is never slowed"


def test_the_words_google_used():
    assert pace_mod.is_identical_refusal("Gemini HTTP 429 (too_many_requests): " + IDENTICAL)
    assert not pace_mod.is_identical_refusal("Resource has been exhausted (e.g. check quota).")
    assert not pace_mod.is_identical_refusal(None)
