"""When to test a prediction instead of trusting it.

Everything the ledger holds is a *guess about the future* — a deadline read
out of a provider's error and believed. Believing it is right almost always,
and the one case where it is catastrophic is precise: when the model in
flight has **no other usable credential**.

With ten keys, a deadline that was too long costs one key for a while and the
pool routes around it. With one key it costs the agent entirely: a daily cap
read as "midnight Pacific" locks the user out for a day, and if that reading
was wrong there is nothing in the system that could ever discover it — the
key is never tried, so no success is ever observed, so the mistake is
invisible by construction. That is the failure this module exists to prevent,
and it is worth being blunt about the asymmetry:

* trusting a correct long deadline saves a handful of doomed calls;
* trusting an incorrect long deadline costs every turn until it lapses.

Hermes reasons the same way and reaches the same answer with
``EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS``: when the offending key is the only
one, a one-hour bench "means an hour of hard failures with nothing to fall
back to", so a transient throttle cools down for a minute instead. The
comment above that constant also says *"Provider-supplied reset_at still
overrides"* — which is exactly what KAME supplies. So the more accurate KAME
gets, the more completely it disables the host's own escape hatch. This
module puts one back, scoped per model, which is the dimension the host
cannot see.

The shape of the answer, deliberately conservative:

* Only when nothing else is usable **for the model in flight**. A probe is
  never a shortcut past a healthy key.
* Only for benches KAME wrote. Somebody else's bench is somebody else's
  business, and the binding proves ownership before asking anything here.
* Only for benches long enough that waiting is the expensive option, and only
  while enough time remains that the probe is answering a real question
  rather than jumping a queue about to clear anyway.
* Never for a depletion a retry cannot fix. Out of credits is not a timing
  problem, and Hermes keeps the full bench for exactly that reason.
* Spaced by a widening backoff, so a correct long deadline costs a bounded
  handful of failed calls rather than one per turn.

Nothing here decides anything on its own — it reads benches and returns which
one, if any, deserves a try. The binding layer applies the host's own
usability gates on top before a key goes anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .ledger import Bench

# How long after a bench is written the first probe becomes due. Long enough
# that an ordinary per-minute throttle has lapsed on its own and no probe is
# ever needed; short enough that a badly-read daily cap costs five minutes of
# lockout instead of a day.
FIRST_PROBE_SECONDS = 300.0

# The backoff doubles from there and stops here. Half an hour is the point
# where a wrong prediction is already cheap to discover and a right one is not
# worth re-asking more often.
MAX_PROBE_INTERVAL_SECONDS = 1800.0

# Once a probe is issued the key stays offered for this long. ``_available_entries``
# is asked several times per turn and the answers have to agree with each
# other: a key offered on the first call and withheld on the third would make
# the selection depend on call order. It also bounds the cost — one failing
# call per probe, not one per query.
PROBE_WINDOW_SECONDS = 60.0

# Below this, waiting is cheaper than asking. A 21-second throttle needs no
# probe, and a bench about to lapse answers the question by lapsing.
MIN_BENCH_SECONDS = 600.0

# A probe must land far enough before the deadline that the refusal it may
# provoke cannot be mistaken for "the cooldown was too short" by the journal,
# whose under-prediction grace is 180s. Keeping this comfortably above that
# means probing can never poison the statistic it exists to feed.
MIN_REMAINING_SECONDS = 300.0

# A stop, not a schedule. At the maximum interval this is more than a day of
# probing; reaching it means something is wrong with the deadline itself, and
# continuing to hammer would not fix it.
MAX_PROBES = 64

# Reasons a retry cannot help. These are depletions and faults, not clocks:
# no amount of waiting-and-trying converts an empty balance or a rejected
# credential into a working one, so probing spends real calls for nothing.
# Matched as substrings because the host stores whatever the classifier
# produced, and that text varies.
NEVER_PROBE_REASONS = (
    "billing",
    "insufficient",
    "credit",
    "payment",
    "auth",
    "invalid",
    "revoked",
    "permission",
    "suspend",
)


@dataclass(frozen=True)
class Probe:
    """One bench worth testing, and whether this is a new attempt.

    ``fresh`` is the difference between *issuing* a probe and *continuing* one
    already in flight. A fresh probe has to be written down before the key is
    handed over, or a burst of availability queries inside one turn would each
    count as an attempt and burn the whole backoff in a second. A continuing
    one must not be written down again, for the same reason in reverse.
    """

    bench: Bench
    fresh: bool

    @property
    def credential_id(self) -> str:
        return self.bench.credential_id

    @property
    def model(self) -> str:
        return self.bench.model


def interval_for(probes: int) -> float:
    """Seconds to wait before attempt ``probes + 1``.

    Doubling from five minutes: 5m, 10m, 20m, then 30m forever. A wrong
    deadline is found in the first few attempts or its window genuinely has
    not opened yet, and past that the cost of asking again should stop
    growing with it.
    """
    if probes < 0:
        probes = 0
    interval = FIRST_PROBE_SECONDS * (2.0**probes)
    return min(interval, MAX_PROBE_INTERVAL_SECONDS)


def _reason_forbids(bench: Bench) -> bool:
    reason = (bench.reason or "").strip().lower()
    if not reason:
        return False
    return any(marker in reason for marker in NEVER_PROBE_REASONS)


def eligible(bench: Bench, *, now: float) -> bool:
    """Whether this bench is the kind of claim a probe can usefully test."""
    if not bench.is_live(now):
        return False
    if bench.probes >= MAX_PROBES:
        return False
    if _reason_forbids(bench):
        return False
    # ``until``, not ``reset_at``: what makes waiting expensive is how long the
    # key is actually being withheld, and from v0.1.0 that can be KAME's own
    # widened deadline rather than the one the host stored. A stretch is a
    # prediction like any other and has to be as testable as the rest.
    sentence = bench.until - (bench.recorded_at or now)
    if sentence < MIN_BENCH_SECONDS:
        return False
    if bench.until - now < MIN_REMAINING_SECONDS:
        return False
    return True


def window_is_open(bench: Bench, *, now: float) -> bool:
    """Whether a probe already issued is still the answer for this moment."""
    last = bench.last_probe_at or 0.0
    if last <= 0.0:
        return False
    return 0.0 <= now - last < PROBE_WINDOW_SECONDS


def next_probe_at(bench: Bench) -> float:
    """When this bench next becomes worth testing.

    Counted from the last attempt if there was one, otherwise from the moment
    the bench was written — so a long sentence is first questioned five
    minutes in, not five minutes after somebody happens to look.
    """
    started = bench.last_probe_at or bench.recorded_at or 0.0
    return started + interval_for(bench.probes)


def is_due(bench: Bench, *, now: float) -> bool:
    return eligible(bench, now=now) and now >= next_probe_at(bench)


def _rank(bench: Bench) -> tuple:
    # Soonest deadline first: the bench closest to lapsing is the one most
    # likely to be wrong by now, and the cheapest to be wrong about. The id
    # breaks ties so repeated queries in one turn agree with each other.
    return (bench.until, bench.credential_id)


def choose(benches: Iterable[Bench], *, now: float) -> Optional[Probe]:
    """Pick the one bench to test right now, or ``None`` to keep waiting.

    Callers pass only benches they have already proved are theirs, on the
    model in flight, backed by a credential the host would otherwise be
    willing to use. This decides *whether* and *which*, never *may I*.
    """
    rows: Sequence[Bench] = [bench for bench in benches if eligible(bench, now=now)]
    if not rows:
        return None

    open_windows = [bench for bench in rows if window_is_open(bench, now=now)]
    if open_windows:
        # Continuity wins over freshness: a probe already in flight is the
        # answer until its window closes, even if another bench came due
        # meanwhile. Two probes at once would spend two calls to answer one
        # question.
        return Probe(bench=min(open_windows, key=_rank), fresh=False)

    due = [bench for bench in rows if now >= next_probe_at(bench)]
    if not due:
        return None
    return Probe(bench=min(due, key=_rank), fresh=True)
