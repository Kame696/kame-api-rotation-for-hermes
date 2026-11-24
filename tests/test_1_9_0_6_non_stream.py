"""1.9.0.6 (decisions/0037 C19b) - a non-streaming call on a KAME client never gives up and never says "try again".

Owner, 2026-11-23: "Não pode ter tente de novo... Nem desistir." Hermes cuts a non-streaming call by wall clock (its
stale budget) and gives up after a streak; KAME moves that budget into itself (``core.nonstream_guard``): Hermes' timer
is disarmed (``inf``) only for a call KAME carries, and KAME applies the same budget to each provider request, so a
hung socket is still cut as fast as before and the reply waits as long as it takes.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from tests import test_1_8_2_9_aux_budget as B
from tests.test_1_8_2_9_aux_budget import FakeCall, Plan, fresh, rest_all  # noqa: F401  (fixture by name)

transport = B.transport
guard = importlib.import_module(f"{B.PACKAGE}.core.nonstream_guard")
facade = importlib.import_module(f"{B.PACKAGE}.facade")

DEV_VENV = Path.home() / "dev" / "hermes-venv" / "Scripts" / "python.exe"   # a local Hermes checkout and its venv, when present
DEV_CLONE = Path.home() / "dev" / "hermes-agent"
ONE = "model:\n  provider: gemini\n  default: gemini-3.8-flash\n"


class _KameClient(facade._KameMixin):
    """A client of KAME's shape (no network)."""


class _Agent:
    """The host's AIAgent, reduced to what the budget reads."""

    def __init__(self, client=None, api_mode="chat_completions", platform="desktop", explicit=False, base=90.0):
        self.client = client if client is not None else _KameClient()
        self.api_mode = api_mode
        self.platform = platform
        self._explicit = explicit
        self._base = base

    def _stale_timeout_is_explicit(self):
        return self._explicit

    def _compute_non_stream_stale_timeout(self, api_payload):
        return self._base


@pytest.fixture
def host(monkeypatch):
    """A ``run_agent`` module holding the host class the guard wraps; removed afterwards."""
    module = types.ModuleType("run_agent")
    module.AIAgent = _Agent
    monkeypatch.setitem(sys.modules, "run_agent", module)
    original = _Agent._compute_non_stream_stale_timeout
    yield module
    guard.uninstall()
    _Agent._compute_non_stream_stale_timeout = original
    guard._BUDGET.set(None)


def test_a_kame_call_gets_no_host_timer_and_kame_keeps_the_same_budget(host):
    assert guard.install() is True
    agent = _Agent()
    assert agent._compute_non_stream_stale_timeout({"messages": []}) == float("inf")
    assert guard.host_budget() == 90.0
    # the budget Hermes would have used is the leash of every provider request of that call
    assert transport.attempt_read_timeout("https://generativelanguage.googleapis.com/v1beta", 1,
                                          streaming=False) == 90.0
    # a slow (not hung) model still gets through: the leash doubles per attempt, capped at sixteen times
    assert [transport.attempt_read_timeout("https://x.example/v1", n, streaming=False) for n in (1, 2, 3, 5, 9)] \
        == [90.0, 180.0, 360.0, 1440.0, 1440.0]
    # a stream is not touched: it has keep-alives (same value with or without a recorded budget)
    url = "https://generativelanguage.googleapis.com/v1beta"
    with_budget = transport.attempt_read_timeout(url, 1, streaming=True)
    guard._BUDGET.set(None)
    assert transport.attempt_read_timeout(url, 1, streaming=True) == with_budget


@pytest.mark.parametrize("agent", [
    _Agent(client=object()),                    # not KAME's client
    _Agent(platform="cron"),                    # Hermes' own job timeout is the bound there
    _Agent(explicit=True),                      # the user's own deadline stands
    _Agent(api_mode="codex_responses"),         # Codex has its own watchdogs
], ids=["other client", "cron", "explicit", "codex"])
def test_everything_else_keeps_hermes_own_budget(host, agent):
    guard.install()
    assert agent._compute_non_stream_stale_timeout({}) == 90.0
    assert guard.host_budget() is None


def test_kame_switched_off_keeps_hermes_own_budget(host, monkeypatch):
    guard.install()
    monkeypatch.setattr(facade, "enabled", lambda: False)
    assert _Agent()._compute_non_stream_stale_timeout({}) == 90.0


def test_installed_once_and_removed_cleanly(host):
    original = _Agent._compute_non_stream_stale_timeout
    assert guard.install() and guard.install()
    assert _Agent._compute_non_stream_stale_timeout.__wrapped__ is original
    guard.uninstall()
    assert _Agent._compute_non_stream_stale_timeout is original


def test_a_host_without_the_method_is_left_alone(monkeypatch):
    module = types.ModuleType("run_agent")
    module.AIAgent = type("AIAgent", (), {})
    monkeypatch.setitem(sys.modules, "run_agent", module)
    assert guard.install() is False


def test_a_broken_host_value_is_returned_unchanged(host):
    guard.install()
    agent = _Agent(base=float("inf"))
    assert agent._compute_non_stream_stale_timeout({}) == float("inf")
    assert guard.host_budget() is None


