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

from . import catalog
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
    # A counter named by its unit and its window and by nothing else:
    # "You have exceeded your limit of 200000 tokens per day". No "rate
    # limit", no "quota exceeded" — the two words every pattern above waits
    # for are the two words this sentence does not contain. Carried over from
    # the Agent Zero plugin, where it was one of the markers that kept the
    # agent running through refusals this port went quiet on. ``quota.py``
    # already reads the window out of the same words (``tokensperday`` is in
    # ``_PER_DAY_MARKERS``); it was never asked, because nothing upstream
    # called this a quota failure.
    re.compile(r"tokens?[\s_-]*per[\s_-]*(?:min|minute|hour|day|week|month)", re.I),
    # "no quota left", "Quota left: 0". Same family, stated as a remainder
    # instead of as a breach.
    re.compile(r"quota[\s_-]*left", re.I),
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
def structured_error_values(
    body: Any, error: Any = None, host_code: Any = None
) -> list:
    """Every machine-readable type/code string a failure carries.

    **Ordered by specificity, not by where it was found**, because
    :func:`catalog.look_up` takes the first row that matches and the same
    payload routinely carries a precise field and a vague one that disagree.

    The case that settles the order: Google's invalid-key body says
    ``status: "INVALID_ARGUMENT"`` — which is true, and means "malformed
    request", and would end the turn — while carrying
    ``ErrorInfo.reason: "API_KEY_INVALID"``, which is the actual fact. OpenAI
    does the same one level down: ``type: "invalid_request_error"`` with
    ``code: "invalid_api_key"``. In both, the coarse field is a *family* and
    the precise one is the *member*, so reading the family first retires a
    turn over a key that simply needed replacing.

    Only strings are taken. OpenRouter's ``error.code`` is the integer 429 and
    says nothing a status code has not already said; Alibaba's is
    ``"Throttling.RateQuota"`` and says a great deal.
    """
    # Four buckets, most specific first, flattened at the end.
    reason: list = []    # google.rpc.ErrorInfo.reason — names the exact fact
    code: list = []      # error.code / exc.code — names the member
    title: list = []     # RFC 7807 title — the only field some APIs fill in
    family: list = []    # error.status / error.type — names the family

    def take(bucket: list, value: Any) -> None:
        if isinstance(value, str) and value.strip():
            bucket.append(value)

    # The host's own extraction, handed to the hook as ``error_code``. Not
    # redundant with the reads below: ``agent/error_classifier.py``'s
    # ``_extract_error_code`` walks the exception's ``__cause__``/``__context__``
    # chain five levels deep, parses a JSON object nested inside
    # ``error.message``, and knows the ``error_code``/``errorCode`` spellings —
    # all of which this module's fixed path list misses. It is the same *kind*
    # of value from a strictly better extractor, so it goes in the same bucket
    # and is ranked first within it.
    take(code, host_code)

    if error is not None:
        try:
            take(code, getattr(error, "code", None))
        except Exception:  # pragma: no cover - hostile attribute access
            pass
        try:
            take(family, getattr(error, "type", None))
        except Exception:  # pragma: no cover - hostile attribute access
            pass
        # ``exc.details`` — the parsed error the host already extracted, and
        # for one whole provider the *only* place the machine-readable values
        # survive. Hermes serves Gemini through its own adapter
        # (``agent/gemini_native_adapter.gemini_http_error``), which reads the
        # response body once, files ``{"status", "reason", "metadata",
        # "message"}`` on the exception, and sets ``code`` to a name it invents
        # — ``gemini_rate_limited``, ``gemini_unauthorized`` — which no
        # catalogue can know. The body is then spent: a streaming
        # ``httpx.Response`` raises on a second read, so ``error_body`` arrives
        # empty and every read below it finds nothing.
        #
        # Measured before this existed: 225 Gemini refusals over 13.9 days,
        # **zero** catalogue hits, 184 of them decided by prose instead — while
        # ``details["status"]`` held ``RESOURCE_EXHAUSTED`` and
        # ``details["reason"]`` held ``RATE_LIMIT_EXCEEDED``, both of which the
        # table has always recognised. The values were never missing; they were
        # never offered.
        try:
            detail_map = getattr(error, "details", None)
        except Exception:  # pragma: no cover - hostile attribute access
            detail_map = None
        if isinstance(detail_map, dict):
            # ``google.rpc.ErrorInfo.reason``, already unwrapped by the host.
            take(reason, detail_map.get("reason"))
            # Google's canonical status. A family, ranked with the families
            # exactly as the same value is when it arrives inside a body.
            take(family, detail_map.get("status"))
            metadata = detail_map.get("metadata")
            if isinstance(metadata, dict):
                take(code, metadata.get("provider_code"))
                take(family, metadata.get("error_type"))

    if isinstance(body, dict):
        take(code, body.get("code"))
        take(family, body.get("type"))
        # RFC 7807 problem+json. NVIDIA NIM's entire 429 body is
        # ``{"status": 429, "title": "Too Many Requests"}`` — ``title`` is the
        # only structured field it fills in, and reading ``type``/``code``
        # alone meant the one thing NVIDIA said was the one thing nobody
        # looked at. Fifteen of thirty-eight recorded NVIDIA blocks arrived
        # with no usable status either.
        take(title, body.get("title"))
        inner = body.get("error")
        if isinstance(inner, dict):
            take(code, inner.get("code"))
            take(family, inner.get("type"))
            # Google's canonical status string — ``RESOURCE_EXHAUSTED``,
            # ``FAILED_PRECONDITION``, ``INVALID_ARGUMENT``. A family name, so
            # it sits with the families rather than above them.
            take(family, inner.get("status"))
            metadata = inner.get("metadata")
            if isinstance(metadata, dict):
                # An aggregator relaying the upstream's own code. As specific
                # as a code gets, and the only evidence available when the
                # normalised type is ``unmapped``.
                take(code, metadata.get("provider_code"))
                take(family, metadata.get("error_type"))
            # ``google.rpc.ErrorInfo.reason`` — ``API_KEY_INVALID``,
            # ``RATE_LIMIT_EXCEEDED``, ``SERVICE_DISABLED``.
            for member in inner.get("details") or []:
                if not isinstance(member, dict):
                    continue
                if str(member.get("@type") or "").endswith("/google.rpc.ErrorInfo"):
                    take(reason, member.get("reason"))

    return reason + code + title + family


