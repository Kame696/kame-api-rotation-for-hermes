"""1.6.0.0 — the plugin's job is not to avoid errors. It is to survive them.

Every release before this one was aimed at a defect. This one is aimed at a
contract, stated by the owner in the words the code now has to keep:

    the agent is uninterrupted. It never tries the same API twice in a row.
    It does not merely rotate, it rotates *intelligently*, picking the one
    most likely to be healthy. And it never stops — except when only one
    credential with any health is left. The agent should not stop because of
    errors.

Read as a test suite, that sentence has parts that can pass or fail, and this
file is where they do.

--------------------------------------------------------------------------
Part one: the evidence was never offered
--------------------------------------------------------------------------

Hermes does not reach Google through an OpenAI-shaped client. It has its own
adapter, ``agent/gemini_native_adapter``, and that adapter reads the response
body exactly once:

    gemini_http_error(response) -> GeminiAPIError(
        message, code="gemini_rate_limited", status_code=429,
        response=response, retry_after=...,
        details={"status": ..., "reason": ..., "metadata": ..., "message": ...},
    )

Two consequences, both measured on the owner's own journal — 225 Gemini
refusals over 13.9 days, **zero** catalogue hits, 184 decided by prose:

1. ``error_body`` arrives **empty**. The body was consumed; a streaming
   ``httpx.Response`` raises on a second read. So every reader in
   ``classify`` that searches a body found nothing, ``quotaId`` included.
2. ``error.code`` is **not a provider's word**. It is Hermes'
   (``gemini_rate_limited``), which no catalogue of provider vocabulary can
   ever contain.

Meanwhile ``details`` held ``RESOURCE_EXHAUSTED``, ``RATE_LIMIT_EXCEEDED``
and ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` — every one of
which the table has recognised since 1.2.0. The values were not missing. They
were never read.

The worst single consequence had nothing to do with silence. Google's
free-tier 429 says, word for word, OpenAI's out-of-credits sentence:

    "You exceeded your current quota, please check your plan and billing
    details."

``_AMBIGUOUS_BILLING_PATTERNS`` exists for exactly that sentence, and it is
settled by ``_names_a_wait`` — which was handed ``error_body``. Empty. So the
tiebreak lost, every time, and a twenty-one second per-minute throttle was
read as **billing**: the key benched a full day, at ``account`` scope so
every other model went down with it, with ``billing`` in
``probe.NEVER_PROBE_REASONS`` so the escape hatch could not fire and
``account`` in ``escalate.NEVER_STRETCH_WINDOWS`` so nothing learned from it.

For a pool that is 76% Gemini, that is the contract's "it never stops"
failing on the single commonest refusal there is.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1600_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_package()
plugin_settings = importlib.import_module(f"{PACKAGE}.settings")

catalog = importlib.import_module("hermes-kame-api-rotation.core.catalog")
classify_mod = importlib.import_module("hermes-kame-api-rotation.core.classify")
quota = importlib.import_module("hermes-kame-api-rotation.core.quota")
runtime = importlib.import_module("hermes-kame-api-rotation.runtime")
classify = classify_mod.classify

QuotaWindow = quota.QuotaWindow
QuotaScope = quota.QuotaScope


# --------------------------------------------------------------------------
# The payload, built the way the host builds it.
# --------------------------------------------------------------------------

class GeminiAPIError(Exception):
    """``agent/gemini_native_adapter.GeminiAPIError``, attribute for attribute.

    Deliberately not a mock. The point of every assertion below is that the
    plugin reads what this class actually carries, so the class has to carry
    what the host's does — including ``response``, which is the object whose
    unrepeatable read is the reason ``error_body`` is empty in the first
    place.
    """

    def __init__(self, message, *, code=None, status_code=None, response=None,
                 retry_after=None, details=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.response = response
        self.retry_after = retry_after
        self.details = details or {}


#: The sentence. Copied from Google, and identical to OpenAI's for an empty
#: balance apart from the trailing link.
FREE_TIER_SENTENCE = (
    "You exceeded your current quota, please check your plan and billing "
    "details. For more information on this error, head to: "
    "https://ai.google.dev/gemini-api/docs/rate-limits."
)


def gemini(status, *, code, error_status="", reason="", quota_limit=None,
           message="", retry_after=None):
    """One refusal, shaped exactly as ``gemini_http_error`` shapes it."""
    metadata = {"service": "generativelanguage.googleapis.com"}
    if quota_limit:
        metadata["quota_limit"] = quota_limit
        metadata["quota_limit_value"] = "15"
    return GeminiAPIError(
        f"Gemini HTTP {status} ({error_status or 'error'}): {message}",
        code=code,
        status_code=status,
        retry_after=retry_after,
        details={
            "status": error_status,
            "reason": reason,
            "metadata": metadata,
            "message": message,
        },
    )


def verdict_for(exc, *, provider="google", model="gemini-2.5-flash"):
    """Call the hook the way ``error_classifier`` calls it — body and all.

    ``error_body=None`` is not a shortcut. It is the fact this whole part of
    the release is about: by the time the hook runs, the body is gone.
    """
    return classify(
        provider=provider,
        model=model,
        status_code=exc.status_code,
        error_message=str(exc),
        error_body=None,
        headers={},
        error=exc,
        error_type=type(exc).__name__,
        error_code=exc.code,
    )


# --------------------------------------------------------------------------
# The contract, one clause at a time.
# --------------------------------------------------------------------------

def test_free_tier_throttle_is_not_read_as_billing():
    """The regression that cost the most, stated as the sentence that caused it.

    Before this release: ``billing``, ``account``/``account``, a day benched,
    no probe, no learning. After: a per-minute throttle on one model, sized
    from the counter Google named.
    """
    verdict = verdict_for(gemini(
        429, code="gemini_rate_limited", error_status="RESOURCE_EXHAUSTED",
        reason="RATE_LIMIT_EXCEEDED",
        quota_limit="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        message=FREE_TIER_SENTENCE,
    ))
    assert verdict is not None
    assert verdict.reason == "rate_limit"
    assert verdict.should_rotate_credential is True
    assert verdict.retryable is True
    assert verdict.quota_window == QuotaWindow.PER_MINUTE
    # A per-model counter says nothing about the other models this key reaches.
    assert verdict.quota_scope == QuotaScope.PER_MODEL


def test_a_catalogued_throttle_settles_the_ambiguous_sentence_by_itself():
    """Belt and braces: the field wins even with no counter to size it by.

    ``_names_a_wait`` needs the counter. This does not — it is the standing
    rule that a field outranks a sentence, applied to the one sentence the
    module already admits is ambiguous. Without it the fix would depend on
    Google always sending ``quota_limit``, and a payload that omitted it
    would fall straight back into a day-long billing bench.
    """
    verdict = verdict_for(gemini(
        429, code="gemini_rate_limited", error_status="RESOURCE_EXHAUSTED",
        reason="", quota_limit=None, message=FREE_TIER_SENTENCE,
    ))
    assert verdict is not None
    assert verdict.reason == "rate_limit"
    assert verdict.source == "catalog"


def test_an_unambiguous_billing_sentence_still_wins():
    """The gate above is narrow on purpose, and this is what keeps it narrow.

    "credit balance is too low" beside a throttle field is a real
    disagreement between two specific claims, not a known-ambiguous sentence.
    The more specific claim still wins, exactly as before — otherwise the
    gate would quietly turn every genuine depletion into an hourly retry.
    """
    verdict = verdict_for(gemini(
        429, code="gemini_rate_limited", error_status="RESOURCE_EXHAUSTED",
        reason="", message="Your credit balance is too low to access the API.",
    ))
    assert verdict is not None
    assert verdict.reason == "billing"


def test_the_daily_counter_is_read_as_a_day():
    """``QUOTA_EXCEEDED`` — the half of Google's pair the catalogue was missing.

    Sizing matters more here than anywhere: read as a minute, a key that is
    out until midnight is re-probed all day and every probe is a refusal.
    """
    verdict = verdict_for(gemini(
        429, code="gemini_rate_limited", error_status="RESOURCE_EXHAUSTED",
        reason="QUOTA_EXCEEDED",
        quota_limit="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        message=FREE_TIER_SENTENCE,
    ))
    assert verdict is not None
    assert verdict.reason == "rate_limit"
    assert verdict.quota_window == QuotaWindow.PER_DAY


def test_a_429_with_nothing_in_it_still_rotates():
    """The contract's hardest clause: never hand a spent key back.

    No ``ErrorInfo``, no metadata, no message — Gemini does answer this way.
    All that survives is Hermes' own invented code, which is why that code is
    catalogued. ``reset_at`` stays unset because nothing said how long, and
    inventing a deadline here is what the escalation ladder exists to avoid.
    """
    verdict = verdict_for(GeminiAPIError(
        "Gemini returned HTTP 429: ", code="gemini_rate_limited",
        status_code=429,
        details={"status": "", "reason": "", "metadata": {}, "message": ""},
    ))
    assert verdict is not None
    assert verdict.reason == "rate_limit"
    assert verdict.should_rotate_credential is True
    assert verdict.reset_at is None


def test_a_dead_key_is_named_dead_and_not_benched_for_an_hour():
    """``API_KEY_INVALID`` arrives only in ``details``. Nowhere else.

    Rediscovering a dead key once an hour, forever, is the failure mode the
    ordering in ``classify`` was written to prevent — and it ran anyway on
    every Gemini key, because the word that prevents it was in a field
    nothing read.
    """
    verdict = verdict_for(gemini(
        401, code="gemini_unauthorized", error_status="UNAUTHENTICATED",
        reason="API_KEY_INVALID",
        message="API key not valid. Please pass a valid API key.",
    ))
    assert verdict is not None
    assert verdict.reason == "auth_permanent"
    assert verdict.retryable is False
    assert verdict.should_rotate_credential is True


def test_a_request_the_pool_cannot_fix_is_left_to_the_host():
    """The other half of the contract, and the easier one to get wrong.

    A misspelt model name is not a credential problem. Claiming it would walk
    the entire pool over a typo and bench every key in it. ``None`` means the
    host decides, which is this module's standard for staying quiet.
    """
    for exc in (
        gemini(404, code="gemini_model_not_found", error_status="NOT_FOUND",
               message="models/gemini-9 is not found for API version v1beta"),
        gemini(500, code="gemini_http_500", error_status="INTERNAL",
               message="Internal error encountered."),
        gemini(503, code="gemini_http_503", error_status="UNAVAILABLE",
               message="The model is overloaded. Please try again later."),
    ):
        assert verdict_for(exc) is None, exc.code


def test_a_disabled_api_is_per_model_not_a_dead_key():
    verdict = verdict_for(gemini(
        403, code="gemini_http_403", error_status="PERMISSION_DENIED",
        reason="SERVICE_DISABLED",
        message=("Generative Language API has not been used in project 123 "
                 "before or it is disabled."),
    ))
    assert verdict is not None
    assert verdict.reason == "auth"
    assert verdict.quota_scope == QuotaScope.PER_MODEL


# --------------------------------------------------------------------------
# The reads themselves, pinned so a later refactor cannot quietly undo them.
# --------------------------------------------------------------------------

def test_structured_values_read_the_exception_details():
    exc = gemini(
        429, code="gemini_rate_limited", error_status="RESOURCE_EXHAUSTED",
        reason="RATE_LIMIT_EXCEEDED", message=FREE_TIER_SENTENCE,
    )
    values = classify_mod.structured_error_values(None, exc, exc.code)
    assert "RATE_LIMIT_EXCEEDED" in values
    assert "RESOURCE_EXHAUSTED" in values
    # Ordered by specificity: the ``ErrorInfo.reason`` names the exact fact,
    # so it has to outrank both the invented code and the family.
    assert values.index("RATE_LIMIT_EXCEEDED") < values.index("gemini_rate_limited")
    assert values.index("gemini_rate_limited") < values.index("RESOURCE_EXHAUSTED")


def test_a_quota_id_is_found_in_a_parsed_payload_not_only_in_raw_json():
    """``str(dict)`` uses single quotes. The old pattern required double.

    The field survived every hop and matched nothing at the last one.
    """
    raw = ('{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.'
           'QuotaFailure", "violations": [{"quotaId": '
           '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}')
    parsed = str({"metadata": {
        "quota_limit": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}})
    for text in (raw, parsed):
        window, scope = catalog.read_quota_id(text)
        assert window == QuotaWindow.PER_DAY, text[:40]
        assert scope == QuotaScope.PER_MODEL, text[:40]


def test_quota_limit_value_is_not_mistaken_for_the_identifier():
    """``quota_limit_value`` is the number, and it sits right beside the name."""
    window, _scope = catalog.read_quota_id(str({"quota_limit_value": "15"}))
    assert window == QuotaWindow.UNKNOWN


# --------------------------------------------------------------------------
# Markers carried back from the Agent Zero plugin.
# --------------------------------------------------------------------------

def test_a_counter_named_only_by_its_unit_and_window_is_still_a_counter():
    """"...your limit of 200000 tokens per day" — no "rate limit", no "quota".

    Three of the five markers audited out of the Agent Zero plugin were
    already covered here. These two were not, and they are the shape a
    refusal takes when the provider states the counter instead of naming the
    category.
    """
    for sentence in (
        "You have exceeded your limit of 200000 tokens per day",
        "Request rejected: limit of 40000 tokens per min reached",
        "No quota left for this key",
    ):
        assert classify_mod._matches(classify_mod._QUOTA_PATTERNS, sentence), sentence


# --------------------------------------------------------------------------
# Part two: the verdict that never arrived.
#
# The journal is unambiguous about this one. 295 benches over 13.9 days, and
# on every single one of them the pool entry's ``last_error_reset_at`` was
# unset — ``sized_by: kame`` a standing zero, 185 rows saying ``dropped``
# (KAME sized it, the number did not land) and 110 saying ``host``.
#
# The verdict travelled correctly right up to the last hop. KAME's hook
# returns ``error_context``; ``hermes_cli/plugins.py:6151`` keeps it;
# ``ClassifiedError.error_context`` holds it. Then
# ``agent/conversation_loop.py:4280`` rebuilds the context from the raw
# exception and passes *that* to the pool, so the classifier's own field is
# read by nobody.
#
# These pin the two hand-offs that make the deadline land. The end-to-end
# proof — against the host's real ``CredentialPool``, its real
# ``extract_api_error_context`` and its real ``GeminiAPIError`` — is
# ``tools/sandbox_binding.py`` sections 18b–18d and 10b.
# --------------------------------------------------------------------------

MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"
NOW = 1_700_000_000.0


def _clear():
    runtime.forget_judgement()
    runtime.forget_bench_model()
    runtime.forget_call()


def test_a_verdict_can_be_read_twice_by_the_two_halves_that_need_it():
    """One writes the bench, the other records it. Only the second claims.

    Before this there was a single reader. Adding the writer with
    ``take_judgement`` would have made every bench KAME sized report itself
    as ``sized_by: host`` — the plugin doing the right thing and filing a row
    saying it had not.
    """
    _clear()
    runtime.note_judgement(
        "gemini", MAIN, window="per_minute", source="catalog",
        reset_at=NOW + 21.0, now=NOW, scope="per_model",
    )
    peeked = runtime.peek_judgement("gemini", MAIN, now=NOW)
    assert peeked is not None and peeked.reset_at == NOW + 21.0
    # Still there for the recorder.
    assert runtime.peek_judgement("gemini", MAIN, now=NOW) is not None
    taken = runtime.take_judgement("gemini", MAIN, now=NOW)
    assert taken is not None
    # And gone afterwards, so a second key failing in the same turn is
    # recorded as itself rather than inheriting the first one's reasoning.
    assert runtime.peek_judgement("gemini", MAIN, now=NOW) is None
    _clear()


def test_a_stale_verdict_is_not_read_by_either_half():
    _clear()
    runtime.note_judgement(
        "gemini", MAIN, window="per_minute", source="catalog",
        reset_at=NOW + 21.0, now=NOW, scope="per_model",
    )
    stale = NOW + runtime.JUDGEMENT_TTL_SECONDS + 1.0
    assert runtime.peek_judgement("gemini", MAIN, now=stale) is None
    _clear()


def test_the_bench_model_hint_only_answers_for_its_own_provider_and_window():
    """The auxiliary lane benches after its own call has already unwound.

    ``scoped_call`` restores the conversation's model on the way out — which
    is right, and which is why the attribution needs saying separately rather
    than by holding the announcement open past its call.
    """
    _clear()
    runtime.note_call("gemini", MAIN)
    assert runtime.bench_model_for("gemini", now=NOW) == ""

    runtime.note_bench_model("gemini", AUX, now=NOW)
    assert runtime.bench_model_for("gemini", now=NOW) == AUX
    # A different pool must never see it.
    assert runtime.bench_model_for("nvidia", now=NOW) == ""
    # And it expires on the same clock as a verdict.
    assert runtime.bench_model_for(
        "gemini", now=NOW + runtime.JUDGEMENT_TTL_SECONDS + 1.0
    ) == ""
    _clear()


# --------------------------------------------------------------------------
# Part three: staying on the model that was asked for.
#
# The owner's request, in their words: *o KAME deveria ter opção nas
# configurações de forma nativa não cair em fallback (para outro provedor)* —
# because the point of the pool is to wait a quota out and come back on the
# same model, the way the Agent Zero plugin does, rather than to answer with
# a different model and let the user notice afterwards.
#
# ``should_fallback`` is the field that grants Hermes that permission, and
# ``Verdict`` defaults it to ``True`` on purpose: the host builds
# ``ClassifiedError(**plugin_result)``, whose own default is ``False``, so a
# hint this plugin leaves out is a hint it silently turns off. The switch has
# to remove the permission deliberately, in one place, and nowhere else.
# --------------------------------------------------------------------------

@pytest.fixture
def _no_fallback(monkeypatch):
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    plugin_settings.forget()
    yield
    plugin_settings.forget()


def _hint(verdict):
    return plugin._to_hook_result(verdict)


def test_fallback_is_permitted_by_default():
    verdict = verdict_for(gemini(
        429, code="gemini_rate_limited", error_status="RESOURCE_EXHAUSTED",
        reason="RATE_LIMIT_EXCEEDED", message=FREE_TIER_SENTENCE,
    ))
    assert _hint(verdict)["should_fallback"] is True


def test_the_switch_takes_the_permission_away(_no_fallback):
    verdict = verdict_for(gemini(
        429, code="gemini_rate_limited", error_status="RESOURCE_EXHAUSTED",
        reason="RATE_LIMIT_EXCEEDED",
        quota_limit="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        message=FREE_TIER_SENTENCE,
    ))
    hint = _hint(verdict)
    assert hint["should_fallback"] is False
    # And takes away nothing else. Rotation is the recovery this plugin is
    # for; a switch that stopped the pool from rotating while it waited would
    # be the opposite of what was asked for.
    assert hint["should_rotate_credential"] is True
    assert hint["retryable"] is True
    assert hint["error_context"]["reset_at"] == verdict.reset_at


def test_a_dead_credential_still_does_not_ask_for_a_fallback(_no_fallback):
    # The switch only ever removes permission. A verdict that never granted
    # it must read the same either way, or the setting would look like it was
    # doing something on a path it does not touch.
    verdict = verdict_for(gemini(
        401, code="gemini_unauthorized", error_status="UNAUTHENTICATED",
        reason="API_KEY_INVALID", message="API key not valid.",
    ))
    assert _hint(verdict)["should_fallback"] is False


def test_the_switch_is_off_unless_it_is_asked_for():
    # Falling back is Hermes' own behaviour, and this plugin does not get to
    # decide nobody wants it.
    plugin_settings.forget()
    assert plugin_settings.is_on(plugin_settings.NO_MODEL_FALLBACK) is False
    assert plugin_settings.NO_MODEL_FALLBACK in plugin_settings.ALL_FLAGS
    # Not an escape hatch: it adds a behaviour rather than taking one away,
    # so it does not belong on the shelf that warns about taking things away.
    assert plugin_settings.NO_MODEL_FALLBACK not in plugin_settings.DISABLE_FLAGS
    assert plugin_settings.group_of(plugin_settings.NO_MODEL_FALLBACK) == "extra"


def test_the_help_text_admits_the_lane_it_cannot_reach():
    # The auxiliary lane fires no classification hook, so no verdict of
    # KAME's reaches it and this switch cannot govern it. Saying so in the
    # panel is the difference between a documented limit and a bug report.
    explanation = plugin_settings.explain(plugin_settings.NO_MODEL_FALLBACK).lower()
    assert "titling" in explanation or "summaris" in explanation


# --------------------------------------------------------------------------
# Whose bench is it? The note the auxiliary lane leaves, and when it stops
# being true.
#
# Found by running ``tools/live_429.py`` against the installed build: a sole
# key benched for a daily cap was offered again the instant its bench was
# written. The bench was real; it had simply been filed under the auxiliary
# model, because a titling call had failed seconds earlier and the note that
# says which model an auxiliary bench belongs to was still standing. Per-model
# release then did exactly what it is for, and handed the key back to the
# model that had just spent it.
# --------------------------------------------------------------------------

_plugin_runtime = importlib.import_module(f"{PACKAGE}.runtime")


def test_a_main_lane_refusal_ends_the_auxiliary_lane_note():
    _plugin_runtime.forget_bench_model()
    _plugin_runtime.note_bench_model("gemini", AUX, now=NOW)
    assert _plugin_runtime.bench_model_for("gemini", now=NOW) == AUX

    sized = plugin._on_api_error_classification(
        provider="gemini",
        model=MAIN,
        status_code=429,
        error_message=FREE_TIER_SENTENCE,
        error_body=None,
        error=gemini(
            429,
            code="gemini_rate_limited",
            error_status="RESOURCE_EXHAUSTED",
            reason="RATE_LIMIT_EXCEEDED",
            quota_limit="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            message=FREE_TIER_SENTENCE,
            retry_after=21.0,
        ),
        error_type="GeminiAPIError",
        error_code="gemini_rate_limited",
    )
    # Sized, not declined — otherwise this would be the same proof as the
    # test below and would say nothing about the path that writes a deadline.
    assert sized is not None
    # The bench about to be written belongs to the conversation, so the note
    # from the call that already ended must not decide where it is filed.
    assert _plugin_runtime.bench_model_for("gemini", now=NOW) == ""
    _plugin_runtime.forget_bench_model()


def test_the_note_ends_even_when_the_classifier_declines():
    # Declining is the common path, and the host benches on it just the same.
    # A note that survived a declined refusal would misfile exactly the
    # benches KAME had no opinion about.
    _plugin_runtime.forget_bench_model()
    _plugin_runtime.note_bench_model("gemini", AUX, now=NOW)
    verdict = plugin._on_api_error_classification(
        provider="gemini",
        model=MAIN,
        status_code=418,
        error_message="I am a teapot",
        error_body=None,
        error=None,
    )
    assert verdict is None
    assert _plugin_runtime.bench_model_for("gemini", now=NOW) == ""
    _plugin_runtime.forget_bench_model()


def test_the_note_survives_when_no_main_lane_refusal_intervenes():
    # The other half of the same contract: the note exists because the host
    # benches an auxiliary refusal after that call has unwound, and nothing
    # about this change may shorten the gap it was written to cross.
    _plugin_runtime.forget_bench_model()
    _plugin_runtime.note_bench_model("gemini", AUX, now=NOW)
    assert _plugin_runtime.bench_model_for("gemini", now=NOW + 1.0) == AUX
    _plugin_runtime.forget_bench_model()


# --------------------------------------------------------------------------
# Gap 12 and gap 13: what the provider said, and what kept going wrong.
#
# The journal recorded KAME's conclusion and nothing able to contradict it, so
# a confident wrong verdict and a confident right one were the same row. And
# the fortnight tally grouped by the window KAME concluded, which cannot
# answer the question an owner asks first — what keeps happening, and is
# waiting even the right answer to it.
# --------------------------------------------------------------------------

journal_mod = importlib.import_module("hermes-kame-api-rotation.core.journal")
report_mod = importlib.import_module("hermes-kame-api-rotation.core.report")
stated_window = classify_mod.stated_window

BLOCK_AT = 1_700_000_000.0


def _block(**overrides):
    fields = dict(
        at=BLOCK_AT,
        provider="gemini",
        model=MAIN,
        credential_id="key-1",
        status_code=429,
        window="per_day",
        source="catalog",
        reason="rate_limit",
    )
    fields.update(overrides)
    return journal_mod.Block(**fields)


def _journal(blocks):
    return journal_mod.Journal.from_dict(
        {"version": 1, "blocks": [block.to_dict() for block in blocks], "recoveries": []}
    )


def test_the_provider_s_own_counter_is_read_from_where_the_body_is_not():
    # The whole reason this is separate from the verdict: for Gemini the body
    # is spent by the time anything here runs, and the counter survives only
    # on the exception's details.
    exc = gemini(
        429,
        code="gemini_rate_limited",
        error_status="RESOURCE_EXHAUSTED",
        reason="RATE_LIMIT_EXCEEDED",
        quota_limit="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        message=FREE_TIER_SENTENCE,
    )
    assert stated_window(error_body=None, error=exc) == QuotaWindow.PER_MINUTE


def test_a_provider_that_names_no_counter_states_nothing():
    # Silence, not agreement. Reporting "unknown" as a match would make every
    # provider that says nothing look like a provider KAME reads perfectly.
    exc = gemini(429, code="gemini_rate_limited", message="Too Many Requests")
    assert stated_window(error_body=None, error=exc) == QuotaWindow.UNKNOWN
    assert stated_window(error_body=None, error=None) == QuotaWindow.UNKNOWN


def test_a_contradiction_needs_both_halves_to_be_known():
    assert _block(window="per_day", stated_window="per_minute").contradicted is True
    assert _block(window="per_day", stated_window="per_day").contradicted is False
    # Neither of these is a disagreement: one side said nothing.
    assert _block(window="per_day", stated_window="unknown").contradicted is False
    assert _block(window="unknown", stated_window="per_day").contradicted is False


def test_a_row_written_before_this_release_reads_back_as_unknown():
    old = _block().to_dict()
    del old["stated_window"]
    restored = journal_mod.Block.from_dict(old)
    assert restored.stated_window == "unknown"
    assert restored.contradicted is False
    # And a row written now survives the round trip it will actually take.
    fresh = journal_mod.Block.from_dict(_block(stated_window="per_minute").to_dict())
    assert fresh.stated_window == "per_minute"


def test_the_reason_decides_the_kind_before_the_window_does():
    # A 429 wearing an auth failure is an auth failure. Calling it a throttle
    # because of the status it arrived on is the mistake the whole plugin
    # exists to stop making.
    assert journal_mod.describe_kind(
        _block(reason="auth_permanent", window="per_minute")
    ) == "credential rejected"
    assert journal_mod.describe_kind(
        _block(reason="billing", window="per_minute")
    ) == "out of credits"
    assert journal_mod.describe_kind(
        _block(reason="rate_limit", window="per_day")
    ) == "daily cap"
    assert journal_mod.describe_kind(
        _block(reason="rate_limit", window="unknown")
    ) == "rate limit, window unread"


def test_the_kinds_are_counted_with_what_kame_made_of_them():
    book = _journal([
        _block(window="per_day", stated_window="per_minute",
               sized_by=journal_mod.SIZED_BY_KAME),
        _block(window="per_day", stated_window="per_minute"),
        _block(window="per_day", stated_window="per_day",
               sized_by=journal_mod.SIZED_BY_KAME),
        _block(reason="auth_permanent", window="unknown"),
    ])
    rows = {row.kind: row for row in journal_mod.count_kinds(book, now=BLOCK_AT + 1.0)}

    daily = rows["daily cap"]
    assert daily.blocks == 3
    assert daily.kame_sized == 2
    # Three rows carried a stated counter; two of them named another window.
    assert daily.stated == 3
    assert daily.contradicted == 2
    assert daily.needs_a_person is False

    rejected = rows["credential rejected"]
    assert rejected.blocks == 1
    assert rejected.needs_a_person is True


def test_a_provider_is_never_printed_twice():
    # Found rendering the owner's real journal: ordering on the row count
    # alone lets one provider's busiest kind outrank another's while its
    # quieter kinds fall below, and the section then prints a heading twice.
    book = _journal(
        [_block(provider="gemini", window="per_day") for _ in range(10)]
        + [_block(provider="nvidia", window="per_minute") for _ in range(5)]
        + [_block(provider="gemini", window="per_minute") for _ in range(2)]
    )
    rows = journal_mod.count_kinds(book, now=BLOCK_AT + 1.0)
    order = [row.provider for row in rows]
    assert order == ["gemini", "gemini", "nvidia"]
    # And the busier provider leads, with its own busiest kind first.
    assert rows[0].kind == "daily cap"


def test_the_section_says_nothing_when_there_is_nothing_to_say():
    assert report_mod.render_kinds([]) == []


def test_the_section_carries_counts_and_never_provider_text():
    tag = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    book = _journal([
        _block(window="per_day", stated_window="per_minute"),
        _block(reason="billing", window="unknown"),
    ])
    text = "\n".join(report_mod.render_kinds(
        journal_mod.count_kinds(book, now=BLOCK_AT + 1.0)
    ))
    assert "daily cap" in text
    assert "the provider named another window on 1 of 1" in text
    assert "waiting does not fix this one" in text
    # The identifier is deliberately never carried past the classifier, so it
    # cannot appear here even by accident.
    assert tag not in text
    assert FREE_TIER_SENTENCE not in text
