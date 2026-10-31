"""The carousel, owned by KAME's own client instead of wrapped around Hermes'.

Until 1.8.1.8 this logic lived in ``dispatch_binding``, which replaced the two
Hermes functions every model call goes through. The plugin catalog's rule 9
forbids that (NousResearch/hermes-agent#131918), so 1.8.1.9 moves it one level
down: Hermes builds a client for each provider through the provider profile's
public ``create_client`` seam, the companion ``hermes-kame-provider`` plugin
answers that seam with ``facade.KameClient``, and every request that client is
asked to make runs through :class:`KameTransport` here.

The decisions are the 1.8.1.8 decisions, byte for byte where they do not touch
the agent: ``_on_failure`` (how a refusal is read, how long the key rests, when
the pool has proved the *request* is at fault), the unanimity rules, the
recovery wait, the resume budget and the stitcher. What changed is only where
the request comes from and where the answer goes:

* the key is chosen per call and handed to a per-key inner client, instead of
  being written onto the agent;
* a streamed answer reaches Hermes through the iterator this module yields,
  so "what the user has seen" is what this module passed on, and a cut answer
  is continued on another key inside the same stream Hermes is reading;
* a tool call is held until it is complete, so a stream that dies in the
  middle of one can be asked for again without Hermes ever holding half of it;
* while every key rests, the stream yields an empty keep-alive chunk now and
  then, so Hermes' own silence watchdog — which resets on every chunk — sees a
  provider that is alive rather than one that hung.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import queue
import re
import threading
import time
from collections import OrderedDict, deque
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import host_text, recorder, runtime, settings, timings, wires
from .core import aux_budget, evidence, multikey, provider_quirks, stitch, vocabulary
from .core.storm import StormFilter, Verdict as StormVerdict
from .core.events import EVENTS
from .core.events import active_events as _active_events  # 1.8.2.3
from .core.carousel import (
    EMPTY_REST_S,
    DENIED_REST_S,
    REJECTED_REST_S,
    EMPTY_RETRY_BUDGET,
    ENGINE,
    Carousel,
    fingerprint,
    format_duration,
    is_bare_resource_exhausted,
    is_terminal,
)
from .core.classify import classify, looks_like_upstream_wrapper, resolve_evidence_body
from .core.quota import QuotaScope

logger = logging.getLogger(__name__)

#: What the storm filter would have said if it were switched off: print
#: everything, hold nothing back. A constant rather than a branch around the
#: logging block, so the switched-off path runs the same code as the other one
#: and cannot drift away from it.
_ALWAYS_LOUD = StormVerdict(speak_full=True)
#: Raised to mean "the user pressed the button", not "the provider failed".
#: Retrying these would ignore an interrupt, so they pass straight through.
_CONTROL_FLOW = (KeyboardInterrupt, SystemExit, GeneratorExit, InterruptedError)

#: Longest single sleep while the whole pool is resting. Slept in one-second
#: slices so an interrupt is honoured within a second, and re-checked after so
#: a key that recovers early is used immediately.
_SLEEP_SLICE_S = 1.0
_MAX_SLEEP_S = 60.0

# 1.8.2.3. Three hint families, one per truth an error can carry.
#
# TRANSIENT — the next call on a fresh key very likely succeeds: per-minute
# throttles, timeouts, server errors, connection resets, a dropped stream,
# an empty answer. Retrying is the right move and the agent should know it.
#
# QUOTA_WALL — a daily/monthly quota is genuinely spent. This is the v1/v2
# bug the design pass called out: a "retry and you will succeed" hint on a
# daily quota turns a clean failure into an agent retry loop, because the
# one thing that will NOT help until the quota rolls over is retrying.
# The hint says the opposite, in the same breath it names the wait.
#
# PERMANENT — auth, billing, permission, a model the key may not use: no
# amount of retrying repairs these, and the hint says so.
#
# Unknown kinds get NO hint: a confident wrong hint is worse than silence.
_TRANSIENT_KINDS = frozenset({
    "timeout", "server", "server_overload", "connection",
    "rate_limit", "empty", "",  # empty kind = unknown -> transient-shaped silence below
})
_QUOTA_WALL_KINDS = frozenset({
    "quota", "daily", "insufficient_quota",
})
_PERMANENT_KINDS = frozenset({
    "auth", "billing", "denied", "permission",
})
_RETRY_HINT = (
    "\n\n⚠️ This appears to be a temporary API issue.  "
    "Retrying the same tool call will very likely succeed."
)
_QUOTA_HINT = (
    "\n\n⛔ This is a spent daily/monthly quota, not a temporary failure.  "
    "Do NOT keep retrying this model now — switch model or wait for the quota "
    "to roll over."
)
_PERMANENT_HINT = (
    "\n\n⛔ This failure will repeat until its cause is fixed (a bad or "
    "unauthorised key, or a model this key may not use).  Retrying will not help."
)
_RETRY_HINT_MARKER = "Retrying the same tool call"
_QUOTA_HINT_MARKER = "Do NOT keep retrying"
_PERMANENT_HINT_MARKER = "Retrying will not help"


class KamePoolResting(Exception):
    """Every key of this model is resting for long and Hermes has another model.

    1.8.2.7 (decisions/0025, notes/2026-10-16-fallback-design-review.md). Raised
    instead of waiting, ONLY when the user set a fallback chain in Hermes and
    turned off "never fall back to another model". The shape is chosen so Hermes
    takes its fastest correct path: ``__init__._on_api_error_classification``
    maps this class to ``upstream_rate_limit`` (no host pool rotation, no pool
    gate, eager switch), and ``body["error"]["reset_at"]`` is where the host
    reads the cooldown that decides when it returns to the primary model.
    ``status_code`` is 429 so a hook that fails still reads a rate limit. No
    ``response`` and no ``retry_after`` on purpose: no header may add a backoff.
    """

    status_code = 429

    def __init__(self, message: str, reset_at: float) -> None:
        super().__init__(message)
        self.body = {"error": {
            "code": "kame_pool_resting",
            "type": "kame_pool_resting",
            "message": message,
            "reset_at": float(reset_at),
        }}


class KameAuxTimeout(TimeoutError):
    """A background job (a title, a summary...) used the budget KAME gives it and stops.

    1.8.2.9 (decisions/0032 A5). The shape is chosen for what the host does with it
    (``agent/auxiliary_client.py``, read on the real install): the TYPE NAME carries "Timeout", so
    ``_is_timeout_error`` is true; there is no ``status_code`` and the message holds none of the
    payment, rate-limit or auth words, so the host's credential rungs do not retry the call or
    rotate its own pool; title, compression and vision skip the same-provider retry and go to the
    task's fallback, then the user's own fallback list. KAME never picks that model. Never use
    ``KamePoolResting`` here: a 429 shape is retried and rotates the host's pool.
    """


class _StandIn:
    """A sibling model that answers this reply inside KAME instead of a hand-over to Hermes.

    1.8.3.0 (decisions/0033 D2 a). Returned by :meth:`KameTransport._maybe_hand_over` in place of
    ``KamePoolResting`` when the first usable later model of the user's list is on the same provider
    and endpoint, and the caller can take it. Hermes is never asked to switch, so the chat's model
    selector stays on the main model. ``identity`` carries the pool suffix, ``model`` is the name as
    the user wrote it, ``why`` completes "<model> ... - trying <next> (your list, best first)".
    """

    __slots__ = ("identity", "model", "why")

    def __init__(self, identity: str, model: str, why: str) -> None:
        self.identity = identity
        self.model = model
        self.why = why

    def target(self) -> Tuple[str, str]:
        return self.identity, self.model


def _with_retry_hint(exc: BaseException, kind: str = "", quota_window: str = "") -> BaseException:
    """Append the hint that matches what the error actually is.

    Never creates a new exception — modifies ``args`` in place so the
    exception type, status_code and traceback are preserved.  Idempotent per
    family: a hint already present is not doubled, and a re-surfaced error
    never migrates families (the first, correct, hint wins).

    ``quota_window`` is the classifier's own verdict (``per_day``,
    ``per_month``, ``unknown``). A ``quota`` kind with a per-day window is
    the wall; a quota kind with an unknown window is only *assumed* transient
    when the classifier called the refusal per-minute — the reason the kind
    carries the window's name when it knows it.
    """
    try:
        msg = str(exc)
        if _RETRY_HINT_MARKER in msg or _QUOTA_HINT_MARKER in msg or _PERMANENT_HINT_MARKER in msg:
            return exc
        hint = ""
        if kind in _QUOTA_WALL_KINDS or quota_window in ("per_day", "per_month"):
            hint = _QUOTA_HINT
        elif kind in _TRANSIENT_KINDS:
            # "rate_limit" with a daily window was caught above; an unknown
            # empty kind is transient-shaped (a bare connection error from a
            # provider that said nothing) but stays silent rather than
            # confidently wrong.
            if kind == "":
                return exc
            hint = _RETRY_HINT
        elif kind in _PERMANENT_KINDS:
            hint = _PERMANENT_HINT
        else:
            return exc
        exc.args = (msg + hint,) + exc.args[1:]
    except Exception:
        pass
    return exc

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

#: The provider's own word for *which* quota refused, put on the log line.
#: Only the period matters to a reader deciding whether a rest is sane: a
#: rolling window is seconds and a daily cap is hours, and Google's sentence
#: for the two is identical.
_WINDOW_TAG = {
    "per_minute": " {per-minute}",
    "per_day": " {PER-DAY}",
    "per_hour": " {per-hour}",
}


def _no_clock_fixes(body: Any, message: str) -> bool:
    """Whether a billing refusal names a missing entitlement, not an empty balance.

    Evidence only (R01): the structured code ``usage_not_included`` (the plan
    does not include this service) or the status ``FAILED_PRECONDITION`` (the
    free tier is not available where the account is). Both are read from the
    payload the provider sent, never from its name.
    """
    try:
        text = json.dumps(body, default=str) if isinstance(body, (dict, list)) else str(body or "")
    except Exception:
        text = str(body or "")
    text = (text + " " + str(message or "")).lower()
    return "usage_not_included" in text or "failed_precondition" in text


_NEVER_PROMOTED = frozenset(
    {"server", "timeout", "per_minute", "daily", "insufficient_quota", "auth",
     # 1.6.0.3, and it is the same defect the carousel's ladder had: this set
     # spoke only ``per_minute`` while ``classify.Verdict.reason`` says
     # ``rate_limit``. The carousel branch was taught both names in 1.4.0 and
     # this set was not, so for two years the commonest refusal there is could
     # walk straight past the exclusion that exists for it.
     #
     # What that cost is visible in the owner's 1.6.0.2 log: six times in
     # 46 minutes, every key in the pool answered ``rate_limit [429]``,
     # unanimity was read as proof that the *request* was at fault, and the
     # turn ended with a quota error on screen. The docstring below has always
     # claimed this cannot happen — "the case this plugin exists for, every
     # key spent, can never reach here" — and it was true only for a name the
     # classifier had stopped using.
     #
     # Unanimity is proof about the request only when the failure is one that
     # cannot pass on its own. A throttle passes on its own. That is what
     # waiting is for.
     "rate_limit",
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



# --- identities ---------------------------------------------------------

def _key_of(entry: Any) -> str:
    # Shared with ``pool_binding``, which fingerprints the same value on the
    # other side of the call to ask whether the bench is blaming the key that
    # actually went out. See ``core.multikey.key_on``.
    return multikey.key_on(entry)


#: 1.8.2.4. The request fields that carry prompt text, across the wires KAME
#: drives (chat ``messages``, Messages ``system``, Responses ``input``, Gemini
#: ``contents``, legacy ``prompt``).
_PROMPT_FIELDS = ("messages", "system", "input", "contents", "prompt")


def _estimate_prompt_tokens(request: Any) -> int:
    """About how many tokens the prompt carries — four characters to a token.

    1.8.2.3 passed ``len(request) // 4``. ``request`` is the kwargs *dict*,
    so that was the number of keyword arguments over four — zero or one for
    every call — and the hedging guard against doubling a huge prompt never
    fired. This walks every string inside the prompt fields instead. An
    inline image counts by its encoded size, which is right: a backup leg
    re-sends the image too.
    """
    if not isinstance(request, dict):
        return 0
    chars = 0
    stack: List[Any] = [request.get(name) for name in _PROMPT_FIELDS]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            chars += len(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return chars // 4


def counted_as(entry: Any, key: str) -> str:
    """The id this *key* is journalled under — never the row's, when they differ.

    One pool row can carry a comma-separated list, and ``candidates`` below
    maps every part of that list back to the same row. That is right for
    ``_apply_key``, which needs the row's base URL, and wrong for anything
    that counts: fourteen keys sharing one id are one credential as far as
    ``core.journal`` is concerned, and the journal is what feeds
    ``escalate.stretch`` — the only mechanism allowed to widen a bench on
    measured evidence.

    Two things broke on the owner's install because of it, both silently:

    * ``summarize`` groups blocks by ``(credential_id, model)`` and asks
      whether each one landed just after the *previous* block's deadline. With
      the fourteen merged into one series the previous block is a different
      key that refused a second earlier, its deadline is still a minute out,
      and the elapsed time is negative — so ``under_predictions`` could never
      reach the two it needs. Measured across 79 real blocks: zero.
    * ``_already_written`` drops a second row for the same ``(id, model)``
      inside ``ROTATION_DEDUPE_S``. Fourteen keys refusing about a second
      apart are, by that key, one refusal repeated — so most of them were
      never written down at all.

    The name is the one the split path already assigns
    (``core.multikey.child_id``), so a row written here lands in the same
    bucket as one written when the pool itself was split, and both sides of
    the journal finally speak about the same credential.
    """
    row = str(getattr(entry, "id", "") or "")
    if not row or not key:
        # No row means a key the pool does not know — a resolver
        # substitution, a fallback on ``agent.api_key``. ``record_block``
        # drops those, which is right: a statistic about a credential nobody
        # can name later teaches nothing.
        return row
    try:
        if multikey.key_on(entry) == key:
            # The row carries exactly this key. Its own id is already the
            # key's id, and inventing a second name for it would split one
            # credential's history in two.
            return row
        return multikey.child_id(row, key)
    except Exception:  # pragma: no cover - defensive
        return row


# --- watching the stream ----------------------------------------------------


class _Progress:
    """Tracks two different facts about one attempt, and never confuses them.

    The wrapper cannot count tokens — it does not own the stream any more than
    Agent Zero's v1.0.9 carousel does. It watches the callbacks instead, and
    every shim returns whatever the real callback returned, because Hermes uses
    those return values to control early stopping.

    * ``any`` — **the user has seen part of the answer.** This is the flag that
      forbids a plain retry, because a retry would print the same text twice.
      Only a delivery callback carrying a non-empty string can set it.
    * ``last_activity`` — **the stream is alive.** A reasoning token, a spinner
      frame, a tool name and a ``None`` sentinel all say yes, and not one of
      them is the answer.

    Until 1.6.0.3 both were the same ``touch()``, and the cost was measured on
    04/09/2026. Hermes fires ``on_first_delta`` on the first *reasoning* delta
    (``chat_completion_helpers.py:3937``) and on the first *tool name*
    (``:4039``), and routes its own spinner through ``thinking_callback``
    (``conversation_loop.py:2483``). On a thinking model — Gemini 3.8-flash —
    every turn opens with reasoning, so ``any`` was true before a single
    character of the answer existed. A 503 arriving there was read as a cut
    mid-answer, could not be stitched (there was nothing to continue from), and
    was handed back to Hermes as a failed turn. The host's own log for both of
    those turns says *"Streaming failed before delivery"*: it had delivered
    nothing at all.
    """

    __slots__ = (
        "any", "last_activity", "completed", "first_sign_at", "first_text_at",
    )

    def __init__(self) -> None:
        self.any = False
        self.last_activity = time.monotonic()
        self.completed = False
        #: When anything at all came back, and when the *answer* started —
        #: read only by ``timings``, which decides nothing. Kept apart for the
        #: same reason ``any`` and ``last_activity`` are: on a thinking model
        #: the first sign of life arrives long before the first character the
        #: user can read, and reporting one as the other would make every
        #: reasoning model look instant.
        self.first_sign_at: Optional[float] = None
        self.first_text_at: Optional[float] = None

    def touch(self) -> None:
        """Part of the answer reached the user. A retry would now duplicate it."""
        self.any = True
        self.last_activity = time.monotonic()
        if self.first_text_at is None:
            self.first_text_at = self.last_activity
        if self.first_sign_at is None:
            self.first_sign_at = self.last_activity

    def stir(self) -> None:
        """The stream is alive, and none of the answer has been shown yet."""
        self.last_activity = time.monotonic()
        if self.first_sign_at is None:
            self.first_sign_at = self.last_activity


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

#: A structured code or type that names a moderation refusal (1.8.1.4). Field
#: values only -- prose such as "blocked by" also names ordinary key denials.
_CONTENT_POLICY_FIELD = re.compile(r"content[\s_-]*(?:policy|filter)", re.I)


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


def _inflight_start(call: Any) -> None:
    """1.8.2.6: register a running call for the panel's "still waiting" line."""
    try:
        from .core.events import INFLIGHT as _inflight
        _inflight.start(call)
    except Exception:
        pass


def _inflight_end(call: Any) -> None:
    try:
        from .core.events import INFLIGHT as _inflight
        _inflight.end(call)
    except Exception:
        pass


def _emit(call: Any, kind: str, **fields: Any) -> Dict[str, Any]:
    """Record one event in the buffer this call belongs to.

    1.8.2.3: a call knows its channel (``_FacadeCall.channel`` — "main"
    for the chat model, "secondary" for auxiliary work), and the panel
    shows the two in separate tabs so a burst of title/vision traffic
    never buries the conversation's own rotations. Falls back to Main
    whenever the channel is absent, which is any caller that does not
    future path that forgets — a missed split degrades, it never throws.
    """
    try:
        from .core.events import INFLIGHT as _inflight
        _inflight.note(call, kind, str(fields.get("reason") or ""),
                       identity=str(fields.get("identity") or ""), fingerprint=str(fields.get("key") or ""))
    except Exception:
        pass
    try:
        if getattr(call, "channel", "main") == "secondary":
            from .core.events import SECONDARY_EVENTS as _secondary
            # 1.8.2.6: the row carries its task, so the Background tab can
            # name the job and filter by it (1.8.2.3 built the column, but
            # nothing ever wrote it).
            return _secondary.add(kind, task=str(getattr(call, "task", "") or ""), **fields)
    except Exception:
        pass
    return EVENTS.add(kind, **fields)


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

        # 1.8.2.9 (decisions/0031, step 0): a note for the heartbeat thread, not a snapshot
        # built and written on this thread while a model is being waited for.
        state.request_publish(binding, activity=activity)
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


_NOTIFY: Any = None


def _turn_status(kind: str, message: str) -> bool:
    """Hand ``message`` to the status rail of the turn this call belongs to.

    Hermes' ``agent.status_output.notify_turn_status`` (upstream #133474): the
    seam that lets a provider-supplied client say what it is doing, on the same
    surfaces 1.8.1.8 reached through ``agent._emit_status`` and
    ``agent._emit_wait_notice``. ``kind="activity"`` is the transient spinner
    line, ``"lifecycle"`` a status line. ``False`` on a Hermes without the seam
    or outside a turn; never raises.
    """
    global _NOTIFY
    if not _NOTIFY:
        # Looked up again while absent (1.8.2.0): the seam can appear after
        # KAME loads — the optional hermes-kame-bridge installs it at its own
        # registration — and an attribute read on an imported module is cheap.
        try:
            import agent.status_output as status_output

            _NOTIFY = getattr(status_output, "notify_turn_status", None) or False
        except Exception:
            _NOTIFY = False
    if not _NOTIFY:
        return False
    try:
        return bool(_NOTIFY(message, kind=kind))
    except Exception:  # pragma: no cover - documented never to raise
        logger.debug("kame: could not hand a line to the status rail", exc_info=True)
        return False


def status_rail_available() -> bool:
    """Whether this Hermes offers ``notify_turn_status`` (the spinner line is Hermes')."""
    _turn_status("lifecycle", "")  # resolves the import once; an empty line is never shown
    return bool(_NOTIFY)


class _Spinner:
    """Live status on the spinner line, throttled exactly as 1.8.1.8's.

    1.8.1.8 wrote this line through ``agent._emit_wait_notice``; a client has
    no agent, so it goes through :func:`_turn_status` with ``kind="activity"``,
    which the host routes to that same ``_emit_wait_notice``. The line is a
    transient ``thinking.delta``: it never becomes a message, so it cannot
    disturb message ordinals (rewind, edit, resend).

    The guards are 1.8.1.8's, unchanged:

    * **Diff** — the same text twice in a row is not re-sent, except after
      :attr:`_REFRESH_S`, because Hermes writes its own activity into the same
      spinner and a strict diff gate would never put KAME's line back.
    * **Interval** — at least ``interval`` seconds between updates, chosen by
      :meth:`cadence_for` from the countdown being drawn; a *changed* line only
      waits :attr:`_CHANGED_FLOOR_S`, so a finished countdown never stays on
      screen after a new wait began.

    Kept per conversation: the key is the client making the call (one per
    agent), so one chat's throttle cannot swallow another chat's line.
    """

    _DEFAULT_INTERVAL_S = 10.0
    _REFRESH_S = 30.0
    _MAX_TRACKED = 64
    _CHANGED_FLOOR_S = 1.0

    _lock = threading.Lock()
    _state: "OrderedDict[str, tuple]" = OrderedDict()

    @staticmethod
    def cadence_for(eta: Optional[float]) -> float:
        """How often a countdown showing ``eta`` deserves to be redrawn."""
        if eta is None:
            return _Spinner._DEFAULT_INTERVAL_S
        if eta <= 10.0:
            return 1.0
        if eta <= 60.0:
            return 5.0
        return 30.0

    @classmethod
    def reset(cls, owner: Optional[str] = None) -> None:
        """Forget what ``owner`` was last shown; everything, when it is not known here.

        A session reset names a session, and the throttle is normally kept per
        client: a name it does not hold clears every line, which costs at most
        one extra redraw elsewhere and never holds a line back.
        """
        with cls._lock:
            if owner is not None and str(owner) in cls._state:
                cls._state.pop(str(owner), None)
            else:
                cls._state.clear()

    @classmethod
    def update(cls, owner: str, text: str, *, interval: Optional[float] = None) -> None:
        if settings.is_on(settings.LIVE_STATUS_DISABLED):
            return
        every = cls._DEFAULT_INTERVAL_S if interval is None else interval
        now = time.monotonic()
        key = str(owner)
        with cls._lock:
            last_say, last_text = cls._state.get(key, (0.0, ""))
            if text == last_text and now - last_say < cls._REFRESH_S:
                return
            gate = cls._CHANGED_FLOOR_S if text != last_text else every
            if now - last_say < gate:
                return
            cls._state[key] = (now, text)
            cls._state.move_to_end(key)
            while len(cls._state) > cls._MAX_TRACKED:
                cls._state.popitem(last=False)
        # Outside the lock: the host's callback is foreign code.
        _turn_status("activity", text)


class _Vigil:
    """Tells the user a long wait is a wait, not a freeze.

    Agent Zero's carousel waits without a ceiling and without a word; ADR 0002
    records the consequence honestly — *"the user typically restarts A0 in that
    scenario"*. Restarting is not a decision the user made, it is one the
    silence made for them. A pool that hits its daily cap at 14:00 does not
    recover until the daily quota rolls over, and hours of a spinner is
    indistinguishable from a hang no matter how correct the waiting is.

    So the wait stays unbounded and stops being silent. 1.8.1.8 spoke through
    ``agent._emit_status``; a client has no agent to speak through, so the
    notice goes through :func:`_turn_status` (``kind="lifecycle"``), which the
    host routes to that same status rail. On a Hermes without that seam it
    goes to the log, and the wait itself is on the Desktop chip through
    ``_publish`` for as long as it lasts.
    """

    __slots__ = ("label", "started", "_next", "notices")

    def __init__(self, label: str) -> None:
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
        if not _turn_status("lifecycle", message):
            logger.info("kame: %s", message)


#: 1.8.2.7 (decisions/0027): a refusal that benches its key at least this long
#: (past any per-minute window) counts towards a probe burst.
_BURST_LONG_REST_S = 60.0

#: 1.8.2.7 (review H1): how long a conversation that was just handed over waits
#: instead of being handed over again. Long enough to stop a same-turn bounce,
#: short enough that the next minute's re-check hands over again.
_HANDOVER_MEMO_S = 30.0
# 1.8.2.8: what Hermes is told about the primary's return after a hand-over,
# so its fallback lasts one reply (the next reply restores the primary).
_HANDOVER_HOST_BACK_S = 2.0
#: 1.8.3.0 (decisions/0033 D1): after KAME hands a reply to Hermes, a call that arrives on one of the
#: entries it was handed to, within this long, is the same reply walking down the list. Hermes
#: rebuilds the message at once; a minute and a half covers its retries.
#: 1.8.3.1 (decisions/0034 D1): the permit carries the reply's ORIGIN, so the walk goes on down the
#: ladder of the model the reply started on, whichever model that was.
_WALK_PERMIT_S = 90.0

#: 1.8.3.1 (D1, review R3 of 1.8.3.0): ``entry -> (until, origin)`` for the walks Hermes starts ON ITS OWN -
#: the classification hook let a reply fall back without KAME handing it over (an error KAME passes
#: through). The call Hermes rebuilds on the next entry is the same reply and keeps the origin, exactly like
#: the walk after KAME's own hand-over (``KameTransport._walk_permits``).
_HOST_WALK_PERMITS: Dict[str, Tuple[float, str]] = {}


def _permit_origin(position: str, *tables: Dict[str, Tuple[float, str]]) -> Optional[str]:
    """The origin of the live walk permit that covers ``position``, or ``None``. The first table wins."""
    from .core.fallback_chain import _same

    now = time.time()
    for table in tables:
        for entry, (until, origin) in list(table.items()):
            if until > now and _same(position, entry):
                return origin
    return None


def _is_main_model(identity: str) -> bool:
    """Is ``identity`` the profile's main model (``model.default``)? (1.8.3.1, review R1/R2)"""
    try:
        from .core import fallback_chain

        main = fallback_chain.primary_identity()
        return bool(main) and fallback_chain._same(str(identity or "").strip().lower(), main)
    except Exception:
        return False


#: 1.8.3.1 (review R8): Hermes retries a failed reply up to ``agent.api_max_retries`` times, each a new call;
#: failures of the same conversation on the same model within this long are one failed reply, one row.
_FAILED_ROW_WINDOW_S = 90.0


