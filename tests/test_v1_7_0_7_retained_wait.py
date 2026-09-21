"""An overlapping shorter failure does not rewrite the actual deadline."""
import pytest
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel


@pytest.mark.parametrize("count", [1, 2, 14])
def test_mark_reports_remaining_older_hold(count):
    engine = carousel.Carousel()
    engine.select("p:m", [str(n) for n in range(count)], now=100)
    assert engine.mark("p:m", "0", False, 60, "rate_limit", now=100, stated=True) == 60
    actual = engine.mark("p:m", "0", False, 20, "rate_limit", now=110, stated=True)
    assert actual == 50
    assert actual.retained
    assert actual.held_until == 160
    assert engine._pools["p:m"]["0"]["sick_until"] == 160


def test_retained_hold_is_not_a_new_provider_prediction(monkeypatch):
    monkeypatch.setattr(dispatch.time, "time", lambda: 110)
    engine = carousel.Carousel()
    engine.mark("p:m", "a", False, 60, "rate_limit", now=100, stated=True)
    records = []
    monkeypatch.setattr(dispatch.runtime, "record_rotation", lambda **kw: records.append(kw))
    exc = Exception("Rate limit exceeded")
    exc.status_code = 429
    exc.headers = {"retry-after":"20"}
    binding = dispatch.DispatchBinding(engine=engine)
    binding._on_failure("p:m", "a", exc, "test", 1, False, credential_id="fixture")
    assert records == []  # Original episode must remain the learning record.
    assert engine._pools["p:m"]["a"]["sick_until"] == 160
