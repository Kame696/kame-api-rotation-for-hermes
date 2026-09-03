"""The last fifty things that went wrong, in the order they happened.

Every fact in here is already in the log. That is not an argument against it:
the log is a file on a machine the user may not have open, mixed with every
other subsystem's lines, and reading it means knowing it exists, where it is,
and which of the six hundred lines belong to a key pool. "Why did my answer
stall for a minute" deserves an answer on screen.

So this is a ring buffer of *decisions*, not a log copy. Each entry says what
happened, to which key, why, and when — and nothing else. In particular:

* **never key material.** The ``key`` field carries a fingerprint from
  :func:`core.carousel.fingerprint`, which is a hash prefix and not a prefix of
  the key. This buffer is written into a JSON file a UI reads; the rule that
  file follows is the rule this follows.
* **never provider text.** A provider's error body can quote the request, and
  the request can be the user's private prompt. ``reason`` is one of this
  module's own short phrases, and ``code`` is a status number.

Bounded on purpose, and small. It exists to answer "what just happened", which
is a question about the last few minutes; an unbounded history would be a
second log with none of a log's rotation, and would grow the snapshot file
this ships inside without bound.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Deque, Dict, List, Optional

from .redact import redact
from collections import deque

#: How many entries are kept.
#:
#: Fifty until 1.6.0.1, chosen when the buffer held failures only. It now
#: holds the other half of each story — which key took over, and whether that
#: key answered — so the same outage produces roughly three rows where it
#: produced one, and fifty would have shown a third of the incident it was
#: sized to explain. A hundred and fifty rows is about 25 KB inside a snapshot
#: file the panel already re-reads every two seconds.
MAX_EVENTS = 150

#: The kinds. Named for what the user sees, not for the code path: "quarantine"
#: rather than "mark(ok=False, kind=auth)", because the first is what the pool
#: looks like from outside.
ROTATION = "rotation"
#: 1.6.0.1. The rotation itself, rather than the refusal that caused it: this
#: key is the one that just took over. Until this release the Events tab was a
#: list of things that went wrong with no record of what KAME did about them,
#: which is the half the owner actually asked to see: every rotation, not
#: only the errors. A ``switch`` is always preceded by the event that caused it
#: and usually followed by the ``recovery`` that ends it.
SWITCH = "switch"
QUARANTINE = "quarantine"
INVALID_KEY = "invalid_key"
#: 1.6.0.1. A 403 that refused this key for *this model* — a suspended
#: project, an API never enabled, a model outside the tier the key pays for.
#: Kept apart from :data:`INVALID_KEY` because the two ask the reader for
#: opposite things: one says replace the key, and this one says the key is
#: fine and it is the pairing that is not. They were the same row until this
#: release, so a model nobody was entitled to reported a healthy credential
#: as dead.
DENIED_MODEL = "denied_model"
STORM = "storm"
STREAM_DROP = "stream_drop"
STITCH = "stitch"
WAIT = "wait"
RECOVERY = "recovery"
SURFACED = "surfaced"

_KINDS = frozenset(
    {ROTATION, SWITCH, QUARANTINE, INVALID_KEY, DENIED_MODEL, STORM,
     STREAM_DROP, STITCH, WAIT, RECOVERY, SURFACED}
)

#: The kinds that are KAME working rather than a provider failing. The panel
#: colours and filters on this split, and the distinction is the point of the
#: 1.6.0.1 Events tab: a screen that shows only refusals reads like a fault
#: report, and a rotation engine that is doing its job is not a fault.
GOOD_KINDS = frozenset({SWITCH, RECOVERY, STITCH, WAIT})


class Events:
    """A fixed-size, thread-safe record of what the carousel decided."""

    def __init__(self, limit: int = MAX_EVENTS) -> None:
        self._lock = threading.Lock()
        self._items: Deque[Dict[str, Any]] = deque(maxlen=max(1, int(limit)))
        #: Monotonic within a process, so a reader can tell "nothing new" from
        #: "the same thing again" without comparing whole rows.
        self._seq = 0

    def add(
        self,
        kind: str,
        *,
        identity: str = "",
        key: str = "",
        reason: str = "",
        code: Optional[int] = None,
        seconds: Optional[float] = None,
        at: Optional[float] = None,
        detail: str = "",
        sized_by: str = "",
    ) -> Dict[str, Any]:
        """Record one event. Never raises — a readout must not end a turn.

        ``detail`` is the provider's own payload, and it is **redacted here**
        rather than trusted. 1.1.1 kept no provider text at all, because a
        provider can quote the request back inside an error and the request can
        be the user's prompt; 1.2.9 put it back under a click without meeting
        that rule. Neither is right on its own — the rule protects something
        real, and a person watching fourteen keys rotate needs to see *why*.
        Scrubbing on the way in gives both: the secret is not in this file, so
        it is not in a screenshot of the panel or in a support bundle either,
        and the evidence is still there to read.

        ``sized_by`` is where the cooldown came from — ``catalog``, ``header``,
        ``retryinfo``, ``pattern``, ``table``, ``dropped``. It is stored
        because the single most useful number in nine days of telemetry was the
        proportion of decisions that were guesses (**67 %**), and nobody could
        see it. Now it is one column in the Events tab.
        """
        try:
            row = {
                "seq": 0,
                "at": float(time.time() if at is None else at),
                "kind": str(kind if kind in _KINDS else "rotation"),
                "identity": str(identity or ""),
                # Already a fingerprint when it reaches here. Truncated rather
                # than trusted: a caller that passes a raw key by mistake gets
                # a useless string in the UI, not a leaked credential.
                "key": str(key or "")[:16],
                "reason": str(reason or "")[:120],
                "code": int(code) if isinstance(code, int) else None,
                "seconds": round(float(seconds), 1) if seconds is not None else None,
                "detail": redact(detail) if detail else "",
                "sized_by": str(sized_by or "")[:24],
            }
        except Exception:
            return {}
        with self._lock:
            self._seq += 1
            row["seq"] = self._seq
            self._items.append(row)
            return dict(row)

    def recent(self, limit: int = MAX_EVENTS) -> List[Dict[str, Any]]:
        """Newest first, because that is the end anybody reads."""
        with self._lock:
            rows = list(self._items)
        rows.reverse()
        return rows[: max(0, int(limit))]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    @property
    def total(self) -> int:
        """How many events have ever been recorded, including dropped ones."""
        with self._lock:
            return self._seq


#: The one the plugin uses. A module-level instance for the same reason the
#: carousel has one: a pool shared by every conversation in the process has a
#: history shared by every conversation in the process.
EVENTS = Events()
