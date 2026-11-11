"""E7 (1.8.4.0): a prompt no key can answer stops at once, and rests nothing.

The phrases are the host's own (`agent/error_classifier.py`, `_CONTENT_POLICY_BLOCKED_PATTERNS`, v0.23.0). A 403 is read as
auth first, so before this version each of them walked every key and two of them never stopped. The long-context 429 is
the one Hermes cures by compressing, which it could not do while KAME kept asking.
"""
from __future__ import annotations

import pytest

from tests.test_v1_8_1_4_robustness import carousel, dispatch_binding

SAFETY_PHRASES = (
    "flagged for possible cybersecurity risk",
    "trusted access for cyber",
    "violates our usage policies",
    "violates openai's usage policies",
    "your request was flagged by",
    "prompt was flagged by our safety",
    "responses cannot be generated due to safety",
    "content_filter",
    "responsibleaipolicyviolation",
    "new_sensitive",
    "content exists risk",
    "data may contain inappropriate content",
    "data_inspection_failed",
)
LONG_CONTEXT = "Extra usage is required for long context requests over 200k tokens"
KEY = "sk-safety-refusal-0001"
IDENT = "openai:gpt-x"


class _Refused(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status_code = status


def _fail(message, status):
    engine = carousel.Carousel()
    binding = dispatch_binding.DispatchBinding(engine=engine)
    decision = binding._on_failure(IDENT, KEY, _Refused(message, status), "x", 1, False)
    return decision[0], engine


def _rested(engine):
    """Whether the key came out of the failure with anything against it."""
    key, status = engine.select(IDENT, [KEY])
    return not (key == KEY and status == "SUCCESS")


@pytest.mark.parametrize("status", [400, 403])
@pytest.mark.parametrize("phrase", SAFETY_PHRASES)
def test_the_hosts_safety_phrases_end_the_attempt_and_rest_no_key(phrase, status):
    verdict, engine = _fail(f"Error: {phrase} (request id 7)", status)
    assert verdict == "raise"
    assert not _rested(engine)


@pytest.mark.parametrize("phrase", SAFETY_PHRASES)
def test_the_match_ignores_case(phrase):
    assert carousel.is_terminal(None, phrase.upper(), 403)


def test_the_long_context_429_goes_back_to_the_host_to_compress():
    verdict, engine = _fail(LONG_CONTEXT, 429)
    assert verdict == "raise"
    assert not _rested(engine)


@pytest.mark.parametrize("message", [
    "API key blocked by admin",
    "key suspended for violating our content policy",
    "blocked for a previous policy violation",      # names an account: another key may answer
    "Permission denied",
])
def test_a_refusal_of_the_key_still_tries_the_next_key(message):
    verdict, _engine = _fail(message, 403)
    assert verdict != "raise"


@pytest.mark.parametrize("message", [
    "Request blocked by rate limiting rule",
    "Too many requests: blocked by rate limiter",
    "Resource has been exhausted (e.g. check quota).",
])
def test_a_throttle_stays_a_throttle(message):
    verdict, _engine = _fail(message, 429)
    assert verdict != "raise"