@pytest.fixture
def _one_model(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(ONE, encoding="utf-8")
    B.settings.forget()
    B.chain_mod.forget()
    yield


def test_a_non_streaming_reply_waits_an_hour_and_answers_never_a_503(fresh, _one_model):
    """The 1.9.0.6 C19 path is gone: no ``KameRetryLater``, no 503, no give-up - the reply waits for the keys."""
    assert not hasattr(transport, "KameRetryLater")
    rest_all(fresh, 3600.0)
    plan = Plan(["ok"], fresh.clock)
    started = fresh.clock.now
    out = fresh.complete(FakeCall(plan))
    assert out.choices[0].message.content == "ok"
    assert fresh.clock.now - started >= 3590.0


@pytest.mark.skipif(not (DEV_VENV.exists() and DEV_CLONE.exists()), reason="the dev clone of Hermes is not on this machine")
def test_hermes_treats_an_infinite_budget_as_no_timer_on_both_paths():
    """Host facts the guard relies on, read from the real code (dev clone): no timer, no socket cap, no notice crash."""
    script = textwrap.dedent(f"""
        import sys, math
        sys.path.insert(0, {str(DEV_CLONE)!r})
        from agent import chat_completion_helpers as h
        from agent import chat_completion_wait_notice as wn
        class A:
            api_mode = "chat_completions"; base_url = "https://example.invalid/v1"
            def _compute_non_stream_stale_timeout(self, kw): return float("inf")
            def _touch_activity(self, *a, **k): pass
        a = A()
        assert h._resolve_direct_stale_timeout(a, {{}}) == float("inf")
        assert h._inline_nonstream_hard_timeout(float("inf")) is None
        r = h._InlineRequest(a, {{}}, float("inf"), 0.0)
        r.start_watchdogs(); assert r._watchdog is None; r.stop_watchdogs()
        assert wn.codex_watchdog_deadline(stale_timeout=float("inf"), ttfb_enabled=False, ttfb_timeout=120.0,
            last_event_ts=None, last_progress_ts=None, retry_started_ts=None, call_start=0.0, idle_enabled=False,
            idle_timeout=0.0, idle_requires_progress=False, elapsed=5000.0) is None
        import inspect
        src = inspect.getsource(h._resolve_nonstream_watchdogs)
        assert "agent._compute_non_stream_stale_timeout(api_kwargs)" in src
        import agent.chat_completion_nonstream as ns
        assert "elapsed > wd.stale_timeout" in inspect.getsource(ns._NonStreamRequest.run)
        assert "copy_context" in inspect.getsource(h._context_thread_target)
        print("INF-BUDGET-OK")
    """)
    done = subprocess.run([str(DEV_VENV), "-B", "-c", script], capture_output=True, text=True, timeout=180)
    assert "INF-BUDGET-OK" in done.stdout, done.stdout + done.stderr[-2000:]


@pytest.mark.skipif(not (DEV_VENV.exists() and DEV_CLONE.exists()), reason="the dev clone of Hermes is not on this machine")
def test_no_kame_error_trips_the_hosts_streaming_switch():
    cases = []
    for exc in (transport.KamePoolResting("every key rests", 1e12),
                transport.KameAuxTimeout("a background job stopped"),
                transport.KameAuxStopped("a background job stopped moments ago")):
        cases.append({"text": str(exc), "body": getattr(exc, "body", None)})
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(DEV_CLONE)!r})
        from agent.chat_completion_helpers import _is_provider_stream_empty_frame_error
        from agent.anthropic_adapter import _is_stream_unavailable_error
        class E(Exception):
            pass
        for case in json.loads({json.dumps(cases)!r}):
            e = E(case["text"]); e.body = case["body"]
            assert not _is_provider_stream_empty_frame_error(e), case
            assert not _is_stream_unavailable_error(e), case
        print("STREAM-SWITCH-OK")
    """)
    done = subprocess.run([str(DEV_VENV), "-B", "-c", script], capture_output=True, text=True, timeout=120)
    assert "STREAM-SWITCH-OK" in done.stdout, done.stdout + done.stderr[-1500:]


def test_loading_the_plugin_never_imports_hermes_agent_module(monkeypatch):
    """Review fix: ``import run_agent`` costs up to a minute; the guard only wraps a module Hermes already loaded."""
    monkeypatch.delitem(sys.modules, "run_agent", raising=False)
    imported = []
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def spy(name, *args, **kwargs):
        if name == "run_agent":
            imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", spy)
    assert guard.install() is False
    monkeypatch.setattr(facade, "_GUARD_TRIES", [0])
    facade._ensure_guards()
    assert imported == [] and facade._GUARD_TRIES == [0], "nothing imported, nothing counted"


def test_the_turn_start_puts_the_guard_in_place(host, monkeypatch):
    monkeypatch.setattr(facade, "_GUARD_TRIES", [0])
    plugin = importlib.import_module(B.PACKAGE)
    plugin._on_turn_start(session_id="s1")
    assert _Agent()._compute_non_stream_stale_timeout({}) == float("inf")