def structured_error_tokens(
    body: Any, error: Any = None, host_code: Any = None, host_class: Any = None
) -> str:
    """The same values, joined — for the regex contradiction check below.

    The exception's class name joins the stream here even though it is read
    through a separate table for verdicts. The contradiction check asks
    whether a *structured* signal disagrees with prose that says "overloaded",
    and a class is exactly such a signal: ``InternalServerError`` beside
    "overloaded" is agreement, ``RateLimitError`` beside it is the
    disagreement this check exists to catch.
    """
    values = structured_error_values(body, error, host_code)
    if isinstance(host_class, str) and host_class.strip():
        values = values + [host_class]
    return " ".join(values)

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


def _details_text(error: Any, limit: int = 20000) -> str:
    """The searchable text of ``exc.details``, or ``""``.

    Same material as a body and read for the same reasons — it is the
    provider's own words about *this* call, parsed by the host instead of
    being left in the response. It is kept in its own function because it is
    only ever *appended*: an exception whose body survived already carries
    this text, and a payload has one story, not two.

    Nothing from another party can enter here. ``looks_like_upstream_wrapper``
    guards against a relayed upstream failure and reads ``error_body``, which
    this does not touch.
    """
    if error is None:
        return ""
    try:
        details = getattr(error, "details", None)
    except Exception:  # pragma: no cover - hostile attribute access
        return ""
    if not details:
        return ""
    try:
        return str(details)[:limit]
    except Exception:  # pragma: no cover - hostile __str__
        return ""


