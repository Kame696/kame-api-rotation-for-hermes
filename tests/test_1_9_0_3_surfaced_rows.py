"""1.9.0.3 (C1) - every failure row can be opened and read.

Evidence: a request NVIDIA itself refused ("404 page not found", 17/11 13:06 and 16:51) became a ``surfaced``
event with ``reason="other"`` and no ``detail``; the panel said "an error nothing recognised" and the row
could not be opened. Built on the stream harness of the 1.8.2.5 race test (real transport, fake provider).
"""
from __future__ import annotations

import importlib

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_2_5_stream_race import (  # noqa: F401  (fresh is a fixture)
    FakeCall, KEYS, Pool, chunk, fresh, settings, transport)

events_mod = importlib.import_module(f"{R.PACKAGE}.core.events")
host_text = importlib.import_module(f"{R.PACKAGE}.host_text")

FAKE_KEY = "AIzaSy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"       # AIza + 35 characters, the shape of a Google key


class NotFoundError(Exception):
    """The shape of ``openai.NotFoundError``: a status code and a text."""

    def __init__(self, message, status=404):
        super().__init__(message)
        self.status_code = status
        self.message = message


@pytest.fixture()
def rail(monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_NOTIFY", lambda message, *, kind="lifecycle": said.append((kind, message)) or True)
    transport._Spinner.reset()
    events_mod.EVENTS.clear()
    yield said
    transport._Spinner.reset()


def _surfaced():
    return [row for row in events_mod.EVENTS.recent() if row["kind"] == "surfaced"]


def _run_refused(fresh, exc):
    pool = Pool({KEYS[0]: [[exc]], KEYS[1]: [[exc]]})
    with pytest.raises(Exception):
        list(fresh.stream(FakeCall(pool, keys=KEYS[:2])))
    return pool


def test_a_refused_request_is_one_surfaced_row_with_the_providers_text(fresh, rail):
    _run_refused(fresh, NotFoundError("404 page not found"))
    rows = _surfaced()
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "request", "a refused request is not 'an error nothing recognised'"
    assert row["code"] == 404
    assert "404 page not found" in row["detail"]
    assert row["error_type"] == "NotFoundError"


def test_a_key_inside_the_error_text_never_reaches_the_stored_row(fresh, rail):
    _run_refused(fresh, NotFoundError(f"404 page not found for key {FAKE_KEY}"))
    row = _surfaced()[0]
    assert FAKE_KEY not in row["detail"] and FAKE_KEY not in str(row)
    assert "404 page not found" in row["detail"]


def test_the_rail_says_the_request_was_refused_and_passes_the_gate(fresh, rail):
    _run_refused(fresh, NotFoundError("404 page not found"))
    lines = [m for k, m in rail if k == "activity" and "refused" in m]
    assert lines, rail
    line = lines[-1]
    assert line == ("⚠ model returned HTTP 404 for gemini-race — the request itself was refused, "
                    "not a key; Hermes decides what happens next")
    assert transport.passes_desktop_status_gate(line)
    assert FAKE_KEY not in line


def test_the_rail_line_without_a_status_code(fresh, rail):
    class Odd(Exception):
        pass

    # a refused request that carries no number: a content-filter block names no status
    _run_refused(fresh, Odd("content_policy_violation: the prompt was refused"))
    lines = [m for k, m in rail if k == "activity" and "refused" in m]
    assert lines, rail
    assert lines[-1] == ("⚠ model returned an error for gemini-race — the request itself was refused, "
                         "not a key; Hermes decides what happens next")
    assert transport.passes_desktop_status_gate(lines[-1])


def test_the_timeout_surfaced_row_also_carries_its_text(fresh, rail):
    class Slow(TimeoutError):
        pass

    pool = Pool({KEYS[0]: [[Slow("read timed out after 30s")]], KEYS[1]: [[Slow("read timed out after 30s")]],
                 KEYS[2]: [[Slow("read timed out after 30s")]]})
    with pytest.raises(Exception):
        list(fresh.stream(FakeCall(pool)))
    rows = [row for row in _surfaced() if row["reason"] == "timeout"]
    assert rows, events_mod.EVENTS.recent()
    assert rows[0]["detail"] and rows[0]["error_type"] == "Slow"


def test_events_add_keeps_error_type_only_when_given_and_cuts_it():
    row = events_mod.EVENTS.add("surfaced", reason="request", error_type="NotFoundError")
    assert row["error_type"] == "NotFoundError"
    plain = events_mod.EVENTS.add("rotation", reason="rate_limit")
    assert "error_type" not in plain, "older rows read exactly as they did"
    assert len(events_mod.EVENTS.add("surfaced", error_type="X" * 200)["error_type"]) <= 60


def test_force_skips_the_floor_and_the_dedupe_but_not_the_switch(monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_NOTIFY", lambda message, *, kind="lifecycle": said.append(message) or True)
    transport._Spinner.reset()
    transport._Spinner.update("o", "one")
    transport._Spinner.update("o", "two")                    # inside the 1 s floor: held back
    assert said == ["one"]
    transport._Spinner.update("o", "two", force=True)
    transport._Spinner.update("o", "two", force=True)        # same text again: forced too
    assert said == ["one", "two", "two"]
    monkeypatch.setenv("KAME_LIVE_STATUS_DISABLED", "1")
    settings.forget()
    transport._Spinner.update("o", "three", force=True)
    assert said == ["one", "two", "two"]
    settings.forget()
    transport._Spinner.reset()
