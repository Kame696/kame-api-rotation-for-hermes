"""Tests for the KAME core. No Hermes, no network, no filesystem.

The fixtures below are grouped by provider. The Google and OpenRouter ones
are verbatim captures. The others are reconstructed from each provider's
documented error shape — the point of the evidence cascade is that it never
needs to know which provider it is looking at, so a reconstruction that
carries the right *shape* exercises the same code a live response would.

That last sentence is true and was twice a trap, because a reconstruction
carries the shape its author remembered. It cost v0.1.0-v0.1.2, where every
Google body stopped at a sentence Google had stopped sending, and again in
v0.1.4, where the OpenRouter fixture put its rate-limit headers in the
headers — which is the one place OpenRouter does not put them. **A fixture
written from memory tests the memory.** Where a payload here is a capture,
it says so; where it is not, it is not evidence about that provider.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The core ships inside the plugin package (self-contained, the way the real
# loader expects). Point at that directory so it imports as a plain package.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation")
)

from core import (  # noqa: E402
    QuotaScope,
    QuotaWindow,
    classify,
    compute_reset_at,
    detect_quota_window,
    extract_from_headers,
    extract_retry_delay_seconds,
    looks_like_google,
    looks_like_upstream_wrapper,
    parse_absolute_timestamp,
    parse_duration_to_seconds,
    seconds_until_pacific_midnight,
)
from core.quota import SOURCE_ANCHOR, extract_from_body  # noqa: E402

NOW = 1_000_000.0


# ── Google (verbatim) ─────────────────────────────────────────────────────

# Captured 2026-08-16 from generativelanguage.googleapis.com, by sending one
# request with an obviously fake key: no credential of anyone's was used and
# no quota was spent. Recorded because the wording of this one was, until
# then, the last Google payload here still written from memory — and the
# lesson of v0.1.3 is that a remembered sentence is a test of the memory.
# It happens to match, byte for byte, what was written from memory. The
# capture stays anyway: what makes it evidence is that it came from Google.
#
# Two things in it that are not in the prose: `reason: API_KEY_INVALID`, a
# machine-readable code, and a `LocalizedMessage` — which raises the obvious
# question of whether the message translates, since every pattern here is
# English. Checked, not assumed: `Accept-Language: pt-BR`, `ja` and `de` all
# come back `locale: en-US` with identical text. So no locale handling was
# written, and no reason-code pattern either: the prose patterns already
# catch this payload, and a guard nothing can fell does not go in.
INVALID_KEY_BODY = {
    "error": {
        "code": 400,
        "message": "API key not valid. Please pass a valid API key.",
        "status": "INVALID_ARGUMENT",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "domain": "googleapis.com",
                "metadata": {"service": "generativelanguage.googleapis.com"},
            },
            {
                "@type": "type.googleapis.com/google.rpc.LocalizedMessage",
                "locale": "en-US",
                "message": "API key not valid. Please pass a valid API key.",
            },
        ],
    }
}

PER_MINUTE_BODY = {
    "error": {
        "code": 429,
        "message": "Resource has been exhausted (e.g. check quota).",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
        ],
    }
}

PER_DAY_BODY = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"},
        ],
    }
}

# The sentence Google actually sends today, in full. The two bodies above
# carry older wordings that are still in the wild — this is the current one,
# and it is word-for-word OpenAI's out-of-credits message with a rate-limit
# docs link glued on. Every free-tier 429 wears it, per-minute ones included.
#
# It is here because the fixtures above were *kinder than the provider*: they
# stop at "You exceeded your current quota." and the suite was green on a
# sentence Google does not send. See ``TestTheSentenceGoogleSendsOnEveryThrottle``.
GOOGLE_BILLING_SENTENCE = (
    "You exceeded your current quota, please check your plan and billing "
    "details. For more information on this error, read the docs: "
    "https://ai.google.dev/gemini-api/docs/rate-limits."
)


def _google_free_tier(quota_id, retry_delay="21s"):
    """A verbatim free-tier 429, parameterised only by which counter blew."""
    return {
        "error": {
            "code": 429,
            "message": GOOGLE_BILLING_SENTENCE,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaMetric": (
                                "generativelanguage.googleapis.com/"
                                "generate_content_free_tier_requests"
                            ),
                            "quotaId": quota_id,
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": retry_delay,
                },
            ],
        }
    }


GOOGLE_SENTENCE_RPM_BODY = _google_free_tier(
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
)
GOOGLE_SENTENCE_RPD_BODY = _google_free_tier(
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier", retry_delay="37s"
)

# ── other providers (documented shapes) ───────────────────────────────────

OPENAI_RPM_BODY = {
    "error": {
        "message": (
            "Rate limit reached for gpt-4 in organization org-abc on requests "
            "per min (RPM): Limit 3, Used 3, Requested 1. Please try again in 20s."
        ),
        "type": "requests",
        "code": "rate_limit_exceeded",
    }
}

OPENAI_QUOTA_BODY = {
    "error": {
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "type": "insufficient_quota",
        "code": "insufficient_quota",
    }
}

ANTHROPIC_RATE_BODY = {
    "type": "error",
    "error": {
        "type": "rate_limit_error",
        "message": "Number of requests has exceeded your rate limit.",
    },
}

GROQ_DAILY_BODY = {
    "error": {
        "message": (
            "Rate limit reached for model llama-3.3-70b in organization org-x on "
            "requests per day (RPD): Limit 14400, Used 14400. "
            "Please try again in 6m11.52s."
        ),
        "type": "requests",
        "code": "rate_limit_exceeded",
    }
}


class _Exc(Exception):
    """Stand-in for an SDK exception carrying retry metadata."""

    def __init__(self, **attributes):
        super().__init__(attributes.pop("message", "boom"))
        for key, value in attributes.items():
            setattr(self, key, value)


class _Duration:
    """protobuf Duration shape, as Google's client attaches it."""

    def __init__(self, seconds=0, nanos=0):
        self.seconds = seconds
        self.nanos = nanos


# ── duration + timestamp primitives ───────────────────────────────────────


