"""1.8.2.9 (decisions/0030): when every model of the list is down, circle back to the best.

The owner's live test of 1.8.2.8 (2026-10-23): with the primary and every fallback model busy, a
reply ended up waiting on the LAST model of the list - the least capable one - while the best
model may have recovered a moment later. Causes, read in transport.py:

* the hand-over to Hermes asked only whether a later entry had a key resting or not, never
  whether it was known busy, so a busy chain was walked to its last entry;
* the last entry has no later one to hand over to, so it was waited on for ever;
* a model's busy window doubles to five minutes, and inside it the best model is not asked.

Now, when nothing AFTER Hermes' own position is free, and the model in use says it is busy (or
every key of it rests), the call circles back, in the user's list order, to the first model of
the same provider and endpoint that is usable and not asked in the last ``_PROBE_EVERY_S``. One
request at a time, no extra legs. Models of the user's list only. Tests are named after the
review findings of the design pass (H1-H4, M1-M5, L1-L2).

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_wrap_around.py -q
"""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
settings = R.settings
chain_mod = __import__(f"{R.PACKAGE}.core.fallback_chain", fromlist=["x"])

FIRST = "gemini:gemini-race"
SECOND = "gemini:gemini-race-two"
THIRD = "gemini:gemini-race-three"

CHAIN = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: gemini
    model: gemini-race-three
