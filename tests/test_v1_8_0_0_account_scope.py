"""Change 1 (1.8.0.0): an account-wide refusal benches a credential on every
model of a provider, not only the model it happened on.

Evidence (measured, not re-derived): ``research/1.8.0.0/expected/
verdicts.jsonl`` ``real-17`` — a real Codex ``429 usage_limit_reached`` on the
owner's own machine carried the SAME absolute reset (~23:13 UTC) under two
different models, ``gpt-5.6-luna`` at 19:42 and ``gpt-6-astra`` at 20:00. The
window belongs to the account, not to either model.

``core.carousel.Carousel`` keys every pool by ``provider:model`` (R29 —
``audit/1.8.0.0-01-regras-fechadas.md``), correctly, because most real quotas
are metered per model (Gemini's free tier, R07/R08). Until this release that
was the WHOLE story: right after an account-wide refusal on one model, the
plugin spent a call on the same credential under a sibling model and was
refused again. This file proves the fix without breaking the rule it sits
beside: R29's per-model pool is untouched for every refusal that carries no
account-wide evidence — the overwhelmingly common case — and only a refusal
``core.quota.detect_quota_scope`` itself calls ``QuotaScope.ACCOUNT`` (R01:
evidence in the response, never the provider's name) reaches across models.

Two layers, tested separately, the same split ``test_v1_8_0_0_ceiling.py``
uses for its own two chokepoints:

* ``TestClassifyDetectsAccountScopeForTheRealCodexShape`` — the EVIDENCE
  side: ``core.classify.classify()`` (and the ``core.quota`` marker it reads)
  actually reads ``QuotaScope.ACCOUNT`` off the real-17 body. Without this,
  the mechanism below would be correct and permanently dormant.
* Everything else — the MECHANISM side: ``core.carousel.Carousel`` directly,
  scope passed straight in, no host, the same style
  ``test_v1_8_0_0_ceiling.py`` already uses to test ``mark``/``select`` as
  the pure functions they are.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_8_0_0_account_scope_under_test"


def _load_package():
    """Import the plugin as a package, the way the Hermes loader does.

    Self-contained, like every other test module in this suite: its own
    ``PACKAGE`` name, its own load, so this file can be read and run without
    knowing what any other test file did to the module cache.
    """
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
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
quota_mod = importlib.import_module(f"{PACKAGE}.core.quota")
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")

Carousel = carousel_mod.Carousel
QuotaScope = quota_mod.QuotaScope

NOW = 1_000_000.0
HOUR = 3600.0


# ---------------------------------------------------------------------------
# The evidence side: does classify() actually read ACCOUNT off real-17?
# ---------------------------------------------------------------------------


class TestClassifyDetectsAccountScopeForTheRealCodexShape:
    def test_the_real_17_body_classifies_as_account_scope(self):
        # Verbatim shape from verdicts.jsonl's own message_template.
        body = {
            "error": {
                "type": "usage_limit_reached",
                "message": "The usage limit has been reached",
                "plan_type": "plus",
                "resets_at": int(NOW + 12660),
                "eligible_promo": None,
                "resets_in_seconds": 12660,
            }
        }
        message = "Error code: 429 - " + str(body)

        verdict = classify_mod.classify(
            provider="openai-codex",
            status_code=429,
            error_message=message,
            error_body=body,
            now_epoch=NOW,
        )

        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_scope == QuotaScope.ACCOUNT
        assert verdict.reset_at == pytest.approx(NOW + 12660)

    def test_a_gemini_per_model_daily_body_stays_per_model_scope(self):
        # Regression guard for the marker itself: the fix is narrow to
        # `usage_limit_reached`, not a general widening of `rate_limit`'s
        # scope reading. Google's own quotaId+quotaDimensions still reads
        # PER_MODEL, exactly as `test_scope.py` already pins.
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "Quota exceeded for quota metric 'Generate requests per model per day'",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                "quotaDimensions": {"model": "gemini-3.6-flash"},
                            }
                        ],
                    }
                ],
            }
        }
        verdict = classify_mod.classify(
            provider="gemini", status_code=429, error_body=body, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.quota_scope == QuotaScope.PER_MODEL

    def test_a_bare_terse_refusal_stays_unknown_scope(self):
        # A window-naming but scope-silent throttle (the same message
        # ``test_scope.py::test_an_ordinary_throttle_says_nothing_about_scope``
        # pins). No quotaId, no window, no account marker anywhere. Must stay
        # UNKNOWN, not be swept into ACCOUNT by a marker added for a
        # different shape.
        verdict = classify_mod.classify(
            provider="groq",
            status_code=429,
            error_message="Rate limit reached. Please try again in 20s.",
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.quota_scope == QuotaScope.UNKNOWN


# ---------------------------------------------------------------------------
# The mechanism side: core.carousel.Carousel, scope passed straight in
# ---------------------------------------------------------------------------

ID_CODEX_A = "openai-codex:gpt-5.6-luna"
ID_CODEX_B = "openai-codex:gpt-6-astra"
ID_OTHER_PROVIDER = "anthropic:gpt-5.6-luna"  # same model NAME, different provider
KEY = "sk-shared-codex-key"


class TestAccountScopeCrossesModelsOfTheSameProvider:
    def test_regression_first_a_per_model_daily_refusal_does_not_bench_siblings(self):
        # RED before Change 1, and the regression this whole file exists to
        # never break: a refusal with NO scope evidence — the pre-existing,
        # overwhelmingly common case, Gemini's per-key-per-model daily cap
        # (R29/R07) — must leave every OTHER model of the same provider
        # completely untouched. No ``scope=`` passed -> QuotaScope.UNKNOWN,
        # the pre-existing default for every caller this release does not
        # touch.
        engine = Carousel()
        engine.mark(ID_CODEX_A, KEY, False, 0.0, "daily", now=NOW)

        key, status = engine.select(ID_CODEX_B, [KEY], now=NOW + 1.0)
        assert (key, status) == (KEY, "SUCCESS")
        assert engine.healthy_count(ID_CODEX_B, [KEY], now=NOW + 1.0) == 1
        assert engine.next_recovery_seconds(ID_CODEX_B, [KEY], now=NOW + 1.0) is None

    def test_codex_shaped_account_refusal_benches_the_sibling_model(self):
        # research/1.8.0.0/expected/verdicts.jsonl real-17's own numbers.
        engine = Carousel()
        applied = engine.mark(
            ID_CODEX_A, KEY, False, 12660.0, "daily", now=NOW,
            stated=True, scope=QuotaScope.ACCOUNT,
        )
        assert applied == engine.max_hold_s  # G8 ceiling, not the raw 12660s

        key, status = engine.select(ID_CODEX_B, [KEY], now=NOW + 1.0)
        assert (key, status) == (KEY, "EXHAUSTED")
        assert engine.healthy_count(ID_CODEX_B, [KEY], now=NOW + 1.0) == 0
        assert engine.next_recovery_seconds(ID_CODEX_B, [KEY], now=NOW + 1.0) == pytest.approx(
            engine.max_hold_s - 1.0
        )

    def test_it_never_leaks_across_providers(self):
        engine = Carousel()
        engine.mark(
            ID_CODEX_A, KEY, False, 12660.0, "daily", now=NOW,
            stated=True, scope=QuotaScope.ACCOUNT,
        )
        # Same literal key string, a DIFFERENT provider. The account hold is
        # keyed by (provider, key) — see core/carousel.py's own comment on
        # ``_account_hold`` — so a wholly separate provider is untouched even
        # though the string is identical.
        key, status = engine.select(ID_OTHER_PROVIDER, [KEY], now=NOW + 1.0)
        assert (key, status) == (KEY, "SUCCESS")
        assert engine.healthy_count(ID_OTHER_PROVIDER, [KEY], now=NOW + 1.0) == 1

    def test_it_never_retires_the_credential(self):
        # An account-wide bench is a clock, not a verdict about the key
        # itself — it must never touch ``retired``/``consecutive_refusals``.
        engine = Carousel()
        engine.mark(
            ID_CODEX_A, KEY, False, 12660.0, "daily", now=NOW,
            stated=True, scope=QuotaScope.ACCOUNT,
        )
        assert engine.is_retired(ID_CODEX_A, KEY) is False
        assert engine.is_retired(ID_CODEX_B, KEY) is False

    def test_success_on_the_sibling_model_clears_the_account_hold(self):
        # "The same key answering on ANY model of that provider clears it —
        # a success is proof the account is not out" (R15 already reasons
        # this way for the escalation streak; applied here to the hold). A
        # THIRD identity, C, that never failed on its own and is blocked
        # purely by the account hold is what proves the CLEARING reached
        # across models too, not just the marking.
        ID_CODEX_C = "openai-codex:o3-mini"
        engine = Carousel()
        engine.mark(
            ID_CODEX_A, KEY, False, 12660.0, "daily", now=NOW,
            stated=True, scope=QuotaScope.ACCOUNT,
        )
        key, status = engine.select(ID_CODEX_C, [KEY], now=NOW + 5.0)
        assert status == "EXHAUSTED"

        # B answering is still evidence the account is fine, even though the
        # refusal happened on A and the blocked probe was on C.
        engine.mark(ID_CODEX_B, KEY, True, now=NOW + 10.0)

        key, status = engine.select(ID_CODEX_C, [KEY], now=NOW + 10.0)
        assert (key, status) == (KEY, "SUCCESS")
        # A keeps its OWN per-model bench from the refusal that actually
        # happened there — a sibling's success repairs the ACCOUNT-wide
        # reach, never the specific model's own quota, which R29 still
        # tracks separately.
        key, status = engine.select(ID_CODEX_A, [KEY], now=NOW + 10.0)
        assert status == "EXHAUSTED"

    def test_a_scoped_global_reset_does_not_resurrect_the_hold(self):
        # forget() with no identity clears it; forget(identity) for one
        # model does NOT — a provider-wide fact is not evidence a single
        # model's pool reset repaired (see core/carousel.py's own comment on
        # ``forget``).
        engine = Carousel()
        engine.mark(
            ID_CODEX_A, KEY, False, 12660.0, "daily", now=NOW,
            stated=True, scope=QuotaScope.ACCOUNT,
        )
        engine.forget(ID_CODEX_A)
        key, status = engine.select(ID_CODEX_B, [KEY], now=NOW + 1.0)
        assert status == "EXHAUSTED"

        engine.forget(None)
        key, status = engine.select(ID_CODEX_B, [KEY], now=NOW + 1.0)
        assert (key, status) == (KEY, "SUCCESS")

    def test_the_ceiling_bounds_the_account_hold_the_same_as_every_other_hold(self):
        engine = Carousel(max_hold_s=300.0)
        applied = engine.mark(
            ID_CODEX_A, KEY, False, 9 * HOUR, "daily", now=NOW,
            stated=True, scope=QuotaScope.ACCOUNT,
        )
        assert applied == 300.0

        key, status = engine.select(ID_CODEX_B, [KEY], now=NOW + 299.0)
        assert status == "EXHAUSTED"
        key, status = engine.select(ID_CODEX_B, [KEY], now=NOW + 301.0)
        assert (key, status) == (KEY, "SUCCESS")


class TestHealthyCountAndEtaAgreeWithSelectAcrossIdentities:
    """Cross the sets, per the task — not one value read off one identity."""

    def test_every_identity_of_the_held_provider_agrees_on_health(self):
        engine = Carousel()
        keys = ["k1", "k2", "k3"]
        identities = [
            "openai-codex:gpt-5.6-luna",
            "openai-codex:gpt-6-astra",
            "openai-codex:o3-mini",
        ]
        for identity in identities:
            for key in keys:
                engine.select(identity, [key], now=NOW)  # seed every pool row

        # k1: account-wide hold, earned on the FIRST identity only.
        engine.mark(
            identities[0], "k1", False, HOUR, "daily", now=NOW,
            stated=True, scope=QuotaScope.ACCOUNT,
        )
        # k2: an ordinary per-model hold, same identity, no account scope.
        engine.mark(identities[0], "k2", False, HOUR, "daily", now=NOW, stated=True)
        # k3: never fails, stays healthy everywhere.

        later = NOW + 1.0
        for identity in identities:
            healthy_via_select = {
                key for key in keys
                if engine.select(identity, [key], now=later)[1] == "SUCCESS"
            }
            healthy_via_eta = {
                key for key in keys
                if engine.next_recovery_seconds(identity, [key], now=later) is None
            }
            # select()'s SUCCESS set and eta's "no wait" set must be the
            # SAME set on every identity of this provider — crossed, not
            # spot-checked on one key.
            assert healthy_via_select == healthy_via_eta, identity
            assert engine.healthy_count(identity, keys, now=later) == len(healthy_via_select), identity

        # k1: held on EVERY identity of the provider (account-wide) —
        # crossing the three identities' own results, not asserting one.
        assert all(
            engine.select(identity, ["k1"], now=later)[1] == "EXHAUSTED"
            for identity in identities
        )
        # k2: held ONLY on the identity it actually failed on.
        assert engine.select(identities[0], ["k2"], now=later)[1] == "EXHAUSTED"
        assert all(
            engine.select(identity, ["k2"], now=later)[1] == "SUCCESS"
            for identity in identities[1:]
        )
        # k3: healthy everywhere, on every identity, every call.
        assert all(
            engine.select(identity, ["k3"], now=later)[1] == "SUCCESS"
            for identity in identities
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
