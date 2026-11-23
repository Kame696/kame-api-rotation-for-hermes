"""1.9.0.6 (decisions/0037 C9) - repeated timeouts on a chat reply never go back to Hermes.

A fake clock: ten minutes of a provider that times out on every key, then answers.
"""
from __future__ import annotations

import time as _real_time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_7_timeouts as base
from tests.h1906 import FakeTime, clock  # noqa: F401
from tests import test_1_9_0_6_handover_budget as HB          # noqa: F401
from tests import test_1_8_2_5_stream_race as R

transport, carousel_mod, settings = base.transport, base.carousel_mod, base.settings
KEYS, IDENTITY = base.KEYS, base.IDENTITY


class Storm:
    """Every request times out until ``until`` seconds of fake time have passed, then answers."""

    def __init__(self, fake, until):
        self.fake, self.until, self.start, self.asked = fake, until, fake.now, 0

    def run(self, key):
        self.asked += 1
        if self.fake.now - self.start < self.until:
            raise base.timeout(504)
        return base.answer("ok")


class StormCall(base.FakeCall):
    def __init__(self, storm, keys=KEYS):
        super().__init__(None, keys=keys)
        self.storm = storm

    def client_for(self, key, attempt):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **request: self.storm.run(key))))


def drive(t, call):
    beats, last, gaps, result = 0, t_start(), [], None
    for item in t._drive(call, stream=False):
        if item is transport._KEEPALIVE:
            now = transport.time.now
            gaps.append(now - last)
            last = now
            beats += 1
        elif isinstance(item, transport._Done):
            result = item.value
    return result, gaps


def t_start():
    return transport.time.now


def test_ten_minutes_of_timeouts_end_in_an_answer_and_no_exception_reaches_hermes(clock):
    t, fake = clock
    storm = Storm(fake, 600.0)
    result, gaps = drive(t, StormCall(storm))
    assert result.choices[0].message.content == "ok"
    assert t.surfaced == 0
    assert storm.asked <= len(KEYS) * (600 // 60) + len(KEYS) * 4, storm.asked
    assert gaps and max(gaps) <= t.KEEPALIVE_S + 1.0, max(gaps)


def test_the_rest_grows_and_is_capped_at_a_minute(clock):
    t, fake = clock
    rests = []
    real_mark = t.engine.mark

    def spy(identity, key, ok, delay=0.0, kind="", *a, **k):
        if kind == "timeout" and delay:
            rests.append(delay)
        return real_mark(identity, key, ok, delay, kind, *a, **k)

    t.engine.mark = spy
    drive(t, StormCall(Storm(fake, 400.0)))
    firsts = sorted(set(rests))
    assert firsts[0] == 5.0 and firsts[-1] == 60.0 and len(firsts) >= 3, firsts      # grows, capped at a minute


def test_one_key_one_model_no_list_still_never_raises(clock):
    t, fake = clock
    storm = Storm(fake, 300.0)
    result, _ = drive(t, StormCall(storm, keys=KEYS[:1]))
    assert result.choices[0].message.content == "ok" and t.surfaced == 0


def test_a_background_job_still_raises_on_the_second_timeout(clock):
    t, fake = clock
    call = StormCall(Storm(fake, 600.0))
    call.channel = "secondary"
    with pytest.raises(Exception):
        drive(t, call)


def test_the_users_own_total_wait_bound_is_the_only_raise_left(clock, monkeypatch):
    t, fake = clock
    monkeypatch.setenv("KAME_MAX_TOTAL_WAIT", "120")
    settings.forget()
    with pytest.raises(Exception):
        drive(t, StormCall(Storm(fake, 3600.0)))
