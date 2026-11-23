"""1.9.0.6 (decisions/0037 C11) - the live line and the chip show the open chat's own state."""
from __future__ import annotations

import contextvars
import importlib

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_9_0_6_switch_budget as SBT

state = importlib.import_module(f"{R.PACKAGE}.state")


@pytest.fixture()
def host(monkeypatch):
    sid, _ = SBT._install_session_context(monkeypatch)
    state._activities.clear()
    monkeypatch.setattr(state, "_consumers", 0)
    monkeypatch.setattr(state, "publish", lambda *a, **k: None)
    yield sid
    state._activities.clear()


def in_session(sid_var, name, fn, *a, **k):
    ctx = contextvars.copy_context()
    ctx.run(sid_var.set, name)
    return ctx.run(fn, *a, **k)


def test_two_sessions_keep_two_activities(host):
    in_session(host, "A", state.request_publish, None, activity={"kind": "calling", "model": "m1"})
    in_session(host, "B", state.request_publish, None, activity={"kind": "waiting", "model": "m2"})
    view = state._activities_view()
    assert view["A"]["model"] == "m1" and view["B"]["model"] == "m2" and set(view) == {"A", "B"}


def test_the_latest_of_all_stays_in_activity_for_older_readers(host):
    in_session(host, "A", state.request_publish, None, activity={"kind": "calling", "model": "m1"})
    snap = state.snapshot(None, {"kind": "waiting", "model": "m2"})
    assert snap["activity"]["model"] == "m2" and "A" in snap["activities"]


def test_a_conversation_whose_call_ended_has_no_entry(host):
    in_session(host, "A", state.request_publish, None, activity={"kind": "calling"})
    in_session(host, "A", state.request_publish, None, activity=None)
    assert "A" not in state._activities_view()


def test_at_most_thirty_two_the_oldest_dropped(host):
    for i in range(40):
        in_session(host, f"s{i}", state.request_publish, None, activity={"kind": "calling", "n": i})
    view = state._activities_view()
    assert len(view) == 32 and "s0" not in view and "s39" in view


def test_a_call_with_no_session_id_adds_no_entry(host):
    state.request_publish(None, activity={"kind": "calling"})
    assert state._activities_view() == {}


def test_a_reply_that_ends_clears_its_own_chat_only(host):
    transport = R.transport
    in_session(host, "A", state.request_publish, None, activity={"kind": "calling"})
    in_session(host, "B", state.request_publish, None, activity={"kind": "calling"})

    class Call:
        channel = "main"

    in_session(host, "A", transport._inflight_end, Call())
    assert set(state._activities_view()) == {"B"}


import shutil
import subprocess
from pathlib import Path


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_chip_and_the_line_read_the_open_chats_entry_in_the_page():
    script = Path(__file__).with_name("ui_activities.mjs")
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
