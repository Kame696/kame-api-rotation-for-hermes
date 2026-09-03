"""Every API call picks its own key, and a failed call rotates instead of failing.

This is the binding the plugin was missing. Everything before it acted *after*
a refusal — size the cooldown, bench the key, hand the pool a better reset time.
None of that chooses the key a request carries, and none of it survives Hermes'
own retry ceiling.

Where this attaches
-------------------

``run_agent.py`` routes every model call through two forwarders::

    def _interruptible_streaming_api_call(self, api_kwargs, *, on_first_delta=None):
        from agent.chat_completion_helpers import interruptible_streaming_api_call
        return interruptible_streaming_api_call(self, api_kwargs, on_first_delta=on_first_delta)

    def _interruptible_api_call(self, api_kwargs):
        from agent.chat_completion_helpers import interruptible_api_call
        return interruptible_api_call(self, api_kwargs)

Both import the module function **inside the method body**, so replacing the
module attribute reaches every caller in the process — the main loop, the
auxiliary lane, compression, subagents, the gateway and the CLI alike — without
touching a class, a subclass, or an instance. It is the same property that
makes ``resolve_runtime_provider`` wrappable, and it is why this plugin can own
the dispatch point without patching Hermes.

What it does per call
---------------------

1. Collect the keys this agent could use — the credential pool's entries when
   there is one, otherwise a comma-split of ``agent.api_key``.
2. Ask :mod:`core.carousel` which one is healthiest *right now*: fewest requests
   in the last 60 seconds, least recently used to break the tie.
3. Put that key on the agent (cheaply — see ``_apply_key``) and call the host's
   own function. KAME does not re-implement the request, exactly as Agent Zero
   v1.0.9 stopped doing: the streaming, the interrupts, the middleware, the
   response shape all stay Hermes'.
4. On success, clear the key's health and return the host's own result.
5. On failure, classify it, rest the key for as long as the evidence justifies,
   and **take the next key** — without the error ever reaching the conversation
   loop, which is what stops it reaching the chat.

Why the loop lives here and not in the host
-------------------------------------------

Hermes retries ``agent._api_max_retries`` times (default **3**) and rotates the
credential pool only for ``billing``, ``rate_limit`` and ``auth``
(``agent_runtime_helpers.recover_with_credential_pool``). A 503 is none of
those, so the observed failure — three 503s and a dead turn, with fourteen
untouched keys in the pool — is the host behaving exactly as designed. Raising
the retry ceiling alone would not fix it either, because the retries would all
go to the same key.

So the carousel runs *inside* one host retry. From the conversation loop's
point of view a call either succeeds or fails once; the fifteen keys it tried
in between are this module's business.

An answer cut in half is finished, not abandoned (1.1.1)
--------------------------------------------------------

Until 1.1.1 this module had one deliberate limit: **a partial stream was never
replayed**. If the provider delivered tokens and then dropped, the user had
already seen text, retrying would print the answer twice, so the failure went
back to the host — which continues it behind a synthetic
``[System: The previous response was cut off…]`` row that the user sees, that
looks like a bug, and that moves the server's message ordinal without moving
the client's.

The limit was real but it was not necessary. What made replaying unsafe was
not knowing *what had already been shown*; this wrapper does know, because it
owns the delivery path for the duration of the attempt (:class:`_Delivery`).
So 1.1.1 continues the answer instead of surrendering it:

1. the text delivered so far is kept, exactly as the user saw it;
2. the key that dropped is rested and another one is chosen;
3. the request goes out again with that text as a trailing assistant message,
   which is how both OpenAI-compatible endpoints and the Anthropic Messages
   API are asked to continue rather than to start over;
4. whatever the continuation repeats — a few words, or the entire answer from
   the top — is trimmed before it reaches the screen, by
   :mod:`core.stitch`;
5. what comes back to Hermes is one complete response, so the host never
   enters its continuation path at all.

Two cases keep the old behaviour, and both on purpose. A drop **in the middle
of a tool call** is handed back untouched: the arguments are half-written JSON,
there is no honest way to continue them, and Hermes fails that fast and
clearly. And when the resume budget is spent, the merged text goes back as a
partial stub — everything the user saw, nothing printed twice, and the host's
own machinery takes it from there.

``KAME_STREAM_STITCH_DISABLED=1`` restores the 1.1.0 behaviour exactly.

**A terminal error is still terminal**, which is a refusal to rotate rather
than a limit on rotating. A malformed request, a 404 model, a
content-policy refusal: no key answers those, and rotating through fifteen of
them would turn one clear error into fifteen slow ones. ``core.carousel.is_terminal``
draws the line, and it checks *auth before status* — Google reports an invalid
key as ``400 INVALID_ARGUMENT: API key not valid``, which every status-first
classifier reads as a permanent client error and aborts the run on. Here it
quarantines that one key and rotates, which is the difference between a dead
session and a turn that finishes on the other fourteen.

Waiting, since 1.0.1
--------------------

There is no ceiling on how long one call may spend rotating. Agent Zero's
ADR 0002 reached the same conclusion for the same reason — every artificial
timeout it tried became the cause of the failure it was meant to prevent — but
recorded one risk it could not close: a socket that hangs without ever erroring
would be waited on forever. Hermes closes it. Each attempt already carries the
host's own per-request timeout (``run_agent.py:1394``, 1800 s by default), so a
hung connection surfaces as a ``TimeoutError`` the carousel rotates on. And the
agent runs in a worker thread, so a long sleep here blocks neither the event
loop, nor the websocket, nor the stop button.

What Agent Zero does not have is the other half: ADR 0002 admits that when
every key is spent *"the user typically restarts A0"*. A restart is not a
decision the user made; it is one the silence made for them. So the wait is
unbounded **and** narrated — see :class:`_Vigil`.

Switching it off
----------------

``KAME_CAROUSEL_DISABLED=1`` (or ``carousel_disabled`` in the plugin's config
entry) removes the wrapper's effect entirely and leaves Hermes' own retry and
rotation in charge.
"""

from __future__ import annotations

import functools
import logging
import os
import random
import sys
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import host_text, runtime, settings
from .core import evidence, multikey, stitch
from .core.storm import StormFilter, Verdict as StormVerdict
from .core.events import EVENTS
from .core.carousel import (
    EMPTY_REST_S,
    DENIED_REST_S,
    REJECTED_REST_S,
    EMPTY_RETRY_BUDGET,
    ENGINE,
    Carousel,
    fingerprint,
    format_duration,
    is_auth_failure,
    is_terminal,
)
from .core.classify import classify, Verdict

logger = logging.getLogger(__name__)

#: What the storm filter would have said if it were switched off: print
#: everything, hold nothing back. A constant rather than a branch around the
#: logging block, so the switched-off path runs the same code as the other one
#: and cannot drift away from it.
_ALWAYS_LOUD = StormVerdict(speak_full=True)

_MARK = "__kame_carousel__"

#: The second parameter the wrapper reads positionally, and the one worth
#: checking. 1.0.8 tested ``len(params) < 2``, which no version of either
#: function has ever satisfied — so the guard could not fire, and a reshaped
#: host would have been wrapped anyway.
#:
#: Only the second name is pinned. The first is the agent, and "agent" is a
#: generic enough word that a rename would not mean the contract moved; the
#: second carries the request dictionary this wrapper reads and rewrites, and
#: if *that* is no longer ``api_kwargs`` then what the function wants is no
#: longer what KAME is prepared to give it. Agent Zero's 1.0.9 binds by shape
#: for the same reason: step aside whole rather than wrap a moved contract.
_EXPECTED_SECOND_PARAM = "api_kwargs"

#: What the storm filter would have said if it were switched off: print
#: everything, hold nothing back. A constant rather than a branch around the
#: logging block, so the switched-off path runs the same code as the other one
#: and cannot drift away from it.


_MODULE = "agent.chat_completion_helpers"

#: Both dispatch functions, wrapped identically. The streaming one carries an
#: ``on_first_delta`` keyword; the wrapper forwards ``**kwargs`` untouched so a
#: future Hermes adding a parameter cannot break it.
_FUNCTIONS = ("interruptible_streaming_api_call", "interruptible_api_call")

#: Raised to mean "the user pressed the button", not "the provider failed".
#: Retrying these would ignore an interrupt, so they pass straight through.
_CONTROL_FLOW = (KeyboardInterrupt, SystemExit, GeneratorExit, InterruptedError)

#: Longest single sleep while the whole pool is resting. Slept in one-second
#: slices so an interrupt is honoured within a second, and re-checked after so
#: a key that recovers early is used immediately.
_SLEEP_SLICE_S = 1.0
_MAX_SLEEP_S = 60.0

#: A wait shorter than this is a hiccup and saying so would be noise. Longer
#: than this and silence becomes indistinguishable from a hang, which is the
#: real complaint behind "is it frozen?" — so the first notice goes out here.
#: Hermes' own give-up threshold, for the message only -- the real number lives
#: in ``HERMES_STREAM_STALE_GIVEUP`` and KAME never reads it to decide anything.
_HOST_BREAKER_THRESHOLD = 5

#: The kinds a same-answer pool is NOT allowed to promote to terminal. Every one
#: of them describes something that passes on its own: an outage ends, a
#: throttle expires, a quota rolls over, a socket that timed out once answers
#: the next time. Fifteen keys agreeing that the provider is down is fifteen
#: keys being right, not evidence about the request -- and promoting it would
#: throw away the single behaviour this plugin exists for.
#: The refusals that are about the *credential*, as opposed to about the
#: pairing of a credential with one model. Only these earn the "replace this
#: key" sentence; ``denied`` gets its own, because replacing the key is not
#: what fixes it.
_CREDENTIAL_REFUSALS = frozenset({"auth", "revoked"})

_NEVER_PROMOTED = frozenset(
    {"server", "timeout", "per_minute", "daily", "insufficient_quota", "auth",
     # 1.6.0.1. A pool where every key is a key the provider named dead is a
     # pool that needs new keys, not a request that is malformed. Promoting it
     # would end the turn with the wrong sentence on screen.
     "revoked",
     # Same round, same reasoning, and it is here to *preserve* behaviour
     # rather than change it: a denial used to arrive as ``auth`` and was
     # covered by the entry above. Now that it arrives under its own name it
     # needs its own entry, or a pool where no key may use one model would
     # end the turn instead of letting Hermes try another model.
     "denied"}
)

VIGIL_FIRST_S = 90.0

#: And then at this interval, so an hour-long wait produces a handful of lines
#: rather than sixty. Each one carries the current estimate, which moves as
#: keys recover, so a repeat is information rather than a reminder.
VIGIL_REPEAT_S = 600.0


class Incompatible(Exception):
    """The installed Hermes does not present the surface this module needs."""


# --- reading the agent ------------------------------------------------------


def _entries_of(agent: Any) -> List[Any]:
    """The credential pool's entries, or an empty list.

    Entries are preferred over raw strings because they carry a base URL and an
    id, which is what ``_apply_key`` needs to swap a key correctly on the
    Anthropic path. Everything is guarded: this runs on the hot path of every
    API call, and no read of an arbitrary host object is allowed to cost a turn.
    """
    pool = getattr(agent, "_credential_pool", None)
    if pool is None:
        return []
    try:
        entries = list(pool.entries())
    except Exception:
        return []
    usable = []
    for entry in entries:
        try:
            key = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", "")
        except Exception:
            continue
        if str(key or "").strip():
            usable.append(entry)
    return usable


def _key_of(entry: Any) -> str:
    # Shared with ``pool_binding``, which fingerprints the same value on the
    # other side of the call to ask whether the bench is blaming the key that
    # actually went out. See ``core.multikey.key_on``.
    return multikey.key_on(entry)


