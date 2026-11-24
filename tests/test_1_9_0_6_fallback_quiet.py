"""1.9.0.6 (decisions/0037 C4b) - no "⚠️ Model fallback" line in the chat for a switch KAME asked for.

Owner, 2026-11-23: "no 1.9.0.6 acho que não deve aparecer essa notificação. O status bar ... deve ser o suficiente".
A switch KAME hands to Hermes is held the way Hermes holds one on a background wake turn; every other notice shows.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from tests import test_1_8_2_9_aux_budget as B

quiet = importlib.import_module(f"{B.PACKAGE}.core.fallback_quiet")
transport = B.transport

DEV_VENV = Path.home() / "dev" / "hermes-venv" / "Scripts" / "python.exe"   # a local Hermes checkout and its venv, when present
DEV_CLONE = Path.home() / "dev" / "hermes-agent"
KAMES = ("⚠️ Model fallback: gemini-4 via gemini could not respond (upstream model rate limit: KAME: gemini-4 is "
         "busy); using gpt-5 via openai.")
REAL = ("⚠️ Model fallback: gemini-4 via gemini could not respond (provider server error: HTTP 500 internal); "
        "using gpt-5 via openai.")


@pytest.fixture
def host(monkeypatch):
    shown = []
    module = types.ModuleType("agent.fallback_notice")
    module.announce_fallback_switch = lambda agent, notice: shown.append(notice)
    package = types.ModuleType("agent")
    package.fallback_notice = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.fallback_notice", module)
    yield module, shown
    quiet.uninstall()


def test_a_switch_kame_asked_for_is_held_not_posted(host):
    module, shown = host
    assert quiet.install() is True
    agent = types.SimpleNamespace(_hidden_fallback_notice=None)
    module.announce_fallback_switch(agent, KAMES)
    assert shown == [] and agent._hidden_fallback_notice == KAMES


def test_a_real_failure_is_still_shown(host):
    module, shown = host
    quiet.install()
    module.announce_fallback_switch(types.SimpleNamespace(), REAL)
    assert shown == [REAL]


def test_installed_once_and_removed_cleanly(host):
    module, _ = host
    original = module.announce_fallback_switch
    assert quiet.install() and quiet.install()
    assert module.announce_fallback_switch.__wrapped__ is original
    quiet.uninstall()
    assert module.announce_fallback_switch is original


def test_every_hand_over_text_carries_the_mark():
    """The texts ``_maybe_hand_over`` builds (decisions/0037 C4) all start with the mark the filter reads."""
    import re
    source = Path(transport.__file__).read_text(encoding="utf-8")
    shorts = re.findall(r'short = f"(KAME: [^"]*)"', source)
    assert len(shorts) >= 6 and all(s.startswith(quiet.MARK) for s in shorts), shorts


@pytest.mark.skipif(not (DEV_VENV.exists() and DEV_CLONE.exists()), reason="the dev clone of Hermes is not on this machine")
def test_hermes_calls_the_module_attribute_and_honours_a_held_notice():
    """Host facts, from the real code: the switch imports the function at call time; a held notice suppresses the
    restored line and is re-offered (held again) at the next turn."""
    pkg = Path(B.transport.__file__).resolve().parent
    script = textwrap.dedent(f"""
        import sys, types, importlib, inspect
        sys.path.insert(0, {str(DEV_CLONE)!r})
        sys.path.insert(0, {str(pkg.parent)!r})
        from agent import chat_completion_helpers as h, fallback_notice as fn
        src = inspect.getsource(h.try_activate_fallback)
        assert "from agent.fallback_notice import announce_fallback_switch" in src
        assert "announce_fallback_switch(agent, notice)" in src
        q = importlib.import_module({pkg.name!r} + ".core.fallback_quiet")
        assert q.install()
        emitted = []
        class A:
            _turn_display_kind = None; _hidden_fallback_notice = None; _provider_fallback_active = True
            model = "m"; provider = "p"
            def _emit_status_kind(self, *a, **k): emitted.append(a)
            def _emit_diagnostic_status(self, text): emitted.append(text)
        a = A()
        fn.announce_fallback_switch(a, {KAMES!r})
        assert emitted == [] and a._hidden_fallback_notice
        fn.surface_hidden_fallback(a)
        assert emitted == [] and a._hidden_fallback_notice, "re-offered at the next turn and held again"
        fn.announce_primary_restored(a, "old", "prov")
        assert emitted == [], "no 'Primary model restored' for a switch nobody saw"
        fn.announce_fallback_switch(a, {REAL!r})
        assert len(emitted) == 1
        print("QUIET-OK")
    """)
    done = subprocess.run([str(DEV_VENV), "-B", "-c", script], capture_output=True, text=True, timeout=180)
    assert "QUIET-OK" in done.stdout, done.stdout + done.stderr[-2500:]
