"""How many model switches Hermes has made in one conversation since its last response.

1.9.0.6 (decisions/0037 C1). Hermes ends a turn once the "rebuilt-message restarts" since the last response pass
``agent.api_max_retries`` (default 3); every fallback activation, that is every hand-over KAME raises, is one
restart. KAME counts them per conversation and never spends the last one (:func:`allowed` is the limit minus one),
so a reply is never ended because of KAME.

Pure and process-wide: a dict under one lock, no I/O inside the lock. Every function is wrapped so it never raises;
an internal error always resolves to "do not hand over" (the safe direction: KAME waits).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: An entry idle this long is dropped on the next write. A day: a reply may wait hours inside KAME while Hermes'
#: own restart count (not reset without a response) stays, so forgetting earlier could hand over past the limit.
IDLE_S = 24 * 3600.0
#: At most this many conversations are remembered (the oldest ``last_at`` goes first).
MAX_KEYS = 256
#: The shared key ``""`` (conversations KAME cannot name) is reset only when nobody can be mid-walk.
SHARED_IDLE_S = 120.0

_LOCK = threading.Lock()
#: key -> [used, last_at]
_COUNTS: Dict[str, list] = {}


def _now() -> float:
    return time.monotonic()


def conversation_key() -> str:
    """The id of the conversation this call belongs to, or ``""`` when it cannot be told.

    1.9.0.6 (decisions/0037 C1, amended): the task-local ContextVar ONLY, through
    ``gateway.session_context._bound_session_value``. Never ``get_session_env`` and never ``os.environ``:
    Hermes writes the LAST agent's id into ``os.environ`` for the whole process, so two sessions would count
    on each other's key and could under-count (the one unsafe direction)."""
    try:
        from . import session_key      # 1.9.0.6 (C16): the one reader, shared with settings

        return session_key.first_bound()
    except Exception:
        return ""


def allowed() -> int:
    """How many hand-overs one conversation may raise between two responses: the limit minus one."""
    try:
        from . import fallback_chain

        return max(0, int(fallback_chain.switch_limit()) - 1)
    except Exception:
        return 0


def used(key: str) -> int:
    try:
        with _LOCK:
            entry = _COUNTS.get(str(key or ""))
            return int(entry[0]) if entry else 0
    except Exception:
        return 0


def _trim(now: float) -> None:
    """Drop idle entries and cap the table. Called with the lock held; pure dict work."""
    for old in [k for k, (_n, at) in _COUNTS.items() if now - at >= IDLE_S]:
        _COUNTS.pop(old, None)
    while len(_COUNTS) > MAX_KEYS:
        oldest = min(_COUNTS, key=lambda k: _COUNTS[k][1])
        _COUNTS.pop(oldest, None)


def note_switch(key: str) -> None:
    """Hermes was (or may have been) made to switch model: +1."""
    try:
        now = _now()
        with _LOCK:
            _trim(now)
            entry = _COUNTS.setdefault(str(key or ""), [0, now])
            entry[0] += 1
            entry[1] = now
            _trim(now)
    except Exception:
        logger.debug("kame: could not count a switch", exc_info=True)


def _reset(key: str) -> None:
    try:
        key = str(key or "")
        now = _now()
        with _LOCK:
            entry = _COUNTS.get(key)
            if entry is None:
                return
            if key == "" and now - entry[1] < SHARED_IDLE_S:
                return      # another call may be mid-walk on the shared key: it only goes down when idle
            _COUNTS.pop(key, None)
            _trim(now)
    except Exception:
        logger.debug("kame: could not reset the switch count", exc_info=True)


def note_answer(key: str) -> None:
    """A response arrived: Hermes resets its own restart count, so does KAME's."""
    _reset(key)


def turn_started(key: str) -> None:
    """A new turn: Hermes' restart count starts at 0."""
    _reset(key)


def may_hand_over(key: str) -> bool:
    """Is a hand-over still allowed for this conversation? Any internal error -> ``False``."""
    try:
        limit = allowed()
        key = str(key or "")
        now = _now()
        with _LOCK:
            entry = _COUNTS.get(key)
            if entry is not None:
                entry[1] = now      # a call is walking on this key: it is not idle
            count = int(entry[0]) if entry else 0
        return count < limit
    except Exception:
        return False


def reset_all() -> None:
    """Tests, and the panel's Reset pool."""
    try:
        with _LOCK:
            _COUNTS.clear()
    except Exception:
        pass
