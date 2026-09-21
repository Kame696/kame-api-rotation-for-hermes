"""A text retry instruction must not accept a malformed numeric prefix."""
import pytest
from .test_v1_7_0_7_surfaces import NOW
from core.quota import extract_from_text

@pytest.mark.parametrize("value", ["1e3s", "12secondsjunk", "12megabytes", "1.2.3s", "2026-09-12", "12msjunk"])
def test_invalid_numeric_prefix_is_not_seconds(value):
    assert extract_from_text("Please retry after " + value)[0] is None

@pytest.mark.parametrize("value,expected", [(".5s", .5), ("1.5s",1.5), ("12 seconds",12), ("12",12), ("6m11.52s",371.52)])
def test_complete_duration_is_preserved(value, expected):
    assert extract_from_text("Please retry after " + value)[0] == pytest.approx(expected)
