"""1.8.3.0 (PLAN_1.8.3.0 D2 a, decisions/0033) - a sibling model answers inside KAME instead of a hand-over.

When every key of the main model rests, or the model is busy or slow, and the first usable model of the
user's list is on the SAME provider and endpoint, KAME answers with that model itself, on the keys it
already holds. Hermes is never asked to switch, so the chat's model selector never moves off the main
model. Another provider is still Hermes' to switch to.
"""
from __future__ import annotations

import os
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P

transport = R.transport
settings = R.settings
chain_mod = P.chain_mod

PRIMARY = "gemini:gemini-race"
SECOND = "gemini:gemini-race-two"
THIRD = "gemini:gemini-race-three"

CROSS_PROVIDER = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: groq
    model: llama-x
  - provider: gemini
    model: gemini-race-two
"""


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(P.CHAIN, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    transport._HOST_WALK_PERMITS.clear()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    transport._HOST_WALK_PERMITS.clear()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


@pytest.fixture()
def notices(monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_turn_notice", lambda text, *a, **k: said.append(text))
    return said


def rest_all(t, identity, seconds=3600.0, kind="daily"):
    for key in R.KEYS:
        t.engine.mark(identity, key, False, seconds, kind)


def scripted(**models):
    return P.Scripted(P.pool(), {name.replace("_", "-"): ("fast", 0.05) for name in models})


# --- every key resting ------------------------------------------------------------------------------
def test_with_every_key_resting_the_next_model_answers_inside_kame(fresh, notices):
    rest_all(fresh, PRIMARY)
    call = scripted(gemini_race_two=1)
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked == ["gemini-race-two"], "one request, to the sibling, nothing to the resting model"
    assert fresh.handed_over == 0, "Hermes was never asked to switch"
    assert getattr(call, "_kame_answered_by", None) == SECOND


def test_the_notice_names_the_model_that_answered_and_the_one_it_answered_for(fresh, notices):
    rest_all(fresh, PRIMARY)
    list(fresh.stream(scripted(gemini_race_two=1)))
    assert notices and notices[0].startswith("gemini-race-two answered for gemini-race"), notices


def test_the_direct_call_returns_a_stand_in_only_where_the_caller_can_use_it(fresh):
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    plain = fresh._maybe_hand_over(call, PRIMARY, R.KEYS, 0.0, False)
    assert isinstance(plain, transport.KamePoolResting), "the default is the 1.8.2.9 answer: Hermes is handed the call"
    fresh._handover_until.clear()
    fresh._walk_permits.clear()
    chosen = fresh._maybe_hand_over(call, PRIMARY, R.KEYS, 0.0, False, stand_in_ok=True)
    assert isinstance(chosen, transport._StandIn)
    assert chosen.identity == SECOND and chosen.model == "gemini-race-two"


def test_a_stand_in_is_not_a_hand_over_and_leaves_no_memo_or_walk_permit(fresh):
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    fresh._maybe_hand_over(call, PRIMARY, R.KEYS, 0.0, False, stand_in_ok=True)
    assert fresh.handed_over == 0
    assert not fresh._handover_until and not fresh._handover_later and not fresh._walk_permits


def test_when_the_sibling_rests_too_the_next_one_of_the_list_answers(fresh, notices):
    rest_all(fresh, PRIMARY)
    rest_all(fresh, SECOND)
    call = scripted(gemini_race_three=1)
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three here"
    assert fresh.handed_over == 0


# --- busy or slow model ----------------------------------------------------------------------------
def test_a_busy_main_model_hands_nothing_to_hermes_the_sibling_answers(fresh, notices):
    fresh._note_busy(PRIMARY)                       # a fresh window is never due
    call = scripted(gemini_race_two=1)
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked == ["gemini-race-two"]
    assert fresh.handed_over == 0
    assert notices and "answered for gemini-race" in notices[0]


def test_saver_does_not_forbid_it_because_it_is_not_a_race(fresh, monkeypatch, notices):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    fresh._note_busy(PRIMARY)
    call = scripted(gemini_race_two=1)
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert fresh.handed_over == 0


def test_never_forbids_it(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    rest_all(fresh, PRIMARY)
    assert fresh._maybe_hand_over(R.FakeCall(P.pool()), PRIMARY, R.KEYS, 0.0, False, stand_in_ok=True) is None


def test_the_flag_ignore_my_fallback_list_forbids_it(fresh, monkeypatch):
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    rest_all(fresh, PRIMARY)
    assert fresh._maybe_hand_over(R.FakeCall(P.pool()), PRIMARY, R.KEYS, 0.0, False, stand_in_ok=True) is None


def test_never_after_text_reached_the_screen(fresh):
    rest_all(fresh, PRIMARY)
    assert fresh._maybe_hand_over(R.FakeCall(P.pool()), PRIMARY, R.KEYS, 0.0, True, stand_in_ok=True) is None


def test_a_background_job_never_stands_in(fresh):
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    call.channel = "secondary"
    assert fresh._maybe_hand_over(call, PRIMARY, R.KEYS, 0.0, False, stand_in_ok=True) is None


def test_a_reply_that_did_not_start_on_the_main_model_stands_in_with_the_main_model(fresh):
    # 1.8.3.1 (D1): the list serves the model in use. A reply that started on a list model has the main
    # model right after it in its ladder, on the same provider: KAME answers with it, inside.
    other = "gemini:gemini-race-two"
    rest_all(fresh, other)
    call = R.FakeCall(P.pool())
    call.identity = other
    chosen = fresh._maybe_hand_over(call, other, R.KEYS, 0.0, False, stand_in_ok=True)
    assert isinstance(chosen, transport._StandIn) and chosen.identity == PRIMARY


# --- another provider stays Hermes' --------------------------------------------------------------------
def test_a_first_usable_entry_on_another_provider_is_still_handed_to_hermes(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(CROSS_PROVIDER, encoding="utf-8")
    chain_mod.forget()
    rest_all(fresh, PRIMARY)
    call = scripted(gemini_race_two=1)
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(call))
    assert call.models_asked == [], "no request: the chain walk is Hermes' (the first usable entry is groq)"
    assert fresh.handed_over == 1


def test_a_sibling_on_another_endpoint_is_not_a_stand_in(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\n  base_url: https://a.example/v1\n"
        "fallback_providers:\n  - provider: gemini\n    model: gemini-race-two\n    base_url: https://b.example/v1\n",
        encoding="utf-8")
    chain_mod.forget()
    rest_all(fresh, PRIMARY)
    out = fresh._maybe_hand_over(R.FakeCall(P.pool()), PRIMARY, R.KEYS, 0.0, False, stand_in_ok=True)
    assert isinstance(out, transport.KamePoolResting)


# --- what the call does with the answer ---------------------------------------------------------------
def test_the_reply_is_recorded_under_the_model_that_answered(fresh, notices):
    rest_all(fresh, PRIMARY)
    list(fresh.stream(scripted(gemini_race_two=1)))
    assert fresh.replies[-1]["model"] == "gemini-race-two"
    assert fresh.replies[-1]["for"] == "gemini-race"


def test_the_main_model_is_still_asked_first_on_the_next_reply(fresh, notices):
    rest_all(fresh, PRIMARY, seconds=1.0, kind="server")
    first = scripted(gemini_race_two=1)
    list(fresh.stream(first))
    time.sleep(1.2)
    second = scripted(gemini_race=1)
    out = list(fresh.stream(second))
    assert R.shown(out) == "gemini-race here"
    assert second.models_asked == ["gemini-race"]
