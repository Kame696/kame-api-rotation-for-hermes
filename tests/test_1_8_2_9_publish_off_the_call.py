"""1.8.2.9 (decisions/0031, step 0): the snapshot is not built on the request thread.

Measured with ``tools/bench_publish.py``: a ``state.publish`` costs about 10 ms alone and 27 ms
(p95 50 ms) with the panel polling the file, and the transport made it at the start of every
attempt and after most decisions - on the thread that is waiting for the model. Now the request
thread leaves a note and wakes the heartbeat thread, which writes. With no heartbeat running the
note is written inline, as before.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_publish_off_the_call.py -q
"""
from __future__ import annotations

import importlib
import os
import time

import pytest

from tests import test_1_8_2_5_stream_race as R

pkg = importlib.import_module(R.PACKAGE)
state = importlib.import_module(f"{R.PACKAGE}.state")
transport = R.transport


@pytest.fixture()
def clean(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_RECORDER_DISABLED", "1")
    state._pending = None
    state._current_activity = None
    state._consumers = 0
    state._in_flight = False
    state._wake.clear()
    state.clear()
    yield tmp_path
    pkg._stop_state_heartbeat()
    state._consumers = 0
    state._pending = None
    state._current_activity = None


def test_the_request_thread_only_leaves_a_note_when_a_heartbeat_is_running(clean, monkeypatch):
    def slow_publish(*a, **k):
        time.sleep(0.5)
        return True

    monkeypatch.setattr(state, "publish", slow_publish)
    state.add_consumer()
    started = time.perf_counter()
    transport._publish(None, {"phase": "waiting"})
    assert time.perf_counter() - started < 0.02          # one lock, two assignments, one Event.set()
    assert state._pending is not None and state._wake.is_set()


def test_with_no_heartbeat_the_note_is_written_inline_as_before(clean, monkeypatch):
    written = []
    monkeypatch.setattr(state, "publish", lambda binding=None, **k: written.append(k.get("activity")) or True)
    transport._publish(None, {"phase": "now"})
    assert written == [{"phase": "now"}]


def test_a_burst_of_notes_becomes_one_write_with_the_last_activity(clean, monkeypatch):
    written = []
    monkeypatch.setattr(state, "publish", lambda binding=None, **k: written.append(k.get("activity")) or True)
    state.add_consumer()
    for n in range(10):
        state.request_publish(None, activity={"n": n})
    assert state.drain_pending() is True and state.drain_pending() is False
    assert written == [{"n": 9}]


def test_the_activity_survives_for_the_heartbeats_own_writes_until_a_call_ends(clean, monkeypatch):
    monkeypatch.setattr(state, "publish", lambda *a, **k: True)
    state.add_consumer()
    state.request_publish(None, activity={"phase": "waiting on gemini"})
    assert state.current_activity() == {"phase": "waiting on gemini"}
    state.request_publish(None, activity=None)           # the call ended
    assert state.current_activity() is None


def test_drain_waits_until_nothing_is_pending(clean, monkeypatch):
    monkeypatch.setattr(state, "publish", lambda *a, **k: True)
    assert state.drain(0.1) is True
    state.add_consumer()
    state.request_publish(None, activity=None)
    assert state.drain(0.05) is False                     # nobody wrote it yet
    state.drain_pending()
    assert state.drain(0.5) is True


def test_the_real_heartbeat_writes_a_note_within_a_second(clean):
    pkg._start_state_heartbeat()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and state._consumers == 0:
        time.sleep(0.01)
    assert state._consumers == 1
    path = state.state_path()
    before = path.stat().st_mtime_ns if path and path.exists() else 0
    started = time.monotonic()
    state.request_publish(None, activity={"phase": "waiting"})
    while time.monotonic() - started < 1.5:
        if path and path.exists() and path.stat().st_mtime_ns != before:
            break
        time.sleep(0.01)
    else:
        pytest.fail("the heartbeat did not write the snapshot within 1.5 s")
    assert time.monotonic() - started < 1.0
    pkg._stop_state_heartbeat()
    assert state._consumers == 0                          # and a stopped heartbeat hands writing back to callers
