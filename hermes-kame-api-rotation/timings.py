"""How long each attempt took, so "is it slow?" stops being an opinion.

The plugin can already say *"answered on attempt 38 after 139 seconds"*. It
cannot say where those 139 seconds went, and the two possible answers are
opposite verdicts: 130 seconds of benched keys and 9 of provider is a quota
problem, and 9 of benches and 130 of provider is a slow model. Both look
identical from the outside, and both were argued about — from memory, on this
project, more than once.

So this writes one line per attempt. Six facts, and every one of them is a
duration or a count:

``ms_to_first_sign``
    From the request leaving to *anything* coming back — a reasoning token, a
    tool name, a spinner frame. This is what the "first token wait" setting
    actually races against, which is why it is separate from the next one.
``ms_to_first_text``
    From the request leaving to the first character **the user can see**.
    ``_Progress`` keeps these apart for a measured reason (1.6.0.3): on a
    thinking model every turn opens with reasoning, so treating the two as one
    made a 503 look like a cut mid-answer.
``ms_total``
    The whole attempt, including the failure when it failed.
``ms_waited_before``
    Legacy name for elapsed time before this attempt, INCLUDING previous
    provider calls. It is retained for compatibility; never sum it as idle time.
``ms_elapsed_before`` / ``ms_pool_waited_before``
    Explicit elapsed time and cumulative time inside the pool-recovery wait
    function. The latter excludes previous provider calls, but includes the
    recovery function's bookkeeping; it is not all overhead or all user latency.
``outcome`` / ``chars_seen``
    What ended it, and the accumulated continuation buffer. Zero is NOT proof
    that no text was delivered in a normal uninterrupted response. Use the
    first-text signal to distinguish observed delivery from absent telemetry.
``rest_s`` / ``rest_source``
    How long a refusal rested the key, and — from 1.8.1.0 — where that
    number came from when the doubling backoff experiment produced it:
    ``backoff.N`` for rung N. Absent for every other rest, which still reads
    exactly as it did before.

**This module decides nothing.** Delete the file, delete the module, and every
rotation behaves the same. That property is the whole point of it existing:
telemetry that can change a verdict is not telemetry, it is a feature with a
log attached.

**Nothing here can carry a secret.** No key, no prompt, no answer text, no
provider message — numbers, a model name, and a key *fingerprint*, which is a
hash the rest of the plugin already prints on screen.

``KAME_CALL_TIMINGS_DISABLED=1`` (or ``call_timings_disabled`` in the plugin's
config entry) stops the writing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Beside the state file and the refusal log.
FILENAME = "calls.jsonl"

#: One line is roughly 200 bytes, so this is a few hundred thousand attempts —
#: far more than a person generates, and small enough that a forgotten install
#: never becomes a disk problem. At the ceiling it stops writing rather than
#: rotating: a window that slides would quietly discard the beginning of the
#: session somebody is trying to explain.
CEILING_BYTES = 4 * 1024 * 1024

#: Set once the ceiling is hit or the destination proves unwritable, so the
#: cost of being off is one boolean per attempt rather than a failed syscall.
_silenced = False


def _destination() -> Optional[str]:
    try:
        from . import state

        folder = state.state_dir()
        if folder is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder / FILENAME)
    except Exception:
        return None


def _off() -> bool:
    try:
        from . import settings

        return bool(settings.is_on(settings.CALL_TIMINGS_DISABLED))
    except Exception:
        return bool(os.environ.get("KAME_CALL_TIMINGS_DISABLED"))


def _ms(seconds: Optional[float]) -> Optional[int]:
    """Milliseconds, rounded. ``None`` stays ``None`` — it means "never".

    A first-token time of ``None`` on a refused attempt is a fact worth
    keeping, and zero would be a different and false one.
    """
    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0:  # NaN, or a clock that went backwards
        return None
    return int(round(value * 1000.0))


def record(
    *,
    identity: str = "",
    key_fingerprint: str = "",
    attempt: int = 0,
    outcome: str = "",
    kind: str = "",
    status_code: Any = None,
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
    first_sign_at: Optional[float] = None,
    first_text_at: Optional[float] = None,
    waited_before_s: Optional[float] = None,
    chars_seen: Optional[int] = None,
    rest_s: Optional[float] = None,
    pool_waited_before_s: Optional[float] = None,
    call_id: str = "",
    rest_source: Optional[str] = None,
) -> None:
    """Write one attempt down. Fails silently, always.

    Called from the hot path of every API call, including the failing ones, so
    every branch here is guarded as one: an attempt that answered and could not
    be *written about* is still an attempt that answered.
    """
    global _silenced
    try:
        if _silenced or _off():
            return
        path = _destination()
        if path is None:
            _silenced = True
            return
        try:
            if os.path.getsize(path) >= CEILING_BYTES:
                _silenced = True
                logger.info(
                    "kame: %s reached %d MB — no more call timings will be written",
                    FILENAME,
                    CEILING_BYTES // (1024 * 1024),
                )
                return
        except OSError:
            pass  # Not there yet. The open below creates it.

        row = {
            "at": round(time.time(), 3),
            "identity": str(identity or "")[:120],
            # A hash the panel already shows. Never the key.
            "key": str(key_fingerprint or "")[:32],
            "attempt": int(attempt or 0),
            "outcome": str(outcome or "")[:24],
            "kind": str(kind or "")[:24],
            "status": status_code if isinstance(status_code, int) else None,
            "ms_total": _ms(None if (started_at is None or ended_at is None)
                            else ended_at - started_at),
            "ms_to_first_sign": _ms(None if (started_at is None or first_sign_at is None)
                                    else first_sign_at - started_at),
            "ms_to_first_text": _ms(None if (started_at is None or first_text_at is None)
                                    else first_text_at - started_at),
            "ms_waited_before": _ms(waited_before_s),
            "ms_elapsed_before": _ms(waited_before_s),
            "ms_pool_waited_before": _ms(pool_waited_before_s),
            "call_id": str(call_id or "")[:80],
            "chars_seen": int(chars_seen) if isinstance(chars_seen, int) else None,
            "rest_s": None if rest_s is None else round(float(rest_s), 1),
            # 1.8.1.0. Where ``rest_s`` came from, when it is worth saying —
            # today only the doubling ladder fills it, as ``"backoff.N"`` for
            # rung N, so a 4-second rest the ladder produced can be told
            # apart from a 4-second flat rest or a 4-second stated delay in
            # the same file. ``None`` otherwise, same convention as every
            # other optional field here: absent means nothing sized it this
            # way, not "unknown".
            "rest_source": str(rest_source)[:24] if rest_source else None,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        # Never twice: an instrument that logs its own failure on every call
        # is louder than the thing it was measuring.
        _silenced = True
        logger.debug("kame: call timings are off after an error", exc_info=True)


def forget() -> None:
    """Re-enable writing. Tests, and a fresh start after the ceiling."""
    global _silenced
    _silenced = False
