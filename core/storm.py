"""Collapsing a storm of identical failures into something a person can read.

Every rotation writes a line. That is right, and it is what makes a pool that
is working visible instead of a claim — you watch the key change, you see what
each refusal cost. It stops being right during an outage, when the same
sentence repeats with nothing new in it. Agent Zero measured its own version of
this: one sustained Gemini outage produced **1,063 near-identical failure lines
in 83 minutes**, and shipped `kame_collapse_storm_logs` because of it.

Hermes had the same shape and did not have the fix, and **1.0.1 made it worse
rather than better**. Before it, a call gave up after 600 seconds, so a storm
was bounded by the ceiling whether anybody liked it or not. With the ceiling
gone the carousel rotates for as long as the provider keeps refusing — which is
the entire point of the release, and which means an outage now writes lines for
hours instead of ten minutes. Removing a limit moved the cost somewhere else,
and this is where it landed.

The rule, in one sentence: **say what is happening, then say how much of it,
and say when it stopped.**

What is deliberately *not* collapsed:

* **The first few of a shape.** One line tells you what is failing; three let
  you watch the pool actually walk from key to key, which is the thing somebody
  installs this plugin to see. Collapsing from the second would hide the
  feature working in order to hide it failing.
* **A change of shape.** A storm of 503s that becomes a storm of 429s is not
  the same storm. The provider changed its answer, and that is news.
* **An auth failure.** It is rare, permanent, and actionable — no amount of
  rotation repairs a revoked key. It is logged separately and at a higher
  level, and this module never sees it.
* **A waiting notice.** ``_Vigil`` speaks to the user, on its own schedule,
  about a period when nothing is failing because nothing is being sent. A
  storm is the opposite state: calls going out and being refused.

Nothing here decides anything about a key. It decides what gets written down,
and a filter that got its own answer wrong would cost a reader some clarity and
cost the pool nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Set, Tuple

from .carousel import format_duration

#: How many failures of one shape are printed in full before the collapse
#: starts. See the module docstring: one is enough to know *what*, three is
#: enough to watch the rotation happen.
LOUD_N = 3

#: How often a collapsed storm still says something. Agent Zero settled on
#: twenty seconds after the outage above, and the number is kept because the
#: thing being paced is the same thing — a person scrolling a log — rather
#: than anything about either host.
AGGREGATE_EVERY_S = 20.0


@dataclass
class Verdict:
    """What the caller should write, if anything.

    Both fields can be set at once, and that is not an edge case: the failure
    that *changes* the shape of a storm both closes the old one (``summary``)
    and opens the new one (``speak_full``). Returning one and dropping the
    other would lose whichever the caller checked second.
    """

    #: Write the ordinary, complete per-failure line.
    speak_full: bool = False
    #: Write this instead — an aggregate of what was held back. Never carries
    #: a key: counts, a shape, and a duration.
    summary: Optional[str] = None


@dataclass
class Storm:
    """One run of failures that share a shape, and what has been said about it."""

    kind: str
    status: Optional[int]
    started: float
    #: Every failure of this shape, including the ones printed in full.
    seen: int = 0
    #: Failures held back since the last thing that was written.
    withheld: int = 0
    #: When something was last written about this storm.
    spoke_at: float = 0.0
    #: Distinct keys involved, held as fingerprints so the count can be
    #: reported without the set ever containing a secret.
    keys: Set[str] = field(default_factory=set)

    @property
    def shape(self) -> Tuple[str, Optional[int]]:
        return (self.kind, self.status)

    def label(self) -> str:
        return f"{self.kind}{f' [{self.status}]' if self.status else ''}"


class StormFilter:
    """Decides whether a failure line is news or repetition.

    One of these lives on the dispatch binding rather than on a call, because
    an outage does not respect turn boundaries: the same provider is still down
    on the next message, and a filter that reset per call would print the loud
    first lines again every turn and never reach the collapse.
    """

    __slots__ = ("_loud_n", "_every_s", "_storm")

    def __init__(
        self,
        *,
        loud_n: int = LOUD_N,
        every_s: float = AGGREGATE_EVERY_S,
    ) -> None:
        self._loud_n = loud_n
        self._every_s = every_s
        self._storm: Optional[Storm] = None

    # -- reading ---------------------------------------------------------

    @property
    def storming(self) -> bool:
        """Whether anything is currently being held back.

        A storm that has not yet passed ``loud_n`` is not storming: every one
        of its failures has been printed in full, so there is nothing to
        summarise and nothing to close.
        """
        return self._storm is not None and self._storm.seen > self._loud_n

    # -- the decision ----------------------------------------------------

    def observe(
        self,
        kind: str,
        status: Optional[int],
        fingerprint: str,
        now: float,
    ) -> Verdict:
        """Take one failure and answer what should be written about it."""
        storm = self._storm

        if storm is None or storm.shape != (kind, status):
            # A different failure, so whatever was accumulating is over and
            # has to be closed before the new one opens — otherwise a storm
            # that ends by *changing* rather than by succeeding leaves its
            # last withheld lines unaccounted for, which is exactly the
            # arithmetic somebody reading the log would try to do by hand.
            closing = self._close(now)
            self._storm = Storm(
                kind=kind, status=status, started=now, seen=1, spoke_at=now
            )
            self._storm.keys.add(fingerprint)
            return Verdict(speak_full=True, summary=closing)

        storm.seen += 1
        storm.keys.add(fingerprint)

        if storm.seen <= self._loud_n:
            storm.spoke_at = now
            return Verdict(speak_full=True)

        storm.withheld += 1
        if now - storm.spoke_at < self._every_s:
            return Verdict()

        summary = (
            f"{storm.label()} ×{storm.withheld} more in the last "
            f"{format_duration(now - storm.spoke_at)} — "
            f"{len(storm.keys)} key(s) involved, still rotating"
        )
        storm.withheld = 0
        storm.spoke_at = now
        return Verdict(summary=summary)

    def ended(self, now: float) -> Optional[str]:
        """Close the current storm, if one was actually collapsing anything.

        Called when a key answers. Returns the recap line, or ``None`` when
        there was no storm or when every failure in it was already printed —
        a recap of three lines the reader can still see is noise.
        """
        return self._close(now)

    # -- internals -------------------------------------------------------

    def _close(self, now: float) -> Optional[str]:
        storm = self._storm
        self._storm = None
        if storm is None or storm.seen <= self._loud_n:
            return None
        return (
            f"{storm.label()} storm over — {storm.seen} failure(s) over "
            f"{format_duration(now - storm.started)} across "
            f"{len(storm.keys)} key(s)"
        )
