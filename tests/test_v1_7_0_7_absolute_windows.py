"""A calendar reset near now is not the misleading short daily RetryInfo."""
from datetime import datetime, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"))
from core.quota import compute_reset_at, extract_reset_moment_from_text

NOW = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("field", ["reset_at", "resets_at"])
@pytest.mark.parametrize("window", ["daily quota exceeded", "monthly quota reached"])
def test_explicit_reset_inside_an_hour_is_not_discarded(field, window):
    r = compute_reset_at(now_epoch=NOW, message=window,
                         body={"error": {field: NOW+60}})
    assert r.reset_at == NOW+60
    assert r.source == "body."+field


def test_specific_calendar_moment_beats_short_daily_relative_hint():
    r = compute_reset_at(now_epoch=NOW, message="Daily quota exhausted. Resets at 2026-10-01T00:00:00Z",
                         body={"error":{"retryDelay":"1s"}})
    assert r.reset_at == NOW+60 and r.source == "text.reset_at"


def test_anthropic_documented_regain_access_format():
    assert extract_reset_moment_from_text(
        "You will regain access on 2026-10-01 at 00:00 UTC.", NOW) == (60, "text.reset_at")


def test_short_daily_retryinfo_still_does_not_prove_reset():
    r = compute_reset_at(now_epoch=NOW, message="Daily quota exceeded",
                         body={"error":{"retryDelay":"1s"}})
    assert r.reset_at > NOW+60 and r.source == "window"


def test_long_stronger_retry_after_is_not_shortened_by_body_calendar():
    r = compute_reset_at(now_epoch=NOW, message="Daily quota exceeded",
                         headers={"retry-after": "7200"}, body={"error":{"reset_at":NOW+60}})
    assert r.reset_at == NOW+7200 and r.source == "header.retry-after"


@pytest.mark.parametrize("count", [1, 2, 14])
@pytest.mark.parametrize("field", ["reset_at", "resets_at", "text"])
def test_calendar_deadline_survives_dispatch_and_pool(monkeypatch, count, field):
    from tests.test_v1_7_0_7_dispatch_ownership import dispatch, carousel
    monkeypatch.setattr(dispatch.time, "time", lambda: NOW)
    engine = carousel.Carousel()
    binding = dispatch.DispatchBinding(engine=engine)
    identity, key = "gemini:model", "synthetic-0"
    keys = ["synthetic-"+str(i) for i in range(count)]
    engine.select(identity, keys, now=NOW)
    exc = Exception("Daily quota exceeded" + (
        ". Resets at 2026-10-01T00:00:00Z" if field == "text" else ""))
    exc.status_code = 429
    exc.body = {"error": {"code": "RESOURCE_EXHAUSTED", "message": str(exc)}}
    if field != "text":
        exc.body["error"][field] = NOW+60
    binding._on_failure(identity, key, exc, "test", 1, False)
    state = engine._pools[identity][key]
    assert state["sick_until"] == NOW+60
