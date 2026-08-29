"""What providers call things, and what each name actually means.

Every entry here is a **machine-readable field** a provider filled in — an
``error.type``, an ``error.code``, a Google ``status``, an RFC 7807 ``title``,
an ``ErrorInfo.reason``. None of it is prose.

That distinction is the whole point of the module. Prose is the only evidence
two providers can share while meaning opposite things, and they do:

    Google, per-minute throttle, clears in 21 seconds:
        "You exceeded your current quota, please check your plan and billing details."
    OpenAI, out of credits, clears when somebody pays:
        "You exceeded your current quota, please check your plan and billing details."

Word for word the same sentence. The fields are never the same: Google says
``status: RESOURCE_EXHAUSTED`` and ships a ``RetryInfo``; OpenAI says
``type: insufficient_quota`` and ships nothing to wait for. Reading the field
settles in one lookup what no amount of pattern work can settle at all.

The cost of not having done this is measured, on one real pool over nine days:
**1,088** occurrences of that Google sentence, **79** of them benched for an
hour as a daily cap, on a limit that clears in a minute — with fourteen keys
going down one after another and a human opening the panel to press *clear
pool* fifteen times to get working again.

**Identity is still not evidence.** The provider names in the comments are how
a human checks a row; nothing in this module branches on who is calling. A
provider that appears nowhere here is read by exactly the same rules as one
that appears twice, because what is matched is the *shape* of what came back.

The reasoning behind every row, with sources, is in
``knowledge_base/provider-errors.md``. That document and this table change
together.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from .quota import QuotaScope, QuotaWindow

# ── families ──────────────────────────────────────────────────────────────
# What KAME does about a failure, which is a smaller set than what providers
# call them. The names match the vocabulary the rest of the plugin escalates
# against, so a reading here needs no translation downstream.

THROTTLE = "throttle"          # a counter on this credential; rotating helps
BILLING = "billing"            # an empty balance; waiting achieves nothing
DENIAL = "denial"              # deliberate refusal of this key/model pairing
AUTH_DEAD = "auth_dead"        # the credential is not a credential
SERVER = "server"              # the provider is busy; the key is fine
TIMEOUT = "timeout"            # nothing arrived in time; the key is fine
TERMINAL = "terminal"          # no key on earth answers this request
NOT_A_FAILURE = "not_a_failure"  # a success wearing an error's clothes


class Reading:
    """One catalogue row: what the field means, and how sure that is.

    ``window`` and ``scope`` are hints, not conclusions — the sizing cascade in
    :mod:`quota` still runs and still wins when it finds something better. A
    hint exists for the case where the payload names the *kind* of limit and
    nothing else, which is most of them.
    """

    __slots__ = ("family", "window", "scope", "why", "certain")

    def __init__(
        self,
        family: str,
        *,
        window: str = QuotaWindow.UNKNOWN,
        scope: str = QuotaScope.UNKNOWN,
        why: str = "",
        certain: bool = True,
    ) -> None:
        self.family = family
        self.window = window
        self.scope = scope
        self.why = why
        #: ``False`` means "this row is a tiebreak, not a verdict" — it may
        #: refine a reading the rest of the classifier already reached, but it
        #: may not overturn one on its own.
        self.certain = certain

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Reading({self.family!r}, window={self.window!r}, why={self.why!r})"


def _n(text: str) -> str:
    """Normalise a field value for lookup: lowercase, separators removed.

    ``Throttling.RateQuota``, ``throttling_ratequota`` and
    ``THROTTLING-RATEQUOTA`` are one row, because which spelling a provider
    chose is not information.
    """
    return re.sub(r"[\s_.\-]+", "", str(text or "")).lower()


# ── the table ─────────────────────────────────────────────────────────────
# Keyed on the normalised field value. One entry per *meaning*, with every
# spelling that carries it listed together.

_TABLE: Dict[str, Reading] = {}


def _put(reading: Reading, *names: str) -> None:
    for name in names:
        _TABLE[_n(name)] = reading


# --- an empty balance. No timer fixes any of these. ----------------------
_put(
    Reading(BILLING, window=QuotaWindow.ACCOUNT, scope=QuotaScope.ACCOUNT,
            why="the account is out of credit"),
    # OpenAI, and everything that copied its envelope.
    "insufficient_quota", "billing_hard_limit_reached", "billing_not_active",
    # Anthropic 402.
    "billing_error",
    # OpenRouter's normalised vocabulary.
    "payment_required",
    # Alibaba Model Studio: an unpaid bill, delivered on a 400.
    "Arrearage",
    # Alibaba again: the free allowance is gone for good, delivered on a 403 —
    # the status that is otherwise handed straight back to the host.
    "AllocationQuota.FreeTierOnly",
)

# --- a counter on this credential. Rotating to another key helps. --------
_put(
    Reading(THROTTLE, why="a per-credential counter is spent"),
    "rate_limit_exceeded", "rate_limit_error", "rate_limited", "ratelimit",
    "too_many_requests",
    # RFC 7807 `title`, which is the only structured field NVIDIA NIM fills in.
    "Too Many Requests",
    # Google's status string on a 429.
    "RESOURCE_EXHAUSTED",
    # Google ErrorInfo.reason.
    "RATE_LIMIT_EXCEEDED",
    # Alibaba's entire 429 family is spelled with this stem and never with the
    # words "rate limit"; the prefix rule below catches the sub-codes.
    "Throttling",
    # A concurrency ceiling is a per-credential counter like any other.
    "concurrency_limit_exceeded", "too_many_concurrent_requests",
)

# --- the provider is busy. The credential is fine. ------------------------
_put(
    Reading(SERVER, why="the provider is busy, not the credential spent"),
    "overloaded_error", "overloaded", "server_error", "api_error",
    "service_unavailable", "engine_overloaded",
    # Google status strings.
    "UNAVAILABLE", "INTERNAL",
    # OpenRouter's normalised vocabulary.
    "server", "provider_error",
)

# --- nothing arrived in time. --------------------------------------------
_put(
    Reading(TIMEOUT, why="no response in time"),
    "timeout_error", "timeout", "request_timeout", "DEADLINE_EXCEEDED",
)

# --- the credential is not a credential. ---------------------------------
_put(
    Reading(AUTH_DEAD, why="the provider rejected the credential itself"),
    "invalid_api_key", "authentication_error", "authentication",
    "API_KEY_INVALID", "invalid_authentication", "UNAUTHENTICATED",
)

# --- deliberate refusal of this key for this model. -----------------------
_put(
    Reading(DENIAL, scope=QuotaScope.PER_MODEL,
            why="this key is refused for this model"),
    "permission_error", "permission_denied", "PERMISSION_DENIED",
    "SERVICE_DISABLED", "CONSUMER_SUSPENDED", "API_KEY_SERVICE_BLOCKED",
    "access_denied", "model_not_authorized",
)

# --- the request itself. Never touch the credential. ---------------------
_put(
    Reading(TERMINAL, why="no key can answer this request"),
    "model_not_found", "not_found_error", "NOT_FOUND", "request_too_large",
    "context_length_exceeded", "string_too_long", "invalid_prompt",
)

# --- the wastebaskets. -----------------------------------------------------
# These two are *family* names, not facts, and providers put things in them
# that have nothing to do with the request. Anthropic's own documentation says
# `invalid_request_error` "may also be used for other 4XX status codes not
# listed in this section" — and it is what Anthropic returns when an
# organisation hits a **spend limit**. Google's `INVALID_ARGUMENT` is the
# status on an **invalid API key**.
#
# So they are catalogued, because a bare one really does mean the request is
# malformed — and marked uncertain, because whenever a payload carries one of
# these *and* something specific, the specific one is the fact and this is the
# bucket it was filed under. Three real payloads make the point, and they do
# not even agree on which field the bucket sits in:
#
#   Google    status "INVALID_ARGUMENT"      + ErrorInfo.reason "API_KEY_INVALID"
#   OpenAI    type   "invalid_request_error" + code "invalid_api_key"
#   DeepSeek  code   "invalid_request_error" + type "authentication_error"
#
# DeepSeek is why this is a property of the *value* and not of the field: it
# puts the precise name in `type` and the bucket in `code`, exactly inverting
# OpenAI. Any rule that ranks fields instead of meanings gets one of them
# wrong, and getting it wrong ends a turn over a key that needed replacing.
_put(
    Reading(TERMINAL, certain=False,
            why="the provider filed this under its malformed-request family"),
    "invalid_request_error", "INVALID_ARGUMENT",
)

# --- shapes that only *look* like failures. ------------------------------
# OpenRouter turns these into a successful completion with
# `finish_reason: length`, so an engine that reads them as errors rotates a
# perfectly good key over an answer that arrived.
_put(
    Reading(NOT_A_FAILURE, why="the provider returned this as a completion"),
    "max_tokens_exceeded", "token_limit_exceeded",
)

# --- Google's odd one out. ------------------------------------------------
# FAILED_PRECONDITION on a 400 means "the free tier is not available in your
# country; enable billing" — an account problem wearing the status code that
# every rotation engine treats as a malformed request. Read as terminal, it
# ends a turn that no amount of rotation was ever going to save, and does so
# without telling the user the actual reason.
_put(
    Reading(BILLING, window=QuotaWindow.ACCOUNT, scope=QuotaScope.ACCOUNT,
            why="the free tier is unavailable here; billing must be enabled"),
    "FAILED_PRECONDITION",
)


# ── status codes a rotation engine gets wrong by default ─────────────────
# Only the ones whose *default* reading is wrong. Everything ordinary — 401,
# 403, 429, 5xx — is already routed correctly by the classifier and is
# deliberately absent, so this table stays a list of surprises.
_STATUS_READINGS: Dict[int, Reading] = {
    # Anthropic and DeepSeek both use 402 for an empty balance. Most rotation
    # engines have no branch for it at all, so it falls into a generic bucket
    # and the key is re-probed every twenty seconds for ever.
    402: Reading(BILLING, window=QuotaWindow.ACCOUNT, scope=QuotaScope.ACCOUNT,
                 why="payment required"),
    # Groq's custom code: the flex tier is at capacity. Retryable, and in no
    # standard status set, so it lands in `other` and gets a 20s rest when it
    # should be treated exactly like a 503.
    498: Reading(SERVER, why="flex tier at capacity"),
    # Anthropic's overload code. Same reasoning as 498.
    529: Reading(SERVER, why="the provider is overloaded"),
}


# ── prefix rules, for code families rather than single codes ─────────────
# A short, closed list. Each one is a provider that namespaces its codes, where
# the prefix carries the meaning and the suffix only narrows it.
_PREFIX_RULES: Tuple[Tuple[str, Reading], ...] = (
    # Throttling.RateQuota, Throttling.BurstRate, Throttling.AllocationQuota …
    # `AllocationQuota.FreeTierOnly` is billing and is matched exactly above,
    # which is why exact lookups run before these.
    ("throttling", Reading(THROTTLE, why="a per-credential counter is spent")),
)


# ── Google quota identifiers ─────────────────────────────────────────────
# `quotaId` is a single string that names the window and the scope at once —
# `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`. It is the most
# informative field any provider sends on a 429 and the host discards it.
_QUOTA_ID = re.compile(r'"quotaId"\s*:\s*"([^"]+)"')

_WINDOW_FRAGMENTS: Tuple[Tuple[str, str], ...] = (
    # Widest first. A body naming both PerMinute and PerDay is saying the daily
    # counter is the binding one; reading it as a blip re-hammers a key that
    # will refuse all day.
    ("permonth", QuotaWindow.PER_MONTH),
    ("perweek", QuotaWindow.PER_WEEK),
    ("perday", QuotaWindow.PER_DAY),
    ("perhour", QuotaWindow.PER_HOUR),
    ("perminute", QuotaWindow.PER_MINUTE),
)


def read_quota_id(body_text: str) -> Tuple[str, str]:
    """``(window, scope)`` from a Google ``quotaId``, or two unknowns.

    ``-FreeTier`` is a tier marker and deliberately not read as a window: it
    says which allowance was spent, not how long until it refills.

    Scope: ``PerProjectPerModel`` names both, and the narrower half decides —
    a per-model cap says nothing about the other models the key can reach.
    """
    if not body_text:
        return QuotaWindow.UNKNOWN, QuotaScope.UNKNOWN
    match = _QUOTA_ID.search(body_text)
    if not match:
        return QuotaWindow.UNKNOWN, QuotaScope.UNKNOWN
    raw = _n(match.group(1))

    window = QuotaWindow.UNKNOWN
    for fragment, value in _WINDOW_FRAGMENTS:
        if fragment in raw:
            window = value
            break

    if "permodel" in raw or "perbasemodel" in raw:
        scope = QuotaScope.PER_MODEL
    elif "perproject" in raw or "peruser" in raw or "perapikey" in raw:
        # Metered on the key rather than on one model, so every model the key
        # can reach is blocked with it. ``ACCOUNT`` is this vocabulary's word
        # for that reach — there is no third value, and inventing one would
        # give the pool a scope it has no branch for.
        scope = QuotaScope.ACCOUNT
    else:
        scope = QuotaScope.UNKNOWN
    return window, scope


# ── the exception's own class name ───────────────────────────────────────
# A curated table, deliberately *not* folded into ``_TABLE``.
#
# The host hands the plugin ``error_type``, and
# ``agent/error_classifier.py`` computes it as literally
# ``type(error).__name__``. That is the most provider-independent
# machine-readable evidence there is: the OpenAI, Anthropic and Cerebras SDKs
# are all Stainless-generated and raise one shared taxonomy, so a rule written
# once here covers every provider shipping an OpenAI-compatible client —
# without a single branch on who is calling, which is this module's whole
# contract. It is also the *only* evidence that exists for a transport
# failure, which carries no status and no body at any point.
#
# **Why this is a separate table rather than more rows above.** ``_n`` strips
# separators, so a class name and a snake_case field value can normalise to
# the same key. Four of them already do:
#
#     RateLimitError    -> ratelimiterror     == rate_limit_error
#     OverloadedError   -> overloadederror    == overloaded_error
#     NotFoundError     -> notfounderror      == not_found_error
#     AuthenticationError -> authenticationerror == authentication_error
#
# The first three are harmless — the collision lands on a row that means the
# same thing. **The fourth is not, and it is the reason this table exists.**
# ``authentication_error`` is a provider *stating* a credential is dead, and
# reads as ``AUTH_DEAD``: retire the key, permanently, no retry.
# ``AuthenticationError`` is merely the class the SDK raises for **any** 401,
# including an expired OAuth token about to be refreshed and a gateway hiccup.
# Feeding class names into ``look_up`` would have retired healthy credentials
# on a bare 401 — which is precisely the defect 1.4.0 removed when it took
# ``"unauthorized"`` out of the permanent-auth patterns, after 21 hour-long
# quarantines of keys that were fine.
#
# So the mapping is explicit, and the omissions are the point.
_EXCEPTION_CLASSES: Dict[str, Reading] = {}


def _put_class(reading: Reading, *names: str) -> None:
    for name in names:
        _EXCEPTION_CLASSES[_n(name)] = reading


# A throttle. The most valuable row here: a bare ``RateLimitError`` with no
# body and no headers is a spent counter that the sizing cascade cannot
# measure, and without a reading this module falls silent and hands a spent
# key back to the host. The reading buys the right to answer with a default.
_put_class(
    Reading(THROTTLE, why="the SDK raised its rate-limit class"),
    "RateLimitError",
)

# An empty balance. Cerebras' SDK adds this to the shared taxonomy for 402,
# which ``_STATUS_READINGS`` already reads the same way — the two agree, and
# the class covers the case where the status did not survive.
_put_class(
    Reading(BILLING, window=QuotaWindow.ACCOUNT, scope=QuotaScope.ACCOUNT,
            why="the SDK raised its payment-required class"),
    "PaymentRequired", "PaymentRequiredError",
)

# The provider is busy. These return ``None`` from the classifier — the host
# is already right about a 5xx — but they are catalogued so the token stream
# used by the contradiction check sees them, and so the table is honest about
# what it knows.
_put_class(
    Reading(SERVER, why="the SDK raised a server-side class"),
    "InternalServerError", "OverloadedError", "ServiceUnavailableError",
    "APIStatusError",
)

# Nothing arrived in time. The class name is the whole of the evidence here:
# a transport failure has no status and no body, ever. ``core/carousel.py``
# recognised five of these names inline; this is that list, in the one place
# the plugin keeps such things.
_put_class(
    Reading(TIMEOUT, why="the SDK or transport raised a timeout class"),
    "APITimeoutError", "APIConnectionError", "TimeoutError", "ReadTimeout",
    "ConnectTimeout", "ConnectError", "StreamTimeout", "CancelledError",
    "DeadlineExceededError", "ReadTimeoutError", "PoolTimeout",
)

# The request itself. Also ``None`` from the classifier; listed so that a
# reader of this table can tell "we decided to stay out of it" from "we never
# considered it".
_put_class(
    Reading(TERMINAL, certain=False,
            why="the SDK raised a malformed-request class"),
    "BadRequestError", "NotFoundError", "UnprocessableEntityError",
    "RequestTooLargeError", "ContentTooLarge", "ConflictError",
)

#: Class names that reach here and are deliberately left unmapped, with the
#: reason, because an omission nobody wrote down gets "fixed" by the next
#: reader:
#:
#: ``AuthenticationError`` — the class of every 401, not a statement that the
#:   credential is dead. See the note above this table. A provider that has
#:   genuinely retired a key says so in words or in a field, and
#:   ``_PERMANENT_AUTH_PATTERNS`` and ``_TABLE`` both already read that.
#: ``PermissionDeniedError`` — the class of every 403. When the status is
#:   present the classifier's own denial path already handles it; mapping the
#:   class would add a one-hour bench for a status-less 403, which is rare,
#:   on evidence that does not distinguish "wrong model for this tier" from
#:   "this gateway refused once".
#: ``ProviderStreamError`` — Hermes' own wrapper
#:   (``agent/chat_completion_helpers.py:85``), raised when a provider encodes
#:   an API error as streaming content instead of as an SDK error. It says
#:   *how* the failure arrived, not *what* it was, and the host synthesizes
#:   ``status_code`` and ``body`` from the parsed event — so the evidence is in
#:   those fields and a family guessed from the wrapper would shadow them.
_UNMAPPED_ON_PURPOSE: Tuple[str, ...] = (
    "AuthenticationError",
    "PermissionDeniedError",
    "ProviderStreamError",
)


def read_exception_class(name: Any) -> Optional[Reading]:
    """A reading for an exception class name, or ``None``.

    Consulted only after :func:`look_up` and :func:`look_up_status` have both
    come back empty. A class is the vaguest machine-readable evidence a
    failure carries — it names the family the SDK author chose — so anything
    the provider said in a field outranks it, every time.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    return _EXCEPTION_CLASSES.get(_n(name))


