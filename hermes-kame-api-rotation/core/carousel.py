"""The carousel — pick the healthiest key on every call, and learn from every answer.

This is the part of KAME that Agent Zero has had since 1.0.0 and Hermes has
never had. Everything else in this plugin reacts to a *failure*: a 429 arrives,
a cooldown is sized, a key is benched. That is half the machine. The other half
is that **a key is chosen before every single request**, healthy or not, so a
pool of fifteen keys spreads fifteen ways instead of hammering one until it
refuses.

The rules below are a faithful port of ``kame_engine.py`` v1.0.9 (the Agent Zero
build). They are stated here in one framework-free module for the same reason
the rest of ``core`` is: the decision rules are the asset, the host binding is
disposable.

Five rules, and why each one exists
-----------------------------------

**Selection is by load, then by age.** ``select`` keeps a 60-second sliding
window of when each key was used and picks the key with the fewest requests in
that window, breaking ties by least-recently-used. That is an RPM limit
expressed directly: a provider that allows N requests per minute per key is
kept under N by construction rather than by apology afterwards. The chosen key
has ``last_used`` stamped and the window appended *before the lock is released*
— two turns racing in the same process cannot both be handed the same key
(anti-dogpile), and neither can two turns that started in the same millisecond
(anti-thundering-herd).

**Health is per ``provider:model``, not per key.** Google meters free-tier quota
per key *per model*. A key spent on ``gemini-3.7-flash`` still has its whole
allowance on ``gemini-3.5-flash-lite``, and a pool that forgot this benches
fifteen keys for a limit that applied to one model. This mirrors what
``core.ledger`` does for Hermes' own benches; the two agree by design.

**A cooldown is never shortened.** ``mark`` stores ``max(existing, now + delay)``.
A key that just told us it is out for the day must not be released in twenty
seconds because a *different* call got a softer refusal from the same key a
moment later. Backoff escalates per key and per kind, and resets on success.

**Every rest is a number somebody measured — never one this module invented.**
A throttle rests for what the provider stated; if this refusal was not sized,
for what the provider stated about an earlier one on the same
``provider:model``; and only when it has never stated one at all, for a flat
:data:`UNSIZED_THROTTLE_REST_S`. There is no exponential, because a rate limit
has two regimes and nothing between them: a rolling window that closes in
seconds and whose length the provider will tell you, or a daily cap that
closes in hours under a different counter with a different name. No provider
documents "come back in five minutes", and a ladder climbing 1s → 300s spent
its whole range interpolating between regimes that do not meet.

Erring short is the deliberate half of that, and it is the argument that has
to stand on its own. A rest that is too short costs one request that fails in
milliseconds. A rest that is too long costs a healthy key for its whole
duration, and there is no request whose failure announces it: the cost is
silent, which is why every over-long bench in this plugin's history survived
for releases and every short one was reported within a day.

There is also a widening mechanism — :func:`escalate.stretch`, driven by the
journal's count of deadlines that were waited out in full and refused anyway
— and as of 1.6.0.3 it finally sees this path. It used to see only refusals
that reached the *host's* credential pool; the in-turn rotations sized here
were invisible to it, 74 of them and no journal rows in the owner's 1.6.0.2
run. ``dispatch_binding`` now files them through ``runtime.record_rotation``.
That is a correction, not a licence: nothing above is justified by
"measurement will fix it later", and every number here is either the
provider's or a flat re-probe — never a curve waiting to be tuned.

**A daily cap is the exception, and the only one.** Google returns a *short*
retryDelay on a daily-quota 429 — a real payload from its own forum shows 250
daily requests spent and ``retryDelay: "1s"``. Believing that produces a key
that returns to rotation, fails, and repeats, all day. So for ``daily`` and
``insufficient_quota`` the parsed delay is discarded and the configured
cooldown is used instead: probe hourly, not every second. This is v1.0.5's
rule and it was learned the hard way. It is also why the ceiling above refuses
to learn from any number longer than :data:`RL_BACKOFF_CAP_S` — on Gemini a
daily cap classifies as ``rate_limit`` too, and one exhausted day must not
teach every terse throttle afterwards to rest for an hour.

**5xx is checked before 429.** A real quota refusal is a 429, never a 503. An
overloaded provider that returns 503 to every key in the pool must not take the
whole pool offline for an hour — it gets a 5-second rest that escalates to a
90-second ceiling, and ``thaw_server_cooled`` pulls the rest of the pool
forward the moment any key answers again, so recovery from an outage is a snap
rather than a trickle.

What is deliberately *not* here
-------------------------------

No sleeping, no logging, no host objects, no clock other than the one passed in.
``select`` and ``mark`` are pure functions of the state they own, which is what
makes the whole rule set testable without a provider, a network, or a Hermes.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Acyclic on purpose: ``catalog`` imports only ``quota``, and ``quota`` imports
# nothing from this package. The carousel reads the catalogue rather than
# keeping a second copy of the same facts.
from . import catalog
from .quota import DEFAULT_DENIAL_BENCH_SECONDS, DEFAULT_REJECTED_BENCH_SECONDS

# --- the numbers, all in one place ------------------------------------------
#
# Every one of these is a v1.0.9 constant. Where a value is configurable in the
# host, the binding passes it in rather than editing this module.

#: Width of the requests-per-minute window used for selection.
RPM_WINDOW_S = 60.0

#: A daily cap or a permanent denial rests the key this long by default. The
#: host may override it (``daily_quota_cooldown_seconds``); one hour is the
#: Agent Zero default, chosen so a daily-quota key is retried hourly
#: rather than hammered every twenty seconds.
DAILY_COOLDOWN_S = 3600.0

#: No cooldown may exceed a day, whatever a provider claims.
HARD_DELAY_CAP_S = 86400.0

#: How long a key may stay in the pool after a call stopped offering it.
#:
#: The pool mirrors the credential list, but "this call did not offer it" and
#: "the config no longer declares it" are not the same sentence. One identity
#: can be reached by two agents with different lists — a fallback key carried
#: on ``agent.api_key``, a resolver substitution — and dropping a row the
#: moment one of them looks away would erase a cooldown the other one earned.
#: A key really removed from the config is never offered again, so it leaves
#: within this window; a key that belongs to somebody else's list is offered
#: again long before it closes.
MIRROR_GRACE_S = 300.0

#: A 5xx is the provider's problem, not the key's. Rest briefly, escalate
#: slowly, and never past this.
SERVER_BASE_S = 5.0
SERVER_BACKOFF_CAP_S = 90.0

#: A per-minute throttle escalates from the second strike, up to this.
#:
#: The ceiling bounds the ladder **KAME invents**, never a number the provider
#: stated. A provider that asks for longer than this is obeyed: the cap exists
#: so a guess cannot grow without end, not so a guess can overrule evidence.
RL_BACKOFF_CAP_S = 300.0

#: The smallest rest that is a cooldown rather than a spin. A provider that
#: answers "retry in 0.2s" is obeyed to the nearest second and no faster.
RL_BASE_S = 1.0

#: What a throttle rests for when the provider has never named a number for
#: this ``provider:model`` — not once, on any refusal. Deliberately the same
#: value as :data:`core.quota.DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS`, which
#: sizes the *bench* for the same payload: one refusal must not produce two
#: different waits depending on which half of the plugin is asked.
#:
#: Twenty seconds and flat, with no climb behind it. See :meth:`_escalate`
#: for why there is nothing to climb between.
UNSIZED_THROTTLE_REST_S = 20.0

#: A daily refusal escalates from this base toward ``DAILY_COOLDOWN_S``.
DAILY_BASE_S = 20.0

#: A read timeout is nearly always transient. Rest three seconds and rotate.
TIMEOUT_S = 3.0

#: A **bare** 401 or 403 — the provider refused the call and said nothing
#: about why. Twenty seconds, which is :data:`DAILY_BASE_S`, the opening step
#: of the ladder in :meth:`Carousel._escalate` that governs this kind anyway;
#: a larger number here would only flatten the first few strikes and delay the
#: re-check in the very case a re-check is most likely to help.
#:
#: Short is safe because of two things that are not the number: the demotion
#: in :func:`Carousel.select`, and :data:`REFUSALS_BEFORE_RETIRING`. See
#: :data:`~.quota.DEFAULT_REJECTED_BENCH_SECONDS` for the measurement.
REJECTED_REST_S = DEFAULT_REJECTED_BENCH_SECONDS

#: A 403 that says *this key may not use this model*. The opening rest only:
#: ``denied`` is on the doubling ladder in :meth:`Carousel._escalate`, so a
#: refusal that really is permanent reaches :data:`DAILY_COOLDOWN_S` by
#: itself. See :data:`~.quota.DEFAULT_DENIAL_BENCH_SECONDS` for why the short
#: opening is the safe one, and for the two disagreeing constants it replaced.
#:
#: This kind is deliberately **not** in :data:`RETIRING_KINDS`. A model the
#: key may not use says nothing about the models it may — and the carousel's
#: health is per ``provider:model``, so the key goes on working everywhere
#: else in the same second.
DENIED_REST_S = DEFAULT_DENIAL_BENCH_SECONDS

#: How many **consecutive** bare refusals, with no successful call in between,
#: before a key stops being offered at all.
#:
#: Three, and the shape is deliberately the one ``escalate.py`` already uses
#: for widening a deadline: consecutive, same key, self-clearing. One 401 is a
#: coincidence — an OAuth token a second from refreshing, a proxy, a provider
#: incident. Three in a row on the same key with nothing working in between is
#: not, and the cost of being wrong is three requests that fail in
#: milliseconds and are never metered.
#:
#: This governs the **ambiguous** kind only. A provider that used the words —
#: ``revoked`` below — is retired on the first one, because there is nothing
#: ambiguous left to wait for.
REFUSALS_BEFORE_RETIRING = 3

#: The failure kinds that mean *this credential*, not *this moment*. A key
#: resting on one of these is offered last, behind every other healthy key in
#: the pool, until a call on it succeeds and clears ``state["kind"]``.
#:
#: ``daily`` and ``insufficient_quota`` are deliberately absent. Those are
#: clocks: the key is fine and the allowance is not, and demoting it would
#: still be demoting it an hour later when it is the healthiest thing there.
REJECTED_KINDS = frozenset({"auth", "denied", "revoked"})

#: The kinds that put a key out of rotation rather than merely behind the
#: others. ``revoked`` on sight; ``auth`` only after
#: :data:`REFUSALS_BEFORE_RETIRING` of them in a row.
#:
#: ``denied`` is **not** here, and that is the distinction this release exists
#: to draw. "This key may not use this model" is a fact about the *pairing*.
#: The key itself is untouched — it may be the healthiest credential in the
#: account on every other model — so retiring it would throw away a working
#: credential over a permission that was never about the credential.
RETIRING_KINDS = frozenset({"auth", "revoked"})

#: Anything unrecognised. Long enough that a broken key does not spin, short
#: enough that a misclassification costs one turn and not one hour.
OTHER_S = 20.0

#: After an outage, the keys still resting on a 5xx are pulled to roughly this
#: far out, fanned so they do not all return in the same instant.
THAW_BASE_S = 3.0
THAW_FAN_S = 0.4
THAW_FAN_SLOTS = 5

#: An answer that carried nothing is usually a provider hiccup, not a dead key.
#: The first empty from a key is free; the second rests it this long.
EMPTY_REST_S = 3.0

#: How many empty answers one call may rotate through before the empty answer
#: is handed back exactly as the host would have returned it. Without a budget
#: an endlessly-empty provider is an endless loop.
EMPTY_RETRY_BUDGET = 2


# --- what a failure is ------------------------------------------------------

#: Phrases that mean "this key is not a key". Gemini packs these into a **400**,
#: which every host classifier in existence reads as a permanent client error
#: and aborts the run on. It is terminal for the *key* and not for the *run*:
#: quarantine it and rotate, and fourteen good keys carry the turn.
INVALID_KEY_INDICATORS = (
    "api key not valid",
    "api key expired",
    "api_key_invalid",
    "api key not found",
    "invalid api key",
    "invalid_api_key",
    "please renew the api key",
    "invalid authentication",
    "incorrect api key",
    # Anthropic sends "API key is invalid." and DeepSeek "Your api key:
    # ****0000 is invalid" — the same fact with the words the other way round,
    # which none of the phrases above reads. Bounded so the key and the verdict
    # have to sit in one clause: a sentence merely mentioning a key somewhere
    # and the word invalid somewhere else is not evidence.
    "key is invalid",
    "key is no longer valid",
)
# ``unauthorized`` was in that tuple until 1.4.0 and is the reason twenty-one
# healthy keys were quarantined for an hour each. It is the HTTP reason phrase
# for 401, so it arrives on every bare 401 a proxy, a gateway or an expired
# OAuth token produces — and reading it as "this key is not a key" retires a
# working credential over a refresh that was about to succeed.
#
# ``classify.py`` removed it for exactly this reason, with a comment saying
# Hermes' own corpus fails on it (``test_401_classified_as_auth``: the host says
# ``auth``, KAME said ``auth_permanent``). The legacy table kept it. A provider
# that has genuinely retired a key always says more than "Unauthorized", and
# every one of those sentences is matched above.

#: Phrases that mean the account is refused rather than throttled. Same
#: treatment as a daily cap: rest it long, keep using the others.
PERMANENT_DENIAL_INDICATORS = (
    "permission denied",
    "permissiondenied",
    "consumer_suspended",
    "account has been suspended",
    "billing account",
    "has not been used in project",
    # 1.4.0: was the bare stem ``is disabled``, which reaches into any sentence
    # about a feature rather than a credential — "streaming is disabled",
    # "caching is disabled for this model", "thinking is disabled" are all
    # ordinary configuration facts, and each of them benched a healthy key for
    # an hour. The project clause is the part that actually means the
    # credential is refused.
    "is disabled for this project",
    "service_disabled",
    "api_key_service_blocked",
    "api has not been enabled",
    "quota exceeded for quota metric",
)

#: Phrases that mean a throttle of some width.
RATE_LIMIT_INDICATORS = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "resource_exhausted",
    "resource exhausted",
    "too many requests",
    "quota",
    "429",
)

#: Phrases that mean the *daily* or *account* allowance, not the per-minute one.
DAILY_INDICATORS = (
    "per day",
    "perday",
    "daily limit",
    "per-day",
    "requests per day",
    "generaterequestsperday",
    "free_tier_requests",
    "insufficient_quota",
    "insufficient quota",
    "credit balance",
)
# ``exceeded your current quota`` and ``billing`` were in that tuple until
# 1.4.0, and between them they were the single most expensive line in this
# plugin. Google's *per-minute* free-tier 429 reads, word for word:
#
#   "You exceeded your current quota, please check your plan and billing
#    details. For more information on this error, head to: ..."
#
# One sentence, and it trips both markers. Every Gemini throttle — a limit that
# clears in sixty seconds — was therefore read as a daily cap and benched for
# ``daily_cooldown_s``, an hour, on key after key until the pool was empty. The
# user's own telemetry: **1,088 occurrences of that exact message** in nine
# days, and 79 log lines reading ``daily [429] — resting 1h 0m`` against a pool
# of fourteen. It is also the mechanism behind "the pool ran out and never came
# back", and behind the fifteen recorded times a human opened the panel and
# pressed *clear pool* to get working again.
#
# ``classify.py`` already knew. Its ``_AMBIGUOUS_BILLING_PATTERNS`` matches this
# exact sentence and refuses to read it as billing unless the payload *also*
# fails to name a wait or a counter — and its comment says, in as many words,
# that the sentence had already cost one version. That lesson was written down
# in the module that declines most of the time and never carried across to the
# module that decides when it does. Now it is in both.
#
# What replaces it is not another phrase: it is evidence. ``core.evidence``
# harvests the status, the provider's own error code, ``RetryInfo.retryDelay``
# and the quota metadata off the exception, so the ambiguous sentence is
# settled by the payload that always accompanied it instead of by a guess about
# which of its two meanings applies today.

#: Phrases that mean a timeout or connection drop occurred.
TIMEOUT_INDICATORS = (
    "timed out",
    "time out",
    "read operation timed out",
    "streaming request failed",
    "connection timed out",
    "connect timeout",
    "read timeout",
    "deadline exceeded",
)

#: Phrases that mean the request itself is wrong, so no key can answer it.
CONTENT_POLICY_INDICATORS = (
    "content_policy",
    "content policy",
    "content filter",
    "safety",
    "blocked by",
)

_SERVER_STATUS = frozenset({500, 502, 503, 504, 529})

#: Only these, and only after auth and throttling have been ruled out. A 400
#: from Google is far more often an invalid key than a malformed request, which
#: is exactly why the order of the checks in ``is_terminal`` is load-bearing.
#:
#: 405/410/413/415/451/501 were added in 1.0.9 after a real 16-minute loop: a
#: provider answered ``410 Gone`` for a retired model, the status was in none of
#: these sets, so it fell through to ``other``, rested twenty seconds and was
#: tried again -- forever, on every key, because no key can un-retire a model.
#: What the six have in common is that they describe the *request*: the method,
#: the resource, the size, the media type, the legality, the feature. Rotating a
#: credential changes none of those.
_TERMINAL_STATUS = frozenset({400, 404, 405, 410, 413, 415, 422, 451, 501})

#: Hermes' own cross-turn circuit breaker, raised by
#: ``chat_completion_helpers._check_stale_giveup`` once a session has seen
#: ``HERMES_STREAM_STALE_GIVEUP`` (default 5) consecutive stale streams. It
#: raises *before any network attempt*, so rotating into it costs nothing and
#: gains nothing: the counter lives on the agent, not on the key.
#:
#: KAME clears that counter whenever it rotates, on the same reasoning Hermes
#: itself uses when it clears it on a provider swap -- the streak measured the
#: key that is being left behind. If the breaker still fires after that, every
#: key really is wedged, and the honest move is to surface Hermes' own message
#: rather than spin the pool at zero cost per lap.
HOST_BREAKER_INDICATORS = (
    "consecutive stale attempts",
    "provider has been unresponsive",
)

_DURATION = re.compile(
    r"(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m(?!s))?\s*(?:(\d+(?:\.\d+)?)\s*s)?",
    re.IGNORECASE,
)
_RETRY_HINT = re.compile(
    r"retry[\s_-]*(?:after|delay|in)?[\"'\s:=]*(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m)?",
    re.IGNORECASE,
)


def _text_of(error: Any, message: str = "") -> str:
    """Everything about this failure that is worth matching against, lowercased."""
    parts: List[str] = []
    if message:
        parts.append(str(message))
    if error is not None:
        try:
            parts.append(str(error))
        except Exception:  # pragma: no cover — a __str__ that raises
            pass
        for attribute in ("message", "body", "response_text"):
            try:
                value = getattr(error, attribute, None)
            except Exception:
                continue
            if value:
                parts.append(str(value))
    return " ".join(parts).lower()


def _status_of(error: Any, status_code: Optional[int] = None) -> Optional[int]:
    """The HTTP status, from wherever this SDK decided to keep it."""
    if isinstance(status_code, int):
        return status_code
    for attribute in ("status_code", "status", "code", "http_status"):
        try:
            value = getattr(error, attribute, None)
        except Exception:
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    try:
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None) if response is not None else None
        return value if isinstance(value, int) else None
    except Exception:
        return None


def _matches(text: str, indicators: Sequence[str]) -> bool:
    return any(indicator in text for indicator in indicators)


def parse_duration(text: str) -> Optional[float]:
    """Seconds named by a compound duration like ``6m 11.52s``, or ``None``.

    Providers write these three ways in the same week. Reading them is the
    difference between obeying a throttle and guessing at it.
    """
    if not text:
        return None
    match = _DURATION.search(text.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    total = hours * 3600.0 + minutes * 60.0 + seconds
    return total if 0 < total <= HARD_DELAY_CAP_S else None


def extract_delay(error: Any, message: str = "", headers: Any = None) -> Optional[float]:
    """The wait the provider actually asked for, in seconds, or ``None``.

    Three sources in descending order of trustworthiness: an attribute the SDK
    parsed for us, an HTTP header, and finally the error text. The host's own
    classifier reads only the third, which is why a plugin that reads all three
    can size a cooldown the host cannot.
    """
    for attribute in ("retry_after", "retry_delay", "retryDelay"):
        try:
            value = getattr(error, attribute, None)
        except Exception:
            continue
        if value is None:
            continue
        # Google's RetryInfo arrives as a protobuf-ish object, not a number.
        seconds = getattr(value, "seconds", None)
        if seconds is not None:
            try:
                total = float(seconds) + float(getattr(value, "nanos", 0) or 0) / 1e9
            except (TypeError, ValueError):
                total = None
            if total is not None and 0 < total <= HARD_DELAY_CAP_S:
                return total
            continue
        try:
            total = float(value)
        except (TypeError, ValueError):
            total = parse_duration(str(value))
        if total is not None and 0 < total <= HARD_DELAY_CAP_S:
            return total

    for source in (headers, _headers_of(error)):
        value = _header_delay(source)
        if value is not None:
            return value

    match = _RETRY_HINT.search(_text_of(error, message))
    if match:
        try:
            total = float(match.group(1))
        except (TypeError, ValueError):
            return None
        unit = (match.group(2) or "s").lower()
        if unit.startswith("m") and not unit.startswith("ms"):
            total *= 60.0
        if 0 < total <= HARD_DELAY_CAP_S:
            return total
    return None


def _headers_of(error: Any) -> Any:
    if error is None:
        return None
    for attribute in ("headers", "response_headers"):
        try:
            headers = getattr(error, attribute, None)
        except Exception:
            continue
        if headers:
            return headers
    try:
        response = getattr(error, "response", None)
        return getattr(response, "headers", None) if response is not None else None
    except Exception:
        return None


def _header_delay(headers: Any) -> Optional[float]:
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for name in ("retry-after", "x-ratelimit-reset-after", "ratelimit-reset"):
        try:
            raw = getter(name) or getter(name.title())
        except Exception:
            continue
        if raw in (None, ""):
            continue
        try:
            total = float(str(raw).strip())
        except (TypeError, ValueError):
            total = parse_duration(str(raw))
        if total is not None and 0 < total <= HARD_DELAY_CAP_S:
            return total
    return None


def is_auth_failure(error: Any, message: str = "", status_code: Optional[int] = None) -> bool:
    """Whether this failure means the key itself is refused.

    Checked *before* the status code, deliberately. Gemini answers an invalid
    key with ``400 INVALID_ARGUMENT: API key not valid``; reading the number
    first classifies it as a malformed request and aborts a run that fourteen
    healthy keys could have finished.
    """
    text = _text_of(error, message)
    if _matches(text, INVALID_KEY_INDICATORS):
        return True
    status = _status_of(error, status_code)
    if status == 401:
        return True
    # A 403 is auth only when it is not a throttle wearing a 403 — some
    # providers return 403 for spending limits, which is a quota, not a key.
    return status == 403 and not _matches(text, RATE_LIMIT_INDICATORS)


def is_terminal(error: Any, message: str = "", status_code: Optional[int] = None) -> bool:
    """Whether no key on earth could answer this request.

    Order is the whole point: auth first (a bad key is not a bad request),
    then throttling (a 429 is never terminal), and only then the small set of
    status codes that genuinely describe the *request*. Everything else — a
    timeout, a 5xx, a dropped connection, an unrecognised refusal — is a
    reason to rotate, not a reason to stop.
    """
    text = _text_of(error, message)
    if _matches(text, HOST_BREAKER_INDICATORS):
        # Checked before auth because it carries no status and no credential:
        # it is the host refusing to make the call at all.
        return True
    if is_auth_failure(error, message, status_code):
        return False
    if _matches(text, TIMEOUT_INDICATORS):
        return False
    if _matches(text, CONTENT_POLICY_INDICATORS):
        return True
    status = _status_of(error, status_code)
    if status in _SERVER_STATUS:
        return False
    if status == 429 or _matches(text, RATE_LIMIT_INDICATORS):
        return False
    return status in _TERMINAL_STATUS


def classify(
    error: Any,
    message: str = "",
    status_code: Optional[int] = None,
    headers: Any = None,
    *,
    daily_cooldown_s: float = DAILY_COOLDOWN_S,
) -> Tuple[float, str, Optional[int]]:
    """``(delay, kind, status)`` for one failure.

    ``kind`` is one of ``timeout``, ``server``, ``per_minute``, ``daily``,
    ``insufficient_quota``, ``denied``, ``auth``, ``host_breaker``, ``other``
    — the vocabulary ``mark`` escalates against.

    The 5xx check runs **before** the 429 check on purpose. A provider under
    load has been seen to return 503 with a body mentioning "quota"; reading
    the words first turns a two-minute outage into an hour-long bench across
    every key at once.
    """
    text = _text_of(error, message)
    status = _status_of(error, status_code)

    if _matches(text, HOST_BREAKER_INDICATORS):
        # First, and deliberately ahead of the timeout check: the breaker's own
        # message talks about unresponsiveness, and reading it as a timeout
        # would cool a key for a stall the key had no part in. There is no
        # cooldown worth applying here -- ``is_terminal`` stops the turn -- so
        # the delay is nominal.
        return 0.0, "host_breaker", status
    # The class names used to be an inline set of five here — the same idea as
    # ``catalog``'s exception table, written once by hand in the wrong module.
    # Two tables of the same kind of fact drift, and the one nobody remembers
    # is the one that goes stale, so there is now one. The catalogue's set is a
    # superset: it also knows ``APIConnectionError`` and ``ConnectError``,
    # which used to fall through to the twenty-second rest for the unrecognised
    # when three seconds and a rotation is the whole of the right answer.
    _klass = type(error).__name__ if error is not None else ""
    _reading = catalog.read_exception_class(_klass)
    if (
        _reading is not None and _reading.family == catalog.TIMEOUT
    ) or _matches(text, TIMEOUT_INDICATORS):
        return TIMEOUT_S, "timeout", status

    if status in _SERVER_STATUS or _matches(
        text,
        (
            "service unavailable",
            "serviceunavailable",
            "internal server error",
            "bad gateway",
            "gateway timeout",
            "overloaded",
        ),
    ):
        return SERVER_BASE_S, "server", (status or 503)

    # The provider used the words. This is the only branch that may retire a
    # key on sight, and the vocabulary it reads is deliberately narrow — see
    # the note under INVALID_KEY_INDICATORS about ``unauthorized``, which was
    # in that tuple until 1.4.0 and cost twenty-one healthy keys an hour each.
    if _matches(text, INVALID_KEY_INDICATORS):
        return REJECTED_REST_S, "revoked", (status or 401)

    if is_auth_failure(error, message, status_code):
        # A bare 401 or 403: the provider refused and said nothing about why,
        # so this is the *ambiguous* kind. Short rest, demoted, and retired
        # only once REFUSALS_BEFORE_RETIRING of them arrive in a row.
        return REJECTED_REST_S, "auth", (status or 401)

    if status == 429 or _matches(text, RATE_LIMIT_INDICATORS):
        if _matches(text, DAILY_INDICATORS):
            kind = "insufficient_quota" if "insufficient" in text else "daily"
            # The parsed delay is deliberately dropped here. See the module
            # docstring: a daily cap that claims to clear in seconds is the
            # single most expensive lie a provider tells a rotation engine.
            return daily_cooldown_s, kind, (status or 429)
        parsed = extract_delay(error, message, headers)
        return (parsed if parsed is not None else OTHER_S), "per_minute", (status or 429)

    if _matches(text, PERMANENT_DENIAL_INDICATORS):
        # "This key may not use this model." The key is not the problem, the
        # pairing is, so the hour is honest here — nothing about an
        # authorisation moves on its own — and it costs nothing, because the
        # carousel's health is per provider:model.
        return DENIED_REST_S, "denied", (status or 403)

    return OTHER_S, "other", status


# --- the state --------------------------------------------------------------


def _fresh(now: float) -> Dict[str, Any]:
    return {
        "sick_until": 0.0,
        "last_used": 0.0,
        "last_sick_at": 0.0,
        "request_log": [],
        "consecutive_rl": 0,
        "consecutive_server": 0,
        # 1.6.0.1. Bare refusals in a row with no successful call between
        # them. Reset by any success, which is what makes retiring on it
        # self-clearing rather than a verdict nothing can appeal.
        "consecutive_refusals": 0,
        # Out of rotation until something changes: a call on it succeeds, the
        # config stops declaring it, or the pool is cleared. Never means the
        # key was deleted — this plugin does not write credentials.
        "retired": False,
        "kind": "",
        "successes": 0,
        "failures": 0,
        # When a select() last carried this key among its candidates — not
        # when it was last *chosen*, which is ``last_used``. The difference is
        # what tells a key removed from the config apart from a key that is
        # simply resting: both go unused, only one stops being offered.
        "last_offered": now,
    }


class Carousel:
    """Per-``provider:model`` key health, and the rule for choosing the next key.

    One instance is shared by every call in the process, so the lock is real
    and every mutation happens under it. Selection stamps the chosen key
    *inside* the lock — that is the anti-dogpile guarantee, and moving the
    stamp outside would quietly reintroduce two turns picking the same key.
    """

    def __init__(self, *, daily_cooldown_s: float = DAILY_COOLDOWN_S) -> None:
        self._lock = threading.RLock()
        self._pools: Dict[str, Dict[str, Dict[str, Any]]] = {}
        #: The longest rest this identity's provider has ever *asked* for on a
        #: throttle, per ``provider:model``. It is not a cooldown and nothing
        #: rests for it — it is the ceiling on what KAME is allowed to invent
        #: when a later throttle arrives with no number at all. See
        #: :meth:`_escalate`.
        self._stated_rl_ceiling: Dict[str, float] = {}
        self.daily_cooldown_s = float(daily_cooldown_s)
        self.selections = 0
        self.rotations = 0

    # -- identity --------------------------------------------------------

    @staticmethod
    def identity(provider: Any, model: Any) -> str:
        """The health bucket a call belongs to.

        ``provider:model`` and not ``provider``: several providers meter a key
        per model, so a key spent on one model is still whole on another.
        """
        return f"{str(provider or '?').strip().lower()}:{str(model or '?').strip().lower()}"

    def _pool_for(self, identity: str, keys: Sequence[str], now: float) -> Dict[str, Dict[str, Any]]:
        pool = self._pools.setdefault(identity, {})
        for key in keys:
            if key not in pool:
                pool[key] = _fresh(now)
        return pool

    def _mirror(
        self, pool: Dict[str, Dict[str, Any]], keys: Sequence[str], now: float
    ) -> None:
        """Drop the keys nothing has offered for :data:`MIRROR_GRACE_S`.

        The pool is a view of the credential list, not an archive of it. A key
        edited out of the config is a credential nowhere else, and keeping it
        means its last failure is carried for ever: it stays counted in
        ``keys``, stays counted in ``invalid``, and reads on the panel as a
        broken key the user has already replaced. Before the split fix in
        ``candidates()`` it was worse than bookkeeping — the comma-joined
        parent list itself sat in the pool as one malformed credential.

        Two things this deliberately does not do:

        * **An empty candidate set mirrors nothing.** The host failing to load
          a pool for one call is not evidence that every key was deleted.
        * **Absence from one call is not removal.** One ``provider:model`` can
          be reached by two agents carrying different lists, so a row is only
          dropped once nothing has offered it for a while — see
          :data:`MIRROR_GRACE_S`.
        """
        if not keys:
            return
        wanted = set(keys)
        stale = now - MIRROR_GRACE_S
        for key, state in list(pool.items()):
            if key in wanted:
                state["last_offered"] = now
            elif state.get("last_offered", now) <= stale:
                del pool[key]

    # -- selection -------------------------------------------------------

    def select(
        self, identity: str, keys: Sequence[str], now: Optional[float] = None
    ) -> Tuple[Optional[str], str]:
        """``(key, status)`` — the healthiest key, chosen fresh for this call.

        ``status`` is ``"SUCCESS"`` when the key returned is believed healthy,
        ``"EXHAUSTED"`` when every key is resting and the one returned is
        merely the soonest to recover, and ``"EMPTY"`` when there is nothing to
        choose from.

        Fewest-requests-in-the-window first, least-recently-used to break the
        tie. Both halves matter: load alone would let a key that answered once
        an hour ago and a key that answered once a second ago look identical,
        and age alone ignores the rate limit the window exists to respect.
        """
        usable = [k for k in keys if k]
        if not usable:
            return None, "EMPTY"
        now = time.time() if now is None else now
        cutoff = now - RPM_WINDOW_S

        with self._lock:
            pool = self._pool_for(identity, usable, now)
            self._mirror(pool, usable, now)
            for key in usable:
                pool[key]["request_log"] = [t for t in pool[key]["request_log"] if t > cutoff]

            healthy = [k for k in usable if pool[k]["sick_until"] < now]

            # 1.6.0.1. A key the provider refused as a credential is not
            # merely unlucky, and the demotion below — offering it last —
            # still offers it. Three bare refusals in a row, or one where the
            # provider used the words, and it stops being a candidate at all.
            #
            # **Retiring has to outrank being ready, or it buys nothing.** The
            # demotion already handles the easy case, where a working key is
            # sitting there unused. The case it gets wrong is the one that
            # actually happens: the working key is resting off a throttle for
            # twenty seconds, the refused key's own rest has lapsed, so the
            # refused key is the only "healthy" one and the call goes to a
            # credential we have already been told is dead. That spends a
            # request and hands the user an error, where waiting twenty
            # seconds would have handed them an answer. So a retired key is
            # removed from consideration even when that leaves nothing ready,
            # and the wait below is for a key that can actually serve.
            #
            # The escape hatch is the whole safety argument for retiring at
            # all, and it is the condition rather than a comment: this only
            # applies while some key is *not* retired. If every key has been
            # refused, every key is offered again — the request goes out and
            # the provider's own error comes back, exactly as it would with no
            # plugin installed. Retiring can never take a pool to zero, so the
            # worst case of a wrong verdict is no worse than not having the
            # rule, and somebody who mistypes their only key gets an error
            # from the provider rather than silence from a plugin that decided.
            standing = [k for k in usable if not pool[k].get("retired")]
            if standing:
                healthy = [k for k in healthy if not pool[k].get("retired")]
                usable = standing

            if not healthy:
                soonest = min(usable, key=lambda k: pool[k]["sick_until"])
                return soonest, "EXHAUSTED"

            # Refused credentials go last, and that ordering is what lets
            # ``REJECTED_REST_S`` be twenty seconds instead of an hour.
            #
            # Without it the shorter bench would be actively worse than the
            # long one. A key that answered 401 comes back with an empty
            # request window and the oldest ``last_used`` in the pool, which
            # is precisely the profile ``min`` below reaches for — so the one
            # key known not to work would be the very first one tried, every
            # time its bench lapsed. Demoted, it is reached only when every
            # other healthy key is busier, which on any pool with a working
            # key in it means "not until there is nothing better", and on a
            # pool where every key was refused means "immediately", because
            # then they are all equal and there is nothing to lose.
            chosen = min(
                healthy,
                key=lambda k: (
                    1 if pool[k].get("kind") in REJECTED_KINDS else 0,
                    len(pool[k]["request_log"]),
                    pool[k]["last_used"],
                ),
            )
            # Stamped under the lock. A concurrent turn entering select() a
            # microsecond later now sees this key as both busier and newer,
            # and picks a different one.
            pool[chosen]["last_used"] = now
            pool[chosen]["request_log"].append(now)
            self.selections += 1
            return chosen, "SUCCESS"

    # -- learning --------------------------------------------------------

    def mark(
        self,
        identity: str,
        key: str,
        ok: bool,
        delay: float = 0.0,
        kind: str = "",
        now: Optional[float] = None,
    ) -> float:
        """Record one outcome and return the cooldown actually applied.

        The returned number is what got *stored*, not what was asked for — a
        caller that logs the requested delay while the pool stored something
        else is a caller that lies in the log. The two differ whenever backoff
        escalates, whenever a cap bites, and whenever a longer existing
        cooldown wins.
        """
        if not key:
            return 0.0
        now = time.time() if now is None else now
        with self._lock:
            # A mark creates the row when it has to. It cannot make a key
            # selectable: ``select`` only ever chooses from the candidates it
            # was handed this call, so a row nothing declares is never picked,
            # and ``_mirror`` retires it once nothing offers it. Refusing to
            # record the outcome instead would lose the one thing a failure is
            # good for — 1.1.3's ``_rest_unless_it_is_the_only_one`` marks a
            # key the moment a stream drops, and a mark that quietly did
            # nothing there would spend the cooldown on nobody.
            pool = self._pool_for(identity, [key], now)
            state = pool[key]

            if ok:
                state["sick_until"] = 0.0
                state["consecutive_rl"] = 0
                state["consecutive_server"] = 0
                # One good answer is the whole appeal. A key retired on
                # evidence that turns out to have been a provider incident
                # comes back the moment it works — and it will be reached,
                # because the escape hatch in select() offers retired keys
                # whenever nothing else can serve.
                state["consecutive_refusals"] = 0
                state["retired"] = False
                state["kind"] = ""
                state["successes"] += 1
                # A second stamp on success biases the next selection away from
                # the key that just answered, which is what spreads a burst of
                # sequential turns across the pool instead of pinning them to
                # whichever key happened to be freshest.
                state["request_log"].append(now)
                return 0.0

            state["failures"] += 1
            state["last_sick_at"] = now
            state["kind"] = kind
            if kind in RETIRING_KINDS:
                state["consecutive_refusals"] += 1
                # ``revoked`` is the provider having said the words, so there
                # is nothing left to accumulate evidence about. A bare refusal
                # has to happen REFUSALS_BEFORE_RETIRING times in a row.
                if kind == "revoked" or state["consecutive_refusals"] >= REFUSALS_BEFORE_RETIRING:
                    state["retired"] = True
            else:
                # Any other kind of failure breaks the run. A key that is
                # rate-limited between two 401s has not been refused three
                # times in a row, and reading it that way would retire keys
                # on a mixture of unrelated evidence.
                state["consecutive_refusals"] = 0
            if (
                kind in ("per_minute", "rate_limit")
                and 0.0 < delay <= RL_BACKOFF_CAP_S
            ):
                # Learned here rather than in ``_escalate`` because the number
                # is the provider's whatever this key does with it, and the
                # lesson belongs to the identity, not to the credential that
                # happened to collect it.
                #
                # The upper bound is not a clamp, it is a filter on what may
                # teach. A number this long is not describing a rolling
                # window: on Gemini a *daily* cap classifies as ``rate_limit``
                # too, and arrives sized at an hour. Letting that hour in
                # would mean one exhausted day teaching every terse throttle
                # afterwards to rest for an hour, which is the original defect
                # wearing a different hat.
                self._stated_rl_ceiling[identity] = max(
                    self._stated_rl_ceiling.get(identity, 0.0), float(delay)
                )
            applied = self._escalate(
                state, delay, kind, ceiling=self._stated_rl_ceiling.get(identity)
            )
            applied = max(0.0, min(applied, HARD_DELAY_CAP_S))
            # There is deliberately no shorter cap for a pool of one here.
            # The host has one (``EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS`` =
            # 60) and copying it would undo 1.1.3: a cooldown that came from
            # the provider's own words — a daily quota, an auth refusal, a
            # Retry-After — binds no matter what else is in the pool, and
            # retrying it once a minute for an afternoon spends the quota it
            # is waiting for. The cooldowns that exist only to move the next
            # call elsewhere are the ones with nowhere to go when there is one
            # key, and ``dispatch_binding._rest_unless_it_is_the_only_one``
            # already drops exactly those and no others.
            # Never shortens. A key that said "out for the day" must not be
            # released early because a softer refusal arrived afterwards.
            state["sick_until"] = max(state.get("sick_until", 0.0), now + applied)
            self.rotations += 1
            return applied

    def _escalate(
        self,
        state: Dict[str, Any],
        delay: float,
        kind: str,
        *,
        ceiling: Optional[float] = None,
    ) -> float:
        """How long this key rests, given how many times it has said this lately.

        ``ceiling`` is the longest rest this identity's provider has actually
        asked for on a throttle, or ``None`` before it has ever asked for one.
        It bounds the invented ladder and nothing else.
        """
        if kind in ("daily", "insufficient_quota", "denied", "auth", "revoked"):
            state["consecutive_rl"] += 1
            strikes = state["consecutive_rl"]
            grown = DAILY_BASE_S * (2 ** max(0, strikes - 1))
            return min(max(delay, grown), self.daily_cooldown_s)

        # ``rate_limit`` is the same family under the modern classifier's name
        # for it. Until 1.4.0 it was in none of these branches and fell through
        # to the flat rest at the bottom, which returns ``max(delay, 0.0)`` —
        # and a throttle the payload could not size arrives here with
        # ``delay = 0``. So the key was benched for **zero seconds**: twenty
        # such lines in the user's log, reading
        # ``rate_limit [429] — resting 0s, taking the next key``, which is a
        # pool burning through every credential it has in a few hundred
        # milliseconds and then declaring itself exhausted.
        #
        # The cause was never a missing number. It was two vocabularies:
        # ``classify.Verdict.reason`` says ``rate_limit`` and this ladder
        # spoke only ``per_minute``, so the two halves of the same plugin
        # disagreed about the name of the commonest failure there is. A
        # release note blamed an empty error string and added a fallback for
        # it; the fallback was correct and the bench stayed at zero, because
        # the string was never what routed the kind.
        # 1.6.0.3: this branch used to read ``min(max(delay, 1.0) * 2 **
        # (strikes - 1), RL_BACKOFF_CAP_S)`` — it *multiplied* the provider's
        # own number once a key said "rate limit" twice in a row. Its two
        # sibling branches, ``daily`` above and ``server`` below, have always
        # taken ``max(delay, base * 2 ** n)``: the ladder is a floor for the
        # case the payload sized nothing, and a number the provider stated
        # outranks it. Only this branch disagreed, and it is the branch that
        # runs on the commonest failure there is.
        #
        # The owner's log for 1.6.0.2 is what settles it. Across 46 minutes
        # Gemini returned 340 throttles, every one of them carrying a freshly
        # computed ``Please retry in Ns`` between 1.5s and 59.8s — a rolling
        # window recomputing the wait on each refusal, which is the provider
        # answering the question correctly every single time. KAME held keys
        # for 5m 0s on ten of them, and for 1m 4s, 1m 7s, 1m 10s and 1m 34s on
        # others: longer than the provider had *ever* asked. With the pool
        # benched that far out the agent then sat in ``_wait_for_a_key`` for
        # 468s across 33 waits, which is the stall the owner reported.
        #
        # Repeating a throttle is not evidence that the provider's number is
        # wrong. On a rolling window it is the ordinary case: the key is asked
        # again while its window is still full, and the provider says so again
        # with a new, smaller number. Widening on that reads a restatement as
        # a refutation. Measured refutation already has an owner — the journal
        # counts ``under_predictions`` and ``escalate.stretch`` widens only
        # after two of them — and that mechanism is unaffected here. What is
        # removed is this branch's habit of escalating on repetition alone.
        # There is no exponential here any more, and that is the point.
        #
        # A rate limit has two regimes and no middle. Either it is a rolling
        # window, which closes in seconds and whose length the provider will
        # tell you, or it is a daily cap, which closes in hours and is a
        # different counter with a different name. No provider documents
        # "come back in five minutes". The ladder that used to live here —
        # 1s, 2s, 4s … 300s — interpolated between two regimes that have
        # nothing between them, and every number on it was invented.
        #
        # What replaced it says the same thing in one line: **every rest is a
        # number somebody measured.** Either the provider stated it for this
        # refusal, or the provider stated it for an earlier one on the same
        # model, or — only when it has never stated one at all — the flat
        # re-probe that `quota` already uses for exactly this case.
        #
        # Erring short is deliberate and it is the asymmetry that governs the
        # whole plugin: a rest that is too short costs one request that fails
        # in milliseconds; a rest that is too long costs a healthy key for its
        # whole duration, silently. Being wrong in the cheap direction is the
        # correct bet.
        #
        # Something downstream does catch it now — ``escalate.stretch``,
        # widening a bench the journal has recorded as measured short twice —
        # and 1.6.0.3 is also the release that made the journal able to see
        # this path at all. It was fed only from ``pool_binding._remember``,
        # which runs when the *host* benches a credential; the rotations this
        # method sizes happen inside a turn and never got there, 74 of them
        # and 0 rows in the owner's 1.6.0.2 run.
        #
        # None of which changes the rule below. A correction that arrives
        # after two refusals is not a reason to be wrong on the first one.
        if kind in ("per_minute", "rate_limit"):
            state["consecutive_rl"] += 1
            if delay > 0.0:
                # Stated for this very refusal. Nothing outranks it. The floor
                # still applies, because a sub-second rest is a spin rather
                # than a cooldown.
                return max(delay, RL_BASE_S)
            if ceiling is not None and ceiling > 0.0:
                # Stated for an earlier one. The owner's log is why this beats
                # any constant: of 400 Gemini throttles in 46 minutes, 232
                # arrived as the terse "Resource has been exhausted (e.g.
                # check quota)." with no number, and 168 arrived as the same
                # refusal spelled out — never once above 59.8s. The terse form
                # is not a different condition, it is the same condition
                # worded shorter, and the 168 already answered it.
                return max(min(float(ceiling), RL_BACKOFF_CAP_S), RL_BASE_S)
            # Never stated one. ``quota`` reaches the same number by the same
            # reasoning for the same payload, and the two agreeing is what
            # keeps the in-turn rest and the across-turn bench from telling
            # the user two different stories about one refusal.
            return UNSIZED_THROTTLE_REST_S

        if kind == "server":
            state["consecutive_server"] += 1
            strikes = state["consecutive_server"]
            grown = SERVER_BASE_S * (2 ** max(0, strikes - 1))
            return min(max(delay, grown), SERVER_BACKOFF_CAP_S)

        # timeout / other / empty — no escalation ladder, just the flat rest.
        return max(delay, 0.0)

    def thaw_server_cooled(
        self, identity: str, except_key: str, now: Optional[float] = None
    ) -> int:
        """Pull 5xx-rested keys forward after the outage ends. Returns how many.

        Scoped to ``consecutive_server`` on purpose. When one key answers after
        a provider-wide outage, the *provider* recovered — every other key
        resting on a 5xx is almost certainly well too, and making them serve
        out a 90-second sentence turns a recovered pool into a trickle. A key
        resting on a quota or an auth failure learned something about *itself*
        and is never touched here.

        Only ever shortens. A key whose 5xx cooldown is already shorter than
        the thaw target keeps its own, sooner deadline.
        """
        now = time.time() if now is None else now
        thawed = 0
        with self._lock:
            pool = self._pools.get(identity) or {}
            for index, (key, state) in enumerate(pool.items()):
                if key == except_key or state.get("consecutive_server", 0) <= 0:
                    continue
                if state.get("sick_until", 0.0) <= now:
                    continue
                target = now + THAW_BASE_S + (index % THAW_FAN_SLOTS) * THAW_FAN_S
                if target < state["sick_until"]:
                    state["sick_until"] = target
                    thawed += 1
        return thawed

    # -- reporting -------------------------------------------------------

    def next_recovery_seconds(
        self, identity: str, keys: Sequence[str], now: Optional[float] = None
    ) -> Optional[float]:
        """Seconds until the soonest key is usable again, or ``None`` if one is now."""
        usable = [k for k in keys if k]
        if not usable:
            return None
        now = time.time() if now is None else now
        with self._lock:
            pool = self._pools.get(identity) or {}
            deadlines = [pool.get(k, {}).get("sick_until", 0.0) for k in usable]
        soonest = min(deadlines) if deadlines else 0.0
        return None if soonest <= now else soonest - now

    def healthy_count(
        self, identity: str, keys: Sequence[str], now: Optional[float] = None
    ) -> int:
        now = time.time() if now is None else now
        with self._lock:
            pool = self._pools.get(identity) or {}
            return sum(1 for k in keys if k and pool.get(k, {}).get("sick_until", 0.0) < now)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """A copy of the whole bench, for ``/kame-quota``. Keys are never included."""
        now = time.time() if now is None else now
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for identity, pool in self._pools.items():
                healthy = sum(1 for s in pool.values() if s["sick_until"] < now)
                # The soonest deadline still ahead of us, so a reader can be
                # told when the pool comes back rather than only that it is
                # away. ``None`` when something is usable now, which is the
                # same convention ``next_recovery_seconds`` uses.
                resting_for = [
                    s["sick_until"] - now
                    for s in pool.values()
                    if s["sick_until"] >= now
                ]
                # A key benched for ``auth`` is not resting, it is broken: no
                # cooldown repairs a credential the provider has rejected, so
                # the hour it serves will be followed by another hour. Counted
                # apart from ``resting`` since 1.1.1 so a reader is told to
                # replace it rather than to wait for it.
                invalid = [
                    fingerprint(k) for k, s in pool.items()
                    if s["kind"] in REJECTED_KINDS
                ]
                # 1.6.0.1. Out of rotation, not merely benched. The two are
                # different sentences on a screen — one asks the reader to
                # wait, the other asks them to replace a key — and until this
                # release the panel could only say the first.
                retired = [
                    fingerprint(k) for k, s in pool.items() if s.get("retired")
                ]
                out[identity] = {
                    "keys": len(pool),
                    "healthy": healthy,
                    "resting": len(pool) - healthy,
                    "soonest": min(resting_for) if (resting_for and not healthy) else None,
                    "successes": sum(s["successes"] for s in pool.values()),
                    "failures": sum(s["failures"] for s in pool.values()),
                    "kinds": sorted({s["kind"] for s in pool.values() if s["kind"]}),
                    "invalid": len(invalid),
                    "invalid_keys": sorted(invalid),
                    "retired": len(retired),
                    "retired_keys": sorted(retired),
                    # Seconds since this pool was last asked for a key, so a
                    # status bar can show what is in use and leave out what was
                    # touched once an hour ago. ``None`` when it never has been.
                    "idle_for": (
                        None
                        if not any(s["last_used"] for s in pool.values())
                        else now - max(s["last_used"] for s in pool.values())
                    ),
                }
        return out

    def is_retired(self, identity: str, key: str) -> bool:
        """Whether this key has stopped being offered on this identity.

        Read rather than inferred, because the two facts a caller wants to
        tell apart — "resting, come back later" and "out until you replace
        it" — look identical from the outside: both are a key that will not
        be chosen. Only this says which sentence to put on the screen.
        """
        if not key:
            return False
        with self._lock:
            pool = self._pools.get(identity) or {}
            state = pool.get(key)
            return bool(state and state.get("retired"))

    def forget(self, identity: Optional[str] = None) -> None:
        """Drop the bench. For tests, and for a pool that was replaced wholesale."""
        with self._lock:
            if identity is None:
                self._pools.clear()
            else:
                self._pools.pop(identity, None)


def fingerprint(key: Any) -> str:
    """A stable, non-reversible label for one key, safe to log.

    Never the key. Never a prefix of the key either — a prefix of an API key
    is still key material, and a log that carries eight real characters of a
    live credential is a log that cannot be pasted into an issue.
    """
    text = str(key or "")
    if not text:
        return "key:-"
    import hashlib

    return "key:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:6]


def format_duration(seconds: Optional[float]) -> str:
    """``6m 11s`` — for a human reading a status line, not for a machine."""
    if seconds is None:
        return "unknown"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


#: The one instance the bindings share. A module-level singleton because key
#: health is a property of the *process*, not of any one agent: the main loop,
#: the auxiliary lane and every subagent are spending the same quota on the
#: same keys, and three private benches would each learn the same lesson.
ENGINE = Carousel()


__all__ = [
    "Carousel",
    "ENGINE",
    "RPM_WINDOW_S",
    "DAILY_COOLDOWN_S",
    "HARD_DELAY_CAP_S",
    "SERVER_BACKOFF_CAP_S",
    "RL_BACKOFF_CAP_S",
    "RL_BASE_S",
    "UNSIZED_THROTTLE_REST_S",
    "TIMEOUT_S",
    "OTHER_S",
    "EMPTY_RETRY_BUDGET",
    "EMPTY_REST_S",
    "REJECTED_REST_S",
    "REJECTED_KINDS",
    "RETIRING_KINDS",
    "REFUSALS_BEFORE_RETIRING",
    "DENIED_REST_S",
    "INVALID_KEY_INDICATORS",
    "classify",
    "extract_delay",
    "fingerprint",
    "format_duration",
    "is_auth_failure",
    "is_terminal",
    "parse_duration",
]