class TestParseDuration:
    @pytest.mark.parametrize("text,expected", [
        ("21s", 21.0),
        ("1500ms", 1.5),
        ("45 seconds", 45.0),
        ("2h 30m", 9000.0),
        ("6m 11.52s", 371.52),
        ("6m0s", 360.0),          # OpenAI reset-header form
        ("2970.938289688s", 2970.938289688),
        ("90", 90.0),             # bare number reads as seconds
    ])
    def test_compound_and_simple(self, text, expected):
        assert parse_duration_to_seconds(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", [None, "", "   ", "soon", "n/a"])
    def test_no_duration(self, text):
        assert parse_duration_to_seconds(text) is None


class TestParseAbsoluteTimestamp:
    def test_rfc3339_with_z(self):
        # Anthropic's anthropic-ratelimit-*-reset format.
        got = parse_absolute_timestamp("2026-08-15T18:30:00Z")
        assert got == datetime(2026, 8, 15, 18, 30, tzinfo=timezone.utc).timestamp()

    def test_rfc3339_with_offset(self):
        got = parse_absolute_timestamp("2026-08-15T18:30:00+00:00")
        assert got == datetime(2026, 8, 15, 18, 30, tzinfo=timezone.utc).timestamp()

    def test_http_date(self):
        # The other half of RFC 7231's Retry-After.
        got = parse_absolute_timestamp("Wed, 21 Oct 2026 07:28:00 GMT")
        assert got == datetime(2026, 10, 21, 7, 28, tzinfo=timezone.utc).timestamp()

    def test_epoch_seconds(self):
        assert parse_absolute_timestamp("1786829619") == 1786829619.0

    def test_epoch_milliseconds(self):
        assert parse_absolute_timestamp("1786829619000") == 1786829619.0

    @pytest.mark.parametrize("value", [None, "", "later", "0", "42", -5])
    def test_not_a_timestamp(self, value):
        # 42 is a relative delay, not an epoch — reading it as one would put
        # the reset in 1970 and release the key immediately.
        assert parse_absolute_timestamp(value) is None


# ── the evidence cascade ──────────────────────────────────────────────────


class TestExceptionSource:
    def test_retry_after_attribute(self):
        seconds, source = extract_retry_delay_seconds(error=_Exc(retry_after=17))
        assert seconds == 17.0
        assert source == "exception.retry_after"

    def test_google_retry_delay_duration(self):
        error = _Exc(retry_delay=_Duration(seconds=21, nanos=500_000_000))
        seconds, source = extract_retry_delay_seconds(error=error)
        assert seconds == pytest.approx(21.5)
        assert source == "exception.retry_delay"

    def test_absurd_attribute_is_rejected(self):
        assert extract_retry_delay_seconds(error=_Exc(retry_after=99_999_999))[0] is None

    def test_no_attributes(self):
        assert extract_retry_delay_seconds(error=_Exc())[0] is None


class TestHeaderSource:
    def test_retry_after_seconds(self):
        seconds, source = extract_from_headers({"Retry-After": "30"}, NOW)
        assert seconds == 30.0
        assert source == "header.retry-after"

    def test_retry_after_http_date(self):
        moment = datetime.now(timezone.utc) + timedelta(minutes=5)
        stamp = moment.strftime("%a, %d %b %Y %H:%M:%S GMT")
        seconds, _ = extract_from_headers({"retry-after": stamp}, time.time())
        assert 250 <= seconds <= 310

    def test_openai_reset_header_duration(self):
        seconds, source = extract_from_headers(
            {"x-ratelimit-reset-requests": "6m0s"}, NOW
        )
        assert seconds == 360.0
        assert "x-ratelimit-reset-requests" in source

    def test_anthropic_reset_header_timestamp(self):
        now = time.time()
        moment = datetime.now(timezone.utc) + timedelta(seconds=90)
        seconds, _ = extract_from_headers(
            {"anthropic-ratelimit-requests-reset": moment.isoformat().replace("+00:00", "Z")},
            now,
        )
        assert 85 <= seconds <= 95

    def test_retry_after_beats_reset_headers(self):
        # Retry-After is what the provider chose to say; reset headers are
        # ambient state.
        seconds, source = extract_from_headers(
            {"retry-after": "5", "x-ratelimit-reset-requests": "600s"}, NOW
        )
        assert seconds == 5.0
        assert source == "header.retry-after"

    def test_longest_reset_header_wins(self):
        # Releasing early re-hammers a limit that is still spent; benching a
        # little long costs one key out of a pool.
        seconds, _ = extract_from_headers(
            {"x-ratelimit-reset-tokens": "6s", "x-ratelimit-reset-requests": "6m"}, NOW
        )
        assert seconds == 360.0

    def test_unrelated_headers_ignored(self):
        assert extract_from_headers(
            {"content-type": "application/json", "x-request-id": "abc123"}, NOW
        )[0] is None

    @pytest.mark.parametrize("headers", [None, {}, [], "nope", 42])
    def test_malformed_headers(self, headers):
        assert extract_from_headers(headers, NOW)[0] is None

    def test_header_pairs_sequence(self):
        seconds, _ = extract_from_headers([("Retry-After", "12")], NOW)
        assert seconds == 12.0


class TestBodyAndTextSources:
    def test_google_structured_retry_info(self):
        seconds, source = extract_retry_delay_seconds(body=PER_MINUTE_BODY)
        assert seconds == 21.0
        assert source.startswith("body.")

    def test_milliseconds_in_body(self):
        seconds, _ = extract_retry_delay_seconds(body={"error": {"retryDelay": "1500ms"}})
        assert seconds == 1.5

    def test_the_duration_google_actually_serialises(self):
        # ``google.rpc.RetryInfo.retryDelay`` is a Duration *message*. The
        # REST endpoint renders it as the string ``"21s"``, but canonical
        # proto JSON — which is what gRPC-JSON transcoding and the GenAI SDKs
        # produce — renders it as an object. The body walker skipped every
        # dict value, so this whole shape read as "no delay at all".
        seconds, source = extract_retry_delay_seconds(
            body={
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": {"seconds": 30, "nanos": 500000000},
                        }
                    ]
                }
            }
        )
        assert seconds == 30.5
        assert source == "body.retryDelay"

    def test_a_duration_of_whole_seconds_only(self):
        seconds, _ = extract_retry_delay_seconds(
            body={"error": {"retryDelay": {"seconds": "21"}}}
        )
        assert seconds == 21.0

    def test_a_dict_that_is_not_a_duration_is_not_a_duration(self):
        # Only ``seconds``/``nanos`` make a dict a Duration. A retry *policy*
        # under a retry-shaped key is somebody's metadata, and answering
        # "zero seconds" for it would be inventing a reading — even though the
        # bound below happens to reject zero today. Asserted against the
        # helper rather than the cascade, because that is where the promise
        # is made and the only place a wrong answer is observable.
        from core.quota import _duration_message

        assert _duration_message({"policy": "exponential", "max": 5}) is None
        assert _duration_message({"nanos": 500000000}) == 0.5
        assert _duration_message("21s") is None

    def test_and_the_cascade_reads_nothing_out_of_one(self):
        seconds, _ = extract_retry_delay_seconds(
            body={"error": {"retryDelay": {"policy": "exponential", "max": 5}}}
        )
        assert seconds is None

    def test_openai_message_text(self):
        seconds, source = extract_retry_delay_seconds(
            message=OPENAI_RPM_BODY["error"]["message"]
        )
        assert seconds == 20.0
        assert source == "text"

    def test_groq_compound_message_text(self):
        seconds, _ = extract_retry_delay_seconds(
            message=GROQ_DAILY_BODY["error"]["message"]
        )
        assert seconds == pytest.approx(371.52)

    def test_zero_delay_is_not_a_delay(self):
        assert extract_retry_delay_seconds(body={"error": {"retryDelay": "0s"}})[0] is None

    @pytest.mark.parametrize("message,expected", [
        ("Please try again in 20s.", 20.0),
        ("please try again in 6m11.52s.", 371.52),
        ("Weekly limit reached. Resets in 4hr 5min", 4 * 3600 + 5 * 60),
        ("retry after 45 seconds", 45.0),
        ("wait for 2h 30m before retrying", 9000.0),
        ("available in 1500ms", 1.5),
        ("retry after 30", 30.0),
    ])
    def test_text_forms(self, message, expected):
        assert extract_retry_delay_seconds(message=message)[0] == pytest.approx(expected)

    def test_a_date_after_a_retry_keyword_is_not_seconds(self):
        # Without the guard, the leading 2026 would bench a key for 34 minutes.
        assert extract_retry_delay_seconds(
            message="retry after 2026-08-15T18:30:00Z"
        )[0] is None

    def test_a_unit_letter_starting_a_word_is_not_a_unit(self):
        # The `m` of "megabytes" is not minutes.
        seconds, _ = extract_retry_delay_seconds(message="retry after 5 megabytes")
        assert seconds == 5.0

    def test_model_name_is_not_read_as_seconds(self):
        # The reason text extraction anchors on a retry keyword.
        assert extract_retry_delay_seconds(
            message="gemini-3.6-flash failed after 500 tokens"
        )[0] is None

    def test_nothing_at_all(self):
        assert extract_retry_delay_seconds(message="internal server error")[0] is None


