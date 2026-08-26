"""How many failures reached KAME, and how many of them it could size.

Declining is this plugin's common path and its safe one (decision 39): the
host has a competent classifier and overriding it with a guess is strictly
worse than staying quiet. But that makes two very different installs look
identical from the outside — one where KAME reads every refusal the provider
sends, and one where the provider changed a payload shape six weeks ago and
KAME has been inert ever since. The journal cannot tell them apart either: it
only records failures that reached a *bench*, so an error the host classified
as something other than a rate limit leaves no trace at all.

That is the exact failure this project has already had twice — a phrase
written from memory left four versions green and inert against real traffic
(decision 26), and a word too wide was found only by the host's own corpus
(decision 38). Both were found by going and looking. This is the counter that
makes looking unnecessary.

**Holds no text, ever.** The hook payload it is fed from carries
``error_message`` and ``error_body``, which the contract warns may be an
unredacted provider dump. What arrives here is a provider name, an HTTP
status number and two integers. There is nothing in this module that could
print a key, a prompt or a URL, and that is a property of what it stores
rather than of how it is called.

Memory-only and bounded, like ``dispersion``: a count of what this process
has seen since it started, which is the question being asked, and no file to
grow without limit.
"""

from __future__ import annotations

import threading
from typing import Dict, List, NamedTuple, Optional

# One row per provider and status. An agent that walks many providers, each
# failing in several ways, would otherwise accumulate rows for the life of
# the process. At the ceiling the whole tally is dropped rather than evicting
# one row: a partial count read as a total is a wrong answer, and starting the
# count over is an honest one.
MAX_ROWS = 128

# Sizing a wait is the entire job on this status. A row of these with nothing
# sized is the signature of an inert plugin, and it is the only line in the
# report that deserves to be pointed at.
SIZING_MATTERS = frozenset({429})


class Seen(NamedTuple):
    """One provider and status: how many arrived, how many KAME sized."""

    provider: str
    status_code: Optional[int]
    total: int
    sized: int

    @property
    def declined(self) -> int:
        return self.total - self.sized

    @property
    def worth_pointing_at(self) -> bool:
        """A status whose whole point is a wait, and no wait was ever read."""
        return self.status_code in SIZING_MATTERS and self.sized == 0


class Tally:
    """Counts of classification outcomes, safe to call from any thread.

    The classification hook runs on the host's error path, from whichever
    thread happened to make the call, so the counter has to be as thread-safe
    as the selection state is — and just as cheap, because it runs on every
    failure whether KAME acts on it or not.
    """

    def __init__(self, *, max_rows: int = MAX_ROWS) -> None:
        self._max_rows = int(max_rows)
        self._lock = threading.Lock()
        # (provider, status) -> [total, sized]
        self._rows: Dict[tuple, List[int]] = {}

    def note(self, provider: object, status_code: object, *, sized: bool) -> None:
        """Count one failure the hook was asked about.

        ``sized`` is whether KAME returned a verdict, not whether the pool
        kept the deadline in it — that second question is the journal's
        (``SIZED_BY_DROPPED``), and answering it here would need state this
        module deliberately does not have.
        """
        name = str(provider or "").strip().lower()
        try:
            status = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status = None
        with self._lock:
            if len(self._rows) >= self._max_rows and (name, status) not in self._rows:
                self._rows.clear()
            row = self._rows.setdefault((name, status), [0, 0])
            row[0] += 1
            if sized:
                row[1] += 1

    def snapshot(self) -> List[Seen]:
        """Every row, busiest first, as plain values.

        A copy: the caller renders it while other threads keep failing, and a
        report that raised mid-turn would be worse than no report.
        """
        with self._lock:
            rows = [
                Seen(provider=name, status_code=status, total=total, sized=sized)
                for (name, status), (total, sized) in self._rows.items()
            ]
        rows.sort(key=lambda row: (-row.total, row.provider, row.status_code or 0))
        return rows

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
