"""Synthetic transport-equivalence regressions, not captured production failures.

Contract sources: test_core.TestUpstreamWrapper and classify's existing structured
field precedence. The same parsed payload must retain meaning in SDK fallbacks.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"))
from core import classify

NOW = 1800000000


def decision(verdict):
    # Verdict is a slots-based class without value equality.
    return None if verdict is None else {
        name: getattr(verdict, name) for name in verdict.__slots__}


def sdk_error(payload, location, message="Request declined"):
    error = Exception(message)
    if location == "response":
        error.response = SimpleNamespace(text=json.dumps(payload))
    else:
        error.details = payload
    return error


@pytest.mark.parametrize("location", ["response", "details"])
@pytest.mark.parametrize("status,code,message", [
    (401, "invalid_api_key", "API key not valid"),
    (429, "rate_limit_exceeded", "Rate limit exceeded, try again in 30s"),
    (402, "insufficient_quota", "Insufficient credits"),
])
def test_relayed_failure_defers_in_every_payload_location(location, status, code, message):
    payload = {"error": {"code": status, "message": "Provider returned error",
        "metadata": {"provider_name": "upstream", "raw": json.dumps({
            "error": {"code": code, "message": message}})}}}
    kwargs = dict(provider="openrouter", status_code=status,
                  error_message=message, now_epoch=NOW)
    assert classify(**kwargs, error_body=payload) is None
    assert classify(**kwargs, error=sdk_error(payload, location, message)) is None


@pytest.mark.parametrize("location", ["response", "details"])
@pytest.mark.parametrize("code,status,reason", [
    ("invalid_api_key", 429, "auth_permanent"),
    ("insufficient_quota", 429, "billing"),
    ("rate_limit_exceeded", 429, "rate_limit"),
])
def test_structured_code_has_same_meaning_in_fallback(location, code, status, reason):
    payload = {"error": {"code": code, "message": "Request declined"}}
    kwargs = dict(provider="openai", status_code=status,
                  error_message="Request declined", now_epoch=NOW)
    explicit = classify(**kwargs, error_body=payload)
    assert explicit is not None and explicit.reason == reason
    assert decision(classify(**kwargs, error=sdk_error(payload, location))) == decision(explicit)


@pytest.mark.parametrize("location", ["response", "details"])
@pytest.mark.parametrize("status", [429, 503])
def test_busy_prose_and_structured_throttle_keep_existing_precedence(location, status):
    payload = {"error": {"type": "rate_limit_exceeded", "message": "Overloaded"}}
    kwargs = dict(provider="unknown-provider", status_code=status,
                  error_message="Overloaded", now_epoch=NOW)
    explicit = classify(**kwargs, error_body=payload)
    if status == 429:
        assert explicit is not None and explicit.reason == "rate_limit"
    else:
        assert explicit is None
    assert decision(classify(**kwargs, error=sdk_error(payload, location, "Overloaded"))) == decision(explicit)


@pytest.mark.parametrize("location", ["response", "details"])
def test_nonempty_explicit_body_keeps_precedence_over_fallback(location):
    explicit = {"error": {"code": "invalid_api_key", "message": "Request declined"}}
    fallback = {"error": {"message": "Provider returned error",
                           "metadata": {"raw": "{}"}}}
    kwargs = dict(provider="openrouter", status_code=401, error_body=explicit,
                  error_message="Request declined", now_epoch=NOW)
    assert decision(classify(**kwargs, error=sdk_error(fallback, location))) == decision(classify(**kwargs))


@pytest.mark.parametrize("location", ["response", "details"])
def test_http_success_stays_out_of_error_classification(location):
    payload = {"error": {"code": "invalid_api_key"}}
    assert classify(status_code=200, error=sdk_error(payload, location),
                    now_epoch=NOW) is None


@pytest.mark.parametrize("raw", ["not JSON", "null", "[]", "{}"])
def test_unusable_response_preserves_plain_throttle(raw):
    error = Exception("Rate limit exceeded")
    error.response = SimpleNamespace(text=raw)
    verdict = classify(provider="openrouter", status_code=429, error=error,
        error_message=str(error), headers={"retry-after": "25"}, now_epoch=NOW)
    assert verdict.reason == "rate_limit"
    assert verdict.reset_at == NOW + 25
