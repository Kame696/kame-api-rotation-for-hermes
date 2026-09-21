"""Exact documented token-limit wording must not become a billing hold."""
import pytest
from types import SimpleNamespace as NS
from .test_v1_7_0_7_surfaces import classify, NOW

URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
MESSAGE = "You exceeded your current quota, please check your plan and billing details."

def judge(url, message, code="insufficient_quota"):
    exc = Exception(message)
    exc.request = NS(url=url)
    return classify(provider="alibaba", status_code=429, error=exc,
        error_message=message, error_body={"error":{"code":code,"message":message}}, now_epoch=NOW)

@pytest.mark.parametrize("code", ["insufficient_quota", "Throttling.AllocationQuota"])
def test_documented_token_limit_is_temporary(code):
    assert judge(URL, MESSAGE, code).reason == "rate_limit"

@pytest.mark.parametrize("url", ["https://proxy.invalid/compatible-mode/v1/chat/completions",
    "https://dashscope-intl.aliyuncs.com.evil.invalid/compatible-mode/v1/chat/completions",
    "https://dashscope-intl.aliyuncs.com/unrelated"])
def test_same_words_do_not_override_unverified_endpoint(url):
    assert judge(url, MESSAGE).reason == "billing"

@pytest.mark.parametrize("message", ["Free allocated quota exceeded.", "Insufficient balance.",
    MESSAGE + " Insufficient balance."])
def test_free_allowance_and_balance_are_not_short_rate_limits(message):
    assert judge(URL, message).reason == "billing"
