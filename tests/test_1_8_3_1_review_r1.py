"""1.8.3.1, review round 1 (PLAN_1.8.3.1 section 5): R1 R2 R3 R4 R8 (R13 lives in test_1_8_3_1_ladder.py).

R1  walk permits are never written for the main model; a call that IS on the main model is a fresh reply;
    ``note_host_fallback`` also reads the live transport's own permits.
R2  what is decided or planned as a HAND-OVER to Hermes never counts the main model unless it is the origin.
R3  the classification hook withholds ``should_fallback`` under "Never" too.
R4  ``back_to`` is the model as written (config entry, else the string Hermes sent), never a lower-cased guess.
R8  one failed reply writes one "no answer" row, however many times Hermes retries it.
"""
from __future__ import annotations

import importlib
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_8_3_0_sibling_standin as S
from tests import test_1_8_3_1_any_model as A

transport = R.transport
settings = R.settings
chain_mod = P.chain_mod
pkg = A.pkg

fresh = S.fresh
notices = S.notices
rest_all = S.rest_all

MAIN, TWO, THREE, GROQ, TILE = A.MAIN, A.TWO, A.THREE, A.GROQ, A.TILE


def _call(identity, model=None):
    call = R.FakeCall(P.pool())
    call.identity = identity
    if model is not None:
        call.kwargs = dict(call.kwargs, model=model)
    return call


# --- R1 -------------------------------------------------------------------------------------------------
def test_r1_a_host_walk_never_permits_the_main_model(fresh, tmp_path):
    A.write(tmp_path, A.MIDDLE_GROQ)
    transport._HOST_WALK_PERMITS.clear()
    transport.note_host_fallback("gemini", "gemini-race-two")
    assert MAIN not in transport._HOST_WALK_PERMITS, "Hermes never walks TO the main model"
    assert GROQ in transport._HOST_WALK_PERMITS and THREE in transport._HOST_WALK_PERMITS


def test_r1_a_hand_over_never_permits_the_main_model(fresh, tmp_path):
    A.write(tmp_path, A.MIDDLE_GROQ)
    rest_all(fresh, TWO)
    rest_all(fresh, GROQ)
    call = _call(TWO)
    assert isinstance(fresh._maybe_hand_over(call, TWO, R.KEYS, 0.0, False), transport.KamePoolResting)
    assert MAIN not in fresh._walk_permits
    assert fresh._walk_permits, "the entries Hermes can reach are still permitted"


def test_r1_a_call_on_the_main_model_is_a_fresh_reply_whatever_a_permit_says(fresh, tmp_path):
    A.write(tmp_path, A.MIDDLE_GROQ)
    fresh._walk_permits[MAIN] = (time.time() + 60.0, TWO)       # an old or stray permit for main
    transport._HOST_WALK_PERMITS[MAIN] = (time.time() + 60.0, THREE)
    call = _call(MAIN)
    assert fresh._origin_of(call, MAIN) == MAIN
    assert fresh._later(call, MAIN) == [TWO, GROQ, THREE]


def test_r1_a_permit_still_works_for_the_other_entries(fresh, tmp_path):
    A.write(tmp_path, A.MIDDLE_GROQ)
    fresh._walk_permits[GROQ] = (time.time() + 60.0, TWO)
    assert fresh._origin_of(_call(GROQ), GROQ) == TWO


def test_r1_the_hook_reads_the_permits_of_the_live_transport(fresh, tmp_path, monkeypatch):
    facade = importlib.import_module(f"{R.PACKAGE}.facade")
    A.write(tmp_path, A.MIDDLE_GROQ)
    transport._HOST_WALK_PERMITS.clear()
    monkeypatch.setattr(facade, "TRANSPORT", fresh)
    # KAME handed a TWO reply to Hermes: the instance holds the permit for GROQ with origin TWO
    fresh._walk_permits[GROQ] = (time.time() + 60.0, TWO)
    transport.note_host_fallback("groq", "llama-x")             # the host walks on, from GROQ
    assert transport._HOST_WALK_PERMITS[THREE][1] == TWO, "still the reply that started on TWO"
    assert MAIN not in transport._HOST_WALK_PERMITS


# --- R2 -------------------------------------------------------------------------------------------------
def test_r2_hermes_is_not_counted_free_when_only_the_main_model_is(fresh):
    # ladder(TWO) = TWO, MAIN, THREE: only MAIN is free, and Hermes' own list never goes to it
    rest_all(fresh, TWO)
    rest_all(fresh, THREE)
    call = _call(TWO)
    assert fresh._hermes_has_free_after(call, TWO) is False


def test_r2_the_main_model_as_origin_keeps_its_list(fresh):
    rest_all(fresh, MAIN)
    call = _call(MAIN)
    assert fresh._hermes_has_free_after(call, MAIN) is True      # TWO and THREE are after main


def test_r2_no_hand_over_to_hermes_when_only_the_main_model_could_answer(fresh):
    rest_all(fresh, TWO)
    rest_all(fresh, THREE)
    call = _call(TWO)
    assert fresh._maybe_hand_over(call, TWO, R.KEYS, 0.0, False) is None
    assert fresh.handed_over == 0 and not fresh._walk_permits


