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

**A cooldown is never shortened, and never outlasts the ceiling.** ``mark``
stores ``max(existing, now + delay)``. A key that just told us it is out for
the day must not be released in twenty seconds because a *different* call got
a softer refusal from the same key a moment later. Backoff escalates per key
and per kind, and resets on success. That "never shortened" rule is in tension
with :data:`MAX_HOLD_S` — the owner's ceiling on any hold — and the tension is
resolved in the ceiling's favour: ``mark`` also trims ``sick_until`` down to
``now + max_hold_s`` on every call, so a hold stored before the ceiling
existed, or under a since-lowered one, does not go on being honoured past it.

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
import math
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Acyclic on purpose: ``catalog`` imports only ``quota``, and ``quota`` imports
# nothing from this package. The carousel reads the catalogue rather than
# keeping a second copy of the same facts.
from . import catalog
from .quota import (
    DEFAULT_CREDENTIAL_PROBLEM_BENCH_SECONDS,
    DEFAULT_DENIAL_BENCH_SECONDS,
    DEFAULT_REJECTED_BENCH_SECONDS,
    QuotaScope,
    UNIT_SECONDS as _UNIT_SECONDS,
    parse_duration_to_seconds as _quota_parse_duration,
)
# ``shared_health`` imports nothing from this package either — same acyclic
# reasoning as ``catalog`` above — which is what lets ``Carousel`` construct
# one of its own during ``__init__`` with no cycle. 1.8.0.0, decisions/0006:
# the file it reads and writes is how three Hermes profiles sharing the same
# physical keys stop teaching each other the wrong lesson about them.
from . import shared_health

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

#: No cooldown may exceed a day, whatever a provider claims. The absolute
#: sanity bound — see :data:`MAX_HOLD_S` for the operational one that sits
#: under it.
HARD_DELAY_CAP_S = 86400.0

#: No cooldown may exceed this either, and this is the one the owner actually
#: tunes. ``HARD_DELAY_CAP_S`` above never moves; it is the backstop for a
#: number this module fails to bound at all. This is the number G8
#: (``PLAN_1.8.0.0.md``, ``settings.MAX_HOLD``) is about: no credential sits
#: out of rotation longer than it, whatever produced the hold — a stated
#: deadline, a calendar reset, this module's own escalation ladder, or the
#: widening ``escalate.stretch`` applies on the host-block path. The host may
#: override it, the same way ``daily_cooldown_s`` is threaded in below; this
#: default is one hour because that is what the owner asked for on
#: 2026-09-15, and it has to agree with ``settings.ALL_NUMBERS[settings.MAX_HOLD]``
#: for the two to describe the same plugin when nobody has configured
#: anything.
MAX_HOLD_S = 3600.0

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

#: A 5xx is the provider's problem, not the key's. Rest this long, flat.
#:
#: **One second, and it is deliberately the same number as** :data:`RL_BASE_S`
#: — this module's floor for "a cooldown rather than a spin". It is not a new
#: number, and ``test_v1_7_0_4`` pins the two together so they cannot drift.
#:
#: It was 5.0 until 1.7.0.4, and measuring showed the five had never done
#: anything. On the owner's pool of fourteen, the gap between a key's 503 and
#: that same key being offered again was **never below 7.2 seconds** across 59
#: real episodes — the carousel has thirteen others to try first, and a lap
#: costs more than the rest did. The constant only binds when the lap is
#: shorter than the rest, which means a pool of one or two keys, and there it
#: was four dead seconds per blip on the only credential available.
#:
#: 1.7.0.4 removed the ladder that used to sit behind this — 5s, 10s, 20s, 40s,
#: 80s, capped at 90. That historical decision assumed failed requests were
#: unmetered. The 1.7.0.6 review did not establish that assumption across
#: providers: quota accounting remains unknown. The one-second policy is
#: retained for compatibility, not as proof of free retries.
#:
#: What settled it was the ladder climbing on a *healthy* pool. The strike
#: counter is per key and only that key's own success clears it, so a key that
#: caught two blips while thirteen others answered normally still climbed. On
#: the owner's own evidence, 7 of the 16 escalations above the base happened
#: with another key answering inside the previous two minutes — one of them
#: sidelining a healthy key for **40 seconds** while its neighbour was serving.
#: A 5xx is the *model's* condition, not the credential's, and a counter that
#: asks "how many times has this key seen one" is asking the wrong question.
#:
#: The cost of removing it, named rather than discovered later: during a
#: genuinely sustained outage the pool never goes fully cold, so
#: ``_wait_for_recovery`` never sleeps and the carousel keeps turning at about
#: one call a second. A Gemini outage of 83 minutes is on record. That is the
#: eternal carousel working as specified (`decisions/0002`), and the
#: storm-collapse in the log keeps it readable — but it is a
#: real trade and the owner made it knowingly.
SERVER_BASE_S = 1.0

#: Historical cap retained as an import-compatible constant. Since1.7.0.7 it
#: does not truncate explicit server retry instructions; the global horizon
#: still bounds malformed/extreme waits like every other failure family.
SERVER_BACKOFF_CAP_S = 90.0

#: NOT IMPLEMENTED HERE, on purpose, and the reason is worth keeping.
#:
#: The host shortens a transient rest to sixty seconds when a pool holds one
#: credential (``EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS``), for a good reason:
#: benching the only key there is does not route the next request elsewhere, it
#: stops the agent. Copying it here was tried and backed out.
#:
#: What it foundered on is that ``mark`` cannot tell how big the pool is. The
#: carousel remembers only keys it has already seen fail, so "one entry in my
#: memory" reads the same for a genuinely single-key install and for a pool of
#: fourteen on its first refusal — and six tests said so immediately by holding
#: numbers that a one-key reading would have clamped.
#:
#: Where the pool size is genuinely known, the rule is already applied:
#: ``dispatch_binding._rest_after_drop`` checks ``healthy_count`` and rests a
#: sole survivor for nothing at all. Extending that to every refusal means
#: threading the candidate list down here, which is a real change and not a
#: constant, so it is left named rather than half-done.
_SOLE_KEY_RULE = "see dispatch_binding._rest_after_drop"

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
#: **1.8.0.1: twenty seconds measured too short — but the 45 it chose was
#: wrong.** The owner's 1.8.0.0 session, 2026-09-19 22:29–22:40 local: 172
#: real calls, 39 answered, 133 rotated — 129 of the refusals were bare
#: ``429 RESOURCE_EXHAUSTED`` with no ``quotaId``, no ``retryDelay`` and no
#: header but ``date``, plus 5×503. 1.8.0.1 derived 45s from a curve built on
#: that session, and the curve double-counted: a credential retried several
#: times inside one window had its same retry counted once per earlier
#: refusal it followed, inflating every rest past the low end.
#:
#: **1.8.0.2: recounted, corrected to 30.** An independent reviewer, who
#: never read this module, re-derived the number from the same logs and
#: found the same bug from the other direction. Counting each retry exactly
#: once, over the whole real corpus (471 retries that followed a bare 429):
#:
#: .. list-table::
#:    :header-rows: 1
#:
#:    * - rest
#:      - refused calls avoided
#:      - successes delayed
#:      - net seconds returned to the user
#:    * - 20s (before 1.8.0.1)
#:      - 6
#:      - 0
#:      - +5
#:    * - **30s**
#:      - **388**
#:      - 6
#:      - **+304**
#:    * - 45s (1.8.0.1)
#:      - 394
#:      - 9
#:      - +200
#:    * - 60s
#:      - 409
#:      - 9
#:      - +77
#:    * - 90s
#:      - 413
#:      - 11
#:      - −236
#:
#: Thirty is the argmax under this corrected counting and under the
#: reviewer's own, independently-derived one (+480 for 30s against +385 for
#: 45s — see ``research/1.8.0.1/expected/rest-decision.md``). Almost all of
#: the waste — 388 of 413 — is already gone at 30s; every second past that
#: buys roughly six more avoided calls and costs three more delayed
#: successes at growing delay. A refused round trip was measured at about
#: **850ms**, not the "milliseconds" the module docstring above assumes for
#: the general case — which is what makes this particular trade computable
#: rather than assumed. Zero — spinning — is still worse at either count: 0
#: of 73 retries under 30s answered in the original session, so nothing
#: under that mark was ever buying an answer, only spending requests.
#: Google's own troubleshooting guidance agrees with the direction, not the
#: mechanism: it recommends exponential backoff from ~1s with jitter for a
#: 429, and says the quota is per project, not per key — this module's case
#: for a flat rest instead of a ladder is argued in the module docstring
#: above and is unchanged by this.
#:
#: Thirty seconds and flat, with no climb behind it. See :meth:`_escalate`
#: for why there is nothing to climb between, and :data:`settings.
#: UNSIZED_THROTTLE_REST`/``KAME_UNSIZED_REST`` for the dial that lets the
#: owner try zero, or any other number in 0–300s, against his own traffic.
UNSIZED_THROTTLE_REST_S = 30.0

#: 1.8.1.0. Default for :attr:`Carousel.unsized_throttle_backoff` —
#: ``settings.UNSIZED_THROTTLE_BACKOFF`` / ``KAME_UNSIZED_BACKOFF``. On,
#: because this release ships the experiment turned on for the owner to run
#: against his own traffic; off restores the flat rest above exactly as it
#: was before this release. See :meth:`Carousel._escalate` for the ladder and
#: for the measurement this experiment tests against — decisions/0007.
UNSIZED_THROTTLE_BACKOFF_DEFAULT = True

