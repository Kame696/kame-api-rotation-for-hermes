"""1.8.0.0 G9 -- one status/code, several real conditions, turned into tests.

``research/1.8.0.0/expected/same-code-catalogue.md`` was written by a reviewer
who never opened this plugin's code, from sources only (documented provider
error shapes and the owner's own measured recovery-time records). It
enumerates every case where one HTTP status or provider code covers different
real conditions, the field that is supposed to separate them, and -- where no
field separates them -- the safe evidence-based behaviour instead of a false
distinction.

Each class below is one row (or one tight cluster of rows) from that
catalogue: the row's own condition, its own "field that says so", and the
row's own expected family/window/rest, checked against real code -- mostly
``core.classify.classify()``, the single evidence-reading hook every row's
"field" language is written about, and occasionally ``core.carousel``/
``dispatch_binding`` directly for the handful of rows whose answer is about
*who bears the cost* (a rotation vs. a bench) rather than about a
classification verdict.

Where a row's exact wording is not matched by anything in ``core/catalog.py``,
``core/classify.py`` or ``core/provider_rules.py`` -- confirmed by grep across
all three before writing the test, not assumed -- the plugin currently falls
through to a more generic reading than the catalogue asks for. Per the task's
own rule, neither the plugin nor the catalogue is changed to make these
agree: the test is marked ``xfail`` with the one-line reason, and every one is
listed again in the final report for the coordinator to decide.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_gate_same_code_under_test"


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
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")
catalog = importlib.import_module(f"{PACKAGE}.core.catalog")
quota = importlib.import_module(f"{PACKAGE}.core.quota")
stitch = importlib.import_module(f"{PACKAGE}.core.stitch")
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")

classify = classify_mod.classify
QuotaWindow = quota.QuotaWindow
QuotaScope = quota.QuotaScope

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean():
    carousel.ENGINE.forget()
    yield
    carousel.ENGINE.forget()


# ===========================================================================
# 1. HTTP 429 -- at least nine different facts
# ===========================================================================


class TestSection1Http429:
    """Catalogue section 1. Reading order for every row:
    structured type/code > structured details > HTTP status > headers > prose.
    """

    def test_gemini_requests_per_minute(self):
        # quotaId contains PerMinute + RetryInfo.retryDelay -> throttle /
        # per_minute / obey stated, capped 3600.
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "You exceeded your current quota.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                            }
                        ],
                    },
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "21s",
                    },
                ],
            }
        }
        verdict = classify(
            provider="gemini", status_code=429, error_message="You exceeded your current quota.",
            error_body=body, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.PER_MINUTE
        assert verdict.reset_at == pytest.approx(NOW + 21.0, abs=1.0)

    def test_gemini_input_tokens_per_minute(self):
        # Same quotaId string carries both "Tokens" and "PerMinute" -- a
        # distinct QuotaWindow member from a plain per-minute counter.
        body = {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                                "quotaMetric": (
                                    "generativelanguage.googleapis.com/"
                                    "generate_content_free_tier_input_token_count"
                                ),
                                "quotaValue": "250000",
                            }
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "29s"},
                ],
            }
        }
        verdict = classify(
            provider="gemini", status_code=429, error_message="You exceeded your current quota.",
            error_body=body, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.TOKENS_PER_MINUTE

    def test_gemini_requests_per_day(self):
        # quotaId contains PerDay, quotaValue 20 -- and R06: the stated
        # retryDelay is never obeyed as the window length for this label.
        body = {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                "quotaValue": "20",
                            }
                        ],
                    },
                    # A short, lying retryDelay -- measured real shape (R06).
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "22s"},
                ],
            }
        }
        verdict = classify(
            provider="gemini", status_code=429, error_message="You exceeded your current quota.",
            error_body=body, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.PER_DAY
        # Not 22 seconds -- the short hint must never be read as the window.
        assert verdict.reset_at >= NOW + 60.0

    @pytest.mark.xfail(
        reason="deliberate, see decisions/0005: classify.py's own "
        "_AMBIGUOUS_BILLING_PATTERNS reads 'exceeded your current quota ... "
        "billing' as billing whenever nothing names a wait (its docstring: "
        "removing this branch made every OpenAI-out-of-credits payload "
        "wearing this sentence read as rate_limit, failing four of the "
        "host's own tests) -- so a Gemini spend-based paid-tier throttle "
        "with genuinely no other field (the row's own condition) is "
        "classified billing/account/hourly rather than the catalogue's "
        "throttle/rolling_unspecified/20s. This is the plugin's considered, "
        "tested tradeoff, not an oversight; left untouched for the "
        "coordinator.",
        strict=True,
    )
    def test_gemini_spend_based_paid_tier_no_details(self):
        # Nothing in the payload at all -- documented as a rolling
        # 10-minute window; the catalogue's own expectation is "unknown", a
        # short probe.
        verdict = classify(
            provider="gemini", status_code=429,
            error_message="You exceeded your current quota, please check your plan and billing details.",
            error_body=None, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.UNKNOWN

    def test_gemini_vertex_shared_capacity_generic_message(self):
        verdict = classify(
            provider="vertex", status_code=429,
            error_message="Resource has been exhausted (e.g. check quota).",
            error_body={"error": {"message": "Resource has been exhausted (e.g. check quota)."}},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.UNKNOWN

    def test_openai_compatible_out_of_money(self):
        body = {"error": {"type": "insufficient_quota", "message": "You exceeded your current quota, please check your plan and billing details."}}
        verdict = classify(
            provider="openai", status_code=429,
            error_message="You exceeded your current quota, please check your plan and billing details.",
            error_body=body, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_codex_subscription_window(self):
        # Verified real shape (test_v1_7_0_6_policy.py): resets_at wins.
        verdict = classify(
            provider="openai-codex", status_code=429,
            error_message="You have hit your usage limit.",
            error_body={"error": {"type": "usage_limit_reached", "resets_at": NOW + 12660,
                                   "resets_in_seconds": 12660}},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.retryable and verdict.should_rotate_credential
        assert verdict.reset_at == NOW + 12660

    def test_anthropic_monthly_spend_cap(self):
        # Verified real shape (test_v1_7_0_7_monthly_scope.py).
        exc = Exception("Request declined")
        exc.request = NS(url="https://api.anthropic.com/v1/messages")
        verdict = classify(
            provider="custom", status_code=429, error_message="Request declined", error=exc,
            error_body={"error": {"type": "rate_limit_error", "details": {
                "error_code": "enforced_spend_limit_reached"}}},
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "billing"
        assert verdict.quota_window == "per_month"
        assert verdict.quota_scope == "account"
        assert verdict.reset_at >= NOW + 3600

    def test_acceleration_limit_slow_down(self):
        # OpenAI's `code: slow_down` -- documented, but the row's own
        # expected family is still "throttle", the ordinary 429 reading.
        body = {"error": {"code": "slow_down", "message": "Reduce your request rate."}}
        verdict = classify(
            provider="openai", status_code=429, error_message="Reduce your request rate.",
            error_body=body, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"

    def test_aggregator_relaying_an_upstream_429(self):
        # `error.metadata.raw`/`provider_name`, or the envelope phrase --
        # classify() has to get out of the way entirely (None), because the
        # nested text is about somebody else's credential.
        body = {"error": {"message": "Provider returned error", "metadata": {
            "raw": "429 rate limited", "provider_name": "some-upstream"}}}
        verdict = classify(provider="custom", status_code=429, error_body=body, now_epoch=NOW)
        assert verdict is None

    def test_nvidia_bare_gateway_429(self):
        # Verified real shape (test_v1_7_0_7_nvidia_hints.py): no body at
        # all beyond a bare problem-details title, window stays unknown, an
        # explicit header hint is still honoured.
        verdict = classify(
            provider="nvidia", error_body={"status": 429, "title": "Too Many Requests"},
            headers={"retry-after": "30"}, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.quota_window == "unknown"
        assert verdict.reason != "billing"
        assert verdict.reset_at == pytest.approx(NOW + 30.0)

    @pytest.mark.xfail(
        reason="deliberate, see decisions/0005: same tradeoff as "
        "test_gemini_spend_based_paid_tier_no_details above -- with no field "
        "attached at all, classify.py's ambiguous-billing pattern reads this "
        "exact shared sentence as billing, not the catalogue's 'proves "
        "nothing, stay unknown'. Left untouched for the coordinator.",
        strict=True,
    )
    def test_the_sentence_that_separates_nothing_still_reads_as_throttle_here(self):
        # The exact sentence Google's per-minute throttle and OpenAI's
        # out-of-credits refusal share, word for word -- with NEITHER
        # provider's field attached. Prose alone must not manufacture a
        # window; catalogue's own point is that this sentence proves
        # nothing on its own, so the safe reading (unknown, not billing) is
        # what is checked, not a guess at which provider sent it.
        message = "You exceeded your current quota, please check your plan and billing details."
        verdict = classify(provider="", status_code=429, error_message=message, error_body=None,
                            now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == QuotaWindow.UNKNOWN


# ===========================================================================
# 2. HTTP 503 -- four conditions, two opposite rests
# ===========================================================================


class TestSection2Http503:
    def test_transient_overload_key_never_implicated(self):
        # UNAVAILABLE / overload wording -> classify() stays out of it
        # entirely (family=SERVER returns None): the host is already right,
        # and no per-key bench is KAME's to size.
        verdict = classify(
            provider="gemini", status_code=503, error_message="The model is overloaded. Please try again later.",
            error_body={"error": {"status": "UNAVAILABLE",
                                   "message": "The model is overloaded. Please try again later."}},
            now_epoch=NOW,
        )
        assert verdict is None

    def test_maintenance_with_a_stated_wait_is_obeyed_and_key_scoped(self, monkeypatch):
        # RFC 9110 explicitly allows Retry-After on a 503. Sizing this is a
        # dispatch-level concern (core.carousel via dispatch_binding), not
        # classify() -- the same real path R26 is proven against.
        monkeypatch.setattr(dispatch_binding.time, "time", lambda: NOW)
        engine = carousel.Carousel()
        exc = Exception("Service temporarily unavailable")
        exc.status_code = 503
        exc.headers = {"retry-after": "7"}
        binding = dispatch_binding.DispatchBinding(engine=engine)
        binding._on_failure("groq:m", "a", exc, "test", 1, False)
        assert engine._pools["groq:m"]["a"]["sick_until"] == NOW + 7

    def test_a_503_does_not_borrow_unrelated_quota_reset_telemetry(self, monkeypatch):
        # R26: quota-reset telemetry on a 5xx is not a retry instruction --
        # only Retry-After/Retry-After-Ms count for server failures.
        monkeypatch.setattr(dispatch_binding.time, "time", lambda: NOW)
        engine = carousel.Carousel()
        exc = Exception("Service temporarily unavailable")
        exc.status_code = 503
        exc.headers = {"x-ratelimit-reset-requests": "180s", "x-ratelimit-remaining-requests": "999"}
        binding = dispatch_binding.DispatchBinding(engine=engine)
        binding._on_failure("groq:m", "a", exc, "test", 1, False)
        assert engine._pools["groq:m"]["a"]["sick_until"] == NOW + carousel.SERVER_BASE_S

    def test_gateway_has_no_channel_for_this_model_no_per_key_bench(self):
        # aihubmix: "Incorrect model ID ... or you do not have permission to
        # use this model" -- no per-key bench, surface; classify() staying
        # out (None) is the same outcome the catalogue asks for, since
        # nothing here is evidence about the credential.
        verdict = classify(
            provider="aihubmix", status_code=503,
            error_message="Incorrect model ID or you do not have permission to use this model",
            error_body={"error": {"message": "Incorrect model ID or you do not have permission to use this model"}},
            now_epoch=NOW,
        )
        assert verdict is None

    def test_upstream_provider_throttling_never_a_quota_bench_on_our_key(self):
        # aihubmix "Rate limited by provider ..." -- upstream, flat rest,
        # never charged to our credential as a quota depletion.
        verdict = classify(
            provider="aihubmix", status_code=503,
            error_message="Rate limited by provider upstream-inc",
            error_body={"error": {"message": "Rate limited by provider upstream-inc"}},
            now_epoch=NOW,
        )
        assert verdict is None

    def test_a_5xx_carrying_quota_text_is_still_server_the_status_wins(self):
        # provider-errors.md #13: the status wins over quota-shaped text
        # sitting in the same body.
        verdict = classify(
            provider="gemini", status_code=503,
            error_message="RESOURCE_EXHAUSTED: quota exceeded, service unavailable",
            error_body={"error": {"status": "UNAVAILABLE",
                                   "message": "RESOURCE_EXHAUSTED: quota exceeded, service unavailable"}},
            now_epoch=NOW,
        )
        assert verdict is None


# ===========================================================================
# 3. HTTP 401 (and the 400 that should be a 401)
# ===========================================================================


class TestSection3Http401:
    def test_gemini_api_key_invalid_arrives_as_a_400_not_a_401(self):
        # Checking status before text ends a whole run over one dead key in
        # a pool of fourteen -- Google's invalid-key answer is a 400.
        body = {"error": {"status": "INVALID_ARGUMENT",
                           "message": "API key not valid. Please pass a valid API key.",
                           "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                        "reason": "API_KEY_INVALID"}]}}
        verdict = classify(provider="gemini", status_code=400,
                            error_message="API key not valid. Please pass a valid API key.",
                            error_body=body, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth_permanent"

    def test_gemini_legacy_standard_key_rejected_is_auth_dead_not_oauth(self):
        body = {"error": {"status": "UNAUTHENTICATED",
                           "message": "Google Gemini rejected this API key's type -- "
                                      "expected OAuth 2 access token",
                           "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                        "reason": "ACCESS_TOKEN_TYPE_UNSUPPORTED"}]}}
        verdict = classify(provider="gemini", status_code=401,
                            error_message="expected OAuth 2 access token",
                            error_body=body, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth_permanent"

    def test_vertex_oauth_token_expired_defers_to_host_refresh(self):
        # Verified real shape (test_v1_7_0_7_oauth_ownership.py): classify()
        # returns None, and the real dispatch exit is auth_refresh, never a
        # retirement.
        exc = Exception("Missing, invalid, or expired OAuth token")
        exc.status_code = 401
        exc.request = NS(url="https://aiplatform.googleapis.com/v1/projects/p/locations/global/"
                              "publishers/google/models/m:generateContent")
        exc.body = {"error": {"code": 401, "status": "UNAUTHENTICATED",
                               "message": "Missing, invalid, or expired OAuth token"}}
        verdict = classify(provider="vertex", status_code=401, error=exc, error_body=exc.body,
                            now_epoch=NOW)
        assert verdict is None
        engine = carousel.Carousel()
        binding = dispatch_binding.DispatchBinding(engine=engine)
        result = binding._on_failure("vertex:m", "synthetic", exc, "test", 1, False)
        assert result[:2] == ("raise", "auth_refresh")
        assert engine.snapshot() == {}

    @pytest.mark.xfail(
        reason="Codex OAuth session expiry (RefreshTokenFailed) is a "
        "host-owned refresh path with no representation in this plugin's "
        "catalogue/classify.py at all (confirmed by grep) -- classify() "
        "correctly stays out of it (returns None, deferring to the host), "
        "but there is no test proving the *dispatch* exit is auth_refresh "
        "rather than an ordinary rotate/retire the way there is for Vertex "
        "above, so the catalogue's specific claim ('defer_host_retry, "
        "credential health untouched') is unverified for this row. Unlike "
        "the other documented-evidence rows in this file, neither "
        "same-code-catalogue.md nor verdicts.jsonl cites a source for "
        "*how* RefreshTokenFailed reaches classify()/dispatch_binding (no "
        "exception class name, no error.code, no URL surface -- confirmed "
        "by grep across both files) -- so teaching a reading now would mean "
        "inventing the evidence shape rather than reading it, which the "
        "task's own discipline forbids. Left untouched for the coordinator "
        "to either cite a source or accept this stays unverified.",
        strict=True,
    )
    def test_codex_oauth_session_expired_defers_to_host_refresh(self):
        exc = Exception("RefreshTokenFailed")
        exc.status_code = 401
        engine = carousel.Carousel()
        binding = dispatch_binding.DispatchBinding(engine=engine)
        result = binding._on_failure("openai-codex:m", "synthetic", exc, "test", 1, False)
        assert result[:2] == ("raise", "auth_refresh")
        assert engine.snapshot() == {}

    @pytest.mark.xfail(
        reason="deliberate, see decisions/0005 (section 2, real-11, field "
        "`rest`): classify() staying out of a bare 401 'Invalid token "
        "(request id: ...)' is not a gap -- carousel.py's own "
        "CREDENTIAL_PROBLEM_INDICATORS/CREDENTIAL_PROBLEM_REST_S already "
        "reads this exact wording at the dispatch layer and deliberately "
        "gives it 20s, not the catalogue's 300s-then-3600s auth_dead "
        "escalation. The owner's own log refused the 300s number: 12 of 13 "
        "real occurrences of this refusal cured themselves in under 300s, "
        "so treating it as classify()-level auth_permanent here would "
        "retire keys the measured record shows recover on their own. The "
        "catalogue and the plugin disagree on purpose; left untouched.",
        strict=True,
    )
    def test_gateway_token_unknown_is_auth_dead(self):
        body = {"code": "", "message": "Invalid token (request id: abc123)", "type": "api_error"}
        verdict = classify(provider="custom", status_code=401,
                            error_message="Invalid token (request id: abc123)",
                            error_body=body, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth_permanent"

    def test_upstream_401_relayed_by_an_aggregator_is_not_charged_to_our_key(self):
        # Verified real shape (test_v1_7_0_7_explicit_upstream.py): the 401
        # text sits inside metadata/source=upstream, and reading it must
        # never retire our own (healthy) relay key.
        exc = Exception("Upstream request failed")
        exc.status_code = 401
        exc.body = {"error": {"type": "upstream_error", "source": "upstream",
                               "upstream_status": 401, "message": str(exc)}}
        engine = carousel.Carousel()
        binding = dispatch_binding.DispatchBinding(engine=engine)
        result = binding._on_failure("custom:m", "a", exc, "test", 1, False)
        assert result == ("raise", "upstream_error", 401)
        assert engine.rotations == 0


# ===========================================================================
# 4. HTTP 400 -- six conditions
# ===========================================================================


class TestSection4Http400:
    def test_provider_refuses_kames_own_prefill_rewrite(self):
        message = ("Gemini HTTP 400 (INVALID_ARGUMENT): Requests ending with a model turn "
                   "are not supported.")
        assert stitch.refuses_prefill(message) is True

    def test_malformed_transcript_surfaces_as_request_fault(self):
        message = "Please ensure that function call turn comes immediately after a user turn"
        verdict = classify(provider="gemini", status_code=400, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is None

    def test_unknown_parameter_surfaces_as_request_fault(self):
        message = "Unsupported parameter: 'temperture'"
        verdict = classify(provider="openai", status_code=400, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is None

    def test_invalid_api_key_on_a_400_is_auth_dead(self):
        message = "API key not valid. Please pass a valid API key."
        verdict = classify(provider="gemini", status_code=400, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth_permanent"

    def test_account_prerequisite_billing_disabled_is_billing(self):
        message = "This API method requires billing to be enabled."
        verdict = classify(provider="gemini", status_code=400, error_message=message,
                            error_body={"error": {"status": "FAILED_PRECONDITION", "message": message}},
                            now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_anthropic_spend_limit_reached_on_a_400_is_billing(self):
        message = "You have reached your specified API usage limits for this API."
        verdict = classify(provider="anthropic", status_code=400, error_message=message,
                            error_body={"error": {"type": "invalid_request_error", "message": message}},
                            now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_context_overflow_surfaces_as_request_fault(self):
        message = "This model's maximum context length is 8192 tokens."
        verdict = classify(provider="openai", status_code=400, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is None


# ===========================================================================
# 5. HTTP 403 -- seven conditions
# ===========================================================================


class TestSection5Http403:
    def test_this_key_may_not_use_this_model_is_a_short_denial(self):
        # _DENIAL_PATTERNS' `model[\s_-]*not[\s_-]*(?:authorized|available)`
        # wants the two words adjacent -- Hermes' own corpus is why: "not
        # found" (a model-name typo) was in this alternation once and had
        # to come back out, so the match stays narrow on purpose.
        message = "This API key: model not authorized for the requested model"
        verdict = classify(provider="gemini", status_code=403, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth"
        assert verdict.kind == "denied"

    def test_project_has_the_api_disabled_is_per_model_not_a_dead_key(self):
        # Verified real shape (test_v1_6_0_0.py).
        message = ("Generative Language API has not been used in project 123 "
                   "before or it is disabled.")
        verdict = classify(provider="gemini", status_code=403, error_message=message,
                            error_body={"error": {"status": "PERMISSION_DENIED",
                                                   "message": message}}, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth"
        assert verdict.quota_scope == QuotaScope.PER_MODEL

    def test_gateway_account_out_of_credit_is_billing(self):
        # 'insufficient_user_quota' is deliberately NOT a substring of
        # 'insufficient_quota' (test_v1_7_0_0.py documents this exact
        # non-collision) and has no row of its own in core/catalog.py, so
        # this row's family is *not* reached through the code field the
        # catalogue names -- it is reached through the prose
        # ("out of credit"/"credit balance"), which real one-api/new-api
        # gateway bodies carry alongside the code. Kept as a passing test
        # (the catalogued outcome is reached), not xfail, but the comment
        # is the finding: the code-based path itself is still a gap.
        body = {"error": {"code": "insufficient_user_quota",
                           "message": "You have run out of credit, "
                                      "remaining credit limit: $0.000000"}}
        verdict = classify(provider="custom", status_code=403,
                            error_message=body["error"]["message"], error_body=body, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_gateway_account_out_of_credit_is_billing_from_the_code_alone(self):
        body = {"error": {"code": "insufficient_user_quota", "message": "Request declined"}}
        verdict = classify(provider="custom", status_code=403,
                            error_message="Request declined", error_body=body, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_account_suspended_is_auth_dead(self):
        message = "Account suspended."
        verdict = classify(provider="custom", status_code=403, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth_permanent"

    def test_ip_allowlist_denial_is_about_the_network_not_the_key(self):
        message = "key(AIzaSy...q7R8) allowed only from approved IP ranges"
        verdict = classify(provider="gemini", status_code=403, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "auth"
        assert verdict.kind == "denied"

    def test_alibaba_free_allowance_exhausted_is_billing(self):
        # Verified real shape (test_providers.py) -- R17: this scoped
        # meaning has to survive any later window-sizing pass.
        message = "The free tier of the model has been exhausted"
        verdict = classify(provider="alibaba", status_code=403, error_message=message,
                            error_body={"code": "AllocationQuota.FreeTierOnly", "message": message},
                            now_epoch=NOW)
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_content_guardrail_block_surfaces_as_request_fault(self):
        body = {"error": {"code": "content_policy_violation", "message": "Your request was flagged"}}
        verdict = classify(provider="openrouter", status_code=403,
                            error_message="Your request was flagged", error_body=body, now_epoch=NOW)
        assert verdict is None


# ===========================================================================
# 6. HTTP 404 / 410 -- the credential is never the answer
# ===========================================================================


class TestSection6Http404And410:
    def test_model_not_found_is_request_fault_never_a_credential_verdict(self):
        message = "models/gemini-9 is not found for API version v1beta"
        verdict = classify(provider="gemini", status_code=404, error_message=message,
                            error_body={"error": {"status": "NOT_FOUND", "message": message}},
                            now_epoch=NOW)
        assert verdict is None

    def test_permanent_retirement_410_gone_is_request_fault(self):
        message = "This model has reached its end of life and is no longer available"
        verdict = classify(provider="custom", status_code=410, error_message=message,
                            error_body={"error": {"message": message}}, now_epoch=NOW)
        assert verdict is None


# ===========================================================================
# 7. The two cases where NO field separates the conditions
# ===========================================================================


class TestSection7NoFieldSeparatesTheConditions:
    """7.1 and 7.2: the catalogue's own point is that no field distinguishes
    the two real outcomes here, so the only thing worth asserting is the
    documented SAFE behaviour (short probe, no long blind hold) -- never a
    pretend distinction.
    """

    def test_7_1_a_perday_refusal_stays_probeable_never_an_immediate_blind_hour(self):
        # R09: the full hour is bought only after 20 minutes of total pool
        # silence; a lone PerDay refusal must not immediately commit to the
        # long end of the measured range (median 5.3h) nor trust the
        # stated retryDelay, which the record shows is flat 0-59s and
        # carries no information about the day counter.
        body = {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{
                            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                            "quotaValue": "20",
                        }],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3s"},
                ],
            }
        }
        verdict = classify(provider="gemini", status_code=429,
                            error_message="You exceeded your current quota.", error_body=body,
                            now_epoch=NOW)
        assert verdict is not None
        assert verdict.quota_window == QuotaWindow.PER_DAY
        # Never the stated 3 seconds, and never the multi-hour end of the
        # measured range on the very first refusal -- the re-probeable
        # default is what a lone occurrence gets.
        assert verdict.reset_at == pytest.approx(NOW + quota.DEFAULT_PER_DAY_BENCH_SECONDS, abs=1.0)

    def test_7_2_an_empty_429_gets_the_cheap_unsized_bench_not_an_hour(self):
        # R23: a throttle with nothing stated gets the cheapest number that
        # keeps the key in the carousel (30s -- 20s through 1.8.0.0, raised
        # to 45s in 1.8.0.1 on a curve that double-counted retries, and
        # corrected to 30s in 1.8.0.2 by recounting each retry once over the
        # whole real corpus), never the host's blind hour, and R10/R11:
        # repetition alone must never multiply it.
        #
        # classify() itself leaves an unknown-window throttle's reset_at
        # unset -- QuotaWindow.UNKNOWN has no entry in
        # quota._WINDOW_BENCH_DEFAULTS (unlike every named window), which is
        # exactly what hands sizing to R23's flat rest at the dispatch layer
        # rather than a window-shaped guess. What is checked here is the
        # verdict correctly carrying no size of its own, and the constant
        # that governs what happens next being the cheap one R23 names, not
        # the host's hour.
        assert quota.DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS == 30.0
        assert QuotaWindow.UNKNOWN not in quota._WINDOW_BENCH_DEFAULTS
        verdict = classify(provider="nvidia", status_code=429,
                            error_body={"status": 429, "title": "Too Many Requests"},
                            now_epoch=NOW)
        assert verdict is not None
        assert verdict.quota_window == QuotaWindow.UNKNOWN
        assert verdict.reset_at is None


# ===========================================================================
# 8. Statuses that carry no agreed meaning at all
# ===========================================================================


class TestSection8UnrecognisedStatuses:
    def test_an_unrecognised_status_gets_a_short_probe_never_an_escalation(self):
        # 418/498/529/421 and "any 4xx a table has never seen": the
        # expected behaviour is the unknown path -- short probe, never a
        # blind hour on the first sighting.
        verdict = classify(provider="custom", status_code=418, error_message="I'm a teapot",
                            error_body={"error": {"message": "I'm a teapot"}}, now_epoch=NOW)
        # Either classify stays out of it (None, host default applies) or it
        # reads as an ordinary unsized throttle -- what it must never do is
        # commit to the long end of the range on a status nobody has ever
        # catalogued.
        if verdict is not None:
            assert verdict.reset_at is None or verdict.reset_at <= NOW + 3600.0