def _details_body(error: Any) -> Any:
    """``exc.details`` as a walkable structure, for the readers that walk one.

    ``_details_text`` is for the regexes; this is for everything that takes a
    ``body=`` and traverses it — ``detect_quota_window``,
    ``detect_quota_scope``, ``extract_retry_delay_seconds``,
    ``compute_reset_at``. They were all handed ``error_body``, and on Gemini
    ``error_body`` is ``None``, so the field that names the counter
    (``metadata.quota_limit``: ``GenerateRequestsPerMinutePerProjectPerModel``)
    was carried into the process and read by nobody.

    Returns ``None`` for anything that is not a structure those readers can
    walk, so a provider that files a bare string on ``details`` changes
    nothing.
    """
    if error is None:
        return None
    try:
        details = getattr(error, "details", None)
    except Exception:  # pragma: no cover - hostile attribute access
        return None
    return details if isinstance(details, (dict, list, tuple)) and details else None


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


def stated_window(*, error_body: Any = None, error: Any = None) -> str:
    """The window the provider's *own* counter named, whatever KAME concluded.

    Two different questions share one vocabulary here, and keeping them apart
    is the point. ``Verdict.quota_window`` is what KAME decided to act on,
    reached through headers, prose, the catalogue and this identifier
    together. This is only the last of those: the counter Google names in
    ``quotaId``/``quota_limit``, read on its own and reported unchanged.

    Recorded so the two can be compared later. A provider that keeps saying
    ``PerMinute`` on a refusal KAME benches for a day is the shape of a
    misclassification, and it is invisible while only the conclusion is
    stored — the journal would show a confident daily bench and nothing to
    argue with it.

    Returns a word from KAME's own vocabulary, never the provider's text.
    The identifier itself is not carried anywhere: it is provider-authored
    text, and the report has one invariant it will not spend — that no error
    text reaches it.
    """
    text = _body_text(error_body)
    details = _details_text(error)
    if details and details not in text:
        text = (text + "\n" + details) if text else details
    window, _scope = catalog.read_quota_id(text)
    return window


