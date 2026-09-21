"""Vertex's expired OAuth token belongs to host refresh, not dead-key rotation."""
from types import SimpleNamespace as NS
import pytest
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel
from .test_v1_7_0_7_surfaces import classify, NOW


@pytest.mark.parametrize("url", [
    "https://aiplatform.googleapis.com/v1/projects/p/locations/global/publishers/google/models/m:generateContent",
    "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/endpoints/openapi/chat/completions",
])
@pytest.mark.parametrize("message", ["Missing, invalid, or expired OAuth token", "Request has invalid authentication credentials"])
def test_vertex_authentication_returns_to_host_without_retirement(url, message):
    exc = Exception(message)
    exc.status_code = 401
    exc.request = NS(url=url)
    exc.body = {"error":{"code":401, "status":"UNAUTHENTICATED", "message":message}}
    result = classify(provider="vertex", status_code=401, error=exc, error_body=exc.body, now_epoch=NOW)
    assert result is None
    engine = carousel.Carousel()
    binding = dispatch.DispatchBinding(engine=engine)
    for attempt in range(4):
        assert binding._on_failure("vertex:m", "synthetic", exc, "test", attempt+1, False)[:2] == (
            "raise", "auth_refresh")
    assert engine.snapshot() == {}


def test_explicit_invalid_api_key_still_outranks_generic_auth_status():
    exc = Exception("API key not valid")
    exc.status_code = 401
    exc.request = NS(url="https://aiplatform.googleapis.com/v1/publishers/google/models/m:generateContent")
    exc.body = {"error":{"status":"UNAUTHENTICATED", "message":str(exc), "details":[{
        "@type":"type.googleapis.com/google.rpc.ErrorInfo", "reason":"API_KEY_INVALID"}]}}
    result = classify(provider="vertex", status_code=401, error=exc, error_body=exc.body, now_epoch=NOW)
    assert result.reason == "auth_permanent"
