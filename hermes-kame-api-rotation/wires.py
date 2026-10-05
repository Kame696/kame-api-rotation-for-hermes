"""The three wires KAME's carousel speaks, seen through one shape.

``transport.KameTransport`` decides on Chat Completions chunks: text in
``choices[0].delta.content``, tool calls in ``delta.tool_calls``, the end in
``finish_reason``, liveness in a chunk with no choices. Every decision it makes
— which key, how long a refusal rests it, when to wait, when an answer was cut
and how to continue it, when an empty answer is a squeezed key — reads only
those four things.

The Anthropic Messages API and the OpenAI Responses API carry the same four
things in their own events. A :class:`Wire` turns a native response into
*views* the transport can read (``_kame_native`` keeps the original), and turns
what the transport hands back into what Hermes expects on that wire: the
native events, untouched when the transport passed them through, rebuilt when
it changed them (the stitcher trimming a repeated seam), synthesised when it
made them (a keep-alive, the words the stitcher released at the end of an
attempt).

1.8.1.8 ran the same carousel on every wire by wrapping Hermes' dispatch
functions, which catalog rule 9 forbids. These classes are how the client
Hermes asks a provider profile for carries it onto the other two wires.
"""

from __future__ import annotations

import copy
import itertools
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .core import stitch

logger = logging.getLogger(__name__)

#: Marks a view as made by a wire (as opposed to a real Chat Completions chunk).
NATIVE = "_kame_native"
#: Which attempt of the call a view came from.
ATTEMPT = "_kame_attempt"

_ATTEMPTS = itertools.count(1)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _view(native: Any, attempt: int, *, text: Optional[str] = None, tools: Optional[list] = None,
          finish: Optional[str] = None) -> Any:
    """A Chat-Completions-shaped view of one native event."""
    if text is None and tools is None and finish is None:
        view = SimpleNamespace(id=None, model=None, choices=[], usage=None)
    else:
        delta = SimpleNamespace(role=None, content=text, tool_calls=tools)
        choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish, logprobs=None)
        view = SimpleNamespace(id=None, model=None, choices=[choice], usage=None)
    setattr(view, NATIVE, native)
    setattr(view, ATTEMPT, attempt)
    return view


def _tool(name: Any = None) -> list:
    return [SimpleNamespace(index=0, id=None, type="function",
                            function=SimpleNamespace(name=name if isinstance(name, str) else None, arguments=None))]


def _view_text(view: Any) -> Optional[str]:
    choices = getattr(view, "choices", None) or []
    if not choices:
        return None
    delta = getattr(choices[0], "delta", None)
    text = getattr(delta, "content", None) if delta is not None else None
    return text if isinstance(text, str) else None


def _view_finish(view: Any) -> Any:
    choices = getattr(view, "choices", None) or []
    return getattr(choices[0], "finish_reason", None) if choices else None


class Wire:
    """Chat Completions: the transport's own shape, so every method is the identity."""

    name = "chat"
    #: Whether a cut answer on this wire can be continued with a trailing
    #: assistant turn (``core.stitch``).
    stitches = True

    def create(self, client: Any, kwargs: Dict[str, Any]) -> Any:
        return client.chat.completions.create(**kwargs)

    def open(self, client: Any, kwargs: Dict[str, Any], call: Any = None) -> Iterable[Any]:
        return client.chat.completions.create(**dict(kwargs, stream=True))

    def resumable(self, kwargs: Any) -> bool:
        return self.stitches and stitch.resumable(kwargs) is not None

    def emit(self, views: Iterable[Any]) -> Iterator[Any]:
        return iter(views)

    def result(self, value: Any) -> Any:
        return value


CHAT = Wire()


# --- Anthropic Messages -------------------------------------------------------

_MESSAGES_TOOL_BLOCKS = ("tool_use", "server_tool_use", "mcp_tool_use")
#: Events the SDK derives from raw ones (``MessageStream``). Hermes reads only the
#: raw types; these are passed on for the first attempt and dropped for a
#: continuation, whose snapshots describe a different message.
_MESSAGES_DERIVED = ("text", "thinking", "signature", "input_json", "citation")


