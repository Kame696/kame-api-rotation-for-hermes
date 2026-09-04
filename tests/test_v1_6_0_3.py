"""1.6.0.3 — the provider's own number, and the field the host threw away.

Every case here comes from one of two pieces of evidence, and neither is a
hypothesis:

* the owner's Hermes log for 1.6.0.2, 21:03:54 to 21:49 on 2026-09-03, in
  which Gemini returned 340 throttles and KAME held keys for longer than
  Google had ever asked on ten of them;
* Google's own forum thread on ``retryDelay``, which shows a *daily* quota
  exhaustion — ``quotaValue: "250"`` — arriving with ``retryDelay: "1s"``.

Group A is the carousel ladder. Group B is the evidence ceiling. Group C is
the response body Hermes parses, keeps four fields of, and drops the rest of.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1603_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
classify = importlib.import_module(f"{PACKAGE}.core.classify")

Carousel = carousel.Carousel

ID = "gemini:gemini-3.8-flash"
NOW = 1000.0

# The ten stated delays the owner's log actually contains, smallest and
# largest included. Nothing in this file may produce a rest above 59.8.
GOOGLE_ASKED = [53.8, 41.1, 37.2, 31.5, 44.0, 22.3, 1.5, 59.8, 12.0, 48.2]


def _fresh():
    return Carousel(), {"consecutive_rl": 0, "consecutive_server": 0}


# --- A. the ladder stops arguing with the provider ---------------------------


class TestTheLadderObeysAStatedNumber:
    def test_every_repeat_of_a_sized_throttle_rests_for_what_was_stated(self):
        engine = Carousel()
        rests = [
            engine.mark(ID, "a", False, delay, "rate_limit", now=NOW)
            for delay in GOOGLE_ASKED
        ]
        assert rests == GOOGLE_ASKED

    def test_no_rest_exceeds_the_longest_number_google_stated(self):
        # The one-line statement of the whole defect. Under 1.6.0.2 this
        # sequence produced ten 5m benches; the pool was then far enough out
        # that the agent sat in the wait loop for 468s across 33 waits.
        engine = Carousel()
        rests = [
            engine.mark(ID, "a", False, delay, "rate_limit", now=NOW)
            for delay in GOOGLE_ASKED
        ]
        assert max(rests) <= max(GOOGLE_ASKED)

    def test_the_invented_floor_never_outbids_the_provider(self):
        # ``max(delay, ladder)`` would have been the half-fix. By the sixth
        # strike the ladder stands at 32s, and 22.3s is a real number from the
        # log — the provider would have been overruled by an invention.
        engine = Carousel()
        for _ in range(5):
            engine.mark(ID, "a", False, 40.0, "rate_limit", now=NOW)
        assert engine.mark(ID, "a", False, 22.3, "rate_limit", now=NOW) == 22.3

    def test_a_stated_number_above_the_ceiling_is_obeyed_not_clamped(self):
        # The ceiling bounds what KAME invents. Clamping the provider's own
        # number down to it re-probes early, into a window the provider has
        # just said is still spent.
        engine = Carousel()
        assert engine.mark(ID, "a", False, 600.0, "rate_limit", now=NOW) == 600.0

    def test_the_sub_second_floor_still_holds(self):
        # A rest below a second is a spin, not a cooldown.
        engine = Carousel()
        assert engine.mark(ID, "a", False, 0.2, "rate_limit", now=NOW) == carousel.RL_BASE_S

    @pytest.mark.parametrize("kind", ["server", "daily"])
    def test_the_sibling_ladders_are_untouched(self, kind):
        # They already had the right shape — ``max(delay, base * 2 ** n)`` —
        # and this release did not go near them.
        engine = Carousel()
        climb = [engine.mark(ID, "a", False, 0.0, kind, now=NOW) for _ in range(3)]
        base = carousel.SERVER_BASE_S if kind == "server" else carousel.DAILY_BASE_S
        assert climb == [base, base * 2, base * 4]


# --- B. what KAME may invent when nothing sized it ---------------------------


class TestTheInventedLadder:
    def test_an_unsized_throttle_still_climbs(self):
        # The case the ladder exists for, and the one 1.4.0 found benching
        # keys for zero seconds.
        engine = Carousel()
        climb = [
            engine.mark(ID, "a", False, 0.0, "rate_limit", now=NOW) for _ in range(6)
        ]
        assert climb == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]

    def test_with_no_sized_sample_the_climb_stops_at_the_constant(self):
        engine = Carousel()
        climb = [
            engine.mark(ID, "a", False, 0.0, "rate_limit", now=NOW) for _ in range(12)
        ]
        assert climb[-1] == carousel.RL_BACKOFF_CAP_S

    def test_a_stated_number_becomes_the_ceiling_for_the_invented_one(self):
        # 232 of the owner's 400 throttles arrived as the terse "Resource has
        # been exhausted (e.g. check quota)." with no number at all, and 168
        # arrived spelled out, never above 59.8s. The terse form is the same
        # condition worded shorter. A plugin sitting on 168 samples that say
        # under a minute must not invent five.
        engine = Carousel()
        engine.mark(ID, "a", False, 53.8, "rate_limit", now=NOW)
        climb = [
            engine.mark(ID, "a", False, 0.0, "rate_limit", now=NOW) for _ in range(10)
        ]
        assert max(climb) == 53.8

    def test_the_ceiling_is_per_identity_not_per_key(self):
        # The lesson is about the provider's window, so a number collected on
        # one credential bounds the guess made about the next.
        engine = Carousel()
        engine.mark(ID, "a", False, 30.0, "rate_limit", now=NOW)
        climb = [
            engine.mark(ID, "b", False, 0.0, "rate_limit", now=NOW) for _ in range(10)
        ]
        assert max(climb) == 30.0

    def test_another_model_does_not_inherit_the_ceiling(self):
        # Different identity, different window. Borrowing the number would be
        # the same overreach in the other direction.
        engine = Carousel()
        engine.mark(ID, "a", False, 30.0, "rate_limit", now=NOW)
        climb = [
            engine.mark("gemini:gemini-3.5-flash", "a", False, 0.0, "rate_limit", now=NOW)
            for _ in range(10)
        ]
        assert max(climb) == carousel.RL_BACKOFF_CAP_S

    def test_the_ceiling_rises_when_the_provider_asks_for_more(self):
        engine = Carousel()
        engine.mark(ID, "a", False, 30.0, "rate_limit", now=NOW)
        engine.mark(ID, "a", False, 900.0, "rate_limit", now=NOW)
        climb = [
            engine.mark(ID, "b", False, 0.0, "rate_limit", now=NOW) for _ in range(12)
        ]
        # Bounded by the constant, which is the tighter of the two ceilings
        # once the provider's own number has gone above it.
        assert max(climb) == carousel.RL_BACKOFF_CAP_S


# --- C. the field the host parsed and did not keep ---------------------------


def _google_payload(quota_id: str, retry_delay: str, quota_value: str) -> str:
    """The shape Google actually returns, from its own forum thread."""
    return json.dumps(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": (
                    "You exceeded your current quota, please check your plan "
                    "and billing details."
                ),
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
                                "quotaValue": quota_value,
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
    )


PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"

# What Hermes' ``gemini_http_error`` welds onto the message, and what it keeps
# on ``details``: the ``google.rpc.ErrorInfo`` slice and nothing else.
HOST_MESSAGE = (
    "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current quota.\n"
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 20\n"
    "Please retry in 41.3s.\n\n"
    "Your Google API key is on the free tier (a few hundred requests/day for "
    "Gemini Flash models)."
)
HOST_DETAILS = {
    "status": "RESOURCE_EXHAUSTED",
    "reason": "",
    "metadata": {},
    "message": "You exceeded your current quota, please check your plan and billing details.",
}


class _Response:
    def __init__(self, text):
        self.text = text
        self.status_code = 429


class _Unread:
    """An httpx response whose body was never read raises from the property."""

    status_code = 429

    @property
    def text(self):
        raise RuntimeError("ResponseNotRead")

    @property
    def content(self):
        raise RuntimeError("ResponseNotRead")


class _GeminiAPIError(Exception):
    def __init__(self, response):
        super().__init__(HOST_MESSAGE)
        self.code = "gemini_rate_limited"
        self.status_code = 429
        self.response = response
        self.details = dict(HOST_DETAILS)


def _verdict(response):
    return classify.classify(
        provider="gemini",
        model="gemini-3.8-flash",
        status_code=429,
        error_message=HOST_MESSAGE,
        error_body=None,
        error=_GeminiAPIError(response),
        now_epoch=NOW,
    )


class TestTheQuotaIdIsReachableAgain:
    def test_a_daily_quota_is_recognised_as_daily(self):
        # Both windows report the identical ``quotaMetric``; ``quotaId`` is
        # the only field that separates them, and Hermes' adapter keeps four
        # fields of the payload, none of them this one.
        verdict = _verdict(_Response(_google_payload(PER_DAY, "1s", "250")))
        assert verdict.quota_window == "per_day"

    def test_the_misleading_one_second_hint_is_refused(self):
        # Google's own thread: 250 daily requests spent, ``retryDelay: 1s``.
        # Agent Zero learned to distrust this in production; until now Hermes
        # had no field to distrust it with, and re-probed the dead key every
        # twenty seconds for the rest of the day.
        verdict = _verdict(_Response(_google_payload(PER_DAY, "1s", "250")))
        assert verdict.reset_at - NOW >= 3600.0
        assert "ignoring misleading" in verdict.rationale

    def test_an_honest_long_daily_delay_is_still_believed(self):
        # Distrust is for a *short* number on a long window. A provider being
        # specific about two hours is being specific.
        verdict = _verdict(_Response(_google_payload(PER_DAY, "7200s", "250")))
        assert verdict.reset_at - NOW == pytest.approx(7200.0)

    def test_a_per_minute_quota_still_rests_for_what_was_stated(self):
        # The owner's actual traffic. This must not have moved.
        verdict = _verdict(_Response(_google_payload(PER_MINUTE, "41.3s", "20")))
        assert verdict.quota_window == "per_minute"
        assert verdict.reset_at - NOW == pytest.approx(41.3)

    def test_a_body_that_cannot_be_read_changes_nothing(self):
        # ``.text`` on an unread streaming response raises immediately and
        # does no I/O. The verdict falls back to what 1.6.0.2 produced.
        verdict = _verdict(_Unread())
        assert verdict is not None
        assert verdict.reason == "rate_limit"

    def test_a_missing_response_changes_nothing(self):
        verdict = _verdict(None)
        assert verdict is not None
        assert verdict.reason == "rate_limit"

    def test_reading_the_response_never_raises_on_a_hostile_object(self):
        class Hostile:
            def __getattr__(self, name):
                raise RuntimeError("no")

        assert classify._response_text(Hostile()) == ""
        assert classify._response_body(Hostile()) is None

    def test_a_response_that_is_not_json_is_text_only(self):
        # Still searchable, still not walkable. Neither reader may raise.
        assert classify._response_text(_GeminiAPIError(_Response("not json"))) == "not json"
        assert classify._response_body(_GeminiAPIError(_Response("not json"))) is None
