"""Offline parser regressions; only the reported plural pair is capture-derived.

Other payloads are format/edge-case fixtures, not claims about provider behavior.
"""
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"))
from core.quota import extract_from_body, extract_from_headers, extract_retry_delay_seconds

NOW = 1788896543
RESET = 1788909203
DELAY = 12660


@pytest.mark.parametrize("fields", [
    {"resets_at": RESET, "resets_in_seconds": DELAY},
    {"resets_in_seconds": DELAY, "resets_at": RESET},
])
def test_reported_pair_nested_sdk_body(fields):
    assert extract_from_body({"error": fields}, NOW) == (DELAY, "body.resets_at")
    assert extract_from_body({"body": {"error": {"details": [fields]}}}, NOW) == (
        DELAY, "body.resets_at")


@pytest.mark.parametrize("value", [RESET, str(RESET), RESET * 1000,
    str(RESET * 1000), datetime.fromtimestamp(RESET, timezone.utc).isoformat(),
    format_datetime(datetime.fromtimestamp(RESET, timezone.utc), usegmt=True)])
@pytest.mark.parametrize("key", ["reset_at", "resets_at", "X-RateLimit-Reset"])
def test_absolute_formats_and_old_labels(key, value):
    assert extract_from_body({key: value}, NOW) == (DELAY, f"body.{key}")
    assert extract_from_headers({"X-RateLimit-Reset": value}, NOW) == (
        DELAY, "header.x-ratelimit-reset")


@pytest.mark.parametrize("value", [DELAY, float(DELAY), str(DELAY), "12660.0"])
def test_relative_seconds(value):
    assert extract_from_body({"resets_in_seconds": value}, NOW) == (
        DELAY, "body.resets_in_seconds")
    assert extract_from_headers({"Retry-After": value}, NOW) == (
        DELAY, "header.retry-after")


@pytest.mark.parametrize("key", ["reset_at", "resets_at", "resets_in_seconds"])
@pytest.mark.parametrize("bad", [None, True, False, 0, -1, "", "nonsense",
    "garbage 30", {}, [], float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", 10 ** 400])
def test_invalid_fields_supply_no_deadline(key, bad):
    assert extract_from_body({key: bad}, NOW) == (None, "")


@pytest.mark.parametrize("key", ["reset_at", "resets_at"])
@pytest.mark.parametrize("bad", [30, "30", "30s", NOW, NOW - 1, NOW + 86401])
def test_absolute_is_not_relative_or_stale(key, bad):
    assert extract_from_body({key: bad}, NOW) == (None, "")


@pytest.mark.parametrize("bad", [RESET, RESET * 1000, 86401, "30ms", "30s"])
def test_explicit_seconds_have_no_unit_or_epoch_guessing(bad):
    assert extract_from_body({"resets_in_seconds": bad}, NOW) == (None, "")


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("absolute,relative,expected,source", [
    (NOW + 30, 60, 60, "resets_in_seconds"),
    (NOW + 60, 30, 60, "resets_at"),
    (NOW - 1, 30, 30, "resets_in_seconds"),
    (NOW + 30, True, 30, "resets_at"),
    (float("inf"), 30, 30, "resets_in_seconds"),
])
def test_pair_resolution_is_order_independent(absolute, relative, expected, source, reverse):
    fields = [("resets_at", absolute), ("resets_in_seconds", relative)]
    assert extract_from_body(dict(fields[::-1] if reverse else fields), NOW) == (
        expected, f"body.{source}")


def test_preserve_families_not_max_all_fields():
    pair = {"resets_at": NOW + 30, "resets_in_seconds": 60}
    assert extract_from_body({"retry_after": 5, **pair}, NOW) == (5, "body.retry_after")
    assert extract_from_body({**pair, "retry_after": 900}, NOW) == (60, "body.resets_in_seconds")
    assert extract_from_body([{"resets_at": NOW + 30}, {"resets_in_seconds": 900}], NOW) == (
        30, "body.resets_at")
    assert extract_from_body({"resets_at": NOW + 30, "metadata": {"resets_in_seconds": 900}}, NOW) == (
        30, "body.resets_at")


def test_header_precedence_and_existing_body_families():
    assert extract_retry_delay_seconds(body={"error": {"resets_at": RESET}},
        headers={"Retry-After": "7", "X-RateLimit-Reset": RESET}, now_epoch=NOW) == (
            7, "header.retry-after")
    assert extract_from_body({"error": {"details": [{"retryDelay": {"seconds": 2, "nanos": 500000000}}]}}, NOW) == (
        2.5, "body.retryDelay")
    assert extract_from_body({"quotaResetDelay": "1500ms"}, NOW) == (1.5, "body.quotaResetDelay")
