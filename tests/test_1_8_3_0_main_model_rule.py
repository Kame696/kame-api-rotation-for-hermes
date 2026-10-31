"""1.8.3.0 main-model rule - REWRITTEN in 1.8.3.1 (PLAN_1.8.3.1 D1, decisions/0034).

1.8.3.0 allowed the fallback list only for a reply that STARTED on the main model. 1.8.3.1 reverses
that: the list serves the model in use, whichever it is. This file keeps the questions the 1.8.3.0 tests
asked (one by one, in the same order) and gives each its new answer; decisions/0034 lists what every old
assertion now says. The walk inside one reply still keeps its origin; background jobs, "Never" and the
flag still forbid every switch.
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_7_handover as H

transport = H.transport
settings = H.settings
chain_mod = H.chain_mod
pkg = H.pkg

MAIN = "gemini:gemini-3.8-flash"
LIST_1 = "groq:llama-x"
LIST_2 = "openrouter:qwen-y"
TILE = "gemini:gemini-tile-model"      # a model nobody listed, picked by hand in a secondary tile

CONFIG = """\
model:
  provider: gemini
  default: gemini-3.8-flash
fallback_providers:
  - provider: groq
    model: llama-x
  - provider: openrouter
    model: qwen-y
"""

# The user picked a model of the list as the default: Hermes' own list still has it inside.
MAIN_IN_ITS_OWN_LIST = """\
model:
  provider: groq
  default: llama-x
fallback_providers:
  - provider: gemini
    model: gemini-3.8-flash
  - provider: groq
    model: llama-x
  - provider: openrouter
    model: qwen-y
"""

NO_MAIN = """\
fallback_providers:
  - provider: groq
    model: llama-x
"""


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "0")
    settings.forget()
    chain_mod.forget()
    transport._HOST_WALK_PERMITS.clear()
    t = transport.KameTransport(engine=H.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    transport._HOST_WALK_PERMITS.clear()


def rest(t, identity, seconds=3600.0):
    for key in H.KEYS:
        t.engine.mark(identity, key, False, seconds, "daily")


def write(tmp_path, text):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    chain_mod.forget()


# --- the ladder, where 1.8.3.0 asked "is it main?" ---------------------------------------------------------
def test_the_main_model_is_the_primary(fresh):
    assert chain_mod.ladder(MAIN) == [MAIN, LIST_1, LIST_2]
    assert chain_mod.ladder(MAIN + ":global")[0] == MAIN, "a pool suffix is still the same model"


def test_a_list_model_or_a_hand_picked_model_has_the_main_model_after_it(fresh):
    assert chain_mod.ladder(LIST_1) == [LIST_1, MAIN, LIST_2]
    assert chain_mod.ladder(TILE) == [TILE, MAIN, LIST_1, LIST_2]


def test_a_main_model_that_is_also_in_its_own_list_is_one_entry(fresh, tmp_path):
    write(tmp_path, MAIN_IN_ITS_OWN_LIST)
    assert chain_mod.primary_identity() == LIST_1
    assert chain_mod.ladder(LIST_1) == [LIST_1, MAIN, LIST_2], "asked once, in the user's order"


def test_with_no_main_model_known_the_list_alone_serves(fresh, tmp_path):
    write(tmp_path, NO_MAIN)
    assert chain_mod.ladder(LIST_1) == [LIST_1], "one model and nothing to switch to"
    assert chain_mod.ladder("") == [LIST_1]


# --- the reply's origin, per call ---------------------------------------------------------------------------
def test_a_call_on_the_main_model_started_there(fresh):
    assert fresh._origin_of(H.FakeCall(MAIN)) == MAIN


def test_a_call_on_a_hand_picked_model_started_there(fresh):
    assert fresh._origin_of(H.FakeCall(TILE)) == TILE


def test_a_call_on_a_list_model_that_nobody_handed_to_started_there(fresh):
    assert fresh._origin_of(H.FakeCall(LIST_1)) == LIST_1


def test_the_answer_is_kept_on_the_call(fresh):
    call = H.FakeCall(LIST_1)
    assert fresh._origin_of(call) == LIST_1
    fresh._walk_permits[LIST_1] = (time.time() + 60.0, MAIN)      # too late for THIS call: it started without it
    assert fresh._origin_of(call) == LIST_1


# --- the hand-over -----------------------------------------------------------------------------------
def test_a_main_reply_is_handed_to_hermes(fresh):
    rest(fresh, MAIN)
    out = fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    assert isinstance(out, transport.KamePoolResting)


def test_a_hand_picked_model_is_handed_over_down_its_own_ladder(fresh):
    rest(fresh, TILE)
    out = fresh._maybe_hand_over(H.FakeCall(TILE), TILE, H.KEYS, 0.0, False)
    assert isinstance(out, transport.KamePoolResting), "1.8.3.0: None. The list serves the model in use"
    assert fresh.handed_over == 1
    # review R1: Hermes' own list is what it walks; the main model is not in it, so no permit is written
    # for it (KAME reaches the main model itself, as a stand-in). The list entries carry the pick as origin.
    assert MAIN not in fresh._walk_permits
    assert fresh._walk_permits[LIST_1][1] == TILE


def test_a_list_model_chosen_as_the_default_is_handed_over_too(fresh, tmp_path):
    write(tmp_path, MAIN_IN_ITS_OWN_LIST)
    rest(fresh, LIST_1)
    out = fresh._maybe_hand_over(H.FakeCall(LIST_1), LIST_1, H.KEYS, 0.0, False)
    assert isinstance(out, transport.KamePoolResting)


def test_the_walk_inside_one_reply_keeps_its_origin(fresh):
    rest(fresh, MAIN)
    assert isinstance(fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False),
                      transport.KamePoolResting)
    # Hermes rebuilt the message on the next entry: a NEW call on LIST_1 arrives a moment later
    second = H.FakeCall(LIST_1)
    assert fresh._origin_of(second) == MAIN
    rest(fresh, LIST_1)
    assert isinstance(fresh._maybe_hand_over(second, LIST_1, H.KEYS, 0.0, False), transport.KamePoolResting)
    third = H.FakeCall(LIST_2)
    assert fresh._origin_of(third) == MAIN, "#2 -> #3 is still the same reply"


def test_the_permit_is_only_for_the_entries_handed_to(fresh):
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    assert fresh._origin_of(H.FakeCall(TILE)) == TILE


def test_the_permit_lapses(fresh):
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    for entry in list(fresh._walk_permits):
        fresh._walk_permits[entry] = (time.time() - 1.0, MAIN)
    assert fresh._origin_of(H.FakeCall(LIST_1)) == LIST_1


def test_another_conversation_on_the_main_model_does_not_end_a_walk(fresh):
    # Review R2: one conversation's walk down the list must survive another conversation's new reply.
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    assert fresh._origin_of(H.FakeCall(MAIN)) == MAIN
    assert fresh._origin_of(H.FakeCall(LIST_1)) == MAIN, "the first conversation is still walking"


def test_never_still_means_never(fresh, monkeypatch):
    rest(fresh, MAIN)
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    assert fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False) is None


def test_a_background_job_never_switches_main_or_not(fresh):
    rest(fresh, MAIN)
    call = H.FakeCall(MAIN)
    call.channel = "secondary"
    assert fresh._maybe_hand_over(call, MAIN, H.KEYS, 0.0, False) is None


# --- the race, the start on a sibling ---------------------------------------------------------------
SAME_PROVIDER = """\
model:
  provider: gemini
  default: gemini-3.8-flash
