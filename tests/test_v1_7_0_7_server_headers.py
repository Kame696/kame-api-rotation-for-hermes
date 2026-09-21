"""Quota reset telemetry is not a server retry instruction."""
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel
import pytest


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
def test_server_error_does_not_borrow_unrelated_quota_reset(monkeypatch, status):
    monkeypatch.setattr(dispatch.time, "time", lambda: 1800000000)
    engine = carousel.Carousel()
    exc = Exception("Service temporarily unavailable")
    exc.status_code = status
    exc.headers = {"x-ratelimit-reset-requests":"180s", "x-ratelimit-remaining-requests":"999"}
    dispatch.DispatchBinding(engine=engine)._on_failure("groq:m", "a", exc, "test", 1, False)
    assert engine._pools["groq:m"]["a"]["sick_until"] == 1800000000+carousel.SERVER_BASE_S


def test_server_retry_instruction_still_wins(monkeypatch):
    monkeypatch.setattr(dispatch.time, "time", lambda: 1800000000)
    engine = carousel.Carousel()
    exc = Exception("Service temporarily unavailable")
    exc.status_code = 503
    exc.headers = {"retry-after":"7", "x-ratelimit-reset-requests":"180s"}
    dispatch.DispatchBinding(engine=engine)._on_failure("groq:m", "a", exc, "test", 1, False)
    assert engine._pools["groq:m"]["a"]["sick_until"] == 1800000007
