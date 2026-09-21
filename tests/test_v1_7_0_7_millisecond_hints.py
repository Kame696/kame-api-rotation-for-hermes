"""Azure/OpenAI SDK millisecond precision must survive KAME's readers."""
import pytest
from .test_v1_7_0_6_reset import extract_from_body, extract_from_headers, NOW


@pytest.mark.parametrize("value", [2500, "2500", "2500.0"])
def test_header_milliseconds_are_seconds_not_a_default(value):
    assert extract_from_headers({"Retry-After-Ms": value}, NOW) == (2.5, "header.retry-after-ms")


@pytest.mark.parametrize("value", [2500, "2500", "2500.0"])
def test_nested_header_copy_keeps_millisecond_units(value):
    assert extract_from_body({"error":{"metadata":{"headers":{"retry-after-ms":value}}}}, NOW) == (
        2.5, "body.retry-after-ms")


def test_precision_header_has_sdk_precedence_over_rounded_seconds():
    assert extract_from_headers({"retry-after-ms":"2500", "retry-after":"3"}, NOW) == (
        2.5, "header.retry-after-ms")


@pytest.mark.parametrize("value", [None, "bad", -5, "nan", "inf", 1e20])
def test_malformed_ms_hint_leaves_seconds_fallback_intact(value):
    assert extract_from_headers({"retry-after-ms":value, "retry-after":"3"}, NOW) == (
        3, "header.retry-after")


@pytest.mark.parametrize("status", [429, 503])
def test_dispatch_keeps_precision_and_provenance(monkeypatch, status):
    from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel
    monkeypatch.setattr(dispatch.time, "time", lambda: NOW)
    engine = carousel.Carousel()
    binding = dispatch.DispatchBinding(engine=engine)
    exc = Exception("Too many requests" if status == 429 else "Service unavailable")
    exc.status_code = status
    exc.headers = {"retry-after-ms":"2500"}
    binding._on_failure("azure:model", "a", exc, "test", 1, False)
    assert engine._pools["azure:model"]["a"]["sick_until"] == NOW+2.5
    if status == 429:
        assert engine._stated_rl_ceiling[("azure:model", "a")] == 2.5