class TestCascadeOrder:
    def test_exception_outranks_headers_body_and_text(self):
        seconds, source = extract_retry_delay_seconds(
            message="please try again in 99s",
            body=PER_MINUTE_BODY,
            headers={"retry-after": "50"},
            error=_Exc(retry_after=7),
        )
        assert (seconds, source) == (7.0, "exception.retry_after")

    def test_headers_outrank_body_and_text(self):
        seconds, source = extract_retry_delay_seconds(
            message="please try again in 99s",
            body=PER_MINUTE_BODY,
            headers={"retry-after": "50"},
        )
        assert (seconds, source) == (50.0, "header.retry-after")

    def test_body_outranks_text(self):
        seconds, source = extract_retry_delay_seconds(
            message="please try again in 99s", body=PER_MINUTE_BODY
        )
        assert seconds == 21.0
        assert source.startswith("body.")


# ── quota windows ─────────────────────────────────────────────────────────


class TestQuotaWindow:
    def test_google_per_minute(self):
        assert detect_quota_window(body=PER_MINUTE_BODY) == QuotaWindow.PER_MINUTE

    def test_google_per_day(self):
        assert detect_quota_window(body=PER_DAY_BODY) == QuotaWindow.PER_DAY

    def test_openai_rpm_text(self):
        window = detect_quota_window(message=OPENAI_RPM_BODY["error"]["message"])
        assert window == QuotaWindow.PER_MINUTE

    def test_groq_rpd_text(self):
        window = detect_quota_window(message=GROQ_DAILY_BODY["error"]["message"])
        assert window == QuotaWindow.PER_DAY

    def test_openai_insufficient_quota_is_account(self):
        window = detect_quota_window(message=OPENAI_QUOTA_BODY["error"]["message"],
                                     body=OPENAI_QUOTA_BODY)
        assert window == QuotaWindow.ACCOUNT

    @pytest.mark.parametrize("text,expected", [
        ("requests_per_day exceeded", QuotaWindow.PER_DAY),
        ("Requests-Per-Minute exceeded", QuotaWindow.PER_MINUTE),
        ("weekly limit reached", QuotaWindow.PER_WEEK),
        ("monthly quota exhausted", QuotaWindow.PER_MONTH),
        ("hourly rate cap", QuotaWindow.PER_HOUR),
    ])
    def test_separator_and_period_variants(self, text, expected):
        assert detect_quota_window(text) == expected

    def test_widest_window_wins(self):
        # A body naming both is binding on the wider one; treating a spent
        # daily counter as a per-minute blip re-hammers it all day.
        assert detect_quota_window("PerMinute and PerDay limits") == QuotaWindow.PER_DAY

    def test_google_generic_sentence_is_not_account_level(self):
        # "You exceeded your current quota." is Google's wording on *every*
        # free-tier 429, per-minute included. Reading it as account-level
        # would bench a key for a day over a 20-second throttle.
        assert detect_quota_window(body=PER_DAY_BODY) != QuotaWindow.ACCOUNT

    def test_unknown(self):
        assert detect_quota_window("something went wrong") == QuotaWindow.UNKNOWN


class TestLooksLikeGoogle:
    def test_by_provider_name(self):
        for name in ("gemini", "google", "GOOGLE-GEMINI", "vertex"):
            assert looks_like_google(provider=name)

    def test_by_body_fingerprint(self):
        # A proxy forwarding a Google error still gets Google's reset rule.
        assert looks_like_google(provider="openrouter", body=PER_DAY_BODY)

    def test_negative(self):
        assert not looks_like_google(provider="anthropic", body=ANTHROPIC_RATE_BODY)


class TestPacificMidnight:
    def test_is_within_one_day(self):
        assert 0 < seconds_until_pacific_midnight() <= 24 * 3600

    def test_daylight_time(self):
        # 2026-06-15 08:00 UTC == 01:00 PDT (UTC-7). 23h to midnight.
        moment = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)
        assert seconds_until_pacific_midnight(moment) == pytest.approx(23 * 3600, abs=1)

    def test_standard_time(self):
        # 2026-01-15 09:00 UTC == 01:00 PST (UTC-8). 23h to midnight.
        moment = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
        assert seconds_until_pacific_midnight(moment) == pytest.approx(23 * 3600, abs=1)


# ── reconciliation ────────────────────────────────────────────────────────


