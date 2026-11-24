"""1.9.0.6 (decisions/0037 C4) - the hand-over text is short: it lands verbatim inside Hermes' own notice."""
from __future__ import annotations

import importlib
import sys

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_9_0_6_handover_budget import (  # noqa: F401  (fixtures and helpers)
    PRIMARY, ask, events, fresh, rest_all, transport)

pkg = sys.modules[R.PACKAGE]
M = "gemini-race"


def message(t, **kw):
    out = ask(t, R.FakeCall(R.Pool({})), **kw)
    assert isinstance(out, transport.KamePoolResting), out
    return str(out)


def test_skipped(fresh):
    assert message(fresh, busy=True, skipped=5.0) == f"KAME: {M} was slow a moment ago"


def test_slow(fresh):
    assert message(fresh, busy=True, slow=45.0) == f"KAME: {M} is slow right now"


def test_stalled(fresh):
    assert message(fresh, busy=True, stalled=30.0) == f"KAME: {M} stopped mid-answer"


def test_busy(fresh):
    assert message(fresh, busy=True) == f"KAME: {M} is busy"


def test_every_key_resting(fresh):
    rest_all(fresh, PRIMARY, seconds=3600.0)
    text = message(fresh)
    assert text.startswith(f"KAME: every key of {M} is resting (back in "), text
    assert "Handing" not in text


def test_no_answer(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "timed")
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "1")
    transport.settings.forget()
    assert message(fresh, after_failure=True, waited=5.0) == f"KAME: no answer from {M} after 5s"


@pytest.mark.parametrize("kw", [dict(busy=True, skipped=5.0), dict(busy=True, slow=45.0),
                                dict(busy=True, stalled=30.0), dict(busy=True)])
def test_no_message_names_handing_or_the_pool_suffix(fresh, kw):
    text = message(fresh, **kw) if False else str(ask(fresh, R.FakeCall(R.Pool({})), PRIMARY + ":global", **kw))
    assert "Handing" not in text and ":global" not in text and "fallback model" not in text, text
    assert text.startswith("KAME: ")


def test_the_event_row_keeps_its_longer_reason(fresh, events):
    message(fresh, busy=True, slow=45.0)
    reasons = [r for k, r in events if k == "handover"]
    assert reasons and "no first word after" in reasons[0] and "Hermes answers with" in reasons[0], reasons


def test_the_classification_hook_still_reads_it_as_an_upstream_rate_limit(fresh):
    exc = ask(fresh, R.FakeCall(R.Pool({})), busy=True, slow=45.0)
    result = pkg._on_api_error_classification(provider="gemini", model="gemini-race", status_code=429,
                                              error_message=str(exc), error=exc, error_body=exc.body)
    assert result["reason"] == "upstream_rate_limit" and result["should_fallback"] is True
