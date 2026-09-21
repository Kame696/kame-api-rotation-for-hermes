"""Two parallel Gemini tool calls, merged into one broken argument string.

The symptom the user sees is ``Response truncated due to output length limit``
on a turn that was nowhere near any length limit, usually after a search. The
log line one step upstream is the one that names it::

    agent.message_sanitization: Unrepairable tool_call arguments for
    web_search - replaced with empty object
    (was: {"query": "..."}{"query": "..."})

Two complete JSON objects in the argument string of a single tool call. Here is
where they come from.

``agent/gemini_native_adapter.translate_stream_event`` turns each streamed
Gemini event into OpenAI-shaped chunks. A ``functionCall`` part is given a slot
in ``tool_call_indices``, keyed by::

    {"part_index": <position in this event's parts>, "name": ..., "thought_signature": ...}

and the slot remembers ``last_arguments`` so a *repeated* part (Gemini re-sends
the same call in later events) emits an empty delta instead of a duplicate.

When a model issues two calls to the same tool in one turn, both arrive as
``parts[0]`` of their own event, both are named ``web_search``, and neither
carries a ``thoughtSignature`` — so the key is identical and they land in the
same slot. The second call's arguments are not equal to the first's and are not
an extension of it, so the whole object is emitted as a delta on the index the
first call already owns, and the consumer, which accumulates deltas per index
by concatenation, ends up holding ``{...}{...}``.

Hermes then cannot repair it, substitutes ``{}``, reads the empty object as a
truncated tool call, retries four times, and gives up with the length-limit
message that has nothing to do with length.

**What this module does.** It wraps ``translate_stream_event`` and repairs the
output, rather than replacing the host's slot logic. The rule it applies is the
narrowest one that can distinguish the two cases:

    A delta is a *new call* — not a continuation — when the arguments already
    accumulated on that index parse as a complete JSON object **and** the
    incoming delta parses as a complete JSON object of its own.

That can never be true of a genuine continuation: once the accumulated text is
complete JSON, appending anything to it can only corrupt it. So when both halves
parse, the only reading is the one the log already showed. Such a delta is moved
to a fresh index with a fresh call id, and downstream sees the two calls Gemini
actually made.

**What makes it safe to ship in a rotation plugin.** It is guarded, not
assumed:

* it patches only if the installed ``translate_stream_event`` still has the
  three-parameter shape and still carries the ``last_arguments`` prefix logic
  that causes the merge — if Hermes fixes this upstream, the marker goes and
  KAME stops patching on its own;
* before patching it runs a synthetic two-call stream through the real function
  and requires the merge to actually happen and the repair to actually fix it,
  so the patch is never applied on the strength of source-reading alone;
* ``KAME_GEMINI_TOOL_CALL_FIX_DISABLED=1`` turns it off;
* ``/kame`` and the Desktop page both show whether it is applied, and if not,
  why.

This is host code, and KAME's business is keys. It is here because the failure
is invisible to everything else the user has, it lands on Gemini — the provider
this plugin exists for — and a rotation that works perfectly while every
search-shaped turn dies is not a working product.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING = "gemini_tool_call_fix_disabled"

#: Source markers that must still be present for the bug to exist as analysed.
#: Their absence is not an error — it is the upstream fix, and the right
#: response to it is to leave the host alone.
_MARKERS = ("last_arguments", "args_str.startswith(last_arguments)", "tool_call_indices")

#: How many concurrent streams are tracked. One entry is a small dict; the cap
#: only stops a long-lived gateway from growing one row per stream forever.
_MAX_STREAMS = 64

_lock = threading.Lock()
_streams: "OrderedDict[int, Dict[str, Any]]" = OrderedDict()

_state: Dict[str, Any] = {"applied": False, "reason": "not attempted", "repaired": 0}
_original: Optional[Any] = None


def report() -> Dict[str, Any]:
    """What to show the user: applied or not, why, and how often it has fired."""
    with _lock:
        return dict(_state)


def _complete_json_object(text: str) -> bool:
    """True for text that is, on its own, a whole JSON object.

    Anything else — a fragment, a bare scalar, an empty string — is False, so
    the split rule below can only fire on the exact shape the corruption has.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except (TypeError, ValueError):
        return False