def candidates(agent: Any) -> Tuple[List[str], Dict[str, Any]]:
    """``(keys, entry_by_key)`` — everything this agent is allowed to send.

    Three sources, in order of authority:

    * the credential pool, which is where a Hermes that was configured through
      the UI keeps them;
    * a comma-separated ``agent.api_key``, which is where a Hermes configured
      through an environment variable keeps them, and which the resolver
      binding has already reduced to one key for the *first* call of a session
      but not for the rest;
    * the single resolved key, which is not a pool at all — the carousel still
      earns its keep there, because backoff and eternal retry apply to one key
      just as they do to fifteen.
    """
    entry_by_key: Dict[str, Any] = {}
    keys: List[str] = []

    def _add(raw: str, entry: Any) -> None:
        # One raw string may hold several keys. The parts are what the
        # provider accepts; the whole — a comma-joined list — is the malformed
        # credential whose failure is what this binding exists to hide. When
        # the value holds one key, or the split cannot parse the value, the
        # raw string itself is kept; no callers of candidates() ever see a
        # comma.
        split, _rejected = multikey.split_value(raw)
        parts = split if len(split) > 1 else ([raw] if raw else [])
        for part in parts:
            if part not in entry_by_key:
                entry_by_key[part] = entry
                keys.append(part)

    for entry in _entries_of(agent):
        key = _key_of(entry)
        if key:
            _add(key, entry)

    current = str(getattr(agent, "api_key", "") or "").strip()
    if not keys:
        _add(current, None)
    elif current and current not in entry_by_key:
        # The agent is carrying a key the pool does not know — a resolver
        # substitution, a fallback provider, a manually set key. It is a
        # working credential and dropping it would narrow what the host would
        # have sent, which this plugin never does.
        _add(current, None)
    return keys, entry_by_key


def _apply_key(agent: Any, key: str, entry: Any) -> bool:
    """Put ``key`` on the agent for the next request. ``True`` if it took.

    The cheap path is the one Hermes itself uses when it rewrites a key
    mid-loop (``conversation_loop.py``, the non-ASCII sanitiser): assign
    ``api_key``, keep ``_client_kwargs`` in step, and update the live client,
    which reads its own copy when it builds auth headers on every request. No
    client is rebuilt, because every key in a pool shares the provider's base
    URL — rebuilding one client per call would cost more than the rotation
    saves.

    The Anthropic path cannot do that: ``build_anthropic_client`` bakes the key
    into the client at construction. There, and only there, the host's own
    ``_swap_credential`` is used, and only when a pool entry is available to
    hand it. Rotating on that path costs a client rebuild; not rotating would
    be a silent lie about what the plugin does.
    """
    if not key:
        return False
    if str(getattr(agent, "api_key", "") or "") == key:
        # Already carrying it — nothing to write, but the attribution still
        # has to be right. If this call does fail into the host, Hermes reads
        # ``_credential_pool_entry_id`` to decide which entry to bench, and a
        # stale id from a previous rotation would bench a healthy key.
        _attribute(agent, entry)
        return True

    if getattr(agent, "api_mode", "") == "anthropic_messages":
        swap = getattr(agent, "_swap_credential", None)
        if entry is None or not callable(swap):
            return False
        # 1.2.2. ``candidates`` splits a comma-joined row into the keys it
        # holds, and each part is mapped back to the row it came from — which
        # means the entry handed here can be the *list*, not the key. On every
        # other path that does not matter, because the key is written onto the
        # agent directly. Here the entry is the credential: swapping it would
        # send the whole joined list, which is precisely the malformed
        # credential this release exists to stop. Leave the host's own key in
        # place instead and say why once.
        if _key_of(entry) != key:
            logger.debug(
                "kame: not swapping an Anthropic credential whose stored value "
                "holds several keys — the host bakes the key into the client, "
                "so only the pool's own split can rotate on this path"
            )
            return False
        try:
            swap(entry)
            return True
        except Exception:
            logger.debug("kame: could not swap the Anthropic credential", exc_info=True)
            return False

    try:
        agent.api_key = key
        client_kwargs = getattr(agent, "_client_kwargs", None)
        if isinstance(client_kwargs, dict):
            client_kwargs["api_key"] = key
        client = getattr(agent, "client", None)
        if client is not None and hasattr(client, "api_key"):
            client.api_key = key
        _attribute(agent, entry)
        return True
    except Exception:
        logger.debug("kame: could not place the selected key on the agent", exc_info=True)
        return False


def _attribute(agent: Any, entry: Any) -> None:
    """Tell the host which pool entry sent this request.

    Hermes reads ``_credential_pool_entry_id`` when it benches a credential
    (``agent_runtime_helpers.recover_with_credential_pool``). Leaving it
    pointing at the previous rotation's entry would bench a healthy key for a
    failure it never had — the exact mis-attribution the host's own code goes
    out of its way to avoid.
    """
    if entry is None:
        return
    try:
        entry_id = getattr(entry, "id", None)
        if isinstance(entry_id, str) and entry_id:
            agent._credential_pool_entry_id = entry_id
    except Exception:  # pragma: no cover — a plain attribute write
        pass


# --- watching the stream ----------------------------------------------------


class _Progress:
    """Flipped the instant anything reaches the user, and tracks activity timestamp.

    The wrapper cannot count tokens — it does not own the stream any more than
    Agent Zero's v1.0.9 carousel does. It watches the callbacks instead, and
    every shim returns whatever the real callback returned, because Hermes uses
    those return values to control early stopping.
    """

    __slots__ = ("any", "last_activity", "completed")

    def __init__(self) -> None:
        self.any = False
        self.last_activity = time.monotonic()
        self.completed = False

    def touch(self) -> None:
        self.any = True
        self.last_activity = time.monotonic()


def _shim(progress: _Progress, callback: Any) -> Any:
    if not callable(callback):
        return callback

    @functools.wraps(callback)
    def _watched(*args, **kwargs):
        progress.touch()
        return callback(*args, **kwargs)

    return _watched


class _Spinner:
    """Live status through ``thinking.delta`` — safe for ordinals.

    The Hermes spinner text is the natural place to show what KAME is doing,
    because it is exactly where the user looks when the agent is "waiting".
    ``_emit_wait_notice`` routes through ``thinking_callback`` to the
    ``thinking.delta`` gateway event, which is a transient status line — it
    does not create a message and does not affect message ordinals, so it
    cannot break rewind, edit, or resend. That property is why the live status
    lives here and not in a chat message: the one thing this release must not
    do is add rows to a history the client is already counting differently.

    Two guards keep it quiet when quiet is right:

    * **Diff** — the same text twice in a row is not re-sent, so a settled
      wait produces no traffic at all. The line already on screen persists, so
      "always visible" costs nothing to maintain — with one exception, which
      is what ``_REFRESH_S`` is for: the spinner is shared, and Hermes writes
      its own activity into it ("Exploring 6 files"). Once it has, KAME's line
      is gone from the screen while ``_last_text`` still says it was shown, and
      a strict diff gate would never put it back. So an unchanged line is
      allowed through again after half a minute. Two frames a minute at the
      very worst, and the answer to "is it still rotating?" stops depending on
      whether the host happened to overwrite it.
    * **Interval** — at least ``interval`` seconds between updates, chosen by
      the caller through :meth:`cadence_for` rather than fixed. A countdown in
      its last ten seconds is worth a tick a second; the same countdown an hour
      from now is not, and a fixed one-second tick would put thousands of frames
      on the websocket to redraw a number nobody is watching yet.

    Both guards are held under a lock, and both are kept **per session**. One
    Hermes process serves several conversations at once — the gateway's
    sessions, the auxiliary lane, every subagent — all through the one binding.
    Without the lock two lanes interleave a read and a write and one loses its
    update; without the per-session key they share a throttle, and a line one
    conversation never showed is suppressed in it as a duplicate of a line
    another conversation did.
    """

    _DEFAULT_INTERVAL_S = 10.0

    #: How long an unchanged line may stay suppressed by the diff gate before
    #: it is drawn again. Not a cadence — the interval below still applies — a
    #: ceiling on how long KAME's line can be missing from a spinner the host
    #: also writes to.
    _REFRESH_S = 30.0

    #: How many sessions are remembered before the oldest is forgotten. One
    #: entry is two small values, so the number only exists to keep a long-lived
    #: gateway from accumulating a row per session forever. Forgetting a session
    #: costs at most one extra redraw the next time it says something.
    _MAX_TRACKED = 64

    # State shared across calls, but **not** across sessions. It has to survive
    # the boundary between calls, because that is exactly where the flooding
    # used to happen — and it must not be a single pair of values, because one
    # Hermes process serves several conversations at once (the gateway's
    # sessions, the auxiliary lane, every subagent). With one pair, a session
    # rotating and a session sitting idle take turns overwriting each other's
    # throttle, and each one suppresses the other's line as a duplicate of a
    # sentence it never showed. Keyed per session, they cannot see each other.
    _lock = threading.Lock()
    _state: "OrderedDict[str, tuple]" = OrderedDict()

    @staticmethod
    def cadence_for(eta: Optional[float]) -> float:
        """How often a countdown showing ``eta`` deserves to be redrawn.

        The number on screen changes by a minute at a time when the wait is
        long, so redrawing it every second would send the same text sixty
        times. Near zero it changes every second and the user is watching.
        """
        if eta is None:
            return _Spinner._DEFAULT_INTERVAL_S
        if eta <= 10.0:
            return 1.0
        if eta <= 60.0:
            return 5.0
        return 30.0

    @staticmethod
    def key_for(agent: Any) -> str:
        """Which conversation this update belongs to.

        ``session_id`` is what Hermes itself uses to tell one conversation from
        another, so it is what the throttle is keyed on. An agent without one —
        the auxiliary lane, a bare object in a test — falls back to its own
        identity, which is still per-lane and therefore still correct; it is
        only less stable across a restart, and nothing here outlives one.
        """
        session = getattr(agent, "session_id", None)
        if session:
            return str(session)
        return f"anon:{id(agent):x}"

    @classmethod
    def reset(cls, session_id: Optional[str] = None) -> None:
        """Forget what was last shown. Called on a session reset.

        With a ``session_id``, only that conversation is forgotten — a reset in
        one chat must not make another chat redraw. Without one, everything is,
        which is what a test wants and what an unidentified reset has to settle
        for.
        """
        with cls._lock:
            if session_id is None:
                cls._state.clear()
            else:
                cls._state.pop(str(session_id), None)

    @classmethod
    def update(cls, agent: Any, text: str, *, interval: Optional[float] = None) -> None:
        if settings.is_on(settings.LIVE_STATUS_DISABLED):
            return
        every = cls._DEFAULT_INTERVAL_S if interval is None else interval
        now = time.monotonic()
        key = cls.key_for(agent)
        with cls._lock:
            last_say, last_text = cls._state.get(key, (0.0, ""))
            if text == last_text and now - last_say < cls._REFRESH_S:
                return
            if now - last_say < every:
                return
            cls._state[key] = (now, text)
            cls._state.move_to_end(key)
            while len(cls._state) > cls._MAX_TRACKED:
                cls._state.popitem(last=False)
        # Emitted outside the lock on purpose. ``_emit_wait_notice`` calls into
        # the host's own callback, and holding a lock across foreign code is how
        # one slow lane stops every other one.
        notice = getattr(agent, "_emit_wait_notice", None)
        if callable(notice):
            try:
                notice(text)
            except Exception:
                pass


#: Hermes Desktop does not show every wait notice it receives. The renderer
#: runs each ``thinking.delta`` through ``providerWaitText``
#: (``apps/desktop/src/store/provider-wait.ts``), which keeps the text only if
#: it matches
#:
#:     /^(?:⏳|⚠|↻)\s*(?:waiting on|no (?:output|response)|model returned)/i
#:
#: and — this is the part that cost v1.0.9 its status line — sets the row to
#: the empty string for anything else. So a line that does not match is not
#: merely ignored: it *erases* whatever the core had put there, and the user is
#: left with the bare elapsed-seconds timer that started this whole complaint.
#: The core's own notice (``agent/chat_completion_helpers.py:1631``) reads
#: ``⏳ waiting on <model> — <n>s with no response yet (…)``. KAME says its
#: piece in the same shape, for the same row, so the two can take turns without
#: one blanking the other.
STATUS_SYMBOLS = ("⏳", "⚠", "↻")
STATUS_OPENERS = ("waiting on", "no output", "no response", "model returned")

_STATUS_GATE = re.compile(
    r"^(?:⏳|⚠|↻)\s*(?:waiting on|no (?:output|response)|model returned)",
    re.IGNORECASE,
)


