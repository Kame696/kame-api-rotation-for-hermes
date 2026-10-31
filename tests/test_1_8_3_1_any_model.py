"""1.8.3.1 (PLAN_1.8.3.1 D1, H2 + H3) - the fallback list serves the model in use, whichever it is.

A reply that STARTS on model X (the main model, a list model picked by hand, a model nobody listed) walks
``ladder(X)``: X, then the main model and the list in the user's order. Every place that used "the
entries after the origin" uses "the entries after the CURRENT position in ladder(origin)". A walk Hermes
makes inside one reply keeps the reply's ORIGIN (the permit carries it). Background jobs, "Never" and the
flag "ignore my fallback list" still forbid every switch.
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_8_3_0_sibling_standin as S

transport = R.transport
settings = R.settings
chain_mod = P.chain_mod
pkg = __import__("tests.test_1_8_2_7_handover", fromlist=["pkg"]).pkg

fresh = S.fresh
notices = S.notices
rest_all = S.rest_all
scripted = S.scripted

MAIN = "gemini:gemini-race"
TWO = "gemini:gemini-race-two"
THREE = "gemini:gemini-race-three"
GROQ = "groq:llama-x"
TILE = "gemini:gemini-tile-model"

# main + two same-provider list models: P.CHAIN is the config the `fresh` fixture writes.
# Another provider in the middle of the list, for the hand-over and the permits.
MIDDLE_GROQ = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: groq
    model: llama-x
  - provider: gemini
    model: gemini-race-three
"""


def picked(identity, model):
    """A call that starts on ``identity`` (the user picked it): same pool, its own model name."""
    call = scripted(**{model.replace("-", "_"): 1})
    call.identity = identity
    call.kwargs = dict(call.kwargs, model=model)
    return call


def write(tmp_path, text):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    chain_mod.forget()


# --- the 1.8.3.0 rule is gone -------------------------------------------------------------------------
def test_the_main_model_gate_is_gone():
    assert not hasattr(transport.KameTransport, "_reply_on_main")
    assert not hasattr(transport, "reply_may_fall_back")
    assert not hasattr(chain_mod, "starts_on_main")


# --- a hand-picked list model: the main model answers for it, inside KAME --------------------------------
def test_a_picked_model_with_every_key_resting_is_answered_by_the_main_model(fresh, notices):
    rest_all(fresh, THREE)
    call = picked(THREE, "gemini-race-three")
    # the call asks the picked model's name; the stand-in asks gemini-race
    call.script = {"gemini-race": ("fast", 0.05)}
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"], "one request, to the main model; none to the resting pick"
    assert fresh.handed_over == 0, "same provider: KAME answers inside"
    assert getattr(call, "_kame_answered_by", None) == MAIN
    assert notices and notices[0].startswith("gemini-race answered for gemini-race-three"), notices


def test_the_ladder_of_a_picked_model_is_followed_in_order(fresh, notices):
    # list = two, three; main picked after the pick: picked THREE -> main, then two
    rest_all(fresh, THREE)
    rest_all(fresh, MAIN)
    call = picked(THREE, "gemini-race-three")
    call.script = {"gemini-race-two": ("fast", 0.05)}
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked == ["gemini-race-two"]


def test_a_busy_picked_model_is_passed_to_the_main_model(fresh, notices):
    fresh._note_busy(TWO)
    call = picked(TWO, "gemini-race-two")
    call.script = {"gemini-race": ("fast", 0.05), "gemini-race-three": ("fast", 0.05)}
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert call.models_asked == ["gemini-race"], "main first, then the rest of the list - never the busy pick"
    assert fresh.handed_over == 0


def test_the_direct_call_answers_with_a_stand_in_for_a_picked_model(fresh):
    rest_all(fresh, THREE)
    call = R.FakeCall(P.pool())
    call.identity = THREE
    chosen = fresh._maybe_hand_over(call, THREE, R.KEYS, 0.0, False, stand_in_ok=True)
    assert isinstance(chosen, transport._StandIn)
    assert chosen.identity == MAIN and chosen.model == "gemini-race"


def test_the_reply_row_names_the_model_picked_as_the_one_answered_for(fresh, notices):
    rest_all(fresh, THREE)
    call = picked(THREE, "gemini-race-three")
    call.script = {"gemini-race": ("fast", 0.05)}
    list(fresh.stream(call))
    row = fresh.replies[-1]
    assert row["model"] == "gemini-race" and row["for"] == "gemini-race-three"


def test_the_main_model_alone_is_answered_for_nobody_when_it_answers_itself(fresh, notices):
    call = R.FakeCall(P.pool())
    fresh._after_answer(call, MAIN, transport._Progress())
    assert fresh.replies[-1]["for"] == ""


