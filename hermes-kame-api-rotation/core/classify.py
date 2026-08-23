"""Turning one API failure into a verdict — or into silence.

There is no list of supported providers in this module. The earlier draft had
one, and it was a mistake worth naming: an allowlist silently does nothing
for every provider not on it, including every provider that does not exist
yet. The rule here is instead **evidence, not identity** — KAME answers when
the response carries something the host's own classifier does not read, and
declines otherwise.

Declining is the common case and the safe one. The host has a competent
classifier; overriding it with a guess is strictly worse than staying quiet.
Every path that cannot point at concrete evidence returns ``None``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from .quota import (
    DEFAULT_ACCOUNT_BENCH_SECONDS,
    DEFAULT_PER_DAY_BENCH_SECONDS,
    QuotaScope,
    QuotaWindow,
    ResetDecision,
    compute_reset_at,
    detect_quota_window,
    extract_retry_delay_seconds,
)

# ── permanent conditions ──────────────────────────────────────────────────
# A credential in one of these states will not recover on a timer. Getting
# this wrong in the safe direction (treating a dead key as merely throttled)
# costs a wasted call per cooldown; getting it wrong in the other direction
# discards a working credential, so the patterns stay narrow and specific.

_PERMANENT_AUTH_PATTERNS = (
    re.compile(r"api[\s_-]*key[\s_-]*not[\s_-]*valid", re.I),
    re.compile(r"invalid[\s_-]*(?:api[\s_-]*)?key", re.I),
    re.compile(r"(?:api[\s_-]*)?key[\s_-]*(?:has[\s_-]*been[\s_-]*)?(?:expired|revoked|deleted|disabled)", re.I),
    re.compile(r"invalid[\s_-]*authentication", re.I),
    re.compile(r"incorrect[\s_-]*api[\s_-]*key", re.I),
    # The same fact with the words the other way round, which is how two of
    # the six providers checked actually phrase it — Anthropic sends
    # "API key is invalid." and DeepSeek "Your api key: ****0000 is invalid".
    # Every pattern above reads "invalid key"; neither of those says that, so
    # a genuinely dead key on either provider was handed back to the host as
    # a bare 401 and rotated forever instead of being retired. The gap is
    # bounded rather than open: the key and the verdict have to be in the
    # same clause, so a sentence that merely mentions a key somewhere and
    # the word invalid somewhere else does not qualify. The gap is 24
    # characters because the widest real one is twelve — DeepSeek's redacted
    # `: ****0000 ` — and every character of slack past the evidence is a
    # sentence about something else that this pattern can reach into.
    re.compile(r"(?:api[\s_-]*)?key\b[^.\n]{0,24}?\b(?:is|was)\s+(?:no[\s_-]*longer[\s_-]*valid|not[\s_-]*valid|invalid)", re.I),
)
# `unauthorized` was in that list and is not any more. It is the HTTP reason
# phrase for 401, so it arrives on every bare 401 a proxy, a gateway or an
# expired OAuth token produces — and reading it as "this key is not a key"
# retires a healthy credential over a refresh that was going to succeed. It
# also contradicted step 4 below, which is written precisely to leave an
# unadorned 401 to the host. Hermes' own corpus fails on it
# (``test_401_classified_as_auth``: the host says ``auth``, KAME said
# ``auth_permanent``), and nothing in this project's tests ever exercised it.
# A provider that has genuinely retired a key always says more than
# "Unauthorized", and each of those sentences is matched above.

# A 403-class refusal: the provider is deliberately refusing this credential
# for this model. Not a throttle and not a bad key — a suspended project, an
# API never enabled, a model outside the key's tier. None of it clears in
# twenty seconds, but a human can fix all of it, so these are benched for
# re-probing rather than marked permanently dead.
_DENIAL_PATTERNS = (
    re.compile(r"permission[\s_-]*denied", re.I),
    re.compile(r"denied[\s_-]*access", re.I),
    re.compile(r"consumer[\s_-]*suspended", re.I),
    re.compile(r"service[\s_-]*disabled", re.I),
    re.compile(r"api[\s_-]*key[\s_-]*service[\s_-]*blocked", re.I),
    re.compile(r"has[\s_-]*not[\s_-]*been[\s_-]*used[\s_-]*in[\s_-]*project", re.I),
    re.compile(r"is[\s_-]*disabled[\s_-]*for[\s_-]*this[\s_-]*project", re.I),
    # `not authorized` and `not available` are about this key: a model outside
    # the tier the key pays for. `not found` is not — it is about the model
    # name, and it was in this alternation until Hermes' own corpus failed on
    # it (``test_404_model_not_found``: the host says ``model_not_found``, try
    # a different model, do not touch the credential; KAME said ``auth`` and
    # rotated). With several keys that answer walks the whole pool over a
    # misspelt model name and benches every one of them for an hour.
    re.compile(r"model[\s_-]*not[\s_-]*(?:authorized|available)", re.I),
    # Z.AI 1311: "Your current subscription plan does not yet include access
    # to ${model_name}". Per-model by construction — the plan is fine, this
    # one model is outside it — which is what this verdict's scope already
    # says, so the key stays usable for everything else in the pool.
    re.compile(r"does[\s_-]*not[\s_-]*(?:yet[\s_-]*)?include[\s_-]*access[\s_-]*to", re.I),
)

# Decisive on their own: none of these is ever a throttle wearing a
# depletion's words. An empty balance does not refill by waiting, so reading
# one of these is enough to stop asking questions.
_BILLING_PATTERNS = (
    re.compile(r"billing\b[\w\s_-]{0,24}?\b(?:enabled|required|disabled|suspended)", re.I),
    re.compile(r"free[\s_-]*tier.*not[\s_-]*available", re.I),
    re.compile(r"consumer[\s_-]*(?:backend|quota).*disabled", re.I),
    re.compile(r"credit[\s_-]*balance[\s_-]*is[\s_-]*too[\s_-]*low", re.I),
    re.compile(r"insufficient[\s_-]*(?:quota|credits?|funds?|balance)", re.I),
    re.compile(r"(?:out[\s_-]*of|no)[\s_-]*credits?", re.I),
    re.compile(r"payment[\s_-]*required", re.I),
    # Alibaba Model Studio, `AllocationQuota.FreeTierOnly`: "The free tier of
    # the model has been exhausted" — and it arrives on a **403**, the status
    # step 4 hands straight back to the host. Without this the one refusal
    # that means "this key's free allowance is gone for good" was the one
    # refusal KAME said nothing about.
    re.compile(r"free[\s_-]*tier\b[^.\n]{0,40}?\bexhausted", re.I),
    # Hugging Face Inference Providers: "You have exceeded your monthly
    # included credits for Inference Providers". A monthly allowance, not a
    # throttle — an hour's re-probe achieves nothing and the daily one is the
    # right cadence for a human topping it up.
    re.compile(r"included[\s_-]*credits", re.I),
    # Alibaba `Arrearage`, on a 400: "Access denied, please make sure your
    # account is in good standing". The polite phrasing for an unpaid bill.
    re.compile(r"account[\s_-]*is[\s_-]*in[\s_-]*good[\s_-]*standing", re.I),
    # Z.AI 1309/1314: a coding or enterprise package that has run out. Billing
    # rather than denial because it is account-wide and a human fixes it with
    # money, which is exactly the daily-re-probe case.
    re.compile(r"(?:package|plan|subscription)\b[^.\n]{0,40}?\bhas[\s_-]*expired", re.I),
)

# **Shared wording, opposite meanings.** Google's current free-tier 429 says,
# word for word, OpenAI's out-of-credits sentence — with a rate-limit docs
# link appended — and it says it for a twenty-one second per-minute throttle.
# Read as billing, the key is benched a day, at ``account`` scope so every
# other model goes down with it, with ``billing`` in
# ``probe.NEVER_PROBE_REASONS`` so the escape hatch for a wrong long deadline
# cannot fire, and ``account`` in ``escalate.NEVER_STRETCH_WINDOWS`` so
# nothing learns from it either. Four failures pointing the same way, from
# one sentence.
#
# This is the second time this exact sentence has cost a version. ``quota.py``
# keeps it out of ``_ACCOUNT_MARKERS`` for the same reason; the lesson was
# never carried across to here.
_AMBIGUOUS_BILLING_PATTERNS = (
    re.compile(r"exceeded your current quota.*billing", re.I | re.S),
)

# A counter belonging to *this credential* is spent. Rotating to another key
# helps.
_QUOTA_PATTERNS = (
    re.compile(r"rate[\s_-]*limit", re.I),
    re.compile(r"too[\s_-]*many[\s_-]*requests", re.I),
    re.compile(r"quota[\s_-]*exceeded", re.I),
    re.compile(r"resource[\s_-]*exhausted", re.I),
    re.compile(r"exceeded[\s_-]*your[\s_-]*current[\s_-]*quota", re.I),
    # Alibaba's whole 429 family is spelled with this one word and never with
    # "rate limit": `Throttling`, `Throttling.RateQuota`, `Throttling.BurstRate`,
    # `Throttling.AllocationQuota`. The word arrives in the error *code*, which
    # is why it is worth matching as a bare stem.
    re.compile(r"throttl", re.I),
    # Z.AI 1308/1310, whose sentences say "Usage limit reached for ..." and
    # "Weekly/Monthly Limit Exhausted" — a spent counter described without
    # either of the two phrases every pattern above depends on.
    re.compile(r"usage[\s_-]*limit[\s_-]*reached", re.I),
    re.compile(r"limit[\s_-]*exhausted", re.I),
    # A concurrency ceiling is a per-credential counter like any other: the
    # next key has its own. Named by Kimi and MiniMax alongside RPM and TPM.
    re.compile(r"concurren\w*[\s_-]*limit", re.I),
)

# The provider is busy. Nothing is wrong with the credential, so the cooldown
# should be the wait the provider asked for and nothing more. Kept apart from
# the quota patterns because Anthropic's 529 says "Overloaded": folding the
# two together made a server-side hiccup look like a spent key.
_BUSY_PATTERNS = (
    re.compile(r"overloaded", re.I),
    re.compile(r"(?:over|at|exceeded)[\s_-]*capacity", re.I),
    re.compile(r"(?:server|service|model)[\s_-]*(?:is[\s_-]*)?(?:busy|unavailable)", re.I),
)


# ── the structured type, when it disagrees with the sentence ──────────────
# Providers keep two descriptions of the same failure: a machine-readable
# type or code, and a sentence they rewrite for humans. When those two
# disagree, the type is the one to believe — and they do disagree, in the
# direction that costs the most.
#
# Kimi's coding endpoint answers a rate limit with HTTP 429,
# ``error.type: "rate_limit_error"``, and the message "The engine is
# currently overloaded, please try again later." Read as congestion, KAME
# declines and does not rotate — which is correct for congestion and is a
# loop for a throttle, because the next key would have worked. It is
# reported in the wild as exactly that loop.
#
# This is the same class of defect ``_AMBIGUOUS_BILLING_PATTERNS`` above
# exists for: one sentence, two meanings, and the payload containing the
# tiebreak all along. Both fixes have the same shape — do not decide from
# prose when the provider also stated it in a field.
#
# The rule leaves the case ``_BUSY_PATTERNS`` was written for untouched:
# Anthropic's 529 carries ``type: "overloaded_error"``, so its field and its
# sentence agree and nothing changes.
_TYPE_THROTTLE = re.compile(
    r"rate[_\s-]*limit|too[_\s-]*many[_\s-]*requests|throttl|"
    r"resource[_\s-]*exhausted|quota[_\s-]*exceeded",
    re.I,
)

#: Read from the same fields, and the reason the override is not simply
#: "trust any type": a type that says overloaded *confirms* the prose, and
#: confirming evidence must not be able to flip the verdict.
_TYPE_BUSY = re.compile(r"overload|capacity|unavailable|server[_\s-]*error", re.I)

#: Where a structured type or code actually lives. Deliberately a fixed list
#: of paths rather than a walk of the whole body: a free-text field somewhere
#: deep that happens to contain "rate limit" is prose, and treating it as a
#: structured field would give the override the very weakness it exists to
#: correct.
def structured_error_tokens(body: Any, error: Any = None) -> str:
    """Collect the machine-readable type/code strings from a failure.

    Only strings are taken. OpenRouter's ``error.code`` is the integer 429
    and says nothing a status code has not already said; Alibaba's is
    ``"Throttling.RateQuota"`` and says a great deal.
    """
    found: list[str] = []

    def take(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            found.append(value)

    if isinstance(body, dict):
        take(body.get("type"))
        take(body.get("code"))
        inner = body.get("error")
        if isinstance(inner, dict):
            take(inner.get("type"))
            take(inner.get("code"))
            metadata = inner.get("metadata")
            if isinstance(metadata, dict):
                take(metadata.get("error_type"))
                take(metadata.get("provider_code"))
    if error is not None:
        for attribute in ("type", "code"):
            try:
                take(getattr(error, attribute, None))
            except Exception:  # pragma: no cover - hostile attribute access
                pass
    return " ".join(found)

# How long to bench a credential the provider is refusing outright. Long
# enough that a dead key stops costing a round trip on every turn, short
# enough that fixing the project brings it back within the hour.
DENIAL_BENCH_SECONDS = DEFAULT_PER_DAY_BENCH_SECONDS

_RATE_LIMIT_STATUSES = frozenset({429})
_SERVER_STATUSES = frozenset({500, 502, 503, 504, 529})
_AUTH_STATUSES = frozenset({400, 401})
_DENIAL_STATUSES = frozenset({403})


# An aggregator (OpenRouter and friends) answers with its own envelope and
# the real upstream error nested inside `metadata.raw`. The envelope says
# nothing about *our* credential — the upstream model is what failed — so
# every signal inside it belongs to someone else's key.
_WRAPPER_MESSAGE = re.compile(r"^provider returned (?:an? )?error", re.I)


def looks_like_upstream_wrapper(body: Any) -> bool:
    """Is this an aggregator relaying somebody else's failure?

    Matched by shape rather than by aggregator name, same as everything else
    here. Two independent signals: the envelope phrase, and metadata carrying
    a nested raw error or an upstream provider name.
    """
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    if _WRAPPER_MESSAGE.match(str(error.get("message") or "").strip()):
        return True
    metadata = error.get("metadata")
    return isinstance(metadata, dict) and ("raw" in metadata or "provider_name" in metadata)


def _matches(patterns, *texts: str) -> bool:
    haystack = " ".join(t for t in texts if t)
    if not haystack:
        return False
    return any(pattern.search(haystack) for pattern in patterns)


def _names_a_wait(
    *,
    message: str,
    body: Any,
    body_text: str,
    headers: Any,
    error: Any,
    now: float,
) -> bool:
    """Does this failure say when to come back, or which counter blew?

    Either answer settles the ambiguous sentence above, and it settles it by
    evidence rather than by provider name — which matters, because the same
    sentence arrives from a provider that means it and from one that does
    not, and a rule keyed on identity would be wrong for the next one.

    * **A retry delay.** Being told to wait twenty-one seconds is not being
      told the balance is empty. A depletion has nothing to wait for, so a
      provider that names a delay has just said this is a wait.
    * **A named counter.** ``GenerateRequestsPerMinutePerProjectPerModel``
      names a *rate*; a balance has no window. Anything narrower than
      ``account`` is therefore a throttle by the provider's own labelling.

    ``account`` and ``unknown`` are deliberately not enough: the first *is*
    the depletion reading, and the second is the absence of evidence, which
    can never be the thing that overturns a match.
    """
    window = detect_quota_window(message, body)
    if window not in (QuotaWindow.UNKNOWN, QuotaWindow.ACCOUNT):
        return True
    delay, _source = extract_retry_delay_seconds(
        message=message, body=body, headers=headers, error=error, now_epoch=now
    )
    if delay is not None:
        return True
    # Last resort, and only for the flattened body: a ``retryDelay`` key whose
    # value the cascade could not parse still says a wait exists. The reading
    # that follows will fall back to the window's own default, which is the
    # right answer for a throttle and the wrong one for a depletion.
    return bool(_RETRY_INFO.search(body_text))


# The shape of "come back later", for when the value itself is unparseable.
_RETRY_INFO = re.compile(r"retry[\s_-]*(?:info|delay|after)", re.I)


def _body_text(body: Any, limit: int = 20000) -> str:
    """Flatten a body to searchable text, bounded.

    Bounded because the body is arbitrary remote JSON and this runs on the
    host's error path: a megabyte of nested detail must not become a
    megabyte of regex work.
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body[:limit]
    try:
        return str(body)[:limit]
    except Exception:
        return ""


