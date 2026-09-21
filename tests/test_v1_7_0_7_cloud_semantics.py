"""Synthetic source-linked cloud error distinctions, not live SDK certification."""
from .test_v1_7_0_7_surfaces import classify,NOW
from core.quota import extract_from_headers


def test_bedrock_model_readiness_is_not_a_spent_key():
    result=classify(provider="bedrock",status_code=429,
        error_type="ModelNotReadyException",
        error_body={"message":"The model specified in the request is not ready to serve inference requests."},
        now_epoch=NOW)
    assert result is None


def test_azure_retry_after_ms_keeps_millisecond_units():
    delay,source=extract_from_headers({"retry-after-ms":"2000"},NOW)
    assert delay == 2
    assert "retry-after-ms" in source
