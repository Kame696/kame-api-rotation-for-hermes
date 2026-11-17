"""1.9.0.2 (H11) - a model due a second look is asked BESIDE the reply, never in front of it.

Live 2026-11-15 20:10 (installed 41b74c2193cb): the best model failed 10 of 10, the second failed too but its
L12 retest was due, the third was healthy. The reply STARTED on the second (a retest-due model counted as not
failing), the best was asked beside it, both stayed silent, and the healthy third only joined at the 12 s cross
bound: 14.3 s to a first word that the third gives in about two.

Owner, 2026-11-15: fix it before the release. Rule: a reply never starts on a model remembered as failing while
one that is not can start it; the retest-due model is asked beside the reply from its first instant (its judged
probe), so it is still looked at again and, if it answers first, the reply is its own and its record is cleared.
"""
from __future__ import annotations

import time

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_2_9_start_probe import PRIMARY, SECOND, Scripted, fresh, pool  # noqa: F401  (fixture by name)
from tests.test_1_8_4_3_retest import THIRD, HOUR, record

transport = R.transport
settings = R.settings


def dead_probe_not_due(t):
    """The best model's own probe was just sent: this reply goes the hand-over way (no start plan)."""
    t._probed_at[PRIMARY] = time.time()


# -- where the reply starts -------------------------------------------------------------------------------

def test_the_start_plan_takes_the_healthy_model_not_the_one_due_a_retest(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 1, 1, 0, 1, 1, 1, 1, 1, 0], 8 * HOUR)    # failing, retest due
    record(THIRD, [0, 1, 1, 1, 1, 1, 1, 1, 1, 1], 60)           # healthy
    plan = fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS)
    assert plan is not None and plan[0] == THIRD


def test_with_no_healthy_model_the_one_due_a_retest_still_starts(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)                         # retest due
    record(THIRD, [1, 0, 0], 60)                                 # failing, not due
    plan = fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS)
    assert plan is not None and plan[0] == SECOND, "nothing is excluded: the best of what is left"


