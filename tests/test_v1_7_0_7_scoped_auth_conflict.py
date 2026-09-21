"""Scoped quota rules must not hide explicit invalid-key fields."""
import pytest
from types import SimpleNamespace as NS
from .test_v1_7_0_7_surfaces import classify,NOW

@pytest.mark.parametrize("url,code,message", [
    ("https://api.z.ai/api/paas/v4/chat/completions","1316","Request declined"),
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions","insufficient_quota","You exceeded your current quota, please check your plan and billing details.")])
def test_explicit_invalid_key_survives_scoped_throttle(url,code,message):
    e=Exception(message);e.request=NS(url=url)
    result=classify(provider="custom",status_code=429,error=e,error_message=message,
        error_body={"error":{"type":"invalid_api_key","code":code,"message":message}},now_epoch=NOW)
    assert result.reason == "auth_permanent"
