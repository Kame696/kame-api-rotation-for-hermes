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

import json
import logging
import os
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import host_text, recorder, runtime, settings, timings
from .core import evidence, multikey, stitch, vocabulary
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
    notice goes to the log and the Events screen, and the wait itself is on
    the Desktop chip through ``_publish`` for as long as it lasts.
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
        logger.info("kame: %s", message)


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


def attempt_read_timeout(base_url: Any, attempt: int) -> Optional[float]:
    """The per-attempt stream read timeout, or ``None`` to leave Hermes' own.

    1.8.1.8's ``_SilenceTimeout`` rules, unchanged: only when the user set
    ``stream_silence_timeout_seconds``, never over an explicit
    ``HERMES_STREAM_READ_TIMEOUT``, never for a local endpoint, and a quarter
    of the value from the third attempt on (1.2.5: the provider is proven slow,
    so the remaining keys get a shorter leash). 1.8.1.8 scoped it through a
    ContextVar read by a wrapped host reader; the client applies it directly to
    the request it owns.
    """
    seconds = settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0)
    if seconds <= 0 or _looks_local(base_url):
        return None
    if os.environ.get("HERMES_STREAM_READ_TIMEOUT") is not None:
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
        if is_terminal(exc, message) and not (verdict is not None and kind == "insufficient_quota"):
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
        if calendar_reset:
            applied = self.engine.mark(identity, key, False, delay, kind,
                                       stated=True, calendar_reset=True, scope=account_scope)
        else:
            applied = self.engine.mark(identity, key, False, delay, kind, stated=stated, scope=account_scope,
                                       bare_resource_exhausted=bare)
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
        for item in self._drive(call, stream=False):
            if isinstance(item, _Done):
                return item.value
        raise RuntimeError("kame: the transport ended without an answer")  # pragma: no cover

    def stream(self, call: "Call") -> Iterator[Any]:
        """A streaming request. Yields provider chunks, plus keep-alives while it waits."""
        for item in self._drive(call, stream=True):
            if isinstance(item, _Done):
                return
            if item is _KEEPALIVE:
                yield keepalive_chunk()
                continue
            yield item

    def _plain(self, call: "Call", stream: bool) -> Iterator[Any]:
        """No key to choose: the request goes out exactly as Hermes built it."""
        client = call.client_for("", 1)
        if not stream:
            yield _Done(client.chat.completions.create(**call.kwargs))
            return
        for chunk in client.chat.completions.create(**dict(call.kwargs, stream=True)):
            yield chunk
        yield _Done(None)

    def _drive(self, call: "Call", *, stream: bool) -> Iterator[Any]:
        """One API call, as many keys as it takes — ``DispatchBinding.run`` (1.8.1.8), at the client."""
        try:
            keys = call.keys()
        except Exception:
            logger.debug("kame: could not read the keys for this call", exc_info=True)
            keys = []
        if not keys:
            yield from self._plain(call, stream)
            return

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
        consecutive_timeouts = 0
        # The answer as the user has seen it, across every attempt of this
        # call (1.1.1): both the text a continuation is prefilled with and the
        # text it is trimmed against.
        seen = ""
        resumes = 0
        resume_budget = self._resume_budget(call.kwargs) if stream else 0
        budget_label = resume_budget if resume_budget is not None else "ongoing"
        # 1.6.0.0: which keys were asked to continue and added nothing. This,
        # not a count, ends the stitching loop.
        stalled: set = set()
        tool_cut_keys: set = set()
        replay_tool_request = False
        verdicts: Dict[str, Tuple[str, Optional[int]]] = {}

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

            key, status = self.engine.select(identity, keys)
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
                wait_started = time.monotonic()
                recovered = yield from self._wait_for_recovery(call, identity, keys, vigil)
                pool_waited += max(0.0, time.monotonic() - wait_started)
                if not recovered:
                    if seen:
                        self.mid_stream_cuts += 1
                        raise call.cut(last_error)
                    if last_error is not None:
                        self.surfaced += 1
                        raise host_text.take_off_the_advice(last_error)
                    raise call.cancel_error()
                slept = True
                try:
                    keys = call.keys() or keys
                except Exception:
                    logger.debug("kame: could not refresh the keys after a wait", exc_info=True)
                continue

            if attempt > 1:
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
            try:
                runtime.note_sent(call.provider, call.entry_id(key), fingerprint(key), now=time.time())
            except Exception:  # pragma: no cover — a ContextVar set and a hash
                logger.debug("kame: could not note the key in flight", exc_info=True)

            healthy = self.engine.healthy_count(identity, keys)
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
            shown = ""
            held_tools: List[Any] = []
            saw_tools = False
            finished = False
            answered_with_text = False
            result: Any = None

            try:
                client = call.client_for(key, attempt)
                if not stream:
                    result = client.chat.completions.create(**request)
                else:
                    raw = client.chat.completions.create(**dict(request, stream=True))
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
            except BaseException as exc:  # noqa: BLE001 — re-raised below unless rotatable
                last_error = exc
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
                        EVENTS.add(
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
                    credential_id=counted_as(call.entry(key), key),
                )
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
                )
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
                        EVENTS.add(
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
                    EVENTS.add(
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
                    self.rotations += 1
                    continue
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
                EVENTS.add(
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
                    EVENTS.add(
                        "stitch",
                        identity=identity,
                        key=fingerprint(key),
                        reason=f"continuing the answer on another key ({resumes}/{budget_label})",
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
                EVENTS.add(
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
            )
            self.engine.mark(identity, key, True)
            # An answer that carried nothing is not proof the key is back
            # (v0.0.9 / ``core.answer``): refuse to count it, as the
            # ``post_api_request`` hook always has.
            carried = (bool(answered_with_text or saw_tools or shown or seen) if stream
                       else not _result_is_empty(result))
            if carried:
                try:
                    runtime.note_answered(call.provider, identity, counted_as(call.entry(key), key))
                except Exception:  # pragma: no cover — a journal write must not cost the answer
                    logger.debug("kame: could not record the answer in the journal", exc_info=True)
            if vigil is not None:
                vigil.done()
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
                EVENTS.add(
                    "recovery",
                    identity=identity,
                    key=fingerprint(key),
                    reason=f"answered on attempt {attempt}",
                    seconds=elapsed,
                )
            _publish(self, None)
            yield _Done(result)
            return

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
    def _resume_budget(api_kwargs: Any) -> Optional[int]:
        """How many times this call may continue a cut answer. Zero disables it.

        1.8.1.8 also refused when the host had no delivery funnel to watch.
        At the client there is nothing to watch: every character Hermes shows
        passed through this transport's own iterator first.
        """
        if settings.is_on(settings.STREAM_STITCH_DISABLED):
            return 0
        if stitch.resumable(api_kwargs) is None:
            return 0
        fallback = settings.ALL_NUMBERS.get(settings.STREAM_RESUME_LIMIT, -1.0)
        try:
            value = int(settings.number(settings.STREAM_RESUME_LIMIT, fallback))
            return None if value < 0 else value
        except Exception:  # pragma: no cover — settings clamps before this
            return None if fallback < 0 else int(fallback)

    def _wait_for_recovery(
        self, call: "Call", identity: str, keys: Sequence[str], vigil: "_Vigil"
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
        logger.debug(
            "kame: %s every key is resting — waiting %s (no requests sent); "
            "earliest recovery %s",
            vigil.label,
            format_duration(wait),
            "now" if eta is None else f"in {format_duration(eta)}",
        )
        self.waits += 1
        if eta is not None:
            EVENTS.add(
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
    """The finish of ``chunk``, without any of its payload."""
    delta = SimpleNamespace(role=None, content=None, tool_calls=None)
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=reason, logprobs=None)
    return SimpleNamespace(id=getattr(chunk, "id", None), model=getattr(chunk, "model", None),
                           object="chat.completion.chunk", created=getattr(chunk, "created", int(time.time())),
                           choices=[choice], usage=getattr(chunk, "usage", None))


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
