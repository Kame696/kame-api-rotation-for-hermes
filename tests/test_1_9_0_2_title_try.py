"""1.9.0.2 (H10) - one silent key no longer spends a chat title's whole budget.

Live 2026-11-15 16:53 (installed 98cb154e700c, after H9): the title moved to healthy gemini-3.6-flash and was
sent; Google left that key silent and the one attempt's read timeout was the whole 30 s the job had - "timed
out after 1 tries in 30s" - so the 2 other requests the job may send never went out, the title was lost and
Hermes then held Gemini out of every background job for ten minutes. The chat on the same model had its first
word in 6 s that minute.

Rule (owner, 2026-11-15: fixed in 1.9.0.2, for any user and any provider): a COSMETIC attempt that is not the
job's last allowed request gets ``max(10 s, 3 x this model's own median first word)``, never more than the time
left. A fast model (median 1-3 s) gets 10 s, so a silent key leaves room for another; a slow or thinking model
(median >= 10 s) gets the whole time left, as before; a model KAME has no first-word history for gets the whole
time left, as before (never cut on a guess). Important jobs (compression, memory) are untouched.
"""
from __future__ import annotations

import importlib

import pytest

from tests import test_1_8_2_9_aux_budget as B
from tests.test_1_8_2_9_aux_budget import fresh  # noqa: F401  (fixture by name)

aux_budget = importlib.import_module(f"{B.PACKAGE}.core.aux_budget")
hedging = importlib.import_module(f"{B.PACKAGE}.core.hedging")


# -- the rule itself ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("median,expected", [(1.0, 10.0), (2.0, 10.0), (5.0, 15.0), (12.0, 36.0)])
def test_a_cosmetic_try_gets_three_times_the_models_first_word_at_least_ten_seconds(median, expected):
    assert aux_budget.try_cap("cosmetic", 1, 3, median) == expected


def test_a_model_with_no_first_word_history_is_not_cut():
    assert aux_budget.try_cap("cosmetic", 1, 3, None) is None


def test_the_last_allowed_request_gets_what_is_left():
    assert aux_budget.try_cap("cosmetic", 3, 3, 1.0) is None


def test_an_important_job_is_never_cut():
    assert aux_budget.try_cap("important", 1, 6, 1.0) is None


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), True, "2"])
def test_a_median_that_is_not_a_positive_number_is_no_history(bad):
    assert aux_budget.try_cap("cosmetic", 1, 3, bad) is None


# -- through the transport ----------------------------------------------------------------------------------

class SilentFirst(B.Plan):
    """The first key asked is silent until the request's own timeout; every later key answers."""

    def run(self, key, kwargs):
        self.asked.append(key)
        self.kwargs_seen.append(dict(kwargs))
        if len(self.asked) == 1:
            self.clock.now += float(kwargs.get("timeout") or 30.0)
            raise TimeoutError("The read operation timed out")
        return B.answer("A title")


def learn(seconds):
    for ident in (B.IDENTITY, B.IDENTITY + ":global"):
        hedging.HEDGE_TRACKER.seed_stream(ident, [seconds] * 25, reasoning=False)   # _MIN_SAMPLES is 20


@pytest.fixture()
def clean_tracker():
    tracker = hedging.HEDGE_TRACKER
    saved = {k: list(v) for k, v in getattr(tracker, "_samples", {}).items()}
    yield tracker
    tracker._samples.clear()
    tracker._samples.update(saved)


def test_a_fast_models_title_survives_one_silent_key(fresh, clean_tracker):
    learn(1.5)
    plan = SilentFirst([], fresh.clock)
    started = fresh.clock.now
    out = fresh.complete(B.title(plan, timeout=30))
    assert out.choices[0].message.content == "A title"
    assert plan.kwargs_seen[0]["timeout"] == pytest.approx(10.0)
    assert len(plan.asked) == 2 and plan.asked[0] != plan.asked[1], "a second key, after 10 s"
    assert fresh.clock.now - started < 30.0


def test_a_slow_models_title_keeps_the_whole_time(fresh, clean_tracker):
    learn(12.0)
    plan = SilentFirst([], fresh.clock)
    with pytest.raises((B.transport.KameAuxTimeout, B.transport.KameAuxStopped)):
        fresh.complete(B.title(plan, timeout=30))
    assert plan.kwargs_seen[0]["timeout"] == pytest.approx(30.0), "as before H10: one try, all 30 s"


def test_a_model_kame_never_timed_keeps_the_whole_time(fresh, clean_tracker):
    clean_tracker._samples.clear()
    plan = SilentFirst([], fresh.clock)
    with pytest.raises((B.transport.KameAuxTimeout, B.transport.KameAuxStopped)):
        fresh.complete(B.title(plan, timeout=30))
    assert plan.kwargs_seen[0]["timeout"] == pytest.approx(30.0)


def test_an_important_job_keeps_the_hosts_timeout(fresh, clean_tracker):
    learn(1.5)
    plan = B.Plan(["summary"], fresh.clock)
    fresh.complete(B.compression(plan, timeout=120))
    assert plan.kwargs_seen[0]["timeout"] == pytest.approx(120.0)
