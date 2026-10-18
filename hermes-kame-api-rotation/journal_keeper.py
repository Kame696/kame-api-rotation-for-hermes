"""The quota journal's writer, without the pool binding that used to own it.

``core.journal`` records every refusal with what KAME predicted about it, and
every call that answered, so ``/kame-quota`` can say whether the predictions
held. Until 1.8.1.8 the writer lived inside ``pool_binding``, which also
rewrapped Hermes' credential pool; 1.8.1.9 removes that binding (catalog rule
9) and keeps the journal: the rotation path files refusals through
``runtime.record_rotation`` and answers through ``runtime.note_answered``,
both registered on this object.

The two methods below are ``pool_binding.note_rotation`` / ``_record_block``
and the journal half of ``note_success`` (1.8.1.8), unchanged in what they
write. ``_store`` and ``_journal`` keep their names because ``/kame-quota``
and the panel read them under those names.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Callable, Optional, Tuple

from .core import dispersion
from .core import journal as journal_module
from .core import reconcile

logger = logging.getLogger(__name__)


class _JustAnId:
    """A stand-in carrying only the credential id a journal row is filed under."""

    __slots__ = ("id",)

    def __init__(self, credential_id: Any) -> None:
        self.id = str(credential_id or "")


class JournalKeeper:
    """Files refusals and answers into the journal; owns nothing in Hermes."""

    #: Two blocks for the same credential and model this close together are one
    #: refusal seen twice (1.6.0.3).
    ROTATION_DEDUPE_S = 2.0
    ROTATION_DEDUPE_SLOTS = 256

    def __init__(self, store: Any, *, journal: Any = None, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._journal = journal
        self._clock = clock
        self._last_block_at: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        # Read by ``/kame-quota``; selection lives in the carousel now, so this
        # one only ever reports an empty window rather than a wrong one.
        self._dispersion = dispersion.Dispersion()
        self._names: dict = {}
        self.installed = True
        self.reason = "active"
        # The panel's credential card reads these off whatever it was handed
        # as the pool binding. The client splits every comma list itself and
        # never sends one whole, so both answers are simply "yes".
        self.splitting_multikey = True
        self.guarding_current = True
        self.blamed_another_key = 0

    @property
    def seen_pools(self) -> dict:
        from . import facade

        return facade.SEEN_POOLS

    def _already_written(self, credential_id: Any, model: Any, now: float) -> bool:
        try:
            key = (str(credential_id or ""), str(model or ""))
            if not key[0] or not key[1]:
                return False
            seen = self._last_block_at.get(key)
            if seen is not None and 0.0 <= now - seen <= self.ROTATION_DEDUPE_S:
                return True
            self._last_block_at[key] = now
            self._last_block_at.move_to_end(key)
            while len(self._last_block_at) > self.ROTATION_DEDUPE_SLOTS:
                self._last_block_at.popitem(last=False)
            return False
        except Exception:  # pragma: no cover - defensive
            return False

    def note_rotation(
        self,
        *,
        credential_id: str,
        provider: str,
        model: str,
        held_to: Optional[float],
        status_code: Any = None,
        reason: str = "rate_limit",
        judgement: Any = None,
        now: Optional[float] = None,
    ) -> None:
        """File a refusal the carousel benched inside a call."""
        if self._journal is None:
            return
        moment = float(now) if now is not None else self._clock()
        if self._already_written(credential_id, model, moment):
            return
        window = judgement.window if judgement is not None else "unknown"
        source = judgement.source if judgement is not None else ""
        stated = getattr(judgement, "stated", "unknown") if judgement is not None else "unknown"
        sized_by = journal_module.SIZED_BY_HOST
        if judgement is not None and judgement.reset_at is not None:
            sized_by = journal_module.SIZED_BY_DROPPED
            if held_to is not None and (
                abs(float(held_to) - float(judgement.reset_at)) <= reconcile.FINGERPRINT_TOLERANCE_SECONDS
            ):
                sized_by = journal_module.SIZED_BY_KAME
        book = self._journal.load()
        row = book.record_block(
            at=moment,
            provider=provider,
            model=model,
            credential_id=str(credential_id or ""),
            status_code=status_code if isinstance(status_code, int) else None,
            window=window,
            source=source,
            reset_at=held_to,
            sized_by=sized_by,
            reason=reason,
            stated_window=stated,
        )
        if row is None:
            return
        self._journal.save(book, now=moment)

    def note_answered(self, *, provider: str, model: str, credential_id: str) -> None:
        """File a call that answered, against the key that actually answered it."""
        if self._journal is None or not credential_id:
            return
        now = self._clock()
        book = self._journal.load()
        recovery = book.record_success(at=now, provider=provider, model=model, credential_id=credential_id)
        if recovery is None:
            return
        self._journal.save(book, now=now)
        logger.debug(
            "kame: %s came back on %s after %.0fs (predicted %s)",
            credential_id[:8],
            recovery.model,
            recovery.observed_seconds,
            "early" if recovery.was_early else "on time or late",
        )