#: 1.8.1.0. The rung the doubling ladder stops at: 1, 2, 4, 8, 16, 32, 64,
#: then 64 again. ``settings.UNSIZED_BACKOFF_MAX`` / ``KAME_UNSIZED_BACKOFF_MAX``
#: moves it; 3600 gives the uncapped doubling the owner first asked for, still
#: bounded by ``max_hold_s``. Sixty-four because his measured recovery for
#: this shape is 23-34s at the fastest, and the rung after 32 is the first one
#: past it — see the replay comparison in research/1.8.1.0.
UNSIZED_BACKOFF_MAX_S = 64.0


def is_bare_resource_exhausted(evidence: Any, *, stated: bool = False) -> bool:
    """Whether a refusal is exactly the owner's case, judged by its payload.

    HTTP 429, a structured error status of ``RESOURCE_EXHAUSTED``, no stated
    retry number of any kind (``stated``, plus no ``retryDelay`` and no
    ``Retry-After`` on the evidence), and no ``quotaId``. That is 129 of 129
    refusals in the session the owner tested, and it is the only shape the
    doubling backoff applies to.

    Evidence, never the provider's name (R01): any provider that refuses in
    exactly this shape is treated the same, and a Gemini refusal that carries
    a quotaId or a retry number is not. The structured status is read from
    the parsed body's ``error.status``, from a string ``status`` the
    exception carries, or from the host's own ``HTTP 429 (RESOURCE_EXHAUSTED)``
    rendering of it — the form the owner's recorded log holds.
    """
    if stated or evidence is None:
        return False
    if getattr(evidence, "status_code", None) != 429:
        return False
    if getattr(evidence, "retry_after", None) is not None:
        return False
    body = getattr(evidence, "body", None)
    text = " ".join(
        str(getattr(evidence, name, "") or "")
        for name in ("body_text", "raw_message")
    )
    details = getattr(evidence, "details", None) or {}
    headers = getattr(evidence, "headers", None)
    haystack = (text + " " + repr(details) + " " + repr(body) + " " + repr(headers)).lower()
    if "quotaid" in haystack or "quota_id" in haystack or "retrydelay" in haystack:
        return False
    if re.search(r"retry[- ]after", haystack):
        return False
    structured = ""
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            structured = str(inner.get("status") or "")
    if not structured:
        structured = str(getattr(evidence, "code", "") or "")
    if structured.strip().upper() == "RESOURCE_EXHAUSTED":
        return True
    return bool(re.search(r"HTTP 429 \(RESOURCE_EXHAUSTED\)", str(getattr(evidence, "raw_message", "") or "")))

#: Kinds that name a counter longer than a rolling minute. Only these teach
#: ``Carousel._named_window``; a per-minute throttle names a window that has
#: already closed by the time the next refusal arrives, so remembering it
#: would say nothing the next refusal does not say better.
NAMED_WINDOW_KINDS = frozenset({"daily", "insufficient_quota"})

#: How long the **whole pool** must go without a single answer before a daily
#: label is believed and the hour is applied.
#:
#: Twenty minutes, and the number is measured rather than chosen. On the
#: owner's fourteen keys, 2026-09-06/07, probed once every two minutes for
#: fifty minutes on each of four models, the longest the entire pool ever went
#: without one key answering **while the model still had capacity** was:
#:
#: ===============  =======
#: model            silence
#: ===============  =======
#: 3.8-flash        15 min
#: 3.7-flash        14 min
#: 3.6-flash        12 min
#: ===============  =======
#:
#: So a pool that has been quiet for twenty minutes is not being throttled, it
#: is out — and until it has been quiet that long, the label saying otherwise
#: is contradicted by the pool's own traffic.
#:
#: Erring long here is the cheap direction, unusually for this module: being
#: slow to believe the label costs refused requests, which are free of quota
#: and take about a second, while believing it early costs every key in the
#: pool for an hour. That asymmetry is what sets the whole design.
POOL_SILENCE_BEFORE_THE_DAY_S = 1200.0

#: A daily refusal escalates from this base toward ``DAILY_COOLDOWN_S``.
DAILY_BASE_S = 20.0

#: A read timeout says nothing about the *key* — it is the host's own read
#: deadline firing (``HERMES_API_TIMEOUT``/``request_timeout_seconds``), not
#: a refusal from the provider at all. R36: KAME must add no timeout of its
#: own, and a nonzero rest here was exactly that — an invented wait for a
#: credential this module has no evidence against.
#:
#: Zero, not the three seconds this used to be. The answer key's own reading
#: (research/1.8.0.0/expected/verdicts.jsonl real-10, 21 real rows, one bad
#: stretch on 2026-09-06) is stricter still — ``defer_host_retry``, hand the
#: whole retry back to the host — and this release does not go that far:
#: ``is_terminal`` already never routes a timeout to "raise" (see its own
#: docstring), and rotating instead of returning to the host is free — one
#: call, no wait, and it keeps the turn moving rather than stalling it on one
#: credential the timeout never implicated. What changes is only the bench:
#: the key stays immediately selectable, earns no strike (kind "timeout" was
#: already outside RETIRING_KINDS/REJECTED_KINDS, so nothing was learned about
#: the *credential* before either), and nothing invented replaces the old
#: three seconds.
TIMEOUT_S = 0.0

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

#: A refusal that names the *credential* specifically — a gateway's "Invalid
#: token" — without meeting the bar that retires a key outright (see
#: ``INVALID_KEY_INDICATORS``). Still the ambiguous ``auth`` kind (retried,
#: retired only after :data:`REFUSALS_BEFORE_RETIRING` in a row: R34, the
#: same wording covers a relayed upstream 401 and a mis-propagated
#: credential), but ``REJECTED_REST_S``'s twenty seconds is the unsized-
#: throttle number wearing this kind's clothes — see
#: :data:`~.quota.DEFAULT_CREDENTIAL_PROBLEM_BENCH_SECONDS` for the
#: measurement and R22/R23.
CREDENTIAL_PROBLEM_REST_S = DEFAULT_CREDENTIAL_PROBLEM_BENCH_SECONDS

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

#: Phrases that name a *token* or *credential* as the problem without meeting
#: the bar above — a one-api/new-api style gateway (aihubmix, tokenrouter.com
#: and others sharing that envelope) answers a rejected token with exactly
#: this wording, distinct from its own "token has expired" / "token quota is
#: exhausted" strings for the *same* gateway family (same shape, different
#: fact — the wording is the only separator, R34). Deliberately kept out of
#: ``INVALID_KEY_INDICATORS``: that tuple retires a key the moment it is seen,
#: and this evidence is exactly as ambiguous as a bare 401 — a relayed
#: upstream refusal or a temporarily mis-propagated credential reads the same
#: way. So it only widens what ``is_auth_failure`` recognises (a message with
#: no status code still reads as an auth failure) and what the *rest* is for
#: the ambiguous ``auth`` branch below — see ``CREDENTIAL_PROBLEM_REST_S``.
#: Sources: research/1.8.0.0/expected/verdicts.jsonl real-11 ("Invalid token",
#: the measured shape); https://github.com/QuantumNous/new-api's own
#: ``token.invalid``/``token.expired`` strings; https://docs.aihubmix.com's
#: "access token is invalid or expired" on the same gateway family.
CREDENTIAL_PROBLEM_INDICATORS = (
    "invalid token",
    "token is invalid or expired",
    "access token is invalid or expired",
)

#: Phrases that mean the account is refused rather than throttled. Same
#: treatment as a daily cap: rest it long, keep using the others.
#: Never a denial, whatever else the text says. Mirrors Agent Zero's
#: ``_DENIAL_EXCLUDED_ON_PURPOSE``: an exception whose class names
#: authentication is about the credential however the message is worded, and a
#: missing model is about the request. Both would otherwise be caught by the
#: denial list and benched for an hour as a refused pairing.
_DENIAL_EXCLUDED = (
    "model not found",
    "authenticationerror",
    "permissiondeniederror",
)


PERMANENT_DENIAL_INDICATORS = (
    "permission denied",
    "permissiondenied",
    # 1.7.0.0. ``permission_denied`` with the underscore is how Google writes
    # it, and nothing here matched it — substring matching does not normalise
    # separators. So ``403 PERMISSION_DENIED`` was classified ``auth``: a
    # refusal of the *pairing* treated as a refusal of the *credential*, which
    # counts toward retiring a key that is in perfect health. That is 1.6.0.1's
    # defect, still live in the phrasing the provider most often uses.
    "permission_denied",
    "denied access",
    "model not authorized",
    "model not available",
    "does not include access to",
    "does not yet include access to",
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
    # 1.7.0.0: the rest of the Agent Zero list, which the port had left behind.
    # Measured 04/09/2026: nine phrasings this plugin met in the wild, and it
    # classified all nine wrong. A throttle a provider spells without the words
    # "rate limit" and without a 429 — "you are being throttled", "concurrency
    # limit reached", "usage limit reached" — fell through to ``other`` and got
    # a flat twenty-second bench with no idea what it was waiting for.
    "throttl",
    "usage limit reached",
    "limit exhausted",
    "concurrent limit",
    "concurrency limit",
    "concurrent requests limit",
    "quota_exceeded",
    "quota left",
    "no quota",
    "tokens per min",
    "tokens per hour",
    "tokens per day",
    "tokens per week",
    "tokens per month",
    "requests per min",
    "requests per hour",
    "requests per day",
)

