"""Source: https://platform.claude.com/docs/en/api/rate-limits (Messages API)."""
from types import SimpleNamespace as NS
import pytest
from .test_v1_7_0_7_surfaces import classify, NOW


@pytest.mark.parametrize("message", ["Request declined", "Monthly API usage threshold reached"])
@pytest.mark.parametrize("tail", ["", "?beta=true"])
def test_monthly_spend_subcode_refines_generic_429(message, tail):
    exc = Exception(message)
    exc.request = NS(url="https://api.anthropic.com/v1/messages"+tail)
    result = classify(provider="custom", status_code=429, error_message=message,
        error=exc, error_body={"error":{"type":"rate_limit_error", "details":{
            "error_code":"enforced_spend_limit_reached"}}}, now_epoch=NOW)
    # Existing host vocabulary calls a spent allowance without a reset billing;
    # the exact monthly window/account scope, not the generic 429, is the contract.
    assert result.reason == "billing" and result.quota_window == "per_month"
    assert result.quota_scope == "account"
    assert result.reset_at >= NOW+3600


@pytest.mark.parametrize("url", ["https://proxy.invalid/v1/messages",
    "https://api.anthropic.com/v1/messages/count_tokens",
    "https://api.anthropic.com.evil.invalid/v1/messages", None])
def test_monthly_subcode_is_not_assumed_on_unreviewed_surface(url):
    exc = Exception("Request declined")
    exc.request = NS(url=url)
    result = classify(provider="anthropic", status_code=429, error=exc,
        error_body={"error":{"type":"rate_limit_error", "details":{
            "error_code":"enforced_spend_limit_reached"}}}, now_epoch=NOW)
    assert result.quota_window == "unknown"


def test_ordinary_rate_limit_does_not_inherit_monthly_scope():
    exc = Exception("Request declined")
    exc.request = NS(url="https://api.anthropic.com/v1/messages")
    result = classify(provider="anthropic", status_code=429, error=exc,
        headers={"retry-after":"7"}, error_body={"error":{"type":"rate_limit_error"}}, now_epoch=NOW)
    assert result.reset_at == NOW+7 and result.quota_window != "per_month"
