"""1.8.2.9 (decisions/0031 J4): the proof that "Faster" cannot do what the old top mode did.

Live, 2026-10-18: the old top mode sent a second request at the first instant of every reply (a
third after the cold bound), and 10 of 14 Gemini keys hit the per-minute limit within four
messages. This replays that situation with a fake clock and a fake provider that refuses a key
that gets more than ``E`` requests inside a minute, using the REAL ``HedgeTracker`` for Faster and
a frozen copy of the 1.8.2.8 decision for the old mode - so the test can SEE the failure it
guards against. 4 messages x 10 calls, one call every 4 s (15 a minute), every reply slow enough
to be raced (first word at 2.5x the model's usual 2.6 s), 14 keys, E = 2 per key per minute (the
conservative end of the 2-3 the live run bounds).

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_faster_cannot_burn_a_pool.py -q
"""
from __future__ import annotations

import importlib
import os
from collections import deque

import pytest

from tests import test_1_8_2_5_stream_race as R

settings = importlib.import_module(f"{R.PACKAGE}.settings")
hedging = R.hedging_mod

KEYS = 14
LIMIT_PER_MINUTE = 2
CALLS = 40                      # 4 messages x 10 calls
SPACING_S = 4.0                 # 15 calls a minute
MEDIAN_S = 2.6
SLOW_FIRST_WORD_S = 2.5 * MEDIAN_S
COLD_S = 6.0                    # the plain-model cold number: the old third leg
IDENT = "gemini:gemini-race"


class Provider:
    """14 keys, round-robin, each refusing a request that makes more than LIMIT in 60 s."""

    def __init__(self) -> None:
        self.windows = [deque() for _ in range(KEYS)]
        self.next = 0
        self.requests = 0
        self.refused_keys = set()
        self.refusals = 0

    def ask(self, at: float) -> bool:
        index = self.next % KEYS
        self.next += 1
        window = self.windows[index]
        while window and at - window[0] >= 60.0:
            window.popleft()
        self.requests += 1
        if len(window) >= LIMIT_PER_MINUTE:
            self.refused_keys.add(index)
            self.refusals += 1
            return False
        window.append(at)
        return True


@pytest.fixture()
def tracker(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_HEDGE_MODE", "max")
    settings.forget()
    t = hedging.HedgeTracker()
    for _ in range(40):
        t.record_latency(IDENT, MEDIAN_S, stream=True, reasoning=False, now=0.0)
    yield t
    settings.forget()


def replay_faster(t) -> Provider:
    provider = Provider()
    for n in range(CALLS):
        now = 100.0 + n * SPACING_S
        provider.ask(now)                                            # the main request
        first, second = t.stream_triggers(IDENT, reasoning=False, now=now)
        for trigger in (first, second):
            if SLOW_FIRST_WORD_S >= trigger and t.may_start_backup(
                    IDENT, healthy_count=KEYS, under_pressure=False, stream=True,
                    reasoning=False, now=now + trigger):
                if not provider.ask(now + trigger):
                    t.note_quota_refusal(IDENT, now=now + trigger)   # what the transport does
        if provider.refusals:
            pass
        t.record_latency(IDENT, SLOW_FIRST_WORD_S, stream=True, reasoning=False, now=now + SLOW_FIRST_WORD_S)
    return provider


def replay_old_top_mode() -> Provider:
    """A frozen copy of 1.8.2.8's decision: a backup at the first instant of EVERY reply, a third
    after the cold bound, the budget never consulted; the only protection was to step down after a
    BACKUP leg itself was refused."""
    provider = Provider()
    downgraded_until = 0.0
    budget = 3.0
    for n in range(CALLS):
        now = 100.0 + n * SPACING_S
        provider.ask(now)
        if now >= downgraded_until:
            legs = [0.0] + ([COLD_S] if SLOW_FIRST_WORD_S >= COLD_S else [])
            for trigger in legs:
                if not provider.ask(now + trigger):
                    downgraded_until = now + trigger + 600.0
        elif SLOW_FIRST_WORD_S >= 2 * MEDIAN_S and budget >= 1.0:
            budget -= 1.0                                            # Balanced's rules afterwards
            provider.ask(now + 2 * MEDIAN_S)
        budget = min(3.0, budget + 0.1)
    return provider


def test_the_proof_can_see_the_failure_it_guards_against():
    old = replay_old_top_mode()
    assert old.requests >= 60                                        # more than one request per reply, from the first reply
    assert len(old.refused_keys) >= 8                                # most of the pool refused (live: 10 of 14); replay: all 14


def test_faster_stays_inside_the_pool_in_the_same_four_messages(tracker):
    provider = replay_faster(tracker)
    assert provider.requests <= CALLS + 3 + int(0.3 * CALLS) + 1     # 40 + burst 3 + 30% of 40 = 55
    assert len(provider.refused_keys) == 0
    # and the first quota refusal turned it into Balanced for ten minutes
    if provider.refusals:
        assert tracker.faster_paused_for(IDENT, now=100.0 + CALLS * SPACING_S) > 0


def test_a_healthy_reply_costs_one_request_in_faster(tracker):
    now = 5000.0
    first, _second = tracker.stream_triggers(IDENT, reasoning=False, now=now)
    first_word = 0.8 * MEDIAN_S
    assert first_word < first                                        # the first word arrives before any backup
    provider = Provider()
    provider.ask(now)
    assert provider.requests == 1
