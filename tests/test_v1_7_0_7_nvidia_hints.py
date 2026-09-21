"""NVIDIA historical missing headers must not suppress future explicit hints."""
import pytest
from .test_v1_7_0_7_surfaces import classify, NOW


@pytest.mark.parametrize("hint", ["0.5", "30", "120"])
def test_problem_title_preserves_explicit_hint_and_unknown_window(hint):
    result = classify(provider="nvidia", error_body={"status": 429, "title": "Too Many Requests"},
                      headers={"retry-after": hint}, now_epoch=NOW)
    assert result is not None
    assert result.quota_window == "unknown"
    assert result.reason != "billing"
    assert result.reset_at == pytest.approx(NOW + float(hint))
