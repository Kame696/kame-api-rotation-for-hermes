"""1.9.0.6 (owner's live test, 2026-11-23) - Hermes has room to switch to every model of the list while KAME carries
the reply, with no setting of the user's (1.9.0.5 only reached a long list because the owner set api_max_retries: 5)."""
from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

from tests import test_1_8_2_5_stream_race as R

PKG = R.PACKAGE
room_mod = importlib.import_module(f"{PKG}.core.switch_room")
budget = importlib.import_module(f"{PKG}.core.switch_budget")
chain_mod = importlib.import_module(f"{PKG}.core.fallback_chain")
facade = importlib.import_module(f"{PKG}.facade")
nonstream = importlib.import_module(f"{PKG}.core.nonstream_guard")

FIVE = """\
model:
  provider: gemini
  default: gemini-4-hydrogen
fallback_providers:
  - provider: gemini
    model: gemini-3.8-flash
  - provider: openai
    model: gpt-x
  - provider: gemini
    model: gemini-3.6-flash
  - provider: groq
    model: llama-x
"""


class AIAgent:
    def __init__(self, retries=3, client=None, platform="desktop"):
        self._api_max_retries = retries          # what Hermes' agent_init does
        self.client = client
        self.platform = platform


@pytest.fixture()
def host(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(FIVE, encoding="utf-8")
    chain_mod.forget()
    module = types.ModuleType("run_agent")
    module.AIAgent = AIAgent
    monkeypatch.setitem(sys.modules, "run_agent", module)
    monkeypatch.setattr(facade, "enabled", lambda: True)
    kame_client = object()
    monkeypatch.setattr(nonstream, "_is_kame_client", lambda c: c is kame_client)
    room_mod.uninstall()
    yield types.SimpleNamespace(cls=AIAgent, kame=kame_client, home=tmp_path)
    room_mod.uninstall()
    chain_mod.forget()


def test_room_is_every_model_plus_the_one_kame_keeps(host):
    assert room_mod.room() == 6


def test_a_kame_agent_gets_the_room_and_hermes_own_value_is_kept(host):
    assert room_mod.install()
    agent = AIAgent(3, client=host.kame)
    assert agent._api_max_retries == 6
    assert agent.__dict__["_api_max_retries"] == 3, "Hermes' own value is where Hermes put it"
    room_mod.uninstall()
    assert agent._api_max_retries == 3


def test_an_agent_made_before_the_install_gets_it_too(host):
    agent = AIAgent(3, client=host.kame)
    room_mod.install()
    assert agent._api_max_retries == 6


def test_a_larger_user_value_stands(host):
    room_mod.install()
    assert AIAgent(9, client=host.kame)._api_max_retries == 9


def test_cron_or_kame_off_keeps_hermes_value_and_a_hop_to_another_client_keeps_the_room(host, monkeypatch):
    room_mod.install()
    assert AIAgent(3, client=object())._api_max_retries == 6, "a hop to a provider KAME does not carry keeps the room"
    assert AIAgent(3, client=host.kame, platform="cron")._api_max_retries == 3
    monkeypatch.setattr(facade, "enabled", lambda: False)
    assert AIAgent(3, client=host.kame)._api_max_retries == 3


def test_no_list_no_room(host):
    (host.home / "config.yaml").write_text("model:\n  provider: gemini\n  default: g\n", encoding="utf-8")
    chain_mod.forget()
    room_mod.install()
    assert AIAgent(3, client=host.kame)._api_max_retries == 3


def test_the_budget_counts_the_room_so_every_provider_hop_is_reached(host):
    assert budget.allowed() == 2, "stock Hermes, no room: two hand-overs"
    room_mod.install()
    assert budget.allowed() == 5, "with the room: one per model after the first, plus one spare"


def test_install_never_imports_hermes_and_is_idempotent(host, monkeypatch):
    monkeypatch.delitem(sys.modules, "run_agent")
    assert room_mod.install() is False
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=AIAgent))
    assert room_mod.install() and room_mod.install()
    assert AIAgent(3, client=host.kame)._api_max_retries == 6
