"""1.9.0.1 (H4, F1 / F2) - the answer watch reads liveness, and a server-side tool is not a silence.

F1: after the first visible chunk ANY item the provider sends (usage, role, a Messages ``ping``, a Responses
``in_progress``) proves the stream alive and stamps the clock. KAME's own keep-alives never enter a reader.
F2: a tool the provider runs inside the same stream (Messages ``server_tool_use`` / ``mcp_tool_use`` until its
``*_tool_result`` block; Responses ``web_search_call`` and friends until that item's ``output_item.done``) can run
past the limit with no event; cutting it would run the tool again. While one is open the watch does not cut; it
starts counting again when the result arrives.

Driven on ``_WatchedStream`` (the reader of every unraced stream, same clock as the raced one), bound 0.5 s.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.h1901 import chunk, transport

NATIVE = transport.wires.NATIVE
BOUND = 0.5


def usage_chunk():
    return SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=1))


def native(event):
    return SimpleNamespace(choices=[], usage=None, **{NATIVE: event})


def msg_start(block_type):
    return native({"type": "content_block_start", "content_block": {"type": block_type}})


def resp_added(item_type):
    return native({"type": "response.output_item.added", "item": {"type": item_type}})


def resp_done(item_type):
    return native({"type": "response.output_item.done", "item": {"type": item_type}})


class Slow:
    """A provider stream: ``(delay, item)`` steps; a delay is an interruptible sleep. ``closed`` ends it."""

    def __init__(self, steps):
        self.steps = steps
        self.response = R.Response()

    def __iter__(self):
        for delay, item in self.steps:
            end = time.monotonic() + delay
            while time.monotonic() < end:
                if self.response.closed.is_set():
                    raise ConnectionError("closed")
                time.sleep(0.01)
            if item is not None:
                yield item

    def close(self):
        self.response.close()


def read(steps):
    """Read the whole stream through the watch; returns (items, error)."""
    watched = transport._WatchedStream(Slow(steps), BOUND)
    out, err = [], None
    try:
        for item in watched:
            out.append(item)
    except BaseException as exc:  # noqa: BLE001
        err = exc
    finally:
        watched.close()
    return out, err


# F1 --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sign", ["usage", "ping", "role"])
def test_a_stream_that_sends_only_signs_of_life_is_never_cut(sign):
    make = {"usage": usage_chunk, "ping": lambda: native({"type": "ping"}),
            "role": lambda: SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=None, delta=SimpleNamespace(
                role="assistant", content=None, tool_calls=None))], usage=None)}[sign]
    steps = [(0.05, chunk("visible "))] + [(0.3, make()) for _ in range(10)] + [(0.1, chunk("end", finish="stop"))]
    out, err = read(steps)
    assert err is None, err
    assert R.shown(out) == "visible end"


def test_silence_after_the_signs_of_life_is_still_cut():
    steps = [(0.05, chunk("visible ")), (0.3, usage_chunk()), (0.3, usage_chunk()), (5.0, chunk("late"))]
    out, err = read(steps)
    assert isinstance(err, transport._AnswerQuiet), err


# F2 --------------------------------------------------------------------------------------------------------------

def test_a_messages_server_tool_silent_for_three_bounds_is_not_cut_and_its_result_re_arms_the_clock():
    steps = [(0.05, chunk("searching ")), (0.05, msg_start("server_tool_use")), (3 * BOUND, None),
             (0.05, msg_start("web_search_tool_result")), (0.05, chunk("found", finish="stop"))]
    out, err = read(steps)
    assert err is None, err
    assert R.shown(out) == "searching found"


def test_after_the_messages_tool_result_a_silence_past_the_bound_is_cut():
    steps = [(0.05, chunk("a ")), (0.05, msg_start("mcp_tool_use")), (2 * BOUND, None),
             (0.05, msg_start("mcp_tool_result")), (5.0, chunk("never"))]
    out, err = read(steps)
    assert isinstance(err, transport._AnswerQuiet), err


@pytest.mark.parametrize("item", ["web_search_call", "file_search_call", "code_interpreter_call", "computer_call",
                                  "mcp_call", "image_generation_call"])
def test_a_responses_server_tool_silent_for_three_bounds_is_not_cut(item):
    steps = [(0.05, chunk("a ")), (0.05, resp_added(item)), (3 * BOUND, None), (0.05, resp_done(item)),
             (0.05, chunk("b", finish="stop"))]
    out, err = read(steps)
    assert err is None, err
    assert R.shown(out) == "a b"


def test_after_the_responses_item_is_done_a_silence_past_the_bound_is_cut():
    steps = [(0.05, chunk("a ")), (0.05, resp_added("web_search_call")), (2 * BOUND, None),
             (0.05, resp_done("web_search_call")), (5.0, chunk("never"))]
    out, err = read(steps)
    assert isinstance(err, transport._AnswerQuiet), err


def test_an_ordinary_function_call_item_is_not_a_server_tool():
    steps = [(0.05, chunk("a ")), (0.05, resp_added("function_call")), (5.0, chunk("never"))]
    out, err = read(steps)
    assert isinstance(err, transport._AnswerQuiet), err
