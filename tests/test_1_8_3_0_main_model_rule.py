"""1.8.3.0 (PLAN_1.8.3.0 D1, decisions/0033) - the fallback list belongs to the MAIN model.

A reply may go down the fallback list only when it STARTED on the main model: Hermes'
``model.default``, and only when that model is not itself an entry of ``fallback_providers``.
A reply that started on any other model (picked by hand, a secondary tile, a list model chosen as
the default) uses only that model's keys. A walk down the list inside ONE reply - Hermes rebuilt the
message on the next entry after KAME's hand-over - keeps the permission of the reply it belongs to.
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
    transport._REPLY_ORIGIN.clear()
    transport._HOST_WALK_PERMITS.clear()
    t = transport.KameTransport(engine=H.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    transport._REPLY_ORIGIN.clear()
    transport._HOST_WALK_PERMITS.clear()


def rest(t, identity, seconds=3600.0):
    for key in H.KEYS:
        t.engine.mark(identity, key, False, seconds, "daily")


def write(tmp_path, text):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    chain_mod.forget()


# --- the rule, as the chain module states it ---------------------------------------------------------
def test_the_main_model_is_the_primary(fresh):
    assert chain_mod.starts_on_main(MAIN)
    assert chain_mod.starts_on_main(MAIN + ":global"), "a pool suffix is still the same model"


def test_a_list_model_or_a_hand_picked_model_is_not_main(fresh):
    assert not chain_mod.starts_on_main(LIST_1)
    assert not chain_mod.starts_on_main(TILE)


def test_a_main_model_that_is_also_in_its_own_list_is_not_main(fresh, tmp_path):
    write(tmp_path, MAIN_IN_ITS_OWN_LIST)
    assert chain_mod.primary_identity() == LIST_1
    assert not chain_mod.starts_on_main(LIST_1), "picked from the list: the list is not its fallback"


def test_with_no_main_model_known_nothing_changes(fresh, tmp_path):
    write(tmp_path, NO_MAIN)
    assert chain_mod.starts_on_main(LIST_1), "nothing to judge by: the 1.8.2.9 behaviour stays"
    assert chain_mod.starts_on_main("")


# --- the reply's start, per call ---------------------------------------------------------------------
def test_a_call_on_the_main_model_may_fall_back(fresh):
    assert fresh._reply_on_main(H.FakeCall(MAIN)) is True


def test_a_call_on_a_hand_picked_model_may_not(fresh):
    assert fresh._reply_on_main(H.FakeCall(TILE)) is False


def test_a_call_on_a_list_model_that_nobody_handed_to_may_not(fresh):
    assert fresh._reply_on_main(H.FakeCall(LIST_1)) is False


def test_the_answer_is_kept_on_the_call(fresh):
    call = H.FakeCall(LIST_1)
    assert fresh._reply_on_main(call) is False
    fresh._walk_permits[LIST_1] = time.time() + 60.0      # too late for THIS call: it started without it
    assert fresh._reply_on_main(call) is False


# --- the hand-over -----------------------------------------------------------------------------------
def test_a_main_reply_is_handed_to_hermes(fresh):
    rest(fresh, MAIN)
    out = fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    assert isinstance(out, transport.KamePoolResting)


def test_a_hand_picked_model_is_never_handed_over(fresh):
    rest(fresh, TILE)
    assert fresh._maybe_hand_over(H.FakeCall(TILE), TILE, H.KEYS, 0.0, False) is None
    assert fresh._maybe_hand_over(H.FakeCall(TILE), TILE, H.KEYS, 0.0, False, waited=99.0, busy=True,
                                  after_failure=True) is None
    assert fresh.handed_over == 0


def test_a_list_model_chosen_as_the_default_is_never_handed_over(fresh, tmp_path):
    write(tmp_path, MAIN_IN_ITS_OWN_LIST)
    rest(fresh, LIST_1)
    assert fresh._maybe_hand_over(H.FakeCall(LIST_1), LIST_1, H.KEYS, 0.0, False) is None


def test_the_walk_inside_one_reply_keeps_its_permission(fresh):
    rest(fresh, MAIN)
    assert isinstance(fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False),
                      transport.KamePoolResting)
    # Hermes rebuilt the message on the next entry: a NEW call on LIST_1 arrives a moment later
    second = H.FakeCall(LIST_1)
    assert fresh._reply_on_main(second) is True
    rest(fresh, LIST_1)
    assert isinstance(fresh._maybe_hand_over(second, LIST_1, H.KEYS, 0.0, False), transport.KamePoolResting)
    third = H.FakeCall(LIST_2)
    assert fresh._reply_on_main(third) is True, "#2 -> #3 is still the same reply"


def test_the_permission_is_only_for_the_entries_handed_to(fresh):
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    assert fresh._reply_on_main(H.FakeCall(TILE)) is False


def test_the_permission_lapses(fresh):
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    for entry in list(fresh._walk_permits):
        fresh._walk_permits[entry] = time.time() - 1.0
    assert fresh._reply_on_main(H.FakeCall(LIST_1)) is False


def test_another_conversation_on_the_main_model_does_not_end_a_walk(fresh):
    # Review R2: one conversation's walk down the list must survive another conversation's new reply.
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    assert fresh._reply_on_main(H.FakeCall(MAIN)) is True
    assert fresh._reply_on_main(H.FakeCall(LIST_1)) is True, "the first conversation is still walking"


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


def test_a_cross_model_race_is_for_the_main_reply_only(fresh, tmp_path):
    write(tmp_path, SAME_PROVIDER)
    assert fresh._cross_target(H.FakeCall(MAIN), MAIN, False, 40.0)
    other = "gemini:gemini-3.7-flash"
    assert fresh._cross_target(H.FakeCall(other), other, False, 40.0) is None, "a list model chosen by hand"


def test_the_start_on_a_sibling_is_for_the_main_reply_only(fresh, tmp_path):
    write(tmp_path, SAME_PROVIDER)
    fresh._note_busy(MAIN)
    fresh._probed_at[MAIN] = time.time() - 60.0
    assert fresh._start_probe_plan(H.FakeCall(MAIN), MAIN, True, H.KEYS) is not None
    other = "gemini:gemini-3.7-flash"
    fresh._note_busy(other)
    fresh._probed_at[other] = time.time() - 60.0
    assert fresh._start_probe_plan(H.FakeCall(other), other, True, H.KEYS) is None


def test_widening_and_circling_are_for_the_main_reply_only(fresh, tmp_path):
    write(tmp_path, SAME_PROVIDER)
    other = "gemini:gemini-3.7-flash"
    call = H.FakeCall(other)
    assert fresh._widen_candidate(call, other, H.KEYS, [other]) is None
    assert fresh._wrap_target(call, other, seen=False) is None
    assert fresh._slow_after(call, other, False) is None


# --- the hook ---------------------------------------------------------------------------------------
BAD_KEY = dict(provider="gemini", status_code=400, error_message="API key not valid")


def hint(model, **extra):
    args = dict(BAD_KEY, model=model)
    args.update(extra)
    return pkg._on_api_error_classification(**args)


def test_the_hook_keeps_fallback_on_for_the_main_model(fresh):
    fresh._reply_on_main(H.FakeCall(MAIN))                 # a reply on the main model was seen
    assert hint("gemini-3.8-flash")["should_fallback"] is True


def test_the_hook_withholds_fallback_for_a_reply_that_did_not_start_on_main(fresh):
    fresh._reply_on_main(H.FakeCall(TILE))
    out = hint("gemini-tile-model")
    assert out is not None and out["should_fallback"] is False
    assert out["reason"], "only the permission changes, never the reason"


def test_the_hook_keeps_fallback_for_a_walk_inside_a_main_reply(fresh):
    rest(fresh, MAIN)
    fresh._maybe_hand_over(H.FakeCall(MAIN), MAIN, H.KEYS, 0.0, False)
    fresh._reply_on_main(H.FakeCall(LIST_1))
    assert hint("llama-x", provider="groq")["should_fallback"] is True


def test_the_hook_with_no_record_answers_as_before(fresh):
    assert hint("some-unseen-model")["should_fallback"] is True


def test_a_stale_record_is_not_believed(fresh, monkeypatch):
    fresh._reply_on_main(H.FakeCall(TILE))
    key = next(iter(transport._REPLY_ORIGIN))
    on_main, _at = transport._REPLY_ORIGIN[key]
    transport._REPLY_ORIGIN[key] = (on_main, time.time() - 3600.0)
    assert hint("gemini-tile-model")["should_fallback"] is True


# --- review R3: Hermes' own walk (the hook let a main reply fall back without KAME handing it over) -----
def test_a_main_model_failure_seen_by_the_hook_permits_hermes_own_walk(fresh):
    hint("gemini-3.8-flash", provider="gemini")
    assert fresh._reply_on_main(H.FakeCall(LIST_1)) is True
    assert fresh._reply_on_main(H.FakeCall(LIST_2)) is True


def test_a_hand_picked_model_failure_permits_nothing(fresh):
    fresh._reply_on_main(H.FakeCall(TILE))
    hint("gemini-tile-model", provider="gemini")
    assert fresh._reply_on_main(H.FakeCall(LIST_1)) is False
