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
from collections import deque

#: How many entries are kept. Fifty is roughly one screen of a scrolling list
#: and a couple of minutes of a bad outage — long enough to explain a stall,
#: short enough that the whole thing can be read.
MAX_EVENTS = 50

#: The kinds. Named for what the user sees, not for the code path: "quarantine"
#: rather than "mark(ok=False, kind=auth)", because the first is what the pool
#: looks like from outside.
ROTATION = "rotation"
QUARANTINE = "quarantine"
INVALID_KEY = "invalid_key"
STORM = "storm"
STREAM_DROP = "stream_drop"
STITCH = "stitch"
WAIT = "wait"
RECOVERY = "recovery"
SURFACED = "surfaced"

_KINDS = frozenset(
    {ROTATION, QUARANTINE, INVALID_KEY, STORM, STREAM_DROP, STITCH, WAIT, RECOVERY, SURFACED}
)


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
        raw_error: str = "",
    ) -> Dict[str, Any]:
        """Record one event. Never raises - a readout must not end a turn."""
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
                "raw_error": str(raw_error or ""),
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