class TestComputeResetAt:
    def test_per_minute_uses_provider_delay(self):
        decision = compute_reset_at(now_epoch=NOW, provider="gemini", body=PER_MINUTE_BODY)
        assert decision.window == QuotaWindow.PER_MINUTE
        assert decision.reset_at == NOW + 21.0

    def test_google_daily_ignores_short_delay_and_waits_for_midnight(self):
        # The daily body carries retryDelay 37s. Obeying it returns a spent
        # key to rotation every 37 seconds for the rest of the day.
        #
        # Asserted against Pacific midnight rather than against "more than an
        # hour", because the honest answer is *less* than an hour for the
        # sixty minutes before midnight — and an assertion that only holds
        # for 23 hours a day is a suite that fails on the clock.
        decision = compute_reset_at(now_epoch=NOW, provider="gemini", body=PER_DAY_BODY)
        assert decision.window == QuotaWindow.PER_DAY
        assert decision.reset_at - NOW == pytest.approx(
            seconds_until_pacific_midnight(), abs=2
        )

    def test_google_daily_outlasts_the_host_default_at_a_normal_hour(self):
        # Same rule with the clock pinned: mid-morning Pacific, a spent daily
        # quota is benched for the rest of the day, not for the host's hour.
        morning = datetime(2026, 8, 16, 17, 0, tzinfo=timezone.utc)  # 10:00 PDT
        decision = compute_reset_at(
            now_epoch=NOW, provider="gemini", body=PER_DAY_BODY, now=morning
        )
        assert decision.reset_at - NOW == pytest.approx(14 * 3600, abs=2)

    def test_the_midnight_deadline_says_it_came_from_the_calendar(self):
        # It matters downstream. ``escalate`` corrects a deadline that proves
        # short by scaling it, which is right for a length and meaningless for
        # an instant — and it cannot tell them apart from the window, because
        # a non-Google daily cap is an hourly re-probe under the same name.
        decision = compute_reset_at(now_epoch=NOW, provider="gemini", body=PER_DAY_BODY)
        assert decision.source == SOURCE_ANCHOR

    def test_and_says_so_even_when_a_header_offered_a_delay(self):
        # The header's delay was read and then deliberately discarded three
        # lines earlier. Reporting it as the source of a deadline it did not
        # produce is the kind of small lie that costs a later version an hour.
        decision = compute_reset_at(
            now_epoch=NOW, provider="gemini", body=PER_DAY_BODY,
            headers={"retry-after": "37"},
        )
        assert decision.source == SOURCE_ANCHOR
        assert decision.reset_at - NOW == pytest.approx(
            seconds_until_pacific_midnight(), abs=2
        )

    def test_non_google_daily_reprobes_hourly(self):
        # Without knowing the provider's reset clock, re-probing hourly is
        # the honest choice; midnight US/Pacific is a Google fact, not a
        # universal one.
        decision = compute_reset_at(
            now_epoch=NOW, provider="groq",
            message=GROQ_DAILY_BODY["error"]["message"],
        )
        assert decision.window == QuotaWindow.PER_DAY
        assert decision.reset_at - NOW == pytest.approx(3600, abs=1)

    def test_long_window_keeps_a_long_provider_delay(self):
        # A *long* delay on a long window is the provider being specific,
        # not misleading, so it is respected.
        decision = compute_reset_at(
            now_epoch=NOW, provider="opencode-go",
            message="Weekly limit reached. Resets in 4hr 5min",
        )
        assert decision.window == QuotaWindow.PER_WEEK
        assert decision.reset_at - NOW == pytest.approx(4 * 3600 + 5 * 60, abs=1)

    def test_per_minute_without_delay_gets_about_a_minute(self):
        decision = compute_reset_at(
            now_epoch=NOW,
            body={"error": {"message": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}},
        )
        assert 60 <= decision.reset_at - NOW <= 120

    def test_declines_without_any_signal(self):
        decision = compute_reset_at(now_epoch=NOW, message="boom")
        assert decision.reset_at is None
        assert decision.window == QuotaWindow.UNKNOWN

    def test_clamps_absurd_delay(self):
        decision = compute_reset_at(
            now_epoch=NOW, body={"error": {"retryDelay": "9999999s"}}
        )
        # Out of bounds means "not evidence", so it declines rather than
        # inventing a ceiling value.
        assert decision.reset_at is None

    def test_rationale_is_populated(self):
        decision = compute_reset_at(now_epoch=NOW, provider="gemini", body=PER_MINUTE_BODY)
        assert decision.rationale
        assert decision.source


# ── verdicts ──────────────────────────────────────────────────────────────


class TestClassify:
    def test_no_provider_allowlist(self):
        # The whole point of v0.0.3: an unknown provider that supplies
        # evidence is served, not ignored.
        for name in ("gemini", "openrouter", "groq", "some-provider-from-2029"):
            verdict = classify(
                provider=name, status_code=429,
                headers={"retry-after": "42"},
                error_message="rate limit exceeded",
                now_epoch=NOW,
            )
            assert verdict is not None, name
            assert verdict.reset_at == NOW + 42.0

    def test_openai_rpm(self):
        verdict = classify(
            provider="openai", status_code=429,
            error_message=OPENAI_RPM_BODY["error"]["message"],
            error_body=OPENAI_RPM_BODY, now_epoch=NOW,
        )
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.PER_MINUTE
        assert verdict.reset_at == NOW + 20.0

    def test_openai_insufficient_quota_is_billing(self):
        verdict = classify(
            provider="openai", status_code=429,
            error_message=OPENAI_QUOTA_BODY["error"]["message"],
            error_body=OPENAI_QUOTA_BODY, now_epoch=NOW,
        )
        assert verdict.reason == "billing"
        assert verdict.retryable is False

    def test_anthropic_rate_limit_via_headers(self):
        verdict = classify(
            provider="anthropic", status_code=429,
            error_message=ANTHROPIC_RATE_BODY["error"]["message"],
            error_body=ANTHROPIC_RATE_BODY,
            headers={"retry-after": "30"}, now_epoch=NOW,
        )
        assert verdict.reason == "rate_limit"
        assert verdict.reset_at == NOW + 30.0

    def test_groq_daily(self):
        verdict = classify(
            provider="groq", status_code=429,
            error_message=GROQ_DAILY_BODY["error"]["message"],
            error_body=GROQ_DAILY_BODY, now_epoch=NOW,
        )
        assert verdict.quota_window == QuotaWindow.PER_DAY
        assert verdict.reset_at - NOW > 371.52  # not the misleading short hint

    def test_google_per_minute(self):
        verdict = classify(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY, now_epoch=NOW
        )
        assert verdict.reason == "rate_limit"
        assert verdict.should_rotate_credential is True
        assert verdict.reset_at == NOW + 21.0

    def test_google_daily_benches_until_the_quota_actually_resets(self):
        verdict = classify(
            provider="gemini", status_code=429, error_body=PER_DAY_BODY, now_epoch=NOW
        )
        assert verdict.quota_window == QuotaWindow.PER_DAY
        assert verdict.reset_at - NOW == pytest.approx(
            seconds_until_pacific_midnight(), abs=2
        )

    def test_invalid_key_is_permanent(self):
        verdict = classify(
            provider="gemini", status_code=400,
            error_message="API key not valid. Please pass a valid API key.",
        )
        assert verdict.reason == "auth_permanent"
        assert verdict.retryable is False
        assert verdict.reset_at is None

    def test_the_whole_payload_google_actually_sends_reads_the_same(self):
        # The message alone was what the case above tested. This is the body
        # as it arrives, nested duplicate message and machine-readable reason
        # code included — none of which may talk the classifier out of it.
        verdict = classify(
            provider="gemini", status_code=400,
            error_message=INVALID_KEY_BODY["error"]["message"],
            error_body=INVALID_KEY_BODY,
        )
        assert verdict.reason == "auth_permanent"
        assert verdict.retryable is False
        assert verdict.should_rotate_credential is True
        assert verdict.reset_at is None

    def test_permanent_marker_beats_a_429(self):
        # A dead key sometimes arrives wearing a 429. Benching it for a
        # minute means rediscovering it is dead a minute later, forever.
        verdict = classify(
            provider="gemini", status_code=429,
            error_message="API key not valid",
            error_body=PER_MINUTE_BODY,
        )
        assert verdict.reason == "auth_permanent"

    def test_billing_is_not_a_throttle(self):
        verdict = classify(
            provider="gemini", status_code=429,
            error_message="Billing account not enabled for this project.",
        )
        assert verdict.reason == "billing"
        assert verdict.retryable is False

    def test_403_denial_is_benched_not_killed(self):
        verdict = classify(
            provider="gemini", status_code=403,
            error_message="PERMISSION_DENIED: project has been denied access",
            now_epoch=NOW,
        )
        assert verdict.reason == "auth"
        assert verdict.reset_at == NOW + 3600

    def test_a_bare_403_is_left_to_the_host(self):
        # Status alone is not evidence. Hermes checks content-policy blocks
        # ahead of its own status routing, and this hook runs ahead of
        # everything — claiming every 403 would hijack a per-prompt safety
        # refusal and bench a healthy key over it.
        assert classify(provider="openai", status_code=403,
                        error_message="Request forbidden") is None

    def test_plain_401_is_left_to_the_host(self):
        # A transient 401 during token refresh is common and KAME knows
        # nothing the host does not.
        assert classify(provider="anthropic", status_code=401,
                        error_message="authentication failed") is None

    @pytest.mark.parametrize("case", [
        dict(provider="gemini", status_code=429, error_body=PER_MINUTE_BODY),
        dict(provider="openai", status_code=429, error_body=OPENAI_QUOTA_BODY,
             error_message=OPENAI_QUOTA_BODY["error"]["message"]),
        dict(provider="gemini", status_code=403,
             error_message="PERMISSION_DENIED: consumer suspended"),
        dict(provider="gemini", status_code=400, error_message="API key not valid"),
    ])
    def test_every_verdict_keeps_model_fallback_on(self, case):
        # Hermes expands the hook result into ClassifiedError, where
        # should_fallback defaults to False. It sets the flag on every
        # rate_limit, billing and auth it produces itself — so a verdict that
        # stayed quiet about it would silently switch model fallback off.
        assert classify(**case).should_fallback is True

    def test_declines_unclassifiable(self):
        assert classify(provider="gemini", status_code=418, error_message="teapot") is None

    def test_declines_429_without_any_quota_signal(self):
        assert classify(provider="gemini", status_code=429,
                        error_message="too many requests") is None

    def test_defaults_now_to_wall_clock(self):
        verdict = classify(provider="gemini", status_code=429, error_body=PER_MINUTE_BODY)
        assert verdict.reset_at == pytest.approx(time.time() + 21.0, abs=5)


