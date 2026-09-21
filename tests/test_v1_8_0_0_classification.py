"""Three measured classification gaps, closed against the 1.8.0.0 answer key.

``research/1.8.0.0/gate/1800-ceiling.json`` graded the committed tree (the
``max_hold_seconds`` ceiling, nothing past it) against the independently
written answer key, ``research/1.8.0.0/expected/verdicts.jsonl`` — a reviewer
who never opened this plugin's code. Three real shapes disagreed:

* **real-04** (27 real records) — every real Gemini per-minute refusal on
  this machine is a *token* limit (``quotaId`` names
  ``...InputTokensPerModelPerMinute...``), and ``core.quota.QuotaWindow`` had
  no way to say so: its markers collapsed the finding to the same
  ``"per_minute"`` an ordinary request-counted throttle gets.
* **real-10** (21 real records) — a read timeout implicates nothing about the
  credential (R36), and until now it still cost the key a three-second bench
  it never owed.
* **real-11** (16 real records) — a gateway's "Invalid token" (401,
  ``api.tokenrouter.com``, a one-api/new-api style gateway) does not match
  this plugin's Gemini-tuned invalid-key wording, so it fell to the generic
  bare-401 path and rested twenty seconds — the unsized-throttle number,
  R23 — instead of the credential-problem number the host itself already
  uses, R22's ``EXHAUSTED_TTL_401_SECONDS`` = 300s.

Each class below starts with the exact shape from ``verdicts.jsonl``/
``matchers.json`` and asserts the fix; each also carries at least one
regression guard proving the *adjacent*, already-correct behaviour did not
move.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_8_0_0_classification_under_test"


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
quota = importlib.import_module(f"{PACKAGE}.core.quota")
catalog = importlib.import_module(f"{PACKAGE}.core.catalog")
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
escalate = importlib.import_module(f"{PACKAGE}.core.escalate")
doctor = importlib.import_module(f"{PACKAGE}.core.doctor")

QuotaWindow = quota.QuotaWindow
QuotaScope = quota.QuotaScope
classify = classify_mod.classify

NOW = 1_788_900_000.0


# ===========================================================================
# Gap 1 — real-04: a token-per-minute counter is not "per_minute"
# ===========================================================================


class TestGap1TokensPerMinute:
    """research/1.8.0.0/expected/verdicts.jsonl real-04, 27 real records.

    ``quotaId``: ``GenerateContentInputTokensPerModelPerMinute-FreeTier``.
    ``quotaMetric``:
    ``generativelanguage.googleapis.com/generate_content_free_tier_input_token_count``.
    Both carry the fact in the same string (``Tokens`` ... ``PerMinute``, with
    ``PerModel`` inserted between them by Google's own naming, so a single
    contiguous marker can never catch it) — this is why the fix has to look
    for *both* an evidence-of-tokens marker and a per-minute marker in the
    same haystack, not a new contiguous phrase.
    """

    QUOTA_ID = "GenerateContentInputTokensPerModelPerMinute-FreeTier"
    QUOTA_METRIC = (
        "generativelanguage.googleapis.com/"
        "generate_content_free_tier_input_token_count"
    )
    MESSAGE = (
        "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current "
        "quota, please check your plan and billing details. For more "
        "information on this error, head to: "
    )

    def _body(self, *, retry_delay_s=29):
        return {
            "error": {
                "code": 429,
                "message": self.MESSAGE,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": self.QUOTA_ID,
                                "quotaMetric": self.QUOTA_METRIC,
                                "quotaValue": "250000",
                            }
                        ],
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": f"{retry_delay_s}s",
                    },
                ],
            }
        }

    def test_quota_window_has_a_distinct_tokens_per_minute_member(self):
        assert QuotaWindow.TOKENS_PER_MINUTE == "tokens_per_minute"
        assert QuotaWindow.TOKENS_PER_MINUTE != QuotaWindow.PER_MINUTE

    def test_detect_quota_window_reads_the_real_shape_as_tpm(self):
        window = quota.detect_quota_window(self.MESSAGE, self._body())
        assert window == QuotaWindow.TOKENS_PER_MINUTE

    def test_detect_quota_window_reads_quota_limit_alone(self):
        # ``quota_limit`` is what the identical quotaId string is called
        # inside ``google.rpc.ErrorInfo.metadata`` — the half Hermes' Gemini
        # adapter actually keeps when the QuotaFailure block itself is gone
        # (classify.py's own module docstring documents this exact defect).
        # Detection must not depend on which of the two spellings survived.
        body = {"error": {"metadata": {"quota_limit": self.QUOTA_ID}}}
        window = quota.detect_quota_window(self.MESSAGE, body)
        assert window == QuotaWindow.TOKENS_PER_MINUTE

    def test_an_ordinary_request_counted_per_minute_refusal_is_unaffected(self):
        # Regression guard: real-01/real-02's shape (RESOURCE_EXHAUSTED, no
        # quotaId at all, or an RPM-only quotaId) must keep reading as the
        # plain per_minute window, not be swept into the new one.
        window = quota.detect_quota_window(self.MESSAGE, None)
        assert window == QuotaWindow.UNKNOWN

        rpm_body = {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}
                        ],
                    }
                ]
            }
        }
        assert quota.detect_quota_window(self.MESSAGE, rpm_body) == QuotaWindow.PER_MINUTE

    def test_a_bare_401_invalid_token_is_never_read_as_a_tpm_window(self):
        # A message that merely contains the word "token" (an auth token,
        # nothing to do with a quota counter) must not be swept in either —
        # TOKEN_UNIT_MARKERS is quota-shaped ("inputtoken"/"outputtoken"/
        # "tokencount"), not the bare word.
        window = quota.detect_quota_window("Invalid token (request id: X)", None)
        assert window != QuotaWindow.TOKENS_PER_MINUTE

    def test_classify_produces_the_tpm_window_end_to_end(self):
        verdict = classify(
            provider="gemini",
            status_code=429,
            error_message=self.MESSAGE,
            error_body=self._body(),
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.TOKENS_PER_MINUTE
        # Timing is untouched: real-04's own rest already agreed with the
        # answer key before this fix (obey the stated retryDelay, capped),
        # and nothing about naming the window differently may change that.
        assert verdict.reset_at == pytest.approx(NOW + 29.0, abs=1.0)

    def test_catalog_read_quota_id_promotes_the_same_way(self):
        # The second, independent detection path: an unsized TPM refusal
        # (no retryDelay at all) reaches classify.py's catalog_throttle
        # branch, which reads catalog.read_quota_id directly rather than
        # quota.detect_quota_window. Both paths must agree on one payload.
        window, scope = catalog.read_quota_id(f'"quotaId":"{self.QUOTA_ID}"')
        assert window == QuotaWindow.TOKENS_PER_MINUTE
        assert scope == QuotaScope.PER_MODEL

    def test_window_bench_default_exists_for_the_new_member(self):
        # Producer/consumer crossing #1: every QuotaWindow member that can
        # reach compute_reset_at's "nothing stated" branch must have a
        # default, or a TPM refusal with no retryDelay at all would silently
        # fall through to "no quota signal — deferring to host" instead of
        # the per-minute re-probe every sibling window gets.
        assert QuotaWindow.TOKENS_PER_MINUTE in quota._WINDOW_BENCH_DEFAULTS
        assert (
            quota._WINDOW_BENCH_DEFAULTS[QuotaWindow.TOKENS_PER_MINUTE]
            == quota.DEFAULT_PER_MINUTE_BENCH_SECONDS
        )

    def test_escalation_cap_exists_for_the_new_member(self):
        # Producer/consumer crossing #2: escalate.py's per-window ceiling
        # dict must know the new member too, or a stuck TPM key could widen
        # to a full day the way an unnamed window can (see escalate.py's own
        # comment on why "unknown" is deliberately absent but every *named*
        # short window is deliberately present).
        assert QuotaWindow.TOKENS_PER_MINUTE in escalate._WINDOW_ESCALATION_CAPS
        assert (
            escalate._WINDOW_ESCALATION_CAPS[QuotaWindow.TOKENS_PER_MINUTE]
            == escalate.MAX_PER_MINUTE_HOLD_SECONDS
        )

    def test_tokens_per_minute_is_not_treated_as_a_long_window(self):
        # A TPM refusal must obey the stated retryDelay directly, the same
        # as an RPM one — not fall into the "long window" branch that
        # discards a short delay in favour of a daily-style default, which
        # would be real-04's rest field regressing to fix its window field.
        verdict = classify(
            provider="gemini",
            status_code=429,
            error_message=self.MESSAGE,
            error_body=self._body(retry_delay_s=6),
            now_epoch=NOW,
        )
        assert verdict.reset_at == pytest.approx(NOW + 6.0, abs=1.0)


# ===========================================================================
# Gap 2 — real-11: a gateway "Invalid token" is a credential problem, not a
# throttle wearing a 401's clothes
# ===========================================================================


class TestGap2GatewayInvalidToken:
    """research/1.8.0.0/expected/verdicts.jsonl real-11, 16 real records.

    ``matchers.json``'s own predicate for this shape: status 401,
    ``body_contains: ["invalid token", "api_error"]``, expected
    ``{family: auth_dead, window: none, scope: credential, action:
    rotate_key}``, rest ``{kind: fixed, seconds: 300, cap: 3600}``.
    """

    MESSAGE = (
        "Error code: 401 - {'error': {'code': '', 'message': 'Invalid token "
        "(request id: 20260906123456ABCDEFGHIJKLMNOPQ)', 'type': 'api_error'}}"
    )
    BODY = {
        "error": {
            "code": "",
            "message": "Invalid token (request id: 20260906123456ABCDEFGHIJKLMNOPQ)",
            "type": "api_error",
        }
    }

    def test_the_rich_classifier_declines_this_exact_payload(self):
        # Documents *why* the fix lives in the legacy carousel classifier:
        # the structured "type": "api_error" field reads as catalog.SERVER
        # (Anthropic/NVIDIA/etc. use the same value for a busy provider), so
        # core.classify.classify() returns None long before it would ever
        # reach a message-pattern check — status 401 alone reaches the same
        # None at its own step 4 regardless. Either way this shape has
        # always fallen through to core.carousel.classify().
        assert classify(
            status_code=401, error_message=self.MESSAGE, error_body=self.BODY,
            now_epoch=NOW,
        ) is None

    def test_carousel_classify_rests_the_measured_credential_number(self):
        delay, kind, status = carousel.classify(
            None, self.MESSAGE, status_code=401,
        )
        assert kind == "auth"
        assert status == 401
        assert delay == carousel.CREDENTIAL_PROBLEM_REST_S == 20.0

    def test_the_other_two_gateway_wordings_rest_the_same(self):
        for message in (
            "401: This token is invalid or expired",
            "403: Your access token is invalid or expired, please refresh it",
        ):
            delay, kind, _ = carousel.classify(None, message, status_code=401)
            assert kind == "auth", message
            assert delay == 20.0, message

    def test_a_genuinely_wordless_401_still_rests_twenty_seconds(self):
        # Regression guard, pinned already by test_v1_6_0_1.py's
        # test_a_bare_401_stays_ambiguous: the 1.4.0 disaster shape
        # ("Unauthorized" — an expired OAuth token a second from refreshing,
        # or a proxy) has no evidence of a credential problem at all and
        # must keep the short, cheap re-probe.
        delay, kind, _ = carousel.classify(None, "Unauthorized", status_code=401)
        assert kind == "auth"
        assert delay == carousel.REJECTED_REST_S == 20.0

    def test_geminis_explicit_dead_key_wording_is_unaffected(self):
        # Regression guard: the *unambiguous* Gemini wording still retires
        # on sight via INVALID_KEY_INDICATORS/"revoked" — untouched by this
        # gap, which only ever widens the *ambiguous* auth branch.
        delay, kind, status = carousel.classify(
            None, "API key not valid. Please pass a valid API key.",
            status_code=401,
        )
        assert kind == "revoked"
        assert delay == carousel.REJECTED_REST_S == 20.0

    def test_no_key_is_retired_on_one_refusal(self):
        # R24, checked against the actual pool mechanics rather than only
        # against the returned kind: a single "Invalid token" refusal must
        # not flip state["retired"], because RETIRING_KINDS/REJECTED_KINDS
        # gate on the *kind* word ("auth", not "revoked") and this gap must
        # never change which kind a gateway token refusal produces.
        engine = carousel.Carousel()
        delay, kind, status = carousel.classify(None, self.MESSAGE, status_code=401)
        engine.mark("nvidia:some-model", "k1", False, delay, kind, now=NOW, stated=False)
        state = engine._pools["nvidia:some-model"]["k1"]
        assert state["retired"] is False
        assert state["consecutive_refusals"] == 1

    def test_is_auth_failure_recognises_the_wording_without_a_status_code(self):
        # Part (a) of the gap: robustness when status extraction fails and
        # only the message survives — the wording alone must still mark
        # this non-terminal (is_terminal defers to is_auth_failure first).
        assert carousel.is_auth_failure(None, "Invalid token (request id: X)") is True
        assert carousel.is_terminal(None, "Invalid token (request id: X)") is False

    def test_credential_problem_indicators_are_disjoint_from_invalid_key_indicators(self):
        # The design guarantee that keeps R24 true: the new wording must
        # never also satisfy the "revoked, retire on sight" list, or the two
        # branches would race and the actual behaviour would depend on
        # tuple order rather than on the rule.
        for phrase in carousel.CREDENTIAL_PROBLEM_INDICATORS:
            assert not carousel._matches(phrase, carousel.INVALID_KEY_INDICATORS), phrase


# ===========================================================================
# Gap 3 — real-10: a timeout benches the key for nothing it owes
# ===========================================================================


class TestGap3TimeoutBenchesForNothing:
    """research/1.8.0.0/expected/verdicts.jsonl real-10, 21 real records.

    The answer key's own reading is ``defer_host_retry`` / no bench at all.
    The coordinator's decision, implemented here, is narrower: keep
    rotating — free, one call, no wait, keeps the turn moving — but invent
    no wait either. ``action`` is therefore expected to keep disagreeing
    with the literal answer key (it stays ``rotate_key``); only ``rest``
    moves. R36/I2: no timeout of the plugin's own is added or removed.
    """

    MESSAGE = "Gemini streaming request failed: The read operation timed out"

    def test_carousel_classify_benches_zero_seconds(self):
        delay, kind, status = carousel.classify(None, self.MESSAGE, status_code=None)
        assert kind == "timeout"
        assert delay == 0.0
        assert delay == carousel.TIMEOUT_S

    def test_it_still_rotates_rather_than_raising(self):
        # Untouched by this gap on purpose (R36/I2: no timeout logic added
        # or removed) — is_terminal must keep returning False for a timeout,
        # which is what keeps the verdict "rotate" rather than "raise".
        assert carousel.is_terminal(None, self.MESSAGE) is False

    def test_the_key_is_immediately_selectable_again(self):
        engine = carousel.Carousel()
        delay, kind, _ = carousel.classify(None, self.MESSAGE, status_code=None)
        engine.mark("gemini:gemini-3.7-flash", "k1", False, delay, kind, now=NOW)
        key, status = engine.select("gemini:gemini-3.7-flash", ["k1"], now=NOW)
        assert (key, status) == ("k1", "SUCCESS")

    def test_no_strike_is_recorded(self):
        # "timeout" has never been in RETIRING_KINDS/REJECTED_KINDS, so a
        # zero bench changes nothing about retirement counting — pinned here
        # so a future change to either set is caught by this gap's own test,
        # not discovered later against a real pool.
        assert "timeout" not in carousel.RETIRING_KINDS
        assert "timeout" not in carousel.REJECTED_KINDS

    def test_the_doctor_table_mirrors_the_new_number(self):
        # doctor.EXPECTED_RESTS is a hand-kept mirror, by the module's own
        # design (its docstring: "a table generated from the code agrees
        # with the code by construction and proves nothing"), cross-checked
        # by tests/test_v1_6_0_1.py's
        # test_the_rest_table_agrees_with_the_code_it_describes. Pinned here
        # too, directly against this gap's own change.
        expected = dict((kind, rest) for kind, rest, _ in doctor.EXPECTED_RESTS)
        assert expected["timeout"] == carousel.TIMEOUT_S == 0.0
