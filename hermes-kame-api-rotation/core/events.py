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
#:
#: 400 since 1.8.2.0, when every call started to leave a ``sent`` row and an
#: ``answered`` row: at 150 a busy hour of healthy traffic would have pushed
#: the last incident off the screen. About 65 KB in the snapshot.
MAX_EVENTS = 400

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

#: The owner changing a setting. Not a decision KAME made — which is exactly
#: why it belongs on the same timeline. 1.7.0.0 kept only the *last* control
#: request, in one slot, so a setting turned on mid-session and off an hour
#: later left a single trace: the off. "Did that setting help?" could then
#: only be answered with a guessed window.
SETTING = "setting"

#: 1.8.2.0. The ordinary half of every call, so the tab shows everything KAME
#: did and not only what went wrong: which key a call went out on (``sent``)
#: and that it answered, how fast (``answered``). The owner's own test of
#: 1.8.1.9 sent a nine-minute NVIDIA answer through a healthy pool and found
#: no trace of it anywhere on the panel.
SENT = "sent"
ANSWERED = "answered"
#: The opt-in ``max_total_wait_seconds`` ran out and the refusal went to Hermes.
GAVE_UP = "gave_up"
#: 1.8.2.7. Every key of the model in use was resting for long, the user has a
#: fallback chain in Hermes and has opted in, so the call went to Hermes to
#: answer with the next model. A planned switch, not a failure.
HANDOVER = "handover"

_KINDS = frozenset(
    {ROTATION, SWITCH, QUARANTINE, INVALID_KEY, DENIED_MODEL, STORM,
     STREAM_DROP, STITCH, WAIT, RECOVERY, SURFACED, SETTING, SENT, ANSWERED, GAVE_UP, HANDOVER}
)

#: The kinds that are KAME working rather than a provider failing. The panel
#: colours and filters on this split, and the distinction is the point of the
#: 1.6.0.1 Events tab: a screen that shows only refusals reads like a fault
#: report, and a rotation engine that is doing its job is not a fault.
GOOD_KINDS = frozenset({SWITCH, RECOVERY, STITCH, WAIT, SETTING, SENT, ANSWERED, HANDOVER})
#: ``SETTING`` sits on this side of the split for the same reason: it is
#: not a provider refusing, and a row painted as a fault would be read as
#: one. It is the owner acting, which is neither, and of the two colours
#: available that is the honest one.


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
        task: str = "",
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
            # 1.8.2.6: which background job sent the call. Only on the rows
            # that have one, so a Main row reads exactly as it always did.
            if task:
                row["task"] = str(task)[:40]
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

# 1.8.2.3: Secondary events — calls from auxiliary models (title, vision,
# compression, web summary, approval, MCP routing, skill search).  A separate
# buffer so the main Events tab shows only the chat model.
SECONDARY_EVENTS = Events(limit=200)

#: ContextVar-based slot name for the current auxiliary call.
#: Set by hermes-kame-provider before each auxiliary client call, cleared
#: after.  When set, the transport logs to SECONDARY_EVENTS instead of EVENTS.
import contextvars
_CURRENT_AUX_SLOT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kame_aux_slot", default=""
)


def set_aux_slot(slot: str) -> contextvars.Token:
    """Mark the current context as an auxiliary call from the named slot."""
    return _CURRENT_AUX_SLOT.set(slot)


def clear_aux_slot(token: contextvars.Token) -> None:
    """Restore the previous slot (usually empty = main chat)."""
    _CURRENT_AUX_SLOT.reset(token)


def get_aux_slot() -> str:
    """The auxiliary slot name, or ``""`` for the main chat model."""
    return _CURRENT_AUX_SLOT.get()


def active_events() -> "Events":
    """The Events buffer for the current context — main or secondary."""
    return SECONDARY_EVENTS if _CURRENT_AUX_SLOT.get() else EVENTS