def note_host_fallback(provider: Any, model: Any) -> None:
    """Hermes is about to move a reply from ``provider:model`` down the list: permit the walk.

    The permission is to the entries after the failing model in the ladder of the reply's origin - the
    failing model itself, unless a live permit says the reply started elsewhere. A permit that is
    still live is never rewritten: the first writer knows the true origin.

    1.8.3.1 (review R1): Hermes never walks TO the main model, so no permit is written for it, and a
    failure ON the main model starts a reply of its own (it never inherits another reply's origin). The
    permits KAME wrote itself at a hand-over (the live transport's) are read as well as the ones written
    here: a reply handed to another provider keeps its origin when the host walks on."""
    try:
        from .core import fallback_chain

        ident = f"{str(provider or '').strip().lower()}:{str(model or '').strip().lower()}"
        tables: List[Dict[str, Tuple[float, str]]] = [_HOST_WALK_PERMITS]
        try:
            from . import facade

            tables.append(facade.TRANSPORT._walk_permits)
        except Exception:   # no live transport (an import-time hook, a test): the module table alone
            pass
        origin = ident if _is_main_model(ident) else (_permit_origin(ident, *tables) or ident)
        until = time.time() + _WALK_PERMIT_S
        now = time.time()
        for entry in fallback_chain.after(origin, ident):
            if _is_main_model(entry):
                continue
            held = _HOST_WALK_PERMITS.get(entry)
            if held is None or held[0] <= now:
                _HOST_WALK_PERMITS[entry] = (until, origin)
    except Exception:  # pragma: no cover - bookkeeping must never break the hook
        logger.debug("kame: could not note Hermes' own fallback", exc_info=True)


#: 1.8.3.1 (D3): a reply that waited at least this long for a key says so in the popover's Last replies.
_REPLY_WAITED_NOTE_S = 5.0


def _short_why(why: str) -> str:
    """``busy`` / ``slow`` / ``resting`` / ``""``: the one word the Last replies sentence puts in brackets."""
    text = str(why or "").lower()
    if "slow" in text:
        return "slow"
    if "busy" in text:
        return "busy"
    if "resting" in text:
        return "resting"
    return ""


#: 1.8.2.8 (D11): "Automatic" slow-model watch, ``(floor, ceiling, cold)`` in
#: seconds, per class (plain, thinking).
_SLOW_S = {False: (20.0, 45.0, 30.0), True: (25.0, 60.0, 45.0)}
#: 1.8.2.8 (D12): when the next model of the list joins a silent reply.
_CROSS_MIN_S = 8.0
_CROSS_COLD_S = 12.0
_CROSS_STEP_S = 6.0
#: one more second of slow bound per this many prompt tokens (review R9)
_SLOW_TOKENS_PER_S = 4000.0
_CROSS_MAX_LEGS = 2

#: 1.8.2.8 (decisions/0028). A busy provider: failures of these kinds on two
#: different keys in one call, or a throttle whose rest is this short.
_PROVIDER_TIMEOUT_STATUSES = frozenset({408, 499, 504, 524})
#: How long a busy provider is left alone after a switch: 30 s, doubling while
#: the storm repeats, at most 5 minutes; an answer on it ends it at once.
_BUSY_FIRST_S = 30.0
_BUSY_MAX_S = 300.0
# 1.8.2.8 (D11, owner's live test): a SLOW model stays slow for minutes, and every
# re-check costs the user its whole slow bound (26 s live) - so its window is
# longer: 2 minutes, doubling while it stays slow, at most 10.
_SLOW_FIRST_S = 120.0
_SLOW_MAX_S = 600.0
#: "Automatic" model switch: about three times the model's usual first word,
#: within these bounds; the cold number before anything is learned.
_AUTO_SWITCH_MIN_S = 6.0
_AUTO_SWITCH_MAX_S = 20.0
_AUTO_SWITCH_COLD_S = 10.0
#: After a switch for slowness, the model you picked rests at least this long.
_HANDOVER_MIN_BACK_S = 30.0
#: 1.8.2.9 (decisions/0030): when the model in use is busy and nothing after Hermes' position
#: is free, the call circles back through the user's list. It is the half-open interval of a
#: circuit breaker: a model KAME knows busy, or one the call just left, is looked at again at
#: most this often by a circle. Between circles the model in use still rotates its own keys
#: (D2, unchanged), so this bounds the circle, not the request rate on that model.
_PROBE_EVERY_S = 20.0
#: Columns of the cost readout (``KameTransport._speed``): replies, then the extra requests by kind.
_SPEED_REPLIES, _SPEED_BACKUP, _SPEED_RACE, _SPEED_BURST, _SPEED_CIRCLE = 0, 1, 2, 3, 4
#: Rests that are not quota: they never count as "429 pressure" (D5).
_NOT_QUOTA_KINDS = frozenset({"server", "timeout", "other", "empty", None, ""})
#: 1.8.2.9 (decisions/0031 J3): the refusals that say a model's quota is spent or near it, and so
#: step the bounded Faster back to Balanced on that model. A 503 / timeout is not one of them.
_HEDGE_QUOTA_KINDS = frozenset({"per_minute", "rate_limit", "daily", "insufficient_quota"})
#: 1.8.2.9 (decisions/0032 B2): at the slow bound ONE more model of the user's list may join a silent
#: reply (with the two D12 legs, three raced models at most: the owner's list of four).
_WIDEN_MAX = 1


def _second_bound(slow_s: float, starts: Sequence[float]) -> float:
    """How long a reply with raced models keeps waiting, measured from the first request.

    1.8.2.9 (decisions/0032 B1). 1.8.2.8 gave the last raced model its WHOLE slow bound from its own
    start (``last start + slow_s``, 37-111 s with every model silent). A raced model now keeps the
    reply waiting only to its own "late" point (half the bound, at least ``_CROSS_MIN_S``); never
    shorter than the model's own bound, never longer than 1.8.2.8 would have waited.
    ``starts`` are the raced legs' start offsets from the first request.
    """
    if not starts:
        return slow_s
    last = max(starts)
    late = max(_CROSS_MIN_S, 0.5 * slow_s)
    return min(last + slow_s, max(slow_s, last + late))


def _short_secs(seconds: float) -> str:
    """``0.8 s``, ``4.2 s``, ``1 min 05 s`` for the reply notice."""
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest:02d} s"


_NOTICE: Any = None


def _turn_notice(message: str) -> bool:
    """A short on-screen notice for this turn (the bridge's ``notify_turn_notice``).

    Desktop / TUI only - the bridge says no where a notice would become a chat
    message. ``False`` without the bridge; never raises.
    """
    global _NOTICE
    if not _NOTICE:
        try:
            import agent.status_output as status_output

            _NOTICE = getattr(status_output, "notify_turn_notice", None) or False
        except Exception:
            _NOTICE = False
    if not _NOTICE:
        return False
    try:
        return bool(_NOTICE(message))
    except Exception:  # pragma: no cover - documented never to raise
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

#: The same rest, for a pool with barely anything in it.
#:
#: 1.1.3 exempted a pool of **one** key, on the reasoning that a rest whose
#: only job is to route the next request elsewhere buys nothing when there is
#: nowhere else. That reasoning does not stop at one. On a pool of two, the
#: full rest takes **half the pool** out for half a minute over a dropped
#: connection, and the owner's NVIDIA pool is exactly two:
#:
#:     kame: nvidia:moonshotai/kimi-k3 key:b65dd9 cut the answer after 568
#:           character(s) — resting it 30s and continuing on another key (1/10)
#:
#: The answer itself was fine — it continued on the other key and was
#: delivered whole. What is not fine is the half-minute afterwards, in which
#: one refusal on the surviving key leaves the pool with nothing usable and
#: the carousel waits. A dropped stream is the weakest evidence this module
#: acts on; it should not be able to do that.
#:
#: Five seconds is chosen against the sentence above: the rest exists so that
#: *the very next attempt* picks a different key, and the next attempt is
#: immediate. Five is already an eternity at that scale and still enough that
#: a genuinely flapping connection is not re-selected in a tight loop. Pools
#: of three or more keep the full thirty, because there the cost is a third of
#: the capacity rather than a half, and the extra caution is affordable.
DROP_REST_SMALL_POOL_S = 5.0

