"""Widening a deadline that has already been measured too short.

Every other number this plugin produces is *read* — off an exception, a
header, a body, a sentence. This is the only one that is *learned*, and it is
learned from the single thing the journal can prove without ambiguity: the
key was handed back the moment its cooldown lapsed, the very next call was
refused the same way, and that happened twice in a row. There is no reading
of that sequence in which the deadline was long enough.

The cost of being wrong here is the expensive direction — a healthy key sat
out — so the rule is deliberately reluctant:

* **Two strikes, back to back.** One repeat is a coincidence: a key retried
  at an unlucky moment, a burst that had not drained. Two consecutive is a
  pattern, and consecutiveness is what makes it self-clearing — a single
  refusal that lands anywhere else in time breaks the run and the widening
  stops on its own, with no timer to tune.
* **Same key, same model, same window.** A per-minute throttle proving short
  says nothing about a daily cap on the same model, and a free key's limits
  say nothing about a paid key's. Evidence is charged to the thing it was
  measured on.
* **Bounded, twice.** The multiplier stops at 8× and the resulting hold stops
  at a day, so the worst case of a bad chain of evidence is a key benched for
  a day — which is exactly what the host's own account-level default already
  does — rather than a key benched into next week.
* **Never for a depletion.** Out of credits does not become in-credit by
  waiting longer, so widening spends a healthy key's time on nothing. The same
  list of reasons that stops a probe stops a stretch, for the same reason.

The safety net is the one v0.0.9 built. A widened bench is still a
prediction, it is still tested by the escape hatch when a model has nothing
else usable, and a single clean call refutes it for good — so an over-eager
stretch cannot lock anybody out, it can only cost a probe. Escalation was
deliberately not shipped before refutation existed, in that order, because
the reverse order is how a plugin that sizes cooldowns becomes a plugin that
hides keys.

Framework-free like the rest of ``core``: numbers in, a number out.
"""

from __future__ import annotations

from typing import Dict, Optional

from .probe import NEVER_PROBE_REASONS
from .quota import SOURCE_ANCHOR, QuotaWindow

# How many consecutive proven-short deadlines before the next one is widened.
# Two, matching ``WindowStat.looks_short`` — the report has been calling this
# the threshold since v0.0.6, and the number that acts should be the number
# the user was shown.
STRIKES_BEFORE_STRETCHING = 2

# What the multiplier stops at. Doubling per strike reaches it on the fourth,
# which is roughly where a widening that has not yet worked is not going to
# start working: something other than the window length is wrong.
MAX_FACTOR = 8.0

# Per-window escalation ceilings. Per-minute and per-hour caps match Agent
# Zero's backoff caps (_KAME_RL_BACKOFF_CAP_S = 300s) so a stuck RPM key
# cannot escalate to a day-long bench. Daily/weekly/monthly keep the 24h
# ceiling because those windows genuinely take hours to recover.
MAX_HOLD_SECONDS = 24 * 60 * 60.0          # daily / weekly / monthly
MAX_PER_MINUTE_HOLD_SECONDS = 300.0         # matches AZ _KAME_RL_BACKOFF_CAP_S
MAX_PER_HOUR_HOLD_SECONDS   = 3600.0        # one full hour max on per-hour escalation

# Windows a longer wait cannot fix. ``account`` is out of credits, not a
# clock — the host already benches it for a day and only a human topping up
# changes it.
NEVER_STRETCH_WINDOWS = frozenset({QuotaWindow.ACCOUNT})

# A deadline that is a calendar instant rather than a stopwatch reading.
# A stopwatch deadline scales multiplicatively; a calendar anchor (a provider
# that says "your counter rolls at HH:MM") is moved additively instead,
# because multiplying a calendar instant produces nonsense — 2× midnight is
# not a meaningful time.
ANCHOR_SOURCES = frozenset({SOURCE_ANCHOR})