def passes_desktop_status_gate(text: str) -> bool:
    """Would Hermes Desktop show this line, or silently blank the row?

    A copy of the host's own test rather than a paraphrase of it, so the
    tripwire in ``tools/host_assumptions.py`` can check the two against each
    other and say so the day the host's regex moves.
    """
    return bool(_STATUS_GATE.match(text or ""))


def model_label(identity: str) -> str:
    """``google:gemini-2.5-pro`` reads as ``gemini-2.5-pro`` on the status row.

    The provider is already visible in Hermes' own chrome, and the row is one
    line shared with an elapsed timer; the model is the half that tells the
    user which wait this is.
    """
    if not identity:
        return "the provider"
    return identity.split(":", 1)[1] if ":" in identity else identity


def _publish(binding: Any, activity: Optional[Dict[str, Any]] = None) -> None:
    """Mirror the live state into the file the Desktop chip reads.

    Separate from ``_Spinner.update`` on purpose. The spinner belongs to a
    turn: it needs an ``agent`` to write through, it is throttled for a
    websocket, and it disappears the moment the turn ends. The chip is app
    chrome — it is on screen when no turn exists at all, which is exactly the
    moment the user asked to be able to tell "still working" from "frozen".

    Never raises: a status readout that can end a chat turn is not a status
    readout, it is a bug with a nice icon.
    """
    try:
        from . import state

        state.publish(binding, activity=activity)
    except Exception:  # pragma: no cover - a readout must not be able to throw
        logger.debug("kame: could not publish the desktop snapshot", exc_info=True)


def status_line(
    healthy: int,
    total: int,
    tail: str = "",
    *,
    subject: str = "the provider",
    symbol: str = "⏳",
    opener: str = "waiting on",
) -> str:
    """The one sentence KAME says about itself, in one place.

    Shaped to pass :func:`passes_desktop_status_gate` — see the note above the
    gate for what happens to a line that does not. Within that constraint it
    keeps what Agent Zero's spinner says, ``KAME`` and ``X/Y healthy``, because
    two ports of one plugin that describe themselves differently are two
    plugins to the person reading the screen.
    """
    head = f"{symbol} {opener} {subject}".rstrip()
    line = f"{head} — KAME {healthy}/{total} keys healthy"
    return f"{line}, {tail}" if tail else line


def recovery_clock(eta: Optional[float]) -> str:
    """``01:23 (around 14:32:07)`` — the wait, and when it ends by the wall clock.

    Ported from Agent Zero, which learned it the same way: a duration answers
    "how long" and a clock time answers "can I go and do something else", and
    during a daily quota the second question is the one being asked.
    """
    if eta is None:
        return "unknown"
    label = format_duration(eta)
    if eta < 60.0:
        return label
    return f"{label} (around {time.strftime('%H:%M:%S', time.localtime(time.time() + eta))})"


