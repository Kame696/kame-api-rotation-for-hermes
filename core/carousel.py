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

**The provider's own delay is trusted exactly once, and never for a daily cap.**
A per-minute throttle that says "retry in 6s" is honest and worth obeying on
the first strike; from the second strike the same key saying the same thing is
evidence the window is wider than it claims, so the delay escalates. A *daily*
cap is different: Google returns a short retryDelay on a daily-quota 429. Believing it produces a key that returns to
rotation, fails, and repeats — hourly, all day. So for ``daily`` and
``insufficient_quota`` the parsed delay is discarded and the configured
cooldown is used instead. This is v1.0.5's rule and it was learned the hard way.

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
RL_BACKOFF_CAP_S = 300.0

#: A daily refusal escalates from this base toward ``DAILY_COOLDOWN_S``.
DAILY_BASE_S = 20.0

#: A read timeout is nearly always transient. Rest three seconds and rotate.
TIMEOUT_S = 3.0

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
    "unauthorized",
    "invalid authentication",
    "incorrect api key",
)

#: Phrases that mean the account is refused rather than throttled. Same
#: treatment as a daily cap: rest it long, keep using the others.
PERMANENT_DENIAL_INDICATORS = (
    "permission denied",
    "permissiondenied",
    "consumer_suspended",
    "account has been suspended",
    "billing account",
    "has not been used in project",
    "is disabled",
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
    "exceeded your current quota",
    "billing",
    "credit balance",
)

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
    if (
        error is not None
        and type(error).__name__ in {"TimeoutError", "CancelledError", "ReadTimeout", "ConnectTimeout", "StreamTimeout"}
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

    if is_auth_failure(error, message, status_code):
        return daily_cooldown_s, "auth", (status or 401)

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
        return daily_cooldown_s, "denied", (status or 403)

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
            if not healthy:
                soonest = min(usable, key=lambda k: pool[k]["sick_until"])
                return soonest, "EXHAUSTED"

            chosen = min(healthy, key=lambda k: (len(pool[k]["request_log"]), pool[k]["last_used"]))
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
            applied = self._escalate(state, delay, kind)
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

    def _escalate(self, state: Dict[str, Any], delay: float, kind: str) -> float:
        """How long this key rests, given how many times it has said this lately."""
        if kind in ("daily", "insufficient_quota", "denied", "auth"):
            state["consecutive_rl"] += 1
            strikes = state["consecutive_rl"]
            grown = DAILY_BASE_S * (2 ** max(0, strikes - 1))
            return min(max(delay, grown), self.daily_cooldown_s)

        if kind == "per_minute":
            state["consecutive_rl"] += 1
            strikes = state["consecutive_rl"]
            if strikes <= 1:
                # First strike: the provider's own number is honest and
                # obeying it is faster than any guess we could make.
                return max(delay, 1.0)
            grown = max(delay, 1.0) * (2 ** (strikes - 1))
            return min(grown, RL_BACKOFF_CAP_S)

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
                    fingerprint(k) for k, s in pool.items() if s["kind"] == "auth"
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
    "TIMEOUT_S",
    "OTHER_S",
    "EMPTY_RETRY_BUDGET",
    "EMPTY_REST_S",
    "INVALID_KEY_INDICATORS",
    "classify",
    "extract_delay",
    "fingerprint",
    "format_duration",
    "is_auth_failure",
    "is_terminal",
    "parse_duration",
]