def test_the_stand_in_takes_the_healthy_model_too(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    dead_probe_not_due(fresh)
    call = Scripted(pool(), {"gemini-race-three": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three here"
    assert call.models_asked[0] == "gemini-race-three", call.models_asked


# -- the second look happens beside, from the first instant ----------------------------------------------

def test_the_model_due_a_retest_is_asked_beside_at_once(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    dead_probe_not_due(fresh)
    call = Scripted(pool(), {"gemini-race-three": ("fast", 0.4)})     # the second stays silent
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three here"
    assert "gemini-race-two" in call.models_asked, "looked at again"
    assert call.asked_at["gemini-race-two"] - started < 0.5, "beside the reply from its first instant"
    assert time.monotonic() - started < 2.0, "nobody waited for it"


def test_a_retested_model_that_answers_first_takes_the_reply_and_is_cleared(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    dead_probe_not_due(fresh)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05), "gemini-race-three": ("fast", 0.8)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    deadline = time.time() + 3.0
    while transport.HEALTH.risky(SECOND) and time.time() < deadline:
        time.sleep(0.05)
    assert not transport.HEALTH.risky(SECOND), "its answer is in the record: the next reply starts on it"
    plan_target = fresh._stand_in_for(Scripted(pool(), {}), PRIMARY, [SECOND, THIRD], [], "busy", None, 0.0)
    assert plan_target is not None and plan_target.identity.startswith(SECOND)


def test_a_second_look_that_fails_counts_and_the_next_waits_longer(fresh):
    # (an unscripted model of this fixture answers "late" after 3 s: heard, that one is cleared - right. Here the
    # second look is refused, as the owner's 3.7 was.)
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    dead_probe_not_due(fresh)
    call = Scripted(pool(), {"gemini-race-three": ("fast", 0.2), "gemini-race-two": ("503",)})
    list(fresh.stream(call))
    assert "gemini-race-two" in call.models_asked
    deadline = time.time() + 10.0
    while transport.HEALTH.retest_due(SECOND) and time.time() < deadline:
        fresh._drain_probe_verdicts()
        time.sleep(0.2)
    assert transport.HEALTH.risky(SECOND) and not transport.HEALTH.retest_due(SECOND), "its failure counted"


def test_a_second_look_heard_late_still_clears_the_model(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    dead_probe_not_due(fresh)
    list(fresh.stream(Scripted(pool(), {"gemini-race-three": ("fast", 0.2)})))   # the second answers at 3 s
    deadline = time.time() + 10.0
    while transport.HEALTH.risky(SECOND) and time.time() < deadline:
        fresh._drain_probe_verdicts()
        time.sleep(0.2)
    assert not transport.HEALTH.risky(SECOND), "a judged probe is heard to its first word, after the reply too"


def test_a_fresh_failure_is_not_asked_beside(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 120)                               # two minutes ago: not due
    record(THIRD, [1, 1, 1], 60)
    dead_probe_not_due(fresh)
    call = Scripted(pool(), {"gemini-race-three": ("fast", 0.05)})
    list(fresh.stream(call))
    assert "gemini-race-two" not in call.models_asked


def test_the_best_models_own_probe_and_the_retest_go_beside_together(fresh):
    # One slot would let a dead best model, looked at on every reply, keep a recovered middle model out for good.
    record(PRIMARY, [0] * 10, 60)                                # its dead probe is due (never asked)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    call = Scripted(pool(), {"gemini-race-three": ("fast", 0.3)})
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three here"
    assert call.models_asked[0] == "gemini-race-three", "the reply starts on the healthy model"
    assert "gemini-race" in call.models_asked, "the best model keeps its own probe"
    assert "gemini-race-two" in call.models_asked, "and the retest goes beside it"
    assert call.asked_at["gemini-race"] - started < 0.5
    assert call.asked_at["gemini-race-two"] - started < 0.5
    assert time.monotonic() - started < 2.0, "nobody waited for either"


def test_with_both_beside_the_recovered_middle_model_answers_and_takes_the_reply(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05), "gemini-race-three": ("fast", 0.8)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    deadline = time.time() + 3.0
    while transport.HEALTH.risky(SECOND) and time.time() < deadline:
        time.sleep(0.05)
    assert not transport.HEALTH.risky(SECOND)
    assert getattr(call, "_kame_probe_id", None) is None and getattr(call, "_kame_retest_ids", None) is None


def test_two_replies_close_together_look_once(fresh):
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    dead_probe_not_due(fresh)
    first = Scripted(pool(), {"gemini-race-three": ("fast", 0.05), "gemini-race-two": ("503",)})
    list(fresh.stream(first))
    dead_probe_not_due(fresh)
    second = Scripted(pool(), {"gemini-race-three": ("fast", 0.05)})
    list(fresh.stream(second))
    assert "gemini-race-two" in first.models_asked
    assert "gemini-race-two" not in second.models_asked


def test_never_switch_asks_nothing_beside(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    record(PRIMARY, [1, 1, 1], 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    list(fresh.stream(call))
    assert call.models_asked == ["gemini-race"]


def test_a_healthy_best_model_needs_no_second_look_at_anything(fresh):
    record(PRIMARY, [1, 1, 1], 60)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    list(fresh.stream(call))
    assert call.models_asked == ["gemini-race"], "only models ranked above the one that starts are looked at"


def test_when_the_second_look_wins_the_best_models_own_probe_is_left_to_its_judge(fresh):
    # both judged probes start at the same instant; the one that lost is not "slow" - its judge decides
    # (the record keeps ten outcomes: five failures, so a sixth would show)
    record(PRIMARY, [1, 0, 0, 0, 0, 0], 60)                      # its dead probe is due; it answers late (3 s)
    record(SECOND, [1, 0, 0], 8 * HOUR)
    record(THIRD, [1, 1, 1], 60)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05), "gemini-race-three": ("fast", 0.8)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert "gemini-race" in call.models_asked
    assert transport.HEALTH.trailing_failures(PRIMARY) == 5, "no failure added the moment the other probe won"
    assert fresh._busy_until.get(PRIMARY, 0.0) <= time.time(), "and no busy window opened for it"
    deadline = time.time() + 10.0
    while transport.HEALTH.risky(PRIMARY) and time.time() < deadline:
        fresh._drain_probe_verdicts()
        time.sleep(0.2)
    assert not transport.HEALTH.risky(PRIMARY), "heard late: the best model is back"
