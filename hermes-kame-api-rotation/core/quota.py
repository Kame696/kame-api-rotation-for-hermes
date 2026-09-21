"""Reading a provider's own words about when a credential may be used again.

Provider-agnostic on purpose, and that is the whole design. There is no list
of supported providers anywhere in this module, because a list is a promise
to be wrong about whichever provider is not on it — including every provider
that does not exist yet. What exists instead is a cascade of *evidence*: an
exception attribute, an HTTP header, a structured error body, a sentence.
Whoever supplies one gets read; whoever supplies none gets declined.

The cascade runs strongest evidence first:

  1. exception attributes  — ``retry_after`` (litellm/OpenAI/Anthropic SDKs),
                             ``retry_delay`` (Google ``RetryInfo`` Duration)
  2. HTTP headers          — ``Retry-After`` (RFC 7231: seconds *or* an
                             HTTP-date), plus any rate-limit reset header
  3. structured body       — ``details[].retryDelay`` (``google.rpc.RetryInfo``)
  4. free text             — a duration after a retry keyword

Anything numeric is bounded before it leaves this module. A provider that
says "wait 99999999 seconds" is either broken or being parsed wrong, and
neither is a reason to lose a credential for three years.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

# ── bounds ────────────────────────────────────────────────────────────────

# A relative delay the provider hands us. 24h matches the longest honest
# value seen in the wild (OpenAI account-level quota).
MAX_RELATIVE_DELAY_SECONDS = 24 * 60 * 60

# An absolute reset timestamp may legitimately sit further out — weekly
# subscription windows exist (OpenCode Go, Copilot).
MAX_ABSOLUTE_HORIZON_SECONDS = 24 * 60 * 60  # 1 day — matches Agent Zero _KAME_HARD_DELAY_CAP_S

MIN_BENCH_SECONDS = 1.0

# What a deadline was computed from, when it matters to somebody downstream.
# ``anchor`` is the one that does: the deadline is a wall-clock instant this
# module believes the provider's counter rolls at, not a stopwatch reading.
# Being wrong about an anchor is being wrong by minutes; being wrong about a
# stopwatch is being wrong by a proportion. ``escalate`` corrects them
# differently and cannot tell them apart from the window alone, because a
# non-Google daily cap is a one-hour re-probe wearing the same window name.
SOURCE_ANCHOR = "anchor"

# A per-minute window recovers in a minute; the extra five seconds absorb
# clock skew between us and the provider.
DEFAULT_PER_MINUTE_BENCH_SECONDS = 65.0

# A throttle the provider named but did not size, and whose window nobody
# could identify. **The most important number in this file**, because until
# 1.6.0.2 there was no number here at all.
#
# ``classify`` used to return such a throttle with ``reset_at`` unset, and
# said so in a comment: "bench it for nothing". There is no "bench it for
# nothing" in this host. ``agent/credential_pool.py`` reads the deadline KAME
# supplies (``_exhausted_until``, :426) and, finding none, falls back to a
# fixed table (``_exhausted_ttl``, :333) whose entry for a 429 is
# ``EXHAUSTED_TTL_429_SECONDS = 60 * 60``. Silence was not neutrality. Silence
# was an hour, on every key, on the commonest refusal there is — measured on
# the owner's 2026-09-03 14:39 run, where nineteen bare ``RESOURCE_EXHAUSTED``
# replies each cost a credential an hour it did not owe.
#
# Was twenty seconds, for the same reason ``DEFAULT_REJECTED_BENCH_SECONDS``
# is twenty: re-probing costs exactly one failed request, over-benching costs
# the use of a healthy key, and the two are not close. A throttle nobody
# could size is far more often a burst limit that clears in seconds than a
# daily cap — a daily cap says so, in a quota id or in a stated delay, and
# both of those are read before this number is ever reached.
#
# **1.8.0.1: measured too short — then measured wrong.** The owner's 1.8.0.0
# session, 2026-09-19 22:29-22:40 local: 172 real calls, 39 answered, 133
# rotated, 129 of them bare ``429 RESOURCE_EXHAUSTED`` with no ``quotaId``,
# no ``retryDelay`` and no header but ``date``, plus 5x503. 1.8.0.1 raised
# the bench from 20s to 45s on a curve built from that session which
# double-counted retries: a credential retried several times inside one
# window had its same retry counted once per earlier refusal it followed.
#
# **1.8.0.2: recounted, corrected to 30.** An independent reviewer, who
# never read this module, re-derived the number from the same logs and
# found the same bug from the other direction. Counting each retry exactly
# once, over the whole real corpus (471 retries that followed a bare 429):
#
#   rest                     avoided   delayed   net seconds returned
#   20s (before 1.8.0.1)         6         0            +5
#   30s                        388         6          +304
#   45s (1.8.0.1)               394         9          +200
#   60s                        409         9           +77
#   90s                        413        11          -236
#
# Thirty is the argmax under this corrected counting and under the
# reviewer's own, independently-derived one (+480 for 30s against +385 for
# 45s — ``research/1.8.0.1/expected/rest-decision.md``). A refused round
# trip was measured at about 850ms, which is what makes the trade
# computable at all. Zero (spinning) is still worse either way — nothing
# under 30s ever bought an answer in the original session. Deliberately the
# same value as ``core.carousel.UNSIZED_THROTTLE_REST_S`` — see that
# constant for the full measurement and why one refusal must not produce two
# different waits depending on which half of the plugin is asked.
#
# It is an opening, not a ceiling. ``core/escalate.py`` doubles a bench that
# proves too short (30, 60, 120, 240 ...), so a window that really is long is
# found by measurement in a couple of refusals instead of guessed at an hour
# on the first one. That is the whole trade this plugin exists to make.
DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS = 30.0

# What to apply to an hourly window with no explicit delay. Deliberately
# shorter than the window: re-probing costs one failed call, while
# over-benching costs the use of a healthy key.
DEFAULT_PER_HOUR_BENCH_SECONDS = 10 * 60.0

# A daily window with no *provider-specific* reset rule. One hour means
# "re-probe this key hourly" — the same conservative choice Agent Zero's
# KAME made (`_KAME_DAILY_COOLDOWN_S`) and Hermes' own default lands on.
# Only a provider whose reset time we actually know gets benched longer.
DEFAULT_PER_DAY_BENCH_SECONDS = 60 * 60.0

# Account-level exhaustion (out of credits) is not a throttle and will not
# clear on its own, but a human may top up, so it is re-probed hourly rather
# than treated as permanently dead. Matches Agent Zero's _KAME_DAILY_COOLDOWN_S = 3600.
DEFAULT_ACCOUNT_BENCH_SECONDS = 60 * 60.0

# A credential the provider *refused* — a 401, or a key it named as revoked.
# Not a clock, which is why it never used to have a number of its own: it
# borrowed the daily hour, on the reasoning that waiting cannot repair a
# rejected key so the exact wait hardly matters.
#
# It matters, because the two ways of being wrong are not the same size.
# Wrong in the long direction — a key refused during a provider incident, or
# a 401 that was really a transient edge failure — costs a healthy credential
# an hour of the pool's capacity. Wrong in the short direction costs one
# request that fails in milliseconds and never touches the quota, because a
# 401 is refused before it is metered.
#
# **The second cut is the instructive one.** 1.6.0.1 first put this at five
# minutes, which was a number invented here. Twenty is the number the ladder
# in ``carousel._escalate`` applies to this kind anyway, and measuring the two
# side by side showed the invented one was doing no work:
#
#     base  20s ->  20  40  80 160 320 640 1280 2560 3600 ...
#     base 300s -> 300 300 300 300 320 640 1280 2560 3600 ...
#
# The ladder's own floor governs either way and both reach the hourly re-probe
# at the same point. All the larger base bought was flattening the first four
# strikes at five minutes — precisely the window in which a re-check is most
# likely to find a transient refusal already cleared.
#
# What makes a short wait safe is not the wait. It is two things in
# ``carousel``: refused keys are offered *last*, and a key refused
# ``REFUSALS_BEFORE_RETIRING`` times in a row — or once, if the provider used
# the words — stops being offered at all until something changes.
#
# Set to the ladder's own base rather than to a second constant beside it. A
# number that differed from ``DAILY_BASE_S`` would be a claim that a refusal
# needs a different opening rest, and that claim was measured and is false.
#
# This does **not** govern "this key may not use this model" — that is
# ``DEFAULT_DENIAL_BENCH_SECONDS`` below, which happens to open on the same
# step for its own reasons.
DEFAULT_REJECTED_BENCH_SECONDS = 20.0

# A refusal that names the *credential itself* as the problem — a gateway's
# "Invalid token", not a bare, wordless 401 and not the words that establish
# a key is unambiguously dead (see ``carousel.INVALID_KEY_INDICATORS``, which
# retires on sight). This stays in the *ambiguous* kind — R34: the same
# wording covers a relayed upstream 401 and a temporarily mis-propagated
# credential, so nothing is retired on one refusal — but the twenty-second
# opening above is wrong for it, and the paragraph that argued for twenty
# seconds there does not apply here.
#
# That argument measured the ladder, not the opening: base 20s and base 300s
# reach the same hourly ceiling on the same schedule once escalation starts,
# so a longer base "was doing no work" *for a wordless 401*, where nothing
# distinguishes a dead key from a proxy hiccup and re-probing fast is cheap
# either way. A refusal that names the token specifically is not that case —
# it is real, measured evidence (16 real rows, one-api/new-api gateway,
# research/1.8.0.0/expected/verdicts.jsonl real-11) that this is a credential
# problem and not a burst throttle, and twenty seconds was — until 1.8.0.1 —
# ``DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS``'s own number, reserved for
# exactly that, a throttle nobody could size (R23), not for this. The two
# constants happened to share a value, not a reason; 1.8.0.1 moved the
# throttle's number to 45.0 on its own measurement (1.8.0.2 corrected that
# to 30.0 — see the constant's own comment above) and left this one exactly
# where its own paragraph above put it.
#
# The host's own opening wait for "this credential looks wrong" is three
# hundred seconds (``EXHAUSTED_TTL_401_SECONDS``, R22), and copying it here
# was the first answer. The owner's log refused it. Thirteen times in
# 06-08/09/2026 a key that had just been told **401 Invalid token** by the
# gateway answered again on its own, and the whole distribution fits inside
# the host's opening wait: 10.6s, 11.0s, 15.6s, 29.3s, 46.4s, 53.2s, 70.9s,
# 137.8s, 160.7s, 161.4s, 262.1s, 285.0s, 331.6s. Twelve of the thirteen
# would have been sitting on a bench while they were already well.
#
# So this 401 is a refusal that heals, and the asymmetry decides the number:
# resting too long costs a healthy key in silence, resting too short costs
# one request that fails in milliseconds and spends no quota. Twenty seconds,
# with the key demoted to last and the retirement ladder still counting, is
# the cheap mistake. The constant stays separate from ``REJECTED_REST_S``
# anyway, because the two numbers answer different questions and the day one
# of them moves is the day that has to be visible.
DEFAULT_CREDENTIAL_PROBLEM_BENCH_SECONDS = 20.0

# A 403 that says *this key may not use this model*: a suspended project, an
# API never enabled, a model outside the tier the key pays for.
#
# 1.6.0.1 found two of these, disagreeing. ``classify`` benched a denial for
# twenty seconds and ``carousel`` for an hour, so the same refusal cost the
# same key two different amounts depending on which of the two classifiers
# happened to answer first. Two constants describing one fact drift, and the
# one nobody remembers is the one that goes stale, so there is now one.
#
# Twenty seconds is the survivor, and the hour is not lost: ``denied`` is on
# the doubling ladder in ``Carousel._escalate``, so a refusal that really is
# permanent reaches an hour on its own — 20, 40, 80 ... 3600 — while a plan
# somebody upgrades comes back in the seconds it actually took, rather than
# in the hour a constant guessed. What makes the short opening cheap is the
# same pair that makes it cheap for a bare 401: ``denied`` is in
# ``REJECTED_KINDS``, so the key is offered last, and the carousel's health
# is per ``provider:model``, so the key goes on working everywhere else in
# the same second.
DEFAULT_DENIAL_BENCH_SECONDS = 20.0


class QuotaScope:
    """How far a refusal reaches: one model, or every model on the key.

    Orthogonal to the window. "Per minute" says *when* the allowance comes
    back; the scope says *what else* is blocked meanwhile. The pool has a
    field for neither, and the two mistakes they cause are opposite ones —
    over-benching wastes a healthy allowance, under-benching walks into the
    same wall twice.
    """

    PER_MODEL = "per_model"
    ACCOUNT = "account"
    UNKNOWN = "unknown"


class QuotaWindow:
    PER_MINUTE = "per_minute"
    #: A per-minute counter measured in *tokens*, not requests — Google's
    #: ``GenerateContentInputTokensPerModelPerMinute``, and the same shape
    #: under Groq/Kimi/MiniMax's own TPM ceilings (R05). Kept distinct from
    #: ``PER_MINUTE`` because the two counters are spent by different things:
    #: a client that sends few, huge requests can exhaust a TPM bucket while
    #: its RPM bucket is nearly empty, and the reverse is just as real. Every
    #: real per-minute Gemini refusal measured on this machine (research/
    #: 1.8.0.0/expected/verdicts.jsonl real-04, 27 records) was this one, not
    #: a request-counted RPM — see ``TOKEN_UNIT_MARKERS`` below for how the
    #: two are told apart. Timing is unaffected: this window is deliberately
    #: absent from ``_WINDOW_ESCALATION_CAPS``'s long-window set and behaves
    #: exactly like ``PER_MINUTE`` everywhere sizing happens (see
    #: ``_WINDOW_BENCH_DEFAULTS`` and ``core/escalate.py``) — only the *name*
    #: reported to the journal/UI changes, per R05/R12.
    TOKENS_PER_MINUTE = "tokens_per_minute"
    PER_HOUR = "per_hour"
    PER_DAY = "per_day"
    PER_WEEK = "per_week"
    PER_MONTH = "per_month"
    ACCOUNT = "account"
    UNKNOWN = "unknown"


# ── window markers ────────────────────────────────────────────────────────
# Substrings checked against a separator-stripped haystack, so `per_day`,
# `per-day`, `PerDay` and `per day` all reduce to the same token. Kept
# narrow: a marker that can appear in a per-minute message would make every
# throttle look like a daily cap.

# Note what is *not* here: "exceeded your current quota". Google sends that
# sentence on every free-tier 429, per-minute ones included, so treating it
# as account-level would bench a key for a day over a twenty-second
# throttle. OpenAI's account-level version of the same sentence continues
# "...please check your plan and billing details", and that pairing is
# matched by the billing patterns in classify.py instead.
_ACCOUNT_MARKERS = (
    "insufficientquota",       # OpenAI: out of credits
    "insufficient_quota",
    "billinghardlimit",
    "creditbalanceistoolow",   # Anthropic
    "outofcredits",
    "nocreditsremaining",
)

_PER_MONTH_MARKERS = ("permonth", "monthly", "requestspermonth", "/month")
_PER_WEEK_MARKERS = ("perweek", "weekly", "requestsperweek", "/week", "rpw")
_PER_DAY_MARKERS = (
    "perday", "daily", "requestsperday", "tokensperday", "/day", "rpd",
    "quotaexceededperday", "dailylimit",
)
_PER_HOUR_MARKERS = ("perhour", "hourly", "requestsperhour", "/hour", "rph")
_PER_MINUTE_MARKERS = (
    "perminute", "requestsperminute", "tokensperminute", "/minute",
    "rpm", "tpm", "permin", "requestspermin", "tokenspermin",
)

# ── scope markers ─────────────────────────────────────────────────────────
# Read from the same haystack as the window, and with the same rule: only
# what the provider actually said. The asymmetry below is deliberate and is
# the whole safety argument for this dimension.
#
# Silence means ``unknown``, and ``unknown`` behaves exactly as every version
# before this one did — the bench is treated as this model's and the key is
# handed back for other models. So a scope this module fails to detect costs
# nothing that was not already being paid. Only an *explicit* account marker
# changes behaviour, and it changes it towards holding the key, which is the
# expensive direction. Guessing there would re-create, in a new dimension,
# the exact regression the per-model dimension was added to fix.

# "PerProjectPerModel" contains both a project marker and a model one; Google
# means the pair, and the narrower half is the one that decides. So per-model
# evidence is checked first and wins outright.
_PER_MODEL_MARKERS = (
    "permodel",
    "perbasemodel",
    "modelperday",
    "permodelperminute",
)

# Shared across every model the key can reach. ``perapikey`` belongs here for
# the same reason: a limit metered on the key alone does not care which model
# spends it.
_SHARED_MARKERS = (
    "peruser",
    "peraccount",
    "perorganization",
    "perworkspace",
    "perapikey",
    "perproject",
    # OpenRouter's account-wide free-tier ceiling. It meters the same shared
    # bucket in more than one window and names it the same way in each —
    # ``free-models-per-day``, ``free-models-per-min`` — so the bucket's name
    # is the whole of the evidence and the window it happens to be quoting is
    # no part of the claim. Matching only the daily spelling read one of the
    # two as per-model and handed the key back for other models it could not
    # serve. Per-model evidence still wins outright, being checked first.
    "freemodels",
    "accountwide",
    "organizationwide",
    # OpenAI Codex's ChatGPT-subscription usage cap: the provider's OWN
    # error ``type``/``code`` value, ``usage_limit_reached`` — not the
    # provider's name (R01), the exact word it put in the body, the same
    # category of evidence ``insufficientquota``/``creditbalanceistoolow``
    # above already are. codex-rs's own source (cited in full in
    # ``research/1.8.0.0/expected/verdicts.jsonl`` real-17) constructs this
    # variant as ``UsageLimitReached{plan_type, resets_at, rate_limits}`` —
    # a ChatGPT plan's rolling window, with no model dimension anywhere in
    # its shape — and the owner's own machine measured it directly: the
    # SAME absolute reset (~23:13 UTC) arrived under two different models,
    # ``gpt-5.6-luna`` at 19:42 and ``gpt-6-astra`` at 20:00. A per-model
    # marker still wins outright if one is also present (checked first,
    # above); this only fires when nothing narrower was said.
    "usagelimitreached",
)

# Body keys whose value names the model a quota was metered against. Google
# sends ``quotaDimensions: {"model": "...", "location": "global"}``; naming
# the model in the violation is the provider stating the scope structurally
# rather than in a sentence.
_MODEL_DIMENSION_KEYS = frozenset({"model", "modelid", "basemodel"})

# Narrowest window first is wrong; widest first is right. A body that names
# both "PerMinute" and "PerDay" is telling us the daily counter is the one
# that is spent — that is the binding constraint, and treating it as a
# per-minute blip re-hammers a key that will fail all day.
_WINDOW_MARKERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (QuotaWindow.ACCOUNT, _ACCOUNT_MARKERS),
    (QuotaWindow.PER_WEEK, _PER_WEEK_MARKERS),
    (QuotaWindow.PER_MONTH, _PER_MONTH_MARKERS),
    (QuotaWindow.PER_DAY, _PER_DAY_MARKERS),
    (QuotaWindow.PER_HOUR, _PER_HOUR_MARKERS),
    (QuotaWindow.PER_MINUTE, _PER_MINUTE_MARKERS),
)

_WINDOW_BENCH_DEFAULTS = {
    QuotaWindow.PER_MINUTE: DEFAULT_PER_MINUTE_BENCH_SECONDS,
    # Same clock, same skew allowance — a TPM bucket refills on the same
    # sixty-second wall as an RPM one; only what is counted differs.
    QuotaWindow.TOKENS_PER_MINUTE: DEFAULT_PER_MINUTE_BENCH_SECONDS,
    QuotaWindow.PER_HOUR: DEFAULT_PER_HOUR_BENCH_SECONDS,
    QuotaWindow.PER_DAY: DEFAULT_PER_DAY_BENCH_SECONDS,
    QuotaWindow.PER_WEEK: DEFAULT_PER_DAY_BENCH_SECONDS,
    QuotaWindow.PER_MONTH: DEFAULT_PER_DAY_BENCH_SECONDS,
    QuotaWindow.ACCOUNT: DEFAULT_ACCOUNT_BENCH_SECONDS,
}

# The unit a per-minute counter is measured in, when the provider names it.
# Google spells it two ways on the very same refusal — ``quotaId``:
# ``...InputTokensPerModelPerMinute...``, ``quotaMetric``:
# ``..._input_token_count`` — and both reduce to ``inputtoken`` once
# separators are stripped, so one marker catches whichever field survived
# Hermes' Gemini adapter (see ``classify._response_text`` on the body being
# spent after one read). Narrow on purpose, the same reasoning as
# ``_PER_MODEL_MARKERS``: a bare "token" can mean an *auth* token (a 401's
# "Invalid token", nothing to do with a quota counter), so only the specific,
# quota-shaped spellings qualify.
TOKEN_UNIT_MARKERS = ("inputtoken", "outputtoken", "tokencount")

_SEPARATORS = re.compile(r"[\s_\-]+")


def _haystack(*parts: str) -> str:
    """Lowercase and strip separators so marker matching is shape-neutral."""
    return _SEPARATORS.sub("", " ".join(p for p in parts if p).lower())


# ── duration parsing ──────────────────────────────────────────────────────

_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?|\.\d+)\s*"
    r"(milliseconds?|millis|msecs?|ms|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "ms": 0.001, "msec": 0.001, "msecs": 0.001,
    "milli": 0.001, "millis": 0.001,
    "millisecond": 0.001, "milliseconds": 0.001,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
}

#: The same table, under a name another module may import. ``carousel`` reads
#: a retry hint with its own anchored regex — it has to, because it matches on
#: the keyword as well as the number — but the *unit* must mean the same thing
#: in both files. It did not until 1.7.0.3, and a millisecond hint cost the
#: owner five keys for most of a day.
UNIT_SECONDS = _UNIT_SECONDS


def parse_duration_to_seconds(text: Any) -> Optional[float]:
    """Parse a duration *expression* into seconds.

    Handles the compound forms providers actually emit: ``6m 11.52s`` (Groq),
    ``2h 30m``, ``6m0s`` (OpenAI reset headers), ``1500ms``, ``45s``,
    ``2970.938289688s``, and a bare number (read as seconds).

    Only ever call this on a substring already isolated as a duration. Run on
    a whole error message it would read model names and request ids as
    seconds — which is why the text extractor below captures after a retry
    keyword rather than scanning freely.
    """
    if text is None or isinstance(text, bool):
        return None
    raw = str(text).strip()
    if not raw:
        return None

    if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", raw):
        return float(raw)

    total = 0.0
    found = False
    position = 0
    for match in _DURATION_PART.finditer(raw):
        if raw[position:match.start()].strip():
            return None
        value, unit = match.groups()
        position = match.end()
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        total += number * _UNIT_SECONDS.get((unit or "s").lower(), 1.0)
        found = True
    return total if found and not raw[position:].strip() else None


def parse_absolute_timestamp(value: Any) -> Optional[float]:
    """Parse an absolute reset time into an epoch, or return None.

    Accepts what rate-limit headers actually carry: an RFC 3339 timestamp
    (Anthropic's ``anthropic-ratelimit-*-reset``), an HTTP-date (the other
    half of RFC 7231's ``Retry-After``), and epoch seconds or milliseconds
    (GitHub-style ``x-ratelimit-reset``).
    """
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_from_number(float(value))

    raw = str(value).strip().strip("\"'")
    if not raw:
        return None

    # Bare digits: epoch, in seconds or milliseconds.
    if re.fullmatch(r"\d{9,}(?:\.\d+)?", raw):
        try:
            return _epoch_from_number(float(raw))
        except ValueError:
            return None

    # RFC 3339 / ISO 8601.
    iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    iso = re.sub(r"\s+(?:UTC|GMT)$", "+00:00", iso, flags=re.I)
    try:
        moment = datetime.fromisoformat(iso)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except ValueError:
        pass

    # HTTP-date, e.g. "Wed, 21 Oct 2026 07:28:00 GMT".
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _epoch_from_number(number: float) -> Optional[float]:
    """Disambiguate epoch seconds from epoch milliseconds.

    1e12 seconds is the year 33658; 1e12 milliseconds is 2001. Anything at
    or above that magnitude is milliseconds.
    """
    if number <= 0:
        return None
    if number >= 1e12:
        return number / 1000.0
    if number >= 1e9:
        return number
    return None  # Too small to be an epoch — it is a relative duration.


# ── evidence sources ──────────────────────────────────────────────────────


def _bounded_relative(seconds: Optional[float]) -> Optional[float]:
    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if not (0 < value <= MAX_RELATIVE_DELAY_SECONDS):
        return None
    return value


def _relative_from_absolute(epoch: Optional[float], now_epoch: float) -> Optional[float]:
    """Convert an absolute reset time into a delay, rejecting the implausible."""
    if epoch is None:
        return None
    delta = epoch - now_epoch
    if not (0 < delta <= MAX_ABSOLUTE_HORIZON_SECONDS):
        return None
    return delta


def _milliseconds_delay(value: Any) -> Optional[float]:
    try:
        return _bounded_relative(float(value) / 1000.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_getattr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def extract_from_exception(error: Any) -> Tuple[Optional[float], str]:
    """Read the retry hint off an SDK exception object.

    Two shapes, both common enough to be worth naming: ``retry_after`` as a
    plain number of seconds (litellm, OpenAI, Anthropic), and ``retry_delay``
    as a protobuf Duration with ``.seconds``/``.nanos`` (Google).

    Attribute reads are guarded. The argument is an arbitrary exception from
    whichever SDK failed, and a property that raises must cost this source
    only, not the three below it in the cascade.
    """
    if error is None:
        return None, ""

    retry_after = _safe_getattr(error, "retry_after")
    if retry_after is not None:
        seconds = _bounded_relative(retry_after)
        if seconds is None:
            seconds = _bounded_relative(parse_duration_to_seconds(retry_after))
        if seconds is not None:
            return seconds, "exception.retry_after"

    retry_delay = _safe_getattr(error, "retry_delay")
    if retry_delay is not None:
        if hasattr(retry_delay, "seconds") or hasattr(retry_delay, "nanos"):
            try:
                value = (
                    float(getattr(retry_delay, "seconds", 0) or 0)
                    + float(getattr(retry_delay, "nanos", 0) or 0) / 1e9
                )
            except (TypeError, ValueError):
                value = None
            seconds = _bounded_relative(value)
            if seconds is not None:
                return seconds, "exception.retry_delay"
        seconds = _bounded_relative(parse_duration_to_seconds(retry_delay))
        if seconds is not None:
            return seconds, "exception.retry_delay"

    return None, ""


# ``Retry-After`` is authoritative when present. Everything else is a
# rate-limit reset header, whose name varies per provider but whose *shape*
# does not: a name mentioning both a limit and a reset.
_RESET_HEADER = re.compile(r"(?:ratelimit|rate-limit|quota|usage).*reset|reset.*(?:ratelimit|rate-limit|quota)", re.I)


def _header_items(headers: Any) -> List[Tuple[str, Any]]:
    if headers is None:
        return []
    try:
        if hasattr(headers, "items"):
            return [(str(k), v) for k, v in headers.items()]
        if isinstance(headers, (list, tuple)):
            return [(str(k), v) for k, v in headers if k is not None]
    except Exception:
        return []
    return []


def extract_from_headers(headers: Any, now_epoch: float) -> Tuple[Optional[float], str]:
    """Read ``Retry-After`` and any rate-limit reset header.

    ``Retry-After`` wins outright — it is the standard and it is what the
    provider chose to say. Failing that, reset headers are collected and the
    **longest** is taken: benching a key slightly too long costs one key out
    of a pool for a while, whereas releasing it too early re-hammers a limit
    that is still spent, which is the failure that cascades.
    """
    items = _header_items(headers)
    if not items:
        return None, ""

    lowered = {name.strip().lower(): value for name, value in items}

    # Azure's precision hint precedes rounded Retry-After, matching the
    # installed OpenAI SDK parser. Values are milliseconds, never seconds.
    milliseconds = _milliseconds_delay(lowered.get("retry-after-ms"))
    if milliseconds is not None:
        return milliseconds, "header.retry-after-ms"

    raw = lowered.get("retry-after")
    if raw is not None:
        seconds = _bounded_relative(raw)
        if seconds is None:
            # RFC 7231 allows an HTTP-date here, not only a delta.
            seconds = _relative_from_absolute(parse_absolute_timestamp(raw), now_epoch)
        if seconds is None:
            seconds = _bounded_relative(parse_duration_to_seconds(raw))
        if seconds is not None:
            return seconds, "header.retry-after"

    best: Optional[float] = None
    best_name = ""
    for name, value in lowered.items():
        if name == "retry-after" or not _RESET_HEADER.search(name):
            continue
        seconds = _relative_from_absolute(parse_absolute_timestamp(value), now_epoch)
        if seconds is None:
            seconds = _bounded_relative(parse_duration_to_seconds(value))
        if seconds is not None and (best is None or seconds > best):
            best, best_name = seconds, name

    return (best, f"header.{best_name}") if best is not None else (None, "")


def _walk(node: Any, out: List[Tuple[str, Any]], depth: int = 0) -> None:
    """Flatten a nested error body into (key, value) pairs, depth-bounded.

    Error bodies are attacker-adjacent in the sense that they are arbitrary
    remote JSON: a self-referential or absurdly deep structure must cost a
    bounded amount of work, not a stack overflow inside the host's error
    path.
    """
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            out.append((str(key), value))
            _walk(value, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _walk(value, out, depth + 1)


# A body names a reset the same way a header does, and the two have to be read
# the same way. OpenRouter puts its rate-limit headers *inside* the body, under
# ``error.metadata.headers`` — documented, and what litellm surfaces — so the
# identical name ``X-RateLimit-Reset`` arrives on both sides of that line. This
# module used to answer differently depending on which side it landed on:
# ``extract_from_headers`` matched it by shape, while this pattern demanded a
# suffix after ``reset`` and read it as an ordinary key. The exact moment
# OpenRouter states was thrown away and a daily cap fell back to the hourly
# re-probe — one wasted refusal per key per hour, for the rest of the day, on a
# free tier that says precisely when it rolls over.
#
# Sharing ``_RESET_HEADER``'s pattern rather than restating it is the point:
# the defect was not the missing spelling, it was two readings of one name
# drifting apart. It also picks up ``quotaResetDelay`` in a body — a key the
# host's own text scan already knows, and which this one did not.
_RETRY_KEY = re.compile(
    r"retry.?(?:delay|after|in)|reset.?(?:at|after|in|time)|" + _RESET_HEADER.pattern,
    re.I,
)


def _duration_message(value: Any) -> Optional[float]:
    """Read a protobuf ``Duration`` rendered as JSON, or return None.

    ``google.rpc.RetryInfo.retryDelay`` is a Duration *message*, not a string.
    Google's REST endpoint happens to render it as ``"21s"``, but canonical
    proto JSON — what gRPC-JSON transcoding and the GenAI SDKs emit — renders
    it as ``{"seconds": 30, "nanos": 500000000}``. The body walker skipped
    every dict value, so that whole shape read as "no delay at all" and the
    reading fell back to the window's default.

    Requiring one of the two field names is what keeps this narrow: a dict
    under a retry-shaped key that carries neither is somebody's retry
    *policy*, and reading a number out of it would be inventing a deadline.
    """
    if not isinstance(value, dict):
        return None
    if "seconds" not in value and "nanos" not in value:
        return None
    try:
        return (
            float(value.get("seconds", 0) or 0)
            + float(value.get("nanos", 0) or 0) / 1e9
        )
    except (TypeError, ValueError):
        return None


def _body_reset_seconds(value: Any, now_epoch: float, *, absolute: bool):
    """Reject malformed reset scalars without affecting other evidence readers."""
    if value is None or isinstance(value, (bool, dict, list, tuple)):
        return None
    try:
        if absolute:
            return _relative_from_absolute(parse_absolute_timestamp(value), now_epoch)
        return _bounded_relative(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _body_fields(node: Any, depth: int = 0):
    """The existing depth-first body order, retaining each field's mapping."""
    if depth > 8:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key), value, node
            yield from _body_fields(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _body_fields(value, depth + 1)


def extract_from_body(body: Any, now_epoch: float) -> Tuple[Optional[float], str]:
    """Find a retry hint anywhere in a structured error body.

    Google puts it at ``error.details[]`` with ``@type`` of
    ``google.rpc.RetryInfo``; others nest it elsewhere. Rather than encode
    each layout, walk the body and take any key that names a retry or reset.

    Plural ``resets_at`` (epoch seconds/milliseconds or a timestamp string)
    and ``resets_in_seconds`` (numeric seconds) are paired only in the same
    mapping. Use the later valid deadline on disagreement, avoiding an early
    retry; invalid members are ignored. Ties credit ``resets_at``. Existing
    traversal precedence across unrelated families and source labels stay
    intact: this is not a maximum over all fields. Zero, past, nonfinite and
    out-of-horizon readings supply no deadline. Singular ``reset_at`` is also
    absolute, never a small relative duration. Header precedence is unchanged.
    """
    if not isinstance(body, (dict, list, tuple)):
        return None, ""

    try:
        pairs = list(_body_fields(body))
    except Exception:
        return None, ""

    for key, value, siblings in pairs:
        if key.lower().replace("_", "-") in ("retry-after", "retry-after-ms"):
            for sibling, raw in siblings.items():
                if str(sibling).lower().replace("_", "-") == "retry-after-ms":
                    milliseconds = _milliseconds_delay(raw)
                    if milliseconds is not None:
                        return milliseconds, f"body.{sibling}"
            if key.lower().replace("_", "-") == "retry-after-ms":
                continue  # malformed milliseconds must never become seconds
        # Only the plural pair describes one deadline family. Never maximize
        # unrelated retry/reset fields or merge sibling objects in SDK bodies.
        if key in ("resets_at", "resets_in_seconds"):
            candidates = []
            for name in ("resets_at", "resets_in_seconds"):
                raw = siblings.get(name)
                if isinstance(raw, bool) or raw is None:
                    continue
                if name == "resets_at":
                    seconds = _body_reset_seconds(raw, now_epoch, absolute=True)
                else:
                    # Explicit seconds: no epoch guessing or loose prose scan.
                    seconds = _body_reset_seconds(raw, now_epoch, absolute=False)
                if seconds is not None:
                    candidates.append((seconds, f"body.{name}"))
            if candidates:
                return max(candidates, key=lambda item: item[0])
            continue
        if re.fullmatch(r"reset[ _-]?at", key, re.I):
            seconds = _body_reset_seconds(value, now_epoch, absolute=True)
            if seconds is not None:
                return seconds, f"body.{key}"
            continue
        if not _RETRY_KEY.search(key):
            continue
        if _RESET_HEADER.search(key):
            # Match the HTTP reader's absolute-first order, including dates.
            seconds = _body_reset_seconds(value, now_epoch, absolute=True)
            if seconds is not None:
                return seconds, f"body.{key}"
        if isinstance(value, dict):
            seconds = _bounded_relative(_duration_message(value))
            if seconds is not None:
                return seconds, f"body.{key}"
            continue
        if isinstance(value, (list, tuple)):
            continue
        seconds = _bounded_relative(parse_duration_to_seconds(value))
        if seconds is None:
            seconds = _relative_from_absolute(parse_absolute_timestamp(value), now_epoch)
        if seconds is not None:
            return seconds, f"body.{key}"

    return None, ""


_RETRY_PHRASE = (
    r"retry[_\s-]*(?:after|delay|in)|try\s+again\s+in|available\s+in|"
    r"resets?\s+in|wait\s+(?:for\s+)?"
)

# Longest-first inside the alternation, so `ms` is not read as `m`, `hr` not
# as `h`, and `sec` not as `s`. The trailing lookahead stops a unit from
# matching the first letter of an ordinary word — without it, `retry after 5
# megabytes` reads the `m` as minutes. It has to be a lookahead rather than
# `\b`, because `6m11.52s` has no word boundary between the `m` and the `1`.
_UNIT_TOKEN = (
    r"(?:milliseconds?|millis|msecs?|ms|hrs?|hours?|h|mins?|minutes?|m"
    r"|secs?|seconds?|s)(?![a-z])"
)

# A duration is one or more number+unit pairs. Matching the pairs explicitly
# is what makes `6m11.52s` (Groq) and `4hr 5min` (OpenCode Go) survive: an
# earlier draft scanned a character class up to the next punctuation, which
# truncated the first at the decimal point and failed the second outright on
# the `i` of `min`.
_DURATION_EXPR = (
    rf"(?:\d+(?:\.\d+)?|\.\d+)\s*{_UNIT_TOKEN}(?:\s*(?:\d+(?:\.\d+)?|\.\d+)\s*{_UNIT_TOKEN})*"
)

# A unitless number is read as seconds — but only when the character after it
# rules out a date or clock time. Without that guard `retry after 2026-08-15`
# would bench a key for the 2026 seconds it starts with.
_BARE_SECONDS = r"(?:\d+(?:\.\d+)?|\.\d+)(?![\w.:/\-])"

_TEXT_RETRY = re.compile(
    rf"(?:{_RETRY_PHRASE})[:\s\"']*({_DURATION_EXPR}|{_BARE_SECONDS})",
    re.IGNORECASE,
)


def extract_from_text(message: str) -> Tuple[Optional[float], str]:
    """Last resort: a duration written into the error sentence.

    The duration is captured only after a retry keyword. Scanning the whole
    message would read a model name (``gemini-3.6``) or a request id as a
    number of seconds.
    """
    if not message:
        return None, ""
    match = _TEXT_RETRY.search(str(message))
    if not match:
        return None, ""
    seconds = _bounded_relative(parse_duration_to_seconds(match.group(1)))
    return (seconds, "text") if seconds is not None else (None, "")


# A provider that states *when* rather than *how long*. Every reader above
# this point wants a duration, so a whole family of refusals that name the
# exact rollover was being discarded and replaced by the window's flat
# default — a number this module invented, standing in for one the provider
# had already given.
#
# Z.AI is the clearest case: codes 1308, 1310 and 1316-1321 all end in
# "Your limit will reset at {next_flush_time}", and 1310 is a *weekly or
# monthly* limit. Falling back to the hourly re-probe there spends one
# refusal per key per hour for up to a week, against a sentence that said
# precisely when to come back. See ``research/provider-errors.md``.
#
# The timestamp alternation is deliberately narrow — ISO 8601 and a bare
# epoch, nothing else. A looser capture reaches into the rest of the
# sentence, and the value it produces is a *bench deadline*: the cost of
# reading it wrong is a healthy key parked for as long as the misread says.
# ``parse_absolute_timestamp`` then has to agree, and
# ``_relative_from_absolute`` bounds the result to a week, so three
# independent things have to accept the reading before it becomes a wait.
_ABS_MOMENT = (
    r"\d{4}-\d{2}-\d{2}(?:(?:[T ]|\s+at\s+)\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"
    r"(?:\s*(?:Z|[+-]\d{2}:?\d{2}|UTC|GMT))?"
    r"|\d{10,13}"
)

#: "at" and "on" only. "resets in 5 minutes" is a duration and is already
#: read above; admitting the bare preposition-less form here would make the
#: two readers fight over the same sentence.
_TEXT_RESET_MOMENT = re.compile(
    rf"(?:resets?|renews?|refreshes|available|back|try\s+again|come\s+back|regain\s+access)"
    rf"\s+(?:at|on)\s*[:\"']*\s*({_ABS_MOMENT})",
    re.IGNORECASE,
)


def extract_reset_moment_from_text(
    message: str, now_epoch: float
) -> Tuple[Optional[float], str]:
    """A reset *moment* written into the error sentence, as a delay from now.

    Returns ``(None, "")`` unless the captured text parses as a real
    timestamp that is in the future and inside the horizon — a reset time
    already past is a stale message, not a reason to bench anything.
    """
    if not message:
        return None, ""
    match = _TEXT_RESET_MOMENT.search(str(message))
    if not match:
        return None, ""
    seconds = _relative_from_absolute(
        parse_absolute_timestamp(re.sub(r"\s+at\s+", " ", match.group(1), flags=re.I)), now_epoch
    )
    return (seconds, "text.reset_at") if seconds is not None else (None, "")


def extract_retry_delay_seconds(
    message: str = "",
    body: Any = None,
    headers: Any = None,
    error: Any = None,
    now_epoch: Optional[float] = None,
) -> Tuple[Optional[float], str]:
    """Run the evidence cascade. Returns ``(seconds, source)``.

    Ordered by how much interpretation each source needs: a structured field
    means what it says, while a sentence has to be parsed out of prose.
    """
    candidates = _delay_candidates(
        message=message, body=body, headers=headers, error=error, now_epoch=now_epoch
    )
    return candidates[0] if candidates else (None, "")


def _delay_candidates(
    *,
    message: str = "",
    body: Any = None,
    headers: Any = None,
    error: Any = None,
    now_epoch: Optional[float] = None,
) -> List[Tuple[float, str]]:
    """Every reading the cascade produced, strongest first.

    The cascade's job is to pick one, and the first is the pick. The rest are
    kept for the single caller that has a reason to look past it: a *long*
    window whose strongest reading is short. See ``compute_reset_at``.
    """
    import time as _time

    now = float(now_epoch) if now_epoch is not None else _time.time()

    from .provider_rules import timing_headers
    headers = timing_headers(error, headers, detect_quota_window(message, body))

    readings = (
        extract_from_exception(error),
        extract_from_headers(headers, now),
        extract_from_body(body, now),
        # Above the loose duration reader: both look at the same sentence,
        # and a stated moment is the more specific of the two.
        extract_reset_moment_from_text(message, now),
        extract_from_text(message),
    )
    return [(seconds, source) for seconds, source in readings if seconds is not None]


# ── quota window ──────────────────────────────────────────────────────────


def detect_quota_window(message: str = "", body: Any = None) -> str:
    """Name the quota window the provider says is spent.

    Reads the body's values as well as its keys, because the discriminating
    string usually lives in a value (Google's ``quotaId``, Groq's message)
    rather than in a field name.
    """
    parts: List[str] = [str(message or "")]

    if isinstance(body, (dict, list, tuple)):
        pairs: List[Tuple[str, Any]] = []
        try:
            _walk(body, pairs)
        except Exception:
            pairs = []
        for key, value in pairs:
            parts.append(str(key))
            if isinstance(value, str):
                parts.append(value)

    hay = _haystack(*parts)
    if not hay:
        return QuotaWindow.UNKNOWN

    for window, markers in _WINDOW_MARKERS:
        if _mentions(hay, markers):
            # "PerMinute" alone does not say what is being counted. Google's
            # own quotaId/quotaMetric say so in the same string — see
            # TOKEN_UNIT_MARKERS — and when they do, the narrower fact is the
            # one worth reporting: a TPM bucket and an RPM bucket are spent by
            # different things and recover on evidence about different
            # things, even though both close in the same sixty seconds.
            if window == QuotaWindow.PER_MINUTE and _mentions(hay, TOKEN_UNIT_MARKERS):
                return QuotaWindow.TOKENS_PER_MINUTE
            return window
    return QuotaWindow.UNKNOWN


def _mentions(hay: str, markers: Tuple[str, ...]) -> bool:
    return any(
        marker.replace("_", "").replace("-", "").replace("/", "") in hay
        for marker in markers
    )


def detect_quota_scope(message: str = "", body: Any = None, *, billing_context_only: bool = False) -> str:
    """Say whether the refusal covers one model or the whole key.

    Returns ``UNKNOWN`` far more often than it returns anything else, and
    that is the point: ``UNKNOWN`` is the pre-existing behaviour, so this
    function can only ever *remove* a wrong release, never invent a new
    hold. A provider that says nothing about scope is not guessed at.
    """
    parts: List[str] = [str(message or "")]
    names_a_model = False

    if isinstance(body, (dict, list, tuple)):
        pairs: List[Tuple[str, Any]] = []
        try:
            _walk(body, pairs)
        except Exception:
            pairs = []
        for key, value in pairs:
            parts.append(str(key))
            if isinstance(value, str):
                parts.append(value)
                if value.strip() and _haystack(str(key)) in _MODEL_DIMENSION_KEYS:
                    names_a_model = True

    hay = _haystack(*parts)
    if not hay:
        return QuotaScope.UNKNOWN
    if names_a_model or _mentions(hay, _PER_MODEL_MARKERS):
        return QuotaScope.PER_MODEL
    if _mentions(hay, _SHARED_MARKERS) or (not billing_context_only and _mentions(hay, _ACCOUNT_MARKERS)):
        return QuotaScope.ACCOUNT
    return QuotaScope.UNKNOWN




# ── the decision ──────────────────────────────────────────────────────────


class ResetDecision:
    """How long to bench a credential, and the reasoning that produced it."""

    __slots__ = ("reset_at", "window", "source", "rationale", "scope", "window_scoped_reset")

    def __init__(
        self,
        reset_at: Optional[float],
        window: str,
        source: str,
        rationale: str,
        scope: str = QuotaScope.UNKNOWN,
        window_scoped_reset: bool = False,
    ) -> None:
        self.reset_at = reset_at
        self.window = window
        self.source = source
        self.rationale = rationale
        self.scope = scope
        self.window_scoped_reset = window_scoped_reset

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ResetDecision(window={self.window!r}, scope={self.scope!r}, "
            f"source={self.source!r}, rationale={self.rationale!r})"
        )


def compute_reset_at(
    *,
    now_epoch: float,
    provider: str = "",
    message: str = "",
    body: Any = None,
    headers: Any = None,
    error: Any = None,
    now: Optional[datetime] = None,
    window_hint: str = "",
    scope_hint: str = "",
    billing_context_only: bool = False,
) -> ResetDecision:
    """Decide when a credential may be tried again.

    The window and the delay are read independently, then reconciled — and
    the reconciliation is the part that matters. A provider that has spent a
    *daily* quota very often still sends a short ``retryDelay``: Google
    attaches ``37s`` to a daily 429. Obeying it returns the key to rotation
    while it is still spent, so it fails again, and again, for the rest of
    the day. Long windows therefore ignore the short delay and use the
    window's own reset instead.

    Returns a decision with ``reset_at=None`` when there is no evidence at
    all, which is a deliberate refusal rather than a guess: the host's
    default is a reasonable answer, and replacing it with a fabricated one
    would be strictly worse.
    """
    window = detect_quota_window(message, body)
    if billing_context_only and window == QuotaWindow.ACCOUNT:
        window = QuotaWindow.UNKNOWN
    # An endpoint-specific documented code can name a window even when the
    # generic body/prose parser cannot. It never overrides a named counter.
    if window == QuotaWindow.UNKNOWN and window_hint in _WINDOW_BENCH_DEFAULTS:
        window = window_hint
    scope = detect_quota_scope(message, body, billing_context_only=billing_context_only)
    if scope == QuotaScope.UNKNOWN and scope_hint in (QuotaScope.ACCOUNT, QuotaScope.PER_MODEL):
        scope = scope_hint
    candidates = _delay_candidates(
        message=message, body=body, headers=headers, error=error, now_epoch=now_epoch
    )
    delay, source = candidates[0] if candidates else (None, "")
    from .provider_rules import trusts_window_reset
    scoped_hint = trusts_window_reset(error, window, source, headers, now_epoch)

    long_window = window in (
        QuotaWindow.PER_DAY, QuotaWindow.PER_WEEK,
        QuotaWindow.PER_MONTH, QuotaWindow.ACCOUNT,
    )

    if long_window:
        absolute = max((candidate for candidate in candidates if candidate[1] in {
            "body.reset_at", "body.resets_at", "text.reset_at"}),
            key=lambda candidate: candidate[0], default=None)
        if scoped_hint and delay is not None and (absolute is None or delay >= absolute[0]):
            seconds = delay
            rationale = f"{window} quota — documented window retry in {_fmt(delay)}"
        elif absolute is not None and not (delay is not None and delay >= DEFAULT_PER_DAY_BENCH_SECONDS
                                         and delay > absolute[0]):
            # A dated reset one minute from now really is one minute away.
            # It is not the short unscoped RetryInfo attached to a daily quota.
            seconds, source = absolute
            rationale = f"{window} quota — explicit calendar reset in {_fmt(seconds)}"
        elif delay is not None and delay >= DEFAULT_PER_DAY_BENCH_SECONDS:
            # A long delay on a long window is the provider being specific.
            # Only a *short* one is the misleading case worth overriding.
            seconds = min(delay, MAX_ABSOLUTE_HORIZON_SECONDS)
            rationale = f"{window} quota — provider reset in {_fmt(delay)}"
        else:
            # The strongest reading is shorter than the window, so it is the
            # misleading kind and has been set aside. What replaces it would
            # be the flat re-probe — a number this module invented — so ask
            # the readings the cascade skipped first. A daily 429 very often
            # carries both: a per-minute reset *header*, which is about a
            # different counter and is the short hint, and the daily wait
            # spelled out in the message. Strength order is right for picking
            # one reading; it is not a reason to prefer a guess over a number
            # the provider stated.
            #
            # Longest wins among them, for the reason the reset headers use:
            # a key released early re-hammers a limit that is still spent,
            # and that is the failure that cascades. Every candidate here is
            # already bounded, already the provider's own word, and already
            # subject to being tested and refuted later.
            stated = max(
                (
                    reading for reading in candidates[1:]
                    if reading[0] >= DEFAULT_PER_DAY_BENCH_SECONDS
                ),
                key=lambda reading: reading[0],
                default=None,
            )
            if stated is not None:
                seconds, source = stated
                seconds = min(seconds, MAX_ABSOLUTE_HORIZON_SECONDS)
                rationale = f"{window} quota — provider reset in {_fmt(seconds)}"
            else:
                seconds = _WINDOW_BENCH_DEFAULTS[window]
                rationale = (
                    f"{window} quota — re-probe in {_fmt(seconds)}"
                    + (f" (ignoring misleading {_fmt(delay)} hint)" if delay is not None else "")
                )
                # Not the header or the body the hint came from: that number
                # was just discarded, and naming it here would credit the
                # window's own default to a source that did not supply it —
                # the same small lie the anchor branch above stopped telling.
                source = "window"
    elif delay is not None:
        seconds = delay
        rationale = f"provider asked for {_fmt(delay)}"
    elif window in _WINDOW_BENCH_DEFAULTS:
        seconds = _WINDOW_BENCH_DEFAULTS[window]
        rationale = f"{window} quota, no delay supplied — {_fmt(seconds)}"
        source = "window"
    else:
        return ResetDecision(None, window, "", "no quota signal — deferring to host", scope)

    # A window that is spent at the account level is spent for every model by
    # definition — there is no separate per-model allowance left behind it.
    if window == QuotaWindow.ACCOUNT:
        scope = QuotaScope.ACCOUNT

    seconds = max(MIN_BENCH_SECONDS, min(float(seconds), MAX_ABSOLUTE_HORIZON_SECONDS))
    if scope == QuotaScope.ACCOUNT:
        rationale += " — every model on this key"
    window_scoped_reset = scoped_hint or source in ("body.reset_at", "body.resets_at", "text.reset_at")
    return ResetDecision(now_epoch + seconds, window, source, rationale, scope, window_scoped_reset)


def _fmt(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:g}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"
