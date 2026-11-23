"""1.9.0.6 (decisions/0037 C21) - the live line looks alive, and can never stay on a state the turn left."""
from __future__ import annotations

import contextvars
import importlib
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_9_0_6_switch_budget as SBT

state = importlib.import_module(f"{R.PACKAGE}.state")
transport = R.transport


@pytest.fixture()
def host(monkeypatch):
    sid, _ = SBT._install_session_context(monkeypatch)
    state._activities.clear()
    state._started.clear()
    monkeypatch.setattr(state, "_consumers", 0)
    monkeypatch.setattr(state, "publish", lambda *a, **k: None)
    yield sid
    state._activities.clear()
    state._started.clear()


def in_session(sid_var, name, fn, *a, **k):
    ctx = contextvars.copy_context()
    ctx.run(sid_var.set, name)
    return ctx.run(fn, *a, **k)


class Call:
    channel = "main"

    def __init__(self, stopped=False):
        self._stopped = stopped

    def cancelled(self):
        return self._stopped


def test_a_published_activity_carries_the_replys_start(host):
    in_session(host, "A", state.note_reply_start)
    began = state._started["A"]
    in_session(host, "A", state.request_publish, None, activity={"kind": "calling", "model": "m"})
    assert state._activities_view()["A"]["started_at"] == began
    assert state.live_line(state._activities_view()["A"], now=began + 23) == "⏳ Asking m · ? of ? keys ready · 23s"


def test_the_start_belongs_to_the_conversation(host):
    in_session(host, "A", state.note_reply_start)
    in_session(host, "B", state.request_publish, None, activity={"kind": "calling"})
    assert "started_at" not in state._activities_view()["B"]


def test_an_answered_reply_clears_the_line_at_once(host):
    in_session(host, "A", state.note_reply_start)
    in_session(host, "A", state.request_publish, None, activity={"kind": "standin", "model": "m", "other": "n"})
    in_session(host, "A", transport._inflight_end, Call())
    assert "A" not in state._activities_view() and "A" not in state._started


@pytest.mark.parametrize("error,reason", [
    (None, "stopped"),
    (transport.KameAuxStopped("x"), "stopped waiting"),
    (ConnectionError("max_total_wait_seconds"), "stopped waiting"),
    (TimeoutError("read timed out"), "it timed out"),
    (RuntimeError("boom"), "an error"),
])
def test_every_close_outcome_publishes_the_end_state(host, error, reason):
    """The 2026-11-23 bug: the chip stayed on 'is answering instead' after the turn had died."""
    call = Call(stopped=error is None)
    call._kame_call_id = "c1"
    in_session(host, "A", state.note_reply_start)
    in_session(host, "A", state.request_publish, None,
               activity={"kind": "standin", "model": "m", "other": "n", "phase": "standin"})
    in_session(host, "A", lambda: (transport.KameTransport._close_row(object.__new__(transport.KameTransport), call, error),
                                   transport._inflight_end(call)))
    entry = state._activities_view()["A"]
    assert entry["kind"] == "ended" and entry["reason"] == reason and "phase" not in entry
    assert entry["ended_at"] <= time.time()
    assert state.live_line(entry) == f"✕ no answer - {reason}"


def test_a_hand_over_is_not_an_end(host):
    """Review fix: the reply goes on with Hermes' next model, so the line clears instead of saying "no answer"."""
    call = Call()
    call._kame_call_id = "c1"
    in_session(host, "A", state.note_reply_start)
    in_session(host, "A", state.request_publish, None, activity={"kind": "calling", "model": "m"})
    in_session(host, "A", lambda: (transport.KameTransport._close_row(object.__new__(transport.KameTransport), call,
                                                                       transport.KamePoolResting("busy", 1e12)),
                                   transport._inflight_end(call)))
    assert "A" not in state._activities_view()


def test_a_provider_error_names_no_key(host):
    class Refused(Exception):
        status_code = 401

    call = Call()
    in_session(host, "A", lambda: (transport.KameTransport._close_row(object.__new__(transport.KameTransport), call,
                                                                       Refused("bad key AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")),
                                   transport._inflight_end(call)))
    assert state._activities_view()["A"]["reason"] == "the provider refused it"


def test_a_failed_call_through_the_real_transport_ends_on_the_end_state(host, monkeypatch):
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())

    class Boom(Exception):
        status_code = 500

    pool = R.Pool({k: [[Boom("x")]] * 4 for k in R.KEYS})

    def drive():
        in_session_ctx = None
        try:
            list(t.stream(R.FakeCall(pool)))
        except Exception:
            pass

    in_session(host, "A", drive)
    entry = state._activities_view().get("A")
    assert entry is None or entry["kind"] == "ended", entry


def test_a_new_reply_replaces_the_end_line(host):
    in_session(host, "A", state.request_publish, None, activity={"kind": "ended", "reason": "stopped", "ended_at": 1.0})
    in_session(host, "A", state.note_reply_start)
    in_session(host, "A", state.request_publish, None, activity={"kind": "calling", "model": "m"})
    assert state._activities_view()["A"]["kind"] == "calling"
