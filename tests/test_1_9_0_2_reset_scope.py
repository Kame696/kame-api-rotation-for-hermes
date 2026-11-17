"""1.9.0.2 (H2b) - "Reset pool" clears Hermes' own marks on a multiplexed gateway.

Live press, 2026-11-15 15:37 (after H2 made the failure speak): every provider's host-marks step
raised ``UnscopedSecretError: Hermes could not read this profile's GEMINI_BASE_URL (an internal
profile-scoping bug on the multiplexed gateway ...)``. Since the host made one gateway serve every
profile (``gateway.multiplex_profiles``), a credential read outside a profile's secret scope fails
closed. The panel's request is applied on KAME's heartbeat thread, a bare thread with an empty
context, so ``load_pool(p).reset_statuses()`` never had a scope - every press since 2026-10-19 failed.

The host's own answer for work on a bare thread is to bind the profile's scope first
(``gateway/run_profile_reconcile.py::_for_each_served_profile``, ``agent/curator.py``). KAME does the
same, and only after the host refused without one, so a host where the plain call works is untouched.

Pinned:
- an UnscopedSecretError is retried once inside the scope of KAME's own home, and that provider's
  marks are reset; the scope is removed afterwards;
- a host where the plain call works never sees a scope set;
- the scope is built once per press, from the home ``hermes_constants.get_hermes_home()`` names;
- a scoped retry that still fails is reported with the retry's own error type;
- a host without the scope API reports the original error, as before.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_1_9_0_2_reset_scope_under_test"


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

HOME = Path("C:/fake/hermes-home")


class UnscopedSecretError(RuntimeError):
    """Same name as the host's ``agent.secret_scope.UnscopedSecretError``."""


class _Host:
    """A multiplexed host in miniature: credential reads fail closed outside a secret scope."""

    def __init__(self, scoped_only=("gemini", "nvidia"), scope_api=True, still_fails=None):
        self.scope = None
        self.sets = []
        self.resets = 0
        self.built = []
        self.scoped_only = set(scoped_only)
        self.still_fails = still_fails or {}
        self.scope_api = scope_api
        self.reset_calls = {}

    def pool(self, provider):
        host = self

        class _Pool:
            def reset_statuses(self):
                host.reset_calls[provider] = host.reset_calls.get(provider, 0) + 1
                if provider in host.scoped_only and host.scope is None:
                    raise UnscopedSecretError(
                        f"Hermes could not read this profile's {provider.upper()}_BASE_URL "
                        "(an internal profile-scoping bug on the multiplexed gateway, not your configuration)")
                if provider in host.still_fails and host.scope is not None:
                    raise host.still_fails[provider]
                return 2

        return _Pool()

    def install(self, monkeypatch):
        auth = types.ModuleType("hermes_cli.auth")
        auth.read_credential_pool = lambda provider_id=None: {"anthropic": [{}], "gemini": [{}], "nvidia": [{}]}
        cp = types.ModuleType("agent.credential_pool")
        cp.load_pool = self.pool
        consts = types.ModuleType("hermes_constants")
        consts.get_hermes_home = lambda: str(HOME)
        monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
        monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.credential_pool", cp)
        monkeypatch.setitem(sys.modules, "hermes_constants", consts)
        if self.scope_api:
            ss = types.ModuleType("agent.secret_scope")
            ss.UnscopedSecretError = UnscopedSecretError

            def build(home):
                self.built.append(Path(home))
                return {"GEMINI_API_KEY": "x"}

            def set_scope(secrets, *, profile_home=None):
                self.scope = secrets
                self.sets.append(profile_home)
                return object()

            def reset_scope(token):
                self.scope = None
                self.resets += 1

            ss.build_profile_secret_scope = build
            ss.set_secret_scope = set_scope
            ss.reset_secret_scope = reset_scope
            monkeypatch.setitem(sys.modules, "agent.secret_scope", ss)
        else:
            monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
        return self


def test_an_unscoped_refusal_is_retried_in_the_profiles_scope_and_resets(monkeypatch):
    host = _Host().install(monkeypatch)
    done, failed = control._clear_benches_on_disk()
    assert failed == [], failed
    assert "6 host marks" in done                     # anthropic plain, gemini + nvidia scoped
    assert host.reset_calls == {"anthropic": 1, "gemini": 2, "nvidia": 2}
    assert host.scope is None and host.resets == len(host.sets) == 2


def test_the_panel_says_the_reset_worked(monkeypatch):
    _Host().install(monkeypatch)
    ok, detail = control._apply("clear_pool", "", None)
    assert ok is True, detail
    assert detail.startswith("every key starts again as if it had never been tried")


def test_a_host_where_the_plain_call_works_never_sees_a_scope(monkeypatch):
    host = _Host(scoped_only=()).install(monkeypatch)
    done, failed = control._clear_benches_on_disk()
    assert failed == []
    assert host.sets == [] and host.built == []
    assert "6 host marks" in done


def test_the_scope_is_built_once_per_press_from_kames_own_home(monkeypatch):
    host = _Host().install(monkeypatch)
    control._clear_benches_on_disk()
    assert host.built == [HOME]
    assert host.sets == [str(HOME), str(HOME)]


def test_a_scoped_retry_that_still_fails_is_named_with_its_own_error(monkeypatch):
    host = _Host(still_fails={"nvidia": PermissionError("[WinError 5] Access is denied: 'auth.json'")})
    host.install(monkeypatch)
    done, failed = control._clear_benches_on_disk()
    assert any("nvidia" in f and "PermissionError" in f for f in failed), failed
    assert not any("gemini" in f for f in failed), failed
    assert host.scope is None


def test_an_import_error_inside_the_scoped_call_is_reported_as_itself(monkeypatch):
    """Only a missing scope API means "report the original refusal"; an ImportError raised by the
    host's own reset, inside the scope, is the real failure and is named as such."""
    host = _Host(still_fails={"gemini": ImportError("cannot import name 'x' from 'agent.credential_pool'")})
    host.install(monkeypatch)
    done, failed = control._clear_benches_on_disk()
    assert any("gemini" in f and "ImportError" in f for f in failed), failed
    assert not any("UnscopedSecretError" in f for f in failed), failed


def test_a_scope_that_cannot_be_built_is_tried_once_per_press(monkeypatch):
    host = _Host().install(monkeypatch)
    calls = []

    def broken(home):
        calls.append(home)
        raise KeyError("GATEWAY_ALLOW_ALL_USERS")

    sys.modules["agent.secret_scope"].build_profile_secret_scope = broken
    done, failed = control._clear_benches_on_disk()
    assert len(calls) == 1, calls
    assert any("gemini" in f and "KeyError" in f for f in failed), failed
    assert any("nvidia" in f and "KeyError" in f for f in failed), failed
    assert host.scope is None


def test_without_the_hosts_scope_api_the_original_error_is_reported(monkeypatch):
    _Host(scope_api=False).install(monkeypatch)
    done, failed = control._clear_benches_on_disk()
    assert any("gemini" in f and "UnscopedSecretError" in f for f in failed), failed
    assert "2 host marks" in done                     # anthropic still reset
