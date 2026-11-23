"""1.9.0.6 (decisions/0037 C3) - a slow or stalled reply is circled to a free sibling inside KAME, not handed to Hermes.

2026-11-23: five Gemini models on one endpoint, Google 503 on all of them. Every slow moment at the race's slow
bound raised ``KamePoolResting`` (the slow path never offered a stand-in) and Hermes ended the turn at its fourth
switch. Now a sibling on the same provider and endpoint answers inside KAME.

Real time, real threads; the constants are scaled to fractions of a second as in test_1_8_2_9_silent_two_models.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_silent_two_models as T
from tests.h1901 import SeqCall, answer, run, setup, stall, think  # noqa: F401
from tests.test_1_8_4_3_quiet_guard import guard  # noqa: F401  (the fixture of the stall tests)

transport = R.transport
settings = R.settings
chain_mod = T.chain_mod

SIX = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: gemini
    model: gemini-race-three
  - provider: gemini
    model: gemini-race-four
  - provider: gemini
    model: gemini-race-five
  - provider: gemini
    model: gemini-race-six
"""
FOUR = SIX.split("  - provider: gemini\n    model: gemini-race-five")[0]
SILENT = T.SILENT


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(SIX, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    monkeypatch.setattr(transport, "_SLOW_S", {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


def silent_pool(seconds=SILENT):
    return T.silent_pool(seconds)


# --- S4: the slow bound ----------------------------------------------------------------------------------
def test_a_one_provider_list_circles_to_a_free_sibling_and_hermes_hears_nothing(fresh):
    call = T.ByModel(silent_pool(), quick={"gemini-race-five": 0.05})
    out = list(fresh.stream(call))                      # no KamePoolResting escapes
    assert R.shown(out) == "fast answer"
    assert "gemini-race-five" in call.models_asked
    assert call.models_asked[-1] == "gemini-race-five", "the next attempt is on the sibling"
    assert getattr(call, "_kame_answered_by") == "gemini:gemini-race-five"
    assert fresh.handed_over == 0, "no Hermes switch was spent"
    assert fresh.replies and fresh.replies[-1]["model"] == "gemini-race-five"


def test_the_silent_models_are_left_alone_after_a_circle(fresh):
    call = T.ByModel(silent_pool(), quick={"gemini-race-five": 0.05})
    list(fresh.stream(call))
    assert fresh._entry_busy("gemini:gemini-race"), "the home model was silent past the bound: noted slow"
    assert all(s.response.closed.is_set() for _, s in call.pool.opened), "every silent leg was cut"


def test_every_sibling_already_racing_keeps_waiting_as_in_0032_B3(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(FOUR, encoding="utf-8")
    chain_mod.forget()
    call = T.ByModel(silent_pool(2.2))
    out = list(fresh.stream(call))
    assert R.shown(out) == "late"
    assert fresh.handed_over == 0
    assert len(call.models_asked) <= 4, call.models_asked          # nothing outside the race was asked


def test_a_budget_spent_conversation_still_circles_to_a_sibling(fresh):
    from tests.test_1_9_0_6_handover_budget import sb      # the same module the transport imports
    sb.note_switch(""); sb.note_switch("")
    call = T.ByModel(silent_pool(), quick={"gemini-race-five": 0.05})
    out = list(fresh.stream(call))
    assert R.shown(out) == "fast answer" and fresh.handed_over == 0


def test_text_already_shown_never_circles(fresh):
    # a stand-in is only offered while nothing has reached the screen
    call = R.FakeCall(R.Pool({}))
    assert fresh._maybe_hand_over(call, R.IDENTITY, R.KEYS, 0.0, True, busy=True, slow=5.0,
                                  stand_in_ok=True) is None


def test_kame_circle_never_escapes_drive(fresh):
    call = T.ByModel(silent_pool(), quick={"gemini-race-five": 0.05})
    seen_types = []
    try:
        for item in fresh._drive(call, stream=True):
            seen_types.append(type(item).__name__)
    except BaseException as exc:  # noqa: BLE001
        seen_types.append(type(exc).__name__)
    assert "_KameCircle" not in seen_types
    assert not any(isinstance(x, str) and "KamePoolResting" in x for x in seen_types)


def test_kame_circle_is_not_control_flow_and_is_not_a_hermes_exception():
    assert issubclass(transport._KameCircle, Exception)
    assert transport._KameCircle not in transport._CONTROL_FLOW
    stand = transport._StandIn("gemini:x", "x", "is slow right now")
    assert transport._KameCircle(stand).stand is stand


def test_a_circled_call_is_not_closed_as_a_failure(fresh, monkeypatch):
    rows = []
    real = transport.timings.record
    monkeypatch.setattr(transport.timings, "record", lambda **kw: (rows.append(kw), real(**kw))[1])
    call = T.ByModel(silent_pool(), quick={"gemini-race-five": 0.05})
    list(fresh.stream(call))
    assert not [r for r in rows if r.get("record_type") == "close"], "a circle is not an outcome: no closing row"


# --- keep-alive: Hermes never goes quiet while KAME circles --------------------------------------------------
def test_hermes_hears_something_at_least_every_keepalive_while_circling(fresh, monkeypatch):
    monkeypatch.setattr(transport.KameTransport, "KEEPALIVE_S", 0.4)
    call = T.ByModel(silent_pool(), scripts={"gemini-race-five": [2.0, R.chunk("late "), R.chunk("word", finish="stop")]})
    stamps = [time.monotonic()]
    for _ in fresh.stream(call):
        stamps.append(time.monotonic())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) > 6, "keep-alive chunks flowed"
    assert max(gaps) < 0.4 + 0.5, max(gaps)


# --- S5: a reply stalled mid-thought -------------------------------------------------------------------------
def test_a_stalled_reply_circles_to_a_free_sibling(guard, tmp_path):
    setup(tmp_path, """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: gemini
    model: gemini-race-three
""")
    call = SeqCall(by_model={"gemini-race": [stall(), stall()], "gemini-race-two": [stall(), stall()],
                             "gemini-race-three": [answer("third")]})
    res = run(guard, call, cap=8.0)
    assert res.err is None, res.err
    assert res.shown == "third done"
    assert guard.handed_over == 0
    assert getattr(call, "_kame_answered_by", None) == "gemini:gemini-race-three"