class Verdict:
    """What KAME believes about one failure, and why.

    ``should_fallback`` defaults to ``True`` and that default is load-bearing.
    Hermes builds its result with ``ClassifiedError(**plugin_result)``, where
    the field defaults to ``False`` — so every hint this plugin leaves out is
    a hint it silently turns *off*. Hermes sets it on every `rate_limit`,
    `billing` and `auth` it produces; omitting it would have quietly disabled
    model fallback on exactly the errors this plugin exists to handle.
    """

    __slots__ = (
        "reason", "retryable", "should_rotate_credential", "should_fallback",
        "reset_at", "quota_window", "quota_scope", "source", "rationale",
    )

    def __init__(
        self,
        *,
        reason: str,
        retryable: bool,
        should_rotate_credential: bool,
        should_fallback: bool = True,
        reset_at: Optional[float] = None,
        quota_window: str = QuotaWindow.UNKNOWN,
        quota_scope: str = QuotaScope.UNKNOWN,
        source: str = "",
        rationale: str = "",
    ) -> None:
        self.reason = reason
        self.retryable = retryable
        self.should_rotate_credential = should_rotate_credential
        self.should_fallback = should_fallback
        self.reset_at = reset_at
        self.quota_window = quota_window
        # Never leaves this plugin. Hermes' ``ClassifiedError`` has no field
        # for it, so it travels to the pool binding and stops there.
        self.quota_scope = quota_scope
        self.source = source
        self.rationale = rationale

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Verdict(reason={self.reason!r}, window={self.quota_window!r}, "
            f"scope={self.quota_scope!r}, source={self.source!r}, "
            f"rationale={self.rationale!r})"
        )