class TestWhatSixProvidersActuallySayAboutADeadKey:
    """Captured 2026-08-16, one request each with an obviously fake key.

    No credential of anyone's was used and no quota was spent — an invalid
    key is refused before any metering — and the six bodies below are what
    came back, verbatim. Until this class, every one of these providers was
    represented here by a message reconstructed from documented format, which
    is the same class of fixture that was wrong about Google for four
    versions.

    It found the gap it was built to look for. Every permanent-auth pattern
    read *invalid key*, and two of the six say it the other way round: a
    genuinely dead Anthropic or DeepSeek key was handed back to the host as a
    bare 401 and rotated forever instead of being retired.
    """

    ANTHROPIC = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "API key is invalid."},
        "request_id": None,
    }
    DEEPSEEK = {
        "error": {
            "message": "Authentication Fails, Your api key: ****0000 is invalid",
            "type": "authentication_error",
            "param": None,
            "code": "invalid_request_error",
        }
    }
    OPENAI = {
        "error": {
            "message": (
                "Incorrect API key provided: sk-fake-***0000. You can find your "
                "API key at https://platform.openai.com/account/api-keys."
            ),
            "type": "invalid_request_error",
            "code": "invalid_api_key",
            "param": None,
        },
        "status": 401,
    }
    GROQ = {
        "error": {
            "message": "Invalid API Key",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }
    MISTRAL = {"detail": "Invalid API Key"}
    OPENROUTER = {"error": {"message": "Missing Authentication header", "code": 401}}

    @pytest.mark.parametrize("provider,body,message", [
        ("anthropic", ANTHROPIC, "API key is invalid."),
        ("deepseek", DEEPSEEK, DEEPSEEK["error"]["message"]),
        ("openai", OPENAI, OPENAI["error"]["message"]),
        ("groq", GROQ, "Invalid API Key"),
        ("mistral", MISTRAL, "Invalid API Key"),
    ])
    def test_a_dead_key_is_retired_whichever_way_the_provider_says_it(
        self, provider, body, message
    ):
        verdict = classify(
            provider=provider, status_code=401,
            error_message=message, error_body=body,
        )
        assert verdict is not None, provider
        assert verdict.reason == "auth_permanent", provider
        assert verdict.should_rotate_credential is True, provider
        assert verdict.reset_at is None, provider

    def test_a_missing_header_is_not_a_dead_key(self):
        # OpenRouter's answer to a request with no credential at all. Nothing
        # is wrong with any key, so retiring one over it would be a plugin
        # inventing a fault. It goes to the host with the rest of the bare
        # 401s.
        assert classify(
            provider="openrouter", status_code=401,
            error_message="Missing Authentication header",
            error_body=self.OPENROUTER,
        ) is None


class TestWhatHermesOwnCorpusSaid:
    """Two payloads this plugin got wrong, found by somebody else's tests.

    Every case above was written by the hand that wrote the classifier, which
    makes them a statement of intent. Hermes ships its own corpus — roughly
    fourteen providers, written by people who never saw this plugin — and
    ``tools/host_corpus.py`` runs it twice, with and without KAME behind the
    real hook dispatch. Two of its cases changed verdict, and in both the host
    was right.

    Both had the same shape: a word broad enough to appear in a message that
    is not about the credential at all. And both were invisible here, because
    no test in this project ever fed the plugin the exact words a provider
    uses — ``test_plain_401_is_left_to_the_host`` passed throughout while
    saying "authentication failed", which is not what a 401 says.
    """

    def test_a_bare_401_does_not_retire_the_key(self):
        # `Unauthorized` is the HTTP reason phrase, so it arrives on every
        # bare 401 — proxies, gateways, an OAuth token halfway through a
        # refresh. Reading it as a dead key discards a credential that was
        # about to work. The host's verdict is `auth`: rotate, do not retire.
        assert classify(provider="openrouter", status_code=401,
                        error_message="Unauthorized") is None

    def test_the_words_that_do_retire_a_key_still_do(self):
        # The narrowing must not cost the real case: a provider that has
        # genuinely retired a key always says more than "Unauthorized".
        for message in (
            "API key not valid. Please pass a valid API key.",
            "Incorrect API key provided",
            "invalid_api_key",
            "Your API key has been revoked",
            "invalid authentication",
        ):
            verdict = classify(provider="openai", status_code=401,
                               error_message=message)
            assert verdict is not None, message
            assert verdict.reason == "auth_permanent", message

    def test_a_missing_model_is_not_a_credential_problem(self):
        # 404 `model not found` is about the model name. The host answers
        # `model_not_found` — try another model, leave the credential alone.
        # KAME answered `auth` and rotated, which walks the whole pool over a
        # misspelt model name and benches every key in it for an hour.
        assert classify(status_code=404, error_message="model not found") is None

    def test_a_model_the_key_may_not_use_is_still_a_credential_problem(self):
        # The neighbouring wordings are about this key's tier, and they stay.
        for message in ("model not authorized", "model not available for your tier"):
            verdict = classify(provider="gemini", status_code=403,
                               error_message=message, now_epoch=NOW)
            assert verdict is not None, message
            assert verdict.reason == "auth", message
            assert verdict.reset_at == NOW + 3600, message


class TestTheSentenceGoogleSendsOnEveryThrottle:
    """The worst failure this plugin could have, and it shipped four times.

    Google's current free-tier 429 says, word for word, *"You exceeded your
    current quota, please check your plan and billing details."* — OpenAI's
    out-of-credits sentence, with a rate-limit docs link appended. It says it
    for a **twenty-one second** per-minute throttle.

    Read as billing, every consequence compounds in the same direction:

    * benched twenty-four hours for a twenty-one second wait;
    * ``account`` scope, so every other model on that key goes down too, and
      the per-model ledger this plugin exists for is bypassed;
    * ``billing`` is in ``probe.NEVER_PROBE_REASONS``, so the escape hatch
      that exists precisely for a wrong long deadline **cannot fire** — with
      one key the agent is dead for a day and nothing can discover it;
    * ``account`` is in ``escalate.NEVER_STRETCH_WINDOWS`` and the reason
      forbids stretching, so v0.1.0–v0.1.2 were inert against real Google
      traffic — including the anchor correction written *for this provider*.

    The right answers were already in the payload and were thrown away:
    ``detect_quota_window`` reads ``per_minute`` off the quotaId,
    ``detect_quota_scope`` reads ``per_model``, and ``RetryInfo`` carries
    ``21s``. ``classify`` returned at step 2 without asking.

    The discriminator is not a provider name — it is that **a provider which
    tells you when to come back is not telling you that you are out of
    money.** A depletion has nothing to wait for and no window to name.
    """

    def test_the_per_minute_throttle_is_a_throttle(self):
        verdict = classify(
            provider="gemini", status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE,
            error_body=GOOGLE_SENTENCE_RPM_BODY, now_epoch=NOW,
        )
        assert verdict.reason == "rate_limit"
        assert verdict.retryable is True
        assert verdict.quota_window == QuotaWindow.PER_MINUTE
        assert verdict.reset_at == NOW + 21.0

    def test_and_the_rest_of_the_key_stays_up(self):
        # ``account`` scope would bench the auxiliary model too, which is the
        # whole thing the per-model ledger was built to prevent.
        verdict = classify(
            provider="gemini", status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE,
            error_body=GOOGLE_SENTENCE_RPM_BODY, now_epoch=NOW,
        )
        assert verdict.quota_scope == QuotaScope.PER_MODEL

    def test_and_the_escape_hatch_is_not_disarmed(self):
        from core import probe

        verdict = classify(
            provider="gemini", status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE,
            error_body=GOOGLE_SENTENCE_RPM_BODY, now_epoch=NOW,
        )
        assert not any(marker in verdict.reason for marker in probe.NEVER_PROBE_REASONS)

    def test_the_daily_cap_is_still_read_as_the_daily_cap(self):
        verdict = classify(
            provider="gemini", status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE,
            error_body=GOOGLE_SENTENCE_RPD_BODY, now_epoch=NOW,
        )
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.PER_DAY
        # And it reaches the anchor branch v0.1.2 wrote for exactly this case,
        # which until now no real Google payload could ever get to.
        assert verdict.source == SOURCE_ANCHOR

    def test_the_sentence_in_the_message_alone_is_enough_to_be_read_wrong(self):
        # The host hands the hook both halves, and a body that fails to parse
        # leaves only the message. The window still comes off the text.
        verdict = classify(
            provider="gemini", status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE + " Please retry in 21s.",
            now_epoch=NOW,
        )
        assert verdict.reason == "rate_limit"
        assert verdict.reset_at == NOW + 21.0

    def test_a_named_counter_settles_it_with_no_delay_in_sight(self):
        # Google omits ``RetryInfo`` on plenty of daily refusals. The delay is
        # then gone but the quotaId still names the counter that blew, and a
        # balance has no counter — so this is a throttle, sized from the
        # window's own reset.
        body = {
            "error": {
                "code": 429,
                "message": GOOGLE_BILLING_SENTENCE,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                        ],
                    }
                ],
            }
        }
        verdict = classify(
            provider="gemini", status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE, error_body=body, now_epoch=NOW,
        )
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.PER_DAY

    def test_a_wait_that_cannot_be_read_is_still_a_wait(self):
        # Proto3 JSON omits fields sitting at their default, so a
        # ``RetryInfo`` whose delay is zero serialises as the ``@type`` and
        # nothing else. There is no number to read and no window named — the
        # only thing left saying "come back later" is that the provider
        # attached a RetryInfo at all. Without reading that, this lands on
        # billing: a day benched, at account scope, unprobeable.
        body = {
            "error": {
                "code": 429,
                "message": GOOGLE_BILLING_SENTENCE,
                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo"}],
            }
        }
        verdict = classify(
            provider="some-gateway", status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE, error_body=body, now_epoch=NOW,
        )
        assert verdict is None or verdict.reason != "billing"

    # ── the controls: what must not move ─────────────────────────────────

    def test_openai_out_of_credits_is_still_billing(self):
        # ``insufficient_quota`` is decisive on its own and never a throttle.
        verdict = classify(
            provider="openai", status_code=429,
            error_message=OPENAI_QUOTA_BODY["error"]["message"],
            error_body=OPENAI_QUOTA_BODY, now_epoch=NOW,
        )
        assert verdict.reason == "billing"

    def test_and_stays_billing_even_when_a_retry_after_rides_along(self):
        # A decisive marker outranks the ambiguity check. Some gateways attach
        # a stock Retry-After to everything, including a depletion.
        verdict = classify(
            provider="openai", status_code=429,
            error_message=OPENAI_QUOTA_BODY["error"]["message"],
            error_body=OPENAI_QUOTA_BODY,
            headers={"retry-after": "30"}, now_epoch=NOW,
        )
        assert verdict.reason == "billing"

    def test_anthropic_low_balance_is_still_billing(self):
        verdict = classify(
            provider="anthropic", status_code=400,
            error_message="Your credit balance is too low to access the Anthropic API",
            now_epoch=NOW,
        )
        assert verdict.reason == "billing"

    def test_the_bare_sentence_with_no_wait_and_no_window_is_still_billing(self):
        # Nothing to wait for and no counter named: this is the shape the
        # pattern was written for, and it does not move.
        verdict = classify(
            provider="some-gateway", status_code=429,
            error_message=(
                "You exceeded your current quota, please check your plan "
                "and billing details."
            ),
            now_epoch=NOW,
        )
        assert verdict.reason == "billing"
        assert verdict.quota_window == QuotaWindow.ACCOUNT


