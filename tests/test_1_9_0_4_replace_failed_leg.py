"""1.9.0.4 (R4) - a raced leg that fails is replaced at once.

Live 2026-11-18: both raced legs (3.8, 3.7) ended with a 503 and nothing replaced them; their keys' verdicts were
written at the instant the main model answered. Now a raced-in model's leg that fails before any answer has its
key's verdict applied at once and ONE other key of that model asked in its place - not for the request's own
refusal, not after a provisional leg exists, not when no other key is ready, once per model per race.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_2_9_silent_two_models import fresh  # noqa: F401  (fixture by name)

transport = R.transport


def thinking(text="hm"):
    delta = SimpleNamespace(role=None, content=None, tool_calls=None, reasoning_content=text)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, delta=delta, finish_reason=None)])


def overloaded():
    return R.Refusal(503, "The model is overloaded. Please try again later.")


class Planned(R.FakeCall):
    """Each request of a model takes the next script of ``plans[model]``; past the plan it is silent."""

    def __init__(self, plans, silent=30.0, keys=R.KEYS):
        super().__init__(R.Pool({}), keys=keys)
        self.plans = {m: list(v) for m, v in plans.items()}
        self.silent = silent
        self.asked = []          # (model, key, seconds since the first request)
        self.t0 = None

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            now = time.monotonic()
            self.t0 = self.t0 or now
            self.asked.append((model, key, now - self.t0))
            script = self.plans[model].pop(0) if self.plans.get(model) else [self.silent, R.chunk("late", finish="stop")]
            return R.FakeStream(list(script))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def times_asked(self, model):
        return [row for row in self.asked if row[0] == model]


def quick(text="fast answer"):
    first, second = text.split(" ", 1)
    return [0.05, R.chunk(first + " "), R.chunk(second, finish="stop")]


@pytest.fixture()
def scaled(fresh, monkeypatch):      # noqa: F811
    # the next model joins at 0.3 s, the one after at 0.7 s; the slow bound is 3 s
    monkeypatch.setattr(transport, "_SLOW_S", {False: (3.0, 3.0, 3.0), True: (3.0, 3.0, 3.0)})
    rows = []
    real = transport._emit

    def spy(call, kind, **kw):
        rows.append((time.monotonic(), kind, kw.get("reason")))
        return real(call, kind, **kw)

    monkeypatch.setattr(transport, "_emit", spy)
    fresh.rows = rows
    verdicts = []
    inner = fresh._on_failure

    def on_failure(identity, key, exc, *a, **k):
        verdicts.append((time.monotonic(), identity, key))
        return inner(identity, key, exc, *a, **k)

    monkeypatch.setattr(fresh, "_on_failure", on_failure)
    fresh.verdicts = verdicts
    return fresh


def test_a_cross_leg_that_503s_is_replaced_on_another_key_and_the_replacement_wins(scaled):
    call = Planned({"gemini-race-two": [[0.2, overloaded()], quick()]})
    out = list(scaled.stream(call))
    assert R.shown(out) == "fast answer"
    asked = call.times_asked("gemini-race-two")
    assert len(asked) == 2 and asked[0][1] != asked[1][1], "another key of the same model"
    assert asked[1][2] >= asked[0][2] + 0.15, "asked after the first one failed, not at the same instant"
    assert getattr(call, "_kame_answered_by") == "gemini:gemini-race-two"
    assert any("another key of" in (reason or "") and "failed (503)" in (reason or "")
               for _t, _k, reason in scaled.rows), scaled.rows


def test_the_failed_keys_verdict_is_applied_at_the_failure_not_at_the_end(scaled):
    # the replacement answers late (0.8 s after it starts), so the end of the race is far from the failure
    slow_answer = [0.8, R.chunk("late "), R.chunk("answer", finish="stop")]
    call = Planned({"gemini-race-two": [[0.2, overloaded()], slow_answer]})
    started = time.monotonic()
    out = list(scaled.stream(call))
    ended = time.monotonic()
    assert R.shown(out) == "late answer"
    first = min(t for t, _identity, _key in scaled.verdicts)
    # the leg joined at ~0.3 s and failed ~0.2 s later; the race ended ~0.8 s after the replacement
    assert first - started < 0.9 and ended - first > 0.5, (first - started, ended - first)


def test_only_one_replacement_per_model(scaled):
    call = Planned({"gemini-race-two": [[0.1, overloaded()], [0.1, overloaded()], [0.1, overloaded()], quick()],
                    "gemini-race-three": [quick("three says")]})
    out = list(scaled.stream(call))
    assert len(call.times_asked("gemini-race-two")) == 2, "the first leg and ONE replacement, no more"
    assert R.shown(out) == "three says"


@pytest.mark.parametrize("status", [400, 404, 410])
def test_no_replacement_for_the_requests_own_refusal(scaled, status):
    call = Planned({"gemini-race-two": [[0.1, R.Refusal(status, "refused request")], quick()],
                    "gemini-race-three": [quick("three says")]})
    out = list(scaled.stream(call))
    assert len(call.times_asked("gemini-race-two")) == 1
    assert R.shown(out) == "three says"


def test_no_replacement_after_a_provisional_leg_exists(scaled):
    # main shows it is thinking at 0.5 s (the provisional leg); the raced model fails at 0.7 s; main answers at 1.4 s
    main = [0.5, thinking(), 0.9, R.chunk("main "), R.chunk("answer", finish="stop")]
    call = Planned({"gemini-race": [main], "gemini-race-two": [[0.4, overloaded()], quick()]})
    out = list(scaled.stream(call))
    assert R.shown(out) == "main answer"
    assert len(call.times_asked("gemini-race-two")) == 1


def test_no_replacement_when_no_other_key_of_that_model_is_ready(scaled):
    call = Planned({"gemini-race-two": [[0.1, overloaded()], quick()],
                    "gemini-race-three": [quick("three says")]}, keys=R.KEYS[:1])
    out = list(scaled.stream(call))
    assert len(call.times_asked("gemini-race-two")) == 1, "one key only: nothing to replace it with"
    assert R.shown(out) == "three says"


def test_the_primary_is_never_replaced(scaled):
    call = Planned({"gemini-race": [[0.1, overloaded()]], "gemini-race-two": [quick("two says")]})
    out = list(scaled.stream(call))
    assert R.shown(out) == "two says"
    assert len(call.times_asked("gemini-race")) <= 2      # the ordinary rotation, not a race replacement


def test_a_replacement_counts_as_a_race_request(scaled):
    def races():
        return scaled._speed_total[transport._SPEED_RACE]

    before = races()
    call = Planned({"gemini-race-two": [[0.2, overloaded()], quick()]})
    list(scaled.stream(call))
    assert races() - before == 2, "the raced leg and its replacement"
