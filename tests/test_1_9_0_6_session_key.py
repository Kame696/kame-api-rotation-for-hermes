"""1.9.0.6 (decisions/0037 C16) - the cron bound reads the task's own value, never the process environment."""
from __future__ import annotations

import contextvars
import importlib
import sys

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_9_0_6_switch_budget as SBT

settings = R.settings
session_key = importlib.import_module(f"{R.PACKAGE}.core.session_key")


def test_the_environment_alone_never_makes_a_chat_a_cron_job(monkeypatch):
    SBT._install_session_context(monkeypatch)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    settings.forget()
    assert settings.total_wait_bound() == 0.0, "a chat call is NOT capped by another task's process-wide variable"


def test_the_task_local_value_is_what_counts(monkeypatch):
    SBT._install_session_context(monkeypatch)
    settings.forget()
    ctx = contextvars.copy_context()
    ctx.run(sys.modules["gateway.session_context"]._table["HERMES_CRON_SESSION"].set, "1")
    assert ctx.run(settings.total_wait_bound) == 300.0
    assert settings.total_wait_bound() == 0.0


def test_a_user_set_bound_still_wins_in_cron(monkeypatch):
    SBT._install_session_context(monkeypatch)
    monkeypatch.setenv("KAME_MAX_TOTAL_WAIT", "0")
    settings.forget()
    ctx = contextvars.copy_context()
    ctx.run(sys.modules["gateway.session_context"]._table["HERMES_CRON_SESSION"].set, "1")
    assert ctx.run(settings.total_wait_bound) == 0.0


def test_one_reader_serves_both_and_never_raises(monkeypatch):
    monkeypatch.delitem(sys.modules, "gateway.session_context", raising=False)
    monkeypatch.delitem(sys.modules, "gateway", raising=False)
    assert session_key.bound("HERMES_CRON_SESSION") == "" and session_key.first_bound() == ""
