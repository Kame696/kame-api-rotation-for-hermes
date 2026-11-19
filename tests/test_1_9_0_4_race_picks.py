"""1.9.0.4 (R2, R3) - the race asks models by their record, and the next model joins when IT usually answers.

Live 2026-11-18: hydrogen (median 22 s) was main, 3.8 (0 answers in 10 days, remembered failing) was raced
on every slow reply and 3.6 (median 5.4 s) was never reached. ``_cross_target`` now leaves a remembered
failing model out, scans the whole list and orders by what is learned, and times the first raced model by
its OWN usual first word.
"""
from __future__ import annotations

import importlib
import os
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_silent_two_models as S
from tests.test_1_8_2_9_silent_two_models import ByModel, fresh, silent_pool  # noqa: F401  (fixture by name)

transport = R.transport
settings = R.settings
chain_mod = S.chain_mod
mh = importlib.import_module(f"{R.PACKAGE}.core.model_health")

CHAIN_5 = """\
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
  - provider: gemini
    model: gemini-race-five
"""
ID = {n: f"gemini:gemini-race-{n}" for n in ("two", "three", "four", "five")}
MAIN = R.IDENTITY


@pytest.fixture()
def health(monkeypatch, tmp_path):
    h = mh.ModelHealth(directory=tmp_path / "health", calls_path=tmp_path / "absent.jsonl")
    monkeypatch.setattr(transport, "HEALTH", h)
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)
    return h


@pytest.fixture()
def five(fresh, tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(CHAIN_5, encoding="utf-8")
    chain_mod.forget()
    # the real-world constants: 12 s cold, 8 s at least, 6 s a step
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 12.0)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 8.0)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 6.0)
    return fresh


def learned(monkeypatch, table):
    """``{identity prefix: median}`` -> the tracker answers ``(median, 2 x median)`` for it, None otherwise."""
    def stats(identity, *, reasoning):
        for prefix, median in table.items():
            if identity == prefix or identity.startswith(prefix + ":"):
                return (median, 2.0 * median)
        return None
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "first_word_stats", stats)


def fail_twice(h, identity):
    now = time.time()
    h.note(identity + ":global", False, now=now - 30)
    h.note(identity + ":global", False, now=now - 20)
    assert h.risky(identity + ":global")


def picks(t, slow=60.0):
    out = t._cross_target(R.FakeCall(R.Pool({})), MAIN, False, slow)
    return [(i, round(at, 3)) for i, _m, at in (out or [])]


# --- R2: by record, not by place ------------------------------------------------------------------
def test_a_remembered_failing_model_is_left_out_and_the_next_one_races(five, health):
    fail_twice(health, ID["two"])
    assert [i for i, _ in picks(five)] == [ID["three"], ID["four"]]


def test_a_failing_model_whose_retest_is_due_may_race(five, health):
    now = time.time()
    for at in (now - 5000, now - 4900, now - 4800):
        health.note(ID["two"] + ":global", False, now=at)
    assert health.risky(ID["two"] + ":global") and health.retest_due(ID["two"] + ":global")
    assert [i for i, _ in picks(five)][0] == ID["two"]


def test_the_off_switch_keeps_the_1_9_0_3_order(five, health, monkeypatch):
    fail_twice(health, ID["two"])
    monkeypatch.setenv("KAME_NO_REMEMBERED_TROUBLE", "1")
    learned(monkeypatch, {ID["four"]: 1.0})
    assert [i for i, _ in picks(five)] == [ID["two"], ID["three"]]


def test_the_fastest_learned_model_comes_first_even_fourth_in_the_list(five, health, monkeypatch):
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 15.0, ID["five"]: 3.0})
    got = [i for i, _ in picks(five)]
    assert got == [ID["five"], ID["three"]], got


def test_unknown_models_keep_list_order_and_slower_ones_come_last(five, health, monkeypatch):
    learned(monkeypatch, {MAIN: 5.0, ID["two"]: 30.0})
    got = [i for i, _ in picks(five)]
    assert got == [ID["three"], ID["four"]], "two is learned slower than main: after the unknown ones"
    learned(monkeypatch, {MAIN: 5.0, ID["two"]: 30.0, ID["three"]: 31.0, ID["four"]: 32.0, ID["five"]: 33.0})
    assert [i for i, _ in picks(five)] == [ID["two"], ID["three"]], "all slower: list order"


def test_main_with_nothing_learned_takes_any_learned_model_first(five, health, monkeypatch):
    learned(monkeypatch, {ID["four"]: 9.0})
    assert [i for i, _ in picks(five)][0] == ID["four"]


def test_never_another_provider_or_endpoint(five, health, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: groq\n    model: llama-x\n  - provider: gemini\n    model: gemini-race-two\n",
        encoding="utf-8")
    chain_mod.forget()
    assert [i for i, _ in picks(five)] == [ID["two"]]


