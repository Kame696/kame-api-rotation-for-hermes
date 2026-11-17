"""1.9.0.2 (H2) - "Reset pool" says what failed, by name, and still clears everything else.

Live (audit/2026-11-15-pre-1902-bug-hunt.md, F3): every press since 2026-10-19 (8 of 8) answered
"pool reset incomplete: host marks; retry after checking storage access". The step that clears Hermes'
own ``last_status: exhausted`` marks raised, and ``control._clear_benches_on_disk`` logged why at DEBUG
only, so neither the owner nor the log could say which provider failed or how. A dry run of the host's
own ``reset_statuses`` with writes blocked passed for all 9 providers, so the cause lives in the real
write; this release makes the next press name it.

Pinned: a failing provider is named with its exception type in the panel's answer and in a WARNING
line; the others are still reset; nothing that looks like a secret reaches the log; the "storage
access" advice appears only when the failure is a storage error.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_1_9_0_2_reset_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
control = importlib.import_module(f"{PACKAGE}.control")

SECRET = "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"


class _Pool:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def reset_statuses(self):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture()
def host(monkeypatch):
    """A fake host credential store: one provider fails as given, one resets two marks."""
    pools = {}

    def install(failure):
        pools.clear()
        pools["gemini"] = _Pool(failure)
        pools["nvidia"] = _Pool(2)
        auth = types.ModuleType("hermes_cli.auth")
        auth.read_credential_pool = lambda provider_id=None: {name: [{}] for name in pools}
        cp = types.ModuleType("agent.credential_pool")
        cp.load_pool = lambda provider: pools[provider]
        monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
        monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.credential_pool", cp)
        return pools

    return install


def test_a_failing_provider_is_named_with_its_error_type(host):
    pools = host(TimeoutError(f"Timed out waiting for auth store lock (C:/h/auth.lock); held by {SECRET}"))
    done, failed = control._clear_benches_on_disk()
    assert any("gemini" in f and "TimeoutError" in f for f in failed), failed
    assert pools["nvidia"].calls == 1
    assert "2 host marks" in done


def test_the_failure_is_logged_at_warning_without_a_secret(host, caplog):
    host(TimeoutError(f"Timed out waiting for auth store lock; held by {SECRET}"))
    with caplog.at_level(logging.DEBUG):
        control._clear_benches_on_disk()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("gemini" in r.getMessage() and "TimeoutError" in r.getMessage() for r in warnings)
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_the_panel_answer_names_the_provider_and_type_not_a_guess(host):
    host(RuntimeError("the store refused the write"))
    ok, detail = control._apply("clear_pool", "", None)
    assert ok is False
    assert "gemini" in detail and "RuntimeError" in detail
    assert "storage access" not in detail


def test_the_storage_advice_is_kept_for_a_storage_error(host):
    host(PermissionError("[WinError 5] Access is denied: 'auth.json'"))
    ok, detail = control._apply("clear_pool", "", None)
    assert ok is False
    assert "PermissionError" in detail
    assert "storage" in detail


def test_with_no_failure_the_answer_is_unchanged(host):
    host(3)
    ok, detail = control._apply("clear_pool", "", None)
    assert ok is True
    assert detail.startswith("every key starts again as if it had never been tried")