"""


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


class ScriptCall(R.FakeCall):
    """A call on ``model``; ``script`` says how each model answers: "ok", "busy", "bad", or a
    list of outcomes used in order (the last one repeats)."""

    def __init__(self, model, script):
        super().__init__(R.Pool({}), kwargs=dict(R.REQUEST, model=model))
        self.identity = "gemini:" + model
        # 1.8.3.0 (D1): a call on a later model is a reply Hermes walked down the list: it keeps its permission.
        self._kame_on_main = True
        self.script = {k: (list(v) if isinstance(v, list) else [v]) for k, v in script.items()}
        self.models_asked = []

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            self.models_asked.append(model)
            plan = self.script.get(model, ["ok"])
            how = plan.pop(0) if len(plan) > 1 else plan[0]
            if how == "busy":
                raise R.Refusal(503, "The model is overloaded. Please try again later.")
            if how == "bad":
                raise R.Refusal(400, "Request contains an invalid argument for this model.")
            return R.FakeStream([0.02, R.chunk(f"{model} "), R.chunk("answer", finish="stop")])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def busy_known(t, *identities, probed_ago=60.0):
    """Known busy; asked ``probed_ago`` seconds ago (60 = due another look, 1 = just asked)."""
    for ident in identities:
        t._note_busy(ident)
        t._probed_at[ident] = time.time() - probed_ago


def hand_over(t, call, identity=FIRST):
    return t._maybe_hand_over(call, identity, R.KEYS, 0.0, False, waited=0.5, busy=True, after_failure=True)


# --- the hand-over: only to a model that is free (root cause 1) -----------------------

def test_a_hand_over_never_walks_into_models_known_busy(fresh):
    busy_known(fresh, SECOND, THIRD, probed_ago=1.0)
    call = ScriptCall("gemini-race", {})
    assert hand_over(fresh, call) is None
    fresh._busy_until.pop(THIRD)                      # one free model after it: hand over as before
    assert isinstance(hand_over(fresh, call), transport.KamePoolResting)


def test_M5_a_busy_later_model_that_is_due_another_look_is_still_reachable(fresh):
    busy_known(fresh, SECOND, THIRD, probed_ago=60.0)
    assert isinstance(hand_over(fresh, ScriptCall("gemini-race", {})), transport.KamePoolResting)


def test_H1_a_hand_over_is_judged_from_the_position_hermes_is_on(fresh):
    # Hermes is on the LAST entry; the call circled up to the first and it failed. Hermes
    # cannot go back up, so naming the second model would burn its retries (review H1).
    call = ScriptCall("gemini-race-three", {})
    assert hand_over(fresh, call, identity=FIRST) is None


# --- the way back up ------------------------------------------------------------------

def test_the_last_model_busy_circles_back_to_the_best_one_that_recovered(fresh):
    # THIRD answers 503; FIRST and SECOND are known busy, FIRST due another look.
    busy_known(fresh, FIRST, SECOND, probed_ago=60.0)
    call = ScriptCall("gemini-race-three", {"gemini-race-three": "busy", "gemini-race": "ok"})
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race answer"
    assert time.monotonic() - started < 4.0
    assert call.models_asked[-1] == "gemini-race"
    assert "gemini-race-two" not in call.models_asked     # the best was tried before the second
    assert not fresh._entry_busy(FIRST)                   # it answered: not busy any more
    assert getattr(call, "_kame_answered_by") == FIRST    # M2: the reply is named for the model that wrote it


def test_a_best_model_asked_a_moment_ago_is_not_knocked_again(fresh):
    busy_known(fresh, FIRST, SECOND, THIRD, probed_ago=1.0)
    call = ScriptCall("gemini-race-three", {"gemini-race-three": ["busy"] * 5 + ["ok"]})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three answer"
    assert "gemini-race" not in call.models_asked and "gemini-race-two" not in call.models_asked


def test_every_key_of_the_last_model_resting_circles_back_instead_of_waiting(fresh):
    busy_known(fresh, FIRST, SECOND, probed_ago=60.0)
    for key in R.KEYS:
        fresh.engine.mark(THIRD, key, False, 3600.0, "daily")      # THIRD is not known busy: it rests
    call = ScriptCall("gemini-race-three", {"gemini-race": "ok"})
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race answer"
    assert time.monotonic() - started < 4.0


# --- the call start (review H2) -------------------------------------------------------

def test_the_call_start_moves_to_a_free_model_without_asking_the_busy_one(fresh):
    busy_known(fresh, FIRST, probed_ago=1.0)       # SECOND and THIRD are free
    call = ScriptCall("gemini-race", {})
    # 1.8.3.0 (D2 a): the free model is on the same provider, so KAME answers with it itself
    # (it used to hand the call to Hermes); the busy one still gets no request.
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two answer"
    assert call.models_asked == ["gemini-race-two"]


def test_H2_the_call_start_asks_the_best_model_itself_when_it_is_due(fresh):
    busy_known(fresh, FIRST, probed_ago=60.0)
    busy_known(fresh, SECOND, THIRD, probed_ago=1.0)
    call = ScriptCall("gemini-race", {})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race answer" and call.models_asked == ["gemini-race"]


def test_H2_the_call_start_goes_up_to_a_due_better_model_not_to_the_last(fresh):
    busy_known(fresh, FIRST, probed_ago=60.0)
    busy_known(fresh, SECOND, THIRD, probed_ago=1.0)
    call = ScriptCall("gemini-race-three", {})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race answer" and call.models_asked == ["gemini-race"]


def test_H2_a_model_skipped_is_not_stamped_as_asked(fresh):
    busy_known(fresh, FIRST, SECOND, probed_ago=60.0)
    before = fresh._probed(SECOND)
    target = fresh._wrap_target(ScriptCall("gemini-race-three", {}), THIRD, seen=False)
    assert target[0] == FIRST and fresh._probed(SECOND) == before   # SECOND was merely passed over


# --- the gate ("one ask per window") --------------------------------------------------

def test_one_ask_per_window_per_model(fresh):
    busy_known(fresh, FIRST)
    call = ScriptCall("gemini-race-three", {})
    assert fresh._wrap_target(call, THIRD, seen=False)[0] == FIRST
    assert fresh._wrap_target(call, THIRD, seen=False)[0] == SECOND   # FIRST was just asked


def test_H4_a_model_left_in_this_call_is_gated_even_after_its_window(fresh):
    busy_known(fresh, FIRST, probed_ago=1.0)
    fresh._probed_at[SECOND] = time.time() - 1.0      # asked and left a moment ago, window lapsed
    call = ScriptCall("gemini-race-three", {})
    assert fresh._wrap_target(call, THIRD, seen=False, visited=[SECOND]) is None
    assert fresh._wrap_target(call, THIRD, seen=False)[0] == SECOND   # not in this call: free


def test_L2_two_conversations_do_not_ask_the_same_model_in_the_same_instant(fresh):
    busy_known(fresh, FIRST)
    results = []

    def go():
        results.append(fresh._wrap_target(ScriptCall("gemini-race-three", {}), THIRD, seen=False))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for r in results if r and r[0] == FIRST) == 1


# --- "Set time", "Never", text on screen, background jobs -----------------------------

def test_M1_set_time_is_not_bypassed_by_a_circle(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "timed")
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "60")
    settings.forget()
    for key in R.KEYS:
        fresh.engine.mark(FIRST, key, False, 0.4, "per_minute")   # a short rest; SECOND is free
    call = ScriptCall("gemini-race", {})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race answer" and call.models_asked == ["gemini-race"]
    assert fresh.wrapped == 0


def test_nothing_circles_back_when_the_user_said_never_or_text_is_on_screen(fresh, monkeypatch):
    busy_known(fresh, FIRST)
    call = ScriptCall("gemini-race-three", {})
    assert fresh._wrap_target(call, THIRD, seen=True) is None
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    assert fresh._wrap_target(call, THIRD, seen=False) is None
    monkeypatch.delenv("KAME_MODEL_SWITCH")
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    assert fresh._wrap_target(call, THIRD, seen=False) is None


def test_a_background_job_never_circles_back(fresh):
    busy_known(fresh, FIRST)
    call = ScriptCall("gemini-race-three", {})
    call.channel = "aux"
    assert fresh._wrap_target(call, THIRD, seen=False) is None


# --- which models are siblings --------------------------------------------------------

def test_only_the_same_provider_and_endpoint_are_siblings(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: groq\n    model: llama-x\n"
        "  - provider: gemini\n    model: Gemini-Race-Elsewhere\n    base_url: https://elsewhere.example/v1\n"
        "  - provider: gemini\n    model: Gemini-Race-Three\n", encoding="utf-8")
    chain_mod.forget()
    assert fresh._siblings(FIRST) == [("gemini:gemini-race-three", "Gemini-Race-Three", "")]


def test_H3_an_unnamed_endpoint_matches_the_one_the_primary_names_both_ways(fresh, tmp_path):
    # The owner's configuration: the primary names its base_url, the fallbacks do not.
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\n  base_url: https://generativelanguage.example/v1\n"
        "fallback_providers:\n  - provider: gemini\n    model: gemini-race-two\n"
        "  - provider: gemini\n    model: gemini-race-three\n", encoding="utf-8")
    chain_mod.forget()
    assert [s[0] for s in fresh._siblings(THIRD)] == [FIRST, SECOND]      # the way back up reaches the primary
    assert [s[0] for s in fresh._siblings(FIRST)] == [SECOND, THIRD]


def test_an_unknown_model_or_no_list_means_no_siblings(fresh, tmp_path):
    assert fresh._siblings("gemini:not-in-the-list") is None
    (tmp_path / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-race\n",
                                          encoding="utf-8")
    chain_mod.forget()
    assert fresh._siblings(FIRST) is None


# --- a request the circled model refuses (review M3) and what the user sees (L1) ------

def test_M3_a_circled_model_that_refuses_the_request_shape_sends_the_call_back(fresh):
    busy_known(fresh, FIRST, probed_ago=60.0)
    call = ScriptCall("gemini-race-three", {"gemini-race-three": ["busy", "busy", "ok"], "gemini-race": "bad"})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-three answer"
    assert "gemini-race" in call.models_asked


def test_L1_the_circle_is_said_in_the_shape_the_desktop_lets_through(fresh, monkeypatch):
    lines = []
    monkeypatch.setattr(transport._Spinner, "update", staticmethod(lambda owner, text, **kw: lines.append(text)))
    fresh._announce_wrap(ScriptCall("gemini-race-three", {}), THIRD, FIRST, "is busy")
    assert lines and transport.passes_desktop_status_gate(lines[0])
