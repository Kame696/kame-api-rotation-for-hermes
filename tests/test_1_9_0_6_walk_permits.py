"""1.9.0.6 (decisions/0037 C15) - walk permits are per conversation."""
from __future__ import annotations

import contextvars
import importlib
import os

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_9_0_6_switch_budget as SBT

transport, settings, chain_mod = R.transport, R.settings, P.chain_mod

CHAIN = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: groq
    model: llama-x
  - provider: openai
    model: gpt-z
"""
X = "groq:llama-x"


@pytest.fixture()
def host(monkeypatch, tmp_path):
    sid, _ = SBT._install_session_context(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    transport._HOST_WALK_PERMITS.clear()
    yield sid
    transport._HOST_WALK_PERMITS.clear()
    chain_mod.forget()


def as_session(sid_var, name, fn, *a):
    ctx = contextvars.copy_context()
    ctx.run(sid_var.set, name)
    return ctx.run(fn, *a)


def test_a_failure_in_one_conversation_does_not_change_anothers_origin(host):
    as_session(host, "A", transport.note_host_fallback, "groq", "llama-x")        # A's reply fell back from X
    assert as_session(host, "A", transport._permit_origin, "openai:gpt-z", transport._HOST_WALK_PERMITS) == X
    assert as_session(host, "B", transport._permit_origin, "openai:gpt-z", transport._HOST_WALK_PERMITS) is None


def test_the_unnamed_key_keeps_todays_shared_behaviour(host):
    transport.note_host_fallback("groq", "llama-x")
    assert "openai:gpt-z" in transport._HOST_WALK_PERMITS            # the plain key
    assert transport._permit_origin("openai:gpt-z", transport._HOST_WALK_PERMITS) == X
    assert as_session(host, "B", transport._permit_origin, "openai:gpt-z", transport._HOST_WALK_PERMITS) == X


def test_a_named_conversation_writes_its_own_key(host):
    as_session(host, "A", transport.note_host_fallback, "groq", "llama-x")
    assert ("A", "openai:gpt-z") in transport._HOST_WALK_PERMITS and "openai:gpt-z" not in transport._HOST_WALK_PERMITS