# Per-window caps looked up by window string in stretch().
#
# ``unknown`` is deliberately absent, and 1.6.0.3 tried adding it before
# putting it back. The reasoning that argued for it — every Gemini throttle in
# the owner's log classified as ``rate_limit [unknown]``, because Google names
# the window in ``quotaId`` and Hermes' adapter does not carry that field — is
# about the *carousel's* ladder, which fired on repetition and is where the
# five-minute benches came from. This function is the other mechanism, and it
# is the disciplined one: it widens only after the journal has recorded two
# deadlines that were waited out in full and refused anyway. A bench that has
# been measured short twice is short whatever the window is called, and a
# 12-hour deadline refuted four times has earned the day-long ceiling that
# ``test_a_widened_bench_never_outlasts_a_day`` asks for. Capping the unnamed
# window at five minutes would have taken the ceiling away from measurement to
# punish a guess made somewhere else.
_WINDOW_ESCALATION_CAPS: Dict[str, float] = {
    QuotaWindow.PER_MINUTE: MAX_PER_MINUTE_HOLD_SECONDS,
    QuotaWindow.PER_HOUR:   MAX_PER_HOUR_HOLD_SECONDS,
}

# How much later to move an anchor that has been measured early, per strike.
# Fifteen minutes doubling to two hours: wide enough to cover the ways a
# rollover instant is wrong in practice — a zone offset, a rounding, a
# provider that sweeps its counters a little after the hour — and far too
# narrow to be a way of hiding a key.
NUDGE_SECONDS = 15 * 60.0
MAX_NUDGE_SECONDS = 2 * 60 * 60.0


def factor_for(strikes: int) -> float:
    """How much longer to hold, after ``strikes`` consecutive short deadlines.

    2× on the second strike, 4× on the third, 8× from the fourth. Below the
    threshold the answer is 1× — no change at all, which is what "not enough
    evidence" has to mean.
    """
    if strikes < STRIKES_BEFORE_STRETCHING:
        return 1.0
    return min(MAX_FACTOR, 2.0 ** (strikes - 1))


def nudge_for(strikes: int) -> float:
    """How much later to move an anchor, after ``strikes`` short readings.

    Thirty minutes on the second strike, an hour on the third, two from the
    fourth. Additive, because an anchor that is wrong is wrong by an offset —
    a daily counter that rolls five minutes after midnight is not a counter
    that rolls two days later, and scaling the wait would say it was.
    """
    if strikes < STRIKES_BEFORE_STRETCHING:
        return 0.0
    return min(MAX_NUDGE_SECONDS, NUDGE_SECONDS * factor_for(strikes))


def _reason_forbids(reason: str) -> bool:
    text = (reason or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in NEVER_PROBE_REASONS)


def stretch(
    *,
    reset_at: float,
    now: float,
    strikes: int,
    window: str = QuotaWindow.UNKNOWN,
    reason: str = "rate_limit",
    source: str = "",
) -> Optional[float]:
    """The deadline KAME should hold this key to, or ``None`` to accept the host's.

    ``None`` is the answer almost always, and it is the answer that changes
    nothing: the bench is recorded with the host's own number and the plugin
    behaves exactly as v0.0.9 did. Only measured, repeated, same-window
    evidence produces anything else.

    Two corrections, because there are two kinds of deadline. A *stopwatch* —
    "come back in 21 seconds", "re-probe in an hour" — is a length, and a
    length that proves short is scaled. An *anchor* — midnight US/Pacific, the
    instant a daily counter is believed to roll — is a moment, and a moment
    that proves early is moved, by minutes, not multiplied. Scaling an anchor
    is not merely clumsy: on the daily cap it is a no-op, because the next
    anchor is already a day away and the ceiling eats the whole multiplier.
    The deadline in this plugin that most needs correcting was the one
    escalation could not touch.
    """
    if strikes < STRIKES_BEFORE_STRETCHING:
        return None
    if str(window or "").strip().lower() in NEVER_STRETCH_WINDOWS:
        return None
    if _reason_forbids(reason):
        return None
    try:
        seconds = float(reset_at) - float(now)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds <= 0.0:
        # NaN, or a deadline already in the past. Neither is a bench to widen.
        return None

    if str(source or "").strip().lower() in ANCHOR_SOURCES:
        # The day itself came from the provider's own rollover; only the
        # offset is KAME's. So the ceiling applies to the offset, not to the
        # total — capping the sum here would silently discard the correction
        # for exactly the deadline that needed it.
        return float(reset_at) + nudge_for(strikes)

    cap = _WINDOW_ESCALATION_CAPS.get(str(window or "").strip().lower(), MAX_HOLD_SECONDS)
    widened = min(seconds * factor_for(strikes), cap)
    if widened <= seconds:
        # The ceiling already covered this bench. Saying so as ``None`` keeps
        # the row honest: nothing was added, so nothing claims to have been.
        return None
    return float(now) + widened