def classify(
    *,
    provider: str = "",
    model: str = "",
    status_code: Optional[int] = None,
    error_message: str = "",
    error_body: Any = None,
    headers: Any = None,
    error: Any = None,
    error_type: str = "",
    error_code: str = "",
    now_epoch: Optional[float] = None,
) -> Optional[Verdict]:
    """Classify a failure, or return ``None`` to leave the host in charge.

    Order matters. Permanent conditions are checked before throttles because
    an invalid key often arrives wearing a 429, and benching it for a minute
    means rediscovering it is dead a minute later, forever.

    ``error_type`` and ``error_code`` are the host's own parse, handed to the
    hook and — until 1.5.0 — discarded by it. ``error_type`` is literally
    ``type(error).__name__`` (``agent/error_classifier.py:680``); ``error_code``
    comes from an extractor that reaches further than this module's own. Both
    default to empty so every existing caller, and every test, keeps working
    unchanged.
    """
    now = float(now_epoch) if now_epoch is not None else time.time()
    message = str(error_message or "")
    body_text = _body_text(error_body)
    # A body the host consumed leaves nothing here. Hermes' Gemini adapter
    # reads the response once, files the parsed error on the exception, and
    # the ``httpx.Response`` refuses a second read — so for that whole
    # provider ``error_body`` arrives empty and every search below it finds
    # nothing, ``quotaId`` included. The details are the same payload,
    # already parsed; appended, never substituted, and only when they are not
    # already in the text.
    details_text = _details_text(error)
    if details_text and details_text not in body_text:
        body_text = (body_text + "\n" + details_text) if body_text else details_text
    # The same substitution for the readers that traverse a body instead of
    # searching text. Only when there is no body: a real body is the whole
    # payload and the details are a slice of it, so preferring the body keeps
    # every existing provider reading exactly as it was.
    evidence_body = error_body if error_body else _details_body(error)
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

    # 0.5 What the provider said in a *field*, before anything reads a
    #     sentence. This is the whole reason `catalog` exists: the sentence
    #     Google sends for a twenty-one second throttle is, word for word, the
    #     sentence OpenAI sends when the balance is empty, and no amount of
    #     pattern work separates them. The fields never collide.
    #
    #     The catalogue answers for four families only — throttle, billing,
    #     denial, dead credential — because those are the four this plugin acts
    #     on. Server, timeout, terminal and "not actually a failure" are all
    #     cases where the host is already right and KAME's job is to stay out
    #     of the way, so a row of those kinds returns ``None`` here exactly as
    #     the paths below would have.
    #     The class name is consulted last and only when the two lookups above
    #     found nothing. A class names the family the SDK author chose; a field
    #     names what the provider said about this call, and the provider always
    #     outranks the library. Where it earns its place is the payload that
    #     carries neither field nor status — every transport failure, and any
    #     SDK class the host's own ``RateLimitError -> 429`` repair does not
    #     cover.
    #     A status code on its own is the weakest evidence this module reads —
    #     it is the one signal every failure has, and the one that says least
    #     about which of several conditions produced it. So a status-only
    #     reading that would *end* the turn has to yield to a payload that
    #     names a way out. Hermes' own corpus is where this surfaced:
    #
    #         402, body message "Usage limit reached, try again in 5 minutes"
    #
    #     ``_STATUS_READINGS[402]`` reads "payment required" and returns
    #     ``billing`` — a key retired, no retry, never re-probed. But a balance
    #     that is empty does not tell you to come back in five minutes. The
    #     provider stated a window; the status was a guess about what the
    #     window meant. KAME shipped 1.4.0 overriding the host on exactly this
    #     payload, and nobody ran ``tools/host_corpus.py`` to find out.
    status_reading = catalog.look_up_status(status)
    if status_reading is not None and status_reading.family == catalog.BILLING:
        stated, _stated_source = extract_retry_delay_seconds(
            message=message,
            body=evidence_body,
            headers=headers,
            error=error,
            now_epoch=now,
        )
        if stated is not None:
            status_reading = None

    catalog_reading = (
        catalog.look_up(*structured_error_values(error_body, error, error_code))
        or status_reading
        or catalog.read_exception_class(error_type)
    )

    catalog_throttle = False
    if catalog_reading is not None:
        family = catalog_reading.family
        if family in (catalog.SERVER, catalog.TIMEOUT, catalog.TERMINAL,
                      catalog.NOT_A_FAILURE):
            return None
        if family == catalog.AUTH_DEAD:
            return Verdict(
                reason="auth_permanent",
                retryable=False,
                should_rotate_credential=True,
                source="catalog",
                rationale=catalog_reading.why or "the provider named the credential dead",
            )
        if family == catalog.BILLING:
            return Verdict(
                reason="billing",
                retryable=False,
                should_rotate_credential=True,
                reset_at=now + DEFAULT_ACCOUNT_BENCH_SECONDS,
                quota_window=catalog_reading.window or QuotaWindow.ACCOUNT,
                quota_scope=catalog_reading.scope or QuotaScope.ACCOUNT,
                source="catalog",
                rationale=catalog_reading.why or "account-level limit — not a throttle",
            )
        if family == catalog.DENIAL:
            return Verdict(
                reason="auth",
                retryable=False,
                should_rotate_credential=True,
                reset_at=now + DENIAL_BENCH_SECONDS,
                quota_scope=catalog_reading.scope or QuotaScope.PER_MODEL,
                source="catalog",
                rationale=catalog_reading.why or "access denied for this key/model",
            )
        # A throttle does not return here. It still goes through the sizing
        # cascade below, because *how long* is a separate question from *what*
        # — and the cascade reads headers, RetryInfo and absolute anchors that
        # no lookup table can hold. What the reading buys is the right to
        # answer even when the sizing comes back empty, which is where this
        # module used to fall silent and hand a spent key back to the host.
        catalog_throttle = True

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
    #
    #    A catalogued throttle disarms the ambiguous half outright, ahead of
    #    any evidence-weighing. The whole reason that pattern is called
    #    *ambiguous* is that the sentence means two things; a provider that
    #    also filed ``RESOURCE_EXHAUSTED`` or ``RATE_LIMIT_EXCEEDED`` in a
    #    field has already said which one, and this module's standing rule is
    #    that a field outranks a sentence. Nothing here weakens the
    #    unambiguous half: "credit balance is too low" beside a throttle field
    #    is a genuine disagreement, not a known-ambiguous sentence, and the
    #    more specific claim still wins.
    ambiguous = _matches(_AMBIGUOUS_BILLING_PATTERNS, message, body_text)
    if _matches(_BILLING_PATTERNS, message, body_text) or (
        ambiguous
        and not catalog_throttle
        and not _names_a_wait(
            message=message,
            body=evidence_body,
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
    tokens = structured_error_tokens(error_body, error, error_code, error_type)
    busy_prose = _matches(_BUSY_PATTERNS, message, body_text)
    #: A contradiction needs *both* halves. Until 1.4.0 this was computed from
    #: the structured tokens alone, which was harmless while the only tokens
    #: read were ``type`` and ``code`` — and stopped being harmless the moment
    #: ``title`` joined them, because NVIDIA's ``"Too Many Requests"`` then
    #: satisfied "the type names a throttle" with no prose disagreeing with
    #: anything. The verdict was still *rotate*, so the behaviour was right by
    #: luck, and the rationale it printed — "its message says overloaded" —
    #: was a sentence about a message that did not exist. A reason a human
    #: reads in the Events tab has to be true, or the tab is worse than empty.
    contradicted = busy_prose and bool(tokens) and (
        _TYPE_THROTTLE.search(tokens) is not None
        and _TYPE_BUSY.search(tokens) is None
    )
    if status in _SERVER_STATUSES or (busy_prose and not contradicted):
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
        body=evidence_body,
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
        if catalog_throttle:
            # The provider named a throttle in a field and gave nothing to
            # size it by. That is not a reason to stay silent — it is the
            # commonest failure there is. NVIDIA NIM's whole 429 body is
            # ``{"status": 429, "title": "Too Many Requests"}``: no
            # ``Retry-After``, no ``X-RateLimit-*``, sometimes no body at all.
            # Declining here handed a spent key straight back and the pool sat
            # on it while healthy keys waited beside it.
            #
            # ``reset_at`` stays ``None`` on purpose, exactly as in the
            # contradiction case above: nothing said how long, so nothing here
            # says how long. Rotate off this credential, bench it for nothing,
            # and let the ordinary escalation size the next refusal if there is
            # one. That is also the right shape for NVIDIA specifically, whose
            # burst limits clear in seconds — the escalating ladder that used
            # to run instead spent 20s, then 40s, then 1m20s before answering
            # on attempt six.
            window, scope = catalog.read_quota_id(body_text)
            return Verdict(
                reason="rate_limit",
                retryable=True,
                should_rotate_credential=True,
                quota_window=(
                    window if window != QuotaWindow.UNKNOWN
                    else catalog_reading.window
                ),
                quota_scope=(
                    scope if scope != QuotaScope.UNKNOWN
                    else catalog_reading.scope
                ),
                source="catalog",
                rationale=(
                    catalog_reading.why
                    or "the provider named a throttle but nothing to size it by"
                ),
            )
        return None

    # An account-level window reached through the quota path is still an
    # account problem, not a rate limit — the distinction changes whether
    # the host counts it as retryable.
    #
    # A week and a month join it in 1.5.0 — **but only when nothing stated a
    # reset.** Hermes' own corpus taught both halves of that rule, one after
    # the other, which is the entire argument for running it:
    #
    #   "Monthly quota reached."                      -> billing  (#39441)
    #   "Weekly usage limit reached. Resets in 6hr."   -> rate_limit (#63021)
    #
    # Same window, opposite verdicts, and the difference is whether the
    # provider said when it comes back. An allowance that is simply gone is
    # metered on the credential rather than on a request rate: retrying this
    # key sooner cannot help, only a different key can, which is what
    # ``retryable=False, should_rotate_credential=True`` says. An allowance
    # that names its own reset — six hours, not a month — is a wait, and
    # calling it billing would retire a key that is coming back this evening.
    #
    # ``decision.source`` is exactly that distinction already computed:
    # ``"window"`` is KAME's own default, applied because the payload supplied
    # nothing to size by. Any other source is the provider having spoken.
    #
    # The bench length is untouched either way — ``_WINDOW_BENCH_DEFAULTS``
    # gives a week and a month the day-window default, not the account one, so
    # the key returns on the same schedule. What changed is the name of what
    # happened, and therefore whether the host counts it as retryable.
    _spent_allowance = (
        decision.window in (QuotaWindow.PER_WEEK, QuotaWindow.PER_MONTH)
        and decision.source == "window"
    )
    if decision.window == QuotaWindow.ACCOUNT or _spent_allowance:
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
