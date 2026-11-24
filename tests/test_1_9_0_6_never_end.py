"""1.9.0.6 (decisions/0037 C6) - every way Hermes can end a turn that KAME could cause, one test per row.

Hermes' endings (``agent/turn_failure_copy.py``) and the KAME path that could reach each. Where the request or the
user decides (a refused request, the user's own stop, a bound the user set) the test pins that a plain 503, 429 or
slow storm never gets there.
"""
from __future__ import annotations

import importlib
import os
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests.test_1_9_0_6_handover_budget import MIXED, PRIMARY, ask, fresh, rest_all, sb, transport  # noqa: F401

settings = R.settings
chain_mod = P.chain_mod


@pytest.fixture()
def one_provider(fresh, tmp_path):
    """The owner's shape: every model of the list on the same provider and endpoint."""
    (tmp_path / "config.yaml").write_text(P.CHAIN, encoding="utf-8")
    chain_mod.forget()
    return fresh


# --- rebuilt_restart_limit_exceeded: hand-overs past the limit --------------------------------------------------
def test_rebuilt_restart_limit_exceeded_kame_never_asks_hermes_for_its_last_switch(fresh):
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    raised = sum(1 for _ in range(12) if isinstance(ask(fresh, call), transport.KamePoolResting))
    assert raised == sb.allowed() == 2, "Hermes' limit is 3 restarts: KAME spends two and leaves one"


# --- all_retries_exhausted_no_response ----------------------------------------------------------------------------
def test_all_retries_exhausted_the_same_hand_over_is_never_raised_twice_in_a_row(fresh):
    """Hermes retries one model api_max_retries times; a hand-over it cannot act on must not repeat."""
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    fresh._handover_until.clear()
    first = fresh._maybe_hand_over(call, PRIMARY, R.KEYS, 0.0, False)
    again = [fresh._maybe_hand_over(call, PRIMARY, R.KEYS, 0.0, False) for _ in range(3)]
    assert isinstance(first, transport.KamePoolResting)
    assert again == [None, None, None], "the 30 s memo: three retries on one model, one hand-over"


def test_a_plain_503_storm_ends_in_an_answer_never_in_an_error(one_provider):
    """No refused-request raise, no waited-out bound, no cut: a storm of 503s on the first models is rotated
    through and the reply is the next model's."""
    call = P.Scripted(P.pool(), {"gemini-race": ("503",), "gemini-race-two": ("503",),
                                 "gemini-race-three": ("fast", 0.05)})
    out = list(one_provider.stream(call))
    assert R.shown(out) == "gemini-race-three here"


def test_a_rate_limit_storm_ends_in_an_answer_never_in_an_error(one_provider):
    # every key of the first model answers 429 (per minute), the sibling answers
    for key in R.KEYS:
        one_provider.engine.mark(PRIMARY, key, False, 30.0, "per_minute")
    call = P.Scripted(P.pool(), {"gemini-race-two": ("fast", 0.05)})
    assert R.shown(list(one_provider.stream(call))) == "gemini-race-two here"


def test_a_slow_storm_ends_in_an_answer_never_in_an_error(one_provider, monkeypatch):
    monkeypatch.setattr(transport, "_SLOW_S", {False: (0.5, 0.5, 0.5), True: (0.5, 0.5, 0.5)})
    call = P.Scripted(P.pool(), {"gemini-race-three": ("fast", 0.05)})
    out = list(one_provider.stream(call))
    assert R.shown(out).endswith("here") or R.shown(out) == "late"


def test_no_bound_ends_a_chat_reply_by_default(fresh):
    """``max_total_wait_seconds`` is 0 for a chat reply: KAME's own waited-out error cannot fire."""
    assert settings.total_wait_bound() == 0.0


# --- empty_response_exhausted ------------------------------------------------------------------------------------
def test_an_empty_200_is_passed_on_as_today_and_raises_nothing(fresh):
    """Advisory in Hermes (``fails_turn`` False) and never seen in the owner's logs: today's behaviour is pinned."""
    pool = R.Pool({k: [[0.01, R.chunk(None, finish="stop")]] for k in R.KEYS})
    out = list(fresh.stream(R.FakeCall(pool)))          # must not raise
    assert R.shown(out) == ""


# --- repeated_outer_errors / local_processing_error ---------------------------------------------------------------
def test_maybe_hand_over_returns_a_safe_value_when_its_own_code_fails(fresh, monkeypatch):
    rest_all(fresh, PRIMARY)
    monkeypatch.setattr(fresh, "_later", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert fresh._maybe_hand_over(R.FakeCall(P.pool()), PRIMARY, R.KEYS, 0.0, False) is None


def test_stand_in_for_returns_a_safe_value_when_its_own_code_fails(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "_siblings", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert fresh._stand_in_for(R.FakeCall(P.pool()), PRIMARY, [PRIMARY + "-x"], [], "busy", None, None) is None
    assert fresh._stand_in_for(R.FakeCall(P.pool()), PRIMARY, [PRIMARY + "-x"], [], "busy", None, None,
                               any_sibling=True) is None


def test_the_switch_budget_returns_safe_values_when_its_state_is_broken(monkeypatch):
    monkeypatch.setattr(sb, "_COUNTS", None)           # not even a dict
    assert sb.used("a") == 0
    assert sb.may_hand_over("a") is False
    sb.note_switch("a"); sb.note_answer("a"); sb.turn_started("a"); sb.reset_all()
    assert sb.conversation_key() in ("", )


def test_a_hand_over_with_a_broken_budget_waits_instead_of_raising(fresh, monkeypatch):
    rest_all(fresh, PRIMARY)
    monkeypatch.setattr(sb, "_COUNTS", None)
    assert ask(fresh, R.FakeCall(P.pool())) is None


# --- stream stale ------------------------------------------------------------------------------------------------
def test_the_keepalive_is_far_inside_hermes_stale_stream_timeout():
    assert transport.KameTransport.KEEPALIVE_S < 180.0 / 4


# --- all_retries_exhausted_no_response, the timeout path (C9) ----------------------------------------------------
from tests.h1906 import clock  # noqa: E402,F401


def test_repeated_timeouts_on_a_chat_reply_never_reach_hermes(clock):
    """The second timeout in a row used to hand the raw timeout to Hermes (three retries, then the error card)."""
    from tests.test_1_9_0_6_timeouts import Storm, StormCall, drive
    t, fake = clock
    result, _ = drive(t, StormCall(Storm(fake, 240.0)))
    assert result.choices[0].message.content == "ok" and t.surfaced == 0
