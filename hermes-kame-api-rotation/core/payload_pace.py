"""1.9.0.6 (failures/0034, the owner's test of 2026-11-24): one reply never floods a provider with the same request.

What happened: Google answered ``429 too_many_requests: The query-per-second (QPS) limit for identical or near-identical
request payloads has been exceeded``. That refusal is about the REQUEST, not the key: every key and every model of the
account refuses the same payload. KAME read it as one key's throttle, asked the next key 0.6 s later, and after seven
keys the next model (where every key was fresh), so one reply sent 184 copies of the same 26k-token request in two
minutes, renewing the limit with each one, and never answered.

Two rules, both keyed on the payload (the messages, the tools, the system text - never the model, the key or the
stream flag), so a hop to another model of the list or another key keeps them:

* **The provider said "identical payload"**: the reply holds that payload 2 s, then 4, 8, 16, 30 s (no key rests,
  nothing goes in a model's record) and asks again. A leg that would start during the hold waits for it.
* **A streak of throttles** (429, the above) on one payload: the first eight go at once (a lap over the pool to find
  the key that answers - the speed of every version before); then at most one ask per second, per 2 s after sixteen,
  per 4 s after thirty-two. A 503 keeps its own ladder; a refusal slower than the gap is never slowed. A background
  job (title, summary) has its own request budget and is not paced.

The first word of any answer clears the payload's record. Entries are forgotten after three quiet minutes.
Nothing here sends, sleeps or raises: the transport asks how long to wait and waits on its own clock.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Dict, Mapping, Optional

#: The words a provider uses for a refusal of the payload itself (Google, 2026-11-24). Lower-case substrings.
IDENTICAL_PAYLOAD_INDICATORS = (
    "identical or near-identical request",
    "near-identical request",
    "identical request payload",
    "duplicate request payload",
)

#: Seconds the reply holds one payload after the provider said "identical", one step per refusal in a row.
HOLD_STEPS = (2.0, 4.0, 8.0, 16.0, 30.0)
#: Two legs of one race refused at the same instant are one refusal, not two steps.
HOLD_SAME_EVENT_S = 1.0
#: Refusal kinds that count toward a streak. Throttles only: a 503 (server busy) keeps its own per-key ladder and the
#: busy hand-over, which already move a reply fast without flooding (1.9.0.6 review: spacing 503s slowed the walk).
STREAK_KINDS = frozenset({"rate_limit", "per_minute", "payload_repeat"})
#: Refusals in a row that go at once: a lap over a large pool to find the key that answers (1.8.2.8: six limited keys
#: and the seventh answers in 0.07 s - that speed stays).
STREAK_FREE = 8
#: A payload not asked for this long is forgotten.
FORGET_S = 180.0

#: Request fields that make the payload (everything else - model, stream, timeout, key - is the routing).
_PAYLOAD_FIELDS = ("messages", "contents", "input", "system", "system_instruction", "tools", "tool_choice")


def is_identical_refusal(text: Any) -> bool:
    """Did the provider refuse this request for being sent too often?"""
    try:
        low = str(text or "").lower()
    except Exception:
        return False
    return any(marker in low for marker in IDENTICAL_PAYLOAD_INDICATORS)


def fingerprint(request: Any) -> str:
    """A short, stable name for a request's payload; ``""`` when there is none to name."""
    try:
        if not isinstance(request, Mapping):
            return ""
        part = {name: request[name] for name in _PAYLOAD_FIELDS if name in request}
        if not part:
            return ""
        blob = json.dumps(part, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        return ""


def streak_gap(streak: int) -> float:
    """Seconds between two asks of one payload after ``streak`` refusals in a row."""
    if streak < STREAK_FREE:
        return 0.0
    if streak < 16:
        return 1.0
    if streak < 32:
        return 2.0
    return 4.0


class PayloadPace:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, float]] = {}

    def _entry(self, fp: str, now: float) -> Dict[str, float]:
        entry = self._state.get(fp)
        if entry is None:
            if len(self._state) > 256:
                self._forget(now)
            entry = {"last_sent": 0.0, "streak": 0.0, "identical": 0.0, "hold_until": 0.0,
                     "held_at": -1e9, "touched": now}
            self._state[fp] = entry
        entry["touched"] = now
        return entry

    def _forget(self, now: float) -> None:
        for name in [n for n, e in self._state.items() if now - e.get("touched", 0.0) > FORGET_S]:
            self._state.pop(name, None)

    def note_sent(self, fp: str) -> None:
        if not fp:
            return
        with self._lock:
            now = self._clock()
            self._entry(fp, now)["last_sent"] = now

    def note_refusal(self, fp: str, kind: str) -> None:
        if not fp or kind not in STREAK_KINDS:
            return
        with self._lock:
            entry = self._entry(fp, self._clock())
            entry["streak"] += 1.0

    def note_identical(self, fp: str) -> float:
        """The provider said "identical payload": the hold, in seconds from now (0.0 without a payload)."""
        if not fp:
            return 0.0
        with self._lock:
            now = self._clock()
            entry = self._entry(fp, now)
            if now - entry["held_at"] < HOLD_SAME_EVENT_S:
                return max(0.0, entry["hold_until"] - now)
            step = HOLD_STEPS[min(int(entry["identical"]), len(HOLD_STEPS) - 1)]
            entry["identical"] += 1.0
            entry["held_at"] = now
            entry["hold_until"] = max(entry["hold_until"], now + step)
            return step

    def note_answered(self, fp: str) -> None:
        if not fp:
            return
        with self._lock:
            self._state.pop(fp, None)

    def held_s(self, fp: str) -> float:
        """Seconds left of an "identical payload" hold (0.0 when none)."""
        if not fp:
            return 0.0
        with self._lock:
            entry = self._state.get(fp)
            if entry is None:
                return 0.0
            return max(0.0, entry["hold_until"] - self._clock())

    def wait_s(self, fp: str) -> float:
        """Seconds before this payload may be asked again: the hold, or the streak's gap from the last ask."""
        if not fp:
            return 0.0
        with self._lock:
            entry = self._state.get(fp)
            if entry is None:
                return 0.0
            now = self._clock()
            hold = entry["hold_until"] - now
            gap = entry["last_sent"] + streak_gap(int(entry["streak"])) - now if entry["last_sent"] else 0.0
            return max(0.0, hold, gap)

    def why(self, fp: str) -> str:
        """``"identical"`` while the provider's hold runs, ``"streak"`` while only the gap does, else ``""``."""
        if not fp:
            return ""
        with self._lock:
            entry = self._state.get(fp)
            if entry is None:
                return ""
            return "identical" if entry["hold_until"] > self._clock() else "streak"

    def streak(self, fp: str) -> int:
        with self._lock:
            entry = self._state.get(fp)
            return int(entry["streak"]) if entry else 0

    def clear(self) -> None:
        with self._lock:
            self._state.clear()


PACE = PayloadPace()


def payload_of(call: Any) -> str:
    """The fingerprint of a call's payload, computed once per call."""
    try:
        fp = getattr(call, "_kame_payload_fp", None)
        if fp is None:
            fp = fingerprint(getattr(call, "kwargs", None))
            try:
                setattr(call, "_kame_payload_fp", fp)
            except Exception:
                pass
        return fp or ""
    except Exception:
        return ""