class MessagesWire(Wire):
    """The Anthropic Messages API: ``messages.create`` and ``messages.stream``."""

    name = "messages"
    stitches = True

    def create(self, client: Any, kwargs: Dict[str, Any]) -> Any:
        message = client.messages.create(**kwargs)
        return self._result_view(message)

    @staticmethod
    def _result_view(message: Any) -> Any:
        texts: List[str] = []
        tools: List[Any] = []
        for block in _field(message, "content", None) or []:
            kind = _field(block, "type")
            if kind == "text":
                texts.append(str(_field(block, "text", "") or ""))
            elif kind in _MESSAGES_TOOL_BLOCKS:
                tools.append(SimpleNamespace(function=SimpleNamespace(name=_field(block, "name"))))
        stop = _field(message, "stop_reason")
        msg = SimpleNamespace(role="assistant", content="".join(texts), tool_calls=tools or None)
        choice = SimpleNamespace(index=0, message=msg,
                                 finish_reason="tool_calls" if stop == "tool_use" else "stop")
        view = SimpleNamespace(id=_field(message, "id", ""), model=_field(message, "model", ""),
                               choices=[choice], usage=None)
        setattr(view, NATIVE, message)
        return view

    def result(self, value: Any) -> Any:
        return getattr(value, NATIVE, value)

    def open(self, client: Any, kwargs: Dict[str, Any], call: Any = None) -> Iterator[Any]:
        attempt = next(_ATTEMPTS)
        kwargs = {k: v for k, v in kwargs.items() if k != "stream"}
        manager = client.messages.stream(**kwargs)
        stream = manager.__enter__()
        try:
            # Hermes' own repair for providers that send ``usage: null`` (MiniMax), which
            # would otherwise kill the SDK's accumulator mid-stream (#60683).
            from agent.anthropic_adapter import normalize_stream_usage

            stream = normalize_stream_usage(stream)
        except Exception:
            logger.debug("kame: Messages usage normaliser unavailable", exc_info=True)
        if call is not None:
            call.attach_response(getattr(stream, "response", None))
        tool_blocks: set = set()
        try:
            for event in stream:
                kind = _field(event, "type")
                if kind == "content_block_delta":
                    delta = _field(event, "delta")
                    delta_kind = _field(delta, "type")
                    if delta_kind == "text_delta":
                        text = str(_field(delta, "text", "") or "")
                        yield _view(event, attempt, text=text) if text else _view(event, attempt)
                        continue
                    if delta_kind == "input_json_delta" or _field(event, "index") in tool_blocks:
                        yield _view(event, attempt, tools=_tool())
                        continue
                    yield _view(event, attempt)
                    continue
                if kind == "content_block_start":
                    block = _field(event, "content_block")
                    if _field(block, "type") in _MESSAGES_TOOL_BLOCKS:
                        tool_blocks.add(_field(event, "index"))
                        yield _view(event, attempt, tools=_tool(_field(block, "name")))
                        continue
                    yield _view(event, attempt)
                    continue
                if kind == "content_block_stop" and _field(event, "index") in tool_blocks:
                    yield _view(event, attempt, tools=_tool())
                    continue
                if kind == "input_json":
                    yield _view(event, attempt, tools=_tool())
                    continue
                if kind == "message_delta":
                    stop = _field(_field(event, "delta"), "stop_reason")
                    if stop:
                        yield _view(event, attempt, finish="tool_calls" if stop == "tool_use" else "stop")
                        continue
                yield _view(event, attempt)
        finally:
            try:
                manager.__exit__(None, None, None)
            except Exception:  # pragma: no cover - closing a finished stream
                logger.debug("kame: could not close a Messages stream", exc_info=True)

    def emit(self, views: Iterable[Any]) -> Iterator[Any]:
        return _MessagesEmitter().run(views)


