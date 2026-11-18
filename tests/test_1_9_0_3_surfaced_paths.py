"""1.9.0.3 (C1, designer answer 3) - every place that counts a surfaced error and raises it to Hermes emits ONE
``surfaced`` event (reason, code, the provider's text without advice, the error's class). Real transport, fake provider.
"""
from __future__ import annotations

import importlib

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_2_5_stream_race import FakeCall, KEYS, Pool, fresh, settings, transport  # noqa: F401

events_mod = importlib.import_module(f"{R.PACKAGE}.core.events")
FAKE_KEY = "AIzaSy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"


class Odd(Exception):
    def __init__(self, status, message, code="UNKNOWN"):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message, "status": code}}


@pytest.fixture()
def rows(monkeypatch):
    monkeypatch.setattr(transport, "_NOTIFY", None)
    events_mod.EVENTS.clear()
    return lambda: [r for r in events_mod.EVENTS.recent() if r["kind"] == "surfaced"]


def _go(t, call):
    with pytest.raises(Exception):
        list(t.stream(call))


def test_every_key_denied_emits_one_row(fresh, rows):
    denied = Odd(403, f"Permission denied: the project has been denied access (key {FAKE_KEY})", "PERMISSION_DENIED")
    pool = Pool({k: [[denied]] for k in KEYS[:2]})
    before = fresh.surfaced
    _go(fresh, FakeCall(pool, keys=KEYS[:2]))
    assert fresh.surfaced == before + 1
    found = rows()
    assert len(found) == 1, found
    row = found[0]
    assert row["reason"] == "denied" and row["code"] == 403
    assert "denied access" in row["detail"] and FAKE_KEY not in row["detail"]
    assert row["error_type"] == "Odd"


def test_a_pool_that_agrees_it_is_the_request_emits_one_row(fresh, rows):
    teapot = Odd(418, "I am a teapot")
    pool = Pool({KEYS[0]: [[teapot]]})
    before = fresh.surfaced
    _go(fresh, FakeCall(pool, keys=KEYS[:1]))
    assert fresh.surfaced == before + 1
    found = rows()
    assert len(found) == 1, found
    assert found[0]["code"] == 418 and "teapot" in found[0]["detail"] and found[0]["error_type"] == "Odd"


def test_max_total_wait_with_nothing_asked_emits_one_row(fresh, rows, monkeypatch):
    monkeypatch.setenv("KAME_MAX_TOTAL_WAIT", "1")
    settings.forget()
    for key in KEYS[:2]:
        fresh.engine.mark("gemini:gemini-race", key, False, 600.0, "rate_limit")
    pool = Pool({})
    _go(fresh, FakeCall(pool, keys=KEYS[:2]))
    found = rows()
    assert len(found) == 1, found
    assert found[0]["error_type"] == "ConnectionError" and "max_total_wait_seconds" in found[0]["detail"]
    assert found[0]["reason"].startswith("stopped waiting")
    settings.forget()


def test_the_last_error_after_a_wait_emits_one_row(fresh, rows, monkeypatch):
    monkeypatch.setenv("KAME_MAX_TOTAL_WAIT", "1")
    settings.forget()
    spent = Odd(429, "Quota exceeded for metric: generate_content_free_tier_requests, limit: 0", "RESOURCE_EXHAUSTED")
    pool = Pool({k: [[spent]] * 6 for k in KEYS[:2]})
    _go(fresh, FakeCall(pool, keys=KEYS[:2]))
    found = rows()
    assert len(found) == 1, found
    assert found[0]["detail"] and found[0]["error_type"] == "Odd" and found[0]["code"] == 429
    settings.forget()
