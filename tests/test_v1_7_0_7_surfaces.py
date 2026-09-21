"""Google Interactions code meaning must not leak into other Google APIs."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"))
from core import classify
from core.provider_rules import api_surface
from core.quota import DEFAULT_PER_DAY_BENCH_SECONDS

NOW = 1800000000
URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def error_at(url):
    exc = Exception("Request declined")
    exc.response = NS(request=NS(url=url), text=json.dumps({
        "error": {"code": "quota_exceeded", "message": "Request declined"}}))
    return exc


@pytest.mark.parametrize("tail", ["", "/id", "?stream=true"])
@pytest.mark.parametrize("seconds", [None, 1, 20, 7200])
def test_daily_code_only_on_identified_interactions_route(tail, seconds):
    result = classify(provider="gemini", status_code=429, error=error_at(URL+tail),
                      headers={"retry-after":str(seconds)} if seconds else {}, now_epoch=NOW)
    assert result.quota_window == "per_day"
    expected = seconds if seconds and seconds >= DEFAULT_PER_DAY_BENCH_SECONDS else DEFAULT_PER_DAY_BENCH_SECONDS
    assert result.reset_at == NOW+expected


@pytest.mark.parametrize("url", [
    "https://proxy.invalid/v1beta/interactions",
    "https://generativelanguage.googleapis.com.evil.invalid/v1beta/interactions",
    "https://generativelanguage.googleapis.com@evil.invalid/v1beta/interactions",
    "http://generativelanguage.googleapis.com/v1beta/interactions",
    "https://generativelanguage.googleapis.com:123/v1beta/interactions",
    "https://generativelanguage.googleapis.com/v1beta/interactions_fake",
    "https://generativelanguage.googleapis.com/v1beta/models/model:generateContent",
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "https://us-central1-aiplatform.googleapis.com/v1/projects/p/models/m",
    "invalid", "https://[malformed",
])
def test_same_code_elsewhere_keeps_window_unknown(url):
    result = classify(provider="gemini", status_code=429, error=error_at(url), now_epoch=NOW)
    assert result.quota_window == "unknown"


def test_provider_alias_and_message_url_do_not_prove_endpoint():
    result = classify(provider="gemini", status_code=429, error_message=URL,
                      error_body={"error":{"code":"quota_exceeded"}}, now_epoch=NOW)
    assert result.quota_window == "unknown"


def test_hostile_request_metadata_is_ignored():
    class Hostile:
        @property
        def request(self):
            raise RuntimeError("unavailable")
    assert api_surface(Hostile()) == ""


def test_named_counter_outranks_endpoint_window_hint():
    exc = error_at(URL)
    body = {"error":{"code":"quota_exceeded", "details":[{
        "@type":"type.googleapis.com/google.rpc.QuotaFailure",
        "violations":[{"quotaId":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}
    result = classify(status_code=429, error_body=body, error=exc,
                      headers={"retry-after":"19"}, now_epoch=NOW)
    assert result.quota_window == "per_minute" and result.reset_at == NOW+19