class _MessagesEmitter:
    """Turns the transport's output back into one coherent Messages event stream.

    One message for the whole call, however many attempts it took: a
    continuation's ``message_start`` is dropped, its first text block is
    written into the text block that was cut, and its other blocks are moved
    past every index already used.
    """

    def __init__(self) -> None:
        self.snapshot: Any = None
        self.first_attempt: Optional[int] = None
        self.attempt: Optional[int] = None
        self.open_text: Optional[int] = None
        self.next_index = 0
        self.remap: Dict[int, int] = {}
        self.started = False

    def _accumulate(self, event: Any) -> None:
        try:
            from anthropic.lib.streaming._messages import accumulate_event

            if _field(event, "type") in ("message_start", "content_block_start", "content_block_delta",
                                         "content_block_stop", "message_delta"):
                self.snapshot = accumulate_event(event=event, current_snapshot=self.snapshot)
        except Exception:
            logger.debug("kame: could not fold a Messages event into the final message", exc_info=True)

    def _index(self, native_index: Any) -> Any:
        if not isinstance(native_index, int):
            return native_index
        if native_index not in self.remap:
            self.remap[native_index] = self.next_index
            self.next_index += 1
        return self.remap[native_index]

    def _reindexed(self, event: Any) -> Any:
        index = _field(event, "index")
        mapped = self._index(index)
        if mapped == index:
            return event
        clone = copy.copy(event)
        try:
            setattr(clone, "index", mapped)
        except Exception:
            object.__setattr__(clone, "index", mapped)
        return clone

    def _text_event(self, text: str) -> Any:
        from anthropic.types import RawContentBlockDeltaEvent, TextDelta

        index = self.open_text if self.open_text is not None else 0
        return RawContentBlockDeltaEvent(type="content_block_delta", index=index,
                                         delta=TextDelta(type="text_delta", text=text))

    def run(self, views: Iterable[Any]) -> Iterator[Any]:
        for view in views:
            native = getattr(view, NATIVE, None)
            if native is None:
                text = _view_text(view)
                if text:
                    event = self._text_event(text)
                    self._accumulate(event)
                    yield event
                elif not getattr(view, "choices", None):
                    yield SimpleNamespace(type="ping")
                continue
            attempt = getattr(view, ATTEMPT, None)
            if attempt != self.attempt:
                self.attempt = attempt
                if self.first_attempt is None:
                    self.first_attempt = attempt
                else:
                    # A continuation: keep the cut text block, start fresh numbering after it.
                    self.remap = {}
            continuation = attempt != self.first_attempt
            kind = _field(native, "type")
            if continuation and (kind == "message_start" or kind in _MESSAGES_DERIVED):
                continue
            if continuation and kind == "content_block_start" and self.open_text is not None and \
                    _field(_field(native, "content_block"), "type") == "text" and \
                    _field(native, "index") not in self.remap:
                self.remap[_field(native, "index")] = self.open_text
                continue
            if kind in ("content_block_start", "content_block_delta", "content_block_stop"):
                if kind == "content_block_start" and _field(_field(native, "content_block"), "type") == "text":
                    event = self._reindexed(native)
                    self.open_text = _field(event, "index")
                else:
                    event = self._reindexed(native)
            else:
                event = native
            text = _view_text(view)
            if kind == "content_block_delta" and text is not None:
                original = _field(_field(native, "delta"), "text")
                if text != original:
                    if not text:
                        continue
                    event = self._text_event(text)
            self._accumulate(event)
            yield event

    def final_message(self) -> Any:
        return self.snapshot


# --- OpenAI Responses -------------------------------------------------------

_RESPONSES_TOOL_EVENTS = ("response.function_call_arguments.delta", "response.function_call_arguments.done",
                          "response.custom_tool_call_input.delta", "response.custom_tool_call_input.done")