def known_exception_classes() -> Tuple[str, ...]:
    """Every class name this table recognises. For the tests and ``/kame``."""
    return tuple(sorted(_EXCEPTION_CLASSES))


# ── the lookup ────────────────────────────────────────────────────────────

def look_up(*values: Any) -> Optional[Reading]:
    """The most decisive thing this table recognises among the values given.

    Two passes, and the order between them is the whole correctness argument:

    1. **A certain row wins, wherever it sat.** A payload that names both a
       family and a member is naming one fact and the drawer it was filed in.
       Since providers disagree about which *field* holds which — OpenAI puts
       the member in ``code``, DeepSeek puts it in ``type`` — the ranking is on
       the value's own meaning, never on where it was found.
    2. **An uncertain row is the answer only if nothing certain matched**, so a
       bare ``invalid_request_error`` with nothing beside it still reads as a
       malformed request, which it is.

    Exact matches beat prefix rules throughout: ``AllocationQuota.FreeTierOnly``
    is billing even though a prefix pass over ``Throttling``-style codes would
    otherwise call that whole family a throttle.
    """
    candidates = [str(v) for v in values if isinstance(v, (str, int)) and str(v).strip()]

    fallback: Optional[Reading] = None
    for value in candidates:
        hit = _TABLE.get(_n(value))
        if hit is None:
            continue
        if hit.certain:
            return hit
        if fallback is None:
            fallback = hit

    for value in candidates:
        normalised = _n(value)
        for prefix, reading in _PREFIX_RULES:
            if normalised.startswith(prefix):
                return reading
    return fallback


def look_up_status(status_code: Optional[int]) -> Optional[Reading]:
    """A reading for the status codes whose default treatment is wrong."""
    if not isinstance(status_code, int):
        return None
    return _STATUS_READINGS.get(status_code)


def known_names() -> Tuple[str, ...]:
    """Every field value this table recognises. Used by the tests and by
    ``/kame`` to report how large the catalogue is."""
    return tuple(sorted(_TABLE))