#: Below this many healthy keys, a dropped stream gets the short rest above.
#: Two, not three: at three the pool can lose one and still rotate.
DROP_SMALL_POOL_BELOW = 3


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

    **1.6.0.2 extends the same reasoning one step.** The exemption was written
    for a pool of one, and the argument it rests on — a rest that only exists
    to route the next request elsewhere should not cost more than that — does
    not stop at one. On a pool of two, thirty seconds is half the capacity
    withdrawn over a dropped connection, and one refusal on the survivor then
    leaves the carousel with nothing to pick. So a small pool gets
    :data:`DROP_REST_SMALL_POOL_S` instead: still long enough that the next
    attempt goes elsewhere, which the docstring above says is the entire
    point, and short enough that the pool is whole again before the next turn.
    A pool of three or more is unchanged.

    Only cooldowns of the *second* kind reach this function at all — a
    ``Retry-After`` or a daily quota is applied directly, because those bind
    whatever else is in the pool.
    """
    healthy = engine.healthy_count(identity, keys)
    if healthy <= 1:
        engine.mark(identity, key, False, 0.0, kind)
        return 0.0
    if healthy < DROP_SMALL_POOL_BELOW:
        seconds = min(seconds, DROP_REST_SMALL_POOL_S)
    return engine.mark(identity, key, False, seconds, kind)


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


class _ReplayStitcher:
    """An ORIGINAL-request tool replay can repeat even a tiny exact preamble.

    General continuation overlap stays conservative. Only an exact prefix of
    the known displayed preamble is removed here; changed text falls through
    to the existing stitcher without guessed edits.
    """
    def __init__(self, seen: str) -> None:
        self._seen = seen
        self._buffer = ""
        self._resolved = False
        self._delegate = stitch.Stitcher(seen)

    def feed(self, text: str) -> str:
        if self._resolved:
            return self._delegate.feed(text)
        self._buffer += text
        if self._seen.startswith(self._buffer):
            return ""
        buffered, self._buffer = self._buffer, ""
        self._resolved = True
        if buffered.startswith(self._seen):
            self._delegate = stitch.Stitcher("")
            return buffered[len(self._seen):]
        return self._delegate.feed(buffered)

    def flush(self) -> str:
        if not self._resolved:
            buffered, self._buffer = self._buffer, ""
            self._resolved = True
            if self._seen.startswith(buffered):
                return ""
            return self._delegate.feed(buffered) + self._delegate.flush()
        return self._delegate.flush()


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


def _looks_local(base_url: Any) -> bool:
    """Whether this endpoint is a model running on the same machine.

    Only asked in one place — the stream-silence timeout — and only to stay out
    of the way. Hermes raises the read timeout for local endpoints because a
    large context can take minutes to prefill before the first token, and a
    silence timeout that fires during that would make a local model unusable.
    """
    base = str(base_url or "").lower()
    return any(mark in base for mark in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal"))


#: 1.8.2.8 (decisions/0028, review H2): the automatic first-token wait, per
#: class - ``(floor, ceiling, before anything is learned)`` in seconds. The
#: ceiling is Hermes' own 120 s read timeout: "Automatic" only ever cuts a dead
#: connection SOONER than Hermes would, never later; a thinking model (whose
#: silent thinking can be long) is never cut sooner at all.
_AUTO_SILENCE_S = {False: (30.0, 120.0, 120.0), True: (120.0, 120.0, 120.0)}
#: A long prompt takes longer to read before the first word: one more second
#: of patience per this many prompt tokens.
_AUTO_SILENCE_TOKENS_PER_S = 2000.0


def auto_first_token_wait(identity: str, reasoning: bool, prompt_tokens: int = 0) -> float:
    """How long a key may stay silent in "Automatic": far past this model's usual.

    max(4 x median, 1.5 x p95) of its learned first-word times, plus a second
    per 2,000 prompt tokens, clamped to the class's floor and ceiling. The
    backup race (2 x median) is what makes a slow reply fast; this only ends a
    connection that has gone dead.
    """
    floor, ceiling, cold = _AUTO_SILENCE_S[bool(reasoning)]
    floor = floor + max(0, int(prompt_tokens or 0)) / _AUTO_SILENCE_TOKENS_PER_S
    try:
        from .core.hedging import HEDGE_TRACKER as _hedge

        stats = _hedge.first_word_stats(identity, reasoning=bool(reasoning)) if identity else None
    except Exception:
        stats = None
    if not stats:
        return float(min(ceiling, max(cold, floor)))
    median, p95 = stats
    return float(min(ceiling, max(floor, 4.0 * median, 1.5 * p95)))


def attempt_read_timeout(base_url: Any, attempt: int, *, identity: str = "", kwargs: Any = None,
                         streaming: bool = False) -> Optional[float]:
    """The per-attempt stream read timeout, or ``None`` to leave Hermes' own.

    1.8.2.8: ``first_token_wait`` decides. ``off`` = Hermes' own; ``timed`` =
    the 1.8.1.8 rules below on ``stream_silence_timeout_seconds``; ``auto``
    (the default) = :func:`auto_first_token_wait`, for streams only, halved
    from the third attempt on but never under its floor.

    1.8.1.8's ``_SilenceTimeout`` rules, unchanged: only when the user set
    ``stream_silence_timeout_seconds``, never over an explicit
    ``HERMES_STREAM_READ_TIMEOUT``, never for a local endpoint, and a quarter
    of the value from the third attempt on (1.2.5: the provider is proven slow,
    so the remaining keys get a shorter leash). 1.8.1.8 scoped it through a
    ContextVar read by a wrapped host reader; the client applies it directly to
    the request it owns.
    """
    mode = settings.first_token_mode()
    if mode == "off" or _looks_local(base_url):
        return None
    if os.environ.get("HERMES_STREAM_READ_TIMEOUT") is not None:
        return None
    if mode == "auto":
        if not streaming:
            return None
        try:
            from .core.hedging import reasoning_model as _is_reasoning

            reasoning = _is_reasoning(str((kwargs or {}).get("model") or ""), kwargs or {})
        except Exception:
            reasoning = False
        tokens = _estimate_prompt_tokens(kwargs or {})
        seconds = auto_first_token_wait(identity, reasoning, tokens)
        if attempt >= 3:
            seconds = max(_AUTO_SILENCE_S[bool(reasoning)][0], seconds * 0.5)
        return float(seconds)
    seconds = settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0)
    if seconds <= 0:
        return None
    if attempt >= 3:
        seconds = max(5.0, seconds * 0.25)
    return float(seconds)


class Call:
    """What one request needs from the client that is making it.

    The transport decides; the call knows the host. Kept as an explicit
    interface rather than a bag of callables so a test can build one in five
    lines and the facade cannot quietly grow a dependency the transport did
    not ask for.
    """

    identity: str = ""
    provider: str = ""
    kwargs: Dict[str, Any] = {}
    #: The wire this call speaks (``wires``); Chat Completions unless the client says otherwise.
    wire: Any = wires.CHAT
    #: 1.8.2.9: true when the host NAMED the background task (see ``_FacadeCall``); a guess is not.
    task_known: bool = False

    def attach_response(self, response: Any) -> None:
        """The HTTP response of the attempt now streaming, for a client that exposes it."""

    def status_owner(self) -> str:
        """Whose spinner line this call draws on (the throttle is kept per owner)."""
        return f"call:{id(self):x}"

    def keys(self) -> List[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def entry(self, key: str) -> Any:  # pragma: no cover - interface
        return None

    def entry_id(self, key: str) -> str:
        entry = self.entry(key)
        return str(getattr(entry, "id", "") or "") if entry is not None else ""

    def client_for(self, key: str, attempt: int) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def cancelled(self) -> bool:
        return False

    def cancel_error(self) -> BaseException:
        return InterruptedError("the request was cancelled")

    def cut(self, error: Optional[BaseException]) -> BaseException:
        """What to raise once part of an answer is on screen and the call cannot go on.

        Hermes turns an exception that arrives after visible text into its own
        partial-stream stub and continues from there, which is what 1.8.1.8's
        ``answer_so_far`` handed it too.
        """
        if error is not None:
            return host_text.take_off_the_advice(error)
        return ConnectionError("kame: the answer could not be continued on any key")


# --- the transport ----------------------------------------------------------


class KameTransport:
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
        self._timeout_reader_original: Optional[Callable] = None
        self.installed = False
        self.reason = "not installed"
        self.engine = engine or ENGINE
        # 1.8.2.4: hedge legs that lost the race, waiting to be accounted for
        # on the thread that drives calls (``_drain_hedge_losers``). A deque:
        # ``append`` from a worker thread and ``popleft`` here are atomic.
        self._hedge_losers: "deque[Tuple[Any, ...]]" = deque()
        # 1.8.2.5: models whose stream trigger was seeded from calls.jsonl,
        # and that file's first-word history, read once.
        self._race_seeded: set = set()
        self._first_word_history: Optional[Dict[str, List[float]]] = None
        # For ``/kame-quota``: an install that has never rotated and one that
        # is silently inert are otherwise indistinguishable.
        self.calls = 0
        # 1.6.0.2. Wall clock of the last call this binding saw, or 0.0.
        #
        # The Events tab records failures and rotations, which means a healthy
        # stretch draws an empty screen — and an empty screen is exactly what a
        # broken one draws. The owner read one as the other and reported the
        # panel as frozen; it was not, it was quiet, and nothing on it could
        # tell the two apart. A count with no timestamp cannot either: 53 calls
        # is the same number whether the last one was a second ago or before
        # lunch. This is the one fact that separates them.
        self.last_call_at = 0.0
        self.rotations = 0
        self.recovered = 0
        self.surfaced = 0
        #: 1.8.2.7. Calls handed to Hermes' fallback chain instead of waiting,
        #: and per identity until when a second ask must wait (see
        #: :meth:`_maybe_hand_over`).
        self.handed_over = 0
        self._handover_until: Dict[str, float] = {}
        #: 1.8.2.8 (decisions/0028): identity -> until when its provider is
        #: known busy, and the length of that window (30 s, doubling while the
        #: storm repeats, at most 5 min). Cleared by any answer on it.
        self._busy_until: Dict[str, float] = {}
        self._busy_window: Dict[str, float] = {}
        #: 1.8.2.9: chain entry -> when it was last asked while known busy (or left
        #: because it was), so the circle back (:meth:`_wrap_target`) knocks once per
        #: ``_PROBE_EVERY_S``. ``wrapped`` counts the circles.
        self._probed_at: Dict[str, float] = {}
        self._busy_kind: Dict[str, str] = {}
        #: 1.8.2.9 (decisions/0031 J5b): minute -> [replies, backup, race, burst, circle]; the totals
        #: since this process started. Integer adds only; read by the publisher thread.
        self._speed: Dict[int, List[int]] = {}
        self._speed_total: List[int] = [0, 0, 0, 0, 0]
        self._probe_lock = threading.Lock()
        self.wrapped = 0
        #: 1.8.2.9 (decisions/0032 A): (identity, job class) -> until when a background job that
        #: was stopped is answered "stopped" without a request, and how many jobs of each task
        #: were stopped early. Dict writes on the stop path only.
        self._aux_memo: Dict[Tuple[str, str], float] = {}
        self._aux_stopped: Dict[str, int] = {}
        #: 1.8.2.9 (decisions/0032 A7): hour -> identity -> [chat requests, background requests], and
        #: hour -> (identity, task) -> background requests. An integer add where a request starts;
        #: 24 hourly buckets. Read by the publisher thread (``quota_use_view``).
        self._requests: Dict[int, Dict[str, List[int]]] = {}
        self._request_tasks: Dict[int, Dict[Tuple[str, str], int]] = {}
        self._requests_since = time.time()
        #: 1.8.2.8: the last replies of the conversation - which model wrote
        #: each and how long it took - for the panel.
        self.replies: "deque[Dict[str, Any]]" = deque(maxlen=30)
        #: memo key -> the chain entries Hermes could switch to at that hand-over.
        #: A call that arrives on one of them proves Hermes really switched, so the
        #: memo is dropped (decisions/0027): a later ask is a new turn, handed over
        #: again at once instead of waiting for the keys.
        self._handover_later: Dict[str, Tuple[str, ...]] = {}
        #: 1.8.3.0 (decisions/0033 D1): chain entry -> until when a call arriving on it is the walk of a
        #: reply that started on the main model (set where a main reply is handed to Hermes).
        self._walk_permits: Dict[str, Tuple[float, str]] = {}
        #: 1.8.3.1 (review R4): origin identity -> the model string Hermes sent for the first call of a reply
        #: that started there, as the user wrote it (the pill goes back to this, never to a lower-cased guess).
        self._origin_written: Dict[str, str] = {}
        #: 1.8.3.1 (review R8): ``conversation|model -> (when, row)`` of the failed-reply row written last.
        self._failed_rows: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        #: Probe bursts started (decisions/0027).
        self.probe_bursts = 0
        #: ``(identity, key) -> when the provider said this key is back`` for the
        #: last refusal that came with the provider's own number. The rest KAME
        #: applies is capped (``max_hold_seconds``, the owner's one hour); this
        #: remembers what the provider actually said, so a key whose rest ran out
        #: before that moment is known to be most likely still spent
        #: (:meth:`_known_dry`). Decides only how many keys are asked at once.
        self._stated_back: Dict[Tuple[str, str], float] = {}
        #: 1.8.1.2. ``(identity, key)`` whose latest refusal was a billing
        #: refusal that no clock fixes (see :func:`_no_clock_fixes`).
        self._no_clock_fix: Dict[Tuple[str, str], bool] = {}
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

    def _pool_agrees_no_clock_fixes_it(
        self,
        identity: str,
        verdicts: Dict[str, Tuple[str, Optional[int]]],
        keys: Sequence[str],
        kind: str,
    ) -> bool:
        """1.8.1.2 (owner decision): end the turn on an entitlement refusal.

        Credit and spend exhaustion keep waiting, as ``_NEVER_PROMOTED``
        says — time or a top-up fixes them. Two billing refusals are
        different: the plan does not include the service
        (``usage_not_included``) or the account's country needs billing
        (``FAILED_PRECONDITION``). No wait ever fixes those, and the eternal
        carousel would sit on them forever with nothing on screen. Same proof
        as the unanimity rule: every key tried this run, every one refused
        this way, none answered. Each key still keeps its one-hour rest.
        """
        if kind != "insufficient_quota":
            return False
        pool = [k for k in keys if k]
        if not pool or len(verdicts) < len(pool):
            return False
        return all(verdicts.get(k, ("",))[0] == "insufficient_quota"
                   and self._no_clock_fix.get((identity, k), False) for k in pool)

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

        The last condition is what keeps the eternal carousel eternal, and it
        now runs off ``vocabulary.may_end_turn`` rather than a list of failures
        that may not. The old list leaked by default: three of its nine entries
        were produced by nothing, and three words that *are* produced --
        ``other``, ``auth_permanent``, ``billing`` -- were missing from it and
        so ended turns. Reversing it is what stops a fourth word doing the same.

        The allowlist is empty on purpose. A request no key could answer never
        reaches this function: ``carousel.is_terminal`` stops those upstream, on
        the request-shaped statuses and the content-policy refusals. Anything
        arriving here already passed that judgment, and overruling it later on
        weaker evidence is how six turns died with a quota error on screen.
        """
        if not vocabulary.may_end_turn(kind):
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
        credential_id: str = "",
        call: Any = None,
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


        # Write down what the provider actually sent, before anything reads it.
        #
        # 1.7.0.0 put this call in the classification hook only, and the
        # owner's first real session showed what that misses: 79 refusals in
        # the quota journal over the same eighteen minutes, and **one** line in
        # `refusals.jsonl`. Hermes fires `transform_api_error_classification`
        # for a fraction of what fails; the rest is refused here, inside a
        # turn, where the pool rotates without the host's hook running at all.
        #
        # So the instrument was reading one lane of two, and the quieter one.
        # The corpus this plugin is measured against was built from logs with
        # the same blind spot, which is part of why it has thirteen thousand
        # refusals and not one `quotaId`.
        #
        # `recorder.record` never raises and never decides anything. Deleting
        # the module changes no behaviour — which is the point of it existing.
        recorder.record(
            provider=identity.split(":", 1)[0] if ":" in identity else identity,
            model=identity,
            status_code=ev.status_code,
            # The raw message, not `ev.message`: the cleaned copy has Hermes'
            # own appended paragraph taken off, and a recording that has
            # already been edited cannot answer a question about the edit.
            error_message=exc_str,
            error_body=ev.body,
            error=exc,
            error_type=type(exc).__name__,
        )

        if ev.status_code == 421:
            from .core.provider_rules import api_surface
            if api_surface(exc) == "xiaomi_chat":
                # https://mimo.mi.com/docs/en-US/api/guidance/error-codes
                # Preserve the observation above without penalizing a key for
                # a request rejected by this endpoint's content filter.
                return "raise", "content_filter", ev.status_code

        # A relayed failure belongs to the upstream provider, not this key.
        # Core deferral alone is insufficient here: the legacy fallback below
        # would reread the upstream auth/quota prose and bench or retire the
        # aggregator credential. Preserve the original exception for Hermes'
        # model/provider fallback without changing this credential's health.
        body = resolve_evidence_body(ev.body, exc)
        envelope = body.get("error") if isinstance(body, dict) else None
        metadata = envelope.get("metadata") if isinstance(envelope, dict) else None
        if isinstance(metadata, dict) and (
            "flagged_input" in metadata or "reasons" in metadata
            or metadata.get("error_type") == "content_policy_violation"
        ):
            return "raise", "content_filter", ev.status_code
        # 1.8.1.4: the same fact in the provider's own code/type field, not only
        # in OpenRouter's metadata. OpenAI files a moderation refusal as
        # ``code: "content_policy_violation"``, Azure as ``code:
        # "content_filter"`` -- and on a 403, ``is_terminal`` reads auth first,
        # so the legacy table below called it ``auth``: the key rested 20s and
        # the flagged prompt was sent again on every other key in the pool.
        # ``classify`` already declined it (test_v1_8_0_0_same_code:
        # "surfaces as request fault"); the request, not the key, is the fault.
        from .core.classify import structured_error_values
        structured = structured_error_values(body, exc, None)
        if any(_CONTENT_POLICY_FIELD.search(str(value)) for value in structured):
            return "raise", "content_filter", ev.status_code
        # 1.8.1.4: a field that certainly names the REQUEST as the fault --
        # ``context_length_exceeded``, ``model_not_found`` and the other certain
        # TERMINAL rows -- ends the turn before any prose is weighed. ``classify``
        # already declines these; what followed it (the legacy table and
        # ``is_terminal``) still read the words, and a stray "quota" or "429" in
        # them made an oversized prompt rotate every key. The Agent Zero port
        # has checked this first since 1.8.1.0 (``kame_evidence.catalog_terminal``).
        from .core.catalog import TERMINAL as _TERMINAL, look_up as _look_up
        _reading = _look_up(*structured) if structured else None
        if _reading is not None and _reading.family == _TERMINAL and getattr(_reading, "certain", False):
            return "raise", "other", ev.status_code
        if looks_like_upstream_wrapper(body):
            return "raise", "upstream_error", ev.status_code

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

        try:
            aws_response = getattr(exc, "response", None)
        except Exception:
            aws_response = None
        aws_error = aws_response.get("Error") if isinstance(aws_response, dict) else None
        model_not_ready = (type(exc).__name__ == "ModelNotReadyException" or
            (isinstance(aws_error, dict) and aws_error.get("Code") == "ModelNotReadyException"))
        if verdict is None and ev.status_code == 429 and model_not_ready:
            # Bedrock uses429 for model readiness as well as throttling.
            # Keep SDK/host retry ownership; another credential does not make
            # this model ready. Do not manufacture per-minute quota below.
            return "raise", "model_not_ready", ev.status_code
        if (provider_quirks.names_maintenance(ev.status_code, message, ev.body)
                and not self._chain_would_switch(identity)
                and getattr(call, "channel", "main") == "main"):
            # 1.8.2.7: a 503 whose code is `maintenance` is a closed door for
            # every key of this model; walking the pool only multiplies the wait.
            return "raise", "model_not_ready", ev.status_code

        if verdict is None and ev.status_code == 401:
            from .core.provider_rules import api_surface, reading
            from .core.catalog import AUTH_REFRESH, look_up
            from .core.classify import structured_error_values
            resolved = resolve_evidence_body(ev.body, exc)
            owner = reading(api_surface(exc), resolved) or look_up(*structured_error_values(resolved, exc, None))
            if owner is not None and owner.family == AUTH_REFRESH:
                # Let Hermes/its adapter refresh authentication before spending
                # or retiring any credential. Core deferral must survive fallback.
                return "raise", "auth_refresh", ev.status_code

        window_number_declined = False
        if verdict is not None:
            delay = max(0.0, verdict.reset_at - time.time()) if verdict.reset_at else 0.0
            kind = verdict.reason
            if kind == "billing":
                kind = "insufficient_quota"
                delay = self.engine.daily_cooldown_s
                self._no_clock_fix[(identity, key)] = _no_clock_fixes(resolve_evidence_body(ev.body, exc), message)
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
            elif getattr(verdict, "quota_window", "") == "per_day":
                # 1.7.0.0. **A day is not a minute, and the provider will not
                # tell you which one you are in unless you ask the right
                # field.** Google reports its per-minute and its per-day
                # free-tier quotas under the *identical* metric name, and both
                # arrive as a 429 whose prose is word-for-word the same. Only
                # ``quotaId`` separates them, and its retry hint does not:
                # measured on the owner's own keys, 04/09/2026, a genuine
                # ``GenerateRequestsPerDayPerProjectPerModel-FreeTier`` — the
                # day's twenty requests gone until midnight Pacific — came
                # back carrying ``retryDelay: "29s"``.
                #
                # Until now nothing read that field here. The verdict said
                # ``per_day``, this cascade had no branch for it, and the
                # refusal fell through as an ordinary ``rate_limit`` sized by
                # the provider's twenty-nine seconds. So fourteen keys that
                # were out of requests for the rest of the day were re-probed
                # every twenty seconds all night, each probe spending a request
                # against a quota that had none left to spend.
                #
                # The rest is the daily cooldown, and the stated delay is
                # discarded on purpose — that is the whole reason
                # ``daily_quota_cooldown_seconds`` exists and says so in its
                # own help text. A deadline the provider genuinely stated for
                # the *day* is still honoured when it is longer, because that
                # one is a real number about a real reset.
                #
                # 1.7.0.2 stops this line from being the whole decision. The
                # `max()` below said "an hour, unless the provider named
                # longer", and it fired on every one of the owner's 114 daily
                # refusals on 2026-09-06 — with `verdict.source == "window"`
                # every time, which is `quota`'s own word for *this number is
                # KAME's default, not something anybody stated*. So an invented
                # 3600 arrived at `mark` wearing `stated=True`, and every
                # sizing rule downstream, including the probe 1.7.0.1 had just
                # added, was handed a number it could only agree with.
                #
                # Measured consequence: keys benched for the hour answered
                # again 6 to 36 minutes later, twenty-one times, and the owner
                # had to reset the pool by hand to get them back.
                #
                # So when the number is ours, hand the carousel what the
                # provider actually said — usually a small retry hint, often
                # nothing — and let it decide, because it is the only place
                # that can see whether the rest of the pool is still
                # answering. When the number is the provider's (OpenRouter
                # states a real nine-hour reset) nothing here touches it.
                kind = "daily"
                if getattr(verdict, "source", "") == "window":
                    try:
                        from .core.carousel import extract_delay as _stated

                        delay = float(_stated(exc, message, ev.headers) or 0.0)
                    except Exception:
                        delay = 0.0
                    # And say so. `verdict.source` would still read "window",
                    # which is `quota`'s word for the default this branch just
                    # discarded — a log that names a source the number did not
                    # come from is the same small lie `mark` refuses to tell
                    # when it returns what was stored rather than what was
                    # asked for.
                    window_number_declined = True
            status = ev.status_code
            sized_by = (
                "reprobe" if window_number_declined else (verdict.source or "verdict")
            )
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
        # 1.8.2.7: the few places where a provider keeps its meaning in the
        # status and says nothing in the body (see core/provider_quirks.py).
        _quirk = provider_quirks.reinterpret(ev.status_code, kind, message, ev.body)
        if _quirk in ("billing", "per_minute", "server") and ev.status_code == 429:
            # A number the provider STATED outranks a number a text rule would invent.
            try:
                from .core.carousel import extract_delay as _xd
                if _xd(exc, message, ev.headers):
                    _quirk = None
            except Exception:
                pass
        if _quirk == "request_fault":
            # A 5xx whose structured code says the REQUEST is invalid: no key helps.
            return "raise", "other", ev.status_code
        if _quirk == "denied":
            kind, delay, status = "denied", DENIED_REST_S, (status or ev.status_code)
            sized_by = "table"
        elif _quirk == "auth":
            kind, delay, status = "auth", REJECTED_REST_S, (status or ev.status_code)
            sized_by = "table"
        elif _quirk == "billing":
            kind, delay, status = "insufficient_quota", self.engine.daily_cooldown_s, (status or ev.status_code)
            sized_by = "table"
        elif _quirk == "per_minute":
            # A "quota" error that names a per-minute / concurrency counter:
            # a throttle (the unsized-throttle rest), not an empty balance.
            kind, delay, status = "per_minute", settings.number(settings.UNSIZED_THROTTLE_REST, 30.0), (status or ev.status_code)
            sized_by = "table"
        elif _quirk == "server":
            from .core.carousel import SERVER_BASE_S as _SERVER_BASE_S, extract_delay as _stated_for_quirk
            try:
                _stated_s = float(_stated_for_quirk(exc, message, ev.headers) or 0.0)
            except Exception:
                _stated_s = 0.0
            kind, delay, status = "server", (_stated_s if _stated_s > 0 else _SERVER_BASE_S), (status or ev.status_code)
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
        # A proven billing verdict is a credential problem even on HTTP 400.
        # Do not let the weaker status-only fallback discard that evidence.
        if (is_terminal(exc, message) and _quirk != "denied"
                and not (verdict is not None and kind == "insufficient_quota")):
            logger.info(
                "kame: %s %s — the request itself was refused, not the key", label, type(exc).__name__
            )
            return "raise", kind, status
        # 1.7.0.0. Captured here because ``verdict`` is rebound to the storm
        # filter's verdict a few lines below, and this is the last point where
        # the classifier's own answer is still in scope.
        quota_window = getattr(verdict, "quota_window", "") if verdict is not None else ""
        # Did the provider name this number, or did we? The ladders need to
        # know, and the answer is one cheap read of the same evidence the
        # classifier already saw.
        try:
            from .core.carousel import extract_delay as _stated_delay
            stated = bool(_stated_delay(exc, message, ev.headers))
        except Exception:
            stated = False
        # 1.8.1.2. The classifier already read this refusal with the one
        # canonical cascade (``core.quota``, R16), and its ``source`` says
        # where its number came from (``vocabulary.provider_timed`` is the one
        # function that answers this). When that source is the provider's own
        # answer — an SDK attribute, a header, a body field, the prose — the
        # number was stated, whatever the narrower legacy reader above could
        # parse. 1.8.1.1 asked only the legacy reader, which needs the word
        # "retry": "Please try again in 7s" and Codex's
        # ``resets_in_seconds: 12660`` were both treated as unsized and rested
        # 30s (R12). A number KAME derived itself (``window``) stays unstated.
        if not stated and verdict is not None and getattr(verdict, "reset_at", None):
            stated = vocabulary.provider_timed(getattr(verdict, "source", ""))
        calendar_reset =(verdict is not None and getattr(verdict, "window_scoped_reset", False) and delay > 0)
        # ``verdict.quota_scope`` is ``core.classify.classify()``'s own
        # ``detect_quota_scope`` reading of THIS refusal — untouched by the
        # ``kind`` relabelling above (billing/revoked/denied all keep the
        # scope their classification actually found). The legacy
        # ``core.carousel.classify()`` fallback (``verdict is None``, above)
        # computes no scope at all, so it stays the pre-existing default,
        # ``QuotaScope.UNKNOWN`` — R01: no scope is ever invented here from
        # the provider's name.
        account_scope = getattr(verdict, "quota_scope", QuotaScope.UNKNOWN) if verdict is not None else QuotaScope.UNKNOWN
        # 1.8.1.0. The doubling experiment applies to one payload only, and
        # what makes it that payload is judged here, from the evidence this
        # refusal carried — never from the provider's name (R01).
        bare = is_bare_resource_exhausted(ev, stated=stated)
        if (kind in ("rate_limit", "per_minute") and not stated and not calendar_reset
                and quota_window in ("", "unknown")):
            # This number was a classifier default, not a provider deadline.
            # Let the engine apply learned timing, the bare-Gemini ladder or
            # the configured unsized dial instead of laundering default 20s.
            delay = 0.0
        # 1.8.2.3: a per-minute refusal is the one event that teaches the
        # pacer this scope's real RPM. Learned here, never raised later — see
        # Pacer.on_minute_429. This runs before the bench so the not_before
        # the pacer sets and the rest the carousel holds agree on which
        # window just refused.
        if kind in ("rate_limit", "per_minute"):
            try:
                from .core.pacing import PACER as _pacer_429
                _pacer_429.on_minute_429(identity, key)
            except Exception:
                pass
        if calendar_reset:
            applied = self.engine.mark(identity, key, False, delay, kind,
                                       stated=True, calendar_reset=True, scope=account_scope)
        else:
            applied = self.engine.mark(identity, key, False, delay, kind, stated=stated, scope=account_scope,
                                       bare_resource_exhausted=bare)
        try:  # decisions/0027: what the provider said, uncapped, for _known_dry
            if (stated or calendar_reset) and delay > 0:
                self._stated_back[(identity, key)] = time.time() + float(delay)
            else:
                self._stated_back.pop((identity, key), None)
        except Exception:  # pragma: no cover - a dict write
            pass
        retained = bool(getattr(applied, "retained", False))
        # 1.8.1.0. Read back off the engine after ``mark()``, the same way
        # ``rest_s`` is read back for ``timings.record`` below, rather than
        # threaded through as another return value: the streak this reports
        # is exactly what ``_escalate`` just incremented (or reset) for this
        # credential+model, so there is nothing to compute here, only to ask
        # for. Empty when the rest did not come from the ladder — the flag
        # is off, a stated number governed instead, this is not an unsized
        # throttle at all, or the prior hold was retained instead of a fresh
        # one being sized — and both ``sized_by`` and the log line below then
        # read exactly as they did before this release.
        backoff_label = "" if retained else self.engine.unsized_backoff_label(identity, key)
        if retained:
            sized_by = "retained"
        elif backoff_label:
            sized_by = backoff_label
        # 1.6.0.3. Write it down. This is the path almost every refusal takes
        # — the key is benched on the carousel and the turn moves to the next
        # credential without the host's pool being told — and until now the
        # journal never saw one of them: 74 rotations and 0 rows on the
        # owner's 1.6.0.2 build. What reads the journal is
        # ``escalate.stretch``, the only thing allowed to widen a bench on
        # measured evidence, so it could not fire where it was needed most.
        #
        # ``applied`` and not ``delay``: the carousel is what actually holds
        # the key, and a deadline the journal did not see applied is a
        # prediction measured against a bench that never happened.
        # Retaining a prior hold is not a new prediction. Keep the journal's
        # original episode rather than reattributing it to this shorter hint;
        # the event log still records this refusal and its actual remaining wait.
        if credential_id and not retained:
            provider_name = identity.split(":", 1)[0] if ":" in identity else identity
            now_epoch = time.time()
            runtime.record_rotation(
                credential_id=credential_id,
                provider=provider_name,
                model=identity,
                held_to=getattr(applied, "held_until", now_epoch + applied) if applied and applied > 0 else None,
                status_code=status,
                reason=kind,
                # A ``runtime.Judgement``, not the ``classify.Verdict`` above.
                # The journal's recorder reads ``window`` / ``source`` /
                # ``reset_at`` / ``stated`` off a Judgement, and the Verdict
                # spells the first of those ``quota_window``; handing it over
                # directly would have every row read "unknown" while looking
                # like it was recording something. They are different types
                # for a reason — one is what the classifier concluded, the
                # other is what the pool was told — so the translation is
                # explicit rather than duck-typed.
                judgement=runtime.Judgement(
                    provider=provider_name,
                    model=identity,
                    window=getattr(verdict, "quota_window", "unknown") if verdict is not None else "unknown",
                    source="retained" if retained else ((getattr(verdict, "source", "") if verdict is not None else "") or sized_by),
                    reset_at=None if retained else (getattr(verdict, "reset_at", None) if verdict is not None else None),
                    at=now_epoch,
                    scope=getattr(verdict, "quota_scope", "unknown") if verdict is not None else "unknown",
                    reason=kind,
                ) if verdict is not None else None,
            )
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
            # 1.7.0.0 puts the quota's *period* on the line, which is the one
            # thing about a 429 that the sentence itself cannot tell you.
            # Google's per-minute and per-day free-tier refusals are the same
            # words with the same metric, and the whole cost of this release
            # was that nobody could see which one they were reading. The Agent
            # Zero engine printed the same tag from v1.0.6 and said why: so a
            # line that reads "daily" beside a tag reading "per_minute" is a
            # misclassification anyone can spot without turning on debug.
            window = _WINDOW_TAG.get(quota_window, "")
            # 1.8.1.0. The owner is going to read this line to decide whether
            # the doubling experiment is doing anything, and a 4s rest from
            # the ladder has to be told apart from a 4s rest that came from
            # anywhere else — a stated delay, a remembered ceiling, the flat
            # dial at 4. ``backoff_label`` is only ever non-empty when the
            # ladder itself produced ``applied``, so the tag names the exact
            # rung rather than restating the number already on the line.
            ladder_tag = f" [{backoff_label}]" if backoff_label else ""
            logger.warning(
                "kame: %s %s %s%s%s — resting %s%s, taking the next key (attempt %d)",
                label,
                fingerprint(key),
                kind,
                f" [{status}]" if status else "",
                window,
                format_duration(applied),
                ladder_tag,
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
            _emit(call, 
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
            _emit(call, 
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
            _emit(call, 
                "stream_drop",
                identity=identity,
                key=fingerprint(key),
                reason=f"{kind or 'the connection'} failed mid-answer",
                code=status,
                detail=host_text.without_advice(ev.raw_message),
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
                _emit(call, 
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
        _emit(call, 
            "quarantine" if applied >= 60.0 else "rotation",
            identity=identity,
            key=fingerprint(key),
            reason=kind or "refused",
            code=status,
            seconds=applied,
            # Redacted inside `Events.add`, never here — the scrub belongs to
            # the store, so no caller can forget it. What *is* done here is
            # taking off the paragraph Hermes appends: this row is opened
            # under "What the provider actually sent", and the appended
            # paragraph is not something the provider sent.
            detail=host_text.without_advice(ev.raw_message),
            sized_by=sized_by,
        )
        return "rotate", kind, status

    installed = True
    reason = "active"

    #: Longest gap between two chunks Hermes sees while this transport is busy
    #: on its behalf — waiting for a key, or opening the next one. Hermes kills
    #: a stream that stays silent for its stale timeout (180 s by default,
    #: ``HERMES_STREAM_STALE_TIMEOUT``) and every chunk resets that clock, so a
    #: keep-alive this often keeps an hour-long quota wait from being read as a
    #: hung provider.
    KEEPALIVE_S = 15.0

    # -- one call ------------------------------------------------------------

    def complete(self, call: "Call") -> Any:
        """A non-streaming request, on as many keys as it takes."""
        _inflight_start(call)
        try:
            for item in self._drive(call, stream=False):
                if isinstance(item, _Done):
                    return call.wire.result(item.value)
        except Exception as error:
            self._note_failed(call, error)
            raise
        finally:
            _inflight_end(call)
        raise RuntimeError("kame: the transport ended without an answer")  # pragma: no cover

    def stream(self, call: "Call") -> Iterator[Any]:
        """A streaming request. Yields provider chunks, plus keep-alives while it waits.

        On the Messages and Responses wires the chunks are that wire's own
        events (``wires``): the carousel decides on views of them, and the wire
        hands Hermes what it expects.
        """
        return call.wire.emit(self._views(call))

    def _views(self, call: "Call") -> Iterator[Any]:
        _inflight_start(call)
        try:
            for item in self._drive(call, stream=True):
                if isinstance(item, _Done):
                    return
                if item is _KEEPALIVE:
                    yield keepalive_chunk()
                    continue
                yield item
        except Exception as error:
            self._note_failed(call, error)
            raise
        finally:
            _inflight_end(call)

    def _plain(self, call: "Call", stream: bool) -> Iterator[Any]:
        """No key to choose: the request goes out exactly as Hermes built it."""
        client = call.client_for("", 1)
        if not stream:
            yield _Done(call.wire.create(client, call.kwargs))
            return
        for chunk in call.wire.open(client, call.kwargs, call):
            yield chunk
        yield _Done(None)

    def _drive(self, call: "Call", *, stream: bool) -> Iterator[Any]:
        """One API call, as many keys as it takes — ``DispatchBinding.run`` (1.8.1.8), at the client."""
        try:
            keys = call.keys()
        except Exception:
            logger.debug("kame: could not read the keys for this call", exc_info=True)
            keys = []
        # 1.8.2.7 (review H1): a call on a later entry of the fallback list proves
        # Hermes switched, whether or not KAME holds keys for that entry.
        self._note_chain_arrival(getattr(call, "identity", "") or "")
        # 1.8.3.1 (decisions/0034 D1): where this reply started, decided before anything else happens.
        self._origin_of(call)
        if not keys:
            yield from self._plain(call, stream)
            return

        # 1.8.2.4: legs of an earlier race that finished after it was decided.
        self._drain_hedge_losers()

        identity = call.identity
        label = identity
        started = time.monotonic()
        pool_waited = 0.0
        call_id = f"{os.getpid()}-{time.time_ns()}"
        self.calls += 1
        self.last_call_at = time.time()

        try:
            outside = sorted(fingerprint(key) for key in keys if not call.entry_id(key))
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
        last_kind: str = ""  # 1.8.2.3: for the retry hint
        last_window: str = ""  # 1.8.2.3: quota window, for the hint family
        consecutive_timeouts = 0
        # 1.8.2.7 (decisions/0027): refusals in a row that benched their key for a
        # long rest, and how many probe bursts this call has already sent.
        long_refusals = 0
        burst_round = 0
        # 1.8.2.8 (decisions/0028): keys that failed on the provider's side in
        # this call (overloaded, timed out, a throttle with no real wait).
        busy_keys: set = set()
        busy_noted = False
        try:
            setattr(call, "_kame_started", started)
        except Exception:  # pragma: no cover - a test double without attributes
            pass
        # The answer as the user has seen it, across every attempt of this
        # call (1.1.1): both the text a continuation is prefilled with and the
        # text it is trimmed against.
        seen = ""
        resumes = 0
        resume_budget = self._resume_budget(call.kwargs, call.wire) if stream else 0
        budget_label = resume_budget if resume_budget is not None else "ongoing"
        # 1.6.0.0: which keys were asked to continue and added nothing. This,
        # not a count, ends the stitching loop.
        stalled: set = set()
        tool_cut_keys: set = set()
        replay_tool_request = False
        verdicts: Dict[str, Tuple[str, Optional[int]]] = {}

        # 1.8.2.3: latency + pacing instrumentation
        try:
            from .core.latency import LATENCY as _lat
            from .core.pacing import PACER as _pacer
            from .core.hedging import HEDGE_TRACKER as _hedge
        except Exception:
            _lat = None  # type: ignore[assignment]
            _pacer = None  # type: ignore[assignment]
            _hedge = None  # type: ignore[assignment]
        _call_started: float = 0.0  # set per-attempt for TTFT
        _hedging_enabled = not settings.is_on(settings.HEDGING_DISABLED) if hasattr(settings, 'HEDGING_DISABLED') else False

        # 1.8.2.9 (decisions/0032 A3/A4): a background job (channel "secondary") has a budget and
        # sends no backup request. The chat (main lane) reads none of it: `aux` stays None.
        aux: Optional[Any] = None
        aux_requests = 0
        if getattr(call, "channel", "main") != "main":
            _hedging_enabled = False
            if not aux_budget.unbounded():
                aux = aux_budget.budget_for(
                    str(getattr(call, "task", "") or ""), bool(getattr(call, "task_known", False)),
                    (call.kwargs or {}).get("timeout"), settings.total_wait_bound())

        def aux_stop(why: str) -> BaseException:
            """Record that this background job used its budget and return the error to raise."""
            task = str(getattr(call, "task", "") or "")
            self._aux_memo[(identity, aux.klass)] = time.time() + aux_budget.MEMO_S
            self._aux_stopped[task or "?"] = self._aux_stopped.get(task or "?", 0) + 1
            _emit(call,
                  "gave_up",
                  identity=identity,
                  reason=f"background job {task or 'unnamed'} stopped: {why} - Hermes decides what is next")
            _publish(self, None)
            return KameAuxTimeout(aux_budget.stop_message(
                task, model_label(identity), aux_requests, time.monotonic() - started))

        # 1.8.2.9 (decisions/0030, C8): the one place a call moves to another model of the
        # user's list in place - also what the all-keys-resting and the busy paths use.
        visited: List[str] = []     # models left in this call (gated like a busy one: review H4)
        excluded: List[str] = []    # models that refused Hermes' request shape (review M3)
        home_model = (call.kwargs or {}).get("model")
        try:   # the widen step must not ask a model that already refused this request (review, 2026-10-24)
            setattr(call, "_kame_avoid", excluded)
            if home_model:      # 1.8.3.1 (review R4): the name as Hermes sent it, before any circle rewrites it
                setattr(call, "_kame_home_model", home_model)
        except Exception:  # pragma: no cover - a test double without attributes
            pass

        def circle_to(wrapped: Tuple[str, str], why: str) -> None:
            nonlocal identity, label, busy_keys, busy_noted, verdicts, consecutive_timeouts
            nonlocal long_refusals, vigil
            self._announce_wrap(call, identity, wrapped[0], why)
            try:
                setattr(call, "_kame_standin_why", _short_why(why))
            except Exception:  # pragma: no cover - a test double without attributes
                pass
            visited.append(identity)
            identity = label = wrapped[0]
            call.kwargs = dict(call.kwargs, model=wrapped[1])      # a new dict: Hermes' own is never mutated
            # review M2: the reply now belongs to this model, so a race win recorded earlier
            # in the call (R1) cannot flip it back; _after_answer names it from here.
            setattr(call, "_kame_answered_by", wrapped[0])
            setattr(call, "_kame_answered_model", wrapped[1])
            busy_keys = set()
            busy_noted = False
            verdicts = {}
            consecutive_timeouts = 0
            long_refusals = 0
            vigil = None                                            # review L3: its label named the old model
            # pool_waited is kept: it is the user's "Give up after" clock (review M4)

        if aux is not None:
            # A background job that cannot start well does not start: no request at all.
            refusal = self._aux_refusal(identity, keys, aux)
            if refusal:
                raise aux_stop(refusal)

        # 1.8.2.8 (owner, live test): every reply starts on the best model. One
        # KAME still knows busy (inside its busy window) is passed down the
        # chain at once, without spending a request on it.
        probe_leg: Optional[Tuple[str, str]] = None      # (busy model, its name as Hermes wrote it)
        if time.time() < self._busy_until.get(identity, 0.0):
            plan = self._start_probe_plan(call, identity, stream, keys) if home_model else None
            if plan is not None and home_model:
                # decisions/0032 C: start on the next model at once (as the hand-over would have), and
                # ask the busy one again beside it - first sign of work wins; nothing waits for it.
                probe_leg = (identity, str(home_model))
                circle_to(plan, "probe")
            else:
                handover = self._maybe_hand_over(call, identity, keys, 0.0, False, waited=0.0, busy=True,
                                                 skipped=self._busy_until.get(identity, 0.0) - time.time(),
                                                 stand_in_ok=True, avoid=visited + excluded)
                if isinstance(handover, _StandIn):
                    # 1.8.3.0 (decisions/0033 D2 a): a sibling of the same provider answers, here,
                    # on the keys KAME holds - Hermes is not asked to switch.
                    circle_to(handover.target(), handover.why)
                else:
                    if handover is not None:
                        raise handover
                    # 1.8.2.9: nothing after Hermes' position is free - start on the first model of
                    # the list that is (or is due another look), the model in use included, not on
                    # this busy one while a better is due, nor on the last.
                    wrapped = self._wrap_target(call, identity, seen=False, include_self=True)
                    if wrapped is not None and wrapped[0] != identity:
                        circle_to(wrapped, "was busy moments ago")

        while True:
            attempt += 1
            if call.cancelled():
                # Stopped by Hermes (the user pressed stop, or the turn moved
                # on) — 1.8.1.8's exits, unchanged: with text on screen the
                # answer so far goes back; without, the last failure, so the
                # stop is reported against what actually went wrong.
                if seen:
                    self.mid_stream_cuts += 1
                    raise call.cut(last_error)
                raise last_error if last_error is not None else call.cancel_error()

            if aux is not None and not seen:
                if aux_requests >= aux.max_requests:
                    raise aux_stop(f"it used its {aux.max_requests} requests")
                if time.monotonic() - started >= aux.total_s:
                    raise aux_stop("it used its time")

            key, status = self.engine.select(
                identity, keys, spread=not settings.is_on(settings.SPREAD_DISABLED))
            if key is None:
                if seen:
                    self.mid_stream_cuts += 1
                    raise call.cut(last_error)
                yield from self._plain(call, stream)
                return

            if status == "EXHAUSTED":
                # Every key is resting. Calling one anyway would spend a request
                # already known to be refused and deepen the cooldown that
                # caused it. Wait for the soonest — for as long as it takes,
                # since 1.0.1, but never in silence.
                if vigil is None:
                    vigil = _Vigil(label)
                aux_left = None
                if aux is not None and not seen:
                    aux_left = aux.total_s - (time.monotonic() - started)
                    _aux_eta = self.engine.next_recovery_seconds(identity, keys)
                    if not aux.may_wait or aux_left <= 0 or (_aux_eta is not None and _aux_eta > aux_left):
                        raise aux_stop("every key is resting")
                # 1.8.2.0: the total bound. 1.8.2.1: unset, a cron session gets
                # 300 s; a value the user set (0 included) always wins.
                total_cap = settings.total_wait_bound()
                recovered = True
                if total_cap > 0 and pool_waited >= total_cap:
                    recovered = False
                    _emit(call, 
                        "gave_up",
                        identity=identity,
                        reason=f"every key resting for {format_duration(pool_waited)} — "
                        f"max_total_wait_seconds is {format_duration(total_cap)}",
                    )
                else:
                    handover = self._maybe_hand_over(
                        call, identity, keys, pool_waited, bool(seen),
                        waited=time.monotonic() - started, busy=self._is_busy(identity, busy_keys),
                        stand_in_ok=True, avoid=visited + excluded)
                    if isinstance(handover, _StandIn):
                        circle_to(handover.target(), handover.why)      # 1.8.3.0 (D2 a)
                        continue
                    if handover is not None:
                        raise handover
                    # 1.8.2.9: every key of this model rests and nothing after it is free:
                    # circle back through the user's list rather than sleep on a model
                    # that may be the last, least capable one.
                    wrapped = self._wrap_target(call, identity, seen=bool(seen), left_asked=True,
                                                visited=visited, excluded=excluded)
                    if wrapped is not None:
                        circle_to(wrapped, "has every key resting")
                        continue
                    wait_started = time.monotonic()
                    bound = {"budget": total_cap - pool_waited} if total_cap > 0 else {}
                    _probe_in = self._next_probe_in(identity)
                    if _probe_in is not None:
                        # wake when a model of the list is due another look
                        bound["budget"] = min(bound.get("budget", _probe_in + 0.5), _probe_in + 0.5)
                    # 1.8.2.7 (review H1/M3): held back only by the short hand-over
                    # memo? Wake when it ends, so the hand-over is asked again then.
                    _memo_left = self._handover_until.get(self._memo_key(call, identity), 0.0) - time.time()
                    if _memo_left > 0:
                        bound["budget"] = min(bound.get("budget", _memo_left + 0.5), _memo_left + 0.5)
                    if aux_left is not None:
                        bound["budget"] = min(bound.get("budget", aux_left), aux_left)
                    recovered = yield from self._wait_for_recovery(call, identity, keys, vigil, **bound)
                    pool_waited += max(0.0, time.monotonic() - wait_started)
                    if aux_left is not None and not recovered:
                        raise aux_stop("its wait ended")
                if not recovered and total_cap > 0 and pool_waited >= total_cap and last_error is None:
                    self.surfaced += 1
                    raise _with_retry_hint(ConnectionError(
                        f"kame: every key for {label} stayed resting for {format_duration(pool_waited)}, "
                        "past max_total_wait_seconds"))
                if not recovered:
                    if seen:
                        self.mid_stream_cuts += 1
                        raise call.cut(last_error)
                    if last_error is not None:
                        self.surfaced += 1
                        raise _with_retry_hint(host_text.take_off_the_advice(last_error), kind=last_kind,
                                               quota_window=last_window)
                    raise _with_retry_hint(call.cancel_error())
                slept = True
                consecutive_timeouts = 0
                try:
                    keys = call.keys() or keys
                except Exception:
                    logger.debug("kame: could not refresh the keys after a wait", exc_info=True)
                continue

            if attempt > 1:
                _emit(call, 
                    "switch",
                    identity=identity,
                    key=fingerprint(key),
                    reason=(
                        "a key came back after the wait — asking it"
                        if slept
                        else f"the last key did not answer — asking this one (try {attempt})"
                    ),
                )
            else:
                # 1.8.2.0: every call, not only the troubled ones — the owner
                # watched a nine-minute NVIDIA answer leave no row at all.
                _emit(call, 
                    "sent",
                    identity=identity,
                    key=fingerprint(key),
                    reason=f"{self.engine.healthy_count(identity, keys)} of {len(keys)} key(s) ready",
                )
            try:
                runtime.note_sent(call.provider, call.entry_id(key), fingerprint(key), now=time.time())
            except Exception:  # pragma: no cover — a ContextVar set and a hash
                logger.debug("kame: could not note the key in flight", exc_info=True)
            aux_requests += 1
            self._count_request(call, identity)
            _call_started = time.monotonic()  # 1.8.2.3: for latency tracking

            healthy = self.engine.healthy_count(identity, keys)
            # 1.8.1.8's line, word for word: the health count, and at most a
            # word saying a rotation is happening — never an attempt number.
            resting = attempt > 1 or healthy < len(keys)
            _Spinner.update(
                call.status_owner(),
                status_line(
                    healthy, len(keys),
                    "trying the next key" if resting else "",
                    subject=model_label(identity),
                    symbol="↻" if resting else "⏳",
                ),
            )
            _publish(
                self,
                self._mark_stand_in(call, identity, {
                    "kind": "calling",
                    "identity": identity,
                    "model": model_label(identity),
                    "attempt": attempt,
                    "healthy": healthy,
                    "keys": len(keys),
                    # 1.8.2.9: when this reply began and on which key, so the panel can run the
                    # clock itself (no new write per second) and name the key on the ring.
                    "started_at": time.time() - (time.monotonic() - started),
                    "key": fingerprint(key),
                }),
            )

            progress = _Progress()
            attempt_started = time.monotonic()
            waited_before = attempt_started - started
            request = call.kwargs
            stitcher = None
            if seen:
                resumed = call.kwargs if replay_tool_request else _resume_kwargs(
                    call.kwargs, seen, _prefill_refused(identity))
                if resumed is not None:
                    request = resumed
                    stitcher = _ReplayStitcher(seen) if replay_tool_request else stitch.Stitcher(seen)
            if aux is not None and not stream:
                # One attempt cannot outlive the job's total: a NEW dict, Hermes' own is never touched.
                _asked = request.get("timeout")
                if isinstance(_asked, (int, float)) and not isinstance(_asked, bool):
                    _left = max(aux_budget.FLOOR_S, aux.total_s - (time.monotonic() - started))
                    if _left < _asked:
                        request = dict(request, timeout=_left)
            shown = ""
            held_tools: List[Any] = []
            saw_tools = False
            finished = False
            answered_with_text = False
            result: Any = None

            try:
                client = call.client_for(key, attempt)
                if not stream:
                    # 1.8.2.3: adaptive hedging for non-streaming calls.
                    # If hedging is enabled and the call takes longer than p90,
                    # a backup request is sent on a second key concurrently.
                    # Follows hedge-python pattern: token bucket budget, p90
                    # threshold, first-response-wins with proper cleanup.
                    # 1.8.2.3: hedge_max_legs caps the whole mechanism — 1
                    # means serial (the concurrent block is skipped), 2-3 the
                    # race may run. Read at call time so a panel edit applies.
                    _max_legs = 1
                    try:
                        _max_legs = int(settings.number(settings.HEDGE_MAX_LEGS, 3.0))
                    except Exception:
                        pass
                    _burst, _burst_keys = (
                        self._burst_plan(call, identity, keys, key, long_refusals, burst_round, stream=False)
                        if _hedging_enabled and _hedge and not seen else (0, keys))
                    if _burst:
                        burst_round += 1
                        result, key, _call_started = self._burst_create(
                            call, identity, label, attempt, key, _burst_keys, client, request,
                            legs=_burst, started=_call_started,
                        )
                    elif _hedging_enabled and _hedge and attempt == 1 and not seen and _max_legs >= 2:
                        healthy_now = self.engine.healthy_count(identity, keys)
                        under_pressure = self._quota_pressure(identity, keys)
                        # 1.8.2.3: the threshold is mode- and class-aware — a stream's
                        # p90 TTFT, a non-stream's p90 total, thinking models
                        # their own bucket. 1.8.2.4: the class also reads the
                        # request's own thinking switches, not only the name.
                        try:
                            from .core.hedging import reasoning_model as _is_reasoning
                            _hedge_reasoning = _is_reasoning(str(call.kwargs.get("model") or ""), call.kwargs)
                        except Exception:
                            _hedge_reasoning = False
                        _p_threshold = _hedge.threshold(identity, stream=False,
                                                        reasoning=_hedge_reasoning)
                        if _p_threshold is not None and healthy_now >= 2 and not under_pressure:
                            # 1.8.2.4: the race lives in its own method. It
                            # returns the moment the first leg answers — the
                            # 1.8.2.3 block joined every leg in a ``finally``
                            # and so always waited for the slowest one.
                            result, key, _call_started = self._hedged_create(
                                call, identity, label, attempt, key, keys, client, request,
                                threshold=_p_threshold, healthy_now=healthy_now,
                                under_pressure=under_pressure, reasoning=_hedge_reasoning,
                                started=_call_started,
                            )
                        else:
                            result = call.wire.create(client, request)
                    else:
                        result = call.wire.create(client, request)
                else:
                    raw = None
                    # 1.8.2.5: a stream may race backup legs until its first
                    # word (``_race_stream``). Only a fresh request is raced:
                    # a continuation or a tool-call replay is not.
                    _stream_legs = 1
                    try:
                        _stream_legs = min(3, int(settings.number(settings.HEDGE_MAX_LEGS, 3.0)))
                    except Exception:
                        pass
                    _burst, _burst_keys = (
                        self._burst_plan(call, identity, keys, key, long_refusals, burst_round, stream=True)
                        if (_hedging_enabled and _hedge and not seen and stitcher is None
                            and not replay_tool_request) else (0, keys))
                    try:
                        from .core.hedging import reasoning_model as _is_reasoning
                        _race_reasoning = _is_reasoning(str(call.kwargs.get("model") or ""), call.kwargs)
                    except Exception:
                        _race_reasoning = False
                    # 1.8.2.8 (D11): how long this model may stay silent before the
                    # reply falls back to the next one; None without a fallback.
                    _slow_s = (self._slow_after(call, identity, _race_reasoning)
                               if not seen and stitcher is None and not replay_tool_request else None)
                    _cross = (self._cross_target(call, identity, _race_reasoning, _slow_s)
                              if _slow_s is not None else None)
                    _probe_id: Optional[str] = None
                    if probe_leg is not None and not seen and stitcher is None and not replay_tool_request:
                        # decisions/0032 C: the busy model joins the race at t = 0, beside the home one
                        _probe_id, _probe_model = probe_leg[0], probe_leg[1]
                        _cross = [(_probe_id, _probe_model, 0.0)] + list(_cross or [])
                        probe_leg = None
                        setattr(call, "_kame_probe_id", _probe_id)
                    if _burst:
                        burst_round += 1
                        raw, key, _call_started = yield from self._race_stream(
                            call, identity, label, attempt, key, _burst_keys, client, request,
                            reasoning=_race_reasoning, max_legs=_burst, progress=progress, burst=True,
                            slow_after=_slow_s, cross=_cross, probe=_probe_id,
                        )
                    elif (_hedging_enabled and _hedge and not seen and stitcher is None
                            and not replay_tool_request and _stream_legs >= 2 and len(keys) >= 2):
                        _healthy_now = self.engine.healthy_count(identity, keys)
                        if _healthy_now >= 2 and not self._quota_pressure(identity, keys):
                            raw, key, _call_started = yield from self._race_stream(
                                call, identity, label, attempt, key, keys, client, request,
                                reasoning=_race_reasoning, max_legs=_stream_legs, progress=progress,
                                slow_after=_slow_s, cross=_cross, probe=_probe_id,
                            )
                    if raw is None and (_slow_s is not None or _probe_id):
                        # No backup race here, but the slow-model watch (or the probe of the busy
                        # model) still runs: a race of one leg is the plain stream plus that clock.
                        raw, key, _call_started = yield from self._race_stream(
                            call, identity, label, attempt, key, keys, client, request,
                            reasoning=_race_reasoning, max_legs=1, progress=progress,
                            slow_after=_slow_s, cross=_cross, probe=_probe_id,
                        )
                    if raw is None:
                        raw = call.wire.open(client, request, call)
                    _won = getattr(call, "_kame_answered_by", None)
                    if _won and _won != identity:
                        # 1.8.2.8 (review R1): the next model won the race - the rest of
                        # this call is its own: its pool for every mark and rest, its
                        # model name for a continuation, never the slow model's.
                        identity = label = _won
                        call.kwargs = dict(call.kwargs, model=getattr(call, "_kame_answered_model",
                                                                      call.kwargs.get("model")))
                        busy_keys = set()
                        busy_noted = False
                        verdicts = {}
                    try:
                        for chunk in raw:
                            if call.cancelled():
                                if seen or shown:
                                    self.mid_stream_cuts += 1
                                raise call.cancel_error()
                            choices = getattr(chunk, "choices", None) or []
                            if not choices:
                                # Usage and other choiceless chunks: liveness only.
                                progress.stir()
                                yield chunk
                                continue
                            choice = choices[0]
                            delta = getattr(choice, "delta", None)
                            text = getattr(delta, "content", None) if delta is not None else None
                            tools = getattr(delta, "tool_calls", None) if delta is not None else None
                            reason = getattr(choice, "finish_reason", None)
                            if tools:
                                # Held until the call is whole: a stream that dies
                                # inside a tool call can then be asked for again
                                # without Hermes ever holding half of one.
                                progress.stir()
                                saw_tools = True
                                held_tools.append(chunk)
                                if reason:
                                    finished = True
                                    tail = yield from self._flush(stitcher, held_tools, progress)
                                    shown += tail
                                continue
                            if isinstance(text, str) and text:
                                progress.touch()
                                answered_with_text = True
                                original = text
                                if stitcher is not None:
                                    text = stitcher.feed(text)
                                if reason and (held_tools or stitcher is not None):
                                    # Order matters at the end: the last words,
                                    # then the held tool calls, then the finish.
                                    if text:
                                        shown += text
                                        yield _clone_chunk(chunk, content=text, finish=None)
                                    finished = True
                                    tail = yield from self._flush(stitcher, held_tools, progress)
                                    shown += tail
                                    yield _finish_only(chunk, reason)
                                    continue
                                if text:
                                    shown += text
                                    yield chunk if text == original else _clone_chunk(chunk, content=text)
                                elif reason:
                                    yield _clone_chunk(chunk, content="")
                                if reason:
                                    finished = True
                                continue
                            if reason:
                                finished = True
                                tail = yield from self._flush(stitcher, held_tools, progress)
                                shown += tail
                                yield chunk
                                continue
                            # Reasoning, role, an empty delta: passed straight on.
                            progress.stir()
                            yield chunk
                        if held_tools and not finished:
                            # The provider closed the stream inside a tool call
                            # without a finish. Half a call is never released;
                            # it is the same cut as a dropped connection.
                            raise _ToolCallLeftOpen("the stream ended inside a tool call")
                    finally:
                        _close_quietly(raw)
            except _CONTROL_FLOW:
                raise
            except KamePoolResting:
                raise  # 1.8.2.8: a slow model handed over from inside the race (D11)
            except BaseException as exc:  # noqa: BLE001 — re-raised below unless rotatable
                last_error = exc
                last_kind = ""  # set below from classification
                if stitcher is not None:
                    # Words a continuation sent before it dropped are still in
                    # the stitcher, held while it decided how much repeated the
                    # answer so far. They are new text: release them now, or
                    # they vanish between this key and the next (1.8.1.8 kept
                    # them through the cut response's own content).
                    try:
                        tail = stitcher.flush()
                    except Exception:  # pragma: no cover - a pure string function
                        tail = ""
                    if tail:
                        shown += tail
                        yield _text_chunk(tail)
                seen += shown
                if held_tools:
                    # A tool call that never completed. It has not executed, so
                    # the ORIGINAL request is asked for again on another key;
                    # whatever preamble was already shown is trimmed by the
                    # replay stitcher (1.6.0.0 / 1.8.1.8 semantics).
                    first_time = key not in tool_cut_keys
                    tool_cut_keys.add(key)
                    unasked = any(k and k not in tool_cut_keys for k in keys)
                    self.stream_drops += 1
                    if first_time and unasked:
                        replay_tool_request = True
                        self.tool_call_retries += 1
                        rested = _rest_unless_it_is_the_only_one(
                            self.engine, identity, keys, key, DROP_REST_S, "timeout"
                        )
                        logger.info(
                            "kame: %s %s stopped inside a tool call before anything "
                            "completed — asking another key for the same call; %s",
                            label,
                            fingerprint(key),
                            f"the key rests {format_duration(rested)}"
                            if rested
                            else "the key is not rested, it is the only one that is well",
                        )
                        _emit(call, 
                            "stream_drop",
                            identity=identity,
                            key=fingerprint(key),
                            reason="the stream stopped inside a tool call — retrying on another key",
                            seconds=rested or None,
                        )
                        self.rotations += 1
                        continue
                    self.tool_call_cuts += 1
                    named = ", ".join(_tool_names(held_tools)[:3])
                    rested = _rest_unless_it_is_the_only_one(
                        self.engine, identity, keys, key, DROP_REST_S, "timeout"
                    )
                    logger.warning(
                        "kame: %s %s stopped inside a tool call%s — every key was asked, "
                        "so it goes back to Hermes as it arrived; %s",
                        label,
                        fingerprint(key),
                        f" ({named})" if named else "",
                        f"the key rests {format_duration(rested)}"
                        if rested
                        else "the key is not rested, it is the only one that is well",
                    )
                    _emit(call, 
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
                    # Exactly what this key sent: the half call, then the way
                    # its stream ended. Hermes builds from it the same
                    # partial-stream stub it built in 1.8.1.8, when this stream
                    # reached it directly.
                    for chunk in held_tools:
                        yield chunk
                    held_tools.clear()
                    if isinstance(exc, _ToolCallLeftOpen):
                        yield _Done(None)
                        return
                    raise host_text.take_off_the_advice(exc)

                can_stitch = bool(seen) and (resume_budget is None or resumes < resume_budget)
                verdict, kind, status = self._on_failure(
                    identity, key, exc, label, attempt, progress.any or bool(seen), can_stitch,
                    call=call,
                    credential_id=counted_as(call.entry(key), key),
                )
                last_kind = kind  # 1.8.2.3: track for retry hint
                if kind in _HEDGE_QUOTA_KINDS:
                    # 1.8.2.9 (decisions/0031 J3): a quota refusal on this model steps Faster back
                    # to Balanced for ten minutes. A dict write on the failure path only.
                    try:
                        from .core.hedging import HEDGE_TRACKER as _hedge_tracker
                        _hedge_tracker.note_quota_refusal(identity)
                    except Exception:  # pragma: no cover - bookkeeping must never break a call
                        logger.debug("kame: could not note a quota refusal", exc_info=True)
                # 1.8.2.3: the window rides on the verdict — the hint
                # family reads it so a per-day quota can never be told
                # "retrying will succeed".
                last_window = str(getattr(verdict, "quota_window", "") or "") if verdict is not None else ""
                timings.record(
                    identity=identity,
                    key_fingerprint=fingerprint(key),
                    attempt=attempt,
                    outcome=verdict,
                    kind=kind,
                    status_code=status,
                    started_at=attempt_started,
                    ended_at=time.monotonic(),
                    first_sign_at=progress.first_sign_at,
                    first_text_at=progress.first_text_at,
                    waited_before_s=waited_before,
                    pool_waited_before_s=pool_waited,
                    call_id=call_id,
                    chars_seen=len(seen),
                    rest_s=self.engine.next_recovery_seconds(identity, [key]),
                    rest_source=self.engine.unsized_backoff_label(identity, key) or None,
                    channel=getattr(call, "channel", "main"),
                    task=str(getattr(call, "task", "") or ""),
                )
                try:
                    _refused_shape = int(status) in (400, 404)
                except (TypeError, ValueError):
                    _refused_shape = False
                if (verdict == "raise" and _refused_shape and home_model and not seen
                        and visited and identity != call.identity):
                    # 1.8.2.9 (review M3): Hermes built this request for ITS model. A model
                    # circled to that refuses its shape is not the reply's error - leave it
                    # out of this call and go back to the model Hermes asked for.
                    excluded.append(identity)
                    circle_to((call.identity, home_model), "refused this request")
                    continue
                if verdict == "raise" and stitcher is not None:
                    said = f"{getattr(exc, 'message', '') or ''} {exc}"
                    if stitch.refuses_prefill(said) and identity not in _NO_PREFILL:
                        _NO_PREFILL.add(identity)
                        logger.info(
                            "kame: %s refuses a continuation that ends with the "
                            "model's own turn — asking it to continue in a user "
                            "turn instead",
                            label,
                        )
                        _emit(call, 
                            "stitch",
                            identity=identity,
                            key=fingerprint(key),
                            reason="this provider will not continue from a prefill; asking in a user turn",
                            code=status,
                        )
                        continue
                if verdict == "raise" and seen:
                    # Fatal once part of the answer is on screen: what the user
                    # already read goes back to Hermes as the answer so far.
                    self.mid_stream_cuts += 1
                    _publish(self, None)
                    raise call.cut(exc)
                if verdict == "raise":
                    self.surfaced += 1
                    _emit(call, 
                        "surfaced",
                        identity=identity,
                        key=fingerprint(key),
                        reason=kind or "refused",
                        code=status,
                    )
                    raise host_text.take_off_the_advice(exc)
                if verdict == "stitch":
                    # The 1.6.0.0 progress rule, which 1.8.1.8 applied to the
                    # partial-stream stub Hermes returned for a drop. Below
                    # Hermes a drop is this exception instead, so the rule is
                    # applied here too: a continuation that added nothing marks
                    # its key, and once a marked key comes round again every key
                    # has been asked and none had more to say — hand back what
                    # the user already read rather than ask again.
                    if shown:
                        stalled.clear()
                    elif key in stalled:
                        self.mid_stream_cuts += 1
                        logger.info(
                            "kame: %s answer still cut after %d resume(s) — %d key(s) "
                            "continued it and added nothing, handing what arrived back to Hermes",
                            label, resumes, len(stalled),
                        )
                        _publish(self, None)
                        raise call.cut(exc)
                    else:
                        stalled.add(key)
                    resumes += 1
                    self.resumes += 1
                    self.rotations += 1
                    continue
                verdicts[key] = (kind, status)
                _legs_seen = getattr(call, "_kame_leg_verdicts", None)
                if _legs_seen:
                    for _leg_key, _leg_row in list(_legs_seen.items()):
                        _leg_verdict = tuple(_leg_row[:2])
                        if len(_leg_row) > 2 and _leg_row[2] != identity:
                            continue  # another model's leg (D12): not this pool's evidence
                        if _leg_key != key:
                            verdicts[_leg_key] = _leg_verdict
                            # 1.8.2.8 (live, 2026-10-17): a raced leg's 503 is the
                            # provider's answer too - counted, a busy model moves after
                            # the first round instead of the second.
                            if self._busy_failure(identity, _leg_key, *_leg_verdict):
                                busy_keys.add(_leg_key)
                    _legs_seen.clear()
                try:
                    _rest_now = self.engine.next_recovery_seconds(identity, [key])
                except Exception:
                    _rest_now = None
                if _rest_now is not None and _rest_now >= _BURST_LONG_REST_S:
                    long_refusals += 1
                else:
                    long_refusals = 0
                if kind == "timeout":
                    consecutive_timeouts += 1
                else:
                    consecutive_timeouts = 0
                # 1.8.2.8 (decisions/0028): the switch is weighed after every
                # failure, on the time the user has actually waited - not only
                # once every key rests - and a busy provider moves at once.
                if self._busy_failure(identity, key, kind, status):
                    busy_keys.add(key)
                    # 1.8.3.1 (D5, L7): the first busy answer after a busy window ended reopens it,
                    # without waiting for a second busy key (Agent Zero's rule). Otherwise every
                    # reply in the half minute after a window knocks once more on a model that is
                    # still busy. The key itself still rests only the 1 s a 503 costs. Conversation
                    # only, never under "Never switch", and not while the call is already hopping.
                    if (len(busy_keys) == 1 and not busy_noted and not visited
                            and getattr(call, "channel", "main") == "main"
                            and not (settings.is_on(settings.NO_MODEL_FALLBACK)
                                     or settings.model_switch_mode() == "never")
                            and time.time() >= self._busy_until.get(identity, 0.0)
                            and self._is_busy(identity, busy_keys)):
                        busy_noted = True
                        self._note_busy(identity)
                if len(busy_keys) >= 2 and not busy_noted:
                    busy_noted = True
                    self._note_busy(identity)
                handover = self._maybe_hand_over(
                    call, identity, keys, pool_waited, bool(seen),
                    waited=time.monotonic() - started, busy=self._is_busy(identity, busy_keys),
                    after_failure=True,
                    stand_in_ok=stitcher is None and not replay_tool_request, avoid=visited + excluded)
                if isinstance(handover, _StandIn):
                    circle_to(handover.target(), handover.why)          # 1.8.3.0 (D2 a)
                    continue
                if handover is not None:
                    raise handover
                if aux is not None and not seen:
                    # A model that says it is busy, or a key out of quota, is not worth a
                    # second key for a job nobody waits on: Hermes moves it down the user's list.
                    if len(busy_keys) >= 2:
                        raise aux_stop("its model is busy")
                    if aux.klass == "cosmetic" and kind in _HEDGE_QUOTA_KINDS:
                        raise aux_stop("a key is out of quota")
                if self._is_busy(identity, busy_keys):
                    # 1.8.2.9: busy, and nothing after Hermes' position is free to hand to
                    # Hermes: circle back through the user's list instead of rotating keys
                    # on a model that says it cannot answer (the last model of a busy list
                    # was waited on).
                    wrapped = self._wrap_target(call, identity, seen=bool(seen) or stitcher is not None
                                                or replay_tool_request, left_asked=True,
                                                visited=visited, excluded=excluded)
                    if wrapped is not None:
                        circle_to(wrapped, "is busy")
                        continue
                _keep_in_kame = self._chain_would_switch(identity) or getattr(call, "channel", "main") != "main"
                if consecutive_timeouts >= 3 and _keep_in_kame and all(
                        v[0] == "timeout" for v in verdicts.values()):
                    # The 1.8.2.6 behaviour, kept ONLY where handing the call back could
                    # make Hermes switch model on its own (a chain is set and "never
                    # switch" is on): R02 outranks the timeout policy there (review H5).
                    logger.warning(
                        "kame: %s provider appears down - %d consecutive timeouts, "
                        "skipping to recovery wait", label, consecutive_timeouts)
                    for k in keys:
                        if self.engine.healthy_count(identity, [k]) > 0:
                            self.engine.mark(identity, k, False, 5.0, "timeout")
                    self.rotations += 1
                    continue
                if consecutive_timeouts >= 2 and not _keep_in_kame:
                    # 1.8.2.7, owner's policy C (2026-10-16). A timeout says
                    # nothing about the key (it rests 0 s, no strike), so the
                    # first one rotates at once: a different key often
                    # answers. The SECOND in a row says the provider, not the
                    # key, is the problem; asking a third and a fourth key
                    # only multiplies the wait (13/10: 57 minutes, 294
                    # attempts through a Google overload). Hand the call to
                    # Hermes, which owns the deadline and the retry (R36).
                    # Until 1.8.2.6 this was "3 in a row -> bench every key 5 s
                    # and keep rotating", which never handed anything back.
                    self.surfaced += 1
                    logger.warning(
                        "kame: %s %d timeouts in a row — the provider is slow, "
                        "not the keys. Handing the call to Hermes instead of "
                        "waiting through the rest of the pool.",
                        label,
                        consecutive_timeouts,
                    )
                    _emit(call,
                        "surfaced",
                        identity=identity,
                        key=fingerprint(key),
                        reason="timeout",
                        code=status,
                    )
                    raise host_text.take_off_the_advice(exc)
                if kind == "denied" and all(k in verdicts for k in set(keys)) and all(
                        v == ("denied", status) for v in verdicts.values()):
                    # Every key was refused the same way (a country, a disabled org, a plan
                    # none of them has): no key can serve this pairing, the same proof as
                    # the unanimity rule below (review H1).
                    self.surfaced += 1
                    logger.error(
                        "kame: %s every key was refused [%s] - no key can serve this "
                        "request. Surfacing it instead of rotating for ever.", label, status)
                    raise host_text.take_off_the_advice(exc)
                if self._pool_agrees_it_is_the_request(verdicts, keys, kind, status) or (
                        self._pool_agrees_no_clock_fixes_it(identity, verdicts, keys, kind)):
                    self.surfaced += 1
                    logger.error(
                        "kame: %s every key answered %s%s and none answered at "
                        "all — that is evidence about the request, not about "
                        "the keys. Surfacing it instead of rotating further.",
                        label,
                        kind,
                        f" [{status}]" if status else "",
                    )
                    raise host_text.take_off_the_advice(exc)
                self.rotations += 1
                continue

            if stream and not finished:
                # The stream ended without a finish reason after handing over
                # part of an answer: the provider closed it mid-answer. Hermes
                # would build a partial stub from it; KAME continues it on
                # another key first (1.1.1, 1.6.0.0 progress rule).
                if stitcher is not None:
                    tail = stitcher.flush()
                    if tail:
                        shown += tail
                        yield _text_chunk(tail)
                if not answered_with_text and not shown and not seen:
                    if empty_budget > 0:
                        empty_budget -= 1
                        empty_counts[key] = empty_counts.get(key, 0) + 1
                        if empty_counts[key] >= 2:
                            self.engine.mark(identity, key, False, EMPTY_REST_S, "other")
                        logger.info(
                            "kame: %s %s ended the stream with nothing (%d) — next key",
                            label, fingerprint(key), empty_counts[key],
                        )
                        self.rotations += 1
                        continue
                    self.engine.mark(identity, key, True)
                    # 1.8.2.3: record latency + stamp pacing + hedge tracker.
                    # 1.8.2.3: the hedge bucket gets the mode (stream=TTFT here) and
                    # the model class, so a thinking model's threshold is its
                    # own and a stream's first-token time is not normalised
                    # against non-stream totals.
                    # 1.8.2.4: one place learns latency (see ``_learn_latency``).
                    self._learn_latency(call, identity, key, stream=True, started=_call_started, progress=progress)
                    if _pacer:
                        _pacer.stamp(identity, key)
                    _publish(self, None)
                    yield _Done(None)
                    return
                self.stream_drops += 1
                before = len(seen)
                seen += shown
                if len(seen) > before:
                    stalled.clear()
                    going_in_circles = False
                else:
                    going_in_circles = key in stalled
                    stalled.add(key)
                _emit(call, 
                    "stream_drop",
                    identity=identity,
                    key=fingerprint(key),
                    reason="the provider closed the stream mid-answer",
                )
                if seen and not going_in_circles and (resume_budget is None or resumes < resume_budget):
                    resumes += 1
                    self.resumes += 1
                    rested = _rest_unless_it_is_the_only_one(
                        self.engine, identity, keys, key, DROP_REST_S, "timeout"
                    )
                    logger.info(
                        "kame: %s %s cut the answer after %d character(s) — %s (%d/%s)",
                        label,
                        fingerprint(key),
                        len(seen),
                        f"resting it {format_duration(rested)} and continuing on another key"
                        if rested
                        else "it is the only key that is well, so the answer "
                        "continues on it immediately rather than after a rest",
                        resumes,
                        budget_label,
                    )
                    _emit(call, 
                        "stitch",
                        identity=identity,
                        key=fingerprint(key),
                        reason=f"continuing the answer on another key ({resumes}/{budget_label})",
                    )
                    _Spinner.update(
                        call.status_owner(),
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
                    self.rotations += 1
                    continue
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
                # 1.8.2.3: record latency + stamp pacing + hedge tracker (stream).
                # 1.8.2.4: one place learns latency (see ``_learn_latency``).
                self._learn_latency(call, identity, key, stream=True, started=_call_started, progress=progress)
                if seen:
                    self._note_reply_facts(call, aux_requests, resumes, pool_waited)
                    self._after_answer(call, identity, progress)
                if _pacer:
                    _pacer.stamp(identity, key)
                _publish(self, None)
                yield _Done(None)
                return

            if not stream and empty_budget > 0 and _result_is_empty(result):
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
            if stream and not answered_with_text and not saw_tools and not shown and empty_budget > 0 and not seen:
                # A finished stream that carried nothing at all: the same
                # squeezed-key answer as an empty non-streaming one.
                empty_budget -= 1
                empty_counts[key] = empty_counts.get(key, 0) + 1
                if empty_counts[key] >= 2:
                    self.engine.mark(identity, key, False, EMPTY_REST_S, "other")
                logger.info(
                    "kame: %s %s answered with nothing (%d) — next key",
                    label, fingerprint(key), empty_counts[key],
                )
                self.rotations += 1
                continue

            if seen:
                self.stitched += 1
                logger.info(
                    "kame: %s answer completed across %d key(s) — %d character(s), "
                    "delivered as one response",
                    label,
                    resumes + 1,
                    len(seen) + len(shown),
                )
                _emit(call, 
                    "stitch",
                    identity=identity,
                    key=fingerprint(key),
                    reason="the cut answer was completed and joined",
                )

            timings.record(
                identity=identity,
                key_fingerprint=fingerprint(key),
                attempt=attempt,
                outcome="answered",
                started_at=attempt_started,
                ended_at=time.monotonic(),
                first_sign_at=progress.first_sign_at,
                first_text_at=progress.first_text_at,
                waited_before_s=waited_before,
                pool_waited_before_s=pool_waited,
                call_id=call_id,
                chars_seen=len(seen) + len(shown),
                channel=getattr(call, "channel", "main"),
                task=str(getattr(call, "task", "") or ""),
            )
            self.engine.mark(identity, key, True)
            # 1.8.2.3: record latency + stamp pacing + hedge tracker.
            # 1.8.2.3: non-stream success -> the "total" bucket, class-aware.
            # 1.8.2.4: one place learns latency (see ``_learn_latency``).
            self._learn_latency(call, identity, key, stream=stream, started=_call_started, progress=progress)
            if _pacer:
                _pacer.stamp(identity, key)
            # An answer that carried nothing is not proof the key is back
            # (v0.0.9 / ``core.answer``): refuse to count it, as the
            # ``post_api_request`` hook always has.
            carried = (bool(answered_with_text or saw_tools or shown or seen) if stream
                       else not _result_is_empty(result))
            if carried:
                self._note_reply_facts(call, aux_requests, resumes, pool_waited)
                self._after_answer(call, identity, progress)
                try:
                    runtime.note_answered(call.provider, identity, counted_as(call.entry(key), key))
                except Exception:  # pragma: no cover — a journal write must not cost the answer
                    logger.debug("kame: could not record the answer in the journal", exc_info=True)
            if vigil is not None:
                vigil.done()
                _Spinner.update(
                    call.status_owner(),
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
                _emit(call, 
                    "recovery",
                    identity=identity,
                    key=fingerprint(key),
                    reason=f"answered on attempt {attempt}",
                    seconds=elapsed,
                )
            else:
                first = progress.first_text_at or progress.first_sign_at
                _by = getattr(call, "_kame_answered_by", None)  # D12: the model that won the race
                _emit(call,
                    "answered",
                    identity=_by or identity,
                    key=fingerprint(key),
                    reason=(f"first word after {format_duration(first - attempt_started)}"
                            if first else ("tool call" if saw_tools else "answered"))
                           + (f", for {model_label(getattr(call, 'identity', identity))}" if _by else ""),
                    seconds=time.monotonic() - started,
                )
            _publish(self, None)
            yield _Done(result)
            return

    # -- 1.8.2.4: latency learning ------------------------------------------

    def _learn_latency(self, call: "Call", identity: str, key: str, stream: bool, *,
                       started: float, progress: "_Progress") -> None:
        """Teach the latency tracker and the hedge thresholds one answered call.

        What is measured is what each consumer means by latency:

        * a **stream** is timed to its first sign of an answer (first text, or
          the first chunk at all) — how long the user waited to see it start.
          1.8.2.3 recorded the whole stream under the "first token" bucket,
          so a key that served a long answer looked slow in the tiebreak;
        * a **non-stream** call is timed whole, because the whole answer is
          the first thing the caller sees.

        A stream that never showed a sign teaches nothing — there is no first
        token to time.
        """
        if not started:
            return
        try:
            from .core.hedging import HEDGE_TRACKER as _hedge, reasoning_model as _is_reasoning
            from .core.latency import LATENCY as _lat
        except Exception:  # pragma: no cover — the modules ship with the plugin
            return
        if stream:
            first = progress.first_text_at or progress.first_sign_at
            if not first:
                return
            sample = first - started
        else:
            sample = time.monotonic() - started
        try:
            reasoning = _is_reasoning(str(call.kwargs.get("model") or ""), call.kwargs)
        except Exception:
            reasoning = False
        _lat.record(identity, fingerprint(key), sample)
        if stream and getattr(call, "_kame_race_learned", False):
            # 1.8.2.5: the race already taught the trigger its own measure.
            setattr(call, "_kame_race_learned", False)
            return
        _hedge.record_latency(identity, sample, stream=stream, reasoning=reasoning)

    # -- 1.8.2.4: the hedge race ------------------------------------------

    def _hedged_create(
        self,
        call: "Call",
        identity: str,
        label: str,
        attempt: int,
        key: str,
        keys: List[str],
        client: Any,
        request: Any,
        *,
        threshold: float,
        healthy_now: int,
        under_pressure: bool,
        reasoning: bool,
        started: float,
    ) -> Tuple[Any, str, float]:
        """One non-streaming request, with a backup leg if the first is slow.

        Returns ``(result, key that answered, when that key's leg started)``.

        The rules, each one a defect of the 1.8.2.3 block:

        * It returns the moment a leg answers. The executor is shut down
          with ``wait=False``; a leg still running finishes on its own
          thread and is accounted for by :meth:`_drain_hedge_losers`. A
          request already sent cannot be taken back, so waiting for it
          bought nothing but the slow leg's whole latency.
        * Every leg that fails is classified on *its own* key. A failure
          that is raised from here is always the primary's, because the
          caller blames ``key`` — the primary — for whatever is raised.
        * The backup key is never the primary's quota group (two keys in
          one provider project share one quota: a backup there spends the
          same quota twice for no new capacity).
        * Each leg runs in a copy of the caller's context, so whatever the
          host keeps in context variables travels with the request.
        """
        import concurrent.futures
        import contextvars

        from .core.hedging import HEDGE_TRACKER as _hedge
        try:
            from .core.pacing import PACER as _pacer
        except Exception:  # pragma: no cover — the module ships with the plugin
            _pacer = None

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="kame-hedge")
        legs: Dict[Any, Tuple[str, float, bool]] = {}
        try:
            primary = executor.submit(contextvars.copy_context().run, call.wire.create, client, request)
            legs[primary] = (key, started, False)
            # ``max`` mode considers the backup at once; adaptive waits for
            # the call to be slower than this model's usual first.
            first_wait = 0.0 if _hedge.fires_from_dispatch(identity) else threshold
            try:
                return primary.result(timeout=first_wait), key, started
            except concurrent.futures.TimeoutError:
                pass  # slower than this model's usual — consider a backup
            # Any other exception came from the primary before the threshold:
            # it propagates from the line above, blamed on ``key``, as before.

            elapsed = time.monotonic() - started
            hedge = None
            if _hedge.should_hedge(identity, elapsed, healthy_count=healthy_now,
                                   under_pressure=under_pressure, stream=False,
                                   reasoning=reasoning,
                                   est_prompt_tokens=_estimate_prompt_tokens(request)):
                others = [k for k in keys if k != key
                          and not (_pacer is not None and _pacer.same_scope(k, key))]
                hedge_key, hedge_status = self.engine.select(identity, others) if others else (None, "")
                if hedge_key and hedge_status == "SUCCESS":
                    _hedge.note_hedge(identity)
                    self._tally(_SPEED_BACKUP)
                    self._count_request(call, identity)
                    hedge_started = time.monotonic()
                    hedge_client = call.client_for(hedge_key, attempt)
                    hedge = executor.submit(contextvars.copy_context().run,
                                            call.wire.create, hedge_client, request)
                    legs[hedge] = (hedge_key, hedge_started, True)
                    _emit(call, "switch", identity=identity, key=fingerprint(hedge_key),
                          reason=(f"backup request sent — the first key passed {threshold:.1f}s"
                                  if first_wait else "backup request sent at once (Max mode)"))
                    self._publish_backup(call, identity, attempt, keys, key, started)
            if hedge is None:
                return primary.result(), key, started

            pending = set(legs)
            failed: List[Any] = []
            # Legs that answered with nothing. Live on the owner's Gemini pool
            # (2026-10-12) a thinking model answered one leg empty while the
            # other was still working; taking the first answer handed the
            # empty one to the caller, which then spent another attempt. The
            # first *useful* answer wins; an empty one is used only if no leg
            # brings anything better.
            empty: List[Any] = []
            deadline = max(120.0, threshold * 5)
            while pending:
                done, pending = concurrent.futures.wait(
                    pending, timeout=deadline, return_when=concurrent.futures.FIRST_COMPLETED)
                if not done:
                    break
                failed.extend(f for f in done if f.exception() is not None)
                answered = [f for f in done if f.exception() is None]
                empty.extend(f for f in answered if _result_is_empty(f.result()))
                winner = next((f for f in answered if f not in empty), None)
                if winner is not None:
                    win_key, win_started, win_is_hedge = legs[winner]
                    for other in failed + empty + list(pending):
                        self._queue_hedge_loser(other, legs[other], call, identity, label, attempt, reasoning)
                    if win_is_hedge:
                        setattr(call, "_kame_backup_won", True)       # 1.8.3.1
                        _emit(call, "switch", identity=identity, key=fingerprint(win_key),
                              reason=f"the backup answered first, {time.monotonic() - started:.1f}s after the first request")
                    return winner.result(), win_key, win_started

            # No leg brought a useful answer. An empty answer goes back as one
            # (the caller's empty-answer rule then rests the right key);
            # failures are classified on their own keys. What is raised is the
            # primary's, so the caller's blame on ``key`` is right.
            if empty and not pending:
                chosen = primary if primary in empty else empty[0]
                for f in failed + [f for f in empty if f is not chosen]:
                    self._queue_hedge_loser(f, legs[f], call, identity, label, attempt, reasoning)
                chosen_key, chosen_started, _ = legs[chosen]
                return chosen.result(), chosen_key, chosen_started
            for f in failed + empty:
                if f is not primary:
                    self._queue_hedge_loser(f, legs[f], call, identity, label, attempt, reasoning)
            if primary in failed:
                raise primary.exception()
            # The primary is still out (the backup failed or the deadline
            # passed): wait for it exactly as an unhedged call would.
            return primary.result(), key, started
        finally:
            executor.shutdown(wait=False)
            self._drain_hedge_losers()

    def _queue_hedge_loser(self, future: Any, leg: Tuple[str, float, bool], call: "Call",
                           identity: str, label: str, attempt: int, reasoning: bool) -> None:
        """Account for a leg the race did not use, whenever it finishes."""
        loser_key, leg_started, is_hedge = leg

        def _finished(f: Any) -> None:
            try:
                exc = f.exception()
            except BaseException as cancelled:  # pragma: no cover — legs are never cancelled
                exc = cancelled
            self._hedge_losers.append((identity, loser_key, call, label, attempt, exc,
                                       time.monotonic() - leg_started, is_hedge, reasoning))

        future.add_done_callback(_finished)

    def _drain_hedge_losers(self) -> None:
        """Classify the legs that lost a race, on the thread that drives calls.

        A loser that failed goes through ``_on_failure`` on its own key —
        rested, journaled and shown like any other refusal — and a per-minute
        refusal on a backup leg cools hedging for that model. A loser that
        answered spent quota and is a true latency sample: the pacer counts
        it and both latency trackers learn from it.
        """
        while self._hedge_losers:
            try:
                (identity, key, call, label, attempt, exc, elapsed, is_hedge,
                 reasoning) = self._hedge_losers.popleft()
            except IndexError:  # pragma: no cover — another drain emptied it
                return
            try:
                if exc is None:
                    from .core.hedging import HEDGE_TRACKER as _hedge
                    from .core.latency import LATENCY as _lat
                    from .core.pacing import PACER as _pacer
                    _pacer.stamp(identity, key)
                    _lat.record(identity, fingerprint(key), elapsed)
                    _hedge.record_latency(identity, elapsed, stream=False, reasoning=reasoning)
                    continue
                _verdict, kind, _status = self._on_failure(
                    identity, key, exc, label, attempt, False, False,
                    call=call, credential_id=counted_as(call.entry(key), key),
                )
                if getattr(call, "_kame_probe_id", None) == identity and self._busy_failure(
                        identity, key, kind, _status):
                    # decisions/0032 C: the probe of a busy model was refused as busy again: the WINDOW
                    # doubles (D2). The key still rests one second - the 503 rule is untouched.
                    self._note_busy(identity)
                # 1.8.2.7 (decisions/0027): a leg is a key tried this run, so the
                # unanimity rules in ``_drive`` must see its verdict too, or a pool
                # probed several keys at once could never prove "every key refused".
                legs_seen = getattr(call, "_kame_leg_verdicts", None)
                if legs_seen is None:
                    legs_seen = {}
                    try:
                        setattr(call, "_kame_leg_verdicts", legs_seen)
                    except Exception:  # pragma: no cover - a call that refuses attributes
                        pass
                legs_seen[key] = (kind, _status, identity)  # review R2: whose pool it was
                if kind in _HEDGE_QUOTA_KINDS:
                    from .core.hedging import HEDGE_TRACKER as _hedge_q
                    _hedge_q.note_quota_refusal(identity)   # 1.8.2.9: any leg, on its own model
                if is_hedge and kind in ("rate_limit", "per_minute"):
                    from .core.hedging import HEDGE_TRACKER as _hedge
                    _hedge.note_hedge_429(identity)
            except Exception:
                logger.debug("kame: could not account for a hedge leg", exc_info=True)

    # -- 1.8.2.5: the stream race -------------------------------------------

    def _seed_race_history(self, identity: str, reasoning: bool) -> None:
        """Once per model per process: teach the stream trigger the recorded
        first-word times (``timings.first_word_history``), so the race is
        tuned from the first call instead of after twenty."""
        seeded = (identity, reasoning)
        if seeded in self._race_seeded:
            return
        self._race_seeded.add(seeded)
        try:
            if self._first_word_history is None:
                self._first_word_history = timings.first_word_history()
            samples = self._first_word_history.get(identity) or []
            if samples:
                from .core.hedging import HEDGE_TRACKER as _hedge
                added = _hedge.seed_stream(identity, samples, reasoning=reasoning)
                if added:
                    logger.info("kame: %s backup trigger learned from %d recorded first words", identity, added)
        except Exception:
            logger.debug("kame: could not seed the backup trigger", exc_info=True)

    def _race_stream(
        self,
        call: "Call",
        identity: str,
        label: str,
        attempt: int,
        key: str,
        keys: List[str],
        client: Any,
        request: Any,
        *,
        reasoning: bool,
        max_legs: int,
        progress: "_Progress",
        burst: bool = False,
        slow_after: Optional[float] = None,
        cross: Optional[List[Tuple[str, str, float]]] = None,
        probe: Optional[str] = None,
    ) -> Iterator[Any]:
        """Open a stream that may race up to ``max_legs`` keys to its first word.

        ``probe`` (1.8.2.9, decisions/0032 C): the identity of a busy model that is among
        ``cross`` at t = 0 because it was due another look. Winning is not "slow" for the home
        model, and losing says nothing about the busy one.

        ``slow_after`` (1.8.2.8, owner's live test, decisions/0028 D11): when no
        leg has started working by then — no text, tool call or reasoning —
        the model is slow right now, not the key, and the reply falls back to
        the next model of the list (every leg cut, the model left alone for a
        while). ``None`` when no fallback could take it (:meth:`_slow_after`).

        ``burst`` (1.8.2.7, decisions/0027): the pool is running dry, so every leg
        opens at once, outside the backup budget - a probe for a key that still
        has quota, not a backup for a slow one.

        A generator: it yields keep-alives (and, while it is the only leg,
        the primary's own word-less chunks, exactly as an unraced stream
        would) and *returns* ``(stream, key, leg_started)`` for the caller to
        read the answer from — the leg that produced the first word.

        * The primary opens at once. If no word has come after the model's
          first trigger (``HedgeTracker.stream_triggers``: twice its median
          time to first word, learned), a backup leg opens on another healthy
          key outside the primary's quota group; after the second trigger, a
          third — each only when :meth:`HedgeTracker.may_start_backup` says
          the budget, the pool and the 429 pressure allow it.
        * The first leg to bring text or a tool call wins. Every other leg is
          aborted on the spot (its HTTP response closed), counted by the
          pacer, and taught to the latency trackers as a lower bound.
        * A leg that fails is judged on its own key. If no leg brings a
          word: an empty answer goes back as one (the empty-answer rule then
          rests the right key); otherwise the primary's error is raised, and
          the caller blames ``key`` — the primary — for it, which is right.
        * Before any backup exists nothing changes for the user: the
          primary's chunks pass straight through, and its failure is raised
          at once so the ordinary rotation goes on without waiting.
        """
        from .core.hedging import HEDGE_TRACKER as _hedge
        from .core.latency import LATENCY as _lat
        from .core.pacing import PACER as _pacer

        race_q: "queue.Queue[Tuple[_StreamLeg, str, Any]]" = queue.Queue()
        self._seed_race_history(identity, reasoning)
        triggers = ([0.0] * max(0, max_legs - 1) if burst
                    else list(_hedge.stream_triggers(identity, reasoning=reasoning)))
        primary = _StreamLeg(race_q, key, client, is_hedge=False)
        primary.start(call.wire, request)
        legs: List[_StreamLeg] = [primary]
        race_started = primary.started
        est_tokens = _estimate_prompt_tokens(request)
        last_beat = time.monotonic()
        winner: Optional[_StreamLeg] = None
        cross_legs: List[_StreamLeg] = []
        cross = list(cross or [])
        slow_base = slow_after
        slow_extended = False
        widened = 0     # decisions/0032 B2: models added at the slow bound (at most _WIDEN_MAX)
        noted_slow: set = set()     # B3 already opened a slow window for these: not a second one at the end

        def abort_all() -> None:
            for leg in legs:
                leg.abort()

        try:
            while True:
                now = time.monotonic()
                # -- a backup, when the next trigger passes ------------------------
                while (len(legs) < max_legs and triggers and winner is None
                       and now - race_started >= triggers[0]
                       and any(not leg.done for leg in legs)):
                    triggers.pop(0)
                    used = {leg.key for leg in legs}
                    others = [k for k in keys if k not in used and not _pacer.same_scope(k, key)]
                    healthy = self.engine.healthy_count(identity, keys)
                    if not others or (not burst and not _hedge.may_start_backup(
                            identity, healthy_count=healthy, under_pressure=self._quota_pressure(identity, keys),
                            stream=True, reasoning=reasoning, est_prompt_tokens=est_tokens)):
                        continue
                    backup_key, status = self.engine.select(identity, others)
                    if not backup_key or status != "SUCCESS":
                        continue
                    if not burst:
                        _hedge.note_hedge(identity)
                    self._tally(_SPEED_BURST if burst else _SPEED_BACKUP)
                    self._count_request(call, identity)
                    backup = _StreamLeg(race_q, backup_key, call.client_for(backup_key, attempt), is_hedge=True)
                    backup.start(call.wire, request)
                    legs.append(backup)
                    if burst and len(legs) == 2:
                        self.probe_bursts += 1
                    _emit(call, "switch", identity=identity, key=fingerprint(backup_key),
                          reason=("asking several keys at once — the last keys were out of quota" if burst
                                  else "Speed is Faster — a backup key joined a reply that was a little late" if now - race_started < 0.05
                                  else f"no first word after {now - race_started:.1f}s (slower than usual) — a backup key races it"))
                    if not burst:
                        self._publish_backup(call, identity, attempt, keys, key, race_started)
                # -- 1.8.2.8 (D12): the next model races a silent one ----------------------
                while (cross and winner is None and now - race_started >= cross[0][2]
                        and any(not leg.done for leg in legs)):
                    target_id, target_model, _at = cross.pop(0)
                    if target_id != probe and not self._claim_probe(target_id):
                        continue    # another conversation asked this busy model a moment ago (0032 B4)
                    try:
                        ckey, cstatus = self.engine.select(target_id, list(keys))
                    except Exception:
                        ckey, cstatus = None, ""
                    if ckey and cstatus == "SUCCESS":
                        crequest = dict(request)
                        crequest["model"] = target_model
                        self._tally(_SPEED_RACE)
                        self._count_request(call, target_id)
                        cross_leg = _StreamLeg(race_q, ckey, call.client_for(ckey, attempt), is_hedge=True)
                        cross_leg.cross = target_id  # type: ignore[attr-defined]
                        cross_leg.cross_model = target_model  # type: ignore[attr-defined]
                        cross_leg.start(call.wire, crequest)
                        legs.append(cross_leg)
                        cross_legs.append(cross_leg)
                        if probe and target_id == probe:
                            _emit(call, "switch", identity=identity, key=fingerprint(ckey),
                                  reason=f"{model_label(target_id)} was busy a moment ago - asked again beside "
                                         f"{model_label(identity)}; the first to answer is shown")
                        else:
                            _emit(call, "switch", identity=identity, key=fingerprint(ckey),
                                  reason=f"no first word after {now - race_started:.1f}s — {model_label(target_id)} "
                                         "(your next model) races it; the first to answer is shown")
                        self._publish_racing(identity, attempt, keys, key, race_started, cross_legs, probe, call)
                # -- a model that is slow today: the next model answers -------------------
                if (slow_after is not None and slow_base is not None and not slow_extended and winner is None
                        and now - race_started >= slow_after):
                    live = [leg for leg in cross_legs if not leg.done]
                    if live:
                        # decisions/0032 B1 (was review R3's whole bound from the last start): a raced
                        # model keeps the reply waiting only to its own late point, never longer than
                        # 1.8.2.8 did.
                        slow_extended = True
                        slow_after = _second_bound(slow_base, [leg.started - race_started for leg in live])
                if slow_after is not None and winner is None and now - race_started >= slow_after:
                    taken = [identity] + [getattr(leg, "cross") for leg in cross_legs]
                    # decisions/0032 B2: the race WIDENS instead of starting over - one more model of
                    # the user's list joins, nothing already paid for is cut.
                    widen = (self._widen_candidate(call, identity, keys, taken)
                             if (widened < _WIDEN_MAX and slow_base is not None and not burst) else None)
                    if widen is not None and not self._claim_probe(widen[0] + widen[2]):
                        widen = None    # another conversation asked this busy model a moment ago
                    if widen is not None:
                        w_entry, w_model, w_suffix = widen
                        w_id = w_entry + w_suffix
                        try:
                            wkey, wstatus = self.engine.select(w_id, list(keys))
                        except Exception:
                            wkey, wstatus = None, ""
                        widened += 1
                        if wkey and wstatus == "SUCCESS":
                            wrequest = dict(request)
                            wrequest["model"] = w_model
                            self._tally(_SPEED_RACE)
                            self._count_request(call, w_id)
                            wide_leg = _StreamLeg(race_q, wkey, call.client_for(wkey, attempt), is_hedge=True)
                            wide_leg.cross = w_id  # type: ignore[attr-defined]
                            wide_leg.cross_model = w_model  # type: ignore[attr-defined]
                            wide_leg.start(call.wire, wrequest)
                            legs.append(wide_leg)
                            cross_legs.append(wide_leg)
                            _emit(call, "switch", identity=identity, key=fingerprint(wkey),
                                  reason=f"no first word from {model_label(identity)} after {now - race_started:.1f}s — "
                                         f"{model_label(w_id)} (your list) joins; the first to answer is shown")
                            self._publish_racing(identity, attempt, keys, key, race_started, cross_legs, probe, call)
                            slow_after = max(slow_after, _second_bound(
                                slow_base, [leg.started - race_started for leg in cross_legs if not leg.done]))
                            continue
                    # Review finding 1 (2026-10-24): only a model whose request is STILL RUNNING is "in
                    # the race"; one whose leg already failed or ended empty can still be handed to
                    # Hermes, as in 1.8.2.8. `taken` (every model that had a leg) is for the widen only.
                    racing_now = [identity] + [getattr(leg, "cross") for leg in cross_legs if not leg.done]
                    if not self._outside_race_free(call, identity, racing_now):
                        # decisions/0032 B3: every model that could take this reply is already in the
                        # race and silent. Cutting them would hand Hermes one of the same models:
                        # note them slow for the next reply (once) and keep waiting for the first to speak.
                        self._note_busy(identity, first=_SLOW_FIRST_S, ceiling=_SLOW_MAX_S)
                        noted_slow.add(identity)
                        for cross_leg in cross_legs:
                            if not cross_leg.done:
                                self._note_busy(getattr(cross_leg, "cross"), first=_SLOW_FIRST_S,
                                                ceiling=_SLOW_MAX_S)
                                noted_slow.add(getattr(cross_leg, "cross"))
                        _emit(call, "switch", identity=identity,
                              reason="every model of your list is slow right now - KAME keeps waiting for "
                                     "the first of them; nothing is cut")
                        slow_after = None
                        handover = None
                    else:
                        handover = self._maybe_hand_over(call, identity, keys, 0.0, False,
                                                         waited=now - race_started, busy=True,
                                                         slow=now - race_started)
                        if handover is None:
                            slow_after = None  # nothing could take it: keep waiting, as before
                    if handover is not None:
                        for leg in legs:
                            leg.abort()
                            try:
                                _pacer.stamp(identity, leg.key)
                                _hedge.record_latency(identity, now - leg.started, stream=True,
                                                      reasoning=reasoning, censored=True)
                            except Exception:
                                logger.debug("kame: could not account for a slow leg", exc_info=True)
                        self._note_busy(identity, first=_SLOW_FIRST_S, ceiling=_SLOW_MAX_S)
                        for cross_leg in cross_legs:  # the models raced in were silent too
                            self._note_busy(getattr(cross_leg, "cross"), first=_SLOW_FIRST_S,
                                            ceiling=_SLOW_MAX_S)
                        raise handover
                # -- wait for the next event -------------------------------------------
                wait = 0.25
                if triggers and len(legs) < max_legs:
                    wait = min(wait, max(0.01, race_started + triggers[0] - now))
                if slow_after is not None:
                    wait = min(wait, max(0.01, race_started + slow_after - now))
                if cross:
                    wait = min(wait, max(0.01, race_started + cross[0][2] - now))
                try:
                    leg, kind, payload = race_q.get(timeout=wait)
                except queue.Empty:
                    if call.cancelled():
                        abort_all()
                        raise call.cancel_error()
                    if time.monotonic() - last_beat >= self.KEEPALIVE_S:
                        last_beat = time.monotonic()
                        yield _KEEPALIVE
                    continue
                if kind == "chunk":
                    if _carries_answer(payload):
                        leg.buffer.append(payload)
                        winner = leg
                    elif len(legs) == 1:
                        # Alone in the race: a word-less chunk (role, reasoning,
                        # usage) goes out as it would have without a race.
                        progress.stir()
                        last_beat = time.monotonic()
                        yield payload
                    else:
                        leg.buffer.append(payload)
                elif kind == "end":
                    leg.done = True
                else:
                    leg.done = True
                    leg.error = payload
                if winner is None and not all(leg.done for leg in legs):
                    continue
                if winner is None and len(legs) == 1 and primary.error is not None:
                    # No backup ever started: exactly the unraced failure.
                    raise primary.error
                break

        except BaseException:
            # review R8: closed, cancelled or failed while racing - no leg
            # keeps reading (and billing) after the race is gone.
            abort_all()
            raise

        # -- the race is decided ---------------------------------------------------
        now = time.monotonic()
        if winner is None:
            # No leg brought a word. An empty answer goes back as one; if
            # every leg failed, the primary's error is raised.
            ended = [leg for leg in legs if leg.error is None]
            if ended:
                winner = primary if primary in ended else ended[0]
        for leg in legs:
            if leg is winner:
                continue
            elapsed = now - leg.started
            leg_id = getattr(leg, "cross", None) or identity  # D12: a raced-in model is its own pool
            if not leg.done:
                leg.abort()
                try:
                    _pacer.stamp(leg_id, leg.key)
                    _lat.record(leg_id, fingerprint(leg.key), elapsed, censored=True)
                    _hedge.record_latency(leg_id, elapsed, stream=True, reasoning=reasoning, censored=True)
                except Exception:
                    logger.debug("kame: could not account for a cut race leg", exc_info=True)
            elif leg.error is not None and (winner is not None or leg is not primary):
                self._hedge_losers.append((leg_id, leg.key, call, label, attempt, leg.error,
                                           elapsed, leg.is_hedge, reasoning))
            elif leg.error is None:
                try:
                    _pacer.stamp(leg_id, leg.key)
                except Exception:  # pragma: no cover — a dict append
                    pass
        self._drain_hedge_losers()
        if probe:
            setattr(call, "_kame_probe_id", None)   # decided: a later 503 on that model is not this probe's
        if winner is None:
            raise primary.error  # type: ignore[misc]
        if winner.buffer and _carries_answer(winner.buffer[-1]):
            # The trigger learns exactly what decides the race: time to the
            # first working chunk on the leg that won. ``_learn_latency``
            # leaves this call's stream sample to it.
            try:
                _hedge.record_latency(getattr(winner, "cross", None) or identity, now - winner.started,
                                      stream=True, reasoning=reasoning)
                setattr(call, "_kame_race_learned", True)
            except Exception:
                logger.debug("kame: could not learn from the race", exc_info=True)
        if winner.response is not None:
            call.attach_response(winner.response)
        won_by = getattr(winner, "cross", None)
        if won_by:
            # The next model answered first: this reply is its own; the slow model is
            # left alone like a busy one, so the next replies go straight on (D12).
            setattr(call, "_kame_answered_by", won_by)
            setattr(call, "_kame_answered_model", getattr(winner, "cross_model", None))
            if probe and won_by == probe:
                # decisions/0032 C: the best model is back. The home model was not slow, it was only
                # beaten by a recovered one - no window for it, and the reply is not a hand-over.
                _emit(call, "switch", identity=identity, key=fingerprint(winner.key),
                      reason=f"{model_label(won_by)} is back - it answered first, "
                             f"{now - race_started:.1f}s after the request")
                for leg in legs:
                    lost = getattr(leg, "cross", None)
                    if lost and leg is not winner and not leg.buffer and lost not in noted_slow:
                        self._note_busy(lost)
            else:
                self.handed_over += 1
                setattr(call, "_kame_standin_why", "slow")        # 1.8.3.1: the reply row says why
                if identity not in noted_slow:      # review finding 2: B3 already noted it, once
                    self._note_busy(identity)
                for leg in legs:   # a model raced in before the winner and still silent lost too
                    lost = getattr(leg, "cross", None)
                    if lost and leg is not winner and not leg.buffer and lost not in noted_slow:
                        self._note_busy(lost)
                _emit(call, "handover", identity=identity,
                      reason=f"{model_label(identity)} was slower than usual -> {model_label(won_by)} "
                             f"answered first, {now - race_started:.1f}s after the request")
        elif winner.is_hedge and _carries_answer(winner.buffer[-1] if winner.buffer else None):
            setattr(call, "_kame_backup_won", True)       # 1.8.3.1: "a backup key of <model> answered first"
            _emit(call, "switch", identity=identity, key=fingerprint(winner.key),
                  reason=f"the backup key answered first, {now - race_started:.1f}s after the first request — the slow one was cut")
        return _RaceStream(winner, race_q), winner.key, winner.started

    def _flush(self, stitcher: Any, held_tools: List[Any], progress: "_Progress") -> Iterator[Any]:
        """At the end of an attempt: release held text, then the held tool calls."""
        tail = ""
        if stitcher is not None:
            tail = stitcher.flush()
            if tail:
                progress.touch()
                yield _text_chunk(tail)
        for chunk in held_tools:
            yield chunk
        held_tools.clear()
        return tail

    @staticmethod
    def _resume_budget(api_kwargs: Any, wire: Any = None) -> Optional[int]:
        """How many times this call may continue a cut answer. Zero disables it.

        1.8.1.8 also refused when the host had no delivery funnel to watch.
        At the client there is nothing to watch: every character Hermes shows
        passed through this transport's own iterator first.
        """
        if settings.is_on(settings.STREAM_STITCH_DISABLED):
            return 0
        if not (wire or wires.CHAT).resumable(api_kwargs):
            return 0
        fallback = settings.ALL_NUMBERS.get(settings.STREAM_RESUME_LIMIT, -1.0)
        try:
            value = int(settings.number(settings.STREAM_RESUME_LIMIT, fallback))
            return None if value < 0 else value
        except Exception:  # pragma: no cover — settings clamps before this
            return None if fallback < 0 else int(fallback)

    def _memo_key(self, call: "Call", identity: str) -> str:
        """The hand-over memo is per conversation (review H1): ``status_owner`` is
        one string per Hermes client, so another chat is never held back."""
        try:
            owner = call.status_owner()
        except Exception:
            owner = ""
        return f"{identity}|{owner if isinstance(owner, str) else id(owner)}"

    def _known_dry_keys(self, identity: str, keys: Sequence[str]) -> List[str]:
        """Keys usable now whose last outcome was a refusal and whose provider-named
        return is still in the future (decisions/0027)."""
        if not self._stated_back:
            return []
        now = time.time()
        try:
            pool = self.engine._pools.get(identity) or {}
            dry = []
            for key in keys:
                back = self._stated_back.get((identity, key), 0.0)
                state = pool.get(key)
                if back > now and state and state.get("kind") and float(state.get("sick_until") or 0.0) <= now:
                    dry.append(key)
            return dry
        except Exception:  # pragma: no cover - a read of the engine's own dicts
            return []

    def _burst_plan(
        self, call: "Call", identity: str, keys: Sequence[str], key: str,
        long_refusals: int, burst_round: int, *, stream: bool,
    ) -> Tuple[int, Sequence[str]]:
        """``(legs, keys to draw them from)`` for the next attempt; ``(0, keys)`` = one request.

        Known-dry keys (the provider said they are still spent) are asked together,
        but only when the key already chosen is one of them and only among them:
        a key with quota is never added to a probe (review H2). Otherwise the
        running-dry rule below. Background jobs never probe (each leg is a request
        for a title nobody is waiting on), and a call that does not stream asks at
        most 4 at once, because a losing leg there runs to the end.
        """
        if getattr(call, "channel", "main") != "main":
            return 0, keys
        try:
            from .core.hedging import HEDGE_TRACKER as _hedge

            if _hedge._mode() == "off":
                return 0, keys
            cap = int(settings.number(settings.PROBE_BURST, 16.0))
        except Exception:
            return 0, keys
        if cap < 2:
            return 0, keys
        dry = self._known_dry_keys(identity, keys)
        if key in dry and len(dry) >= 2:
            legs = min(cap, len(dry))
            return (legs, dry) if legs >= 2 else (0, keys)
        legs = self._burst_legs(identity, keys, long_refusals, burst_round, cap)
        if legs and not stream:
            legs = min(legs, 4)
        return (legs, keys) if legs >= 2 else (0, keys)

    def _burst_legs(self, identity: str, keys: Sequence[str], long_refusals: int, burst_round: int,
                    cap: int = 16) -> int:
        """How many keys the next attempt asks at once; 0 = the ordinary single request.

        1.8.2.7 (decisions/0027). Two refusals in a row that benched their key for a
        long rest say the pool is running dry: asking the remaining keys one by one
        costs the user ~0.8 s each (median of the owner's Gemini daily refusals).
        The next attempt asks 4 at once, the one after 8, then 16, capped by
        ``probe_burst_max_legs`` and by the keys that are not resting. Keys the
        provider itself said are still spent (:meth:`_known_dry`) start at 16. Off when
        backups are off (``hedge_mode`` off), because each extra leg is a request.
        """
        if long_refusals < 2 or cap < 2:
            return 0
        healthy = self.engine.healthy_count(identity, keys)
        legs = min(cap, healthy, 4 * (2 ** max(0, burst_round)))
        return legs if legs >= 2 else 0

    def _burst_create(
        self,
        call: "Call",
        identity: str,
        label: str,
        attempt: int,
        key: str,
        keys: List[str],
        client: Any,
        request: Any,
        *,
        legs: int,
        started: float,
    ) -> Tuple[Any, str, float]:
        """One non-streaming request asked of ``legs`` keys at once (decisions/0027).

        The same contract as :meth:`_hedged_create`: the first useful answer wins
        and returns at once; every other leg is judged on its own key when it ends
        (:meth:`_drain_hedge_losers`); if no leg answers, the primary's error is
        raised so the caller's blame on ``key`` is right.
        """
        import concurrent.futures
        import contextvars

        try:
            from .core.pacing import PACER as _pacer
        except Exception:  # pragma: no cover - the module ships with the plugin
            _pacer = None
        chosen = [key]
        while len(chosen) < legs:
            others = [k for k in keys if k not in chosen
                      and not (_pacer is not None and _pacer.same_scope(k, key))]
            if not others:
                break
            extra, status = self.engine.select(identity, others)
            if not extra or status != "SUCCESS":
                break
            chosen.append(extra)
        if len(chosen) < 2:
            return call.wire.create(client, request), key, started
        self.probe_bursts += 1
        for _ in range(len(chosen) - 1):
            self._tally(_SPEED_BURST)
            self._count_request(call, identity)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen), thread_name_prefix="kame-probe")
        futures: Dict[Any, Tuple[str, float, bool]] = {}
        try:
            for index, leg_key in enumerate(chosen):
                leg_client = client if index == 0 else call.client_for(leg_key, attempt)
                leg_started = started if index == 0 else time.monotonic()
                future = executor.submit(contextvars.copy_context().run, call.wire.create, leg_client, request)
                futures[future] = (leg_key, leg_started, index > 0)
            primary = next(iter(futures))
            _emit(call, "switch", identity=identity, key=fingerprint(key),
                  reason=f"asking {len(chosen)} keys at once — the last keys were out of quota")
            pending = set(futures)
            failed: List[Any] = []
            empty: List[Any] = []
            while pending:
                done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                failed.extend(f for f in done if f.exception() is not None)
                answered = [f for f in done if f.exception() is None]
                empty.extend(f for f in answered if _result_is_empty(f.result()))
                winner = next((f for f in answered if f not in empty), None)
                if winner is not None:
                    for other in failed + empty + list(pending):
                        self._queue_hedge_loser(other, futures[other], call, identity, label, attempt, False)
                    win_key, win_started, _ = futures[winner]
                    return winner.result(), win_key, win_started
            if empty:
                chosen_future = primary if primary in empty else empty[0]
                for f in failed + [f for f in empty if f is not chosen_future]:
                    self._queue_hedge_loser(f, futures[f], call, identity, label, attempt, False)
                chosen_key, chosen_started, _ = futures[chosen_future]
                return chosen_future.result(), chosen_key, chosen_started
            for f in failed:
                if f is not primary:
                    self._queue_hedge_loser(f, futures[f], call, identity, label, attempt, False)
            raise primary.exception()
        finally:
            executor.shutdown(wait=False)
            self._drain_hedge_losers()

    def _known_dry(self, identity: str, keys: Sequence[str]) -> int:
        """How many of ``keys`` are known dry (:meth:`_known_dry_keys`)."""
        return len(self._known_dry_keys(identity, keys))

    def _note_chain_arrival(self, identity: str) -> None:
        """A call on a model Hermes could only have reached by switching clears the
        hand-overs that pointed to it (decisions/0027)."""
        if not self._handover_later:
            return
        try:
            from .core.fallback_chain import _same

            for memo_key, later in list(self._handover_later.items()):
                if any(_same(identity, entry) for entry in later):
                    self._handover_later.pop(memo_key, None)
                    self._handover_until.pop(memo_key, None)
        except Exception:  # pragma: no cover - bookkeeping must never break a call
            logger.debug("kame: could not note a fallback arrival", exc_info=True)

    def _origin_of(self, call: "Call", identity: str = "") -> str:
        """The model this reply STARTED on (lower case): the root of its ladder. (1.8.3.1, decisions/0034 D1)

        The position of the first call of the reply: ``call.identity``, the model Hermes asked for - the
        main model, a list model picked by hand, anything. A call on an entry that a hand-over (or Hermes'
        own walk, noted by the classification hook) just moved a reply to is the SAME reply walking down
        the list: a live permit names the model it started on. Decided once per call and kept on it; a
        call that cannot hold the attribute (a test double) is decided again, which gives the same answer.
        """
        cached = getattr(call, "_kame_origin", None)
        if isinstance(cached, str) and cached:
            return cached
        position = str(getattr(call, "identity", "") or identity or "").lower()
        origin = position
        try:
            # 1.8.3.1 (review R1): a call that IS on the main model is a fresh reply - Hermes never walks
            # TO the main model, so no permit can name a reply that "arrived" there.
            if not _is_main_model(position):
                origin = _permit_origin(position, self._walk_permits, _HOST_WALK_PERMITS) or position
        except Exception:
            logger.debug("kame: could not tell where this reply started", exc_info=True)
        try:
            # 1.8.3.1 (review R4): the model string Hermes sent for the FIRST call of a reply is how the user
            # wrote its origin; keep it (also for the calls Hermes rebuilds on other entries of the walk).
            if origin == position:
                sent = str(getattr(call, "_kame_home_model", None) or (getattr(call, "kwargs", None) or {}).get("model") or "")
                if sent:
                    self._origin_written[origin] = sent
                    while len(self._origin_written) > 32:
                        self._origin_written.pop(next(iter(self._origin_written)))
        except Exception:  # pragma: no cover - a test double
            pass
        try:
            setattr(call, "_kame_origin", origin)
        except Exception:  # pragma: no cover - a test double without attributes
            pass
        return origin

    def _ladder(self, call: "Call", identity: str = "") -> List[str]:
        """``ladder(origin)``: the models this reply may use, in order (``fallback_chain.ladder``)."""
        from .core import fallback_chain

        return fallback_chain.ladder(self._origin_of(call, identity))

    def _later(self, call: "Call", identity: str = "", *, local: bool = False) -> List[str]:
        """The ladder entries after the CURRENT position (1.8.3.1, decisions/0034 D1).

        The position is the one Hermes is on (``call.identity``): Hermes' chain only goes down inside a
        reply, and a circle or a race win moves KAME's own model in place, so what Hermes can be handed
        is decided from where Hermes is (1.8.2.9 review H1). ``local`` reads from the model KAME itself
        is using instead (the legs KAME races beside it)."""
        from .core import fallback_chain

        position = (identity if local else (getattr(call, "identity", "") or identity))
        return fallback_chain.after(self._origin_of(call, identity), position)

    def _hermes_later(self, call: "Call", identity: str = "") -> List[str]:
        """What HERMES can be handed after the current position (1.8.3.1, review R2).

        ``_later`` without the main model unless it is the reply's origin: Hermes' walk is its own list
        (``fallback_providers``) and never goes TO the main model, so the main model is a candidate for
        KAME's own uses (stand-in, race, widen, circle back) but never for a hand-over. (When the main model
        IS the origin the ladder does not hold it after the origin, so nothing is dropped.)"""
        return [entry for entry in self._later(call, identity) if not _is_main_model(entry)]

    def _entry_usable(self, identity: str) -> bool:
        """Could a call on this chain entry be answered now, as far as KAME knows?

        An entry KAME has never seen is assumed usable (it may not be a pool at
        all); one it manages is usable if any of its keys is not resting.
        """
        try:
            name, pool = self._pool_of(identity)
            if not pool:
                return True
            return self.engine.healthy_count(name, list(pool)) > 0
        except Exception:
            return True

    def _pool_of(self, identity: str):
        """``(engine identity, pool)`` for a chain entry, matched the way KAME names pools
        (lower case, optional ``:global`` style suffix)."""
        wanted = identity.lower()
        for name, pool in list(self.engine._pools.items()):
            if name == wanted or name.startswith(wanted + ":"):
                return name, pool
        return wanted, None

    def _chain_would_switch(self, identity: str) -> bool:
        """True when handing a failure to Hermes could make it switch model by itself
        although the user asked KAME to ignore the fallback list (the flag is ON)."""
        try:
            from .core import fallback_chain

            return ((settings.is_on(settings.NO_MODEL_FALLBACK) or settings.model_switch_mode() == "never")
                    and bool(fallback_chain.ladder(identity)[1:]))      # 1.8.3.1: Hermes walks its list from any model
        except Exception:
            return False

    def _soonest_back_s(self, identity: str) -> Optional[float]:
        try:
            name, pool = self._pool_of(identity)
            if not pool:
                return None
            return self.engine.next_recovery_seconds(name, list(pool))
        except Exception:
            return None

    # -- 1.8.2.8 (decisions/0028) ----------------------------------------------

    def _busy_failure(self, identity: str, key: str, kind: str, status: Any = None) -> bool:
        """A failure in which the PROVIDER says it is busy, not this key.

        A 5xx / "overloaded", or a timeout the provider itself answered with
        (408 / 499 / 504 / 524). Not a timeout on our side - KAME's own
        first-token cut or Hermes' read timeout says nothing about the
        provider (review H2) - and not a throttle, which can be one key's
        own per-minute quota (review M5).
        """
        if kind == "server":
            return True
        if kind == "timeout":
            try:
                return int(status) in _PROVIDER_TIMEOUT_STATUSES
            except (TypeError, ValueError):
                return False
        return False

    def _note_busy(self, identity: str, *, first: float = _BUSY_FIRST_S,
                   ceiling: float = _BUSY_MAX_S) -> None:
        """This call just proved the provider busy (or slow): open, or double, its window.

        It doubles when the model is caught again within one window of the last
        one ending (a reply a minute later in the same storm), not only 30 s."""
        now = time.time()
        self._probed_at[identity] = now   # 1.8.2.9: the evidence is fresh as of now
        self._busy_kind[identity] = "slow" if first >= _SLOW_FIRST_S else "busy"   # for the panel
        until = self._busy_until.get(identity, 0.0)
        window = self._busy_window.get(identity, 0.0)
        if window and now < until + max(_BUSY_FIRST_S, window):
            window = min(ceiling, max(first, window * 2.0))  # review R10
        else:
            window = first
        self._busy_window[identity] = window
        self._busy_until[identity] = now + window

    def _is_busy(self, identity: str, busy_keys: set) -> bool:
        """Two keys busy in this call, or one while the provider is known busy.

        "Known" lasts the busy window and as long again after it: inside the
        window a reply is handed over without a request (the call start), and
        the first knock after it decides alone whether the storm is still on."""
        if len(busy_keys) >= 2:
            return True
        return bool(busy_keys) and time.time() < self._busy_until.get(identity, 0.0) + _BUSY_FIRST_S

    def _aux_refusal(self, identity: str, keys: Sequence[str], aux: Any) -> str:
        """Why a background job should not even start, or ``""`` (decisions/0032 A3, step 1).

        A dict lookup and two reads, secondary calls only. A job that was stopped a moment ago is
        answered without a request (the memo), so Hermes' own retries of it cost nothing."""
        now = time.time()
        if now < self._aux_memo.get((identity, aux.klass), 0.0):
            return "it was stopped a moment ago"
        if aux.klass == "cosmetic":
            if now < self._busy_until.get(identity, 0.0):
                return "its model is busy"
            if self._quota_pressure(identity, keys):
                return "its keys are short of quota"
            if self.engine.healthy_count(identity, keys) == 0:
                return "every key is resting"
        elif self.engine.healthy_count(identity, keys) == 0:
            eta = self.engine.next_recovery_seconds(identity, keys)
            if eta is not None and eta > aux.total_s:
                return "every key rests longer than its budget"
        return ""

    def _quota_pressure(self, identity: str, keys: Sequence[str]) -> bool:
        """Half the keys resting for a quota reason (not a 503, not a timeout)."""
        try:
            now = time.time()
            pool = self.engine._pools.get(identity) or {}
            resting = sum(
                1 for key in keys
                if (pool.get(key) or {}).get("kind") not in _NOT_QUOTA_KINDS
                and float((pool.get(key) or {}).get("sick_until") or 0.0) > now
            )
            return resting >= len(keys) * 0.5
        except Exception:
            return self.engine.healthy_count(identity, keys) < len(keys) * 0.5

    def _auto_switch_s(self, identity: str, call: "Call") -> float:
        """'Automatic': about three times this model's usual first word, 6-20 s."""
        try:
            from .core.hedging import HEDGE_TRACKER as _hedge, reasoning_model as _is_reasoning

            kwargs = getattr(call, "kwargs", None) or {}
            stats = _hedge.first_word_stats(
                identity, reasoning=_is_reasoning(str(kwargs.get("model") or ""), kwargs))
        except Exception:
            stats = None
        if not stats:
            return _AUTO_SWITCH_COLD_S
        return float(min(_AUTO_SWITCH_MAX_S, max(_AUTO_SWITCH_MIN_S, 3.0 * stats[0])))

    def _slow_after(self, call: "Call", identity: str, reasoning: bool) -> Optional[float]:
        """Seconds without a first sign of work after which this model counts as slow.

        1.8.2.8 (owner's live test, decisions/0028 D11): gemini-3.8-flash took two
        minutes to a one-word answer while gemini-3.7-flash answered in three
        seconds. ``None`` - wait as before - unless the reply could fall back:
        the conversation itself, Fallback not "Never", and a later model of the
        list that is neither resting nor known slow or busy. "Set time" is the
        user's number; "Automatic" is 3 x the median of this model's own
        first-word times, clamped (20-45 s, thinking models 25-60 s; 30 / 45 s
        before anything is learned). Not the p95: a model slow today fills its own
        tail with today's slowness, and the bound would grow to its ceiling (live:
        90 s, 2026-10-18).
        """
        try:
            if getattr(call, "channel", "main") != "main" or settings.is_on(settings.NO_MODEL_FALLBACK):
                return None
            mode = settings.model_switch_mode()
            if mode == "never":
                return None
            later = self._hermes_later(call, identity)     # review H1; 1.8.3.1: the ladder of the reply's origin; R2: what Hermes can be handed
            if not later or not any(self._entry_free(e) for e in later):
                # decisions/0032 B4 (review M6): nothing after Hermes' position is free, but a model
                # of the list (an earlier one, best first) could still join the race at the bound.
                try:
                    call_keys = list(call.keys())
                except Exception:
                    call_keys = None
                if self._widen_candidate(call, identity, call_keys, [identity]) is None:
                    return None
            if mode == "timed":
                seconds = settings.number(settings.FAILOVER_AFTER, 60.0)
                return float(seconds) if seconds > 0 else None
            floor, ceiling, cold = _SLOW_S[bool(reasoning)]
            from .core.hedging import HEDGE_TRACKER as _hedge

            stats = _hedge.first_word_stats(identity, reasoning=bool(reasoning))
            # review R9: a long prompt reads longer before the first word
            extra = _estimate_prompt_tokens(getattr(call, "kwargs", None) or {}) / _SLOW_TOKENS_PER_S
            if not stats:
                return cold + extra
            return float(min(ceiling, max(floor, 3.0 * stats[0])) + extra)
        except Exception:
            logger.debug("kame: could not size the slow-model watch", exc_info=True)
            return None

    def _cross_target(self, call: "Call", identity: str, reasoning: bool,
                      slow_s: float) -> Optional[List[Tuple[str, str, float]]]:
        """``[(identity, model, at_s), ...]``: the next models to race a silent reply, or None.

        The first joins at ``after_s``; if it is silent too, the one after it joins
        half that later (6 s at least, before the slow bound) - live, gemini-3.7
        was as slow as 3.8 while 3.6 answered in 2 s (D12).

        1.8.2.8 (owner, live test, decisions/0028 D12): speed comes before the
        first model's priority. Waiting for the slow bound and then starting over
        on the next model cost 25 s per slow model; racing the next model once the
        first is clearly late costs one extra request and no wait. Only the next
        entry of the list on the SAME provider (same keys, same endpoint: only the
        model name changes), usable and not known busy, and not in Saver mode.
        ``after_s``: 3 x this model's usual first word - after a backup key of the
        SAME model has had its chance (2 x) - 8 s at least and
        well before the slow bound; 12 s before anything is learned.
        """
        try:
            if str(settings.text_value(settings.HEDGE_MODE) or "adaptive") == "off":
                return None
            from .core import fallback_chain
            from .core.hedging import HEDGE_TRACKER as _hedge

            order = self._ladder(call, identity)      # 1.8.3.1: the ladder of the reply's origin
            mine = next((e for e in order if fallback_chain._same(identity, e)), None)
            if mine is None:
                return None
            mine_src = fallback_chain.entry_source(mine)
            if mine_src is None:
                return None
            suffix = identity[len(mine):]
            provider = mine.split(":", 1)[0]
            stats = _hedge.first_word_stats(identity, reasoning=bool(reasoning))
            after = float(min(_CROSS_COLD_S if not stats else max(_CROSS_MIN_S, 3.0 * stats[0]),
                              0.5 * slow_s))
            step = max(_CROSS_STEP_S, 0.5 * after)
            out: List[Tuple[str, str, float]] = []
            for entry in self._later(call, identity, local=True):
                if entry.split(":", 1)[0] != provider:
                    continue
                if not self._entry_free(entry):      # decisions/0032 B4: known busy is raced only when due
                    continue
                src = fallback_chain.entry_source(entry)
                if src is None or (src[1] and src[1] != mine_src[1]):
                    # review R7: the next model names an endpoint other than this one's -
                    # another provider here. An entry that names none uses the provider's
                    # own (live: the owner's primary names it, his fallbacks do not).
                    continue
                at = after + step * len(out)
                if out and at >= 0.85 * slow_s:
                    break
                out.append((entry + suffix, src[0], float(at)))
                if len(out) == _CROSS_MAX_LEGS:
                    break
            return out or None
        except Exception:
            logger.debug("kame: could not pick a model to race", exc_info=True)
            return None

    def _start_probe_plan(self, call: "Call", identity: str, stream: bool,
                          keys: Sequence[str]) -> Optional[Tuple[str, str]]:
        """``(identity, model)`` of the model the reply should START on while ``identity`` is asked again.

        1.8.2.9 (decisions/0032 C, amends D8). The model Hermes asked for is inside its busy window
        and is DUE (20 s since it was last asked). D8 would hand the reply to the next model without a
        request; the reply still starts on that model at once, and the busy one runs BESIDE it as a
        race leg, so a recovered best model is found within 20 s and nothing is added to the wait.
        Only for a streamed chat reply, not in Saver, not under "Never"; the next free entry after
        Hermes' position must be a same-provider, same-endpoint sibling (anything else is Hermes' own
        switch). The probe time is stamped under the lock, so two conversations probe once.
        """
        try:
            if not stream or getattr(call, "channel", "main") != "main":
                return None
            if str(settings.text_value(settings.HEDGE_MODE) or "adaptive") == "off":
                return None
            if settings.is_on(settings.NO_MODEL_FALLBACK) or settings.model_switch_mode() == "never":
                return None
            entry = self._chain_entry(identity, self._ladder(call, identity))
            if entry is None:
                return None
            later = self._later(call, identity)
            target = next((e for e in later if self._entry_free(e)), None)
            if target is None:
                return None
            siblings = {e: (m, s) for e, m, s in
                        (self._siblings(identity, origin=self._origin_of(call, identity)) or [])}
            if target not in siblings:
                return None
            model, suffix = siblings[target]
            if self._quota_pressure(target + suffix, keys):
                return None
            with self._probe_lock:
                if not self._entry_due(entry):
                    return None
                target_busy = self._entry_busy(target)
                if target_busy and not self._entry_due(target):
                    return None
                now = time.time()
                self._probed_at[entry] = now
                if target_busy:
                    self._probed_at[target] = now
            return target + suffix, model
        except Exception:
            logger.debug("kame: could not plan a probe of the busy model", exc_info=True)
            return None

    def _chain_entry(self, identity: str, order: Optional[Sequence[str]] = None) -> Optional[str]:
        """The entry of the user's list (or of ``order``, a reply's ladder) an identity - pool suffix and
        all - belongs to, or ``None``."""
        try:
            from .core import fallback_chain

            if order is None:
                primary, chain = fallback_chain.read()
                order = ([primary] if primary else []) + list(chain)
            return next((e for e in order if fallback_chain._same(identity, e)), None)
        except Exception:
            return None

    def _claim_probe(self, identity: str) -> bool:
        """Check-and-stamp, under one lock, before a request goes to a model KAME knows busy.

        ``True`` when the model is not known busy (nothing to claim), or it is busy and DUE and now
        stamped, so it is not asked again for ``_PROBE_EVERY_S``; ``False`` when another conversation
        got there first (decisions/0032 B4; review finding 5, 2026-10-24)."""
        try:
            entry = self._chain_entry(identity)
            if not entry or not self._entry_busy(entry):
                return True
            with self._probe_lock:
                if not self._entry_due(entry):
                    return False
                self._probed_at[entry] = time.time()
                return True
        except Exception:  # pragma: no cover - a dict write
            return True

    def _mark_stand_in(self, call: "Call", identity: str, activity: Dict[str, Any]) -> Dict[str, Any]:
        """A reply answered by a model other than the one it started on says so (1.8.3.1, D2).

        ``phase: "standin"`` rides beside ``kind: "calling"`` (every reader of the old kinds keeps working),
        ``model`` names the model that cannot answer - the reply's origin - and ``other`` the one answering,
        as the shared table of words asks. ``identity`` stays the model in use. Only a conversation's reply."""
        try:
            if getattr(call, "channel", "main") != "main":
                return activity
            from .core.fallback_chain import _same

            origin = self._origin_of(call)
            if origin and identity and not _same(identity, origin):
                activity["phase"] = "standin"
                activity["model"] = model_label(origin)
                activity["other"] = model_label(identity)
        except Exception:  # pragma: no cover - a readout must never cost a reply
            logger.debug("kame: could not tell if a stand-in is answering", exc_info=True)
        return activity

    def _publish_backup(self, call: "Call", identity: str, attempt: int, keys: Sequence[str], key: str,
                        race_started: float) -> None:
        """A backup KEY of the same model was launched beside the first one: say so (1.8.3.1, D2 ``racing``).

        Not for a burst of keys after a run of empty-quota answers (that model is not slow), and a
        stand-in keeps its own line."""
        try:
            activity = {
                "kind": "calling", "phase": "racing", "identity": identity, "model": model_label(identity),
                "attempt": attempt, "healthy": self.engine.healthy_count(identity, keys), "keys": len(keys),
                "started_at": time.time() - (time.monotonic() - race_started), "key": fingerprint(key),
            }
            _publish(self, self._mark_stand_in(call, identity, activity))
        except Exception:  # pragma: no cover - a readout must never cost a reply
            logger.debug("kame: could not say a backup key is asking", exc_info=True)

    def _publish_racing(self, identity: str, attempt: int, keys: Sequence[str], key: str,
                        race_started: float, cross_legs: Sequence[Any], probe: Optional[str] = None,
                        call: Optional["Call"] = None) -> None:
        """Tell the panel which models are racing this reply (``activity.racing``, 0032 B5).

        A busy model being asked again at the start (``probe``) is named apart: the home model is
        not late, the best model is only being asked beside it."""
        try:
            names = [model_label(getattr(leg, "cross")) for leg in cross_legs
                     if not leg.done and getattr(leg, "cross", None) != probe]
            activity = {
                "kind": "calling", "identity": identity, "model": model_label(identity), "attempt": attempt,
                "healthy": self.engine.healthy_count(identity, keys), "keys": len(keys),
                "started_at": time.time() - (time.monotonic() - race_started), "key": fingerprint(key),
                "racing": names,
            }
            if probe and any(not leg.done and getattr(leg, "cross", None) == probe for leg in cross_legs):
                activity["probe"] = model_label(probe)
            _publish(self, self._mark_stand_in(call, identity, activity) if call is not None else activity)
        except Exception:  # pragma: no cover - a readout must never cost a reply
            logger.debug("kame: could not say which models are racing", exc_info=True)

    def _outside_race_free(self, call: "Call", identity: str, taken: Sequence[str]) -> bool:
        """Is some entry after Hermes' position free that is NOT already racing this reply?

        Only such an entry makes the D11 hand-over worth cutting the race for (decisions/0032 B3)."""
        try:
            from .core import fallback_chain

            later = self._hermes_later(call, identity)      # 1.8.3.1 (review R2): the hand-over is Hermes'
            return any(self._entry_free(e) and not any(fallback_chain._same(x, e) for x in taken)
                       for e in later)
        except Exception:
            return True

    def _widen_candidate(self, call: "Call", identity: str, keys: Optional[Sequence[str]],
                         taken: Sequence[str]) -> Optional[Tuple[str, str, str]]:
        """``(entry, model, suffix)`` of the model that may join a silent reply at the slow bound.

        1.8.2.9 (decisions/0032 B2). The first model of the user's list, best first (earlier entries
        included: that closes review M6, every later model known slow), on the same provider and
        endpoint, not already racing, usable, free (known busy only if due) and not under quota
        pressure. Never for a background job, in Saver, or under "Never". It must come no later in
        the list than the first free entry outside the race: otherwise the D11 hand-over to that
        entry is the better move. Pure of side effects; the caller stamps the probe time."""
        try:
            if getattr(call, "channel", "main") != "main":
                return None
            if str(settings.text_value(settings.HEDGE_MODE) or "adaptive") == "off":
                return None
            if settings.is_on(settings.NO_MODEL_FALLBACK) or settings.model_switch_mode() == "never":
                return None
            candidates = self._siblings(identity, origin=self._origin_of(call, identity))
            if not candidates:
                return None
            from .core import fallback_chain

            _same = fallback_chain._same
            taken = list(taken) + list(getattr(call, "_kame_avoid", None) or [])
            order = self._ladder(call, identity)      # 1.8.3.1: best first = the ladder's own order
            later = self._later(call, identity)
            outside = [order.index(e) for e in later
                       if e in order and self._entry_free(e) and not any(_same(x, e) for x in taken)]
            limit = min(outside) if outside else None
            for entry, model, suffix in candidates:
                if any(_same(x, entry) for x in taken) or entry not in order:
                    continue
                if not self._entry_free(entry):
                    continue
                if keys and self._quota_pressure(entry + suffix, keys):
                    continue
                if limit is not None and order.index(entry) > limit:
                    continue
                return entry, model, suffix
            return None
        except Exception:
            logger.debug("kame: could not pick a model to widen the race with", exc_info=True)
            return None

    def _entry_busy(self, entry: str) -> bool:
        """Whether KAME knows this chain entry busy or slow right now."""
        try:
            from .core.fallback_chain import _same

            now = time.time()
            return any(_same(ident, entry) and now < until for ident, until in self._busy_until.items())
        except Exception:
            return False

    def _siblings(self, identity: str, *, include_self: bool = False,
                  origin: Optional[str] = None) -> Optional[List[Tuple[str, str, str]]]:
        """``[(entry, model as written, suffix), ...]``: the models of the user's list this
        call could be asked on - same provider, same endpoint (only the model name
        changes), in the list's own order, best first; the model in use too when
        ``include_self``. ``None`` without a list.

        1.8.2.9. The list is Hermes' own (``fallback_providers``); KAME only reads it, as
        the cross-model race of 1.8.2.8 does. An entry on another provider or another
        ``base_url`` is Hermes' to switch to, never KAME's. Review H3: an entry that names
        no ``base_url`` uses the provider's own endpoint, which is also where an entry that
        names one points in the owner's configuration, so an unnamed endpoint matches in
        BOTH directions (the draft excluded a primary that names one from a fallback that
        does not).

        1.8.3.1 (decisions/0034 D1): with ``origin`` the order is ``ladder(origin)`` - the model the
        reply started on first, then the main model and the list - instead of the list's own order."""
        try:
            from .core import fallback_chain

            primary, chain = fallback_chain.read()
            order = (fallback_chain.ladder(origin) if origin else
                     ([primary] if primary else []) + list(chain))
            mine = next((e for e in order if fallback_chain._same(identity, e)), None)
            if mine is None or len(order) < 2:
                return None
            mine_src = fallback_chain.entry_source(mine)
            if mine_src is None:
                return None
            suffix = identity[len(mine):]
            provider = mine.split(":", 1)[0]
            primary_src = fallback_chain.entry_source(primary) if primary else None

            def endpoint(entry: str, src: Tuple[str, str]) -> str:
                # An entry that names no base_url uses the provider's own endpoint - the one
                # the primary (what the user actually runs) points at; one that names its own
                # is "elsewhere" unless it is the same URL.
                if src[1]:
                    return src[1]
                return primary_src[1] if primary_src and primary and primary.split(":", 1)[0] == provider else ""

            mine_url = endpoint(mine, mine_src)
            out: List[Tuple[str, str, str]] = []
            for entry in order:
                if entry.split(":", 1)[0] != provider:
                    continue
                if entry == mine:
                    if include_self:
                        out.append((entry, mine_src[0], suffix))
                    continue
                src = fallback_chain.entry_source(entry)
                if src is None or endpoint(entry, src) != mine_url:
                    continue
                out.append((entry, src[0], suffix))
            return out
        except Exception:
            logger.debug("kame: could not read the models around this one", exc_info=True)
            return None

    def _entry_due(self, entry: str) -> bool:
        """Known busy, but not asked in the last ``_PROBE_EVERY_S``: worth another look."""
        return time.time() - self._probed(entry) >= _PROBE_EVERY_S

    def _entry_free(self, entry: str) -> bool:
        """Could Hermes be handed to this entry now? Usable, and either not known busy or
        due another look. A busy entry of another provider has no circle to reach it, so
        'due' must count here or it is unreachable for its whole window (review M5)."""
        return self._entry_usable(entry) and (not self._entry_busy(entry) or self._entry_due(entry))

    def _hermes_has_free_after(self, call: "Call", identity: str = "") -> bool:
        """Is some entry AFTER Hermes' own position (``call.identity``) free?

        Hermes' chain only goes down inside one reply and a circle moves KAME's own model
        in place, so the position that decides what Hermes can be handed is the one it
        started the call on, never the local model (review H1)."""
        try:
            from .core import fallback_chain

            # 1.8.3.1 (review R2): Hermes' own walk never goes TO the main model.
            return any(self._entry_free(entry) for entry in self._hermes_later(call, identity))
        except Exception:
            return True

    def _wrap_target(
        self, call: "Call", identity: str, *, seen: bool, left_asked: bool = False,
        visited: Sequence[str] = (), excluded: Sequence[str] = (), include_self: bool = False,
    ) -> Optional[Tuple[str, str]]:
        """``(identity, model)`` of the model this call should circle back to, or ``None``.

        1.8.2.9 (decisions/0030), owner's live test of 1.8.2.8: with the primary and every
        fallback model busy, the reply ended up waiting on the last - least capable - model
        of the list, because Hermes' chain only goes down and the last model has nothing
        after it. This is the way back up, and it applies only when NOTHING after Hermes'
        position is free to be handed to (``_hermes_has_free_after``): otherwise the
        hand-over, with its threshold ("Set time"), is the right tool (review M1).

        The first model of the user's list, best first, that is usable, not ``excluded`` in
        this call, and either not known busy or due another look; one left in this call
        (``visited``) is gated the same way even when its window has lapsed, so two models
        cannot trade the reply at the speed of a round trip (review H4). ``include_self``
        puts the model in use among the candidates (the call start: the best model, when it
        is due, is asked itself - review H2). Sequential, one request at a time - not a
        race. A model is stamped as asked only when a request goes to it (the chosen one) or
        when it was asked and left (``left_asked``); a model merely skipped is not (H2).
        Never with text on screen, never for a background job, never when the user said to
        stay on the model.
        """
        try:
            if seen or getattr(call, "channel", "main") != "main":
                return None
            if settings.is_on(settings.NO_MODEL_FALLBACK) or settings.model_switch_mode() == "never":
                return None
            if self._hermes_has_free_after(call, identity):
                return None
            candidates = self._siblings(identity, include_self=include_self,
                                        origin=self._origin_of(call, identity))
            if not candidates:
                return None
            from .core.fallback_chain import _same

            with self._probe_lock:
                for entry, model, suffix in candidates:
                    if any(_same(x, entry) for x in excluded):
                        continue
                    if not self._entry_usable(entry):
                        continue
                    seen_here = any(_same(x, entry) for x in visited)
                    if (self._entry_busy(entry) or seen_here) and not self._entry_due(entry):
                        continue
                    now = time.time()
                    self._probed_at[entry] = now
                    if left_asked:
                        self._probed_at[identity] = now
                    self.wrapped += 1
                    self._tally(_SPEED_CIRCLE)
                    return entry + suffix, model
            return None
        except Exception:
            logger.debug("kame: could not pick a model to circle back to", exc_info=True)
            return None

    def _announce_wrap(self, call: "Call", old: str, new: str, why: str) -> None:
        """Say the circle back where the user looks (Events row, status line).

        The status line keeps the shape the Desktop lets through (review L1)."""
        if why == "probe":
            # decisions/0032 C: the reply starts on the next model at once, and the best model is
            # asked again beside it.
            say = f"{model_label(old)} was busy moments ago - starting on {model_label(new)} and asking {model_label(old)} again beside it"
            line = f"⏳ waiting on {model_label(old)} — busy a moment ago, starting on {model_label(new)} and asking it again"
        else:
            say = f"{model_label(old)} {why} - trying {model_label(new)} (your list, best first)"
            line = f"⏳ waiting on {model_label(old)} — {why}, trying {model_label(new)} from your list"
        try:
            _emit(call, "handover", identity=old, reason=say, seconds=None)
            _Spinner.update(call.status_owner(), line, interval=0.0)
        except Exception:  # pragma: no cover - a courtesy line
            logger.debug("kame: could not say the circle back", exc_info=True)

    def forget_busy(self, entry: str) -> int:
        """The panel's *Try this model now*: drop one model's busy / slow window and its probe
        stamp, so the next reply asks it first. Returns how many windows were dropped."""
        from .core.fallback_chain import _same

        dropped = 0
        for ident in [i for i in list(self._busy_until) if _same(i, entry)]:
            self._busy_until.pop(ident, None)
            self._busy_window.pop(ident, None)
            self._busy_kind.pop(ident, None)
            dropped += 1
        for ident in [i for i in list(self._probed_at) if _same(i, entry)]:
            self._probed_at.pop(ident, None)
        for memo in [m for m in list(self._handover_until) if _same(m.split("|", 1)[0], entry)]:
            self._handover_until.pop(memo, None)
        return dropped

    def _tally(self, index: int) -> None:
        """One integer add where a reply ends or an extra request starts (the cost readout)."""
        try:
            minute = int(time.time() // 60)
            row = self._speed.get(minute)
            if row is None:
                row = self._speed[minute] = [0, 0, 0, 0, 0]
                for old in [m for m in self._speed if m < minute - 60]:
                    self._speed.pop(old, None)
            row[index] += 1
            self._speed_total[index] += 1
        except Exception:  # pragma: no cover - a counter must never cost a reply
            pass

    def _count_request(self, call: "Call", identity: str) -> None:
        """One integer add at every request start: the chat's or a background job's (decisions/0032 A7)."""
        try:
            hour = int(time.time() // 3600)
            bucket = self._requests.get(hour)
            if bucket is None:
                bucket = self._requests[hour] = {}
                self._request_tasks[hour] = {}
                for old in [h for h in list(self._requests) if h < hour - 23]:
                    self._requests.pop(old, None)
                    self._request_tasks.pop(old, None)
            row = bucket.get(identity)
            if row is None:
                row = bucket[identity] = [0, 0]
            if getattr(call, "channel", "main") == "main":
                row[0] += 1
            else:
                row[1] += 1
                key = (identity, str(getattr(call, "task", "") or "?")[:40])
                tasks = self._request_tasks[hour]
                tasks[key] = tasks.get(key, 0) + 1
        except Exception:  # pragma: no cover - a counter must never cost a reply
            pass

    def quota_use_view(self) -> Dict[str, Any]:
        """The last day of requests per model, split chat / background, and the jobs stopped early.

        ``since`` moves once an hour (the oldest bucket), never continuously, so two snapshots with
        no request between them are identical and the publisher writes nothing."""
        hour = int(time.time() // 3600)
        models: Dict[str, Dict[str, Any]] = {}
        for h in sorted(list(self._requests)):
            if h < hour - 23:
                continue
            for identity, (main, side) in list(self._requests.get(h, {}).items()):
                row = models.setdefault(identity, {"main": 0, "side": 0, "tasks": {}})
                row["main"] += main
                row["side"] += side
            for (identity, task), count in list(self._request_tasks.get(h, {}).items()):
                row = models.setdefault(identity, {"main": 0, "side": 0, "tasks": {}})
                row["tasks"][task] = row["tasks"].get(task, 0) + count
        return {"since": max(self._requests_since, (hour - 23) * 3600.0), "models": models,
                "stopped": dict(self._aux_stopped)}

    def speed_view(self) -> Dict[str, Any]:
        """Extra requests as a share of replies, the last hour and since this process started."""
        minute = int(time.time() // 60)
        rows = [list(r) for m, r in list(self._speed.items()) if m > minute - 60]
        hour = [sum(col) for col in zip(*rows)] if rows else [0] * 5

        def pack(v: List[int]) -> Dict[str, Any]:
            replies, backup, race, burst, circle = v
            extra = backup + race + burst
            return {"replies": replies, "backup": backup, "race": race, "burst": burst, "circle": circle,
                    "extra_pct": round(100.0 * extra / replies, 1) if replies else None}

        return {"hour": pack(hour), "since_start": pack(list(self._speed_total))}

    def busy_view(self, entry: str) -> Dict[str, Any]:
        """What the panel says about one chain entry's busy state (1.8.2.9, decisions/0031 2.6).

        ``busy_until``: epoch the model is left alone until (``None`` when it is not known busy);
        ``busy_kind``: ``"busy"`` (the provider said overloaded) or ``"slow"`` (silent too long);
        ``probe_at``: epoch the circle back may ask it again. Read in the publisher thread."""
        try:
            from .core.fallback_chain import _same

            now = time.time()
            until = max((u for ident, u in list(self._busy_until.items()) if _same(ident, entry) and u > now),
                        default=0.0)
            if not until:
                return {"busy_until": None, "busy_kind": None, "probe_at": None}
            kinds = [self._busy_kind.get(ident) for ident, u in list(self._busy_until.items())
                     if _same(ident, entry) and u > now]
            probe = self._probed(entry)
            return {"busy_until": until, "busy_kind": ("slow" if "slow" in kinds else "busy"),
                    "probe_at": (probe + _PROBE_EVERY_S) if probe else None}
        except Exception:
            return {"busy_until": None, "busy_kind": None, "probe_at": None}

    def _probed(self, entry: str) -> float:
        """When this chain entry was last asked or left while busy (any pool suffix)."""
        try:
            from .core.fallback_chain import _same

            return max((at for ident, at in list(self._probed_at.items()) if _same(ident, entry)), default=0.0)
        except Exception:
            return 0.0

    def _next_probe_in(self, identity: str) -> Optional[float]:
        """Seconds until a sibling known busy may be asked again, or ``None`` when none waits."""
        try:
            siblings = self._siblings(identity)
            if not siblings:
                return None
            now = time.time()
            waits = [
                max(0.0, self._probed(entry) + _PROBE_EVERY_S - now)
                for entry, _model, _suffix in siblings
                if self._entry_usable(entry) and self._entry_busy(entry)
            ]
            return min(waits) if waits else None
        except Exception:
            return None

    def _note_reply_facts(self, call: "Call", requests: int, resumes: int, waited: float) -> None:
        """What this call went through before it answered, for the reply row (1.8.3.1, D3)."""
        try:
            setattr(call, "_kame_facts", {"attempts": int(requests), "resumes": int(resumes), "waited_s": float(waited)})
        except Exception:  # pragma: no cover - a test double without attributes
            pass

    def _note_failed(self, call: "Call", error: BaseException) -> None:
        """A reply that ended without an answer gets a row too (1.8.3.1, D3: ``no answer: <reason>``).

        Only a conversation's reply, and not a hand-over (``KamePoolResting`` is a plan, not a failure).
        The reason is one of a few fixed sentences - never the provider's own text, which can carry anything."""
        try:
            if getattr(call, "channel", "main") != "main" or isinstance(error, KamePoolResting):
                return
            status = getattr(error, "status_code", None)
            name = type(error).__name__.lower()
            if isinstance(status, int) and not isinstance(status, bool):
                reason = f"the provider answered {status}"
            elif "timeout" in name or "timed out" in str(error).lower():
                reason = "it timed out"
            elif "cancel" in name or "interrupt" in name:
                return
            else:
                reason = "the request failed"
            # review R8: Hermes retries a failed reply (agent.api_max_retries), each retry a new call - one reply,
            # one row. The same conversation failing on the same model again within the window refreshes the row.
            now = time.time()
            try:
                owner = call.status_owner()
            except Exception:
                owner = ""
            key = f"{getattr(call, 'identity', '')}|{owner if isinstance(owner, str) else id(owner)}"
            held = self._failed_rows.get(key)
            if held is not None and now - held[0] < _FAILED_ROW_WINDOW_S:
                held[1].update({"at": now, "reason": reason})
                self._failed_rows[key] = (now, held[1])
                return
            row = {"at": now, "model": model_label(str(getattr(call, "identity", "") or "")),
                   "for": "", "first_s": None, "total_s": None, "failed": True, "reason": reason}
            self.replies.append(row)
            self._failed_rows[key] = (now, row)
            for old in [k for k, (at, _row) in self._failed_rows.items() if now - at >= _FAILED_ROW_WINDOW_S]:
                self._failed_rows.pop(old, None)
        except Exception:  # pragma: no cover - a readout must never cost a reply
            logger.debug("kame: could not note a failed reply", exc_info=True)

    def _after_answer(self, call: "Call", identity: str, progress: "_Progress") -> None:
        """An answer: the provider is not busy any more, and the reply is named."""
        identity = getattr(call, "_kame_answered_by", None) or identity
        self._busy_until.pop(identity, None)
        self._busy_window.pop(identity, None)
        self._probed_at.pop(identity, None)   # 1.8.2.9: an answer ends the probing of it
        if getattr(call, "channel", "main") != "main":
            return
        self._tally(_SPEED_REPLIES)
        try:
            from .core import fallback_chain

            started = getattr(call, "_kame_started", None)
            now = time.monotonic()
            total = (now - started) if isinstance(started, (int, float)) else None
            first = progress.first_text_at or progress.first_sign_at
            first_s = (first - started) if (first and isinstance(started, (int, float))) else None
            # 1.8.3.1 (D1): a reply is a stand-in when it was answered by a model other than the one it
            # STARTED on - the main model or a pick of the user's, whichever.
            origin = self._origin_of(call, identity)
            stand_in = bool(origin) and not fallback_chain._same(identity, origin)
            row = {"at": time.time(), "model": model_label(identity),
                   "for": model_label(origin) if stand_in else "",
                   "first_s": first_s, "total_s": total}
            if stand_in:
                # 1.8.3.1 (D1): which model the selector should show again, as the user wrote it - the
                # model the session was on, never "the main model" - and whether Hermes itself moved off
                # it (a hand-over to another provider). A sibling that answered inside KAME left Hermes
                # where it was: nothing to put back. ``main`` is the alias of 1.8.3.0, kept for one version.
                # review R4: the entry as the config wrote it (unsuffixed), else the string Hermes sent for the
                # reply's first call; with neither, no back_to - a lower-cased guess could name another model.
                steps = fallback_chain.ladder(origin)
                written = fallback_chain.entry_source(steps[0] if steps else origin)
                name = written[0] if written else self._origin_written.get(origin)
                if name:
                    back_to = {"provider": origin.split(":", 1)[0], "model": name}
                    row["back_to"] = back_to
                    row["main"] = dict(back_to)
                position = str(getattr(call, "identity", "") or "").lower()
                row["switched"] = bool(position) and not fallback_chain._same(position, origin)
                why = str(getattr(call, "_kame_standin_why", "") or "")
                if why:
                    row["why"] = why
            # 1.8.3.1 (D3): what the popover's "Last replies" tells besides the model and the time
            facts = getattr(call, "_kame_facts", None) or {}
            if int(facts.get("attempts") or 0) > 1:
                row["attempts"] = int(facts["attempts"])        # answered on the Nth request of this call
            if int(facts.get("resumes") or 0) > 0:
                row["cut"] = True                                # the answer was cut off and finished on another key
            if float(facts.get("waited_s") or 0.0) >= _REPLY_WAITED_NOTE_S:
                row["waited_s"] = round(float(facts["waited_s"]), 1)
            if getattr(call, "_kame_backup_won", False):
                row["backup"] = True                             # a backup key of the same model answered first
            self.replies.append(row)
            mode = settings.reply_notice_mode()
            if mode == "never" or (mode == "switches" and not stand_in):
                return
            parts = []
            if first_s is not None and first_s >= 0:
                parts.append(f"first word {_short_secs(first_s)}")
            if total is not None:
                parts.append(f"{_short_secs(total)} total")
            text = row["model"] + (f" answered for {row['for']}" if stand_in else " answered")
            _turn_notice(text + (" · " + " · ".join(parts) if parts else ""))
        except Exception:  # a notice must never cost the answer
            logger.debug("kame: could not name the reply", exc_info=True)

    def _stand_in_for(self, call: "Call", identity: str, later: Sequence[str], avoid: Sequence[str],
                      why: str, slow: Optional[float], skipped: Optional[float]) -> Optional[_StandIn]:
        """The sibling that answers instead of a hand-over, or ``None`` (D2 a).

        The target is the first usable model after Hermes' position, as the hand-over would have
        chosen it; it must be on the same provider and endpoint (``_siblings``) and not the model in
        use nor one this call already left. Saver does NOT forbid it: this is not a race, it is the
        fallback the user configured, answered on keys KAME already holds. A model known busy is only
        taken when it is due another look (the probe time is claimed under the lock)."""
        try:
            from .core.fallback_chain import _same

            target = next((e for e in later if self._entry_free(e) and not _same(identity, e)
                           and not any(_same(x, e) for x in avoid)), None)
            if target is None:
                return None
            siblings = {e: (m, s) for e, m, s in
                        (self._siblings(identity, origin=self._origin_of(call, identity)) or [])}
            if target not in siblings:
                return None
            model, suffix = siblings[target]
            if not self._claim_probe(target + suffix):
                return None
            if skipped is not None:
                said = "was slow or busy moments ago"
            elif slow is not None:
                said = "is slow right now"
            elif why == "busy":
                said = "is busy"
            else:
                said = "has every key resting" if self.engine.healthy_count(identity, list(call.keys())) == 0 \
                    else "did not answer in time"
            return _StandIn(target + suffix, model, said)
        except Exception:
            logger.debug("kame: could not pick a sibling to stand in", exc_info=True)
            return None

    def _maybe_hand_over(
        self, call: "Call", identity: str, keys: Sequence[str], pool_waited: float, seen: bool,
        *, waited: Optional[float] = None, busy: bool = False, after_failure: bool = False,
        slow: Optional[float] = None, skipped: Optional[float] = None,
        stand_in_ok: bool = False, avoid: Sequence[str] = (),
    ) -> Optional[Any]:
        """The exception that lets Hermes' fallback chain answer, or ``None`` to wait.

        1.8.3.0 (decisions/0033 D2 a). With ``stand_in_ok`` the caller can also take a
        :class:`_StandIn`: when the first usable later model is a sibling (same provider, same
        endpoint) KAME answers with it itself and Hermes never switches. ``avoid`` are models this
        call already left or that refused it. Anything else is still a hand-over.

        1.8.2.7, owner's idea 1 (decisions/0025, 0027). Every condition must hold:
        "Ignore my fallback list" is off (the default since 0027); the threshold
        is not 0; Hermes has a chain and some entry AFTER this one is usable;
        nothing has reached the screen (never stitch two models); the wait this
        call would spend (already waited + soonest key back) passes the
        threshold; and Hermes was not already handed this model in the last
        few minutes (a second ask waits, so a chain that cannot be used never
        ends the turn with an error).
        """
        try:
            if seen or settings.is_on(settings.NO_MODEL_FALLBACK):
                return None
            if getattr(call, "channel", "main") != "main":
                return None  # background jobs never switch model (0025 D6, review H4)
            mode = settings.model_switch_mode()
            if mode == "never":
                return None
            from .core import fallback_chain

            # 1.8.2.9 (decisions/0030, review H1): what Hermes can be handed is decided from
            # the position Hermes is on (``call.identity``), not from the local model, which
            # a circle or a race win may have moved - Hermes' chain only goes down.
            # 1.8.3.1 (decisions/0034 D1): the entries after that position in the ladder of the model
            # the reply started on, whichever model that is.
            origin = self._origin_of(call, identity)
            later = self._later(call, identity)
            if not later:
                return None
            # 1.8.3.1 (review R2): what Hermes itself can be handed (its list, never the main model unless
            # it is the origin); ``later`` stays whole for KAME's own stand-in.
            hand = self._hermes_later(call, identity)
            # Only worth handing over if something after that position could answer - a
            # chain whose every later entry is known busy was walked down to its last, least
            # capable model. KAME circles back through the list instead (``_wrap_target``).
            if not any(self._entry_free(entry) for entry in later):
                return None
            eta = self.engine.next_recovery_seconds(identity, keys)
            # 1.8.2.8: the clock is the user's wait, not only the time every
            # key rested (a 503 rests a key 1 s, so that clock never moved).
            waited_s = max(pool_waited, waited if waited is not None else 0.0)
            if busy:
                # A busy provider moves on whatever the time: "my time" is how
                # long to wait for resting keys, not for a model that says it
                # cannot answer (review M6). "Never" returned above.
                why = "busy"
            else:
                threshold = (settings.number(settings.FAILOVER_AFTER, 60.0) if mode == "timed"
                             else self._auto_switch_s(identity, call))
                if threshold <= 0:
                    return None
                if after_failure:
                    if waited_s <= threshold:
                        return None
                elif eta is None or waited_s + max(0.0, eta) <= threshold:
                    return None
                why = "waited"
            if stand_in_ok:
                stand = self._stand_in_for(call, identity, later, avoid, why, slow, skipped)
                if stand is not None:
                    return stand
            if not any(self._entry_free(entry) for entry in hand):
                return None         # only the main model is free, and Hermes cannot be handed to it
            now = time.time()
            memo_key = self._memo_key(call, identity)
            if now < self._handover_until.get(memo_key, 0.0):
                return None
            if not any(self._entry_usable(entry) for entry in hand):
                return None
            if why == "busy":
                back = self._busy_window.get(identity, _BUSY_FIRST_S)
            else:
                back = self._soonest_back_s(origin) if origin else None
                if back is None:
                    back = eta if eta is not None else 0.0
                # A slow model gets a short break before it is asked again.
                back = max(back, _HANDOVER_MIN_BACK_S)
            ceiling = settings.number(settings.MAX_HOLD, 3600.0)
            back_at = now + min(max(back, 0.0), ceiling)
            # 1.8.2.8 (owner, live test): the fallback lasts one reply. Hermes is
            # told the primary is back in a moment, so its own turn-start restore
            # (agent_runtime_helpers.restore_primary_runtime) puts the best model
            # first on the next reply; while KAME still knows it busy or resting,
            # that reply hands over again before any request (see the call start).
            reset_at = now + _HANDOVER_HOST_BACK_S
            self._handover_until[memo_key] = min(back_at, now + _HANDOVER_MEMO_S)
            self._handover_later[memo_key] = tuple(hand)
            # 1.8.3.0 (D1): the reply Hermes rebuilds on one of these entries is this reply's walk.
            # 1.8.3.1 (review R1): never the main model - Hermes does not walk to it.
            for entry in hand:
                self._walk_permits[entry] = (now + _WALK_PERMIT_S, origin)
            healthy = self.engine.healthy_count(identity, keys)
            if skipped is not None:
                said = (f"{model_label(identity)} was slow or busy moments ago - skipped for this "
                        f"reply, tried again in {format_duration(max(skipped, 0.0))}")
            elif slow is not None:
                said = (f"{model_label(identity)} is slow right now (no first word after "
                        f"{format_duration(slow)})")
            elif why == "busy":
                said = f"{model_label(identity)} is busy (overloaded or timing out on several keys)"
            elif eta is not None and healthy == 0:
                said = (f"every key of {model_label(identity)} is resting, the next back in "
                        f"{format_duration(max(eta, 0.0))}")
            else:
                said = f"no answer from {model_label(identity)} after {format_duration(waited_s)}"
            message = f"kame: {said}. Handing the call to Hermes' fallback model."
            # Not counted as "surfaced" (an error KAME let through): a planned switch.
            self.handed_over += 1
            _emit(call,
                "handover",
                identity=identity,
                reason=f"{said} -> Hermes answers with {later[0]}",
                seconds=eta,
            )
            try:
                _Spinner.update(call.status_owner(),
                                f"⏳ falling back to {model_label(later[0])} — {said}",
                                interval=0.0)
            except Exception:  # pragma: no cover - the status line is a courtesy
                logger.debug("kame: could not say the switch on the status line", exc_info=True)
            return KamePoolResting(message, reset_at)
        except Exception:  # a readout of the chain must never break a call
            logger.debug("kame: could not decide on a fallback hand-over", exc_info=True)
            return None

    def _wait_for_recovery(
        self, call: "Call", identity: str, keys: Sequence[str], vigil: "_Vigil",
        budget: Optional[float] = None,
    ) -> Iterator[Any]:
        """Sleep until the soonest key is usable. Returns ``False`` to stop waiting.

        The 1.8.1.8 wait, unchanged in what it decides: no ceiling on the total
        (ADR 0002), slept in one-second slices so a stop is honoured within a
        second, re-checked at most every minute so an early recovery is used at
        once. Two things differ because it now runs inside Hermes' own request:
        a stop arrives as Hermes closing this client rather than as a flag on
        the agent, and the generator yields a keep-alive every
        :attr:`KEEPALIVE_S` so Hermes' silence watchdog sees a live provider.
        """
        eta = self.engine.next_recovery_seconds(identity, keys)
        healthy = self.engine.healthy_count(identity, keys)
        total = len(keys)
        vigil.maybe_speak(healthy, total, eta)
        if eta is None:
            wait = _SLEEP_SLICE_S
        else:
            wait = min(eta + 0.5 + self._jitter(), _MAX_SLEEP_S)
        if wait <= 0:
            wait = _SLEEP_SLICE_S
        if budget is not None:
            # Never sleep past the operator's total bound; the caller checks
            # it again on the way back in.
            wait = max(min(wait, budget), 0.0)
        logger.debug(
            "kame: %s every key is resting — waiting %s (no requests sent); "
            "earliest recovery %s",
            vigil.label,
            format_duration(wait),
            "now" if eta is None else f"in {format_duration(eta)}",
        )
        self.waits += 1
        if eta is not None:
            _emit(call, 
                "wait",
                identity=identity,
                reason=f"every key resting — {healthy} of {total} usable",
                seconds=eta,
            )
        slept = 0.0
        since_keepalive = 0.0
        while slept < wait:
            if call.cancelled():
                self.waited_s += slept
                return False
            remaining = None if eta is None else max(eta - slept, 0.0)
            _Spinner.update(
                call.status_owner(),
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
            _publish(
                self,
                {
                    "kind": "waiting",
                    "identity": identity,
                    "model": model_label(identity),
                    "healthy": healthy,
                    "keys": total,
                    "eta_s": remaining,
                },
            )
            slice_s = min(_SLEEP_SLICE_S, wait - slept)
            self._sleep(slice_s)
            slept += slice_s
            since_keepalive += slice_s
            if since_keepalive >= self.KEEPALIVE_S:
                since_keepalive = 0.0
                yield _KEEPALIVE
            try:
                live_keys = call.keys()
                if any(key not in keys for key in live_keys):
                    break
            except Exception:
                logger.debug("kame: could not inspect the keys during a wait", exc_info=True)
            if eta is not None and slept < eta and self.engine.healthy_count(identity, keys) > 0:
                break
        self.waited_s += slept
        return True


class _ToolCallLeftOpen(ConnectionError):
    """A stream that ended cleanly with a tool call still half written."""


def _tool_names(chunks: Sequence[Any]) -> List[str]:
    names: List[str] = []
    for chunk in chunks:
        for choice in getattr(chunk, "choices", None) or []:
            for call in getattr(getattr(choice, "delta", None), "tool_calls", None) or []:
                name = getattr(getattr(call, "function", None), "name", None)
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
    return names


class _Done:
    """The end of a drive, carrying the non-streaming answer (or ``None``)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


#: Marker a drive yields when Hermes should be shown a sign of life.
_KEEPALIVE = object()

#: The "leave this field alone" default for :func:`_clone_chunk`.
_KEEP = object()


def keepalive_chunk() -> Any:
    """A chunk with no choices: Hermes counts it as liveness and reads nothing from it."""
    return SimpleNamespace(id=None, model=None, object="chat.completion.chunk", created=int(time.time()),
                           choices=[], usage=None)


def _text_chunk(text: str) -> Any:
    """A plain text delta, for words the stitcher released at the end of an attempt."""
    delta = SimpleNamespace(role=None, content=text, tool_calls=None)
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=None, logprobs=None)
    return SimpleNamespace(id=None, model=None, object="chat.completion.chunk", created=int(time.time()),
                           choices=[choice], usage=None)


def _finish_only(chunk: Any, reason: Any) -> Any:
    """The finish of ``chunk``, without any of its payload.

    A view of another wire's event keeps that event (``wires``): on the
    Messages wire the finish *is* an event of its own, and dropping it would
    lose the end of the message.
    """
    delta = SimpleNamespace(role=None, content=None, tool_calls=None)
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=reason, logprobs=None)
    finish = SimpleNamespace(id=getattr(chunk, "id", None), model=getattr(chunk, "model", None),
                             object="chat.completion.chunk", created=getattr(chunk, "created", int(time.time())),
                             choices=[choice], usage=getattr(chunk, "usage", None))
    for name in (wires.NATIVE, wires.ATTEMPT):
        if hasattr(chunk, name):
            setattr(finish, name, getattr(chunk, name))
    return finish


def _set(target: Any, name: str, value: Any) -> None:
    try:
        setattr(target, name, value)
    except Exception:
        object.__setattr__(target, name, value)


def _clone_chunk(chunk: Any, *, content: Any = _KEEP, finish: Any = _KEEP) -> Any:
    """``chunk`` with its first choice's text and/or finish replaced; the original is untouched."""
    import copy

    clone = copy.copy(chunk)
    choices = list(getattr(chunk, "choices", None) or [])
    if not choices:
        return clone
    choice = copy.copy(choices[0])
    delta = getattr(choice, "delta", None)
    if delta is not None:
        delta = copy.copy(delta)
        if content is not _KEEP:
            _set(delta, "content", content)
        _set(choice, "delta", delta)
    if finish is not _KEEP:
        _set(choice, "finish_reason", finish)
    choices[0] = choice
    _set(clone, "choices", choices)
    return clone


#: 1.8.2.5. Delta fields through which providers stream a thinking model's
#: reasoning before its answer (OpenAI-compatible servers, DeepSeek, NIM).
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def _carries_answer(chunk: Any) -> bool:
    """1.8.2.5: whether a chunk is the model *working* — text, a tool call, or
    streamed reasoning.

    The stream race is decided on this. A role chunk or an empty delta says
    nothing about how soon the answer comes, so it never counts. Streamed
    reasoning does: live on the owner's NVIDIA GLM-5.3 pool (2026-10-12) a
    text-only rule started backups on replies that were already thinking,
    doubling the load on two keys and making the replies slower. A backup is
    for a request that is stuck — silent — not for one that is working.
    """
    if _native_is_thinking(getattr(chunk, wires.NATIVE, None)):
        return True
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return False
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return False
    text = getattr(delta, "content", None)
    if isinstance(text, str) and text:
        return True
    if getattr(delta, "tool_calls", None):
        return True
    for name in _REASONING_FIELDS:
        value = getattr(delta, name, None)
        if isinstance(value, str) and value:
            return True
    return False


#: 1.8.2.5. Native Anthropic Messages block/delta types that are thinking.
_MESSAGES_THINKING = ("thinking", "redacted_thinking", "thinking_delta", "signature_delta")


def _native_is_thinking(native: Any) -> bool:
    """Whether a Messages or Responses event (carried on a view) is reasoning.

    The Messages wire hands the transport its thinking events as views with
    no text (``wires._view``), so the chat-shaped check alone would read a
    Anthropic Messages reply that is thinking as silent and start a backup on it.
    """
    if native is None:
        return False

    def field(obj: Any, name: str) -> Any:
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    kind = str(field(native, "type") or "")
    if "reasoning" in kind:  # Responses: response.reasoning_*.delta and friends
        return True
    if kind == "content_block_delta":
        return str(field(field(native, "delta"), "type") or "") in _MESSAGES_THINKING
    if kind == "content_block_start":
        return str(field(field(native, "content_block"), "type") or "") in _MESSAGES_THINKING
    return False


class _StreamLeg:
    """1.8.2.5: one leg of a stream race — opened and read on its own thread.

    Everything it reads goes into the race's one queue as ``(leg, kind,
    payload)``: ``"chunk"``, then ``"end"`` or ``"error"``. It also stands in
    for the call when the wire wants to attach the HTTP response, so a losing
    leg never overwrites the winner's.
    """

    def __init__(self, race_q: "queue.Queue[Tuple[_StreamLeg, str, Any]]", key: str,
                 client: Any, *, is_hedge: bool) -> None:
        self.q = race_q
        self.key = key
        self.client = client
        self.is_hedge = is_hedge
        self.started = time.monotonic()
        self.buffer: List[Any] = []
        self.done = False
        self.error: Optional[BaseException] = None
        self.response: Any = None
        self.stream: Any = None
        self.stop = threading.Event()

    def attach_response(self, response: Any) -> None:
        self.response = response

    def start(self, wire: Any, request: Any) -> None:
        # The leg runs in a copy of the caller's context, so whatever the host
        # keeps in context variables travels with the request.
        context = contextvars.copy_context()
        threading.Thread(target=context.run, args=(self._pump, wire, request),
                         name="kame-stream-race", daemon=True).start()

    def _pump(self, wire: Any, request: Any) -> None:
        stream = None
        try:
            stream = wire.open(self.client, request, self)
            self.stream = stream
            for chunk in stream:
                if self.stop.is_set():
                    return
                self.q.put((self, "chunk", chunk))
            self.q.put((self, "end", None))
        except BaseException as exc:  # noqa: BLE001 — handed to the race, judged there
            if not self.stop.is_set():
                self.q.put((self, "error", exc))
        finally:
            if stream is not None:
                _close_quietly(stream)

    def abort(self) -> None:
        """Stop this leg now: its HTTP response is closed under the reader."""
        self.stop.set()
        for holder in (self.stream, self.response):
            response = getattr(holder, "response", holder) if holder is self.stream else holder
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - closing a finished response
                    logger.debug("kame: could not close a race leg", exc_info=True)


class _RaceStream:
    """1.8.2.5: the winning leg, read like any provider stream.

    Its word-less chunks held during the race come first, then the rest as the
    leg's thread reads them. ``close`` aborts the leg, as closing a provider
    stream would.
    """

    def __init__(self, leg: _StreamLeg, race_q: "queue.Queue[Tuple[_StreamLeg, str, Any]]") -> None:
        self.leg = leg
        self.q = race_q

    def __iter__(self) -> Iterator[Any]:
        held, self.leg.buffer = self.leg.buffer, []
        for chunk in held:
            yield chunk
        if self.leg.done:
            if self.leg.error is not None:
                raise self.leg.error
            return
        while True:
            leg, kind, payload = self.q.get()
            if leg is not self.leg:
                continue
            if kind == "chunk":
                yield payload
            elif kind == "end":
                return
            else:
                raise payload

    def close(self) -> None:
        self.leg.abort()


def _close_quietly(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # pragma: no cover - closing a finished stream
            logger.debug("kame: could not close a provider stream", exc_info=True)


def configure(transport: "KameTransport") -> "KameTransport":
    """Push the owner's dials onto the carousel this transport marks and selects against.

    ``dispatch_binding.install`` (1.8.1.8) did this once at registration, and
    every line of it exists because a dial that is declared and never pushed is
    decorative (RED_TEAM.md F3/F4): the daily cooldown, the hold ceiling, the
    unsized-throttle rest and its doubling ladder, and whether the pool health
    file is shared across profiles. Called from ``register`` after
    ``settings.load``, so the values are the configured ones.
    """
    from .core import shared_health

    engine = transport.engine
    engine.daily_cooldown_s = settings.number(settings.DAILY_COOLDOWN, engine.daily_cooldown_s)
    engine.max_hold_s = settings.number(settings.MAX_HOLD, engine.max_hold_s)
    engine.unsized_throttle_rest_s = settings.number(
        settings.UNSIZED_THROTTLE_REST, engine.unsized_throttle_rest_s
    )
    engine.unsized_throttle_backoff = settings.is_on(settings.UNSIZED_THROTTLE_BACKOFF)
    engine.unsized_backoff_max_s = settings.number(
        settings.UNSIZED_BACKOFF_MAX, engine.unsized_backoff_max_s
    )
    shared_health.set_config_default(settings.is_on(settings.SHARE_POOL_HEALTH))
    return transport
