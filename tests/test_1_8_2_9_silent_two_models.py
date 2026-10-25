"""1.8.2.9 / decisions/0032 B - a reply never sits behind two silent models.

Before: the best model silent, the next model raced in and silent too, the reply waited
``start of the last raced leg + a whole slow bound`` (37-111 s), then every leg was cut and Hermes was
handed the model that had just been marked slow. Now a raced model keeps the reply waiting only to its
own "late" point (never longer than 1.8.2.8), one more model of the list joins at the slow bound
instead of everything starting over, and when nothing else could answer nothing is cut.

Driven through ``KameTransport.stream`` with the stream-race fakes, constants scaled to fractions of
a second.
"""
from __future__ import annotations

import os
import random
import threading
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
settings = R.settings
chain_mod = __import__(f"{R.PACKAGE}.core.fallback_chain", fromlist=["x"])

CHAIN_3 = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: gemini
    model: gemini-race-three
  - provider: gemini
    model: gemini-race-four
"""
SILENT = 4.0


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CHAIN_3, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    # slow bound 1.0 s; the next model joins at 0.3 s and the one after at 0.7 s; "late" = 0.5 s
    monkeypatch.setattr(transport, "_SLOW_S", {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


def silent_pool(seconds=SILENT):
    return R.Pool({k: [[seconds, R.chunk("late", finish="stop")]] * 12 for k in R.KEYS})


class ByModel(R.FakeCall):
    """Each model answers as scripted: ``{model: seconds to first word}``; the others are silent."""

    def __init__(self, pool, quick=None, text="fast answer", scripts=None):
        super().__init__(pool)
        self.quick = dict(quick or {})
        self.scripts = dict(scripts or {})      # {model: raw FakeStream steps}
        self.text = text
        self.models_asked = []
        self.asked_at = {}

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            self.models_asked.append(model)
            self.asked_at.setdefault(model, time.monotonic())
            if model in self.scripts:
                return R.FakeStream(list(self.scripts[model]))
            if model in self.quick:
                first, second = self.text.split(" ", 1) if " " in self.text else (self.text, "")
                return R.FakeStream([self.quick[model], R.chunk(first + " "), R.chunk(second, finish="stop")])
            return self.pool.open(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def with_other_provider(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: gemini\n    model: gemini-race-two\n"
        "  - provider: gemini\n    model: gemini-race-three\n"
        "  - provider: groq\n    model: llama-x\n", encoding="utf-8")
    chain_mod.forget()


# --- B1: one reply clock --------------------------------------------------------------------------
def test_two_silent_models_are_not_waited_out_in_series(fresh, tmp_path):
    with_other_provider(tmp_path)
    call = ByModel(silent_pool())
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(call))
    # measured from the first request, so a cold start of the test process does not count
    took = time.monotonic() - call.asked_at["gemini-race"]
    # raced legs at 0.3 and 0.7 s, "late" 0.5 s -> the reply stops waiting at 1.2 s, not at 0.7 + 1.0 = 1.7 s
    assert 1.1 <= took < 1.6, took


def test_the_second_bound_is_never_longer_than_1_8_2_8(monkeypatch):
    rng = random.Random(11)
    for _ in range(500):
        slow = rng.uniform(3.0, 120.0)
        starts = [rng.uniform(0.0, slow) for _ in range(rng.randint(1, 3))]
        bound = transport._second_bound(slow, starts)
        assert bound <= max(starts) + slow + 1e-9, "never longer than the whole-bound rule of 1.8.2.8"
        assert bound >= slow - 1e-9, "never shorter than the model's own slow bound"
    # the owner's numbers: usual 2.6 s -> bound 20 s, raced at 8 and 14 s; a thinking model 45 s
    assert transport._second_bound(20.0, [8.0, 14.0]) == pytest.approx(24.0)
    assert transport._second_bound(45.0, [22.5, 33.8]) == pytest.approx(56.3)
    assert transport._second_bound(20.0, []) == 20.0


# --- B2: the race widens at the slow bound --------------------------------------------------------
def test_widening_adds_the_next_free_model_at_the_slow_bound(fresh):
    call = ByModel(silent_pool(), quick={"gemini-race-four": 0.05})
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "fast answer"
    assert 1.0 <= time.monotonic() - started < 3.0, "joined at about 1.2 s, not after a restart"
    assert call.models_asked == ["gemini-race", "gemini-race-two", "gemini-race-three", "gemini-race-four"]
    assert getattr(call, "_kame_answered_by") == "gemini:gemini-race-four"
    assert fresh.handed_over == 1


def test_never_more_than_three_raced_models(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        CHAIN_3 + "  - provider: gemini\n    model: gemini-race-five\n"
        "  - provider: gemini\n    model: gemini-race-six\n", encoding="utf-8")
    chain_mod.forget()
    call = ByModel(silent_pool(1.6))
    out = list(fresh.stream(call))
    assert R.shown(out) == "late"
    assert len(call.models_asked) <= 4, call.models_asked          # the primary + at most three raced models


def test_saver_never_widens(fresh, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    call = ByModel(silent_pool(1.3))
    # Saver keeps D11's hand-over at the slow bound (Hermes' own switch) but races nothing and widens nothing.
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(call))
    assert call.models_asked == ["gemini-race"]


def test_a_pool_under_quota_pressure_is_not_widened(fresh):
    for key in R.KEYS:
        fresh.engine.mark("gemini:gemini-race-four", key, False, 3600.0, "daily")
    call = ByModel(silent_pool(1.8), quick={"gemini-race-four": 0.05})
    out = list(fresh.stream(call))
    assert R.shown(out) == "late"
    assert "gemini-race-four" not in call.models_asked


def test_a_background_job_never_widens(fresh):
    call = ByModel(silent_pool(1.6), quick={"gemini-race-four": 0.05})
    call.channel = "secondary"
    out = list(fresh.stream(call))
    assert call.models_asked == ["gemini-race"] and R.shown(out) == "late"


# --- B3: nothing is cut when nothing else could answer ------------------------------------------
def test_nothing_is_cut_when_nothing_else_could_answer(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: gemini\n    model: gemini-race-two\n", encoding="utf-8")
    chain_mod.forget()
    # primary silent for 2.2 s; the one raced model is silent for 4 s. Bound 1.0 s -> past it, in 1.8.2.8,
    # every leg was cut and Hermes was handed the raced model. Now the primary is simply waited for.
    call = ByModel(silent_pool(2.2))
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "late"
    assert time.monotonic() - started >= 2.0
    assert fresh.handed_over == 0
    assert fresh._entry_busy("gemini:gemini-race-two"), "the silent raced model is left alone for a while"


def test_another_provider_still_gets_the_d11_hand_over(fresh, tmp_path):
    with_other_provider(tmp_path)
    call = ByModel(silent_pool())
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(call))
    assert fresh.handed_over == 1


# --- B4: the clock arms, and the cadence ---------------------------------------------------------
def test_all_later_known_slow_but_an_earlier_model_free_still_gets_a_clock(fresh):
    fresh._note_busy("gemini:gemini-race-three:global")
    fresh._note_busy("gemini:gemini-race-four:global")
    call = R.FakeCall(silent_pool())
    call.identity = "gemini:gemini-race-two:global"
    call.kwargs["model"] = "gemini-race-two"
    assert fresh._slow_after(call, "gemini:gemini-race-two:global", False) is not None


def test_a_known_slow_model_with_nothing_free_has_no_clock_as_before(fresh):
    for name in ("two", "three", "four"):
        fresh._note_busy(f"gemini:gemini-race-{name}:global")
    assert fresh._slow_after(R.FakeCall(silent_pool()), R.IDENTITY, False) is None


def test_a_busy_model_is_raced_only_when_it_is_due(fresh):
    fresh._note_busy("gemini:gemini-race-two:global")
    first = fresh._cross_target(R.FakeCall(silent_pool()), R.IDENTITY, False, 30.0)
    assert [t[0] for t in first] == ["gemini:gemini-race-three", "gemini:gemini-race-four"], "two is busy, not due"
    fresh._probed_at["gemini:gemini-race-two"] = time.time() - 60
    fresh._probed_at["gemini:gemini-race-two:global"] = time.time() - 60
    again = fresh._cross_target(R.FakeCall(silent_pool()), R.IDENTITY, False, 30.0)
    assert [t[0] for t in again][0] == "gemini:gemini-race-two", "two is busy but due: asked again"


def test_a_leg_started_on_a_busy_model_claims_its_probe_time(fresh):
    fresh._note_busy("gemini:gemini-race-two:global")
    fresh._probed_at["gemini:gemini-race-two:global"] = time.time() - 60
    assert fresh._entry_due("gemini:gemini-race-two")
    assert fresh._claim_probe("gemini:gemini-race-two:global")      # what starting a leg on it does
    assert fresh._probed_at["gemini:gemini-race-two"] > time.time() - 5
    assert not fresh._entry_due("gemini:gemini-race-two"), "asked now, so not due for 20 s"
    assert not fresh._entry_free("gemini:gemini-race-two")
    assert not fresh._claim_probe("gemini:gemini-race-two:global"), "a second claim in the same window fails"
    assert fresh._claim_probe("gemini:gemini-race-three:global")    # not busy: always allowed
    assert "gemini:gemini-race-three" not in fresh._probed_at


def test_two_conversations_claim_a_due_busy_model_once(fresh):
    fresh._note_busy("gemini:gemini-race-two:global")
    fresh._probed_at["gemini:gemini-race-two:global"] = time.time() - 60
    wins = []
    threads = [threading.Thread(target=lambda: wins.append(fresh._claim_probe("gemini:gemini-race-two:global")))
               for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert wins.count(True) == 1, wins


def test_a_due_busy_model_that_wins_the_race_answers_for_the_reply(fresh):
    fresh._note_busy("gemini:gemini-race-two:global")
    fresh._probed_at["gemini:gemini-race-two:global"] = time.time() - 60
    call = ByModel(silent_pool(1.0), quick={"gemini-race-two": 0.05})
    out = list(fresh.stream(call))
    assert R.shown(out) == "fast answer" and getattr(call, "_kame_answered_by") == "gemini:gemini-race-two"


# --- B5: what the panel is told ---------------------------------------------------------------------
def test_the_activity_lists_every_raced_model(fresh, monkeypatch):
    seen = []
    monkeypatch.setattr(transport, "_publish", lambda binding, activity=None: seen.append(activity))
    call = ByModel(silent_pool(1.6))
    list(fresh.stream(call))
    racing = [a for a in seen if a and a.get("racing")]
    assert racing, "a raced model is announced"
    assert max(len(a["racing"]) for a in racing) >= 2, "both raced models are listed"


# --- review of 2026-10-24 (notes/2026-10-24-review-1829-late-build.md) ----------------------------------
def only_two(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: gemini\n    model: gemini-race-two\n"
        "  - provider: groq\n    model: llama-x\n", encoding="utf-8")
    chain_mod.forget()


def test_a_failed_raced_leg_does_not_hold_the_reply(fresh, tmp_path):
    # Finding 1: a raced model whose request already FAILED is not in the race. The primary is silent;
    # Hermes can still be handed to that model (and to the groq tail), as in 1.8.2.8.
    only_two(tmp_path)
    call = ByModel(silent_pool(3.0), scripts={"gemini-race-two": [0.02, R.per_minute()]})
    started = time.monotonic()
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(call))
    assert time.monotonic() - started < 1.9, "handed over at the slow bound, not held for the primary"


def test_a_raced_leg_that_ended_empty_does_not_hold_the_reply(fresh, tmp_path):
    only_two(tmp_path)
    call = ByModel(silent_pool(3.0), scripts={"gemini-race-two": [0.02, R.chunk(finish="stop")]})
    started = time.monotonic()
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(call))
    assert time.monotonic() - started < 1.9


def test_b3_then_a_late_winner_notes_each_model_once(fresh, tmp_path):
    # Finding 2: no other provider, so B3 notes every silent model slow and waits; the next model then
    # answers. Each model is noted ONCE: one slow window, not two doubled.
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: gemini\n    model: gemini-race-two\n", encoding="utf-8")
    chain_mod.forget()
    call = ByModel(silent_pool(4.0),
                   scripts={"gemini-race-two": [1.3, R.chunk("fast "), R.chunk("answer", finish="stop")]})
    out = list(fresh.stream(call))
    assert R.shown(out) == "fast answer"
    assert fresh._busy_window[R.IDENTITY] == transport._SLOW_FIRST_S, fresh._busy_window
    assert fresh._busy_kind[R.IDENTITY] == "slow"


def test_a_model_that_refused_this_request_is_never_widened_in(fresh):
    call = ByModel(silent_pool(1.8))
    taken = [R.IDENTITY, "gemini:gemini-race-two", "gemini:gemini-race-three"]
    found = fresh._widen_candidate(call, R.IDENTITY, R.KEYS, taken)
    assert found is not None and found[0] == "gemini:gemini-race-four"
    call._kame_avoid = ["gemini:gemini-race-four"]      # what _drive keeps: models that refused the shape
    assert fresh._widen_candidate(call, R.IDENTITY, R.KEYS, taken) is None
