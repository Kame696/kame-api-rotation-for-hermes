"""Optional vendor fields can have unexpected JSON types without breaking recovery."""
import pytest
from .test_v1_7_0_7_surfaces import classify, NOW


@pytest.mark.parametrize("details", [1, True, 1.5, "opaque", {"reason":"RATE_LIMIT_EXCEEDED"}, [None, 3, "x"]])
def test_malformed_optional_details_never_crash_classification(details):
    result = classify(provider="unknown", status_code=429,
        error_body={"error":{"type":"rate_limit_exceeded", "details":details}},
        error_message="Rate limit exceeded", now_epoch=NOW)
    assert result.reason == "rate_limit"
