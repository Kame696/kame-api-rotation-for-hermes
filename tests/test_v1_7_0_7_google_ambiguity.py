"""Response-only evidence must not invent the cause of a bare Google 429.

Sources: research/1.7.0.7/google-429-review-2026-09-15.md.
Synthetic envelopes, not live API observations.
"""
from types import SimpleNamespace

import pytest

from .test_v1_7_0_7_surfaces import classify, NOW


@pytest.mark.parametrize("path", [
    "/v1beta/models/model:generateContent",
    "/v1beta/models/model:streamGenerateContent?alt=sse",
    "/v1beta/openai/chat/completions",
    "/v1beta/interactions",
])
@pytest.mark.parametrize("delay", [None, "0.5", "30"])
def test_bare_resource_exhausted_does_not_invent_quota_window(path, delay):
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "Resource has been exhausted (e.g. check quota)."}}
    error = Exception(body["error"]["message"])
    error.request = SimpleNamespace(url="https://generativelanguage.googleapis.com" + path)
    result = classify(provider="gemini", status_code=429, error=error,
                      error_body=body,
                      headers={"retry-after": delay} if delay else {}, now_epoch=NOW)
    assert result is not None
    assert result.quota_window == "unknown"
    assert result.reason != "billing"
    if delay:
        assert result.reset_at == pytest.approx(NOW + float(delay))
