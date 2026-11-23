"""1.9.0.6 (decisions/0037 C1) - how many model switches Hermes has made in one conversation."""
from __future__ import annotations

import contextvars
import importlib
import os
import sys
import types

import pytest

from tests import test_1_8_2_5_stream_race as R

sb = importlib.import_module(f"{R.PACKAGE}.core.switch_budget")
chain_mod = importlib.import_module(f"{R.PACKAGE}.core.fallback_chain")

_UNSET = object()


def _install_session_context(monkeypatch):
    """A stand-in for the host's gateway.session_context (the real Hermes is never on the path)."""
    sid = contextvars.ContextVar("HERMES_SESSION_ID", default=_UNSET)
    skey = contextvars.ContextVar("HERMES_SESSION_KEY", default=_UNSET)
    cron = contextvars.ContextVar("HERMES_CRON_SESSION", default=_UNSET)
    table = {"HERMES_SESSION_ID": sid, "HERMES_SESSION_KEY": skey, "HERMES_CRON_SESSION": cron}
    mod = types.ModuleType("gateway.session_context")
    mod._UNSET = _UNSET
    mod._bound_session_value = lambda name: table[name].get() if name in table else _UNSET

    def get_session_env(name, default=""):      # the host's wrong-for-us reader: falls back to os.environ
        v = mod._bound_session_value(name)
        return v if v is not _UNSET else os.getenv(name, default)

    mod.get_session_env = get_session_env
    mod._table = table                        # a test reads the other ContextVars from here
    pkg = types.ModuleType("gateway")
    pkg.session_context = mod
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "gateway", pkg)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)
    return sid, skey


@pytest.fixture()
def clock(monkeypatch):
    sb.reset_all()
    now = [1000.0]
    monkeypatch.setattr(sb, "_now", lambda: now[0])
    yield now
    sb.reset_all()


@pytest.fixture()
def limit3(monkeypatch):
    monkeypatch.setattr(chain_mod, "switch_limit", lambda *a, **k: 3)


def test_default_limit_allows_exactly_two_hand_overs(clock, limit3):
    assert sb.allowed() == 2
    for _ in range(2):
        assert sb.may_hand_over("a")
        sb.note_switch("a")
    assert not sb.may_hand_over("a"), "the third is refused: one switch is always left for Hermes' own"
    assert sb.used("a") == 2


def test_note_answer_resets_one_key_and_not_another(clock, limit3):
    sb.note_switch("a"); sb.note_switch("b")
    sb.note_answer("a")
    assert sb.used("a") == 0 and sb.used("b") == 1
    sb.note_switch("b"); sb.turn_started("b")
    assert sb.used("b") == 0


def test_shared_key_is_not_reset_while_in_use_and_is_after_120s_idle(clock, limit3):
    sb.note_switch("")
    clock[0] += 30
    sb.note_answer("")
    assert sb.used("") == 1, "another call may be mid-walk on the shared key"
    sb.turn_started("")
    assert sb.used("") == 1
    clock[0] += 121
    sb.note_answer("")
    assert sb.used("") == 0


def test_a_call_walking_on_the_shared_key_keeps_it_busy(clock, limit3):
    sb.note_switch("")
    clock[0] += 100
    assert sb.may_hand_over("")        # still a call on it: counts as activity
    clock[0] += 100
    sb.note_answer("")
    assert sb.used("") == 1


def test_api_max_retries_one_never_hands_over(clock, monkeypatch):
    monkeypatch.setattr(chain_mod, "switch_limit", lambda *a, **k: 1)
    assert sb.allowed() == 0
    assert not sb.may_hand_over("a")


def test_switch_limit_raising_is_a_no(clock, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no config")
    monkeypatch.setattr(chain_mod, "switch_limit", boom)
    assert sb.allowed() == 0
    assert sb.may_hand_over("a") is False


def test_idle_entries_expire_on_the_next_write(clock, limit3):
    sb.note_switch("old")
    clock[0] += sb.IDLE_S + 1
    sb.note_switch("new")
    assert sb.used("old") == 0 and sb.used("new") == 1


def test_a_conversation_waiting_hours_inside_kame_keeps_its_count(clock, limit3):
    sb.note_switch("waiting"); sb.note_switch("waiting")
    clock[0] += 2 * 3600
    sb.note_switch("other")                      # another conversation writes: the trim runs
    assert sb.used("waiting") == 2
    assert not sb.may_hand_over("waiting")


def test_the_table_is_capped_and_the_oldest_goes_first(clock, limit3):
    for i in range(sb.MAX_KEYS + 10):
        clock[0] += 1
        sb.note_switch(f"k{i}")
    assert len(sb._COUNTS) == sb.MAX_KEYS
    assert sb.used("k0") == 0 and sb.used(f"k{sb.MAX_KEYS + 9}") == 1


def test_no_session_context_gives_the_empty_key(monkeypatch):
    monkeypatch.delitem(sys.modules, "gateway.session_context", raising=False)
    monkeypatch.delitem(sys.modules, "gateway", raising=False)
    assert sb.conversation_key() == ""


def test_session_id_in_a_contextvar_copy_is_the_key(monkeypatch):
    sid, _ = _install_session_context(monkeypatch)
    assert sb.conversation_key() == ""
    ctx = contextvars.copy_context()
    ctx.run(sid.set, "sess-1")
    assert ctx.run(sb.conversation_key) == "sess-1"
    assert sb.conversation_key() == "", "the outer context never saw it"


def test_session_key_is_the_second_choice(monkeypatch):
    sid, skey = _install_session_context(monkeypatch)
    ctx = contextvars.copy_context()
    ctx.run(skey.set, "route-9")
    assert ctx.run(sb.conversation_key) == "route-9"
    ctx.run(sid.set, "sess-2")
    assert ctx.run(sb.conversation_key) == "sess-2"


def test_os_environ_is_never_read(monkeypatch):
    """Hermes writes the LAST agent's id into os.environ for the whole process: reading it would count one
    conversation on another's key (the unsafe direction)."""
    _install_session_context(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_ID", "someone-elses")
    monkeypatch.setenv("HERMES_SESSION_KEY", "someone-elses-key")
    assert sb.conversation_key() == ""


def test_a_broken_host_module_gives_the_empty_key(monkeypatch):
    mod = types.ModuleType("gateway.session_context")      # no _bound_session_value at all
    pkg = types.ModuleType("gateway")
    pkg.session_context = mod
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "gateway", pkg)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)
    assert sb.conversation_key() == ""