class TestProviderIsBusyNotSpent:
    """Congestion is not a spent credential, and Hermes already knows it.

    Its 5xx and overload paths deliberately set no `should_rotate_credential`
    (#14038: rotating "exhausts the pool while the endpoint is still busy,
    and does nothing for a single-key user"). Since `reset_at` is only ever
    applied by `mark_exhausted_and_rotate()`, any cooldown KAME wanted here
    would need exactly the rotation that must not happen. So it declines.
    """

    def test_declines_a_529_even_with_an_explicit_wait(self):
        assert classify(
            provider="anthropic", status_code=529, error_message="Overloaded",
            headers={"retry-after": "8"}, now_epoch=NOW,
        ) is None

    def test_declines_a_503(self):
        assert classify(provider="openai", status_code=503,
                        error_message="service unavailable") is None

    def test_declines_a_500_with_a_retry_header(self):
        assert classify(
            provider="openai", status_code=500, error_message="internal error",
            headers={"retry-after": "30"}, now_epoch=NOW,
        ) is None

    @pytest.mark.parametrize("text", [
        "Overloaded",
        "The service is temporarily overloaded",
        "Model is over capacity, please retry",
        "server is busy",
    ])
    def test_declines_congestion_wearing_a_429(self, text):
        # A 429 whose body says "overloaded" is congestion in a throttle's
        # status code. Treating it as a spent key is the same mistake in a
        # different disguise — and it is the one Hermes fixed in #14038.
        assert classify(
            provider="anthropic", status_code=429, error_message=text,
            headers={"retry-after": "8"}, now_epoch=NOW,
        ) is None

    def test_a_real_throttle_is_still_served(self):
        verdict = classify(
            provider="anthropic", status_code=429,
            error_message="Number of requests has exceeded your rate limit.",
            headers={"retry-after": "8"}, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.reset_at == NOW + 8.0


class TestUpstreamWrapper:
    """An aggregator relaying somebody else's failure is not our problem.

    Hermes classifies this correctly on its own — `upstream_rate_limit`, no
    rotation, fall back to another model. This hook runs *before* that logic,
    so speaking here would override a better answer with a worse one.
    """

    WRAPPED_429 = {
        "error": {
            "code": 429,
            "message": "Provider returned error",
            "metadata": {
                "provider_name": "DeepSeek",
                "raw": '{"error":{"message":"Rate limit reached, try again in 30s"}}',
            },
        }
    }

    WRAPPED_AUTH = {
        "error": {
            "code": 401,
            "message": "Provider returned error",
            "metadata": {
                "provider_name": "DeepSeek",
                "raw": '{"error":{"message":"API key not valid"}}',
            },
        }
    }

    def test_detects_the_envelope(self):
        assert looks_like_upstream_wrapper(self.WRAPPED_429)

    def test_detects_metadata_shape_without_the_phrase(self):
        assert looks_like_upstream_wrapper(
            {"error": {"message": "Upstream error", "metadata": {"raw": "{}"}}}
        )

    def test_a_plain_error_is_not_a_wrapper(self):
        assert not looks_like_upstream_wrapper(PER_MINUTE_BODY)
        assert not looks_like_upstream_wrapper(OPENAI_RPM_BODY)
        assert not looks_like_upstream_wrapper(None)
        assert not looks_like_upstream_wrapper({"error": "string"})

    def test_declines_a_wrapped_rate_limit(self):
        # The 30s in metadata.raw belongs to the upstream model's limit, not
        # to the user's aggregator key. Acting on it benches a healthy key.
        assert classify(
            provider="openrouter", status_code=429,
            error_message="Provider returned error",
            error_body=self.WRAPPED_429, now_epoch=NOW,
        ) is None

    def test_declines_a_wrapped_auth_failure(self):
        # The dangerous one. Without the early exit, "API key not valid" from
        # the upstream provider marks the user's OpenRouter key permanently
        # dead — a worse outcome than the throttle it was reporting.
        assert classify(
            provider="openrouter", status_code=401,
            error_message="Provider returned error",
            error_body=self.WRAPPED_AUTH, now_epoch=NOW,
        ) is None

    def test_a_real_aggregator_key_throttle_is_still_served(self):
        # An aggregator's own 429 about the user's own key carries no
        # envelope, so it is classified normally.
        verdict = classify(
            provider="openrouter", status_code=429,
            error_message="Rate limit exceeded for your key",
            headers={"retry-after": "25"}, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reset_at == NOW + 25.0


class TestRobustness:
    """A classifier that raises inside the host is worse than one that declines."""

    @pytest.mark.parametrize("body", [
        None, {}, {"error": None}, {"error": "string"}, {"error": {"details": "nope"}},
        {"error": {"details": [None, 5, {"retryDelay": None}]}},
        [1, 2, 3], "not a dict", 42,
    ])
    def test_malformed_bodies_do_not_raise(self, body):
        classify(provider="gemini", status_code=429, error_body=body)

    def test_deeply_nested_body_terminates(self):
        node = {"error": {}}
        cursor = node["error"]
        for _ in range(200):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        classify(provider="gemini", status_code=429, error_body=node)

    def test_exception_with_hostile_attributes_falls_through(self):
        # A property that raises costs this one source, not the rest of the
        # cascade — the body below it still gets read.
        class Hostile(Exception):
            @property
            def retry_after(self):
                raise RuntimeError("nope")

        seconds, source = extract_retry_delay_seconds(
            body=PER_MINUTE_BODY, error=Hostile()
        )
        assert seconds == 21.0
        assert source.startswith("body.")

    def test_headers_that_raise_on_items(self):
        class Hostile:
            def items(self):
                raise RuntimeError("nope")

        assert extract_from_headers(Hostile(), NOW)[0] is None


# ── OpenRouter (verbatim) ─────────────────────────────────────────────────


# A real epoch, not this module's ``NOW`` of 1_000_000. The epoch below is
# read as milliseconds *because of its magnitude* — at a fake 1970 timestamp
# the millisecond form is indistinguishable from a plausible second form, so a
# fixture built on ``NOW`` would be testing a discrimination that cannot fail.
REAL_NOW = 1_786_829_619.0


def _openrouter_free_tier(bucket, reset_in_seconds):
    """OpenRouter's 429 for its shared free-model ceiling.

    The part that matters is where the rate-limit headers are: **in the
    body**, under ``error.metadata.headers``. That is documented, it is what
    litellm surfaces to the host, and it means the identical name
    ``X-RateLimit-Reset`` reaches this module through the body on OpenRouter
    and through the headers on everyone else. ``X-RateLimit-Reset`` is an
    epoch in **milliseconds**, not a delay.
    """
    return {
        "error": {
            "code": 429,
            "message": f"Rate limit exceeded: {bucket}",
            "metadata": {
                "headers": {
                    "X-RateLimit-Limit": "50",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int((REAL_NOW + reset_in_seconds) * 1000)),
                }
            },
        }
    }


class TestTheHeadersOpenRouterPutsInTheBody:
    """One name, two answers, depending on which side of a line it landed on.

    ``extract_from_headers`` has always matched a reset header by *shape* — a
    name mentioning both a limit and a reset. The body walker used a separate
    pattern that demanded a suffix after ``reset``, so ``X-RateLimit-Reset``
    was read as a reset in a header and as an ordinary key in a body. Nothing
    connected the two patterns, so they drifted, and the drift is the defect:
    the fix shares one pattern rather than adding the missing spelling.

    What it cost: OpenRouter states the exact moment its free-tier counter
    rolls over, and KAME threw that away and fell back to the hourly
    re-probe — a wasted refusal per key per hour, for the rest of the day, on
    the one provider whose free tier the pool exists to stretch.
    """

    def test_the_reset_moment_openrouter_actually_sends(self):
        body = _openrouter_free_tier("free-models-per-day", 9 * 3600)
        seconds, source = extract_from_body(body, REAL_NOW)
        assert seconds == pytest.approx(9 * 3600, abs=1)
        assert source == "body.X-RateLimit-Reset"

    def test_and_the_daily_cap_is_benched_to_that_moment(self):
        body = _openrouter_free_tier("free-models-per-day", 9 * 3600)
        verdict = classify(
            provider="openrouter", status_code=429,
            error_message="Rate limit exceeded: free-models-per-day",
            error_body=body, now_epoch=REAL_NOW,
        )
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.PER_DAY
        assert verdict.source == "body.X-RateLimit-Reset"
        # Not the hourly re-probe, which is what this used to be.
        assert verdict.reset_at == pytest.approx(REAL_NOW + 9 * 3600, abs=1)

    def test_the_shared_bucket_is_account_wide_in_every_window(self):
        # ``free-models-per-day`` was already read as account-wide. The
        # per-minute spelling names the same bucket and was read as per-model,
        # so the key was handed back for models it could not serve.
        for bucket, seconds in (("free-models-per-min", 41), ("free-models-per-day", 9 * 3600)):
            verdict = classify(
                provider="openrouter", status_code=429,
                error_message=f"Rate limit exceeded: {bucket}",
                error_body=_openrouter_free_tier(bucket, seconds), now_epoch=REAL_NOW,
            )
            assert verdict.quota_scope == QuotaScope.ACCOUNT, bucket
            assert verdict.reset_at == pytest.approx(REAL_NOW + seconds, abs=1), bucket

    def test_per_model_evidence_still_wins_over_the_bucket_name(self):
        # The shared marker is checked after the per-model one, on purpose:
        # a payload naming both is naming a per-model limit.
        body = _openrouter_free_tier("free-models-per-min", 41)
        body["error"]["metadata"]["quotaId"] = "GenerateRequestsPerMinutePerProjectPerModel"
        verdict = classify(
            provider="openrouter", status_code=429,
            error_message="Rate limit exceeded: free-models-per-min",
            error_body=body, now_epoch=REAL_NOW,
        )
        assert verdict.quota_scope == QuotaScope.PER_MODEL

    def test_a_body_key_the_host_itself_knows(self):
        # Hermes' own text scan looks for ``quotaResetDelay``. The body walker
        # did not, which is its own small proof the two patterns had drifted.
        seconds, source = extract_from_body({"quotaResetDelay": "45s"}, REAL_NOW)
        assert seconds == 45.0
        assert source == "body.quotaResetDelay"

    def test_one_name_reads_the_same_on_both_sides(self):
        # The property the fix is really asserting: a reset header means the
        # same thing whichever side of the header/body line it arrives on.
        moment = str(int((REAL_NOW + 300) * 1000))
        from_header, _ = extract_from_headers({"X-RateLimit-Reset": moment}, REAL_NOW)
        from_body, _ = extract_from_body({"X-RateLimit-Reset": moment}, REAL_NOW)
        assert from_header == from_body == pytest.approx(300, abs=1)

    @pytest.mark.parametrize("key", [
        "X-RateLimit-Limit", "X-RateLimit-Remaining", "limit", "remaining",
        "password_reset", "reset_password", "resetTokens",
    ])
    def test_a_key_that_is_not_a_reset_moment_is_not_read(self, key):
        # Widening a pattern is only safe if it stayed narrow where it counts:
        # none of these names a reset *time*, and a number read out of one
        # would be an invented deadline.
        assert extract_from_body({key: "50"}, REAL_NOW) == (None, "")


class TestAskingTheSourcesTheCascadeSkipped:
    """A long window whose strongest reading is short.

    A daily 429 very often carries both kinds of number at once: a
    per-minute reset *header*, which is about a different counter entirely,
    and the daily wait spelled out in the message. The cascade takes the
    header — right, in general, because a structured field means what it says
    — and the long-window rule then sets it aside as misleading. What
    replaced it was the flat hourly re-probe, a number this module invented,
    while the provider's own six-hour figure sat unread one rung down.

    Strength order is the right way to pick one reading. It is not a reason
    to prefer a guess over something the provider actually stated.
    """

    @staticmethod
    def _daily(**kwargs):
        return compute_reset_at(
            now_epoch=NOW, provider="openai",
            message=(
                "Rate limit reached for gpt-4o on requests per day (RPD): "
                "Limit 200, Used 200. Please try again in 6h12m."
            ),
            **kwargs,
        )

    def test_the_message_is_read_when_the_header_is_about_another_counter(self):
        decision = self._daily(headers={
            "x-ratelimit-reset-requests": "58s",
            "x-ratelimit-reset-tokens": "12s",
        })
        assert decision.window == QuotaWindow.PER_DAY
        assert decision.source == "text"
        assert decision.reset_at == pytest.approx(NOW + 6 * 3600 + 12 * 60)

    def test_a_short_hint_with_nothing_behind_it_is_still_the_flat_re_probe(self):
        # The rule only reaches past the first reading; it does not invent one.
        decision = compute_reset_at(
            now_epoch=NOW, provider="groq",
            message="Rate limit reached on requests per day (RPD): Limit 14400, Used 14400.",
            headers={"retry-after": "45"},
        )
        assert decision.reset_at == pytest.approx(NOW + 3600)
        assert "ignoring misleading" in decision.rationale

    def test_a_second_reading_that_is_also_short_is_not_promoted(self):
        # Below the window's own default it is the same misleading kind, from
        # a weaker source. Reaching further down for it would be worse than
        # the guess it replaced.
        decision = compute_reset_at(
            now_epoch=NOW, provider="openai",
            message=(
                "Rate limit reached on requests per day (RPD): Limit 200, Used 200. "
                "Please try again in 20m."
            ),
            headers={"retry-after": "45"},
        )
        assert decision.reset_at == pytest.approx(NOW + 3600)
        assert decision.source == "window"

    def test_the_longest_stated_wait_wins(self):
        # Same reason the reset headers take the longest of themselves: a key
        # released early re-hammers a limit that is still spent, and that is
        # the failure that cascades. Every candidate here is already bounded
        # and already testable later.
        decision = self._daily(
            headers={"x-ratelimit-reset-requests": "58s"},
            body={"retryAfter": "2h"},
        )
        assert decision.reset_at == pytest.approx(NOW + 6 * 3600 + 12 * 60)

    def test_a_short_window_is_untouched(self):
        # None of this applies below a day: there the strongest reading is
        # simply obeyed, short or not.
        decision = compute_reset_at(
            now_epoch=NOW, provider="openai",
            message="Rate limit reached on requests per min (RPM). Please try again in 8m.",
            headers={"retry-after": "20"},
        )
        assert decision.reset_at == pytest.approx(NOW + 20)
        assert decision.source == "header.retry-after"

    def test_google_still_reaches_its_anchor(self):
        # The calendar branch is checked before any of this, so the daily
        # anchor is not something a stray long reading can displace.
        decision = compute_reset_at(
            now_epoch=NOW, provider="gemini",
            message="Please try again in 23h.",
            body=GOOGLE_SENTENCE_RPD_BODY,
        )
        assert decision.source == SOURCE_ANCHOR
