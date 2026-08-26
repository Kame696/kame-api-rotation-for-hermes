"""Durable home for KAME's two documents, on top of ``ctx.state``.

``ctx.state`` is the storage Hermes gives a plugin: profile-scoped, atomic,
file-locked, quota-bounded. It is the right place for both the ledger and the
journal, and the wrong thing to call from a hot path — every ``get`` takes a
cross-process lock and re-reads the whole JSON file, and the pool asks "which
keys are available" on every single credential selection.

So this class sits in between: an in-memory copy that answers instantly, a
short staleness window before it re-reads, and a write-through on every
change. The tradeoff is deliberate and small. Within the window a second
Hermes process can record a bench this one has not seen yet; the cost is one
request that tries a key already spent elsewhere, after which this process
records the bench itself and converges. The alternative — a locked file read
per selection — would put disk I/O in front of every API call to save a
failure that costs one retry.

Two documents, two keys, two caches. They are kept apart rather than merged
into one because they are written at different rates and read by different
things: the ledger changes on every bench and is consulted on every selection,
while the journal is appended to on failure and read by a report. A single
document would make the report's history share a write path with the hot
loop, and a corrupt journal would take the ledger down with it.

**Nothing here raises.** These are optimisations over the host's own
cooldowns; if their storage is unreadable, unwritable, or over quota, the
right outcome is to lose the optimisation quietly, not to break an API call
on the error path. Failures are logged once per kind and then suppressed,
because a disk that cannot be written will not start working on the next of
five hundred retries and does not deserve five hundred log lines.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .core.journal import Journal
from .core.ledger import Ledger

logger = logging.getLogger(__name__)

# Keys inside the plugin's state document. Namespaced and versioned so a later
# KAME feature can add its own, and a shape change can land beside the old one
# instead of on top of it.
STATE_KEY = "quota_ledger_v1"
JOURNAL_KEY = "quota_journal_v1"

# How long an in-memory ledger copy is trusted before the file is consulted
# again. Sized against what it protects: a cross-process race whose penalty is
# a single failed request. Long enough that a burst of selections costs one
# read, short enough that a second profile's benches land within a turn.
DEFAULT_TTL_SECONDS = 15.0

# The journal is written on failure and read by a report, so a stale copy
# costs nothing but a slightly late statistic. It gets a longer window.
JOURNAL_TTL_SECONDS = 60.0


class _Document:
    """Read-mostly cache over one ``ctx.state`` key.

    Subclasses supply the empty value and the decoder. Everything stored
    through here must offer ``prune(now)`` and ``to_dict()``; that is the
    whole contract, and it is what lets one cache serve two unrelated shapes.
    """

    __slots__ = (
        "_state",
        "_key",
        "_ttl",
        "_clock",
        "_cached",
        "_read_at",
        "_warned",
        "_unsaved",
    )

    _LABEL = "document"

    def __init__(
        self,
        state: Any,
        *,
        key: str = "",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state = state
        self._key = key or self._default_key()
        self._ttl = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._cached: Any = None
        self._read_at = 0.0
        self._warned: set = set()
        # Set when a write did not land. While it is set, storage holds a copy
        # known to be older than the one in hand, and re-reading it would throw
        # away facts this process has already acted on.
        self._unsaved = False

    # -- subclass hooks --------------------------------------------------

    @staticmethod
    def _default_key() -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def _empty() -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def _decode(payload: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- reading ---------------------------------------------------------

    def load(self, *, force: bool = False) -> Any:
        """The current document, re-reading storage when the copy is stale.

        Always returns something usable; an unreadable store yields an empty
        document, which degrades the plugin to sizing cooldowns correctly
        without the per-model rescue.
        """
        now = self._clock()
        if not force and self._cached is not None and (now - self._read_at) < self._ttl:
            return self._cached
        # ``force`` means "do not serve a stale copy". While a write is
        # outstanding the copy in hand is the fresh one and storage is the
        # stale one, so the guard below applies to forced reads too.
        if self._unsaved and self._cached is not None:
            # A failed write does not make the plugin forget. Re-reading here
            # would resurrect benches this process has already released and
            # lose probe attempts it has already spent — turning a degraded
            # store into wrong behaviour rather than merely non-durable
            # behaviour.
            return self._cached
        self._cached = self._read()
        self._read_at = now
        return self._cached

    def _read(self) -> Any:
        if self._state is None:
            return self._empty()
        try:
            payload = self._state.get(self._key, None)
        except Exception:
            self._warn_once("read", "cannot read plugin state; per-model memory disabled")
            return self._cached if self._cached is not None else self._empty()
        if payload is None:
            return self._empty()
        return self._decode(payload)

    # -- writing ---------------------------------------------------------

    def save(self, document: Any, *, now: Optional[float] = None) -> bool:
        """Persist the document, pruning first. Returns whether the write landed.

        The in-memory copy is updated either way: a bench this process just
        learned about is worth acting on for the rest of the session even if
        it could not be written down for the next one.
        """
        moment = self._clock() if now is None else now
        document.prune(moment)
        self._cached = document
        self._read_at = moment
        if self._state is None:
            self._unsaved = True
            return False
        try:
            self._state.set(self._key, document.to_dict())
        except ValueError:
            # Quota or non-serialisable. Quota should be unreachable — the caps
            # inside both documents keep them to tens of kilobytes against a
            # 10 MiB allowance — so reaching it means something else shares the
            # document and KAME should yield rather than fight.
            self._warn_once("quota", "plugin state rejected the write; keeping it in memory only")
            self._unsaved = True
            return False
        except Exception:
            self._warn_once("write", "cannot write plugin state; this memory is session-only")
            self._unsaved = True
            return False
        self._unsaved = False
        return True

    def clear(self) -> bool:
        """Forget everything — the operator's reset button."""
        return self.save(self._empty())

    # -- diagnostics -----------------------------------------------------

    def _warn_once(self, kind: str, message: str) -> None:
        if kind in self._warned:
            logger.debug("kame %s: %s", self._LABEL, message)
            return
        self._warned.add(kind)
        logger.warning("kame %s: %s", self._LABEL, message, exc_info=True)


class LedgerStore(_Document):
    """The live per-model benches — read on every credential selection."""

    __slots__ = ()

    _LABEL = "ledger"

    def __init__(
        self,
        state: Any,
        *,
        key: str = STATE_KEY,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(state, key=key, ttl_seconds=ttl_seconds, clock=clock)

    @staticmethod
    def _default_key() -> str:
        return STATE_KEY

    @staticmethod
    def _empty() -> Ledger:
        return Ledger()

    @staticmethod
    def _decode(payload: Any) -> Ledger:
        return Ledger.from_dict(payload)

    def load(self, *, force: bool = False) -> Ledger:
        return super().load(force=force)


class JournalStore(_Document):
    """The history — appended on failure, read by ``/kame-quota``."""

    __slots__ = ()

    _LABEL = "journal"

    def __init__(
        self,
        state: Any,
        *,
        key: str = JOURNAL_KEY,
        ttl_seconds: float = JOURNAL_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(state, key=key, ttl_seconds=ttl_seconds, clock=clock)

    @staticmethod
    def _default_key() -> str:
        return JOURNAL_KEY

    @staticmethod
    def _empty() -> Journal:
        return Journal()

    @staticmethod
    def _decode(payload: Any) -> Journal:
        return Journal.from_dict(payload)

    def load(self, *, force: bool = False) -> Journal:
        return super().load(force=force)
