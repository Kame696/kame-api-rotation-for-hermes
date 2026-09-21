"""Documented Groq counters are distinct; generic provider labels prove nothing."""
from types import SimpleNamespace as NS
import pytest
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel
from .test_v1_7_0_7_surfaces import classify, NOW

URL = "https://api.groq.com/openai/v1/chat/completions"


def failure(message, headers, url=URL):
    exc = Exception(message)
    exc.status_code = 429
    exc.request = NS(url=url)
    exc.body = {"error":{"type":"rate_limit_exceeded", "message":message}}
    exc.headers = headers
    return exc


def verdict(exc):
    return classify(provider="custom", status_code=429, error=exc,
        error_message=str(exc), error_body=exc.body, headers=exc.headers, now_epoch=NOW)


@pytest.mark.parametrize("count", [1, 2, 14])
def test_documented_retry_after_survives_daily_dispatch(monkeypatch, count):
    monkeypatch.setattr(dispatch.time, "time", lambda: NOW)
    exc = failure("Rate limit reached on requests per day (RPD)", {"retry-after":"2"})
    result = verdict(exc)
    assert result.reset_at == NOW+2
    engine = carousel.Carousel()
    engine.select("groq:m", [str(i) for i in range(count)], now=NOW)
    dispatch.DispatchBinding(engine=engine)._on_failure("groq:m", "0", exc, "test", 1, False)
    assert engine._pools["groq:m"]["0"]["sick_until"] == NOW+2


@pytest.mark.parametrize("message,remaining", [
    ("Rate limit reached on tokens per minute (TPM)", None),
    ("Rate limit exceeded", "100"),
])
def test_unrelated_rpd_reset_does_not_hide_token_wait(message, remaining):
    exc = failure(message, {"x-ratelimit-reset-requests":"180s",
        "x-ratelimit-remaining-requests":remaining, "x-ratelimit-reset-tokens":"7.66s"})
    assert verdict(exc).reset_at == pytest.approx(NOW+7.66, rel=0, abs=0.00001)


def test_positive_token_count_is_not_assumed_sufficient_for_request():
    exc = failure("Rate limit exceeded", {"x-ratelimit-reset-requests":"3s",
        "x-ratelimit-remaining-requests":"0", "x-ratelimit-reset-tokens":"30s",
        "x-ratelimit-remaining-tokens":"5"})
    assert verdict(exc).reset_at == NOW+30


def test_second_exhausted_counter_is_not_discarded_by_message_priority():
    exc = failure("Tokens per minute (TPM) exhausted", {"x-ratelimit-reset-requests":"180s",
        "x-ratelimit-remaining-requests":"0", "x-ratelimit-reset-tokens":"7s"})
    assert verdict(exc).reset_at == NOW+180


def test_later_calendar_reset_is_not_shortened_by_retry_instruction():
    exc = failure("Requests per day (RPD) exhausted", {"retry-after":"2"})
    exc.body["error"]["reset_at"] = NOW+60
    assert verdict(exc).reset_at == NOW+60


def test_documented_rpd_reset_can_be_shorter_than_one_hour():
    exc = failure("Requests per day (RPD) exhausted", {"x-ratelimit-reset-requests":"12s"})
    assert verdict(exc).reset_at == NOW+12


def test_daily_and_token_resets_use_later_known_deadline():
    exc = failure("Requests per day (RPD) exhausted", {
        "x-ratelimit-reset-requests":"12s", "x-ratelimit-reset-tokens":"60s"})
    result = verdict(exc)
    assert result.reset_at == NOW+60
    assert result.window_scoped_reset


@pytest.mark.parametrize("daily", [None, "garbage", "0s", "-12s"])
def test_token_reset_alone_does_not_explain_daily_recovery(daily):
    exc = failure("Requests per day (RPD) exhausted", {
        "x-ratelimit-reset-requests":daily, "x-ratelimit-reset-tokens":"60s"})
    assert verdict(exc).reset_at >= NOW+3600


def test_hostile_url_object_does_not_break_optional_surface_detection():
    class BrokenURL:
        def __str__(self):
            raise RuntimeError("URL metadata unavailable")
    exc = failure("Requests per day (RPD) exhausted", {"retry-after":"2"}, BrokenURL())
    assert verdict(exc).reset_at >= NOW+3600


@pytest.mark.parametrize("url", ["https://proxy.invalid/openai/v1/chat/completions",
    "https://api.groq.com.evil.invalid/openai/v1/chat/completions", None])
def test_unverified_route_does_not_inherit_vendor_trust(url):
    exc = failure("Requests per day (RPD) exhausted", {"retry-after":"2"}, url)
    assert verdict(exc).reset_at >= NOW+3600
