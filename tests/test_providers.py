"""The provider shapes read in 1.0.3, and what KAME is allowed to do with them.

Every payload here traces to a row in ``research/provider-errors.md``, which
records for each one whether it is **documented** (the provider's own error
table) or **observed** (quoted from a real response in a bug report). That
distinction is the whole reason the research file exists, and it carries
through to here: a documented fixture proves the *strings* are right, not that
the envelope around them is. Where a test depends on the envelope, it says so.

``test_core.py`` already says the thing worth repeating: a fixture written from
memory tests the memory. Nothing below was written from memory.

The tests fall into two halves, and the second is the important one:

* what KAME now recognises that it did not before, and
* what it still declines — including several payloads it *could* have an
  opinion about and must not.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation")
)

from core import QuotaWindow, classify  # noqa: E402
from core.quota import extract_reset_moment_from_text  # noqa: E402


NOW = 1_800_000_000.0


def iso(seconds_from_now: float) -> str:
    moment = datetime.fromtimestamp(NOW + seconds_from_now, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the contradiction: a type that says throttle, prose that says busy ────


class TestWhenTheTypeAndTheSentenceDisagree:
    """Kimi's coding endpoint, observed — ``pawwork#740``.

    Reported in the wild as a death loop, and the mechanism is worth stating
    once: the sentence says congestion, congestion means "do not rotate", and
    the key that was actually rate-limited gets retried until the turn dies
    with working keys sitting untouched beside it.
    """

    KIMI = {
        "error": {
            "type": "rate_limit_error",
            "message": "The engine is currently overloaded, please try again later.",
        }
    }

    def test_the_structured_type_wins(self):
        verdict = classify(
            provider="kimi-coding",
            status_code=429,
            error_message="The engine is currently overloaded, please try again later.",
            error_body=self.KIMI,
            now_epoch=NOW,
        )
        assert verdict is not None, "declining here leaves the host's wrong reading"
        assert verdict.reason == "rate_limit"
        assert verdict.should_rotate_credential

    def test_it_rotates_without_inventing_a_wait(self):
        # The payload said which counter, never how long. Benching on a
        # number nothing stated would be the same fabrication this module
        # refuses everywhere else.
        verdict = classify(
            provider="kimi-coding",
            status_code=429,
            error_message="The engine is currently overloaded, please try again later.",
            error_body=self.KIMI,
            now_epoch=NOW,
        )
        assert verdict.reset_at is None
        assert verdict.source == "type"

    def test_a_stated_delay_is_still_preferred_to_the_bare_rotation(self):
        body = {
            "error": {
                "type": "rate_limit_error",
                "message": "The engine is currently overloaded, try again in 45s",
            }
        }
        verdict = classify(
            provider="kimi-coding",
            status_code=429,
            error_message="The engine is currently overloaded, try again in 45s",
            error_body=body,
            now_epoch=NOW,
        )
        assert verdict.reset_at == pytest.approx(NOW + 45, abs=1)

    def test_anthropic_overload_is_untouched(self):
        # The case ``_BUSY_PATTERNS`` was written for. Its type agrees with
        # its sentence, so there is no contradiction and nothing to override
        # — rotating here would drain the pool against a busy endpoint.
        body = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        assert classify(
            provider="anthropic",
            status_code=529,
            error_message="Overloaded",
            error_body=body,
            now_epoch=NOW,
        ) is None

    def test_overloaded_prose_with_no_type_at_all_is_still_congestion(self):
        # No structured field, no contradiction, no change from 1.0.2. The
        # override needs evidence; absence of evidence is not the evidence.
        assert classify(
            provider="whoever",
            status_code=429,
            error_message="The server is overloaded, please try again later",
            error_body={"message": "The server is overloaded, please try again later"},
            now_epoch=NOW,
        ) is None

    def test_a_5xx_is_never_overridden(self):
        # The transport agreeing with the prose. No field inside a body
        # outranks the status the connection came back with.
        body = {"error": {"type": "rate_limit_error", "message": "overloaded"}}
        assert classify(
            provider="whoever",
            status_code=503,
            error_message="overloaded",
            error_body=body,
            now_epoch=NOW,
        ) is None

    def test_an_integer_code_is_not_a_type(self):
        # OpenRouter's ``error.code`` is the integer 429 and says nothing the
        # status has not. Only strings are read, so this stays congestion.
        body = {"error": {"code": 429, "message": "overloaded"}}
        assert classify(
            provider="openrouter",
            status_code=429,
            error_message="overloaded",
            error_body=body,
            now_epoch=NOW,
        ) is None


# ── a reset stated as a moment rather than a duration ─────────────────────


class TestAResetTimeInsteadOfADelay:
    """Z.AI 1308 / 1310 / 1316-1321, documented — ``docs.z.ai``.

    The window was already detected; the moment was thrown away. A weekly
    limit falling back to the hourly re-probe spends one refusal per key per
    hour for up to a week, against a sentence that named the rollover.
    """

    def test_it_reads_the_moment(self):
        seconds, source = extract_reset_moment_from_text(
            f"Weekly Limit Exhausted. Your limit will reset at {iso(20 * 3600)}", NOW
        )
        assert seconds == pytest.approx(20 * 3600, abs=2)
        assert source == "text.reset_at"

    def test_a_weekly_limit_benches_until_the_stated_rollover(self):
        message = (
            "Weekly/Monthly Limit Exhausted. Your limit will reset at "
            f"{iso(12 * 3600)}"
        )
        verdict = classify(
            provider="zai",
            status_code=429,
            error_message=message,
            error_body={"error": {"code": "1310", "message": message}},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.quota_window == QuotaWindow.PER_WEEK
        assert verdict.reset_at == pytest.approx(NOW + 12 * 3600, abs=2)

    def test_a_moment_already_past_is_ignored(self):
        # A stale message is not a reason to bench anything, and a negative
        # delay must never become one.
        seconds, _ = extract_reset_moment_from_text(
            f"Your limit will reset at {iso(-3600)}", NOW
        )
        assert seconds is None

    def test_a_moment_beyond_the_horizon_is_ignored(self):
        seconds, _ = extract_reset_moment_from_text(
            f"Your limit will reset at {iso(60 * 86400)}", NOW
        )
        assert seconds is None

    def test_a_duration_is_left_to_the_duration_reader(self):
        # "resets in 5m" is a delay. Only "at"/"on" reach this reader, so the
        # two never fight over one sentence.
        seconds, _ = extract_reset_moment_from_text("resets in 5m", NOW)
        assert seconds is None

    def test_a_bare_year_is_not_a_timestamp(self):
        # The alternation is narrow on purpose: this value is a bench
        # deadline, and a loose capture parks a healthy key on a misread.
        seconds, _ = extract_reset_moment_from_text("try again at 2026", NOW)
        assert seconds is None


# ── wording added in 1.0.3, each from one documented row ──────────────────


class TestNewlyRecognisedWording:
    def test_alibaba_free_tier_exhausted_on_a_403(self):
        # `AllocationQuota.FreeTierOnly`, documented. A 403 is the status
        # step 4 hands back to the host, so before 1.0.3 the single refusal
        # meaning "this key's free allowance is gone" was the one KAME had
        # nothing to say about.
        message = "The free tier of the model has been exhausted"
        verdict = classify(
            provider="alibaba",
            status_code=403,
            error_message=message,
            error_body={"code": "AllocationQuota.FreeTierOnly", "message": message},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_huggingface_monthly_credits(self):
        message = (
            "You have exceeded your monthly included credits for Inference Providers"
        )
        verdict = classify(
            provider="huggingface",
            status_code=402,
            error_message=message,
            error_body={"error": message},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_alibaba_arrearage(self):
        message = "Access denied, please make sure your account is in good standing"
        verdict = classify(
            provider="alibaba",
            status_code=400,
            error_message=message,
            error_body={"code": "Arrearage", "message": message},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_zai_expired_package_is_billing_not_a_throttle(self):
        message = "Your GLM Coding Plan package has expired and is temporarily unavailable."
        verdict = classify(
            provider="zai",
            status_code=429,
            error_message=message,
            error_body={"error": {"code": "1309", "message": message}},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_zai_model_outside_the_plan_stays_per_model(self):
        # The plan is fine; this one model is outside it. The key has to stay
        # usable for every other model in the pool.
        message = "Your current subscription plan does not yet include access to glm-4.7"
        verdict = classify(
            provider="zai",
            status_code=429,
            error_message=message,
            error_body={"error": {"code": "1311", "message": message}},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "auth"
        assert verdict.quota_scope == "per_model"

    @pytest.mark.parametrize(
        "code, message",
        [
            ("Throttling", "Requests throttling triggered"),
            ("Throttling.RateQuota", "You have exceeded your request limit"),
            ("Throttling.BurstRate", "Request rate increased too quickly"),
            (
                "Throttling.AllocationQuota",
                "Allocated quota exceeded, please increase your quota limit",
            ),
        ],
    )
    def test_alibaba_throttling_family_is_a_throttle(self, code, message):
        # Documented. The whole family is spelled with "throttling" and never
        # with "rate limit", which is why the stem is matched in the code as
        # well as the sentence. Sized or not, none of these may read as
        # billing — that verdict costs a day at account scope.
        verdict = classify(
            provider="alibaba",
            status_code=429,
            error_message=message,
            error_body={"code": code, "message": message},
            headers={"Retry-After": "30"},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.reset_at == pytest.approx(NOW + 30, abs=1)


# ── what KAME still refuses to answer ─────────────────────────────────────


class TestStillDeclined:
    """The half that keeps the other half safe.

    Recognising more is only an improvement while the plugin still gets out
    of the way everywhere it has nothing to add. Each of these is a payload
    somebody could reasonably have written a rule for.
    """

    def test_nvidia_bare_429_stays_unsized(self):
        # Observed, NVIDIA developer forums: `{"status": 429, "title": "Too
        # Many Requests"}`, sometimes with no body at all. Nothing in it can
        # size a wait. The reason is right and the timing is the host's, and
        # this test exists so nobody closes the gap with an invented number.
        verdict = classify(
            provider="nvidia",
            status_code=429,
            error_message="Too Many Requests",
            error_body={"status": 429, "title": "Too Many Requests"},
            now_epoch=NOW,
        )
        assert verdict is None

    def test_nvidia_429_with_no_body_at_all(self):
        assert classify(
            provider="nvidia", status_code=429, error_message="", error_body=None,
            now_epoch=NOW,
        ) is None

    def test_mistral_bare_rate_limit_stays_unsized(self):
        # Observed, pydantic-ai#1885. Top-level `message`, no envelope, no
        # delay anywhere.
        assert classify(
            provider="mistral",
            status_code=429,
            error_message="Requests rate limit exceeded",
            error_body={"message": "Requests rate limit exceeded"},
            now_epoch=NOW,
        ) is None

    def test_minimax_numeric_code_is_not_read(self):
        # Documented codes, deliberately not acted on: MiniMax reports them
        # in `base_resp` and has been observed sending them under HTTP 200,
        # with a live report of a *false* insufficient-balance on a working
        # plan. A bare integer in an unfamiliar envelope, from a provider
        # known to send it wrongly, is the guess this project does not make.
        assert classify(
            provider="minimax",
            status_code=200,
            error_message="",
            error_body={"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}},
            now_epoch=NOW,
        ) is None

    def test_an_upstream_wrapper_still_shields_the_aggregator_key(self):
        # 1.0.3 added wording that matches inside the nested error. Step 0
        # runs before all of it, and has to keep running before all of it:
        # the sentence is about somebody else's credential.
        body = {
            "error": {
                "message": "Provider returned error",
                "code": 429,
                "metadata": {
                    "provider_name": "somebody",
                    "raw": "Weekly Limit Exhausted. Your limit will reset at "
                           + iso(86400),
                },
            }
        }
        assert classify(
            provider="openrouter",
            status_code=429,
            error_message="Provider returned error",
            error_body=body,
            now_epoch=NOW,
        ) is None

    def test_a_content_refusal_on_403_is_not_a_credential_problem(self):
        assert classify(
            provider="whoever",
            status_code=403,
            error_message="The prompt was blocked by the safety filter",
            error_body={"error": {"message": "blocked by the safety filter"}},
            now_epoch=NOW,
        ) is None