# --- R3: joins when it usually answers ------------------------------------------------------------
def test_balanced_joins_at_its_own_median_first_word(five, health, monkeypatch):
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 5.4})
    got = picks(five)
    assert got[0] == (ID["three"], 5.4)


def test_faster_joins_at_half_its_median(five, health, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "max")
    settings.forget()
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 5.4})
    assert picks(five)[0] == (ID["three"], 2.7)
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 1.2})
    assert picks(five)[0] == (ID["three"], 1.0), "the floor of Faster is 1 s"


def test_the_floor_in_balanced_is_two_seconds(five, health, monkeypatch):
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 0.8})
    assert picks(five)[0] == (ID["three"], 2.0)


def test_nothing_learned_about_the_candidate_keeps_todays_time(five, health, monkeypatch):
    learned(monkeypatch, {MAIN: 22.0})
    # main median 22 s: after = min(3 x 22, 0.5 x 60) = 30
    assert picks(five)[0] == (ID["two"], 30.0)
    learned(monkeypatch, {})
    assert picks(five)[0] == (ID["two"], 12.0), "nothing learned at all: the cold 12 s"


def test_a_candidate_slower_than_main_keeps_todays_time(five, health, monkeypatch):
    learned(monkeypatch, {MAIN: 5.0, ID["two"]: 35.0, ID["three"]: 36.0, ID["four"]: 37.0, ID["five"]: 38.0})
    # after = max(8, 3 x 5) = 15
    assert picks(five)[0] == (ID["two"], 15.0)


def test_never_later_than_todays_after(five, health, monkeypatch):
    # main median 1 s -> after 8 s; the candidate's own median (20 s, "faster" than a main that has none learned)
    learned(monkeypatch, {ID["two"]: 20.0})
    assert picks(five)[0] == (ID["two"], 12.0)          # min(20, cold 12)


def test_the_second_leg_steps_after_the_first_and_the_085_cut_stays(five, health, monkeypatch):
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 5.4})
    got = picks(five)
    assert got[1][1] == pytest.approx(5.4 + max(6.0, 0.5 * 5.4))
    # a bound of 12 s: the second would join at 11.4 s >= 0.85 x 12 = 10.2 -> only one leg
    assert len(picks(five, slow=12.0)) == 1


def test_saver_races_nothing(five, health, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    assert picks(five) == []


# --- end to end: the faster model's reply is the one returned --------------------------------------
def test_the_faster_listed_model_answers_first_and_its_reply_is_returned(five, health, monkeypatch):
    # fractions of a second: main thinks 30 s; three usually answers in 0.25 s; two is remembered failing
    monkeypatch.setattr(transport, "_SLOW_S", {False: (2.0, 2.0, 2.0), True: (2.0, 2.0, 2.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 1.0)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.5)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_FAST_MIN_S", 0.25)
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 0.25})
    fail_twice(health, ID["two"])
    call = ByModel(silent_pool(30.0), quick={"gemini-race-three": 0.05})
    started = time.monotonic()
    out = list(five.stream(call))
    took = time.monotonic() - started
    assert R.shown(out) == "fast answer"
    assert getattr(call, "_kame_answered_by") == ID["three"]
    assert took < 1.0, took
    assert "gemini-race-two" not in call.models_asked


# --- review: a faster model that answers inside the main model's own usual time does not make it "slow" ---------
def _tiny(monkeypatch):
    monkeypatch.setattr(transport, "_SLOW_S", {False: (2.0, 2.0, 2.0), True: (2.0, 2.0, 2.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 1.0)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.5)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_FAST_MIN_S", 0.25)


def test_beaten_inside_its_own_time_the_main_model_keeps_a_clean_record(five, health, monkeypatch):
    _tiny(monkeypatch)
    learned(monkeypatch, {MAIN: 22.0, ID["three"]: 0.25})       # main late only after 1 s (half the 2 s bound)
    call = ByModel(silent_pool(30.0), quick={"gemini-race-three": 0.05})
    out = list(five.stream(call))
    assert R.shown(out) == "fast answer"
    assert getattr(call, "_kame_standin_why") == "faster"
    assert health.summary(MAIN) == (0, 0)                     # no failure in its record
    assert not five._entry_busy(MAIN)                         # and no slow window: the next reply starts on it


def test_silent_past_its_own_late_point_the_main_model_is_still_slow(five, health, monkeypatch):
    _tiny(monkeypatch)
    learned(monkeypatch, {MAIN: 0.05, ID["three"]: 0.25})       # main late after 0.5 s; three is not faster
    call = ByModel(silent_pool(30.0), quick={"gemini-race-three": 0.2})
    out = list(five.stream(call))
    assert R.shown(out) == "fast answer"
    assert getattr(call, "_kame_standin_why") == "slow"
    assert health.summary(MAIN)[0] == 1                       # 1.9.0.3's judgement, unchanged