def _tool_call_deltas(chunk: Any) -> List[Any]:
    """Every tool-call delta in one chunk, or an empty list.

    Written to survive a chunk shape it does not recognise rather than raise
    inside a live stream.
    """
    try:
        choices = getattr(chunk, "choices", None) or []
        out: List[Any] = []
        for choice in choices:
            delta = getattr(choice, "delta", None)
            calls = getattr(delta, "tool_calls", None) if delta is not None else None
            if calls:
                out.extend(calls)
        return out
    except Exception:  # pragma: no cover — defensive, a live stream is not a test
        return []


def _stream_state(indices: Dict[str, Any], fresh: bool) -> Dict[str, Any]:
    """Per-stream bookkeeping, keyed by the identity of the host's own dict.

    ``tool_call_indices`` is created empty once per stream
    (``gemini_native_adapter`` line ~1173) and threaded through every event, so
    it *is* the stream's identity. It cannot be weak-referenced and must not be
    written to — the host derives the next slot index from its length — hence a
    side table keyed by ``id()``, reset whenever a stream starts with an empty
    dict, which also makes an id reused by a later object harmless.
    """
    token = id(indices)
    with _lock:
        if fresh or token not in _streams:
            _streams[token] = {"acc": {}, "ids": {}, "remap": {}, "next": 0}
            _streams.move_to_end(token)
            while len(_streams) > _MAX_STREAMS:
                _streams.popitem(last=False)
        else:
            _streams.move_to_end(token)
        return _streams[token]


def _repair(chunks: List[Any], indices: Dict[str, Any], fresh: bool) -> int:
    """Move a second complete object off the index the first one owns.

    Returns how many deltas were moved, for the counter the panel shows.
    """
    state = _stream_state(indices, fresh)
    acc: Dict[int, str] = state["acc"]
    ids: Dict[int, str] = state["ids"]
    remap: Dict[int, int] = state["remap"]
    moved = 0

    for chunk in chunks:
        for call in _tool_call_deltas(chunk):
            try:
                source_index = int(getattr(call, "index", 0) or 0)
            except (TypeError, ValueError):
                continue
            target = remap.get(source_index, source_index)
            function = getattr(call, "function", None)
            text = getattr(function, "arguments", "") if function is not None else ""

            if text:
                current = acc.get(target, "")
                if current and _complete_json_object(current) and _complete_json_object(text):
                    # The corruption, caught before it is concatenated. A new
                    # index and a new id make this the separate call it is.
                    target = max([*acc.keys(), *remap.values(), state["next"], source_index]) + 1
                    remap[source_index] = target
                    ids[target] = f"call_{uuid.uuid4().hex[:12]}"
                    acc[target] = ""
                    state["next"] = target
                    moved += 1
                acc[target] = acc.get(target, "") + text

            if target != source_index:
                try:
                    call.index = target
                    if ids.get(target):
                        call.id = ids[target]
                except Exception:  # pragma: no cover — SimpleNamespace is writable
                    logger.debug("kame: could not re-index a Gemini tool call", exc_info=True)
            else:
                # Remember the host's own id for this index, so a later split
                # can tell the two calls apart by id as well as by index.
                identifier = getattr(call, "id", "")
                if identifier:
                    ids.setdefault(target, identifier)
    return moved


def _wrap(original: Any) -> Any:
    def translate_stream_event(event: Any, model: str, tool_call_indices: Dict[str, Any]) -> List[Any]:
        # Read emptiness BEFORE the call: the host fills the dict as it goes,
        # so afterwards every stream looks like a continuation.
        fresh = not tool_call_indices
        chunks = original(event, model, tool_call_indices)
        try:
            moved = _repair(chunks, tool_call_indices, fresh)
        except Exception:
            # A status feature must never be able to end a turn. If the repair
            # itself is broken, the user is exactly where they were without it.
            logger.debug("kame: the Gemini tool-call repair failed; passing through", exc_info=True)
            return chunks
        if moved:
            with _lock:
                _state["repaired"] = _state.get("repaired", 0) + moved
            logger.info(
                "kame: split %d merged Gemini tool call(s) that would have "
                "become 'Response truncated due to output length limit'",
                moved,
            )
        return chunks

    translate_stream_event.__kame_patched__ = True  # type: ignore[attr-defined]
    translate_stream_event.__wrapped__ = original  # type: ignore[attr-defined]
    return translate_stream_event


