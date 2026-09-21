"""How far a refusal reaches — the second dimension the pool has no field for.

v0.0.4 taught the plugin that a bench belongs to a model. That is right where
the provider meters per model, and wrong where it meters per account: there,
handing the key back for a different model walks into the same wall, costs a
call, and writes a refusal into the journal that means nothing.

The rule these tests pin down is asymmetric on purpose. Only an explicit
account-wide statement changes anything; silence keeps the behaviour every
version before v0.0.8 had. So a scope this code fails to detect costs nothing
new, and a scope it detects wrongly can only ever cost what was already being
paid — which is the only shape of guess worth making about a credential.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation")
)

from core.classify import classify  # noqa: E402
from core.ledger import (  # noqa: E402
    SCOPE_ACCOUNT,
    SCOPE_PER_MODEL,
    SCOPE_UNKNOWN,
    Bench,
    Ledger,
)
from core.quota import QuotaScope, compute_reset_at, detect_quota_scope  # noqa: E402
from core.reconcile import (  # noqa: E402
    HOLD,
    RELEASE,
    STATUS_EXHAUSTED,
    EntryView,
    plan,
)

NOW = 1_000_000.0
HOUR = 3600.0
MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"

# Verbatim shape of a Google free-tier 429: the metric names the project *and*
# the model, and the violation names the model again as a dimension.
GOOGLE_PER_MODEL = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded for quota metric 'Generate requests per model per day'",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_requests_per_model",
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                        "quotaDimensions": {"model": "gemini-3.6-flash", "location": "global"},
                    }
                ],
            }
        ],
    }
}

# OpenRouter meters its free tier across the whole account: the same key is
# spent on every free model at once.
OPENROUTER_ACCOUNT = {
    "error": {
        "code": 429,
        "message": "Rate limit exceeded: free-models-per-day. Add credits to unlock more.",
    }
}


class TestReadingWhatTheProviderSaid:
    def test_a_metric_that_names_the_model_is_per_model(self):
        assert detect_quota_scope(body=GOOGLE_PER_MODEL) == QuotaScope.PER_MODEL

    def test_a_dimension_that_names_the_model_is_enough_on_its_own(self):
        body = {"violations": [{"quotaDimensions": {"model": "some-model"}}]}
        assert detect_quota_scope(body=body) == QuotaScope.PER_MODEL

    def test_an_empty_model_dimension_proves_nothing(self):
        body = {"violations": [{"quotaDimensions": {"model": "   "}}]}
        assert detect_quota_scope(body=body) == QuotaScope.UNKNOWN

    def test_a_daily_free_account_ceiling_is_account_wide(self):
        assert detect_quota_scope(body=OPENROUTER_ACCOUNT) == QuotaScope.ACCOUNT

    @pytest.mark.parametrize(
        "message",
        [
            "rate limit exceeded per user",
            "Requests per organization exceeded",
            "limit reached: per-api-key",
            "per_workspace quota exhausted",
        ],
    )
    def test_a_limit_metered_on_the_account_is_account_wide(self, message):
        assert detect_quota_scope(message=message) == QuotaScope.ACCOUNT

    def test_the_narrower_half_of_per_project_per_model_wins(self):
        # Google's own string carries both. Reading it as project-wide would
        # hold back a model with its whole allowance intact.
        assert (
            detect_quota_scope(message="GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
            == QuotaScope.PER_MODEL
        )

    def test_out_of_credits_is_account_wide(self):
        assert (
            detect_quota_scope(message="You exceeded your current quota: insufficient_quota")
            == QuotaScope.ACCOUNT
        )

    @pytest.mark.parametrize(
        "message",
        [
            "",
            "429 Too Many Requests",
            "Rate limit reached. Please try again in 20s.",
        ],
    )
    def test_silence_is_not_a_statement(self, message):
        # The reading that changes nothing. Everything the plugin did before
        # this version, it keeps doing.
        assert detect_quota_scope(message=message) == QuotaScope.UNKNOWN

    def test_an_unwalkable_body_does_not_raise(self):
        class Hostile:
            def __iter__(self):
                raise RuntimeError("boom")

        assert detect_quota_scope(body={"nested": Hostile()}) in (
            QuotaScope.UNKNOWN,
            QuotaScope.ACCOUNT,
            QuotaScope.PER_MODEL,
        )


class TestCarryingItForward:
    def test_the_decision_carries_the_scope(self):
        decision = compute_reset_at(now_epoch=NOW, provider="gemini", body=GOOGLE_PER_MODEL)
        assert decision.scope == QuotaScope.PER_MODEL

    def test_an_account_window_is_account_scope_whatever_the_text_says(self):
        decision = compute_reset_at(
            now_epoch=NOW,
            message="You exceeded your current quota, please check your plan and billing details",
            body={"error": {"code": "insufficient_quota"}},
        )
        assert decision.scope == QuotaScope.ACCOUNT

    def test_the_rationale_says_so_when_the_whole_key_is_spent(self):
        decision = compute_reset_at(now_epoch=NOW, body=OPENROUTER_ACCOUNT)
        assert "every model on this key" in decision.rationale

    def test_a_verdict_carries_it_too(self):
        verdict = classify(
            provider="openrouter", status_code=429,
            error_body=OPENROUTER_ACCOUNT, now_epoch=NOW,
        )
        assert verdict.quota_scope == QuotaScope.ACCOUNT

    def test_a_denial_is_about_this_pairing_only(self):
        # "This model is not available for your tier" says nothing about the
        # models that *are*.
        verdict = classify(
            provider="openai", status_code=403,
            error_message="model not available for your account tier",
            now_epoch=NOW,
        )
        assert verdict.quota_scope == QuotaScope.PER_MODEL

    def test_an_ordinary_throttle_says_nothing_about_scope(self):
        verdict = classify(
            provider="groq", status_code=429,
            error_message="Rate limit reached. Please try again in 20s.",
            now_epoch=NOW,
        )
        assert verdict.quota_scope == QuotaScope.UNKNOWN


class TestRememberingIt:
    def _ledger(self, scope: str, *, model: str = MAIN, offset: float = HOUR) -> Ledger:
        ledger = Ledger()
        ledger.record(
            credential_id="k0", provider="gemini", model=model,
            reset_at=NOW + offset, now=NOW, scope=scope,
        )
        return ledger

    def test_a_scope_survives_a_round_trip(self):
        restored = Ledger.from_dict(self._ledger(SCOPE_ACCOUNT).to_dict())
        assert restored.find("k0", MAIN).covers_every_model is True

    def test_a_row_written_before_scopes_existed_reads_as_unknown(self):
        payload = self._ledger(SCOPE_ACCOUNT).to_dict()
        del payload["benches"][0]["scope"]
        restored = Ledger.from_dict(payload)
        assert restored.find("k0", MAIN).scope == SCOPE_UNKNOWN
        # And "unknown" is the inert reading, not a third behaviour.
        assert restored.find("k0", MAIN).covers_every_model is False

    def test_a_scope_nobody_recognises_reads_as_unknown(self):
        payload = self._ledger(SCOPE_ACCOUNT).to_dict()
        payload["benches"][0]["scope"] = "per_galaxy"
        assert Ledger.from_dict(payload).find("k0", MAIN).scope == SCOPE_UNKNOWN

    def test_evidence_is_not_erased_by_a_later_silence(self):
        # The provider named the scope once and stayed quiet on the retry. It
        # has not changed its mind, and forgetting would release a key the
        # provider already said is spent everywhere.
        ledger = self._ledger(SCOPE_ACCOUNT)
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 2 * HOUR, now=NOW, scope=SCOPE_UNKNOWN,
        )
        assert ledger.find("k0", MAIN).covers_every_model is True

    def test_fresh_evidence_does_replace_the_old_reading(self):
        ledger = self._ledger(SCOPE_UNKNOWN)
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 2 * HOUR, now=NOW, scope=SCOPE_ACCOUNT,
        )
        assert ledger.find("k0", MAIN).scope == SCOPE_ACCOUNT

    def test_a_probe_does_not_lose_the_scope(self):
        ledger = self._ledger(SCOPE_ACCOUNT)
        ledger.note_probe("k0", MAIN, NOW + 60)
        assert ledger.find("k0", MAIN).covers_every_model is True

    def test_the_later_of_two_account_benches_is_the_answer(self):
        ledger = self._ledger(SCOPE_ACCOUNT)
        ledger.record(
            credential_id="k0", provider="gemini", model=AUX,
            reset_at=NOW + 5 * HOUR, now=NOW, scope=SCOPE_ACCOUNT,
        )
        assert ledger.shared_bench_for("k0", NOW).reset_at == NOW + 5 * HOUR

    def test_an_expired_account_bench_holds_nothing(self):
        ledger = self._ledger(SCOPE_ACCOUNT)
        assert ledger.shared_bench_for("k0", NOW + 2 * HOUR) is None

    def test_spent_until_takes_the_later_of_both_reasons(self):
        ledger = self._ledger(SCOPE_ACCOUNT, model=MAIN, offset=5 * HOUR)
        ledger.record(
            credential_id="k0", provider="gemini", model=AUX,
            reset_at=NOW + HOUR, now=NOW, scope=SCOPE_PER_MODEL,
        )
        # AUX is spent for an hour on its own account, and for five by the
        # key-wide limit. It comes back when the last one lapses.
        assert ledger.spent_until("k0", AUX, NOW) == NOW + 5 * HOUR

    def test_spent_until_is_none_when_nothing_holds_the_key(self):
        assert Ledger().spent_until("k0", MAIN, NOW) is None


class TestPlanningAroundIt:
    def _benched(self, reset_at: float = NOW + HOUR) -> EntryView:
        return EntryView(credential_id="k0", status=STATUS_EXHAUSTED, reset_at=reset_at)

    def _free(self) -> EntryView:
        return EntryView(credential_id="k0", status="ok", reset_at=None)

    def _ledger(self, scope: str, *, offset: float = HOUR) -> Ledger:
        ledger = Ledger()
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + offset, now=NOW, scope=scope,
        )
        return ledger

    def test_a_key_wide_limit_is_not_released_for_another_model(self):
        actions = plan([self._benched()], self._ledger(SCOPE_ACCOUNT), model=AUX, now=NOW)
        assert actions == []

    def test_a_per_model_limit_still_is(self):
        actions = plan([self._benched()], self._ledger(SCOPE_PER_MODEL), model=AUX, now=NOW)
        assert [a.kind for a in actions] == [RELEASE]

    def test_an_unstated_scope_behaves_exactly_as_before(self):
        # The regression guard for this whole version: saying nothing must not
        # become a new way to lock a key up.
        actions = plan([self._benched()], self._ledger(SCOPE_UNKNOWN), model=AUX, now=NOW)
        assert [a.kind for a in actions] == [RELEASE]

    def test_a_key_the_host_freed_is_held_again_for_every_model(self):
        actions = plan([self._free()], self._ledger(SCOPE_ACCOUNT), model=AUX, now=NOW)
        assert [a.kind for a in actions] == [HOLD]
        assert actions[0].reset_at == NOW + HOUR
        assert "every model" in actions[0].why

    def test_a_bench_kame_did_not_write_is_still_untouched(self):
        # The fingerprint gate outranks everything, including this dimension.
        actions = plan(
            [self._benched(reset_at=NOW + 999.0)],
            self._ledger(SCOPE_ACCOUNT),
            model=AUX,
            now=NOW,
        )
        assert actions == []

    def test_an_expired_key_wide_limit_stops_holding(self):
        ledger = self._ledger(SCOPE_ACCOUNT)
        actions = plan([self._free()], ledger, model=AUX, now=NOW + 2 * HOUR)
        assert actions == []

    def test_the_model_that_earned_it_is_unaffected(self):
        actions = plan([self._benched()], self._ledger(SCOPE_ACCOUNT), model=MAIN, now=NOW)
        assert actions == []

    def test_one_key_wide_bench_does_not_hold_a_different_credential(self):
        other = EntryView(credential_id="k1", status="ok", reset_at=None)
        actions = plan([other], self._ledger(SCOPE_ACCOUNT), model=AUX, now=NOW)
        assert actions == []