#: 1.8.2.6. Where Hermes itself says "this is an auxiliary call, for task X".
#: ``agent.auxiliary_client`` binds a fresh dict to this ContextVar for every
#: auxiliary call (``_relay_aux_call_scope``: ``{"task": ..., ...}``) and copies
#: the context into the daemon thread that runs the provider callback, so it is
#: visible exactly where a KAME client is asked for a completion. The main
#: agent loop never binds it. ``set_aux_slot`` above was meant to be fed by a
#: ``pre_auxiliary_call`` hook, but that hook runs in a bounded worker thread,
#: so a ContextVar set there never reaches the call — which is why the
#: Background tab stayed empty through 1.8.2.5.
_HOST_AUX_MODULE = "agent.auxiliary_client"
_HOST_AUX_VAR = "_RELAY_AUX_CALL_CONTEXT"


def host_aux_task() -> str:
    """The auxiliary task Hermes is running in this context, or ``""``.

    Read-only and import-free: if Hermes has not loaded its auxiliary client,
    no auxiliary call can be in flight. Never raises.
    """
    try:
        import sys

        module = sys.modules.get(_HOST_AUX_MODULE)
        var = getattr(module, _HOST_AUX_VAR, None) if module is not None else None
        context = var.get() if var is not None else None
    except Exception:
        return ""
    if not isinstance(context, dict):
        return ""
    task = str(context.get("task") or "").strip()
    return task if task and task != "unknown" else "auxiliary"


class InFlight:
    """The calls that have not finished yet — for the "still waiting" line.

    1.8.2.6. On 2026-10-13 one background call waited 57 minutes and 294
    attempts through a Google overload before it answered. Nothing on the
    panel said so: the rows scrolled past one by one. This keeps one entry
    per running call (started, attempts, the last reason), so the panel can
    say "title: no answer for 57 min, 294 tries". It decides nothing.
    """

    #: An entry with no news for this long is dropped even if its end was
    #: never seen (a generator Hermes abandoned without closing it).
    STALE_S = 600.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: Dict[int, Dict[str, Any]] = {}

    def start(self, call: Any) -> None:
        try:
            now = time.time()
            row = {
                "channel": str(getattr(call, "channel", "main") or "main"),
                "task": str(getattr(call, "task", "") or ""),
                "identity": str(getattr(call, "identity", "") or ""),
                "started": now,
                "last_at": now,
                "failures": 0,
                "reason": "",
            }
        except Exception:
            return
        with self._lock:
            self._calls[id(call)] = row

    def note(self, call: Any, kind: str, reason: str = "", *, identity: str = "", fingerprint: str = "") -> None:
        with self._lock:
            row = self._calls.get(id(call))
            if row is None:
                return
            row["last_at"] = time.time()
            if fingerprint and kind in (SENT, SWITCH):
                # 1.8.2.9: the key now on the wire, for the panel's ring ("answering now")
                row["key"] = str(fingerprint)
                row["key_identity"] = str(identity or row.get("identity") or "")
            if kind not in GOOD_KINDS:
                row["failures"] += 1
                if reason:
                    row["reason"] = str(reason)[:120]

    def end(self, call: Any) -> None:
        with self._lock:
            self._calls.pop(id(call), None)

    def keys_on_the_wire(self) -> Dict[str, List[str]]:
        """``{identity: [key fingerprints]}`` of the key each running call last put on the wire."""
        out: Dict[str, List[str]] = {}
        with self._lock:
            for row in self._calls.values():
                if row.get("key"):
                    out.setdefault(row.get("key_identity") or row.get("identity") or "", []).append(row["key"])
        return out

    def rows(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Running calls, longest-waiting first."""
        now = time.time() if now is None else now
        with self._lock:
            for key in [k for k, r in self._calls.items() if now - r["last_at"] > self.STALE_S]:
                self._calls.pop(key, None)
            rows = [dict(r, waiting_s=round(now - r["started"], 1)) for r in self._calls.values()]
        rows.sort(key=lambda r: -r["waiting_s"])
        return rows

    def clear(self) -> None:
        with self._lock:
            self._calls.clear()


INFLIGHT = InFlight()