def classify(
    *,
    provider: str = "",
    model: str = "",
    status_code: Optional[int] = None,
    error_message: str = "",
    error_body: Any = None,
    headers: Any = None,
    error: Any = None,
    now_epoch: Optional[float] = None,
) -> Optional[Verdict]:
    """Classify a failure, or return ``None`` to leave the host in charge.

    Order matters. Permanent conditions are checked before throttles because
    an invalid key often arrives wearing a 429, and benching it for a minute
    means rediscovering it is dead a minute later, forever.
    """
    now = float(now_epoch) if now_epoch is not None else time.time()
    message = str(error_message or "")
    body_text = _body_text(error_body)
    status = status_code if isinstance(status_code, int) else None

    # HTTP 200 OK is an HTTP success; KAME does not classify HTTP 200 as an API error.
    if status == 200:
        return None

    # 0. Somebody else's failure, relayed. This has to come before every
    #    pattern below, because the nested upstream error is full of text
    #    about a credential that is not ours: an "API key not valid" from the
    #    upstream model would otherwise mark the user's healthy aggregator key
    #    permanently dead. Hermes already classifies this correctly
    #    (`upstream_rate_limit`, no rotation, fall back to another model) —
    #    and this hook runs *before* that logic, so the only safe move is to
    #    get out of the way.
    if looks_like_upstream_wrapper(error_body):
        return None

    # 1. A key the provider says is not a key. No timer fixes this.
    if _matches(_PERMANENT_AUTH_PATTERNS, message, body_text):
        return Verdict(
            reason="auth_permanent",
            retryable=False,
            should_rotate_credential=True,
            rationale="provider rejected the credential itself",
            source="pattern",
        )

    # 2. Out of money, or on a plan that forbids this call. A human can fix
    #    it, so re-probe daily rather than declaring the key dead.
    #
    #    This is the most expensive verdict the module can return — a day
    #    benched, at account scope, with both the probe and the escalation
    #    disarmed by the reason itself — so the ambiguous wording has to earn
    #    it. A provider that names a wait or names the counter that blew has
    #    already said this is a throttle, and the reading below will size it
    #    correctly from the same payload.
    ambiguous = _matches(_AMBIGUOUS_BILLING_PATTERNS, message, body_text)
    if _matches(_BILLING_PATTERNS, message, body_text) or (
        ambiguous
        and not _names_a_wait(
            message=message,
            body=error_body,
            body_text=body_text,
            headers=headers,
            error=error,
            now=now,
        )
    ):
        return Verdict(
            reason="billing",
            retryable=False,
            should_rotate_credential=True,
            reset_at=now + DEFAULT_ACCOUNT_BENCH_SECONDS,
            quota_window=QuotaWindow.ACCOUNT,
            quota_scope=QuotaScope.ACCOUNT,
            source="pattern",
            rationale="account-level limit — not a throttle",
        )

    # 3. Deliberate refusal for this key/model pairing — a suspended project,
    #    an API never enabled, a model outside the key's tier. Requires the
    #    text to say so; a bare 403 is not enough. Hermes runs a
    #    content-policy check ahead of its own status routing, and this hook
    #    fires ahead of *everything*, so claiming every 403 would hijack a
    #    per-prompt safety refusal and bench a healthy key over it.
    if _matches(_DENIAL_PATTERNS, message, body_text):
        return Verdict(
            reason="auth",
            retryable=False,
            should_rotate_credential=True,
            reset_at=now + DENIAL_BENCH_SECONDS,
            # The refusal is about this pairing: a model outside the key's
            # tier says nothing about the models inside it.
            quota_scope=QuotaScope.PER_MODEL,
            source="pattern",
            rationale="access denied for this key/model — re-probe hourly",
        )

    # 4. Authentication and authorization failures with nothing recognisable
    #    in them stay the host's problem: a transient 401 during token refresh
    #    is common, and KAME knows nothing the host does not.
    if status in _AUTH_STATUSES or status in _DENIAL_STATUSES:
        return None

    # 5. The provider is busy, not the credential spent. Hermes routes this
    #    to `overloaded` / `server_error` with **no** credential rotation, on
    #    purpose: rotating drains the pool while the endpoint is still busy
    #    and does nothing at all for a single-key user. Since `reset_at` is
    #    only ever applied by `mark_exhausted_and_rotate()`, a cooldown here
    #    would require exactly the rotation that must not happen — so there
    #    is nothing KAME can add, and a working recovery it could break.
    #
    #    This also has to gate the quota path below, not just the 5xx one: a
    #    429 whose body says "Overloaded" is congestion wearing a throttle's
    #    status code, and treating it as a spent key is the same mistake.
    #    The one exception is a provider that contradicts itself: prose that
    #    says "overloaded" beside a structured type that says "rate_limit".
    #    The status branch is *not* covered by it — a 5xx is the transport
    #    agreeing with the prose, and no field in the body outranks that.
    tokens = structured_error_tokens(error_body, error)
    contradicted = bool(tokens) and (
        _TYPE_THROTTLE.search(tokens) is not None
        and _TYPE_BUSY.search(tokens) is None
    )
    if status in _SERVER_STATUSES or (
        _matches(_BUSY_PATTERNS, message, body_text) and not contradicted
    ):
        return None

    if not (
        status in _RATE_LIMIT_STATUSES
        or _matches(_QUOTA_PATTERNS, message, body_text)
    ):
        return None

    decision: ResetDecision = compute_reset_at(
        now_epoch=now,
        provider=provider,
        message=message,
        body=error_body,
        headers=headers,
        error=error,
    )

    # 6. A throttle we cannot size is a throttle the host already handles.
    #
    #    Unless the payload contradicted itself, which is the one case where
    #    silence is not neutral. The host reads the same sentence KAME just
    #    set aside — "the engine is currently overloaded" — and reaches the
    #    same wrong conclusion: congestion, so do not rotate. Declining here
    #    would hand the decision back to the reading this whole branch exists
    #    to correct, and the pool would sit on a spent key with working keys
    #    beside it. The contradiction *is* the thing KAME knows and the host
    #    does not, which is this module's own standard for speaking at all.
    #
    #    ``reset_at`` stays ``None`` on purpose. Nothing in the payload said
    #    how long, so nothing here says how long either: rotate off this
    #    credential, bench it for nothing, let the ordinary escalation size
    #    the next refusal if there is one.
    if decision.reset_at is None:
        if contradicted:
            return Verdict(
                reason="rate_limit",
                retryable=True,
                should_rotate_credential=True,
                quota_window=decision.window,
                quota_scope=decision.scope,
                source="type",
                rationale=(
                    "provider's error type names a rate limit while its "
                    "message says overloaded — rotating, not waiting"
                ),
            )
        return None

    # An account-level window reached through the quota path is still an
    # account problem, not a rate limit — the distinction changes whether
    # the host counts it as retryable.
    if decision.window == QuotaWindow.ACCOUNT:
        return Verdict(
            reason="billing",
            retryable=False,
            should_rotate_credential=True,
            reset_at=decision.reset_at,
            quota_window=decision.window,
            quota_scope=decision.scope,
            source=decision.source,
            rationale=decision.rationale,
        )

    return Verdict(
        reason="rate_limit",
        retryable=True,
        should_rotate_credential=True,
        reset_at=decision.reset_at,
        quota_window=decision.window,
        quota_scope=decision.scope,
        source=decision.source,
        rationale=decision.rationale,
    )