def _two_calls_event(query: str) -> Dict[str, Any]:
    return {
        "candidates": [
            {"content": {"parts": [{"functionCall": {"name": "web_search", "args": {"query": query}}}]}}
        ]
    }


def _self_check(original: Any) -> Tuple[bool, str]:
    """Reproduce the merge, then prove the repair fixes it.

    Source markers say the code still looks like the code that has the bug.
    This says the bug is still real in the interpreter that is running, and
    that the wrapper's answer is the right one. Patching on anything less is
    how 1.0.9 shipped a fix for a problem it had only read about.
    """
    try:
        indices: Dict[str, Any] = {}
        before = original(_two_calls_event("first"), "gemini-3.7-flash", indices)
        after = original(_two_calls_event("second"), "gemini-3.7-flash", indices)
        merged = [
            getattr(getattr(call, "function", None), "arguments", "")
            for chunk in [*before, *after]
            for call in _tool_call_deltas(chunk)
        ]
        joined = "".join(merged)
        if _complete_json_object(joined):
            return False, "this Hermes already keeps the two calls apart"
        indexes = {
            int(getattr(call, "index", 0))
            for chunk in [*before, *after]
            for call in _tool_call_deltas(chunk)
        }
        if len(indexes) > 1:
            return False, "this Hermes already gives the second call its own index"

        # The bug is real here. Now the repair, on a fresh stream.
        indices = {}
        first = original(_two_calls_event("first"), "gemini-3.7-flash", indices)
        _repair(first, indices, True)
        second = original(_two_calls_event("second"), "gemini-3.7-flash", indices)
        _repair(second, indices, False)
        repaired = {
            int(getattr(call, "index", 0))
            for chunk in [*first, *second]
            for call in _tool_call_deltas(chunk)
        }
        if len(repaired) != 2:
            return False, "the repair did not separate the two calls in this Hermes"
        return True, ""
    except Exception as exc:
        return False, f"the self-check could not run: {type(exc).__name__}: {exc}"
    finally:
        with _lock:
            _streams.clear()
            _state["repaired"] = 0


def apply() -> bool:
    """Patch if every guard passes. Returns whether the patch is now in place."""
    global _original
    from . import settings

    with _lock:
        if _state.get("applied"):
            return True

    if settings.is_on(SETTING):
        _set_state(False, "switched off (gemini_tool_call_fix_disabled)")
        return False

    try:
        from agent import gemini_native_adapter as adapter
    except Exception:
        _set_state(False, "this Hermes has no Gemini native adapter")
        return False

    original = getattr(adapter, "translate_stream_event", None)
    if original is None or not callable(original):
        _set_state(False, "translate_stream_event is gone")
        return False
    if getattr(original, "__kame_patched__", False):
        _set_state(True, "")
        return True

    try:
        import inspect

        parameters = list(inspect.signature(original).parameters)
        source = inspect.getsource(original)
    except Exception:
        _set_state(False, "translate_stream_event could not be inspected")
        return False

    if parameters[:3] != ["event", "model", "tool_call_indices"]:
        _set_state(False, "translate_stream_event has a different shape now")
        return False
    missing = [marker for marker in _MARKERS if marker not in source]
    if missing:
        # The most likely reason for a missing marker is that Hermes fixed it.
        _set_state(False, "this Hermes no longer merges the calls the way KAME repairs")
        return False

    ok, why = _self_check(original)
    if not ok:
        _set_state(False, why)
        return False

    adapter.translate_stream_event = _wrap(original)
    _original = original
    _set_state(True, "")
    logger.info(
        "kame: repairing merged Gemini parallel tool calls "
        "(agent.gemini_native_adapter.translate_stream_event)"
    )
    return True


def revert() -> None:
    """Put the host's own function back. For tests, and for a clean unload."""
    global _original
    if _original is None:
        return
    try:
        from agent import gemini_native_adapter as adapter

        adapter.translate_stream_event = _original
    except Exception:
        logger.debug("kame: could not revert the Gemini patch", exc_info=True)
    _original = None
    with _lock:
        _streams.clear()
    _set_state(False, "reverted")


def _set_state(applied: bool, reason: str) -> None:
    with _lock:
        _state["applied"] = applied
        _state["reason"] = reason
        _state.setdefault("repaired", 0)