fallback_providers:
  - provider: gemini
    model: gemini-3.7-flash
  - provider: gemini
    model: gemini-3.6-flash
"""


def test_a_cross_model_race_is_for_every_reply_the_main_model_first(fresh, tmp_path):
    write(tmp_path, SAME_PROVIDER)
    assert fresh._cross_target(H.FakeCall(MAIN), MAIN, False, 40.0)
    other = "gemini:gemini-3.7-flash"
    legs = fresh._cross_target(H.FakeCall(other), other, False, 40.0)
    assert legs and legs[0][0] == MAIN, "a list model picked by hand: the main model joins first"


def test_the_start_on_a_sibling_is_for_every_reply(fresh, tmp_path):
    write(tmp_path, SAME_PROVIDER)
    fresh._note_busy(MAIN)
    fresh._probed_at[MAIN] = time.time() - 60.0
    assert fresh._start_probe_plan(H.FakeCall(MAIN), MAIN, True, H.KEYS) is not None
    fresh.forget_busy(MAIN)         # the main model is back; the pick is the busy one now
    other = "gemini:gemini-3.7-flash"
    fresh._note_busy(other)
    fresh._probed_at[other] = time.time() - 60.0
    plan = fresh._start_probe_plan(H.FakeCall(other), other, True, H.KEYS)
    assert plan is not None and plan[0] == MAIN


def test_widening_and_the_slow_watch_are_for_every_reply(fresh, tmp_path):
    write(tmp_path, SAME_PROVIDER)
    other = "gemini:gemini-3.7-flash"
    call = H.FakeCall(other)
    found = fresh._widen_candidate(call, other, H.KEYS, [other])
    assert found is not None and found[0] == MAIN
    assert fresh._slow_after(call, other, False) is not None


# --- the hook ---------------------------------------------------------------------------------------
BAD_KEY = dict(provider="gemini", status_code=400, error_message="API key not valid")


def hint(model, **extra):
    args = dict(BAD_KEY, model=model)
    args.update(extra)
    return pkg._on_api_error_classification(**args)


def test_the_hook_keeps_fallback_on_for_the_main_model(fresh):
    assert hint("gemini-3.8-flash")["should_fallback"] is True


def test_the_hook_no_longer_withholds_fallback_for_a_reply_that_did_not_start_on_main(fresh):
    fresh._origin_of(H.FakeCall(TILE))
    out = hint("gemini-tile-model")
    assert out is not None and out["should_fallback"] is True
    assert out["reason"], "only the permission could have changed, never the reason"


def test_the_hook_keeps_fallback_for_a_walk_inside_a_reply(fresh):
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    fresh._origin_of(H.FakeCall(LIST_1))
    assert hint("llama-x", provider="groq")["should_fallback"] is True


def test_the_hook_with_no_record_answers_as_before(fresh):
    assert hint("some-unseen-model")["should_fallback"] is True


# --- review R3: Hermes' own walk (the hook lets a reply fall back without KAME handing it over) ----------
def test_a_main_model_failure_seen_by_the_hook_permits_hermes_own_walk(fresh):
    hint("gemini-3.8-flash", provider="gemini")
    assert fresh._origin_of(H.FakeCall(LIST_1)) == MAIN
    assert fresh._origin_of(H.FakeCall(LIST_2)) == MAIN


def test_a_hand_picked_model_failure_permits_the_walk_down_its_ladder(fresh):
    hint("gemini-tile-model", provider="gemini")
    # review R1: 1.8.3.1 as built said "the main model comes right after the pick"; Hermes never walks TO
    # the main model, so a call on it is a fresh reply (it must not inherit a pick's origin for 90 s).
    assert fresh._origin_of(H.FakeCall(MAIN)) == MAIN
    assert fresh._origin_of(H.FakeCall(LIST_1)) == TILE
