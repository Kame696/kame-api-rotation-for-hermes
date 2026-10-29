"""1.8.2.9 / decisions/0032 C - ask the busy best model again at the call start, as a race leg.

D8 hands a reply that starts inside its model's busy window to the next model without a request, so a
recovered best model went unnoticed for up to 5 minutes. Now, when that model is DUE (20 s since it
was last asked), the reply starts on the next model at t = 0 exactly as D8 would have, and the best
model runs BESIDE it: first sign of work wins. Added latency: zero.
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
PRIMARY = "gemini:gemini-race"
SECOND = "gemini:gemini-race-two"
SILENT = 3.0


class Refusal(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"message": message}}


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
    monkeypatch.setattr(transport, "_SLOW_S", {False: (2.0, 2.0, 2.0), True: (2.0, 2.0, 2.0)})
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


def busy_and_due(t, entry="gemini:gemini-race", ago=60.0):
    t._note_busy(entry)
    for name in (entry,):
        t._probed_at[name] = time.time() - ago


class Scripted(R.FakeCall):
    """Each model: ('fast', delay) answers; ('503',) raises overloaded; anything else is silent."""

    def __init__(self, pool, script):
        super().__init__(pool)
        self.script = dict(script)
        self.models_asked = []
        self.asked_at = {}

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            self.models_asked.append(model)
            self.asked_at.setdefault(model, time.monotonic())
            how = self.script.get(model, ("silent",))
            if how[0] == "503":
                raise Refusal(503, "The model is overloaded. Please try again later.")
            if how[0] == "fast":
                return R.FakeStream([how[1], R.chunk(f"{model} "), R.chunk("here", finish="stop")])
            return self.pool.open(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def pool():
    return R.Pool({k: [[SILENT, R.chunk("late", finish="stop")]] * 12 for k in R.KEYS})


# --- the rule ---------------------------------------------------------------------------------------
def test_not_due_hands_over_without_a_request(fresh):
    fresh._note_busy(PRIMARY)                      # a fresh window is never due
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    # 1.8.3.0 (D2 a): the next model is on the same provider, so KAME answers with it itself and
    # Hermes is not asked to switch; the busy model still gets no request.
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked == ["gemini-race-two"], "no request went to the busy model"
    assert fresh.handed_over == 0
    assert fresh._start_probe_plan(call, PRIMARY, True, R.KEYS) is None


def test_a_due_primary_is_asked_beside_the_next_model(fresh):
    busy_and_due(fresh)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    out = list(fresh.stream(call))
    assert R.shown(out).endswith("here")
    assert set(call.models_asked) == {"gemini-race", "gemini-race-two"}
    assert abs(call.asked_at["gemini-race"] - call.asked_at["gemini-race-two"]) < 0.15, "both go out at once"


def test_the_probe_never_delays_the_reply(fresh):
    busy_and_due(fresh)
    started = time.monotonic()
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert time.monotonic() - started < 0.8, "the next model answered at once; the silent primary cost nothing"


def test_a_recovered_primary_answers_and_its_window_closes(fresh):
    busy_and_due(fresh)
    call = Scripted(pool(), {"gemini-race": ("fast", 0.05), "gemini-race-two": ("silent",)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race here"
    assert getattr(call, "_kame_answered_by", None) == PRIMARY or getattr(call, "_kame_answered_by", None) == PRIMARY
    assert not fresh._entry_busy("gemini:gemini-race"), "the best model is back after one answer"
    assert not fresh._entry_busy("gemini:gemini-race-two"), "losing a race is not being slow"
    assert fresh.handed_over == 0, "the reply stayed on the model Hermes asked for"


def test_a_lost_race_leaves_the_window_as_it_was(fresh):
    busy_and_due(fresh)
    window = fresh._busy_window[PRIMARY]
    until = fresh._busy_until[PRIMARY]
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(call))
    assert fresh._busy_window[PRIMARY] == window, "a silent probe does not double the window"
    assert fresh._busy_until[PRIMARY] == until


def test_a_probe_503_doubles_the_window_and_the_reply_is_unharmed(fresh):
    busy_and_due(fresh)
    window = fresh._busy_window[PRIMARY]
    call = Scripted(pool(), {"gemini-race": ("503",), "gemini-race-two": ("fast", 0.4)})
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    fresh._drain_hedge_losers()
    assert fresh._busy_window.get(PRIMARY, 0) >= window * 2 - 0.01, fresh._busy_window


# --- when it does not run -------------------------------------------------------------------------
def test_no_probe_in_saver(fresh, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    busy_and_due(fresh)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    out = list(fresh.stream(call))                           # 1.8.3.0 (D2 a): the sibling stands in
    assert R.shown(out) == "gemini-race-two here"
    assert call.models_asked == ["gemini-race-two"], "no probe: the busy model is not asked"


def test_no_probe_for_a_non_stream_call(fresh):
    busy_and_due(fresh)
    assert fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, False, R.KEYS) is None
    assert fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, True, R.KEYS) is not None


def test_no_probe_for_a_background_job(fresh):
    busy_and_due(fresh)
    call = R.FakeCall(pool())
    call.channel = "secondary"
    assert fresh._start_probe_plan(call, PRIMARY, True, R.KEYS) is None


def test_no_probe_when_the_user_said_never_to_switch(fresh, monkeypatch):
    busy_and_due(fresh)
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    assert fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, True, R.KEYS) is None


def test_no_probe_when_the_first_free_entry_is_another_provider(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: groq\n    model: llama-x\n  - provider: gemini\n    model: gemini-race-two\n",
        encoding="utf-8")
    chain_mod.forget()
    busy_and_due(fresh)
    assert fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, True, R.KEYS) is None


# --- cadence -------------------------------------------------------------------------------------
def test_replies_close_together_probe_at_most_every_twenty_seconds(fresh):
    busy_and_due(fresh)
    first = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(first))
    assert "gemini-race" in first.models_asked, "the first reply asked the primary beside the next model"
    fresh._note_busy(PRIMARY)                       # still busy after the probe lost... a new window
    second = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(second))                              # not due again: the sibling alone (1.8.3.0)
    assert second.models_asked == ["gemini-race-two"], "5 seconds later the primary is not asked again"


def test_two_conversations_probe_once(fresh):
    busy_and_due(fresh)
    plans = []

    def ask():
        plans.append(fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, True, R.KEYS))

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sum(1 for p in plans if p is not None) == 1


# --- review of 2026-10-24 ------------------------------------------------------------------------
def test_the_activity_names_the_probe_and_does_not_call_the_home_model_late(fresh, monkeypatch):
    seen = []
    monkeypatch.setattr(transport, "_publish", lambda binding, activity=None: seen.append(activity))
    busy_and_due(fresh)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.3)})
    list(fresh.stream(call))
    during = [a for a in seen if a and a.get("probe")]
    assert during, "the panel is told a busy model is being asked again"
    assert during[0]["probe"] == "gemini-race"
    assert "gemini-race" not in (during[0].get("racing") or []), "the probe is not listed as a raced model"


def test_the_probe_marker_is_cleared_once_the_race_is_decided(fresh):
    busy_and_due(fresh)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    list(fresh.stream(call))
    assert getattr(call, "_kame_probe_id", None) is None


def test_a_missing_model_name_loses_no_probe_time(fresh):
    busy_and_due(fresh)
    call = Scripted(pool(), {"gemini-race-two": ("fast", 0.05)})
    call.kwargs = {k: v for k, v in call.kwargs.items() if k != "model"}
    before = fresh._probed_at.get("gemini:gemini-race")
    list(fresh.stream(call))                                # no name to ask it by: no probe; the sibling answers
    assert call.models_asked == ["gemini-race-two"]
    assert fresh._probed_at.get("gemini:gemini-race") == before, "the 20 s clock was not spent on nothing"
