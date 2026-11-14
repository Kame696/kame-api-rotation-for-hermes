"""1.8.4.3 (L12, PLAN section 8) - a model left out is looked at again; a dead best model is asked less often.

Live 2026-11-13 20:30: one outage gave the owner's fastest model (gemini-3.6-flash, eight of its last ten answered) a
latest failure; 1.8.4.2 then left it out of every start, stand-in and title and nothing ever asked it again, so every
reply went to the slowest model of the list (13 - 19 s to a first sign) for as long as the failure stayed in the
seven-day record. And the best model, failing ten in a row for five days, was asked beside every reply.
"""
from __future__ import annotations

import time

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_2_9_start_probe import PRIMARY, SECOND, Scripted, fresh, pool  # noqa: F401  (fixture by name)

transport = R.transport
mh = __import__(f"{R.PACKAGE}.core.model_health", fromlist=["x"])
THIRD = "gemini:gemini-race-three"
HOUR = 3600.0


def record(identity, outcomes, latest_ago):
    """``outcomes`` oldest first (1 answered, 0 failed); the latest one ``latest_ago`` seconds ago, one second apart."""
    now = time.time()
    n = len(outcomes)
    for i, ok in enumerate(outcomes):
        transport.HEALTH.note(identity, bool(ok), now=now - latest_ago - (n - 1 - i))


# -- the record ----------------------------------------------------------------------------------------

def test_trailing_failures_count_from_the_latest(fresh):
    record(SECOND, [1, 1, 1, 0, 1, 1, 1, 1, 1, 0], 60)
    record(THIRD, [1, 0, 0, 0], 60)
    assert transport.HEALTH.trailing_failures(SECOND) == 1
    assert transport.HEALTH.trailing_failures(THIRD) == 3
    assert transport.HEALTH.trailing_failures("gemini:never-seen") == 0


def test_a_retest_is_due_ten_minutes_after_one_failure_and_doubles_per_failure(fresh):
    now = time.time()
    record(SECOND, [1, 1, 0, 1, 0], 0)
    latest = now
    assert transport.HEALTH.risky(SECOND)
    assert not transport.HEALTH.retest_due(SECOND, now=latest + 599)
    assert transport.HEALTH.retest_due(SECOND, now=latest + 601)
    record(THIRD, [1, 0, 0], 0)                     # k = 2: twenty minutes
    assert not transport.HEALTH.retest_due(THIRD, now=latest + 1199)
    assert transport.HEALTH.retest_due(THIRD, now=latest + 1201)


def test_the_wait_is_capped_at_two_hours(fresh):
    record(SECOND, [0] * 10, 0)                     # k = 10: 600 x 512 s, capped
    now = time.time()
    assert not transport.HEALTH.retest_due(SECOND, now=now + mh.RETEST_MAX_S - 5)
    assert transport.HEALTH.retest_due(SECOND, now=now + mh.RETEST_MAX_S + 5)


def test_a_model_that_answers_is_never_due_a_retest(fresh):
    record(SECOND, [0, 0, 1], HOUR)
    assert not transport.HEALTH.risky(SECOND)
    assert not transport.HEALTH.retest_due(SECOND)
    assert not fresh._entry_risky("gemini:gemini-race-two")


# -- the choice among models that are not the one in use ----------------------------------------------

def test_the_owners_2030_record_starts_the_reply_on_a_model_that_was_left_out(fresh):
    # 3.8 / 3.6 / 3.5 of the owner become race / race-two / race-three; the record of 20:30, eight hours on
    record(PRIMARY, [0] * 10, 60)
    record(SECOND, [1, 1, 1, 0, 1, 1, 1, 1, 1, 0], 8 * HOUR)
    record(THIRD, [0, 1, 1, 1, 1, 1, 1, 1, 1, 1], 60)
    plan = fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS)
    assert plan is not None and plan[0] == SECOND, "looked at again, in the list's own order (1.8.4.2: the third)"


def test_a_fresh_failure_still_leaves_the_model_out(fresh):
    record(PRIMARY, [0, 0], HOUR)
    record(SECOND, [1, 1, 0, 0], 120)               # two minutes ago: not due (k = 2 waits 20 min)
    plan = fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS)
    assert plan is not None and plan[0] == THIRD


def test_a_reply_started_on_a_looked_at_model_that_answers_clears_it(fresh):
    record(PRIMARY, [0, 0], HOUR)
    record(SECOND, [1, 0, 0], HOUR)                 # k = 2: due after 20 min
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05), "gemini-race-three": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked[0] == "gemini-race-two"
    deadline = time.time() + 3.0
    while transport.HEALTH.risky(SECOND) and time.time() < deadline:
        time.sleep(0.05)
    assert not transport.HEALTH.risky(SECOND), "its answer is in the record"


def test_the_model_hermes_asked_for_is_still_asked_beside_never_made_to_lead(fresh):
    record(PRIMARY, [1, 0, 0], 8 * HOUR)            # its retest would be due: it does not lead anyway (S1 kept)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked[0] == "gemini-race-two"
    assert "gemini-race" in call.models_asked, "asked beside"


# -- a dead best model is asked less often -------------------------------------------------------------

def test_the_dead_probe_schedule(fresh):
    # one identity per k: a Clear pool tombstone would drop the back-dated rows of a re-used one
    for k, want in ((0, 20.0), (1, 20.0), (2, 20.0), (3, 40.0), (4, 80.0), (5, 160.0), (6, 300.0), (10, 300.0)):
        ident = f"gemini:gemini-dead-{k}"
        if k:
            record(ident, [1] + [0] * k, 60)
        assert fresh._dead_probe_every(ident) == want, k


def test_a_best_model_dead_ten_in_a_row_is_not_asked_again_within_five_minutes(fresh):
    record(PRIMARY, [0] * 10, 60)
    fresh._probed_at[PRIMARY] = time.time() - 120     # asked two minutes ago
    assert fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS) is None
    fresh._probed_at[PRIMARY] = time.time() - 301
    assert fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS) is not None


def test_inside_a_busy_window_the_probe_stays_every_twenty_seconds(fresh):
    record(PRIMARY, [0] * 10, 60)
    fresh._note_busy(PRIMARY)
    fresh._busy_until[PRIMARY] = time.time() + 5.0
    fresh._probed_at[PRIMARY] = time.time() - 25
    assert fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS) is not None


def test_the_kill_switch_turns_both_off(fresh, monkeypatch):
    record(SECOND, [0, 0], 8 * HOUR)
    monkeypatch.setenv(mh.ENV_OFF, "1")
    assert not transport.HEALTH.retest_due(SECOND)
    assert transport.HEALTH.trailing_failures(SECOND) == 0
    assert fresh._dead_probe_every(PRIMARY) == 20.0
