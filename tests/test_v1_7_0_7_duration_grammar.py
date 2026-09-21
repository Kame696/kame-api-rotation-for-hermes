"""A relative duration is a whole expression, not arbitrary digit substrings."""
import pytest
from .test_v1_7_0_7_surfaces import NOW
from core.quota import parse_duration_to_seconds, extract_from_headers


@pytest.mark.parametrize("value", ["-12s", "1m -2s", "1e3s", "NaN12s", "12megabytes", "1.2.3s", "2026-09-12", "12s junk"])
def test_malformed_duration_does_not_become_a_wait(value):
    assert parse_duration_to_seconds(value) is None


@pytest.mark.parametrize("value,seconds", [("6m11.52s",371.52), ("4hr 5min",14700), ("1500ms",1.5), (".5s",.5), ("12",12)])
def test_whole_valid_duration(value, seconds):
    assert parse_duration_to_seconds(value) == pytest.approx(seconds)


def test_negative_retry_after_is_not_a_positive_twelve_second_hold():
    assert extract_from_headers({"retry-after":"-12s"}, NOW)[0] is None