class ResponsesWire(Wire):
    """The OpenAI Responses API: ``responses.create`` (streamed or not).

    A cut answer is not continued on this wire — its request carries ``input``
    items, not a message list, and 1.8.1.8 did not continue it either. A cut
    goes back to Hermes, whose own Responses retry asks again; the key that
    dropped is rested so that retry lands on another one.
    """

    name = "responses"
    stitches = False

    def create(self, client: Any, kwargs: Dict[str, Any]) -> Any:
        response = client.responses.create(**{k: v for k, v in kwargs.items() if k != "stream"})
        return self._result_view(response)

    @staticmethod
    def _result_view(response: Any) -> Any:
        texts: List[str] = []
        tools: List[Any] = []
        for item in _field(response, "output", None) or []:
            kind = str(_field(item, "type") or "")
            if kind == "message":
                for part in _field(item, "content", None) or []:
                    if _field(part, "type") == "output_text":
                        texts.append(str(_field(part, "text", "") or ""))
            elif "call" in kind:
                tools.append(SimpleNamespace(function=SimpleNamespace(name=_field(item, "name"))))
        msg = SimpleNamespace(role="assistant", content="".join(texts), tool_calls=tools or None)
        choice = SimpleNamespace(index=0, message=msg, finish_reason="tool_calls" if tools else "stop")
        view = SimpleNamespace(id=_field(response, "id", ""), model=_field(response, "model", ""),
                               choices=[choice], usage=None)
        setattr(view, NATIVE, response)
        return view

    def result(self, value: Any) -> Any:
        return getattr(value, NATIVE, value)

    def open(self, client: Any, kwargs: Dict[str, Any], call: Any = None) -> Iterator[Any]:
        attempt = next(_ATTEMPTS)
        stream = client.responses.create(**dict(kwargs, stream=True))
        if call is not None:
            call.attach_response(getattr(stream, "response", None))
        try:
            for event in stream:
                kind = str(_field(event, "type") or "")
                if kind == "response.output_text.delta":
                    text = str(_field(event, "delta", "") or "")
                    yield _view(event, attempt, text=text) if text else _view(event, attempt)
                    continue
                if kind in _RESPONSES_TOOL_EVENTS:
                    yield _view(event, attempt, tools=_tool())
                    continue
                if kind in ("response.output_item.added", "response.output_item.done"):
                    item = _field(event, "item")
                    if "call" in str(_field(item, "type") or ""):
                        yield _view(event, attempt, tools=_tool(_field(item, "name")))
                        continue
                if kind == "response.completed":
                    output = _field(_field(event, "response"), "output", None) or []
                    called = any("call" in str(_field(item, "type") or "") for item in output)
                    yield _view(event, attempt, finish="tool_calls" if called else "stop")
                    continue
                if kind == "response.incomplete":
                    yield _view(event, attempt, finish="length")
                    continue
                yield _view(event, attempt)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # pragma: no cover - closing a finished stream
                    logger.debug("kame: could not close a Responses stream", exc_info=True)

    def emit(self, views: Iterable[Any]) -> Iterator[Any]:
        for view in views:
            native = getattr(view, NATIVE, None)
            if native is not None:
                text = _view_text(view)
                original = _field(native, "delta") if _field(native, "type") == "response.output_text.delta" else None
                if text is not None and original is not None and text != original:
                    if not text:
                        continue
                    clone = copy.copy(native)
                    try:
                        setattr(clone, "delta", text)
                    except Exception:
                        object.__setattr__(clone, "delta", text)
                    yield clone
                    continue
                yield native
                continue
            text = _view_text(view)
            if text:
                # Words released by the transport itself; this wire never stitches, so
                # there is no item of KAME's own to attach them to.
                logger.debug("kame: dropped %d released character(s) on the Responses wire", len(text))
                continue
            if not getattr(view, "choices", None):
                yield SimpleNamespace(type="kame.keepalive", sequence_number=None, created_at=time.time())


MESSAGES = MessagesWire()
RESPONSES = ResponsesWire()