def test_r2_kames_own_stand_in_still_reaches_the_main_model(fresh):
    rest_all(fresh, TWO)
    rest_all(fresh, THREE)
    call = _call(TWO)
    out = fresh._maybe_hand_over(call, TWO, R.KEYS, 0.0, False, stand_in_ok=True)
    assert isinstance(out, transport._StandIn) and out.identity == MAIN


def test_r2_the_circle_back_is_not_blocked_by_a_main_model_hermes_cannot_reach(fresh):
    # nothing after the position is reachable by Hermes -> the circle back (KAME's own) may walk the ladder
    rest_all(fresh, TWO)
    rest_all(fresh, THREE)
    call = _call(TWO)
    picked = fresh._wrap_target(call, TWO, seen=False)
    assert picked is not None and picked[0] == MAIN


# --- R3 -------------------------------------------------------------------------------------------------
BAD_KEY = dict(provider="gemini", status_code=400, error_message="API key not valid")


def test_r3_never_withholds_should_fallback_in_the_hook(fresh, monkeypatch):
    out = pkg._on_api_error_classification(**dict(BAD_KEY, model="gemini-race-two"))
    assert out["should_fallback"] is True
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    out = pkg._on_api_error_classification(**dict(BAD_KEY, model="gemini-race-two"))
    assert out["should_fallback"] is False


# --- R4 -------------------------------------------------------------------------------------------------
def _answered(fresh, position, answered, model_sent):
    call = _call(position, model_sent)
    call._kame_answered_by = answered
    call._kame_started = time.monotonic() - 1.0
    fresh._after_answer(call, answered, transport._Progress())
    return fresh.replies[-1]


def test_r4_a_model_outside_the_list_goes_back_as_hermes_sent_it(fresh, notices):
    row = _answered(fresh, TILE, MAIN, "Gemini-Tile-Model")
    assert row["back_to"] == {"provider": "gemini", "model": "Gemini-Tile-Model"}
    assert row["main"] == row["back_to"]


def test_r4_a_listed_model_is_written_as_the_config_wrote_it(fresh, tmp_path, notices):
    A.write(tmp_path, A.MIDDLE_GROQ.replace("model: gemini-race-two", "model: Gemini-Race-Two"))
    row = _answered(fresh, TWO, MAIN, "gemini-race-two")
    assert row["back_to"]["model"] == "Gemini-Race-Two", "the config wins over the lower-cased string"


def test_r4_the_walk_of_a_cross_provider_hand_over_keeps_the_written_name(fresh, tmp_path, notices):
    A.write(tmp_path, A.MIDDLE_GROQ)
    first = _call(TILE, "Gemini-Tile-Model")
    assert fresh._origin_of(first, TILE) == TILE             # the first call of the reply: the name is learned
    fresh._walk_permits[GROQ] = (time.time() + 60.0, TILE)
    second = _call(GROQ, "llama-x")                          # Hermes' rebuilt call, on the other provider
    second._kame_answered_by = GROQ
    second._kame_started = time.monotonic() - 1.0
    fresh._after_answer(second, GROQ, transport._Progress())
    row = fresh.replies[-1]
    assert row["back_to"]["model"] == "Gemini-Tile-Model"
    assert row["switched"] is True


def test_r4_nothing_known_means_no_back_to_never_a_guess(fresh, notices):
    call = R.FakeCall(P.pool())
    call.identity = TILE
    call.kwargs = {}                                         # no model string at all
    call._kame_answered_by = MAIN
    call._kame_started = time.monotonic() - 1.0
    fresh._after_answer(call, MAIN, transport._Progress())
    row = fresh.replies[-1]
    assert row["for"] == "gemini-tile-model"
    assert "back_to" not in row and "main" not in row


# --- R8 -------------------------------------------------------------------------------------------------
def _fail(fresh, identity, status=429, owner="chat-1"):
    call = _call(identity)
    call.status_owner = lambda: owner
    error = RuntimeError("boom")
    error.status_code = status
    fresh._note_failed(call, error)


def test_r8_hermes_retries_of_one_failed_reply_write_one_row(fresh):
    for _ in range(5):
        _fail(fresh, MAIN)
    failed = [r for r in fresh.replies if r.get("failed")]
    assert len(failed) == 1 and failed[0]["reason"] == "the provider answered 429"


def test_r8_another_conversation_or_another_model_is_another_row(fresh):
    _fail(fresh, MAIN, owner="chat-1")
    _fail(fresh, MAIN, owner="chat-2")
    _fail(fresh, TWO, owner="chat-1")
    assert len([r for r in fresh.replies if r.get("failed")]) == 3


def test_r8_a_failure_long_after_the_first_is_a_new_row(fresh, monkeypatch):
    _fail(fresh, MAIN)
    later = time.time() + transport._FAILED_ROW_WINDOW_S + 5.0
    monkeypatch.setattr(transport.time, "time", lambda: later)
    _fail(fresh, MAIN)
    assert len([r for r in fresh.replies if r.get("failed")]) == 2


def test_r8_the_one_row_carries_the_latest_reason(fresh):
    _fail(fresh, MAIN, status=429)
    _fail(fresh, MAIN, status=503)
    failed = [r for r in fresh.replies if r.get("failed")]
    assert len(failed) == 1 and failed[0]["reason"] == "the provider answered 503"