# --- the order of 1.8.3.0 for the main model is unchanged ---------------------------------------------------
def test_the_main_model_still_walks_main_then_the_list(fresh, notices):
    rest_all(fresh, MAIN)
    rest_all(fresh, TWO)
    call = scripted(gemini_race_three=1)
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three here"


# --- another provider is Hermes' to switch to (the ladder still decides the first usable entry) ----------
def test_a_picked_model_is_handed_to_hermes_when_the_next_usable_entry_is_another_provider(fresh, tmp_path):
    write(tmp_path, MIDDLE_GROQ)
    rest_all(fresh, TWO)
    rest_all(fresh, MAIN)           # ladder(TWO) = TWO, MAIN, GROQ, THREE: MAIN rests, GROQ is next
    call = R.FakeCall(P.pool())
    call.identity = TWO
    out = fresh._maybe_hand_over(call, TWO, R.KEYS, 0.0, False, stand_in_ok=True)
    assert isinstance(out, transport.KamePoolResting)
    assert fresh.handed_over == 1


def test_a_model_nobody_listed_is_handed_to_hermes_not_answered_inside(fresh, tmp_path):
    rest_all(fresh, TILE)
    call = R.FakeCall(P.pool())
    call.identity = TILE
    out = fresh._maybe_hand_over(call, TILE, R.KEYS, 0.0, False, stand_in_ok=True)
    # its endpoint is unknown to KAME: no stand-in inside, Hermes walks ITS list
    assert isinstance(out, transport.KamePoolResting)


# --- the rules that never changed -----------------------------------------------------------------------
def test_a_background_job_never_switches_picked_or_not(fresh):
    rest_all(fresh, THREE)
    call = R.FakeCall(P.pool())
    call.identity = THREE
    call.channel = "secondary"
    assert fresh._maybe_hand_over(call, THREE, R.KEYS, 0.0, False, stand_in_ok=True) is None


