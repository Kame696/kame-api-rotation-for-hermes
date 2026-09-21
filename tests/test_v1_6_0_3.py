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
            engine.mark(ID, "a", False, delay, "rate_limit", now=NOW + attempt*60)
            for attempt, delay in enumerate(GOOGLE_ASKED)
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
        for attempt in range(5):
            engine.mark(ID, "a", False, 40.0, "rate_limit", now=NOW + attempt*60)
        assert engine.mark(ID, "a", False, 22.3, "rate_limit", now=NOW + 300) == 22.3

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

    def test_the_only_ladder_left_is_the_one_that_retires_a_credential(self):
        # This test used to hold the ``server`` ladder as the sibling 1.6.0.3
        # deliberately did not touch, and 1.7.0.0 had already taken ``daily``
        # out of it. 1.7.0.4 takes ``server`` out too — see
        # ``test_v1_7_0_4.py`` for the measurement — which leaves exactly one
        # climbing rest in the whole module, and it is not really about time.
        #
        # ``auth`` / ``denied`` / ``revoked`` grow because growing is how the
        # pool counts to "this credential is not coming back" before retiring
        # it. Every other kind now rests a number somebody stated, or a flat
        # re-probe when nobody did.
        engine = Carousel()
        climb = [engine.mark(ID, "a", False, 0.0, "auth", now=NOW) for _ in range(3)]
        base = carousel.DAILY_BASE_S
        assert climb == [base, base * 2, base * 4]

    def test_a_spent_day_does_not_climb(self):
        # 1.7.0.0 removed the 20s/40s/80s/160s climb that made a genuinely
        # spent day cost eight refusals to earn an hour. That is still gone.
        #
        # 1.7.0.1 put one rung back — a strike counter — and 1.7.0.2 removes it
        # again, for a reason the counter could not answer. A benched key is
        # not asked, so "twice in a row" is not a measurement of anything; it
        # is a count of what did not happen. And the counter never even ran:
        # `dispatch_binding` replaced `delay` with 3600 one call upstream, so
        # its probe computed `max(3600, 20)` on all 114 of the owner's daily
        # refusals and returned the hour it existed to postpone.
        #
        # What 1.7.0.2 measures instead is answers, and the rest stays short
        # for as long as the pool keeps producing them. Repetition alone never
        # widens it — which is the property this test was written to hold, and
        # it holds more strictly now than it did before.
        engine = Carousel()
        rests = [engine.mark(ID, "a", False, 0.0, "daily", now=NOW) for _ in range(5)]

        assert rests == [carousel.RL_BACKOFF_CAP_S] * 5, (
            "five refusals in the same instant are one claim repeated, not "
            "five measurements — no climbing, and no hour"
        )

    def test_a_spent_day_does_buy_the_hour_once_the_pool_goes_quiet(self):
        # The other half, and the half that keeps a real exhaustion cheap:
        # once nobody has answered for POOL_SILENCE_BEFORE_THE_DAY_S the label
        # is no longer contradicted by the pool's own traffic, so it is
        # believed. Measured threshold — the owner's pool never went more than
        # 15 minutes without an answer while the model still had capacity.
        engine = Carousel()
        engine.mark(ID, "a", False, 0.0, "daily", now=NOW)
        quiet = NOW + carousel.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
        assert engine.mark(ID, "a", False, 0.0, "daily", now=quiet) == (
            engine.daily_cooldown_s
        )

    def test_a_retirement_ladder_still_climbs(self):
        # The ladder is kept where it is the mechanism rather than the guess:
        # ``auth`` grows because that count is how a credential gets retired.
        engine = Carousel()
        climb = [engine.mark(ID, "b", False, 0.0, "auth", now=NOW) for _ in range(3)]
        base = carousel.DAILY_BASE_S
        assert climb == [base, base * 2, base * 4]


# --- B. what KAME may invent when nothing sized it ---------------------------


