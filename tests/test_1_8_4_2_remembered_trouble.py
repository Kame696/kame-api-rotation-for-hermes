"""1.8.4.2 (S1b-S1d) - a model that keeps failing does not lead the reply.

The first reply after a pause used to start on the best model even when that model had failed every time
for days, because KAME's busy windows live in memory for minutes. Now the record of recent judged outcomes
(``core/model_health``) says so: such a reply starts on the next model of the user's list and the failing
model is asked BESIDE it as a start-probe leg, exactly as inside a busy window.

Fixtures are those of the start-probe and storm-tail tests.
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_9_start_probe as S
from tests.test_1_8_2_9_start_probe import PRIMARY, SECOND, Scripted, fresh, pool  # noqa: F401  (fixture by name)

R = S.R
transport = R.transport
settings = S.settings
THIRD = "gemini:gemini-race-three"
EVENTS = __import__(f"{R.PACKAGE}.core.events", fromlist=["x"]).EVENTS


def sicken(identity, failures=2, ago=3600.0):
    """The record says ``identity`` failed ``failures`` times, the latest ``ago`` seconds ago."""
    now = time.time()
    for n in range(failures):
        transport.HEALTH.note(identity, False, now=now - ago - (failures - n))


@pytest.fixture()
def legs(monkeypatch):
    """Every leg that is started, with the kind it was given (None for the reply's own legs)."""
    seen = []
    original = transport._StreamLeg.start

    def start(self, wire, request):
        seen.append((request.get("model"), self.race_kind))
        return original(self, wire, request)

    monkeypatch.setattr(transport._StreamLeg, "start", start)
    return seen


# (a) --------------------------------------------------------------------------------------------------

def test_a_failing_best_model_does_not_lead_the_first_reply_and_is_asked_beside(fresh, legs):
    sicken(PRIMARY)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked[0] == "gemini-race-two", "the first request goes to the next model"
    assert "gemini-race" in call.models_asked
    assert abs(call.asked_at["gemini-race"] - call.asked_at["gemini-race-two"]) < 0.15, "both go out at once"
    assert ("gemini-race", "probe") in legs, legs
    assert fresh.handed_over == 0


# (b) --------------------------------------------------------------------------------------------------

def test_a_model_not_remembered_as_failing_leads_alone(fresh, legs):
    transport.HEALTH.note(PRIMARY, False, now=time.time() - 100)       # one failure is not enough
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"]
    assert not [x for x in legs if x[1] == "probe"]


def test_two_failures_then_an_answer_lead_alone(fresh):
    sicken(PRIMARY)
    transport.HEALTH.note(PRIMARY, True, now=time.time() - 10)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    list(fresh.stream(call))
    assert call.models_asked == ["gemini-race"]


# (c) --------------------------------------------------------------------------------------------------

def test_the_probe_not_due_sends_nothing_to_the_failing_model(fresh):
    sicken(PRIMARY)
    fresh._probed_at[PRIMARY] = time.time() - 5.0            # asked 5 s ago: not due for 20 s
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked == ["gemini-race-two"], "zero requests on the failing model"


# (d) --------------------------------------------------------------------------------------------------

def test_a_background_job_is_not_moved_by_the_record(fresh, legs):
    """A background job never gets the probe and, in 1.8.4.1, never a stand-in either (`_maybe_hand_over`
    returns None for a secondary call): remembered trouble leaves it exactly where it was."""
    sicken(PRIMARY)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    call.channel = "secondary"
    assert fresh._remembered_risky(PRIMARY, call) is False
    assert fresh._start_probe_plan(call, PRIMARY, True, R.KEYS) is None
    try:
        list(fresh.stream(call))
    except Exception:
        pass
    assert call.models_asked[:1] == ["gemini-race"]
    assert not [x for x in legs if x[1] == "probe"]


# (e) --------------------------------------------------------------------------------------------------

def test_never_switch_keeps_the_best_model(fresh, monkeypatch):
    sicken(PRIMARY)
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"]


def test_ignore_my_fallback_list_keeps_the_best_model(fresh, monkeypatch):
    sicken(PRIMARY)
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    list(fresh.stream(call))
    assert call.models_asked == ["gemini-race"]


# (f) --------------------------------------------------------------------------------------------------

def test_the_target_skips_a_second_failing_model(fresh):
    sicken(PRIMARY)
    sicken(SECOND)
    call = Scripted(pool(), {"gemini-race-three": ("fast", 0.3)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three here"
    assert call.models_asked[0] == "gemini-race-three"
    assert "gemini-race" in call.models_asked


# (g) --------------------------------------------------------------------------------------------------

def test_every_model_failing_gives_todays_target(fresh):
    sicken(PRIMARY)
    sicken(SECOND)
    sicken(THIRD)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked[0] == "gemini-race-two", "never excludes: the next model, as today"


def test_the_target_preference_is_in_the_plan_and_the_stand_in(fresh):
    sicken(SECOND)
    plan = fresh._start_probe_plan(Scripted(pool(), {}), PRIMARY, True, R.KEYS)
    assert plan is not None and plan[0] == THIRD
    stand = fresh._stand_in_for(Scripted(pool(), {}), PRIMARY, ["gemini:gemini-race-two", THIRD], [], "busy", None, None)
    assert stand is not None and stand.identity == THIRD


# (h) --------------------------------------------------------------------------------------------------

def test_the_kill_switch_gives_the_start_of_1_8_4_1(fresh, monkeypatch):
    sicken(PRIMARY)
    monkeypatch.setenv("KAME_NO_REMEMBERED_TROUBLE", "1")
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"]


# (i) --------------------------------------------------------------------------------------------------

def test_one_event_line_per_risky_reply_with_the_counts(fresh):
    sicken(PRIMARY, failures=3)
    EVENTS.clear()
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    list(fresh.stream(call))
    lines = [r["reason"] for r in EVENTS.recent(50) if "failed 3 of its last 3 tries" in r["reason"]]
    assert len(lines) == 1, lines
    assert "starts on" in lines[0] and "asks it beside" in lines[0]
    assert not [r for r in EVENTS.recent(50) if "busy moments ago" in r["reason"]], \
        "the 'busy moments ago' line does not tell this story"


def test_a_risky_reply_without_a_probe_says_where_it_starts(fresh):
    sicken(PRIMARY)
    fresh._probed_at[PRIMARY] = time.time() - 5.0
    EVENTS.clear()
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(call))
    lines = [r["reason"] for r in EVENTS.recent(50) if "failed 2 of its last 2 tries" in r["reason"]]
    assert len(lines) == 1 and "asks it beside" not in lines[0], lines


# (j) --------------------------------------------------------------------------------------------------

def test_try_this_model_now_clears_the_record(fresh):
    sicken(PRIMARY)
    fresh.forget_busy("gemini:gemini-race")
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"]


# S1b: what is noted ----------------------------------------------------------------------------------

def test_an_answer_is_a_success_in_the_record(fresh):
    sicken(PRIMARY)
    fresh._busy_until.pop(PRIMARY, None)
    fresh._probed_at[PRIMARY] = time.time() - 5.0          # the probe is not due: the reply starts on the next model
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(call))
    assert transport.HEALTH.summary(SECOND)[1] == 1 and not transport.HEALTH.risky(SECOND)


def test_a_503_storm_in_a_reply_is_a_failure_once_per_call(fresh):
    call = Scripted(pool(), {"gemini-race": ("503",), "gemini-race-two": ("503",), "gemini-race-three": ("fast", 0.05)})
    try:
        list(fresh.stream(call))
    except Exception:
        pass
    fresh._drain_hedge_losers()
    failed, total = transport.HEALTH.summary(PRIMARY)
    assert failed <= 1 and failed == total, (failed, total)      # one call, at most one event