def test_never_still_means_never(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    rest_all(fresh, THREE)
    call = R.FakeCall(P.pool())
    call.identity = THREE
    assert fresh._maybe_hand_over(call, THREE, R.KEYS, 0.0, False, stand_in_ok=True) is None


def test_the_flag_ignore_my_fallback_list_still_forbids_it(fresh, monkeypatch):
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    rest_all(fresh, THREE)
    call = R.FakeCall(P.pool())
    call.identity = THREE
    assert fresh._maybe_hand_over(call, THREE, R.KEYS, 0.0, False, stand_in_ok=True) is None
    assert fresh._chain_would_switch(THREE) is True, "the list would move this model: the flag has to hold it"


def test_never_after_text_reached_the_screen(fresh):
    rest_all(fresh, THREE)
    call = R.FakeCall(P.pool())
    call.identity = THREE
    assert fresh._maybe_hand_over(call, THREE, R.KEYS, 0.0, True, stand_in_ok=True) is None


# --- every walk reads the ladder of the reply's origin ------------------------------------------------------
def test_the_cross_model_race_for_a_picked_model_names_the_main_model_first(fresh):
    call = R.FakeCall(P.pool())
    call.identity = TWO
    legs = fresh._cross_target(call, TWO, False, 40.0)
    assert legs and legs[0][0] == MAIN, legs


def test_the_start_probe_for_a_picked_busy_model_runs_beside_the_main_model(fresh):
    fresh._note_busy(TWO)
    fresh._probed_at[TWO] = time.time() - 60.0
    call = R.FakeCall(P.pool())
    call.identity = TWO
    plan = fresh._start_probe_plan(call, TWO, True, R.KEYS)
    assert plan is not None and plan[0] == MAIN


def test_the_widen_candidate_for_a_picked_model_is_the_main_model(fresh):
    call = R.FakeCall(P.pool())
    call.identity = TWO
    found = fresh._widen_candidate(call, TWO, R.KEYS, [TWO])
    assert found is not None and found[0] == MAIN


def test_the_slow_watch_runs_for_a_picked_model(fresh):
    call = R.FakeCall(P.pool())
    call.identity = TWO
    assert fresh._slow_after(call, TWO, False) is not None


def test_the_circle_back_walks_the_ladder_from_the_top(fresh):
    call = R.FakeCall(P.pool())
    call.identity = THREE
    siblings = fresh._siblings(THREE, include_self=True, origin=THREE)
    assert [s[0] for s in siblings] == [THREE, MAIN, TWO], "ladder(THREE), not the list's own order"
    # everything after the pick is busy and not due; the pick is due: the circle comes back to it
    for entry in (MAIN, TWO):
        fresh._note_busy(entry)
    fresh._note_busy(THREE)
    fresh._probed_at[THREE] = time.time() - 60.0
    wrapped = fresh._wrap_target(call, THREE, seen=False, include_self=True)
    assert wrapped is not None and wrapped[0] == THREE


def test_the_circle_back_is_not_for_a_background_job(fresh):
    call = R.FakeCall(P.pool())
    call.identity = THREE
    call.channel = "secondary"
    assert fresh._wrap_target(call, THREE, seen=False) is None


# --- H3: the walk permit carries the reply's origin -----------------------------------------------------------
def test_a_hand_over_writes_permits_that_name_the_origin(fresh, tmp_path):
    write(tmp_path, MIDDLE_GROQ)
    rest_all(fresh, TWO)
    rest_all(fresh, MAIN)
    call = R.FakeCall(P.pool())
    call.identity = TWO
    assert isinstance(fresh._maybe_hand_over(call, TWO, R.KEYS, 0.0, False), transport.KamePoolResting)
    until, origin = fresh._walk_permits[GROQ]
    assert origin == TWO and until > time.time()
    assert fresh._walk_permits[THREE][1] == TWO


def test_a_call_on_a_permitted_entry_continues_the_ladder_of_the_origin(fresh, tmp_path):
    write(tmp_path, MIDDLE_GROQ)
    fresh._walk_permits[GROQ] = (time.time() + 60.0, TWO)
    walking = R.FakeCall(P.pool())
    walking.identity = GROQ
    assert fresh._origin_of(walking, GROQ) == TWO
    assert fresh._later(walking, GROQ) == [THREE], "after GROQ in ladder(TWO) = TWO, MAIN, GROQ, THREE"


def test_without_a_permit_the_entry_is_a_fresh_origin(fresh, tmp_path):
    write(tmp_path, MIDDLE_GROQ)
    own = R.FakeCall(P.pool())
    own.identity = GROQ
    assert fresh._origin_of(own, GROQ) == GROQ
    assert fresh._later(own, GROQ) == [MAIN, TWO, THREE]


def test_a_lapsed_permit_is_not_believed(fresh, tmp_path):
    write(tmp_path, MIDDLE_GROQ)
    fresh._walk_permits[GROQ] = (time.time() - 1.0, TWO)
    call = R.FakeCall(P.pool())
    call.identity = GROQ
    assert fresh._origin_of(call, GROQ) == GROQ


def test_the_origin_is_kept_on_the_call(fresh, tmp_path):
    write(tmp_path, MIDDLE_GROQ)
    call = R.FakeCall(P.pool())
    call.identity = GROQ
    assert fresh._origin_of(call, GROQ) == GROQ
    fresh._walk_permits[GROQ] = (time.time() + 60.0, TWO)       # too late for THIS call
    assert fresh._origin_of(call, GROQ) == GROQ


def test_hermes_own_walk_keeps_the_ladder_of_the_first_model(fresh, tmp_path):
    write(tmp_path, MIDDLE_GROQ)
    transport._HOST_WALK_PERMITS.clear()
    transport.note_host_fallback("gemini", "gemini-race-two")      # the host moves TWO's reply down the list
    transport.note_host_fallback("groq", "llama-x")                 # ... to another provider, then on
    assert transport._HOST_WALK_PERMITS[THREE][1] == TWO, "the third model is still the first reply's walk"
    third = R.FakeCall(P.pool())
    third.identity = THREE
    assert fresh._origin_of(third, THREE) == TWO


def test_a_failure_on_a_model_with_no_list_writes_no_permit(fresh, tmp_path):
    write(tmp_path, "model:\n  provider: gemini\n  default: gemini-race\n")
    transport._HOST_WALK_PERMITS.clear()
    transport.note_host_fallback("gemini", "gemini-race")
    assert not transport._HOST_WALK_PERMITS


# --- the hook ---------------------------------------------------------------------------------------------
BAD_KEY = dict(provider="gemini", status_code=400, error_message="API key not valid")


def hint(model, **extra):
    args = dict(BAD_KEY, model=model)
    args.update(extra)
    return pkg._on_api_error_classification(**args)


def test_the_hook_no_longer_withholds_fallback_for_a_picked_model(fresh):
    out = hint("gemini-tile-model")
    assert out is not None and out["should_fallback"] is True


def test_the_hook_keeps_the_reason_and_fallback_for_a_list_model(fresh):
    out = hint("gemini-race-two")
    assert out["should_fallback"] is True and out["reason"]
