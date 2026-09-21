"""Subscription quota codes are not all permanent billing failures."""
import pytest
from types import SimpleNamespace as NS
from .test_v1_7_0_7_surfaces import classify, NOW


@pytest.mark.parametrize("code", ["1316", "1317", "1318", "1319", "1320", "1321"])
def test_resettable_subscription_limit_survives_billing_words(code):
    body = {"error":{"code":code, "message":"Insufficient balance. Usage limit reached.", "reset_at":NOW+60}}
    exc = Exception(body["error"]["message"])
    exc.request = NS(url="https://api.z.ai/api/paas/v4/chat/completions")
    result = classify(provider="custom", status_code=429, error=exc,
        error_message=str(exc), error_body=body, now_epoch=NOW)
    assert result.reason == "rate_limit"
    assert result.reset_at == NOW+60


def test_opaque_expired_subscription_code_is_not_ordinary_throttling():
    body = {"error":{"code":"1309", "message":"Request declined"}}
    exc = Exception("Request declined")
    exc.request = NS(url="https://api.z.ai/api/paas/v4/chat/completions")
    result = classify(provider="custom", status_code=429, error=exc,
        error_message=str(exc), error_body=body, now_epoch=NOW)
    assert result.reason == "billing"


@pytest.mark.parametrize("url", ["https://proxy.invalid/api/paas/v4/chat/completions", "https://api.z.ai.evil.invalid/api/paas/v4/chat/completions", "https://api.z.ai/unrelated"])
def test_unverified_route_does_not_override_billing(url):
    body = {"error":{"code":"1316", "message":"Insufficient balance", "reset_at":NOW+60}}
    exc = Exception("Insufficient balance")
    exc.request = NS(url=url)
    result = classify(provider="zai", status_code=429, error=exc,
        error_message=str(exc), error_body=body, now_epoch=NOW)
    assert result.reason == "billing"