class TestNothingIsInvented:
    """Every rest is a number somebody measured. There is no ladder left."""

    def test_a_provider_that_never_named_a_number_gets_a_flat_re_probe(self):
        # No climb. A rate limit is a rolling window (seconds, and the
        # provider says so) or a daily cap (hours, different counter). Nothing
        # lives between them, so a ladder from 1s to 300s spent its whole
        # range interpolating between regimes that do not meet.
        engine = Carousel()
        rests = [
            engine.mark(ID, "a", False, 0.0, "rate_limit", now=NOW) for _ in range(12)
        ]
        assert rests == [carousel.UNSIZED_THROTTLE_REST_S] * 12

    def test_the_flat_re_probe_is_the_same_number_quota_uses(self):
        # One refusal must not produce two different waits depending on which
        # half of the plugin is asked.
        from importlib import import_module

        quota = import_module(f"{PACKAGE}.core.quota")
        assert (
            carousel.UNSIZED_THROTTLE_REST_S
            == quota.DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS
        )

    def test_one_stated_number_answers_every_later_terse_refusal(self):
        # 232 of the owner's 400 throttles arrived as the terse "Resource has
        # been exhausted (e.g. check quota)." with no number at all, and 168
        # arrived spelled out, never above 59.8s. The terse form is the same
        # condition worded shorter, and the 168 already answered it.
        engine = Carousel()
        engine.mark(ID, "a", False, 53.8, "rate_limit", now=NOW, stated=True)
        rests = [
            engine.mark(ID, "a", False, 0.0, "rate_limit", now=NOW) for _ in range(10)
        ]
        assert rests == [53.8] * 10

    def test_what_the_provider_said_does_not_leak_to_an_independent_key(self):
        # 1.7.0.7: independent accounts need not share a counter or reset time.
        # This old cross-account assertion was the defect, not a guarantee.
        engine = Carousel()
        engine.mark(ID, "a", False, 30.0, "rate_limit", now=NOW, stated=True)
        rests = [
            engine.mark(ID, "b", False, 0.0, "rate_limit", now=NOW) for _ in range(10)
        ]
        assert rests == [carousel.UNSIZED_THROTTLE_REST_S] * 10

    def test_another_model_does_not_inherit_it(self):
        # Different identity, different window. Borrowing the number would be
        # the same overreach in the other direction.
        engine = Carousel()
        engine.mark(ID, "a", False, 30.0, "rate_limit", now=NOW)
        rests = [
            engine.mark("gemini:gemini-3.5-flash", "a", False, 0.0, "rate_limit", now=NOW)
            for _ in range(4)
        ]
        assert rests == [carousel.UNSIZED_THROTTLE_REST_S] * 4

    def test_a_daily_length_number_never_teaches_the_short_window(self):
        # On Gemini a *daily* cap classifies as ``rate_limit`` too and arrives
        # sized at an hour. Letting that hour in would mean one exhausted day
        # teaching every terse throttle afterwards to rest for an hour, which
        # is the original defect wearing a different hat.
        engine = Carousel()
        engine.mark(ID, "a", False, 3600.0, "rate_limit", now=NOW)
        rests = [
            engine.mark(ID, "b", False, 0.0, "rate_limit", now=NOW) for _ in range(4)
        ]
        assert rests == [carousel.UNSIZED_THROTTLE_REST_S] * 4

    def test_a_number_the_provider_stated_is_still_obeyed_beyond_the_cap(self):
        # The learning filter above is about what may *teach*. It must not
        # become a clamp on what the provider asked for right now.
        engine = Carousel()
        assert engine.mark(ID, "a", False, 3600.0, "rate_limit", now=NOW) == 3600.0


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


# --- D. the dispatch path reaches the journal -------------------------------
#
# Group C above tests the recorder; this tests the *call site*, which is the
# half that was actually missing. `dispatch_binding` benches a key on the
# carousel inside a turn and moves on without the host's pool being told, and
# until 1.6.0.3 nothing wrote that down: 74 rotations, 0 rows, on the owner's
# 1.6.0.2 build.


dispatch_module = importlib.import_module(f"{PACKAGE}.dispatch_binding")
runtime_module = importlib.import_module(f"{PACKAGE}.runtime")


class _Boom(Exception):
    def __init__(self, message, status_code=429):
        super().__init__(message)
        self.status_code = status_code


class TestTheDispatchPathIsRecorded:
    def _fresh_binding(self):
        return dispatch_module.DispatchBinding(engine=Carousel())

    def test_a_rotation_hands_the_journal_what_it_needs(self):
        seen = []
        runtime_module.set_rotation_recorder(lambda **f: seen.append(f))
        try:
            self._fresh_binding()._on_failure(
                ID, "AIzaSyEXAMPLE", _Boom("429: Please retry in 41.3s"),
                "label", 1, False, credential_id="entry-7",
            )
        finally:
            runtime_module.set_rotation_recorder(None)
        assert len(seen) == 1, seen
        row = seen[0]
        assert row["credential_id"] == "entry-7"
        assert row["provider"] == "gemini"
        assert row["model"] == ID
        assert row["reason"] == "rate_limit"
        # The deadline is the one the *carousel* applied, not the one the
        # classifier proposed: a prediction measured against a bench that
        # never happened teaches nothing.
        assert row["held_to"] is not None

    def test_the_judgement_carries_the_window_under_the_name_the_journal_reads(self):
        # `classify.Verdict` spells it `quota_window`; `runtime.Judgement`
        # spells it `window`. Handing the Verdict over directly would have
        # written "unknown" into every row while looking like it recorded
        # something.
        seen = []
        runtime_module.set_rotation_recorder(lambda **f: seen.append(f))
        try:
            self._fresh_binding()._on_failure(
                ID, "AIzaSyEXAMPLE",
                _Boom("429 RESOURCE_EXHAUSTED: Please retry in 41.3s"),
                "label", 1, False, credential_id="entry-7",
            )
        finally:
            runtime_module.set_rotation_recorder(None)
        judgement = seen[0]["judgement"]
        if judgement is not None:
            assert hasattr(judgement, "window")
            assert not hasattr(judgement, "quota_window")

    def test_a_key_the_pool_cannot_name_is_never_recorded(self):
        seen = []
        runtime_module.set_rotation_recorder(lambda **f: seen.append(f))
        try:
            self._fresh_binding()._on_failure(
                ID, "AIzaSyEXAMPLE", _Boom("429: Please retry in 41.3s"),
                "label", 1, False,
            )
        finally:
            runtime_module.set_rotation_recorder(None)
        assert seen == []

    def test_a_recorder_that_raises_does_not_end_the_turn(self):
        def explode(**_fields):
            raise RuntimeError("the journal is on fire")

        runtime_module.set_rotation_recorder(explode)
        try:
            verdict, _kind, _status = self._fresh_binding()._on_failure(
                ID, "AIzaSyEXAMPLE", _Boom("429: Please retry in 41.3s"),
                "label", 1, False, credential_id="entry-7",
            )
        finally:
            runtime_module.set_rotation_recorder(None)
        assert verdict == "rotate"