def _install_shims(agent: Any, progress: _Progress, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap every delivery callback for one attempt; returns what to restore."""
    restore: Dict[str, Any] = {}
    for name in ("stream_delta_callback", "_stream_callback", "thinking_callback"):
        original = getattr(agent, name, None)
        if callable(original):
            restore[name] = original
            try:
                setattr(agent, name, _shim(progress, original))
            except Exception:
                restore.pop(name, None)
    if callable(kwargs.get("on_first_delta")):
        kwargs["on_first_delta"] = _shim(progress, kwargs["on_first_delta"])
    return restore


def _remove_shims(agent: Any, restore: Dict[str, Any]) -> None:
    for name, original in restore.items():
        try:
            setattr(agent, name, original)
        except Exception:  # pragma: no cover — the setattr that worked once
            pass


class _Vigil:
    """Tells the user a long wait is a wait, not a freeze.

    Agent Zero's carousel waits without a ceiling and without a word; ADR 0002
    records the consequence honestly — *"the user typically restarts A0 in that
    scenario"*. Restarting is not a decision the user made, it is one the
    silence made for them. A pool that hits its daily cap at 14:00 does not
    recover until the daily quota rolls over, and hours of a spinner is
    indistinguishable from a hang no matter how correct the waiting is.

    So the wait stays unbounded and stops being silent. ``agent._emit_status``
    is the host's own lifecycle channel (``run_agent.py:964``): CLI users see
    it printed with ``force=True``, gateway users receive it through
    ``status_callback("lifecycle", ...)``, and it is documented never to raise
    — "exceptions are swallowed so it cannot interrupt the retry/fallback
    logic". Exactly the guarantee a notice from inside the carousel needs.
    """

    __slots__ = ("agent", "label", "started", "_next", "notices")

    def __init__(self, agent: Any, label: str) -> None:
        self.agent = agent
        self.label = label
        self.started = time.monotonic()
        self._next = VIGIL_FIRST_S
        self.notices = 0

    @property
    def waited(self) -> float:
        return time.monotonic() - self.started

    def maybe_speak(self, healthy: int, total: int, eta: Optional[float]) -> None:
        """Emit a notice if this wait has now been going on long enough."""
        waited = self.waited
        if waited < self._next:
            return
        self._next = waited + VIGIL_REPEAT_S
        self.notices += 1
        if eta is None:
            when = "waiting for the next opening"
        else:
            when = f"next key in {format_duration(eta)}"
        resting = max(total - healthy, 0)
        self._emit(
            f"KAME: {self.label} — {resting} of {total} key(s) resting, {when}. "
            f"Waiting {format_duration(waited)} so far; no requests are being "
            f"sent. Press stop to cancel."
        )

    def done(self) -> None:
        """Close the loop, but only if the user was told it was open."""
        if not self.notices:
            return
        self._emit(
            f"KAME: {self.label} — back up after {format_duration(self.waited)}."
        )

    def _emit(self, message: str) -> None:
        emit = getattr(self.agent, "_emit_status", None)
        if not callable(emit):
            logger.info("kame: %s", message)
            return
        try:
            emit(message)
        except Exception:
            # Documented not to raise; if a host ever changes that, a notice
            # must not be the thing that ends a turn that was recovering.
            logger.debug("kame: could not emit a waiting notice", exc_info=True)


def _interrupted(agent: Any) -> bool:
    try:
        return bool(getattr(agent, "_interrupt_requested", False))
    except Exception:
        return False


def _result_is_empty(result: Any) -> bool:
    """Whether the host's answer carried no content at all.

    A squeezed free-tier key returns 200 and nothing rather than refusing. The
    first such answer from a key is treated as a provider hiccup and costs the
    key nothing; the rule for the second one lives in the carousel loop.
    """
    if result is None:
        return True
    try:
        choices = getattr(result, "choices", None)
        if choices:
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None) if message is not None else None
            tool_calls = getattr(message, "tool_calls", None) if message is not None else None
            if tool_calls:
                return False
            return not str(content or "").strip()
        content = getattr(result, "content", None)
        if isinstance(content, str):
            return not content.strip()
        if isinstance(content, list):
            return not any(
                str(getattr(part, "text", "") or "").strip() for part in content
            )
    except Exception:
        return False
    return False


# --- continuing an answer that was cut in half ------------------------------

#: The id Hermes stamps on the response it builds when a stream ends without a
#: ``finish_reason`` after delivering something (``hermes_constants.py``). It
#: is a *return value*, not an exception, which is why every version before
#: 1.1.1 saw a mid-stream drop as a successful call: the ``except`` branch this
#: module watches was never entered.
PARTIAL_STUB_ID = "partial-stream-stub"

#: How long the key that dropped rests before it is eligible again. Short,
#: because a dropped stream is weak evidence — one bad connection, not a spent
#: quota — and long enough that the very next attempt goes to a different key,
#: which is the entire point of resting it.
DROP_REST_S = 30.0


def _rest_unless_it_is_the_only_one(
    engine: Any,
    identity: str,
    keys: Sequence[str],
    key: str,
    seconds: float,
    kind: str,
) -> float:
    """Rest a key only while there is another key to send the next request to.

    KAME's cooldowns come in two kinds and 1.1.3 is the version that stopped
    treating them alike. Some are the provider's own words — a ``Retry-After``,
    a daily quota, an auth refusal — and those bind whatever else is in the
    pool: sending again immediately would only collect the same refusal. The
    others exist for exactly one reason, which is to make the *next* selection
    pick a different key.

    A cooldown of the second kind is meaningless when this is the only key that
    is well. There is nowhere to route to, and the carousel's answer to a pool
    with nothing usable in it is to wait for one — so a single-key pool sat out
    :data:`DROP_REST_S` seconds before continuing an answer it could have
    continued at once. That is a rest that buys nothing and costs half a
    minute, on the install least able to afford it.

    The failure is still recorded either way: ``failures``, ``last_sick_at``
    and ``kind`` are written on both paths, so the key's history is the same
    and only the sentence is dropped. Returns the cooldown actually applied,
    which is ``0.0`` when this was the last key standing.
    """
    if engine.healthy_count(identity, keys) > 1:
        return engine.mark(identity, key, False, seconds, kind)
    engine.mark(identity, key, False, 0.0, kind)
    return 0.0


def _partial_text(result: Any) -> Optional[str]:
    """The text a mid-stream drop delivered, or ``None`` if this is not one.

    ``None`` for anything that can not be honestly continued: a normal
    response, and — the case worth naming — a drop that happened while a tool
    call's arguments were still being written. Hermes tags that one with
    ``_dropped_tool_names``, and half-written JSON arguments are not something
    a second model call can be asked to finish.
    """
    if getattr(result, "id", "") != PARTIAL_STUB_ID:
        return None
    if getattr(result, "_dropped_tool_names", None):
        return None
    try:
        choices = getattr(result, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        if message is None or getattr(message, "tool_calls", None):
            return None
        return str(getattr(message, "content", "") or "")
    except Exception:  # pragma: no cover — a response shape nobody has seen
        return None


def _with_content(result: Any, text: str) -> Any:
    """The same response, carrying the whole answer instead of half of it.

    Written in place when the object allows it, which it does for the
    ``SimpleNamespace`` Hermes builds for streaming responses. A provider SDK
    model that refuses the assignment is rebuilt into the shape the rest of the
    loop reads, rather than returned with the wrong text in it.
    """
    try:
        choices = getattr(result, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        if message is None:
            return result
        try:
            message.content = text
            return result
        except Exception:
            from types import SimpleNamespace

            rebuilt_message = SimpleNamespace(
                role=getattr(message, "role", "assistant"),
                content=text,
                tool_calls=getattr(message, "tool_calls", None),
                reasoning_content=getattr(message, "reasoning_content", None),
            )
            rebuilt_choice = SimpleNamespace(
                index=getattr(choices[0], "index", 0),
                message=rebuilt_message,
                finish_reason=getattr(choices[0], "finish_reason", "stop"),
            )
            return SimpleNamespace(
                id=getattr(result, "id", ""),
                model=getattr(result, "model", ""),
                choices=[rebuilt_choice],
                usage=getattr(result, "usage", None),
            )
    except Exception:  # pragma: no cover — a response shape nobody has seen
        return result


class _Delivery:
    """Owns the one funnel every text delta passes through, for one attempt.

    Hermes fires visible text at ``agent._fire_stream_delta`` — from the
    streaming loop, from the SSE-buffering flush, and from the tail flush at
    the end of a turn. Wrapping that single method rather than the two
    callbacks under it matters for a reason that only shows up in the output:
    ``_fire_stream_delta`` calls *both* callbacks with the same string, so a
    shim installed on each would see every delta twice and a stitcher behind it
    would trim the same text twice.

    It does three things, in this order:

    * marks progress, so the carousel knows something reached the user;
    * remembers what was delivered, which is what makes a continuation
      possible at all — the alternative is guessing from the response object,
      and the response object is what was *received*, not what was shown;
    * on a resumed attempt, passes each delta through the stitcher and
      forwards only the part that is not a repeat.

    Text that is entirely a repeat is dropped here and never reaches the host's
    method, so Hermes' own accumulation of "what the user has been shown" stays
    exactly in step with the screen.
    """

    __slots__ = ("progress", "stitcher", "text", "_fire")

    def __init__(self, progress: _Progress, fire: Callable, stitcher: Any = None) -> None:
        self.progress = progress
        self.stitcher = stitcher
        self._fire = fire
        #: Everything this attempt actually put on screen.
        self.text = ""

    def __call__(self, text: Any) -> Any:
        self.progress.touch()
        if not isinstance(text, str) or not text:
            return self._fire(text)
        if self.stitcher is not None:
            text = self.stitcher.feed(text)
            if not text:
                return None
        self.text += text
        return self._fire(text)

    def finish(self) -> str:
        """Release anything the stitcher was still holding. Returns what was shown.

        A continuation shorter than the stitcher's probe never fills the
        buffer, so without this the last few words of a resumed answer would
        be held for ever — the exact failure the feature exists to prevent,
        arrived at from the other side.
        """
        if self.stitcher is not None:
            tail = self.stitcher.flush()
            if tail:
                self.text += tail
                self.show(tail)
        return self.text

    def show(self, text: str) -> None:
        """Put text on screen outside the stream. Never raises."""
        if not text:
            return
        try:
            self._fire(text)
        except Exception:  # pragma: no cover — the host swallows its own
            logger.debug("kame: could not deliver stitched text", exc_info=True)


def _install_delivery(
    agent: Any, progress: _Progress, stitcher: Any = None
) -> Tuple[_Delivery, Optional[Callable]]:
    """``(delivery, what_to_restore)`` for one attempt.

    A host with no ``_fire_stream_delta`` gets a delivery object that is never
    called: the carousel keeps working, ``seen`` stays empty, and stitching
    never triggers — the 1.1.0 behaviour, reached by noticing rather than by
    assuming.
    """
    original = getattr(agent, "_fire_stream_delta", None)
    if not callable(original):
        return _Delivery(progress, lambda text: None, None), None
    delivery = _Delivery(progress, original, stitcher)
    try:
        agent._fire_stream_delta = delivery
    except Exception:
        return _Delivery(progress, lambda text: None, None), None
    return delivery, original


def _remove_delivery(agent: Any, original: Optional[Callable]) -> None:
    if original is None:
        return
    try:
        # Deleted rather than reassigned, so the class's own method is exposed
        # again and the agent carries no instance attribute it did not have
        # before this call.
        delattr(agent, "_fire_stream_delta")
    except Exception:
        try:
            agent._fire_stream_delta = original
        except Exception:  # pragma: no cover — the setattr that worked once
            pass


def _contribution(delivery: _Delivery, seen: str, content: str, stitching: bool) -> str:
    """What this attempt added to the answer, reconciled against what was shown.

    The stream and the final response should carry the same text, and in every
    provider path Hermes has they do — the content it accumulates is the same
    string it fires. ``should`` is not ``does``: a provider that returns text
    it never streamed would otherwise lose that text from the screen, so the
    difference is put on screen here rather than left to be noticed.
    """
    shown = delivery.text
    if not stitching:
        return content or shown
    if not content:
        return shown
    want = stitch.stitch_text(seen, content)
    if want and want.startswith(shown) and len(want) > len(shown):
        delivery.show(want[len(shown) :])
        return want
    return shown or want


#: Identities whose provider refused a request ending in an assistant turn.
#: Learned from the refusal itself and kept for the life of the process, so the
#:400 is paid once rather than on every cut answer.
_NO_PREFILL: set = set()

def _prefill_refused(identity: str) -> bool:
    """Whether this provider needs the continuation to end in a user turn.

    Answered from what a provider has actually said, never from its name. A
    list of "providers that refuse prefills" would be wrong the first time a
    gateway put a different provider behind the same name, and wrong again the
    day one of them starts accepting it — and this plugin has been down that
    road once already, with the provider allowlist 0.0.3 took back out.
    """
    return identity in _NO_PREFILL


def _resume_kwargs(api_kwargs: Any, seen: str, trailing_user: bool = False) -> Optional[Dict[str, Any]]:
    """The same request, asking the model to continue the answer it lost.

    Always built from the *original* request rather than from the previous
    resume, so a third attempt carries one trailing assistant message and not
    three.
    """
    messages = stitch.resumable(api_kwargs)
    if messages is None:
        return None
    resumed = dict(api_kwargs)
    resumed["messages"] = list(messages) + stitch.continuation(
        seen, trailing_user=trailing_user
    )
    return resumed


def _looks_local(agent: Any) -> bool:
    """Whether this agent points at a model running on the same machine.

    Only asked in one place — the stream-silence timeout — and only to stay out
    of the way. Hermes raises the read timeout for local endpoints because a
    large context can take minutes to prefill before the first token, and a
    silence timeout that fires during that would make a local model unusable.
    """
    try:
        base = str(getattr(agent, "base_url", "") or "").lower()
    except Exception:
        return False
    return any(mark in base for mark in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal"))


# Serialises os.environ access across concurrent calls. os.environ is
# process-wide; without this two tasks could stomp each other's timeout value
# or restore the wrong previous value. The lock is held only for the duration
# of one API call, which is already bounded by HERMES_STREAM_READ_TIMEOUT.
_SILENCE_TIMEOUT_LOCK = threading.Lock()


class _SilenceTimeout:
    """Hermes' own stream read timeout, lowered for one attempt and put back.

    The setting behind this is off by default, and the mechanism is worth being
    careful about: a plugin that changes a *host* variable and forgets to say
    so is how 1.0.9 shipped a retry knob nobody asked for, which 1.0.10 had to
    take back out. So the rules are narrow.

    * Nothing happens unless the user set ``stream_silence_timeout_seconds``.
    * Nothing happens if they have set ``HERMES_STREAM_READ_TIMEOUT``
      themselves — their number is an explicit instruction to the host and
      this plugin does not overrule it.
    * Nothing happens for a local endpoint, where Hermes raises this same
      timeout on purpose and lowering it would break long prefills.
    * The variable is restored when the attempt ends, whatever ended it.

    The host reads it inside the call (``chat_completion_helpers``:
    ``env_float("HERMES_STREAM_READ_TIMEOUT", 120.0)``), which is what makes a
    scoped change possible at all; ``tools/host_assumptions.py`` checks that it
    still does.
    """

    VARIABLE = "HERMES_STREAM_READ_TIMEOUT"

    __slots__ = ("_seconds", "_previous", "_applied")

    def __init__(self, agent: Any, attempt: int = 1) -> None:
        self._seconds = 0.0
        self._previous: Optional[str] = None
        self._applied = False
        seconds = settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0)
        if seconds <= 0 or _looks_local(agent):
            return
        if os.environ.get(self.VARIABLE) is not None:
            return
        # 1.2.5: adaptive storm timeout — after two timeouts the provider is
        # proven slow, so the remaining keys get a shorter leash.
        if attempt >= 3:
            seconds = max(5.0, seconds * 0.25)
        self._seconds = seconds

    def __enter__(self) -> "_SilenceTimeout":
        if self._seconds <= 0:
            return self
        with _SILENCE_TIMEOUT_LOCK:
            self._previous = os.environ.get(self.VARIABLE)
            os.environ[self.VARIABLE] = f"{self._seconds:g}"
        self._applied = True
        return self

    def __exit__(self, *_exc: Any) -> None:
        if not self._applied:
            return
        with _SILENCE_TIMEOUT_LOCK:
            if self._previous is None:
                os.environ.pop(self.VARIABLE, None)
            else:
                os.environ[self.VARIABLE] = self._previous
        self._applied = False


# --- the binding ------------------------------------------------------------


def _clear_host_stale_streak(agent: Any) -> None:
    """Tell Hermes this is a different key, the way a provider swap would.

    ``agent._consecutive_stale_streams`` drives
    ``chat_completion_helpers._check_stale_giveup``, which past
    ``HERMES_STREAM_STALE_GIVEUP`` (5) raises **before making any network
    attempt**. Hermes clears the counter itself on ``switch_model`` /
    ``try_activate_fallback`` / ``restore_primary_runtime``, and its own comment
    gives the reason: "the streak measured the OLD provider". A rotation is the
    same event -- the streak measured the key being left behind -- and Hermes
    cannot see it happen.

    Without this, five silent streams anywhere in a session arm a breaker that
    then refuses every key in the pool instantly and for free, so the carousel
    spins at zero cost per lap with no network traffic at all to show for it.
    """
    try:
        if getattr(agent, "_consecutive_stale_streams", 0):
            agent._consecutive_stale_streams = 0
    except Exception:  # pragma: no cover — a plain attribute write
        pass


class DispatchBinding:
    """Installs, owns, and can fully remove the per-call carousel."""

    def __init__(
        self,
        *,
        engine: Optional[Carousel] = None,
        sleep: Optional[Callable[[float], None]] = None,
        jitter: Optional[Callable[[], float]] = None,
    ) -> None:
        # ``sleep`` is injectable for one reason: since 1.0.1 a wait can last
        # hours, and a test that proves an hour-long wait behaves must not take
        # an hour. Nothing in the shipped path passes it.
        self._sleep = sleep or time.sleep
        # ``jitter`` adds a small random delay to each recovery wait to avoid
        # anti-bot detection and multi-client sync collisions (Agent Zero
        # parity). Injectable so tests can pin it; ``None`` means no jitter,
        # which is the safe default — the carousel's correctness never
        # depends on it.
        self._jitter = jitter or (lambda: 0.0)
        self._module: Any = None
        self._originals: Dict[str, Callable] = {}
        self.installed = False
        self.reason = "not installed"
        self.engine = engine or ENGINE
        # For ``/kame-quota``: an install that has never rotated and one that
        # is silently inert are otherwise indistinguishable.
        self.calls = 0
        self.rotations = 0
        self.recovered = 0
        self.surfaced = 0
        # Since 1.0.1 the carousel can wait for hours, so how much of a turn
        # was spent waiting is the number that explains a slow session.
        self.waited_s = 0.0
        self.waits = 0
        # Since 1.0.9. Every one of these makes Hermes append a synthetic
        # continuation row the client never renders, which is what makes the
        # client's ordinal disagree with the server's and makes rewind and
        # edit refuse later in the same session. KAME cannot fix that
        # arithmetic -- it is the host's -- but it is the only thing in the
        # process that can see the cause happen, so it counts them and
        # ``/kame`` says so.
        self.mid_stream_cuts = 0
        # Since 1.1.1, and the three numbers that describe the feature above:
        # how many answers were cut, how many continuations were sent, and how
        # many answers were finished and joined. ``mid_stream_cuts`` keeps its
        # old meaning — a cut that reached the user as one — so a rising
        # ``stream_drops`` with a flat ``mid_stream_cuts`` is the whole story.
        self.stream_drops = 0
        self.resumes = 0
        self.stitched = 0
        # Since 1.1.3. The drops that arrive in the one shape nothing can
        # continue — the stream stopped while a tool call's arguments were
        # being written. Counted apart from ``mid_stream_cuts`` because the two
        # ask for different things: a cut answer is a provider being flaky,
        # while a pool that keeps losing tool calls is usually one key that
        # cannot hold a long stream, and the only way anyone finds that out is
        # by seeing the number climb.
        self.tool_call_cuts = 0
        # 1.6.0.0. The same event, caught one step earlier: a tool call the
        # stream dropped before anything reached the screen, asked for again
        # on another key. Counted separately from ``tool_call_cuts`` because
        # they now mean opposite things — this one is a cut the user never
        # saw, and the other is one that got through to Hermes.
        self.tool_call_retries = 0
        # 1.6.0.1. Per identity, the keys the carousel is using that the
        # credential pool has never heard of. ``candidates()`` adds the key the
        # agent is already carrying when the pool does not know it, on purpose
        # — it is a working credential and dropping it would narrow what the
        # host would have sent. What was missing is anybody being told.
        #
        # It is worth telling. On this machine the pool held one NVIDIA row
        # pointing at an environment variable with two keys in it, both of
        # which the provider answered 401; the key that actually authenticated
        # was a third one, resolved from somewhere else entirely, and nothing
        # on any screen said it existed. Fingerprints and counts only.
        self.keys_outside_the_pool: Dict[str, List[str]] = {}
        # Since 1.0.2. It lives on the binding rather than on a call because an
        # outage does not end when a turn does: the provider is still down on
        # the next message, and a filter that reset per call would print its
        # loud opening lines again every turn and never reach the collapse.
        self._storm = StormFilter()
        self.suppressed = 0

    # -- lifecycle -------------------------------------------------------

    def install(self, module: Any) -> bool:
        """Wrap both dispatch functions. Never raises; a refusal is an outcome."""
        if self.installed:
            return True
        if module is None:
            self.reason = f"{_MODULE} is not importable in this process"
            logger.info("kame: every call keeps the host's key — %s", self.reason)
            return False

        originals: Dict[str, Callable] = {}
        for name in _FUNCTIONS:
            function = getattr(module, name, None)
            if not callable(function):
                self.reason = f"{_MODULE} has no {name}()"
                logger.info("kame: every call keeps the host's key — %s", self.reason)
                return False
            if getattr(function, _MARK, False):
                self.reason = "already wrapped by another KAME instance"
                logger.debug("kame: %s", self.reason)
                return False
            # Bind by signature (Agent Zero v1.0.9 parity): if a future
            # Hermes moves the function but changes its shape, wrapping it
            # would break every call. Check that it accepts at least the two
            # positional arguments the wrapper depends on — ``(agent,
            # api_kwargs)`` — and step aside if it does not, rather than
            # wrapping something whose contract has moved.
            try:
                import inspect

                sig = inspect.signature(function)
                params = list(sig.parameters.keys())
                if len(params) < 2 or params[1] != _EXPECTED_SECOND_PARAM:
                    self.reason = (
                        f"{_MODULE}.{name}() signature changed "
                        f"(params={params[:3]}) — stepping aside"
                    )
                    logger.warning("kame: %s", self.reason)
                    return False
            except (ValueError, TypeError):
                pass  # builtin or C function — proceed optimistically
            originals[name] = function

        self._module = module
        self._originals = originals
        for name, original in originals.items():
            setattr(module, name, self._wrap(original))
        self.installed = True
        self.reason = "active"
        logger.info(
            "kame: every API call picks the healthiest key and rotates on "
            "failure, waiting as long as a key needs"
        )
        return True

    def uninstall(self) -> None:
        if not self.installed or self._module is None:
            return
        for name, original in self._originals.items():
            if getattr(getattr(self._module, name, None), _MARK, False):
                setattr(self._module, name, original)
        self._module = None
        self._originals = {}
        self.installed = False
        self.reason = "uninstalled"

    # -- the wrapper -----------------------------------------------------

    def _wrap(self, original: Callable) -> Callable:
        binding = self

        @functools.wraps(original)
        def _kame_dispatch(agent, api_kwargs, *args, **kwargs):
            if settings.is_on(settings.ROTATION_DISABLED) or settings.is_on(
                settings.CAROUSEL_DISABLED
            ):
                return original(agent, api_kwargs, *args, **kwargs)
            try:
                return binding.run(original, agent, api_kwargs, args, kwargs)
            except _CONTROL_FLOW:
                raise
            except Exception:
                raise

        setattr(_kame_dispatch, _MARK, True)
        return _kame_dispatch

    # -- the carousel ----------------------------------------------------

    def run(
        self,
        original: Callable,
        agent: Any,
        api_kwargs: Any,
        args: Sequence[Any],
        kwargs: Dict[str, Any],
    ) -> Any:
        """One API call, as many keys as it takes."""
        try:
            keys, entry_by_key = candidates(agent)
        except Exception:
            logger.debug("kame: could not read the agent's keys", exc_info=True)
            return original(agent, api_kwargs, *args, **kwargs)

        if not keys:
            # Nothing to choose from — an OAuth provider, a callable bearer, a
            # local model. The host's own call is the whole behaviour.
            return original(agent, api_kwargs, *args, **kwargs)

        identity = Carousel.identity(
            getattr(agent, "provider", ""), getattr(agent, "model", "")
        )
        label = identity
        started = time.monotonic()
        self.calls += 1

        # Recorded once per call rather than per attempt: the answer is a
        # property of how this agent was configured, not of how the call went.
        try:
            outside = sorted(
                fingerprint(key) for key in keys if entry_by_key.get(key) is None
            )
            if outside:
                self.keys_outside_the_pool[identity] = outside
            else:
                self.keys_outside_the_pool.pop(identity, None)
        except Exception:  # pragma: no cover — a dict write and a hash
            logger.debug("kame: could not note the keys outside the pool", exc_info=True)

        attempt = 0
        slept = False
        vigil: Optional[_Vigil] = None
        empty_budget = EMPTY_RETRY_BUDGET
        empty_counts: Dict[str, int] = {}
        last_error: Optional[BaseException] = None
        consecutive_timeouts = 0
        # 1.1.1. The answer as the user has seen it, across every attempt of
        # this call. Empty until a stream is cut; from then on it is both the
        # text a continuation is prefilled with and the text a continuation is
        # trimmed against, which is exactly why one variable holds both jobs.
        seen = ""
        resumes = 0
        resume_budget = self._resume_budget(agent, api_kwargs)
        # 1.6.0.0. Which keys have been asked to continue this answer and
        # added nothing to it. This, and not the count above, is what ends the
        # stitching loop.
        #
        # A number was the wrong shape for the question. Three resumes is
        # generous for a provider that hiccupped once and far too few for one
        # having a bad ten minutes — and whichever number is chosen, the turn
        # it ends is the turn the answer was still growing. That is the
        # ceiling this plugin's own ADR 0002 rejects, and the owner's contract
        # rejects it in one sentence: *the agent should not stop because of
        # errors*.
        #
        # So the loop asks for proof instead, in the shape
        # ``_pool_agrees_it_is_the_request`` already uses for rotation:
        # unanimity. Continue while any key is still adding words to the
        # answer; stop only once every key in the pool has been asked and not
        # one of them contributed anything. With fifteen keys that is fifteen
        # empty continuations. With one key it is one, which is right — a
        # single-key pool that returned nothing has already asked everyone
        # there is to ask.
        #
        # Progress clears the set, because a key that answers proves the pool
        # is not out of answers, whatever the others did before it.
        stalled: set = set()
        #: Keys that stopped mid-tool-call this run. Same rule as ``stalled``:
        #: ask each key once, and stop when the pool starts repeating itself.
        tool_cut_keys: set = set()
        #: The cut response the last resume was launched from. Kept because a
        #: continuation can fail in a way that ends the call, and the answer the
        #: user has already read needs a response object to travel home in.
        cut_result: Any = None
        # What each key answered this run, for the same-answer rule below. Keyed
        # by the key itself so a pool that rotates twice through does not count
        # the second lap as new evidence.
        verdicts: Dict[str, Tuple[str, Optional[int]]] = {}

        def answer_so_far(reason: str, key: str = "", status: Optional[int] = None) -> Any:
            """Hand back the text the user has already read, as the answer.

            Once part of an answer is on screen there are only two wrong moves
            left, and every exit from the loop below can make one of them:
            *raising* throws away text the user is in the middle of reading,
            and letting the host make the call itself writes that text a
            second time, because the host's request carries no record of what
            was already delivered. This is the third move, and 1.1.2 routes
            every one of those exits through it.

            Only reachable with a response object to carry the text home in —
            the cut one the continuation was launched from.
            """
            self.mid_stream_cuts += 1
            logger.warning(
                "kame: %s %s — handing back the %d character(s) already shown "
                "instead of failing the turn",
                label,
                reason,
                len(seen),
            )
            EVENTS.add(
                "stream_drop",
                identity=identity,
                key=fingerprint(key) if key else "",
                reason=reason,
                code=status,
            )
            _publish(self, None)
            return _with_content(cut_result, seen)

        def the_answer_is_worth_more_than_this_exit() -> bool:
            """Whether there is a partial answer that must not be lost here."""
            return bool(seen) and cut_result is not None

        while True:
            attempt += 1
            if _interrupted(agent):
                if the_answer_is_worth_more_than_this_exit():
                    return answer_so_far("the turn was interrupted mid-answer")
                if last_error is not None:
                    raise last_error
                return original(agent, api_kwargs, *args, **kwargs)

            key, status = self.engine.select(identity, keys)
            if key is None:
                if the_answer_is_worth_more_than_this_exit():
                    return answer_so_far("no key left to continue the answer with")
                return original(agent, api_kwargs, *args, **kwargs)

            if status == "EXHAUSTED":
                # Every key is resting. Calling one anyway would spend a
                # request we already know will be refused and would deepen the
                # cooldown that caused this. Wait for the soonest instead —
                # for as long as it takes, since 1.0.1, but never in silence.
                if vigil is None:
                    vigil = _Vigil(agent, label)
                if not self._wait_for_recovery(agent, identity, keys, vigil):
                    # Only an interrupt, or a pool with no recovery to wait
                    # for, ends the wait now.
                    if the_answer_is_worth_more_than_this_exit():
                        return answer_so_far(
                            "every key is resting and the answer was already "
                            "half delivered"
                        )
                    if last_error is not None:
                        self.surfaced += 1
                        raise last_error
                    # Nothing failed yet and the pool is cold from an earlier
                    # turn: let the host make the call and report honestly.
                    return original(agent, api_kwargs, *args, **kwargs)
                slept = True
                continue

            _apply_key(agent, key, entry_by_key.get(key))

            if attempt > 1:
                # The other half of the story. Every event kind this buffer
                # had recorded until 1.6.0.1 was something going wrong; what
                # KAME *did* about it — which key it moved to — was in the log
                # and nowhere on screen, so the Events tab read as a fault
                # report rather than as a rotation engine working. The owner
                # asked for exactly this: every rotation, not only the errors.
                EVENTS.add(
                    "switch",
                    identity=identity,
                    key=fingerprint(key),
                    reason=(
                        "waited for a key, then took this one"
                        if slept
                        else f"attempt {attempt} — this key took over"
                    ),
                )

            # The one moment the plugin knows what is actually going out. The
            # pool's pointer says which credential it believes is in use;
            # this says which key the request will carry, and the two are not
            # the same claim — a row holding several keys, or a pointer
            # restored from disk, is exactly where they come apart. Recorded
            # as a fingerprint so the bench that may follow can be asked
            # whether it is blaming this key or another one.
            try:
                sent_entry = entry_by_key.get(key)
                runtime.note_sent(
                    getattr(agent, "provider", ""),
                    getattr(sent_entry, "id", "") if sent_entry is not None else "",
                    fingerprint(key),
                    now=time.time(),
                )
            except Exception:  # pragma: no cover — a ContextVar set and a hash
                logger.debug("kame: could not note the key in flight", exc_info=True)

            # Live UI feedback through the spinner, throttled so a rapid
            # rotation does not flood the thinking channel. Always shows pool
            # health so the user can see KAME is active — even on the first
            # attempt when everything is healthy.
            healthy = self.engine.healthy_count(identity, keys)
            _Spinner.update(
                agent,
                status_line(healthy, len(keys), subject=model_label(identity))
                if attempt == 1 and healthy == len(keys)
                else status_line(
                    healthy,
                    len(keys),
                    f"on key {attempt}",
                    subject=model_label(identity),
                    symbol="↻",
                ),
            )
            # The same fact, written where the Desktop chip can read it. The
            # spinner line lives inside one conversation's turn; the chip is
            # the app's own chrome and has to be able to say what is happening
            # without a turn to hang off.
            _publish(
                self,
                {
                    "kind": "calling",
                    "identity": identity,
                    "model": model_label(identity),
                    "attempt": attempt,
                    "healthy": healthy,
                    "keys": len(keys),
                },
            )

            progress = _Progress()
            call_kwargs = dict(kwargs)
            restore = _install_shims(agent, progress, call_kwargs)
            # A resumed attempt sends the answer so far as a trailing assistant
            # message and trims whatever the model repeats of it. The first
            # attempt has neither, and pays for neither.
            attempt_kwargs = api_kwargs
            stitcher = None
            if seen:
                resumed = _resume_kwargs(api_kwargs, seen, _prefill_refused(identity))
                if resumed is not None:
                    attempt_kwargs = resumed
                    stitcher = stitch.Stitcher(seen)
            delivery, restore_fire = _install_delivery(agent, progress, stitcher)
            try:
                with _SilenceTimeout(agent, attempt):
                    result = original(agent, attempt_kwargs, *args, **call_kwargs)
            except _CONTROL_FLOW:
                raise
            except BaseException as exc:  # noqa: BLE001 — re-raised below unless rotatable
                last_error = exc
                # Whatever reached the screen before the failure is part of the
                # answer now. Recording it before deciding what to do means a
                # continuation resumes from what the user actually saw, and
                # that a hand-back to Hermes reports the truth about it.
                seen += _contribution(delivery, seen, "", stitcher is not None)
                can_stitch = bool(seen) and resumes < resume_budget
                # "The user saw something" and "KAME captured what they saw"
                # are two different facts, and only the first one decides
                # whether a plain retry would print the answer twice. On a host
                # whose agent carries no delivery funnel the second is always
                # false, so reading only ``seen`` here would turn every drop
                # after visible text back into a replay — the exact bug
                # ``progress.any`` was introduced to prevent.
                verdict, kind, status = self._on_failure(
                    identity, key, exc, label, attempt, progress.any or bool(seen), can_stitch
                )
                if verdict == "raise" and stitcher is not None:
                    # A refusal earned by KAME's own rewriting, not by the
                    # user's request. Both branches below exist because this
                    # plugin must never turn "the answer was cut" into "the
                    # turn failed": the user has already read part of it.
                    said = f"{getattr(exc, 'message', '') or ''} {exc}"
                    if stitch.refuses_prefill(said) and identity not in _NO_PREFILL:
                        # This provider will not take a request that ends in
                        # its own voice. Remembered for the process, so the
                        # next cut answer is continued the other way round
                        # without spending a request to rediscover it.
                        _NO_PREFILL.add(identity)
                        logger.info(
                            "kame: %s refuses a continuation that ends with the "
                            "model's own turn — asking it to continue in a user "
                            "turn instead",
                            label,
                        )
                        EVENTS.add(
                            "stitch",
                            identity=identity,
                            key=fingerprint(key),
                            reason="this provider will not continue from a prefill; asking in a user turn",
                            code=status,
                        )
                        continue
                if verdict == "raise" and the_answer_is_worth_more_than_this_exit():
                    # Anything else that is fatal once part of the answer has
                    # been delivered — the refusal above, an unknown parameter,
                    # a request that grew too long with the prefill in it. The
                    # continuation is abandoned; what the user already read
                    # goes back to Hermes as the answer so far, which is the
                    # 1.1.0 behaviour and infinitely better than raising away
                    # an answer that is on screen.
                    return answer_so_far(
                        "the continuation was refused; returning the answer so far",
                        key,
                        status,
                    )

                if verdict == "raise":
                    self.surfaced += 1
                    EVENTS.add(
                        "surfaced",
                        identity=identity,
                        key=fingerprint(key),
                        reason=kind or "refused",
                        code=status,
                    )
                    raise
                if verdict == "stitch":
                    resumes += 1
                    self.resumes += 1
                    _clear_host_stale_streak(agent)
                    self.rotations += 1
                    continue
                verdicts[key] = (kind, status)
                if kind == "timeout":
                    consecutive_timeouts += 1
                else:
                    consecutive_timeouts = 0
                if consecutive_timeouts >= 3 and all(v[0] == "timeout" for v in verdicts.values()):
                    logger.warning(
                        "kame: %s provider appears down — %d consecutive "
                        "timeouts, skipping to recovery wait",
                        label,
                        consecutive_timeouts,
                    )
                    for k in keys:
                        if self.engine.healthy_count(identity, [k]) > 0:
                            self.engine.mark(identity, k, False, 5.0, "timeout")
                    _clear_host_stale_streak(agent)
                    self.rotations += 1
                    continue
                if self._pool_agrees_it_is_the_request(verdicts, keys, kind, status):
                    if the_answer_is_worth_more_than_this_exit():
                        # The pool agreeing is a statement about the request,
                        # and the request it agrees about is the *continuation*
                        # KAME wrote. The user's own turn was answered well
                        # enough to put text on screen, so that text is what
                        # goes back.
                        return answer_so_far(
                            f"every key refused the continuation ({kind})", key, status
                        )
                    self.surfaced += 1
                    logger.error(
                        "kame: %s every key answered %s%s and none answered at "
                        "all — that is evidence about the request, not about "
                        "the keys. Surfacing it instead of rotating further.",
                        label,
                        kind,
                        f" [{status}]" if status else "",
                    )
                    raise
                # Hermes counts consecutive silent streams on the AGENT, and
                # clears the count when the provider is swapped, because the
                # streak measured the provider being left behind. Rotating a
                # key is the same event by the same reasoning, and Hermes has
                # no way to know it happened. Left uncleared, five rotations
                # through a slow provider trip a breaker that then refuses
                # every remaining key before touching the network.
                _clear_host_stale_streak(agent)
                self.rotations += 1
                continue
            finally:
                _remove_shims(agent, restore)
                _remove_delivery(agent, restore_fire)
                # Releases anything the stitcher was still holding back, on
                # every path out of the attempt — including the failing ones,
                # where text held for a decision that will never come would
                # simply be lost.
                delivery.finish()

            # A stream that stopped in the middle of a sentence. Hermes returns
            # this rather than raising it, which is why every version before
            # 1.1.1 saw it as a successful call and let the host paper over it.
            partial = _partial_text(result)
            if partial is not None:
                self.stream_drops += 1
                before = len(seen)
                seen += _contribution(delivery, seen, partial, stitcher is not None)
                # Measured on the answer itself rather than on what the
                # response object claims: a continuation that returns text the
                # stitcher recognises as a repeat has added nothing, and is
                # exactly the case a length check catches and a truthiness
                # check does not.
                if len(seen) > before:
                    # This key added words, so the pool is not out of answers
                    # — whatever the ones before it did.
                    stalled.clear()
                    going_in_circles = False
                else:
                    # Asked twice, silent twice, with nothing having changed
                    # in between: the loop has come back round to a key that
                    # already proved it had nothing to add. That, and not a
                    # count, is the end of the road.
                    #
                    # Stated this way rather than as "every key has stalled"
                    # because the two are not the same and the difference is
                    # a hang: a key rested by an earlier drop is still in
                    # ``keys`` and will not be handed out again for thirty
                    # seconds, so waiting for it to stall too is waiting for
                    # an attempt that is never going to be made.
                    going_in_circles = key in stalled
                    stalled.add(key)
                EVENTS.add(
                    "stream_drop",
                    identity=identity,
                    key=fingerprint(key),
                    reason="the provider closed the stream mid-answer",
                )
                if seen and not going_in_circles and resumes < resume_budget:
                    resumes += 1
                    self.resumes += 1
                    cut_result = result
                    rested = _rest_unless_it_is_the_only_one(
                        self.engine, identity, keys, key, DROP_REST_S, "timeout"
                    )
                    logger.info(
                        "kame: %s %s cut the answer after %d character(s) — %s (%d/%d)",
                        label,
                        fingerprint(key),
                        len(seen),
                        f"resting it {format_duration(rested)} and continuing on another key"
                        if rested
                        else "it is the only key that is well, so the answer "
                        "continues on it immediately rather than after a rest",
                        resumes,
                        resume_budget,
                    )
                    EVENTS.add(
                        "stitch",
                        identity=identity,
                        key=fingerprint(key),
                        reason=f"continuing the answer on another key ({resumes}/{resume_budget})",
                    )
                    _Spinner.update(
                        agent,
                        status_line(
                            self.engine.healthy_count(identity, keys),
                            len(keys),
                            "continuing the answer on the next key",
                            subject="a cut answer",
                            symbol="↻",
                        ),
                    )
                    _publish(
                        self,
                        {
                            "kind": "stitching",
                            "identity": identity,
                            "model": model_label(identity),
                            "resume": resumes,
                            "budget": resume_budget,
                            "characters": len(seen),
                        },
                    )
                    _clear_host_stale_streak(agent)
                    self.rotations += 1
                    continue
                # Nothing was ever shown, or the whole pool has now been asked
                # and none of it added a word. Everything that did arrive goes
                # back in one piece, and Hermes' own continuation takes it from
                # there — which is the 1.1.0 behaviour, reached only after
                # trying not to need it.
                self.mid_stream_cuts += 1
                logger.info(
                    "kame: %s answer still cut after %d resume(s) — %s, handing "
                    "what arrived back to Hermes",
                    label,
                    resumes,
                    "nothing was shown to continue from" if not seen
                    else f"{len(stalled)} key(s) continued it and added nothing"
                    if going_in_circles
                    else f"the resume ceiling of {resume_budget} was reached",
                )
                self.engine.mark(identity, key, True)
                _publish(self, None)
                return _with_content(result, seen) if seen else result

            # The same drop, in the one shape that must not be continued: the
            # stream stopped while a tool call's arguments were being written.
            # ``_partial_text`` returns ``None`` for it, on purpose — half a
            # JSON payload is not something a second model call can be asked to
            # finish, and guessing at one would hand Hermes a call it never
            # made.
            #
            # Refusing to continue was right; going quiet about it was not.
            # Until 1.1.3 this stub fell through to the success path below, so
            # a key that did this on every turn was recorded as having answered
            # every turn: nothing in Events, nothing in the counters, and the
            # next selection treating it as the freshest key in the pool. The
            # evidence is written here instead. The stub still goes back
            # untouched — the host has its own handling for it — and the key
            # rests only when there is another one to route to.
            if getattr(result, "id", "") == PARTIAL_STUB_ID:
                self.stream_drops += 1
                # 1.6.0.0. Half a tool call cannot be *continued* — that has
                # not changed, and is why ``_partial_text`` returns ``None``
                # for this shape. But it can be asked for again, from the
                # start, on a different key: the request is unchanged and,
                # when nothing has reached the screen, a fresh attempt cannot
                # print anything twice. Nothing was risked by giving up here
                # and something real was lost.
                #
                # What the user sees when this reaches Hermes is the reason
                # it matters. The host reads the stub, decides the call was
                # oversized, and writes into the conversation:
                #
                #     your previous tool call was too large ...
                #     Do NOT retry ... break it into smaller calls
                #
                # That instruction is addressed to the model and it is often
                # simply false — the cause was a key that timed out
                # mid-argument, and the identical call on the next key
                # succeeds. Left in place it teaches the model to avoid a
                # tool that was never the problem.
                #
                # Bounded the same way the empty-answer branch below is
                # bounded, and by the same rule the resume loop uses: walk
                # the pool once. Coming back to a key that already did this
                # is the proof that it is the request, and then the stub goes
                # home exactly as it did before.
                # ``seen`` is not the whole question here. It is filled by the
                # partial-text branch above, and ``_partial_text`` returns
                # ``None`` for this stub on purpose — so a first attempt that
                # streamed a sentence and *then* dropped inside a tool call
                # arrives with ``seen`` still empty and the sentence sitting
                # in the delivery. Retrying on that would print it twice.
                shown = seen or getattr(delivery, "text", "")
                first_time = key not in tool_cut_keys
                tool_cut_keys.add(key)
                # Walk the pool once and stop: another key left to ask, and
                # this key not already having been asked. The second half is
                # what makes it terminate when the engine hands back a key it
                # has already tried — with every key rested there is nothing
                # left for it to hand back but a repeat.
                unasked = any(k and k not in tool_cut_keys for k in keys)
                if not shown and first_time and unasked:
                    self.tool_call_retries += 1
                    rested = _rest_unless_it_is_the_only_one(
                        self.engine, identity, keys, key, DROP_REST_S, "timeout"
                    )
                    logger.info(
                        "kame: %s %s stopped inside a tool call before anything "
                        "was shown — asking another key for the same call "
                        "rather than telling the model its call was too big; %s",
                        label,
                        fingerprint(key),
                        f"the key rests {format_duration(rested)}"
                        if rested
                        else "the key is not rested, it is the only one that is well",
                    )
                    EVENTS.add(
                        "stream_drop",
                        identity=identity,
                        key=fingerprint(key),
                        reason="the stream stopped inside a tool call — retrying on another key",
                        seconds=rested or None,
                    )
                    _clear_host_stale_streak(agent)
                    self.rotations += 1
                    continue
                self.tool_call_cuts += 1
                if seen:
                    content = ""
                    try:
                        content = str(
                            getattr(result.choices[0].message, "content", "") or ""
                        )
                    except Exception:  # pragma: no cover — shape nobody has seen
                        content = ""
                    seen += _contribution(delivery, seen, content, stitcher is not None)
                    result = _with_content(result, seen)
                dropped = getattr(result, "_dropped_tool_names", None)
                named = ""
                if isinstance(dropped, (list, tuple, set)):
                    named = ", ".join(str(name) for name in list(dropped)[:3])
                rested = _rest_unless_it_is_the_only_one(
                    self.engine, identity, keys, key, DROP_REST_S, "timeout"
                )
                logger.warning(
                    "kame: %s %s stopped inside a tool call%s — nothing can "
                    "continue a half-written call, so it goes back to Hermes; %s",
                    label,
                    fingerprint(key),
                    f" ({named})" if named else "",
                    f"the key rests {format_duration(rested)}"
                    if rested
                    else "the key is not rested, it is the only one that is well",
                )
                EVENTS.add(
                    "stream_drop",
                    identity=identity,
                    key=fingerprint(key),
                    reason=(
                        f"the stream stopped inside a tool call ({named})"
                        if named
                        else "the stream stopped inside a tool call"
                    ),
                    seconds=rested or None,
                )
                _publish(self, None)
                return result

            # An answer that carried nothing. Usually a provider hiccup or a
            # filtered completion, so the first one from a key costs it
            # nothing. A second from the same key rests it briefly. Bounded by
            # the budget, after which the empty answer is returned exactly as
            # the host would have returned it — this can never become a loop.
            if empty_budget > 0 and not progress.any and _result_is_empty(result):
                empty_budget -= 1
                empty_counts[key] = empty_counts.get(key, 0) + 1
                if empty_counts[key] >= 2:
                    self.engine.mark(identity, key, False, EMPTY_REST_S, "other")
                logger.info(
                    "kame: %s %s answered with nothing (%d) — next key",
                    label,
                    fingerprint(key),
                    empty_counts[key],
                )
                self.rotations += 1
                continue

            # The answer finished. If part of it was delivered by an earlier
            # key, what goes back to Hermes is the whole thing as one
            # response — no stub, no continuation row, nothing for the host to
            # explain to the user.
            if seen:
                content = ""
                try:
                    content = str(getattr(result.choices[0].message, "content", "") or "")
                except Exception:  # pragma: no cover — shape checked by _result_is_empty
                    content = ""
                seen += _contribution(delivery, seen, content, stitcher is not None)
                result = _with_content(result, seen)
                self.stitched += 1
                logger.info(
                    "kame: %s answer completed across %d key(s) — %d character(s), "
                    "delivered as one response",
                    label,
                    resumes + 1,
                    len(seen),
                )
                EVENTS.add(
                    "stitch",
                    identity=identity,
                    key=fingerprint(key),
                    reason="the cut answer was completed and joined",
                )

            self.engine.mark(identity, key, True)
            if vigil is not None:
                vigil.done()
                # The wait is over — tell the spinner the pool is back.
                _Spinner.update(
                    agent,
                    status_line(
                        self.engine.healthy_count(identity, keys),
                        len(keys),
                        f"back after {format_duration(time.monotonic() - started)}",
                        subject="",
                        symbol="↻",
                        opener="model returned",
                    ),
                )
                _publish(
                    self,
                    {
                        "kind": "recovered",
                        "identity": identity,
                        "model": model_label(identity),
                        "waited_s": time.monotonic() - started,
                    },
                )
            # A key answered, so whatever was repeating has stopped. The recap
            # is the line that makes the collapse safe to read: without it a
            # reader knows a storm started and never learns how big it got.
            recap = self._storm.ended(time.monotonic())
            if recap:
                logger.warning("kame: %s %s", label, recap)
            if slept:
                thawed = self.engine.thaw_server_cooled(identity, key)
                if thawed:
                    logger.info(
                        "kame: %s recovered — %d key(s) brought back early", label, thawed
                    )
            if attempt > 1:
                self.recovered += 1
                elapsed = time.monotonic() - started
                logger.info(
                    "kame: %s answered on attempt %d with %s after %s",
                    label,
                    attempt,
                    fingerprint(key),
                    format_duration(elapsed),
                )
                # The end of the story, and the only row in the buffer that
                # says the rotation worked. ``recovery`` has been in the event
                # vocabulary since 1.1.1 and was never once written, so the
                # panel could show a turn falling apart and never show it
                # being put back together.
                EVENTS.add(
                    "recovery",
                    identity=identity,
                    key=fingerprint(key),
                    reason=f"answered on attempt {attempt}",
                    seconds=elapsed,
                )
            # Nothing is in flight any more. Clearing the activity is what
            # lets the chip fall back to plain pool health instead of leaving
            # "on key 3" frozen on screen until the next call.
            _publish(self, None)
            return result

    @staticmethod
    def _resume_budget(agent: Any, api_kwargs: Any) -> int:
        """How many times this call may continue a cut answer. Zero disables it.

        Everything that could make stitching unsafe is decided once, here,
        before a single request goes out — so the hot path never has to ask,
        and a host that cannot support it degrades to exactly the 1.1.0
        behaviour instead of half of it.
        """
        if settings.is_on(settings.STREAM_STITCH_DISABLED):
            return 0
        if stitch.resumable(api_kwargs) is None:
            # A request shape this plugin does not recognise. Continuing it
            # would mean guessing where the conversation is kept.
            return 0
        if not callable(getattr(agent, "_fire_stream_delta", None)):
            # Without the host's delivery funnel there is no record of what
            # the user has seen, and a continuation without that record is the
            # answer printed twice.
            return 0
        # The default is read from ``settings.ALL_NUMBERS`` rather than
        # written here. It was a literal ``3.0``, which meant the table said
        # one thing and the only code that reads the value said another —
        # raising the documented default in 1.6.0.0 would have changed the
        # panel and nothing else.
        fallback = settings.ALL_NUMBERS.get(settings.STREAM_RESUME_LIMIT, 10.0)
        try:
            return max(0, int(settings.number(settings.STREAM_RESUME_LIMIT, fallback)))
        except Exception:  # pragma: no cover — settings clamps before this
            return int(fallback)

    @staticmethod
    def _pool_agrees_it_is_the_request(
        verdicts: Dict[str, Tuple[str, Optional[int]]],
        keys: Sequence[str],
        kind: str,
        status: Optional[int],
    ) -> bool:
        """Whether the pool has proved the request is at fault, not the keys.

        Agent Zero rotates without a ceiling and so does this, and neither has
        an attempt limit -- a number like "give up after ten" is the thing ADR
        0002 rejects, because whatever it is set to, some real quota wait is
        longer. This is not that. It asks for proof, and the proof is unanimity:

        * every key in the pool has been tried at least once this run, and
        * every one of them failed the same way, and
        * not one of them succeeded, and
        * the way they failed is not something that passes on its own.

        With fifteen keys that takes fifteen identical refusals. With one key it
        takes one, which is correct and not aggressive: a single-key pool that
        got a 418 has already asked everyone there is to ask.

        The last condition is what keeps the eternal carousel eternal. A daily
        quota, a throttle, an outage, a timeout and a bad credential are all
        excluded by ``_NEVER_PROMOTED``, so the case this plugin exists for --
        every key spent, wait for midnight -- can never reach here.
        """
        if kind in _NEVER_PROMOTED:
            return False
        pool = [k for k in keys if k]
        if len(pool) < 1 or len(verdicts) < len(pool):
            return False
        return all(verdicts.get(k) == (kind, status) for k in pool)

    # -- the two decisions -----------------------------------------------

    def _on_failure(
        self,
        identity: str,
        key: str,
        exc: BaseException,
        label: str,
        attempt: int,
        streamed: bool,
        can_stitch: bool = False,
    ) -> Tuple[str, str, Optional[int]]:
        """``(verdict, kind, status)`` — and the key's rest recorded either way.

        ``verdict`` is ``"rotate"``, ``"stitch"`` or ``"raise"``. The ``(kind, status)`` pair
        comes back with it because the caller counts how many *different*
        answers the pool gave: fifteen keys refusing fifteen different ways is
        a bad afternoon, and fifteen keys refusing the same way is a bad
        request. Only the caller can tell those apart, and only if it is told
        what each key said.

        A failure is always *learned from*, even when it is re-raised — a
        terminal error still tells us nothing bad about the key, and a
        mid-stream drop still does.
        """
        # 1.4.0: read the failure for everything it is willing to say, before
        # anything decides anything.
        #
        # What was here until now was one line — `getattr(exc, "message", "")` —
        # and it was the most expensive line in the plugin. The host's
        # `GeminiAPIError` passes its text to `Exception.__init__` and defines
        # no `message` attribute, so that read returned the empty string on
        # every Gemini failure there has ever been. An empty message means the
        # footer strip below it had nothing to strip, the classifier had no
        # prose to match, and the sizing cascade in `quota` had nothing to size
        # from. Nine days of the user's own telemetry, 276 recorded blocks:
        # `reset_at` set 0 times, `sized_by: dropped` 184 times, and the header
        # source never firing once.
        #
        # Everything the cascade wanted was on the exception the whole time —
        # `status_code`, `code`, `retry_after`, `details`, and the response
        # carrying the body and headers. `core.evidence` reads all of it,
        # guarded field by field, and takes Hermes' own appended guidance back
        # off the message so the classifier is never matching the host's
        # handwriting.
        ev = evidence.harvest(
            exc,
            message=str(exc),
            guidance_blocks=host_text.guidance_blocks(),
        )
        message = ev.message
        exc_str = ev.raw_message

        verdict = classify(
            # The provider's real name, from the identity this call is on.
            # Until 1.2.9 this was the literal "gemini" for every provider on
            # earth, which meant NVIDIA's refusals were sized with Google's
            # rules. `identity` is `provider:model`, and the model half can
            # itself contain colons (`nvidia:z-ai/glm-5.2`), so the split is
            # bounded to one.
            provider=identity.split(":", 1)[0] if ":" in identity else identity,
            model=identity,
            status_code=ev.status_code,
            error_message=message,
            error_body=ev.body,
            headers=ev.headers,
            error=exc,
            now_epoch=time.time(),
        )

        if verdict is not None:
            delay = max(0.0, verdict.reset_at - time.time()) if verdict.reset_at else 0.0
            kind = verdict.reason
            if kind == "billing":
                kind = "insufficient_quota"
                delay = self.engine.daily_cooldown_s
            elif kind == "auth_permanent":
                # ``revoked``, not ``auth``, and the difference is the whole
                # of what this branch is for. ``classify`` reaches
                # ``auth_permanent`` only when the provider used the words —
                # "API key not valid", "invalid api key" — and until 1.6.0.1
                # that finding was flattened into the same kind as a bare 401
                # one line later, which threw away the only evidence strong
                # enough to act on. A bare 401 needs three in a row; this one
                # leaves rotation immediately, because there is nothing
                # ambiguous left to gather evidence about.
                kind = "revoked"
                delay = REJECTED_REST_S
            elif getattr(verdict, "kind", "") == "denied":
                # A 403 that named a *model*, not the key. It reaches here as
                # ``auth`` because that is the only word Hermes has for it —
                # ``reason`` is coerced to a ``FailoverReason`` member on the
                # host side and an unknown one drops the whole classification
                # — so the distinction rides on ``Verdict.kind`` instead.
                #
                # Keeping them apart is the point. ``auth`` is in
                # ``RETIRING_KINDS``; ``denied`` is deliberately not. Three
                # refusals from one model the key was never entitled to must
                # not cost a credential that works everywhere else, and until
                # 1.6.0.1 it did.
                kind = "denied"
                delay = DENIED_REST_S
            elif kind == "auth":
                # A bare refusal. Short rest, offered last, and out only after
                # ``REFUSALS_BEFORE_RETIRING`` of them in a row — the shape
                # that keeps an expired OAuth token, a proxy or a provider
                # incident from retiring a working credential, which is
                # exactly what cost 1.4.0 twenty-one healthy keys.
                delay = REJECTED_REST_S
            status = ev.status_code
            sized_by = verdict.source or "verdict"
        else:
            # The evidence-first classifier declined, which is the common and
            # the safe case: it answers only when the payload carries something
            # Hermes' own classifier does not read. The table-driven reading
            # below is the fallback, and it now gets the same evidence — the
            # status the exception did not put where it was looked for, the
            # headers, and a message with the host's guidance already off it.
            from .core.carousel import classify as legacy_classify
            delay, kind, status = legacy_classify(
                exc,
                message,
                status_code=ev.status_code,
                headers=ev.headers,
                daily_cooldown_s=self.engine.daily_cooldown_s,
            )
            sized_by = "table"
        if kind == "host_breaker":
            # Hermes' own cross-turn breaker. It raises before touching the
            # network, so rotating into it is free and useless in equal
            # measure: the counter it trips on lives on the agent, not on the
            # key. KAME clears that counter on every rotation for exactly this
            # reason, so reaching here means the clearing did not help and
            # every key really is wedged. Hermes' message already says what to
            # do, so it is passed through rather than dressed up.
            logger.error(
                "kame: %s Hermes stopped this call itself — %d consecutive "
                "silent streams across the pool. Rotating cannot help: the "
                "counter is per session, not per key. Switch model or start a "
                "new session.",
                label,
                _HOST_BREAKER_THRESHOLD,
            )
            return "raise", kind, status
        if is_terminal(exc, message):
            logger.info(
                "kame: %s %s — the request itself was refused, not the key", label, type(exc).__name__
            )
            return "raise", kind, status
        applied = self.engine.mark(identity, key, False, delay, kind, )
        # Never the error text: a provider's unredacted dump of a failed auth
        # call can carry the key that failed.
        #
        # Since 1.0.2 this goes past the storm filter first. During an outage
        # the same sentence repeats with nothing new in it, and 1.0.1 made that
        # worse rather than better: with the ceiling gone the carousel keeps
        # rotating for as long as the provider keeps refusing, so what used to
        # be ten minutes of repetition is now however long the outage lasts.
        # The filter is bypassed entirely when switched off, and it can only
        # ever decide what gets *written* — the rest, and the key, are already
        # decided above.
        if settings.is_on(settings.STORM_COLLAPSE_DISABLED):
            verdict = _ALWAYS_LOUD
        else:
            verdict = self._storm.observe(
                kind, status, fingerprint(key), time.monotonic()
            )
        if verdict.summary:
            logger.warning("kame: %s %s", label, verdict.summary)
        if verdict.speak_full:
            logger.warning(
                "kame: %s %s %s%s — resting %s, taking the next key (attempt %d)",
                label,
                fingerprint(key),
                kind,
                f" [{status}]" if status else "",
                format_duration(applied),
                attempt,
            )
        else:
            self.suppressed += 1
        if kind == "denied":
            # A refusal of this *pairing*, and the sentence has to say so.
            #
            # This used to fall into the branch below, because the gate was
            # ``is_auth_failure`` — a fresh look at the text, which answers
            # yes for a 403 — rather than the verdict already decided above.
            # So a key that simply is not entitled to one model was announced
            # as "not a valid credential — replace it in Settings", which is
            # false twice over: nothing is wrong with the key, and replacing
            # it would change nothing. The owner's rule again: refusing a
            # model does not mean the API does not work.
            logger.warning(
                "kame: %s %s may not use this model — rested %s. "
                "The key is untouched everywhere else; another model or "
                "another key answers this turn.",
                label,
                fingerprint(key),
                format_duration(applied),
            )
            EVENTS.add(
                "denied_model",
                identity=identity,
                key=fingerprint(key),
                reason="this key may not use this model — it still works elsewhere",
                code=status,
                seconds=applied,
            )
        elif kind in _CREDENTIAL_REFUSALS:
            # Actionable and permanent: this one is worth saying loudly, once
            # per occurrence, because no amount of rotation repairs it.
            #
            # Gated on the kind decided above rather than on a second reading
            # of the text. The two disagreed, and the disagreement is what
            # put the wrong sentence on a denial.
            #
            # 1.6.0.1 stopped saying "quarantined for N" here for the kind
            # that is not a quarantine. A key the provider named dead is out
            # of rotation until somebody replaces it, and telling the reader
            # a duration invites them to wait for a key that is not coming
            # back — which is the sentence the owner objected to after
            # watching four keys serve an hour and come round again.
            out_for_good = self.engine.is_retired(identity, key)
            logger.error(
                "kame: %s %s is not a valid credential — %s. "
                "Replace it in Settings; the remaining keys are carrying this turn.",
                label,
                fingerprint(key),
                "out of rotation until it is replaced"
                if out_for_good
                else f"rested {format_duration(applied)}",
            )
            EVENTS.add(
                "invalid_key",
                identity=identity,
                key=fingerprint(key),
                reason=(
                    "out of rotation — replace this key, it is not a valid credential"
                    if out_for_good
                    else "the provider refused this key as a credential"
                ),
                code=status,
                seconds=applied,
            )
        if streamed:
            # The user has already seen part of an answer, so this cannot be a
            # plain retry: the same text would be printed twice.
            self.stream_drops += 1
            EVENTS.add(
                "stream_drop",
                identity=identity,
                key=fingerprint(key),
                reason=f"{kind or 'the connection'} failed mid-answer",
                code=status,
                detail=ev.raw_message,
                sized_by=sized_by,
            )
            if can_stitch:
                # Since 1.1.1: continue it on another key instead, prefilled
                # with what was shown, and trim whatever the model repeats.
                # Nothing reaches the screen twice, and Hermes never sees a
                # cut to paper over. The counter and the budget belong to the
                # caller, which is the only place that knows how many
                # continuations this one call has already spent.
                logger.info(
                    "kame: %s dropped mid-answer — continuing it on another key", label
                )
                EVENTS.add(
                    "stitch",
                    identity=identity,
                    key=fingerprint(key),
                    reason="continuing the answer after a failed stream",
                )
                return "stitch", kind, status
            self.mid_stream_cuts += 1
            logger.info(
                "kame: %s dropped mid-answer and cannot be continued — handing "
                "it to Hermes rather than printing the reply twice",
                label,
            )
            return "raise", kind, status
        EVENTS.add(
            "quarantine" if applied >= 60.0 else "rotation",
            identity=identity,
            key=fingerprint(key),
            reason=kind or "refused",
            code=status,
            seconds=applied,
            # Redacted inside `Events.add`, never here — the scrub belongs to
            # the store, so no caller can forget it.
            detail=ev.raw_message,
            sized_by=sized_by,
        )
        return "rotate", kind, status

    def _wait_for_recovery(
        self, agent: Any, identity: str, keys: Sequence[str], vigil: "_Vigil"
    ) -> bool:
        """Sleep until the soonest key is usable. ``False`` means stop waiting.

        Since 1.0.1 there is no ceiling on the total wait, matching Agent Zero's
        ADR 0002. Two host facts make that safe here in a way it never was
        there: every attempt already carries Hermes' own per-request timeout
        (1800 s by default, ``run_agent.py:1394``), so a genuinely hung socket
        surfaces as a ``TimeoutError`` this carousel rotates on rather than
        hanging forever; and the agent runs in a worker thread
        (``asyncio.to_thread``), so sleeping here never blocks the event loop,
        the websocket, or the stop button.

        Only two things end the wait: an interrupt, or a pool with nothing to
        wait for. Slept in one-second slices so a stop is honoured within a
        second, and capped per pass so a long cooldown is re-checked rather
        than committed to — a key that recovers early, or a pool that gains a
        key, is used immediately.
        """
        eta = self.engine.next_recovery_seconds(identity, keys)
        healthy = self.engine.healthy_count(identity, keys)
        total = len(keys)
        vigil.maybe_speak(healthy, total, eta)
        if eta is None:
            # The selector said every key was resting and the clock says one is
            # ready — a key recovered in the microseconds between the two
            # reads, or another thread just freed one. Re-select immediately,
            # but never without yielding first: with no ceiling above this loop
            # any disagreement between the two answers would otherwise spin a
            # core until the process died. One slice is enough to make it a
            # re-check instead of a spin, and it keeps the stop button honest.
            wait = _SLEEP_SLICE_S
        else:
            wait = min(eta + 0.5 + self._jitter(), _MAX_SLEEP_S)
        if wait <= 0:
            wait = _SLEEP_SLICE_S
        # Debug since 1.0.2, and it was a warning before. This fires once per
        # pass through the wait, which under 1.0.1's unbounded wait means once
        # a minute for as long as the quota lasts — the same storm the failure
        # lines had, in the one place where nothing is actually happening.
        # ``_Vigil`` above already narrates the wait, on a schedule built for a
        # person to read (90 s, then every 10 minutes) and through the user's
        # own channel, falling back to this logger when the host has none. So
        # the information is not lost, only paced.
        logger.debug(
            "kame: %s every key is resting — waiting %s (no requests sent); "
            "earliest recovery %s",
            vigil.label,
            format_duration(wait),
            "now" if eta is None else f"in {format_duration(eta)}",
        )
        self.waits += 1
        if eta is not None:
            # ``wait`` is also in the vocabulary since 1.1.1 and was never
            # written either. It is the one event that explains a stall: the
            # panel could show a pool with nothing healthy in it and give no
            # sign that KAME was awake and counting down.
            EVENTS.add(
                "wait",
                identity=identity,
                reason=f"every key resting — {healthy} of {total} usable",
                seconds=eta,
            )
        slept = 0.0
        while slept < wait:
            if _interrupted(agent):
                self.waited_s += slept
                return False
            # Redrawn every slice, but only *sent* when the text would change
            # and no more often than the countdown deserves — a wait an hour
            # out is redrawn twice a minute, the last ten seconds tick once a
            # second. The line already on screen persists in between, so the
            # status is continuously visible without being continuously sent.
            #
            # This is the one thing 1.0.9 exists to fix from the user's side:
            # a carousel that waits correctly and says nothing is, from the
            # chair, indistinguishable from one that has frozen.
            remaining = None if eta is None else max(eta - slept, 0.0)
            _Spinner.update(
                agent,
                status_line(
                    healthy,
                    total,
                    f"next key in {recovery_clock(remaining)}",
                    subject="a key to come back",
                )
                if remaining is not None
                else status_line(healthy, total, subject="a key to come back"),
                interval=_Spinner.cadence_for(remaining),
            )
            # The wait is the one state where an ETA exists, so it is the one
            # state the chip most needs published: "13/15" is reassuring on its
            # own, "0/15, next key in 1m 23s" is the difference between waiting
            # and being stuck.
            _publish(
                self,
                {
                    "kind": "waiting",
                    "healthy": healthy,
                    "keys": total,
                    "eta_s": remaining,
                },
            )
            slice_s = min(_SLEEP_SLICE_S, wait - slept)
            self._sleep(slice_s)
            slept += slice_s
        self.waited_s += slept
        return True


def install(module: Optional[Any] = None) -> Optional[DispatchBinding]:
    """Convenience entry point used by ``register()``; never raises.

    Imports the module when it is not already loaded, for the same reason the
    resolver binding does: plugin registration runs early, and a binding that
    installs only when something happened to import the host module first is a
    binding that works on some starts and not others. The module is plain — no
    server, no socket, no I/O at import.
    """
    if settings.is_on(settings.ROTATION_DISABLED) or settings.is_on(
        settings.CAROUSEL_DISABLED
    ):
        return None
    try:
        if module is None:
            module = sys.modules.get(_MODULE)
        if module is None:
            from importlib import import_module

            module = import_module(_MODULE)
        binding = DispatchBinding(
            jitter=lambda: random.uniform(0.1, 1.5)
        )
        binding.engine.daily_cooldown_s = settings.number(
            settings.DAILY_COOLDOWN, binding.engine.daily_cooldown_s
        )
        return binding if binding.install(module) else None
    except ImportError:
        logger.debug("kame: no %s to bind", _MODULE)
        return None
    except Exception:
        logger.warning("kame: every call keeps the host's key", exc_info=True)
        return None
