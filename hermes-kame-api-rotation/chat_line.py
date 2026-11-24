"""1.9.0.5 (PLAN C1, C2, C3): a live line in the conversation, through the public ``ctx.post_message``.

What this is for: while API Rotation acts on a reply (a refused key replaced, every key resting, another
model answering) the person should see it in the chat itself, not only in a panel they may not have open.

What this is NOT: a message the model reads (``post_message`` never enters the model's context), a second
copy of the transport's decisions (it only listens: ``note`` is called beside ``_emit`` and ``note_status``
beside ``_Spinner.update``), or a way to block a reply (nothing here waits for the gateway: the reply
thread only stores text; ONE daemon worker thread posts and edits).

Surface rules (``PLAN_1.9.0.5`` C1, from Hermes v0.23.3 ``gateway/plugin_messages.py``: a local post is a
*sticky* ``notification.show`` with a NEW key per post, edits keep the key, there is no clear call):

* ``desktop``  - no live posts at all. Only the one-time notices (C3 "turn it on", A2/A4), once per session.
* ``tui``      - one handle per SESSION: posted on the first acting reply, only edited afterwards.
* a messaging platform - one message per acting reply, edited in place, and only for a reply that has waited
  a minute or another model answering (1.9.0.6, decisions/0037 C20: was ten seconds). Its final edit is one short
  line, ``✓ KAME: answered after N s``; a failure keeps its text.
* classic ``cli``, ``acp`` and anything unknown - nothing.

A clean reply posts nothing. ``ok == False`` from the host (no conversation: cron, timers) drops the line
silently, never retried. The setting ``live_chat_line`` (on by default) switches the whole thing off.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from . import settings

logger = logging.getLogger(__name__)

#: At most one edit per this long per message; the last text wins.
EDIT_EVERY_S = 1.5
#: Messaging platforms rate-limit edits hard: at least this long between two edits of one message. The final
#: edit (the answer, or the failure) is always sent at once.
MESSAGING_EDIT_EVERY_S = 20.0
#: A messaging-platform message is posted when a reply has waited this long (the event's own countdown, or the
#: time since the reply began) or when KAME moved it to another model. 1.9.0.6 (decisions/0037 C20): was 10 s; the
#: host's handle can only edit, never delete, so a short wait must leave nothing behind.
MESSAGING_WAIT_S = 60.0
#: The Desktop panel reads ``/state`` every couple of seconds; no read for this long = it is not open/installed.
DESKTOP_READ_FRESH_S = 600.0
#: A reply's line that has not been touched for this long is a leftover of an unfinished reply.
_STALE_S = 600.0
_MAX_TRACKED = 64

DESKTOP_NOTICE = "Turn on API Rotation in Capabilities -> Plugins to see its status bar and panel."
DESKTOP_READ_FILE = "desktop_read.txt"

#: Events that are KAME acting on a reply (``core.events`` kinds). ``sent`` / ``answered`` / ``setting`` are not.
_RETRY_KINDS = frozenset({"rotation", "quarantine", "invalid_key", "denied_model", "storm", "stream_drop"})
_ACTING = _RETRY_KINDS | {"switch", "stitch", "wait", "handover", "retest"}
_FAILED = frozenset({"surfaced", "gave_up"})
_ANSWERED = frozenset({"answered", "recovery"})

_LOCK = threading.Condition()
_CTX: Any = None
_LINES: "OrderedDict[str, _Line]" = OrderedDict()
_SESSIONS: "OrderedDict[str, _Slot]" = OrderedDict()
_NOTICED: "OrderedDict[tuple, bool]" = OrderedDict()
_PENDING: List["_Slot"] = []
_BUSY = False
_THREAD: Optional[threading.Thread] = None


# --- lifecycle ---------------------------------------------------------------------------------------------


def attach(ctx: Any) -> None:
    """``register(ctx)`` stores the plugin context (``post_message`` / ``session_surface`` live on it)."""
    global _CTX
    with _LOCK:
        _CTX = ctx


def detach() -> None:
    """Unload: forget the context and every open line. The worker thread stays idle (daemon)."""
    global _CTX
    with _LOCK:
        _CTX = None
        _LINES.clear()
        _SESSIONS.clear()
        _NOTICED.clear()
        del _PENDING[:]


def reset() -> None:
    """Forget every line, session handle and notice (tests; the context is kept)."""
    with _LOCK:
        _LINES.clear()
        _SESSIONS.clear()
        _NOTICED.clear()
        del _PENDING[:]


def flush(timeout: float = 5.0) -> bool:
    """Wait until the worker has nothing left to send. For tests and the unload path; never the reply thread."""
    deadline = time.monotonic() + timeout
    with _LOCK:
        while _PENDING or _BUSY:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _LOCK.wait(min(left, 0.05))
    return True


# --- one message: a slot -----------------------------------------------------------------------------------


class _Slot:
    """One message in the conversation: first a post, then edits. ``text`` is the next thing to say."""

    __slots__ = ("plugin_ctx", "run_ctx", "handle", "dead", "text", "sent_text", "last_at", "messaging", "final")

    def __init__(self, messaging: bool = False) -> None:
        self.messaging = messaging
        self.final = False
        self.plugin_ctx: Any = None
        self.run_ctx: Optional[contextvars.Context] = None
        self.handle: Any = None
        self.dead = False
        self.text: Optional[str] = None
        self.sent_text = ""
        self.last_at = 0.0


def _submit(slot: _Slot, text: str, plugin_ctx: Any, run_ctx: contextvars.Context) -> None:
    """Hand ``text`` to the worker. Never blocks: memory only."""
    global _THREAD
    with _LOCK:
        if slot.dead or text == slot.sent_text or text == slot.text:
            return
        slot.text = text
        if slot.plugin_ctx is None:
            slot.plugin_ctx, slot.run_ctx = plugin_ctx, run_ctx
        if slot not in _PENDING:
            _PENDING.append(slot)
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(target=_work, name="kame-chat-line", daemon=True)
            _THREAD.start()
        _LOCK.notify_all()


def _work() -> None:
    global _BUSY
    while True:
        with _LOCK:
            while True:
                now = time.monotonic()
                ready: Optional[_Slot] = None
                wait: Optional[float] = None
                for slot in list(_PENDING):
                    every = MESSAGING_EDIT_EVERY_S if slot.messaging else EDIT_EVERY_S
                    due = slot.last_at + every if slot.handle is not None and not (slot.messaging and slot.final) else 0.0
                    if due <= now:
                        ready = slot
                        break
                    wait = (due - now) if wait is None else min(wait, due - now)
                if ready is not None:
                    _PENDING.remove(ready)
                    text, ready.text = ready.text, None
                    _BUSY = True
                    break
                _LOCK.wait(wait)
        try:
            if text is not None and not ready.dead:
                _send(ready, text)
        except Exception:  # pragma: no cover - the host's call is foreign code
            logger.debug("kame: the chat line could not be sent", exc_info=True)
            ready.dead = True
        finally:
            with _LOCK:
                ready.last_at = time.monotonic()
                _BUSY = False
                _LOCK.notify_all()


def _send(slot: _Slot, text: str) -> None:
    """Post (inside the context captured on the reply thread) or edit. ``ok == False`` ends the slot, silently."""
    if slot.handle is None:
        result = slot.run_ctx.run(slot.plugin_ctx.post_message, text)
        if result is None or getattr(result, "ok", False) is not True:
            slot.dead = True
            return
        slot.handle = result
        slot.sent_text = text
        return
    result = slot.handle.edit(text)
    if isinstance(result, dict) and result.get("ok") is False:
        slot.dead = True
        return
    slot.sent_text = text


# --- one reply: a line -------------------------------------------------------------------------------------


class _Line:
    __slots__ = ("owner", "call_id", "touched", "policy", "session", "run_ctx", "acted", "trigger", "done", "text",
                 "keys", "attempts", "slot", "began")

    def __init__(self, owner: str, call_id: int, policy: str, session: str, run_ctx: contextvars.Context) -> None:
        self.owner, self.call_id, self.policy, self.session, self.run_ctx = owner, call_id, policy, session, run_ctx
        self.touched = self.began = time.monotonic()
        self.acted = False
        #: messaging only: has a wait of more than ten seconds / another model happened?
        self.trigger = False
        self.done = False
        self.text = ""
        self.keys: set = set()
        self.attempts = 0
        self.slot: Optional[_Slot] = None


#: Surfaces with no conversation to post into (or no way to edit one): nothing is said there.
_NO_SURFACE = frozenset({"", "cli", "acp", "local", "api_server", "webhook", "cron"})


def _policy(surface: str) -> str:
    if surface in ("desktop", "tui"):
        return surface
    if surface in _NO_SURFACE:
        return "none"
    return "messaging"


def _surface(ctx: Any) -> str:
    try:
        return str(ctx.session_surface() or "").strip().lower()
    except Exception:
        return ""


def _session_key() -> str:
    """The route key of the session whose turn runs here, ``""`` outside one. 1.9.0.6 (catalog review of #159044):
    the public ``gateway.session_context.bound_session_key`` (ContextVar only, no ``os.environ`` fallback), not the
    private ``hermes_cli.plugin_session.session_key``."""
    try:
        from gateway.session_context import bound_session_key

        return str(bound_session_key() or "")
    except Exception:
        return ""


def _label(identity: Any) -> str:
    identity = str(identity or "")
    if not identity:
        return "the provider"
    return identity.split(":", 1)[1] if ":" in identity else identity


def _duration(seconds: Any) -> str:
    try:
        from .state import live_duration

        return live_duration(seconds)
    except Exception:  # pragma: no cover
        return f"{seconds}s"


def _acting_text(kind: str, text: str, fields: Dict[str, Any]) -> str:
    """The sentence for an acting event, in the words the panel and the spinner already use."""
    model = _label(fields.get("identity"))
    if kind == "wait":
        from .state import live_line

        return live_line({"kind": "waiting", "model": model, "eta_s": fields.get("seconds")})
    if kind == "stitch":
        from .state import live_line

        return live_line({"kind": "stitching", "model": model})
    if kind in ("switch", "handover", "retest"):
        if not text:
            return f"↻ {model}: trying another key"
        return f"{'↪' if 'model' in text.lower() or kind == 'handover' else '↻'} {text}"
    if kind in ("stream_drop", "storm") and text:
        return f"↻ {text}"
    return f"↻ {model}: trying another key"


def _final_text(kind: str, text: str, line: _Line, fields: Dict[str, Any]) -> str:
    if kind in _FAILED:
        return f"✕ no answer: {text}" if text else "✕ no answer"
    seconds = fields.get("seconds")
    if line.policy == "messaging":
        # 1.9.0.6 (decisions/0037 C20): one short line, no key count; the reply's own running time.
        try:
            spent = max(time.monotonic() - line.began, float(seconds or 0.0))
        except (TypeError, ValueError):
            spent = time.monotonic() - line.began
        return f"✓ KAME: answered after {max(int(round(spent)), 1)} s"
    tried = len(line.keys) or line.attempts or 1
    took = "" if seconds is None else f" in {float(seconds):.1f} s"
    return f"✓ answered by {_label(fields.get('identity'))}{took} ({tried} key{'s' if tried != 1 else ''} tried)"


def note(call: Any, kind: str, text: str = "", fields: Optional[Dict[str, Any]] = None) -> None:
    """Called beside ``transport._emit``: one event of a reply. Never raises, never blocks."""
    try:
        _note(call, str(kind), str(text or ""), fields or {})
    except Exception:  # pragma: no cover - a courtesy line must never cost a reply
        logger.debug("kame: the chat line ignored an event", exc_info=True)


def note_status(owner: str, text: str) -> None:
    """Called beside ``_Spinner.update``: the live sentence of the reply that owns that spinner line."""
    try:
        if _CTX is None or not text:
            return
        with _LOCK:
            line = _LINES.get(str(owner))
            if line is None or line.done or not line.acted:
                return
            if line.policy == "messaging":
                return  # messaging edits come from events only (rate limits), never from status ticks
            line.text = str(text)
            line.touched = time.monotonic()
        _publish(line, final=False)
    except Exception:  # pragma: no cover
        logger.debug("kame: the chat line ignored a status", exc_info=True)


def _note(call: Any, kind: str, text: str, fields: Dict[str, Any]) -> None:
    ctx = _CTX
    if ctx is None or not settings.is_on(settings.LIVE_CHAT_LINE):
        return
    if getattr(call, "channel", "main") != "main":
        return
    owner = str(call.status_owner())
    now = time.monotonic()
    created = False
    with _LOCK:
        line = _LINES.get(owner)
        if line is not None and (line.call_id != id(call) or now - line.touched > _STALE_S or line.done):
            _LINES.pop(owner, None)
            line = None
    if line is None:
        surface = _surface(ctx)
        line = _Line(owner, id(call), _policy(surface), _session_key(), contextvars.copy_context())
        created = True
        with _LOCK:
            _LINES[owner] = line
            _LINES.move_to_end(owner)
            while len(_LINES) > _MAX_TRACKED:
                _LINES.popitem(last=False)
    line.touched = now
    key = str(fields.get("key") or "")
    if kind in ("sent", "switch") and key:
        line.keys.add(key)
    if kind == "sent":
        line.attempts += 1
    if created:
        _notices(line, ctx)

    if kind in _ACTING:
        line.acted = True
        line.text = _acting_text(kind, text, fields)
        if kind == "handover" or (kind == "switch" and "model" in text.lower()):
            line.trigger = True
        if kind == "wait":
            seconds = fields.get("seconds")
            if seconds is None or float(seconds) > MESSAGING_WAIT_S:
                line.trigger = True
        if now - line.began >= MESSAGING_WAIT_S:
            line.trigger = True             # 1.9.0.6 (C20): the reply itself has waited a minute
        _publish(line, final=False)
    elif kind in _ANSWERED or kind in _FAILED:
        if line.acted or line.slot is not None:
            line.text = _final_text(kind, text, line, fields)
            _publish(line, final=True)
        with _LOCK:
            line.done = True
            if _LINES.get(owner) is line:
                del _LINES[owner]


def _publish(line: _Line, final: bool) -> None:
    """Give the line's sentence to its slot when the surface's rules say a message is wanted."""
    if line.policy not in ("tui", "messaging") or not line.text:
        return
    ctx = _CTX
    if ctx is None:
        return
    slot = line.slot
    if slot is None:
        if not line.acted or (line.policy == "messaging" and not line.trigger):
            return
        with _LOCK:
            if line.policy == "tui":
                slot = _SESSIONS.get(line.session)
                if slot is None:
                    slot = _SESSIONS[line.session] = _Slot()
                    while len(_SESSIONS) > _MAX_TRACKED:
                        _SESSIONS.popitem(last=False)
                else:
                    _SESSIONS.move_to_end(line.session)
            else:
                slot = _Slot(messaging=True)
        line.slot = slot
    if final:
        slot.final = True
    _submit(slot, line.text, ctx, line.run_ctx)


# --- notices: once per session -----------------------------------------------------------------------------


def desktop_read_age() -> Optional[float]:
    """Seconds since the Desktop panel last read ``/state`` (``dashboard/plugin_api.py`` writes the stamp).

    ``None`` only when the stamp file is MISSING (the panel never read). A stamp that cannot be read or parsed
    right now (a lock, a partial write) is *unknown*: ``nan``, which is neither fresh nor stale, so no notice is
    owed on its account. A file, not a variable: the panel's door can live in another process."""
    try:
        from .state import state_dir

        directory = state_dir()
        if directory is None:
            return None
        stamp_file = directory / DESKTOP_READ_FILE
        if not stamp_file.exists():
            return None
    except Exception:
        return float("nan")
    try:
        stamp = float(stamp_file.read_text(encoding="utf-8").strip())
        return max(0.0, time.time() - stamp)
    except FileNotFoundError:
        return None
    except Exception:
        return float("nan")


def _notices(line: _Line, ctx: Any) -> None:
    if line.policy not in ("desktop", "tui"):
        return
    wanted: List[tuple] = []
    if line.policy == "desktop":
        age = desktop_read_age()
        if age is None or age > DESKTOP_READ_FRESH_S:  # nan (unknown) is neither: no notice
            wanted.append(("desktop-off", DESKTOP_NOTICE))
    try:
        from . import provider_profiles

        wanted.extend((f"profiles:{text}", text) for text in provider_profiles.notices())
    except Exception:
        logger.debug("kame: could not read the package notices", exc_info=True)
    for notice_id, text in wanted:
        marker = (line.session, notice_id)
        with _LOCK:
            if marker in _NOTICED:
                continue
            _NOTICED[marker] = True
            while len(_NOTICED) > 256:
                _NOTICED.popitem(last=False)
        _submit(_Slot(), text, ctx, line.run_ctx)