#: Phrases that mean the *daily* or *account* allowance, not the per-minute one.
DAILY_INDICATORS = (
    "per day",
    "perday",
    "daily limit",
    "per-day",
    "requests per day",
    "generaterequestsperday",
    # 1.7.0.0. Three phrasings the port dropped, each of which a provider
    # actually uses. Without them "daily quota exceeded" and "RPD exceeded"
    # were read as per-minute throttles and rested twenty seconds — the same
    # defect this release is named for, arriving in prose instead of in a
    # ``quotaId``.
    "daily",
    "rpd",
    "/day",
    # 1.7.0.0 removed ``free_tier_requests``. It is the *metric* name, and
    # Google reports its per-minute and its per-day free-tier quotas under the
    # same one — ``generativelanguage.googleapis.com/generate_content_free_tier_requests``
    # is in the message of both. So this table, reached whenever the
    # evidence-first classifier declines, answered "daily" to every free-tier
    # 429 Gemini sends and benched a key for an hour over a throttle that
    # clears in four seconds. Measured against the real sentence, 04/09/2026.
    #
    # The Agent Zero engine this was ported from carried the rule as a comment
    # over its own list: *"These are intentionally narrow so a per-minute
    # (RPM/TPM) error is NEVER misclassified as daily."* The port widened the
    # list and lost the property the comment existed to protect. Every token
    # here now names a **period**, never a tier and never a metric.
    "insufficient_quota",
    "insufficient quota",
    "credit balance",
    # 1.7.0.0, from the owner's log, 04/09/2026 03:09:09. A provider answered
    # a 403 with::
    #
    #     "User's credit limit is insufficient, remaining credit limit: $0.00"
    #     code: "insufficient_user_quota"
    #
    # An account with no money in it. Both tokens above miss it by one word —
    # ``insufficient_quota`` does not appear in ``insufficient_user_quota``,
    # and the provider wrote ``credit limit`` where this list said ``credit
    # balance``. So it fell through to ``per_minute`` and the key was re-probed
    # **every twenty seconds**, for ever, against a balance that only a payment
    # changes. Waiting is not what repairs this one, but a wrong twenty seconds
    # is what makes it loud.
    "insufficient_user_quota",
    "insufficient user quota",
    "credit limit",
    "out of credit",
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

# 1.7.0.3. The unit alternation lists the *milliseconds* spellings first,
# and that order is the whole point: Python's alternation takes the leftmost
# branch that matches, so with `m` ahead of `ms` the text `683.050353ms` gave
# up its `m`, the trailing `s` was left behind, and the number was read as
# 683 *minutes*. Measured on the owner's own pool on 07/09/2026: five keys
# benched between 4 and 12 hours off sub-second hints, and only a manual
# `/kame clear_pool` let them back in.
#
# Google emits the millisecond spelling whenever the wait is under a second,
# which is exactly when the number is least worth obeying and most expensive
# to misread — the two smallest hints in that session, 252ms and 683ms,
# became the longest benches of the day.
_RETRY_HINT = re.compile(
    r"retry[\s_-]*(?:after|delay|in)?[\"'\s:=]*(\d+(?:\.\d+)?)\s*"
    r"(milliseconds?|millis|msecs?|ms|seconds?|secs?|s|minutes?|mins?|m)?",
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

    1.7.0.3: this module used to carry its own regex for the job, and
    ``core.quota`` carried a second one. They disagreed — quota's knew about
    milliseconds and this one did not — so the same sentence was worth 0.68
    seconds to one reader and 11.4 hours to the other, and which reader ran
    depended on the branch. One parser now, quota's, because it is the one
    that was right; this keeps the bound, which is this module's own rule and
    not part of parsing.
    """
    if not text:
        return None
    total = _quota_parse_duration(text.strip())
    return total if total is not None and 0 < total <= HARD_DELAY_CAP_S else None


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
        status = _status_of(error)
        value = _header_delay(source, include_quota_resets=not (status is not None and 500 <= status < 600))
        if value is not None:
            return value

    match = _RETRY_HINT.search(_text_of(error, message))
    if match:
        try:
            total = float(match.group(1))
        except (TypeError, ValueError):
            return None
        # 1.7.0.3. This was `if unit.startswith("m") and not unit.startswith("ms")`,
        # and the guard could never fire: the regex had no `ms` branch to
        # capture, so a millisecond hint arrived here spelled `m` and was
        # multiplied by sixty. The unit now comes from the same table
        # `core.quota` parses with, so there is one answer to "what is a `m`"
        # in this package instead of two.
        unit = (match.group(2) or "s").lower()
        total *= _UNIT_SECONDS.get(unit, 1.0)
        if 0 < total <= HARD_DELAY_CAP_S:
            return total
    return None


def _stated_or(error: Any, text: str, headers: Any, fallback: float) -> float:
    """The number the provider stated, or ours when it stated none.

    Written once because the same omission shipped in three branches: the 5xx,
    the bare refusal and the denial all returned a constant and threw the
    provider's own ``Retry-After`` away. Measured, a 503 asking for seven
    seconds was rested five and then grown to ninety.

    A credential the provider named **dead** is deliberately not routed here.
    There is nothing to wait for on a key that is not coming back, and a wait
    it happened to state alongside would only delay retiring it.
    """
    stated = extract_delay(error, text, headers)
    return float(stated) if stated and stated > 0 else float(fallback)


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


def _header_delay(headers: Any, *, include_quota_resets: bool = True) -> Optional[float]:
    # One reader for units, dates, case and bounds. A separate header parser
    # silently lost Azure millisecond hints in the legacy server/provenance path.
    from .quota import extract_from_headers, _header_items
    if not include_quota_resets:
        # Counters can be attached even when they did not cause the failure.
        # A server outage owns Retry-After, not an unrelated daily quota reset.
        headers = [(name, value) for name, value in _header_items(headers)
                   if name.strip().lower() in ("retry-after", "retry-after-ms")]
    total, _ = extract_from_headers(headers, time.time())
    return total if total is not None and 0 < total <= HARD_DELAY_CAP_S else None


def is_auth_failure(error: Any, message: str = "", status_code: Optional[int] = None) -> bool:
    """Whether this failure means the key itself is refused.

    Checked *before* the status code, deliberately. Gemini answers an invalid
    key with ``400 INVALID_ARGUMENT: API key not valid``; reading the number
    first classifies it as a malformed request and aborts a run that fourteen
    healthy keys could have finished.
    """
    text = _text_of(error, message)
    if _matches(text, INVALID_KEY_INDICATORS) or _matches(text, CREDENTIAL_PROBLEM_INDICATORS):
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
        # A 503 that names its own wait is obeyed, like every other refusal
        # that names one.
        #
        # This branch used to return the constant unconditionally, and the
        # ladder above then grew it: measured, a provider asking for seven
        # seconds was rested five, then ten, twenty, forty, eighty. The rule
        # has one line for this — a number the provider stated outranks a
        # number we invented — and the server branch was the last place still
        # arguing with it.
        #
        # The constant stays as the floor for the ordinary case, where a 503
        # carries no header at all, and the ladder keeps growing *that*, which
        # is the legitimate half: spacing out probes when nobody said when.
        stated = extract_delay(error, text, headers)
        if stated and stated > 0:
            return float(stated), "server", (status or 503)
        return SERVER_BASE_S, "server", (status or 503)

    # The provider used the words. This is the only branch that may retire a
    # key on sight, and the vocabulary it reads is deliberately narrow — see
    # the note under INVALID_KEY_INDICATORS about ``unauthorized``, which was
    # in that tuple until 1.4.0 and cost twenty-one healthy keys an hour each.
    if _matches(text, INVALID_KEY_INDICATORS):
        return REJECTED_REST_S, "revoked", (status or 401)

    # 1.7.0.0 moved this **above** the bare-auth branch, where the Agent Zero
    # engine has always had it.
    #
    # "This key may not use this model." The key is not the problem, the
    # pairing is, so the hour is honest here — nothing about an authorisation
    # moves on its own — and it costs nothing, because the carousel's health
    # is per provider:model.
    #
    # It used to sit *after* ``is_auth_failure``, which answers yes to any 403.
    # So a ``403 PERMISSION_DENIED`` — Google's own wording, and the commonest
    # form of this refusal — never reached here at all: it was classified
    # ``auth``, which counts toward ``REFUSALS_BEFORE_RETIRING`` and can retire
    # a credential that is in perfect health everywhere else. That is exactly
    # the defect 1.6.0.1 was written to fix, still live in the phrasing the
    # provider actually uses, because the fix was made in the *verdict* path
    # and this table was left in the old order.
    #
    # The exclusion mirrors A0's ``_DENIAL_EXCLUDED_ON_PURPOSE``: an exception
    # whose *class* names authentication is about the credential whatever its
    # message says, and "model not found" is about the request.
    if _matches(text, PERMANENT_DENIAL_INDICATORS) and not _matches(
        text, _DENIAL_EXCLUDED
    ):
        return _stated_or(error, text, headers, DENIED_REST_S), "denied", (status or 403)

    if is_auth_failure(error, message, status_code):
        # A bare 401 or 403: the provider refused and said nothing about why,
        # so this is the *ambiguous* kind. Short rest, demoted, and retired
        # only once REFUSALS_BEFORE_RETIRING of them arrive in a row.
        #
        # "Short" is not one number, though. A refusal that names the token
        # specifically (CREDENTIAL_PROBLEM_INDICATORS) is real evidence this
        # is a credential problem, not a throttle — REJECTED_REST_S's twenty
        # seconds belongs to a throttle nobody could size (R23), and using it
        # here too was never a decision, just the two kinds sharing one
        # constant. A truly wordless refusal (``"Unauthorized"``, nothing
        # else) has no such evidence and keeps the short opening.
        base = (
            CREDENTIAL_PROBLEM_REST_S
            if _matches(text, CREDENTIAL_PROBLEM_INDICATORS)
            else REJECTED_REST_S
        )
        return _stated_or(error, text, headers, base), "auth", (status or 401)

    if status == 429 or _matches(text, RATE_LIMIT_INDICATORS):
        if _matches(text, DAILY_INDICATORS):
            kind = "insufficient_quota" if "insufficient" in text else "daily"
            # The parsed delay is deliberately dropped here. See the module
            # docstring: a daily cap that claims to clear in seconds is the
            # single most expensive lie a provider tells a rotation engine.
            return daily_cooldown_s, kind, (status or 429)
        parsed = extract_delay(error, message, headers)
        return (parsed if parsed is not None else OTHER_S), "per_minute", (status or 429)

    return OTHER_S, "other", status


# --- the state --------------------------------------------------------------


def _fresh(now: float) -> Dict[str, Any]:
    return {
        "sick_until": 0.0,
        "non_server_until": 0.0,
        "stated_server_until": 0.0,
        "last_used": 0.0,
        "last_sick_at": 0.0,
        "request_log": [],
        "consecutive_rl": 0,
        "consecutive_server": 0,
        # 1.8.1.0. Consecutive refusals on this credential+model that reached
        # the truly-unsized branch of the throttle ladder below — no delay,
        # no ceiling either, nothing the provider ever named. A separate
        # counter from ``consecutive_rl`` above, which is shared by three
        # unrelated kinds (daily, denied/auth/revoked, and this one) and would
        # let an unrelated refusal advance a ladder it never fed. Reset by a
        # success and by any refusal where the provider names a number, on
        # this credential+model only — see :meth:`Carousel._escalate`.
        "consecutive_unsized_throttle": 0,
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
        # 1.8.0.0. When THIS process last learned anything about this key on
        # this identity — success or failure alike, set on every ``mark()``.
        # The shared file carries the same idea per event (``at``), and the
        # two are compared, never blindly maxed: a fresher shared success has
        # to be able to zero out an older *local* bench for decisions/0006's
        # promise to hold ("a success in profile k releases the key base had
        # benched"), which a plain ``max(local_until, shared_until)`` cannot
        # do — zero never wins a max against a positive number. See
        # ``Carousel._model_deadline``.
        "health_at": 0.0,
    }


class _RetainedCooldown(float):
    """Per-call metadata, without racing through a mutable last-result field."""

    def __new__(cls, remaining, held_until):
        result = super().__new__(cls, remaining)
        result.retained = True
        result.held_until = held_until
        return result


class Carousel:
    """Per-``provider:model`` key health, and the rule for choosing the next key.

    One instance is shared by every call in the process, so the lock is real
    and every mutation happens under it. Selection stamps the chosen key
    *inside* the lock — that is the anti-dogpile guarantee, and moving the
    stamp outside would quietly reintroduce two turns picking the same key.
    """

    def __init__(
        self,
        *,
        daily_cooldown_s: float = DAILY_COOLDOWN_S,
        max_hold_s: float = MAX_HOLD_S,
        unsized_throttle_rest_s: float = UNSIZED_THROTTLE_REST_S,
        unsized_throttle_backoff: bool = UNSIZED_THROTTLE_BACKOFF_DEFAULT,
        unsized_backoff_max_s: float = UNSIZED_BACKOFF_MAX_S,
        shared_health_store: Optional[shared_health.SharedHealth] = None,
    ) -> None:
        self._lock = threading.RLock()
        #: 1.8.0.0. A real handle that does nothing until ``KAME_SHARE_POOL_
        #: HEALTH`` says otherwise and a root can be derived — see
        #: ``shared_health``'s own docstring. Passed in by tests that want two
        #: ``Carousel``s to share one temp file; a real one is built lazily
        #: from the environment otherwise, the same convention ``ENGINE``
        #: itself is constructed under at import time, before any host
        #: environment is necessarily set up yet.
        self._shared = shared_health_store if shared_health_store is not None else shared_health.SharedHealth()
        self._pools: Dict[str, Dict[str, Dict[str, Any]]] = {}
        #: The longest rest this identity's provider has ever *asked* for on a
        #: throttle, per credential and ``provider:model``. It is not a cooldown and nothing
        #: rests for it — it is the ceiling on what KAME is allowed to invent
        #: when a later throttle arrives with no number at all. See
        #: :meth:`_escalate`.
        self._stated_rl_ceiling: Dict[Tuple[str, str], float] = {}
        #: The last window the provider **named**, per credential and
        #: ``provider:model``. Google can send terse and detailed refusals; only
        #: one of them says which counter is spent — measured on the owner's
        #: 06/09 session, 447 of 594 recorded 429s arrived as the terse
        #: ``"Resource has been exhausted (e.g. check quota)."`` with no
        #: ``quotaId``, no ``retryDelay`` and no details at all, against 51
        #: that carried the lot.
        #:
        #: Read on its own each terse refusal is a throttle, so it rested 20
        #: seconds and the pool was swept again twenty seconds later: 251
        #: refusals in seven minutes on ``gemini-3.7-flash``, every one of
        #: them against a day that was already spent. It only stopped when a
        #: verbose refusal happened to arrive and said ``PerDay``.
        #:
        #: This is the same reasoning ``_stated_rl_ceiling`` above already
        #: applies to the provider's *number*, and 1.6.0.3 wrote the sentence
        #: for it: the terse form "is not a different condition, it is the
        #: same condition worded shorter". What was missing is that the
        #: *window* is learnable the same way.
        #:
        #: Since 1.7.0.6 this memory is per credential and model. Only that
        #: credential's success clears it. Another independent account
        #: answering does not prove this account has recovered.
        # Account/project limits belong to the credential, not every key on
        # the model. A daily label on A is not evidence about independent B.
        self._named_window: Dict[Tuple[str, str], str] = {}

        #: When the current run of daily labels with **no answer in between**
        #: started, per identity. 1.7.0.2, and it is the only thing that
        #: separates a day that is over from a label that says so and is wrong.
        #:
        #: Measured on the owner's keys, 2026-09-06: a key labelled
        #: ``GenerateRequestsPerDayPerProjectPerModel-FreeTier`` answered again
        #: 6 to 36 minutes later, twenty-one times, and never once needed an
        #: hour. A day that is genuinely spent serves nothing until the
        #: provider's reset, so those refusals were not about the day.
        #:
        #: One answer anywhere on the identity clears this — the same reasoning
        #: as ``thaw_server_cooled``, which the owner wrote for 5xx: when one
        #: credential gets through, the *provider* is serving, and what the
        #: others are resting on is not what they think it is.
        #:
        #: Starts at the first daily label rather than at process start, so a
        #: session whose very first call is refused still gets the short probe
        #: instead of an hour of silence bought on no evidence at all.
        self._no_answer_since: Dict[str, float] = {}

        #: R29 keys every pool by ``provider:model`` because most quotas are
        #: metered per model — a key spent on ``gemini-3.7-flash`` is whole on
        #: ``gemini-3.6-flash``. Some refusals say otherwise: Codex's
        #: ``usage_limit_reached`` carried the SAME absolute reset across two
        #: different models on the owner's own machine (real-17,
        #: ``research/1.8.0.0/expected/verdicts.jsonl`` — 19:42 on
        #: ``gpt-5.6-luna`` and 20:00 on ``gpt-6-astra`` both resolve to
        #: ~23:13 UTC), which is proof the window belongs to the *account*,
        #: not the model. Reading a ``provider:model`` pool as if it covered
        #: the whole account would break R29 for the common case; reading
        #: every refusal as per-model breaks it for this one. Both stay true
        #: by keeping them in separate structures: ``_pools`` is still
        #: ``provider:model``, and this is the one place a credential's
        #: health is tracked by ``(provider, key)`` alone, consulted
        #: *alongside* ``sick_until`` — never replacing it — in
        #: :meth:`select`, :meth:`healthy_count` and
        #: :meth:`next_recovery_seconds`.
        #:
        #: Only :meth:`mark` writes here, and only when the caller passes
        #: ``scope=QuotaScope.ACCOUNT`` — evidence ``core.quota.
        #: detect_quota_scope`` produced from the refusal itself (R01: never
        #: from the provider's name). A kind this module invents no evidence
        #: for (``server``, ``timeout``, the bare ladder kinds with no scope
        #: hint) never sets this dict, so R29's per-model pool is exactly as
        #: narrow as it always was for every refusal that isn't proven
        #: account-wide.
        self._account_hold: Dict[Tuple[str, str], float] = {}
        #: 1.8.0.0. Parallel to ``_account_hold``, and by the same
        #: (provider, key) key: WHEN this process last learned the account-
        #: wide fact it holds, set on every touch — both when a hold is
        #: recorded and when a success pops it. Mirrors ``state["health_at"]``
        #: for the per-``provider:model`` bench; see that field's comment for
        #: why "the later of local and shared" has to mean "the fresher
        #: event", not a numeric ``max()``.
        self._account_hold_at: Dict[Tuple[str, str], float] = {}
        self.daily_cooldown_s = float(daily_cooldown_s)
        #: The owner's ceiling on any single hold — see :data:`MAX_HOLD_S`.
        #: Read fresh in :meth:`mark`, so lowering it mid-incident (the host
        #: sets this from ``settings.MAX_HOLD`` the same way it sets
        #: ``daily_cooldown_s`` from ``settings.DAILY_COOLDOWN``) trims every
        #: credential's stored hold on its very next mark rather than only
        #: bounding new ones — see the never-shorten note in :meth:`mark`.
        self.max_hold_s = float(max_hold_s)
        #: What an unsized per-minute/rate-limit throttle rests for — see
        #: :data:`UNSIZED_THROTTLE_REST_S` for the measurement behind the
        #: default. Read fresh in :meth:`_escalate`, the same way
        #: ``max_hold_s`` is read fresh in :meth:`mark`, so the owner's dial
        #: (``settings.UNSIZED_THROTTLE_REST`` / ``KAME_UNSIZED_REST``) reaches
        #: every hold this engine computes from here on, not only ones built
        #: after the dial was set.
        self.unsized_throttle_rest_s = float(unsized_throttle_rest_s)
        #: 1.8.1.0. Whether an unsized throttle's rest doubles per consecutive
        #: refusal on the same credential+model (1, 2, 4, 8 …) instead of
        #: staying flat at :attr:`unsized_throttle_rest_s`. See the ladder
        #: itself in :meth:`_escalate` for the measurement this experiment is
        #: run against. Read fresh, the same way ``unsized_throttle_rest_s``
        #: and ``max_hold_s`` are, so the owner's dial
        #: (``settings.UNSIZED_THROTTLE_BACKOFF`` / ``KAME_UNSIZED_BACKOFF``)
        #: reaches every hold this engine computes from here on.
        self.unsized_throttle_backoff = bool(unsized_throttle_backoff)
        #: 1.8.1.0. Where the ladder stops growing and holds — see
        #: :data:`UNSIZED_BACKOFF_MAX_S`. ``max_hold_s`` still bounds it too.
        self.unsized_backoff_max_s = float(unsized_backoff_max_s)
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

    @staticmethod
    def _provider_of(identity: str) -> str:
        """The provider half of an ``identity()`` string.

        The same split ``dispatch_binding`` already does to log a rotation's
        provider (``dispatch_binding.py``, ``provider_name = identity.
        split(":", 1)[0]``) — one definition of "provider" in this plugin,
        not two that could drift apart.
        """
        return identity.split(":", 1)[0] if ":" in identity else identity

    def _account_until(self, provider: str, key: str) -> float:
        """The account-wide deadline for this credential, or ``0.0`` if none.

        Read by :meth:`select`, :meth:`healthy_count` and
        :meth:`next_recovery_seconds` *alongside* ``sick_until`` — this is
        the "consulted by select/healthy_count/eta" half of the account-scope
        design; the per-``provider:model`` bench is never replaced by it.
        """
        return self._account_hold.get((provider, key), 0.0)

    # -- shared health (1.8.0.0, decisions/0006) --------------------------

    def _model_deadline(
        self, state: Dict[str, Any], identity: str, key: str, now: float, shared_active: bool
    ) -> float:
        """The per-``provider:model`` deadline for ``key``, local or shared.

        **Not** a ``max()`` of the two numbers. A shared success (``until:
        0``) has to be able to zero out an older *local* bench for
        decisions/0006's own promise to hold — "a success in profile k
        releases the key base had benched" — and zero can never win a
        ``max()`` against a positive number. So the two sources are compared
        by **freshness** instead: whichever side last learned something
        newer about this credential is the one believed, exactly the same
        "newest event wins" rule :func:`shared_health.SharedHealth.record`
        already applies when two profiles write to the file at once. Local
        wins ties (both zero, nothing known anywhere), which is also correct
        — a key nobody has ever seen is healthy by default either way.
        """
        local_until = state.get("sick_until", 0.0)
        if not shared_active:
            return local_until
        local_at = state.get("health_at", 0.0)
        shared_until, shared_at = self._shared.model_entry(identity, fingerprint(key), now)
        # RED_TEAM.md F6 (2026-09-19): ``shared_until`` comes off disk
        # unbounded — it is whatever some OTHER profile's ``mark()`` wrote,
        # clamped by THAT profile's own ``max_hold_s``, which the owner may
        # have set differently (three profiles, ``decisions/0006``). Before
        # this, a profile with a tighter dial than its sibling would honour
        # the sibling's looser number verbatim: measured, a writer's 9h hold
        # read back as a 9h ``next_recovery_seconds`` in a reader whose own
        # ceiling was 1h. This is also how a backwards clock step could hold
        # a key past the ceiling forever — an absolute ``until`` written
        # before the step sits further into the future in the new frame, and
        # nothing here re-derives it until this reader's own ``mark`` runs.
        # Re-bounding by THIS reader's ceiling on every read, not only on
        # write, is what makes "no credential sits out longer than this"
        # true regardless of whose number produced the hold.
        return min(shared_until, now + self.max_hold_s) if shared_at > local_at else local_until

    def _account_deadline(self, provider: str, key: str, now: float, shared_active: bool) -> float:
        """The account-wide deadline for ``key``, local or shared.

        Same freshness comparison as :meth:`_model_deadline`, against
        ``_account_hold``/``_account_hold_at`` instead of the per-identity
        pool — see that pair's own comments for why an account fact needs
        its own timestamp separate from any one model's bench.
        """
        local_until = self._account_hold.get((provider, key), 0.0)
        if not shared_active:
            return local_until
        local_at = self._account_hold_at.get((provider, key), 0.0)
        shared_until, shared_at = self._shared.account_entry(provider, fingerprint(key), now)
        # RED_TEAM.md F6 (2026-09-19): same re-bounding as :meth:`_model_
        # deadline`, and for the same reason — ``shared_until`` here is a
        # sibling profile's account-wide hold, clamped by ITS ceiling, not
        # this reader's.
        return min(shared_until, now + self.max_hold_s) if shared_at > local_at else local_until

    def _combined_until(
        self,
        pool: Dict[str, Dict[str, Any]],
        identity: str,
        provider: str,
        key: str,
        now: float,
        shared_active: bool,
    ) -> float:
        """The deadline :meth:`select`, :meth:`healthy_count` and
        :meth:`next_recovery_seconds` all judge a key by: the later of its
        per-model and its account-wide deadline, each already reconciled
        against whatever the shared file knows.
        """
        state = pool.get(key) or {}
        return max(
            self._model_deadline(state, identity, key, now, shared_active),
            self._account_deadline(provider, key, now, shared_active),
        )

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
            provider = self._provider_of(identity)
            before_mirror = set(pool)
            self._mirror(pool, usable, now)
            for memory in (self._stated_rl_ceiling, self._named_window):
                for subject in list(memory):
                    if subject[0] == identity and subject[1] not in pool:
                        memory.pop(subject, None)
            for removed in before_mirror.difference(pool):
                self._named_window.pop((identity, removed), None)
            for key in usable:
                pool[key]["request_log"] = [t for t in pool[key]["request_log"] if t > cutoff]

            # A key held by an account-wide refusal on a SIBLING model is not
            # healthy here either — see ``_account_hold``. ``max`` rather than
            # a separate branch: whichever deadline is further out is the one
            # that actually governs, and a key can be resting on both at once
            # (a per-model throttle on this model, an account hold from a
            # refusal on another).
            #
            # 1.8.0.0: a sibling Hermes profile's own last word on this same
            # credential joins the reckoning too (decisions/0006), via
            # ``_combined_until`` — see that method for why it is a
            # freshness comparison and not a third term in this ``max``.
            # ``shared_active`` is checked once per call rather than inside
            # the closure so a disabled switch costs this hot path nothing
            # beyond one flag read, not a fingerprint hash per candidate key.
            shared_active = self._shared.active()

            def _until(k: str) -> float:
                return self._combined_until(pool, identity, provider, k, now, shared_active)

            healthy = [k for k in usable if _until(k) <= now]

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
                soonest = min(usable, key=_until)
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
        stated: bool = False,
        calendar_reset: bool = False,
        scope: str = QuotaScope.UNKNOWN,
        bare_resource_exhausted: bool = False,
    ) -> float:
        """Record one outcome and return the cooldown actually applied.

        ``bare_resource_exhausted`` is 1.8.1.0 and it is evidence about the
        refusal, decided by the caller from the payload
        (:func:`is_bare_resource_exhausted`): the only shape the doubling
        backoff experiment applies to. False — the default, and what every
        caller written before this release passes — keeps the flat rest.

        ``stated`` says the number came out of the provider's own answer rather
        than out of this module. It travels with the number because the ladders
        need to tell the two apart: growing a number we invented is how the
        pool stops hammering when nobody said when, and growing a number the
        provider stated is arguing with it.

        ``calendar_reset`` carries a window-scoped reset: an absolute moment
        or a reviewed endpoint/counter retry instruction. It is distinct from
        a relative RetryInfo hint that may describe the wrong counter.

        ``scope`` is ``core.quota.detect_quota_scope``'s own verdict for this
        refusal, threaded straight through by the caller — never re-derived
        here and never guessed from the provider's name (R01). Only
        ``QuotaScope.ACCOUNT`` does anything: it is the one value that means
        the refusal covers every model this credential can reach, not only
        ``identity``'s, and it is what populates ``_account_hold`` (see that
        dict's own comment for why R29's per-model pool is unaffected by
        every other value, including ``UNKNOWN`` — the default, and the
        pre-existing behaviour for every caller that does not pass this).

        The returned number is what got *stored*, not what was asked for — a
        caller that logs the requested delay while the pool stored something
        else is a caller that lies in the log. The two differ whenever backoff
        escalates, whenever a cap bites, and whenever a longer existing
        cooldown wins.
        """
        if not key:
            return 0.0
        now = time.time() if now is None else now
        provider = self._provider_of(identity)
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
                state["non_server_until"] = 0.0
                state["stated_server_until"] = 0.0
                state["consecutive_rl"] = 0
                state["consecutive_server"] = 0
                # 1.8.1.0. This credential answered — the ladder's own reset
                # condition (see ``_escalate``): the next unsized throttle on
                # it starts back at the first rung.
                state["consecutive_unsized_throttle"] = 0
                # 1.8.0.0. This process just learned something, right now —
                # see the field's own comment in ``_fresh``.
                state["health_at"] = now
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
                # The day is not over if a key just answered on this model.
                # Dropping it here rather than on a timer means the memory
                # cannot outlive the evidence for it by even one call.
                self._named_window.pop((identity, key), None)
                self._stated_rl_ceiling.pop((identity, key), None)
                # And the pool is demonstrably serving, so any run of daily
                # labels that was accumulating doubt starts over. 1.7.0.2.
                self._no_answer_since.pop(identity, None)
                # This credential answering on ANY model of this provider is
                # proof the account is not out — R15 already reasons this way
                # for the escalation streak ("success on a different model...
                # only refutes an account-wide scope"). Unconditional: a
                # success carries no ``scope`` argument of its own (there was
                # no refusal to classify), so this has to run regardless of
                # what this call's ``scope`` happens to default to.
                self._account_hold.pop((provider, key), None)
                self._account_hold_at[(provider, key)] = now
                # 1.8.0.0: a success writes ``until: 0`` to the shared file
                # too, unconditionally — decisions/0006's whole mechanism. A
                # sibling profile may be holding this same credential on the
                # strength of a refusal THIS profile never saw; this is what
                # tells it the refusal is over. Both scopes, because either
                # one may be what another profile is honouring.
                fp = fingerprint(key)
                self._shared.record(scope="model", subject=identity, fingerprint_key=fp, until=0.0, kind="", at=now)
                self._shared.record(scope="account", subject=provider, fingerprint_key=fp, until=0.0, kind="", at=now)
                return 0.0

            state["failures"] += 1
            state["last_sick_at"] = now
            # 1.8.0.0. This failure is itself the freshest thing this process
            # knows about this key on this identity — see ``_fresh``.
            state["health_at"] = now
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
                and stated
                and 0.0 < delay <= RL_BACKOFF_CAP_S
            ):
                # Learned here rather than in ``_escalate`` because the number
                # is the provider's whatever this key does with it, and the
                # lesson belongs to this credential/model episode. Separate
                # accounts can have different counters and reset times.
                #
                # The upper bound is not a clamp, it is a filter on what may
                # teach. A number this long is not describing a rolling
                # window: on Gemini a *daily* cap classifies as ``rate_limit``
                # too, and arrives sized at an hour. Letting that hour in
                # would mean one exhausted day teaching every terse throttle
                # afterwards to rest for an hour, which is the original defect
                # wearing a different hat.
                self._stated_rl_ceiling[(identity, key)] = max(
                    self._stated_rl_ceiling.get((identity, key), 0.0), float(delay)
                )
            if kind in NAMED_WINDOW_KINDS:
                # Remember the counter for THIS credential's terse refusals.
                self._named_window[(identity, key)] = kind
            elif (
                kind in ("per_minute", "rate_limit")
                and not stated
                and delay <= 0.0
                and self._named_window.get((identity, key)) in NAMED_WINDOW_KINDS
            ):
                # A throttle that named nothing at all — no window, no number
                # — on a credential/model whose provider has named a longer
                # window and has not answered since. Narrow on purpose: a
                # refusal carrying its own number is obeyed, because that
                # number is about this refusal and the memory is not.
                kind = self._named_window[(identity, key)]
                state["kind"] = kind
            # 1.7.0.2. Open the doubt on the first daily label and leave it
            # open. ``setdefault`` on purpose: the clock must run from the
            # *first* label of the run, not be pushed forward by each new one,
            # or a pool refusing steadily would never reach the threshold.
            pool_alive = True
            if kind == "daily":
                since = self._no_answer_since.setdefault(identity, now)
                pool_alive = (now - since) < POOL_SILENCE_BEFORE_THE_DAY_S
            applied = self._escalate(
                state, delay, kind, ceiling=self._stated_rl_ceiling.get((identity, key)),
                stated=stated, pool_alive=pool_alive, calendar_reset=calendar_reset,
                bare_resource_exhausted=bare_resource_exhausted,
            )
            # HARD_DELAY_CAP_S is the absolute sanity bound and never moves.
            # ``max_hold_s`` is the operational one — the owner's dial,
            # ``settings.MAX_HOLD`` — and by default it is six times tighter.
            # Both are applied together, here, because this is the single
            # point every branch of ``_escalate`` funnels through before a
            # number becomes a stored cooldown: the stated/calendar-reset
            # branch, the server branch, the ladder, and ``daily_cooldown_s``
            # itself when the owner has set that long. One clamp here bounds
            # all of them without repeating the logic at each return
            # statement above. See ``PLAN_1.8.0.0.md`` G8 and
            # ``research/1.8.0.0/gate/1707.json``'s ``real-17``: five real
            # ``openai-codex`` refusals, each obeying a stated deadline near
            # 12,000s, held a key for 3h+ before this clamp existed.
            applied = max(0.0, min(applied, HARD_DELAY_CAP_S, self.max_hold_s))
            # The pool has one key and it is this one: a long rest here does
            # not send the next request elsewhere, it sends it nowhere. See
            # :data:`SOLE_KEY_TRANSIENT_CAP_S` for why this copies the host and
            # why a spent day is left alone.
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
            #
            # But never-shorten must not defeat the ceiling above it — that is
            # the whole point of having one. A ``sick_until`` stored under a
            # longer ``max_hold_s`` (or before the owner tightened it, or
            # before this release existed at all) can still be further out
            # than ``now + max_hold_s``. Trimming it here, on whatever
            # refusal happens to call ``mark`` next — not only a refusal that
            # would itself have produced a long hold — is what makes the
            # ceiling a real ceiling on what the pool is holding, rather than
            # a limit that only new holds respect.
            previous_until = state.get("sick_until", 0.0)
            sick_until = min(max(previous_until, now + applied), now + self.max_hold_s)
            state["sick_until"] = sick_until
            if kind != "server":
                # A later outage and another account's success do not erase
                # quota/auth evidence on this credential. Track its own floor.
                state["non_server_until"] = max(state.get("non_server_until", 0.0), sick_until)
            elif stated and delay > 0:
                state["stated_server_until"] = max(state.get("stated_server_until", 0.0), sick_until)
            if scope == QuotaScope.ACCOUNT:
                # RED_TEAM.md F9 (2026-09-19): the comment this replaces
                # claimed "the account hold inherits the same ceiling for
                # free" because ``sick_until`` is already clamped — true of
                # the NEW number, false of the one already sitting in
                # ``_account_hold``. ``sick_until`` is safe from this because
                # ``previous_until`` is re-read from ``state`` and re-clamped
                # by ``min(..., now + self.max_hold_s)`` on every single
                # ``mark`` — including one that did not itself produce a long
                # hold, which is what actually re-trims a hold stored under a
                # looser dial. The line below took a plain ``max()`` against
                # the *stored* ``_account_hold`` value instead, so a hold
                # recorded while the dial was wide (or before this ceiling
                # existed) never shrank: measured, an account hold of 1h
                # survived a dial tightened to 60s indefinitely, because
                # every later ``max(3600s, <=60s)`` still picked 3600s.
                # ``min(..., now + self.max_hold_s)`` after the ``max`` — the
                # exact same two-step ``sick_until`` uses just above — keeps
                # the never-shorten rule (a softer refusal on a second model
                # must not cut short a longer account-wide deadline) while
                # still trimming to whatever the dial says right now.
                self._account_hold[(provider, key)] = min(
                    max(self._account_hold.get((provider, key), 0.0), sick_until),
                    now + self.max_hold_s,
                )
                self._account_hold_at[(provider, key)] = now
            # 1.8.0.0. The final, already-clamped ``sick_until`` — never the
            # requested ``applied`` — is what reaches the shared file, so a
            # sibling profile learns exactly what this one actually stored,
            # ceiling included. ``at=now`` is this event's own moment, which
            # is what reconciliation compares (see ``shared_health._write_
            # locked``) — not when the write happens to land.
            fp = fingerprint(key)
            self._shared.record(
                scope="model", subject=identity, fingerprint_key=fp,
                until=sick_until, kind=kind, at=now,
            )
            if scope == QuotaScope.ACCOUNT:
                self._shared.record(
                    scope="account", subject=provider, fingerprint_key=fp,
                    until=self._account_hold[(provider, key)], kind=kind, at=now,
                )
            self.rotations += 1
            if sick_until > now + applied:
                # Either the retained prior hold simply outlasted this
                # refusal's own number (the pre-existing rule), or it did and
                # was then trimmed to the ceiling (the new one) — either way,
                # report what ``sick_until`` actually holds now, never a
                # number the pool has already stopped honouring.
                return _RetainedCooldown(sick_until - now, sick_until)
            return applied

    def _escalate(
        self,
        state: Dict[str, Any],
        delay: float,
        kind: str,
        *,
        ceiling: Optional[float] = None,
        stated: bool = False,
        pool_alive: bool = True,
        calendar_reset: bool = False,
        bare_resource_exhausted: bool = False,
    ) -> float:
        """How long this key rests, given how many times it has said this lately.

        ``ceiling`` is the longest rest this identity's provider has actually
        asked for on a throttle, or ``None`` before it has ever asked for one.
        It bounds the invented ladder and nothing else.

        ``pool_alive`` is 1.7.0.2 and it only matters to the daily branch: it
        says whether any key on this identity has answered recently enough that
        a "the day is over" label is contradicted by the pool's own traffic.
        Computed in ``mark``, which is the only place that can see the pool.
        """
        if kind == "insufficient_quota":
            # Money, not a window. No amount of pool liveness makes a key with
            # no credit work, so this one keeps the hour unconditionally —
            # which is also why 1.7.0.2 splits it out of the branch below
            # rather than leaving the two to share a verdict they no longer
            # share a reason for.
            return max(float(delay), self.daily_cooldown_s)

        if kind == "daily":
            # 1.7.0.0. **A spent day is spent on the first refusal.** This used
            # to share the ladder below: 20s, 40s, 80s, 160s … so a key whose
            # daily quota was genuinely gone needed *eight* refusals to earn an
            # hour off, and spent a request on every rung — against a quota
            # with nothing left to spend. Climbing to an hour is a way of
            # asking "is it over yet?" repeatedly, and the answer to that is
            # already known: the provider said the *day*.
            #
            # So it starts where it used to end. A longer number the provider
            # genuinely stated still wins, because that one is a real deadline
            # about a real reset; the ladder is what goes.
            #
            # 1.7.0.1 puts **one** probe in front of it, and only one, because
            # the owner's machine refuted the sentence above. Measured on his
            # session of 2026-09-06:
            #
            #     14:32:44  key:1521bb  429 daily  ->  benched 3600s
            #     14:40:47  key:1521bb  ANSWERED
            #
            # Eight minutes, not an hour. A day that is spent does not serve a
            # request, so Google attached ``GenerateRequestsPerDayPerProject-
            # PerModel-FreeTier`` to a refusal from a key that was not out of
            # daily quota. The label is real evidence and it is not proof, and
            # only a manual pool reset rescued that key. The same shape was
            # recorded once before, on 2026-09-05 at 19:28, and written down
            # as unexplained; this is the second instance and it has a name.
            #
            # The asymmetry sizes the step, as everywhere else here. Wrong
            # short costs one request that fails in a second — these refusals
            # come back in about 1.0s, measured across all 21 of them. Wrong
            # long costs a healthy key for an hour, and when the label arrives
            # on the whole pool at once it costs the whole pool.
            #
            # So the first refusal is a probe sized by whatever the provider
            # said, and the **second in a row on the same key** — the provider
            # repeating itself after its own wait was served — buys the hour.
            # A genuinely spent day pays one extra request per key for that,
            # which is the entire price of not taking a label's word for it.
            #
            # Deliberately not applied to ``quota.compute_reset_at``: that path
            # sizes the *host's* bench, nothing there escalates from seconds to
            # an hour (``escalate.stretch`` multiplies, and multiplying one
            # second reaches nowhere), and thirteen tests hold that line on
            # measured Agent Zero production evidence. All 21 of the owner's
            # daily benches were sized here, by ``kame``, not there.
            # 1.7.0.2 replaces the strike counter 1.7.0.1 added here. That
            # counter never fired: ``dispatch_binding`` had already replaced
            # ``delay`` with 3600 one call upstream, so its probe computed
            # ``max(3600, DAILY_BASE_S)`` and returned the hour it was meant to
            # postpone — on all 114 of the owner's daily refusals. Counting
            # refusals was also the wrong instrument. A benched key is not
            # asked, so "two in a row" can take an hour to happen or never
            # happen at all; counting what did *not* occur measures nothing.
            #
            # What the pool can measure is answers. Three rules, in order:
            state["consecutive_rl"] += 1

            if (calendar_reset and delay > 0) or (stated and float(delay) >= self.daily_cooldown_s):
                # A deadline the provider genuinely named, and long. OpenRouter
                # states a real one — ``free-models-per-day`` arrives with nine
                # hours in the header, and ``test_binding`` holds that contract
                # because capping it at an hour would spend eight hours of
                # pointless probing. Nothing outranks a real number.
                return min(float(delay), HARD_DELAY_CAP_S)

            if pool_alive:
                # The label says the day is over and the pool says otherwise,
                # by answering. Re-probe flat — and deliberately **not** at the
                # retry hint the payload carries, which is the one place this
                # module knowingly declines a number the provider stated.
                #
                # It declines it because the number is not about this. Of the
                # 114 PerDay refusals recorded on the owner's keys on
                # 2026-09-06, 107 carried a hint equal to the seconds left in
                # the current clock minute — 85 of them exact to the second.
                # A field that counts down to the top of the minute is a clock,
                # not a statement about a daily counter, and "the provider's
                # number beats ours" presumes the provider's number is evidence
                # about the condition. Here it measurably is not, so the rule
                # lands where it always does when nothing was stated: a flat
                # re-probe.
                #
                # Five minutes because that is ``RL_BACKOFF_CAP_S``, the
                # ceiling this module already uses for a throttle it could not
                # size, and because the cadence has to be paid for. At the
                # hint's ~29s a pool of fourteen keys spends about 574 refused
                # calls inside the doubt window below; at five minutes it
                # spends about 56. Both are free of quota and cost about a
                # second each, and one of them is still ten times the other.
                return min(RL_BACKOFF_CAP_S, self.daily_cooldown_s)

            # Nobody has answered on this identity for POOL_SILENCE_BEFORE_THE
            # _DAY_S. The label is no longer contradicted, so believe it.
            return max(float(delay), self.daily_cooldown_s)

        if kind in ("denied", "auth", "revoked"):
            # These keep the ladder, and for these the ladder is the point: it
            # is how ``REFUSALS_BEFORE_RETIRING`` counts. A credential refused
            # once may be an expired token or a provider incident, and one
            # refused three times running is a credential. Growing the rest is
            # how the pool stops asking while it makes up its mind.
            state["consecutive_rl"] += 1
            # A refusal that named its own wait is obeyed here too.
            #
            # This ladder is described as a counter that retires a credential
            # rather than a guess about time, and the retiring half is true —
            # but it is counted in ``mark`` on ``consecutive_refusals``, not
            # here, so obeying a stated number costs the retirement nothing.
            # What is left is the timing half, and there the same rule applies
            # as everywhere else: a number the provider stated beats a number
            # this module invented.
            if stated and delay > 0:
                return min(delay, self.daily_cooldown_s)
            strikes = state["consecutive_rl"]
            # Saturate before exponentiation. A model denial is not retired,
            # so it can legitimately survive thousands of timed re-probes.
            # Applying min() after an overflowing power never reaches the cap.
            cap = min(self.daily_cooldown_s, HARD_DELAY_CAP_S)
            exponent = max(0, strikes - 1)
            grown = cap if exponent >= math.log2(max(1.0, cap / DAILY_BASE_S)) else (
                DAILY_BASE_S * (2 ** exponent))
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
                # Stated for this very refusal. Nothing outranks it, and R10/
                # R11 close the question of whether repetition may multiply
                # it: it may not. 1.8.1.0 adds one more consequence of the
                # same rule — a number the provider names is evidence the
                # ladder below has none of, so the streak it counts resets
                # here. The floor still applies, because a sub-second rest is
                # a spin rather than a cooldown.
                state["consecutive_unsized_throttle"] = 0
                return max(delay, RL_BASE_S)
            if ceiling is not None and ceiling > 0.0:
                # Stated for an earlier one. The owner's log is why this beats
                # any constant: of 400 Gemini throttles in 46 minutes, 232
                # arrived as the terse "Resource has been exhausted (e.g.
                # check quota)." with no number, and 168 arrived as the same
                # refusal spelled out — never once above 59.8s. The terse form
                # is not a different condition, it is the same condition
                # worded shorter, and the 168 already answered it. Same reset
                # as the branch above and for the same reason: this number is
                # still the provider's, just read off an earlier refusal
                # rather than this one, and the ladder only ever fills the gap
                # left when the provider has said nothing at all, ever.
                state["consecutive_unsized_throttle"] = 0
                return max(min(float(ceiling), RL_BACKOFF_CAP_S), RL_BASE_S)
            # Never stated one, on this refusal or on any earlier one this
            # credential/model has seen. This is deliberately still keyed on
            # the *shape* of the evidence — nothing named a number — and not
            # on which provider sent it. R01 forbids branching on the
            # provider's identity, and the payload that motivated this rung
            # was the owner's own Gemini traffic (2026-09-19: 129 of 172 calls
            # came back bare "RESOURCE_EXHAUSTED", no quotaId, no retryDelay,
            # no header but date) — but any provider that refuses this way,
            # today or in a future release, lands here the same way Gemini
            # does, because the code never asks who sent the 429.
            #
            # 1.8.1.0. Below this comment used to be one line: return the flat
            # rest every time. The owner asked for the alternative he had
            # already named out loud — a doubling ladder, "1 -> 2 -> 4 -> 8 ->
            # etc" — on the belief that a refused call costs no quota, so
            # re-probing faster than a flat 30s is free to try and might
            # recover sooner. That belief has never been measured either way
            # in this project, which is exactly why this ships as an
            # experiment rather than a silent default change: simulated
            # against the owner's own corpus with call suppression, the
            # doubling ladder sends about 242 more refused calls than the
            # flat rest and nets +13 seconds returned against the flat rest's
            # +193 — worse on both counts measured so far. See decisions/0007
            # for the tables. It ships on anyway, because the owner is the
            # owner of this install and asked to watch it react to his own
            # traffic, not to be told the answer in advance.
            #
            # **Narrowed by the owner to one payload.** He wants to test the
            # ladder on this one error and nothing else, so the ladder does
            # not apply to "an unsized throttle" in general — Alibaba's
            # ``Throttling.RateQuota``, Z.AI's "usage limit reached" and a
            # bare 429 with no structured status all reach this line and all
            # keep the flat rest. What separates them is evidence, decided by
            # the caller from the payload and handed in as
            # ``bare_resource_exhausted`` (:func:`is_bare_resource_exhausted`):
            # HTTP 429, a structured status of RESOURCE_EXHAUSTED, no stated
            # number, no quotaId. Never the provider's name — R01.
            if self.unsized_throttle_backoff and bare_resource_exhausted:
                state["consecutive_unsized_throttle"] += 1
                step = state["consecutive_unsized_throttle"]
                # Saturate before exponentiation — the same guard the
                # denied/auth/revoked ladder above uses, for the same reason:
                # a streak long enough to overflow ``2.0 ** (step - 1)``
                # before ``min()`` ever ran would raise instead of clamping.
                # ``max_hold_s`` is the natural cap here, not
                # ``RL_BACKOFF_CAP_S`` — the whole point of this experiment is
                # to let the owner's ceiling, not this module's per-minute
                # cap, be the thing that eventually stops the climb, and
                # ``mark()`` re-clamps to both anyway before anything is
                # stored.
                #
                # And the owner's own ceiling on the ladder, below that one:
                # ``unsized_backoff_max_s`` (default 64). Uncapped, the replay
                # over his corpus produced 138 holds over 300s against 43 for
                # the flat rest and 37,162 contradicted-hold seconds against
                # 999; Decision 12 says a punishment must never exceed the
                # real recovery time, measured at 23-34s at the fastest for
                # this shape. Rung 7 (64s) is the last one that grows.
                cap = max(1.0, min(self.max_hold_s, self.unsized_backoff_max_s))
                exponent = max(0, step - 1)
                grown = cap if exponent >= math.log2(cap) else 2.0 ** exponent
                return min(grown, cap)
            # The dial is off: today's measured behaviour, unchanged. A
            # credential that reaches here with the dial off also has its
            # streak reset, so turning the ladder back on later always starts
            # counting from the first rung rather than resuming mid-climb on
            # a count nothing was rung for while it was off.
            state["consecutive_unsized_throttle"] = 0
            #
            # ``self.unsized_throttle_rest_s`` rather than the module
            # constant: the owner's dial (``settings.UNSIZED_THROTTLE_REST``)
            # is pushed onto this instance by ``dispatch_binding.install()``
            # the same way ``max_hold_s`` is, and reading it fresh here is
            # what makes the dial reach a real hold instead of sitting
            # decorative — see :data:`UNSIZED_THROTTLE_REST_S` for the class
            # default and the measurement behind it.
            return self.unsized_throttle_rest_s

        if kind == "server":
            # The counter is still kept, and it still has a job: it is how
            # ``thaw_server_cooled`` tells a key benched by somebody else's
            # outage from a key benched by its own quota. Thawing the second
            # kind would clear a real cooldown, so the counter stays even
            # though nothing sizes a rest from it any more.
            state["consecutive_server"] += 1

            # A 503 that named its own wait is obeyed, every repeat.
            #
            # Measured before this: a provider asking for seven seconds was
            # rested five, ten, twenty, forty, eighty. A stated number is
            # evidence and everything else here is a guess, so the guess
            # loses. Same rule the throttle branch learned in 1.6.0.3.
            if stated and delay > 0:
                return min(delay, HARD_DELAY_CAP_S)

            # 1.7.0.4. **Flat. A 5xx never escalates.** See ``SERVER_BASE_S``
            # for the owner's reasoning and the seven measured escalations on
            # a healthy pool that ended the ladder. This is the existing
            # local fallback policy, not proof that a failed request is free
            # or unmetered. Provider-specific retry instructions are separate.
            return max(float(delay), SERVER_BASE_S)

        # timeout / other / empty — no escalation ladder, just the flat rest.
        return max(delay, 0.0)

    def thaw_server_cooled(
        self, identity: str, except_key: str, now: Optional[float] = None
    ) -> int:
        """Pull 5xx-rested keys forward after the outage ends. Returns how many.

        Only inferred server holds can be shortened. A peer answering is useful
        evidence for retrying an outage, not proof of another account's capacity.
        Active quota/auth holds and explicit server retry instructions retain
        their own deadlines. Own success or expiry clears those constraints.

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
                target = max(target, state.get("non_server_until", 0.0), state.get("stated_server_until", 0.0))
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
            provider = self._provider_of(identity)
            # Match select(): a retired credential cannot shorten the ETA
            # while another credential remains eligible. Preserve the
            # all-retired fallback, including a single credential pool.
            standing = [k for k in usable if not pool.get(k, {}).get("retired")]
            if standing:
                usable = standing
            # Same reasoning as select()'s ``_until``: an account hold earned
            # on a sibling model still governs when THIS identity is the one
            # asking how soon it recovers, and 1.8.0.0 folds in whatever a
            # sibling Hermes profile last wrote — see ``_combined_until``.
            shared_active = self._shared.active()
            deadlines = [
                self._combined_until(pool, identity, provider, k, now, shared_active)
                for k in usable
            ]
        soonest = min(deadlines) if deadlines else 0.0
        return None if soonest <= now else soonest - now

    def healthy_count(
        self, identity: str, keys: Sequence[str], now: Optional[float] = None
    ) -> int:
        """Ready candidates under select's retirement/fallback policy."""
        now = time.time() if now is None else now
        with self._lock:
            pool = self._pools.get(identity) or {}
            provider = self._provider_of(identity)
            usable = [k for k in keys if k]
            standing = [k for k in usable if not pool.get(k, {}).get("retired")]
            if standing:
                usable = standing
            # 1.8.0.0: same reconciliation as select() and next_recovery_seconds().
            shared_active = self._shared.active()
            return sum(
                1 for k in usable
                if self._combined_until(pool, identity, provider, k, now, shared_active) <= now
            )

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """A copy of the whole bench, for ``/kame-quota``. Keys are never included."""
        now = time.time() if now is None else now
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for identity, pool in self._pools.items():
                healthy = sum(1 for s in pool.values() if s["sick_until"] <= now)
                # The soonest deadline still ahead of us, so a reader can be
                # told when the pool comes back rather than only that it is
                # away. ``None`` when something is usable now, which is the
                # same convention ``next_recovery_seconds`` uses.
                resting_for = [
                    s["sick_until"] - now
                    for s in pool.values()
                    if s["sick_until"] > now
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

    def unsized_backoff_step(self, identity: str, key: str) -> int:
        """The doubling ladder's current rung for this credential+model, or 0.

        0 means the most recent rest on this pair — if there was one — did
        not come from the ladder in :meth:`_escalate`: either nothing has
        failed here yet, the last failure was sized some other way (a stated
        delay, a ceiling), the ladder is switched off, or the credential last
        answered (which resets the streak to 0, same as never having failed).

        1.8.1.0. Read after :meth:`mark`, not during it, so a caller that logs
        or journals the outcome — ``dispatch_binding`` — can say *which rung
        of the ladder produced this rest* without ``_escalate`` having to
        return anything richer than the number it always has.
        """
        if not key:
            return 0
        with self._lock:
            pool = self._pools.get(identity) or {}
            state = pool.get(key)
            return int(state.get("consecutive_unsized_throttle", 0)) if state else 0

    def unsized_backoff_label(self, identity: str, key: str) -> str:
        """``"backoff.<rung>"`` if the last rest came from the ladder, else ``""``.

        The dot, not a colon: :func:`format_duration`'s callers write this
        straight into the ``sized_by`` family the desktop panel already
        splits on ``"."`` (``verdict.source`` is the existing example), so a
        new rung needs no new parsing on that side — only a new label, added
        beside the others in ``desktop/plugin.js``.
        """
        step = self.unsized_backoff_step(identity, key)
        return f"backoff.{step}" if step > 0 else ""

    def forget(self, identity: Optional[str] = None) -> None:
        """Drop the bench. For tests, and for a pool that was replaced wholesale.

        1.7.0.2 clears the daily doubt with it. ``/kame clear_pool`` promises
        "every key starts again as if it had never been tried", and a run of
        daily labels the pool accumulated before the reset is exactly the kind
        of thing that promise is about — the owner reset five times in one
        session on 06/09 precisely to escape one.
        """
        with self._lock:
            if identity is None:
                self._pools.clear()
                self._no_answer_since.clear()
                self._named_window.clear()
                self._stated_rl_ceiling.clear()
                self._account_hold.clear()
                self._account_hold_at.clear()
            else:
                self._pools.pop(identity, None)
                self._no_answer_since.pop(identity, None)
                for memory in (self._named_window, self._stated_rl_ceiling):
                    for subject in list(memory):
                        if subject[0] == identity:
                            memory.pop(subject, None)
                # Deliberately NOT cleared here (nor is ``_account_hold_at``,
                # which travels with it). ``_account_hold`` is keyed by
                # ``(provider, key)``, not ``identity`` — it is a fact about
                # the credential's account, established by a refusal that may
                # have happened on a *different* model of this same provider.
                # Resetting one model's pool is not evidence the account
                # recovered, so a scoped ``forget`` leaves it standing; only
                # the global reset above (or this credential's own next
                # success, on any model) clears it.

    def reset_all(self) -> Optional[int]:
        """The *Clear pool* button: memory AND the shared file.

        :meth:`forget` alone is what tests want — it never touches disk. The
        button needs more: with ``share_pool_health`` on (the default) the
        shared file still held every bench, and being fresher than the
        emptied memory it won the very next ``select``, so a cleared pool came
        back exactly as benched. Returns what :meth:`shared_health.
        SharedHealth.release_all` returned.
        """
        self.forget()
        try:
            return self._shared.release_all()
        except Exception:
            return None


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
    "CREDENTIAL_PROBLEM_REST_S",
    "REJECTED_KINDS",
    "RETIRING_KINDS",
    "REFUSALS_BEFORE_RETIRING",
    "DENIED_REST_S",
    "INVALID_KEY_INDICATORS",
    "CREDENTIAL_PROBLEM_INDICATORS",
    "classify",
    "extract_delay",
    "fingerprint",
    "format_duration",
    "is_auth_failure",
    "is_terminal",
    "parse_duration",
]
