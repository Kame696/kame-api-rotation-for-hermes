"""1.8.4.3 (PLAN section 6, components L1-L9) - thoughts stay LIVE, the limit follows the model, waste is shown.

Section 6 replaces the "holding" of H2/H3: the provisional leg's thoughts go to Hermes as they arrive, a
rescue leg's chunks are buffered until it wins (a take-over), the Messages emitter restarts when an attempt
showed nothing, the Responses wire lets only reasoning deltas out early, the quiet bound follows the model,
rescue 2 does not wait for a silent rescue 1, a leg that worked and lost teaches its real first word, and
the waste is counted and told.

Real time and real threads, short bounds (``KAME_THOUGHT_QUIET_S=0.2``), nothing sleeps more than ~2 s. The
fakes are those of ``test_1_8_2_5_stream_race`` and ``test_1_8_4_3_quiet_guard``.
"""
from __future__ import annotations

import importlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_4_3_quiet_guard import (  # noqa: F401  (fixture by name)
    EVENTS, SeqCall, answered, guard, kinds, reasons, think, timings_mod, transport, settings, chunk,
    IDENTITY, KEYS)

wires = importlib.import_module(f"{R.PACKAGE}.wires")
facade = importlib.import_module(f"{R.PACKAGE}.facade")
HERE = importlib.import_module("pathlib").Path(__file__).resolve().parents[1]


def no_k(out):
    return [k for k in kinds(out) if k != "K"]


class GatedStream(R.FakeStream):
    """Yields ``first`` at once, then waits for ``gate`` (at most 3 s) before the rest."""

    def __init__(self, first, gate, rest):
        super().__init__(rest)
        self.first = first
        self.gate = gate

    def __iter__(self):
        yield self.first
        end = time.monotonic() + 3.0
        while not self.gate.is_set() and time.monotonic() < end:
            if self.response.closed.is_set():
                raise ConnectionError("response closed")
            time.sleep(0.005)
        yield from super().__iter__()


class StreamCall(SeqCall):
    """Every stream the transport opens is the next of the ready-made ones."""

    def __init__(self, streams):
        super().__init__([])
        self.streams = list(streams)

    def client_for(self, key, attempt):
        def create(**request):
            with self.lock:
                stream = self.streams.pop(0) if self.streams else R.FakeStream([chunk("extra"), chunk(finish="stop")])
                self.opened.append((request.get("model"), key, stream))
            return stream
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


# 1 - L1 ------------------------------------------------------------------------------------------------

def test_a_watched_replys_thoughts_go_out_while_it_is_still_thinking(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "30")      # guard on, no rescue
    settings.forget()
    gate = threading.Event()
    sources = [think("a"), think("b"), chunk("ans"), chunk("wer", finish="stop")]
    call = StreamCall([GatedStream(sources[0], gate, [0.01] + sources[1:])])
    out = []
    started = time.monotonic()
    for c in guard.stream(call):
        out.append(c)
        if len(out) == 1:
            # the leg has not produced its second chunk yet (it waits for this very gate): a held thought
            # would have blocked here for the whole 3 s
            assert time.monotonic() - started < 1.5
            assert out[0] is sources[0]
            assert not gate.is_set()
            gate.set()
    assert no_k(out) == ["T:a", "T:b", "X:ans", "X:wer"], "the whole yielded sequence equals the source"


def test_guard_on_without_a_stall_is_the_1842_stream(guard, monkeypatch):
    steps = [0.02, think("a"), 0.05, think("b"), 0.05, chunk("ans"), chunk("wer", finish="stop")]
    on = no_k(list(guard.stream(SeqCall([list(steps)]))))
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0")
    settings.forget()
    off = no_k(list(guard.stream(SeqCall([list(steps)]))))
    assert on == off == ["T:a", "T:b", "X:ans", "X:wer"]


# 2 - L1 take-over, chat wire ---------------------------------------------------------------------------

def test_a_take_over_shows_the_quiet_legs_thoughts_then_the_rescues_buffer_in_order(guard):
    call = SeqCall([[0.02, think("first thought"), 30.0, chunk("late", finish="stop")],
                    [0.05, think("rescue thought"), chunk("rescued "), chunk("text", finish="stop")]])
    out = list(guard.stream(call))
    assert no_k(out) == ["T:first thought", "T:rescue thought", "X:rescued ", "X:text"]
    assert "late" not in R.shown(out), "nothing of the provisional after the take-over"
    assert getattr(call, "_kame_rescues_asked", None) == 1
    row = answered(guard)[-1]
    assert row["rescued"] is True and row["rescues_asked"] == 1
    sign = row["first_sign_at"] - row["started_at"]
    assert 0.0 < sign <= row["ended_at"] - row["started_at"], "inside the reply"
    # The quiet leg's live thought, not the rescue's own first chunk: the rescue is only asked one bound (0.2 s)
    # after that thought, so the text it brings is at least that far behind the first sign.
    assert row["first_text_at"] - row["first_sign_at"] >= 0.2, \
        "the first sign is when the user first saw work: the quiet leg's live thought"
    speed = guard.speed_view()["hour"]
    assert speed["rescue"] == 1 and speed["rescue_won"] == 1


def test_the_provisional_resuming_first_counts_the_rescue_but_not_a_win(guard):
    call = SeqCall([[0.02, think("a"), 0.4, chunk("primary", finish="stop")], [2.0, chunk("late")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "primary"
    assert getattr(call, "_kame_rescues_asked", None) == 1
    row = answered(guard)[-1]
    assert not row.get("rescued") and row["rescues_asked"] == 1
    speed = guard.speed_view()["hour"]
    assert speed["rescue"] == 1 and speed["rescue_won"] == 0
    assert row["first_sign_at"] - row["started_at"] < 0.2, "the provisional's own first sign"


def test_no_stall_asks_no_rescue(guard):
    call = SeqCall([[0.02, think("a"), 0.05, chunk("answer", finish="stop")]])
    list(guard.stream(call))
    assert getattr(call, "_kame_rescues_asked", None) == 0
    assert "rescues_asked" not in answered(guard)[-1] or not answered(guard)[-1]["rescues_asked"]


# 3, 4 - L3 Messages wire ---------------------------------------------------------------------------------

@pytest.fixture()
def sdk(monkeypatch):
    """The Anthropic SDK types. The emitter calls the SDK's own ``accumulate_event`` unpatched: an SDK that
    wants ``json_bufs`` (newer than the 0.87.0 Hermes pins) gets the emitter's own dict."""
    pytest.importorskip("anthropic")
    return importlib.import_module("anthropic.types")


def test_the_emitter_gives_a_newer_sdk_its_json_bufs(sdk, monkeypatch):
    """An SDK whose ``accumulate_event`` requires ``json_bufs`` is handed one dict per message; an older one
    is called as before. Without this the final message is None on a newer SDK."""
    mod = importlib.import_module("anthropic.lib.streaming._messages")
    seen = []

    def newer(*, event, current_snapshot, json_bufs, output_format=None):
        seen.append(json_bufs)
        return SimpleNamespace(content=[], id="m")

    def older(*, event, current_snapshot, output_format=None):
        seen.append("no bufs")
        return SimpleNamespace(content=[], id="m")

    ev = Ev(sdk)
    for fake, expected in ((newer, dict), (older, str)):
        seen.clear()
        monkeypatch.setattr(mod, "accumulate_event", fake)
        emitter = wires._MessagesEmitter()
        list(emitter.run([view_of(ev.start("m1"), 1), view_of(ev.thinking_start(), 1)]))
        assert len(seen) == 2 and all(isinstance(s, expected) for s in seen)
        if expected is dict:
            assert seen[0] is seen[1] is emitter.json_bufs, "one dict for the whole message"


def test_a_restart_starts_a_new_json_bufs(sdk):
    emitter = wires._MessagesEmitter()
    ev = Ev(sdk)
    list(emitter.run([view_of(ev.start("m1"), 1), view_of(ev.thinking_start(), 1)]))
    first = emitter.json_bufs
    first[7] = b"left over"
    list(emitter.run([view_of(ev.start("m2"), 2)]))
    assert emitter.json_bufs == {} and emitter.json_bufs is not first


class Ev:
    """Typed Messages events (the SDK's own classes), so the accumulator really folds them."""

    def __init__(self, t):
        self.t = t

    def start(self, mid, input_tokens=3):
        t = self.t
        return t.RawMessageStartEvent(type="message_start", message=t.Message(
            id=mid, type="message", role="assistant", model="claude", content=[], stop_reason=None,
            stop_sequence=None, usage=t.Usage(input_tokens=input_tokens, output_tokens=0)))

    def thinking_start(self, index=0):
        t = self.t
        return t.RawContentBlockStartEvent(type="content_block_start", index=index, content_block=t.ThinkingBlock(
            type="thinking", thinking="", signature=""))

    def thinking(self, text, index=0):
        t = self.t
        return t.RawContentBlockDeltaEvent(type="content_block_delta", index=index, delta=t.ThinkingDelta(
            type="thinking_delta", thinking=text))

    def signature(self, sig, index=0):
        t = self.t
        return t.RawContentBlockDeltaEvent(type="content_block_delta", index=index, delta=t.SignatureDelta(
            type="signature_delta", signature=sig))

    def text_start(self, index=1):
        t = self.t
        return t.RawContentBlockStartEvent(type="content_block_start", index=index, content_block=t.TextBlock(
            type="text", text=""))

    def text(self, text, index=1):
        t = self.t
        return t.RawContentBlockDeltaEvent(type="content_block_delta", index=index, delta=t.TextDelta(
            type="text_delta", text=text))

    def tool_start(self, index=1):
        t = self.t
        return t.RawContentBlockStartEvent(type="content_block_start", index=index, content_block=t.ToolUseBlock(
            type="tool_use", id="tool_1", name="f", input={}))

    def stop(self, index):
        return self.t.RawContentBlockStopEvent(type="content_block_stop", index=index)

    def end(self, output_tokens):
        t = self.t
        from anthropic.types.raw_message_delta_event import Delta

        return [t.RawMessageDeltaEvent(type="message_delta", delta=Delta(stop_reason="end_turn", stop_sequence=None),
                                       usage=t.MessageDeltaUsage(output_tokens=output_tokens)),
                t.RawMessageStopEvent(type="message_stop")]


def view_of(event, attempt):
    """What ``MessagesWire.open`` hands the transport for one native event (the shapes the emitter reads)."""
    kind = wires._field(event, "type")
    if kind == "content_block_delta" and wires._field(wires._field(event, "delta"), "type") == "text_delta":
        return wires._view(event, attempt, text=wires._field(wires._field(event, "delta"), "text"))
    if kind == "content_block_start" and wires._field(wires._field(event, "content_block"), "type") == "tool_use":
        return wires._view(event, attempt, tools=wires._tool("f"))
    return wires._view(event, attempt)


def run_emitter(*attempts):
    """``attempts`` is a list of event lists; each is one attempt's views, in order."""
    emitter = wires._MessagesEmitter()
    out = []
    for n, events in enumerate(attempts, start=1):
        out += list(emitter.run(view_of(e, n) for e in events))
    return emitter, out


class TestMessagesRestartAndContinuation:
    def test_an_attempt_after_thinking_only_is_a_restart_with_its_own_message(self, sdk):
        e = Ev(sdk)
        first = [e.start("m1"), e.thinking_start(0), e.thinking("hm-one", 0)]
        second = [e.start("m2", input_tokens=5), e.text_start(0), e.text("hi", 0), e.stop(0)] + e.end(7)
        emitter, out = run_emitter(first, second)
        final = emitter.final_message()
        assert final.id == "m2" and final.usage.input_tokens == 5 and final.usage.output_tokens == 7
        assert [b.type for b in final.content] == ["text"] and final.content[0].text == "hi"
        assert "hm-one" not in repr(final), "no block of the earlier attempt"
        starts = [x for x in out if wires._field(x, "type") == "message_start"]
        assert [s.message.id for s in starts] == ["m1", "m2"], "Hermes sees the second message_start"
        text_start = [x for x in out if wires._field(x, "type") == "content_block_start"][-1]
        assert text_start.index == 0, "its indices are its own"

    def test_an_attempt_after_text_is_a_continuation_exactly_as_before(self, sdk):
        e = Ev(sdk)
        first = [e.start("m1"), e.text_start(0), e.text("abc", 0)]
        second = [e.start("m2"), e.text_start(0), e.text("def", 0), e.stop(0)] + e.end(4)
        emitter, out = run_emitter(first, second)
        final = emitter.final_message()
        assert final.id == "m1", "one message for the whole call"
        assert [b.type for b in final.content] == ["text"] and final.content[0].text == "abcdef"
        assert [x for x in out if wires._field(x, "type") == "message_start"].__len__() == 1

    def test_an_attempt_after_a_tool_event_is_a_continuation_too(self, sdk):
        e = Ev(sdk)
        first = [e.start("m1"), e.tool_start(0)]
        emitter, _ = run_emitter(first)
        assert emitter.visible is True
        second = [e.start("m2")]
        emitter2 = emitter
        list(emitter2.run(view_of(x, 2) for x in second))
        assert emitter2.final_message().id == "m1", "visible: the second attempt's message_start is dropped"

    def test_thinking_alone_is_not_visible(self, sdk):
        e = Ev(sdk)
        emitter, _ = run_emitter([e.start("m1"), e.thinking_start(0), e.thinking("x", 0), e.signature("s", 0)])
        assert emitter.visible is False


class TestTheMessagesTakeOver:
    def test_the_stored_message_is_the_rescues_alone(self, guard, sdk, monkeypatch):
        e = Ev(sdk)
        quiet = [0.02, e.start("m-quiet"), e.thinking_start(0), e.thinking("hm-quiet", 0),
                 e.signature("sig-quiet", 0), 30.0]
        rescue = ([0.05, e.start("m-rescue", input_tokens=5), e.thinking_start(0), e.thinking("hm-rescue", 0),
                   e.signature("sig-rescue", 0), e.stop(0), e.text_start(1), e.text("answer", 1), e.stop(1)]
                  + e.end(9))
        plans = [quiet, rescue]
        opened = []

        class Manager:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self.stream

            def __exit__(self, *exc):
                self.stream.close()

        class MessagesCall(R.FakeCall):
            wire = wires.MESSAGES
            response = None

            def attach_response(self, response):
                self.response = response

            def client_for(self, key, attempt):
                def stream(**request):
                    s = R.FakeStream(list(plans.pop(0)))
                    opened.append(s)
                    return Manager(s)
                return SimpleNamespace(messages=SimpleNamespace(stream=stream))

        monkeypatch.setattr(facade, "TRANSPORT", guard)
        stream = facade._MessageStream(MessagesCall(R.Pool({})))
        events = list(stream)
        final = stream.get_final_message()
        assert len(opened) == 2 and opened[0].response.closed.is_set(), "the quiet leg was cut"
        assert final.id == "m-rescue" and final.model == "claude"
        assert final.usage.input_tokens == 5 and final.usage.output_tokens == 9
        assert [b.type for b in final.content] == ["thinking", "text"]
        assert final.content[0].thinking == "hm-rescue" and final.content[0].signature == "sig-rescue"
        assert final.content[1].text == "answer"
        assert "quiet" not in repr(final), "no block of the quiet leg"
        types = [wires._field(x, "type") for x in events]
        starts = [x for x in events if wires._field(x, "type") == "message_start"]
        assert [s.message.id for s in starts] == ["m-quiet", "m-rescue"], "the quiet leg's events, then the rescue's whole stream"
        first_rescue = types.index("message_start", types.index("message_start") + 1)
        rescue_starts = [x.index for x in events[first_rescue:] if wires._field(x, "type") == "content_block_start"]
        assert rescue_starts == [0, 1], "the rescue's own indices"
        assert any("hm-quiet" in repr(x) for x in events[:first_rescue]), "the quiet leg's thinking was live"


    def test_a_thinking_only_message_ends_in_its_native_order(self, guard, sdk, monkeypatch):
        """The end of a reply is not sent ahead: ``message_delta`` (the stop reason) waits for the main loop,
        and ``message_stop`` waits behind it. Sent live, the stop would have reached Hermes first."""
        e = Ev(sdk)
        only_thinks = ([0.02, e.start("m-1"), e.thinking_start(0), e.thinking("hm", 0), e.signature("s", 0),
                        e.stop(0)] + e.end(3))
        plans = [list(only_thinks) for _ in range(12)]

        class Manager:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self.stream

            def __exit__(self, *exc):
                self.stream.close()

        class MessagesCall(R.FakeCall):
            wire = wires.MESSAGES
            response = None

            def attach_response(self, response):
                self.response = response

            def client_for(self, key, attempt):
                return SimpleNamespace(messages=SimpleNamespace(
                    stream=lambda **request: Manager(R.FakeStream(list(plans.pop(0))))))

        monkeypatch.setattr(facade, "TRANSPORT", guard)
        types = [wires._field(x, "type") for x in facade._MessageStream(MessagesCall(R.Pool({})))]
        ends = [t for t in types if t in ("message_delta", "message_stop")]
        assert ends and ends[0] == "message_delta", types
        for first, second in zip(ends, ends[1:]):
            assert (first, second) != ("message_stop", "message_stop")
        assert all(ends[i] == ("message_delta" if i % 2 == 0 else "message_stop") for i in range(len(ends))), ends


# 4b - L1: the end of a reply is the main loop's --------------------------------------------------------------

def _thinking_only_run(guard, monkeypatch, quiet_setting, plans):
    """One call on a fresh transport; everything observable of it: what was yielded, the finish chunks, the
    rows, the streams opened, the reply count, the events (seconds blanked) and the backup-won mark."""
    import re

    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", quiet_setting)
    settings.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    EVENTS.clear()
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    before = len(guard.rows)
    call = SeqCall([list(p) for p in plans])
    out = list(t.stream(call))
    finishes = sum(1 for c in out if getattr(c, "choices", None) and c.choices[0].finish_reason)
    rows = [(r.get("outcome"), r.get("kind")) for r in guard.rows[before:]]
    said = sorted(re.sub(r"[0-9.]+ ?s\b", "Ns", str(r)) for r in reasons())
    return (no_k(out), finishes, rows, len(call.opened), t.speed_view()["since_start"]["replies"], said,
            getattr(call, "_kame_backup_won", None))


def test_a_reply_that_only_thinks_and_finishes_is_handled_as_in_1842(guard, monkeypatch):
    """With the watch on, the finish chunk of a reasoning-only reply must still reach the main loop: it is
    what tells a finished empty reply from a stream that never finished (rows, the reply count, how many keys
    are asked). Everything observable is equal to the watch being off."""
    plans = [[0.02, think("a"), 0.02, think("b"), chunk(finish="stop")] for _ in range(20)]
    watched = _thinking_only_run(guard, monkeypatch, "0.2", plans)
    plain = _thinking_only_run(guard, monkeypatch, "0", plans)
    assert watched == plain
    assert watched[1] >= 1, "the finish chunk reached Hermes"


def test_a_backup_key_that_only_thinks_and_finishes_is_accounted_as_in_1842(guard, monkeypatch):
    """The same through a backup leg: a watched winner's buffer no longer holds its thoughts, so "did the
    winner work" is asked of the race, not of the buffer - the backup-won mark, its event and the learned
    first word are those of the watch being off."""
    plans = [[5.0, chunk("late primary")], [0.02, think("a"), chunk(finish="stop")]]
    watched = _thinking_only_run(guard, monkeypatch, "0.2", plans)
    plain = _thinking_only_run(guard, monkeypatch, "0", plans)
    assert watched == plain
    assert any("backup key answered first" in s for s in watched[5])


def test_a_raced_in_model_that_answers_is_not_noted_busy_by_its_own_rescue(guard, monkeypatch):
    """The home model is silent; the next model races in, thinks, goes quiet, a rescue is asked beside it
    and stays silent, then the raced-in model answers. A rescue leg that lost without a word says nothing
    about its model: neither the model that just answered nor the next one is noted busy."""
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    monkeypatch.setattr(transport, "_SLOW_S", {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    call = SeqCall(by_model={
        "gemini-race": [[30.0, chunk("silent home")]],
        "gemini-race-two": [[0.02, think("c"), 1.2, chunk("the raced-in model answers", finish="stop")],
                            [30.0, chunk("silent rescue")]],
        "gemini-race-three": [[30.0, chunk("silent second rescue")]],
    })
    out = list(guard.stream(call))
    assert R.shown(out) == "the raced-in model answers"
    assert call.models().count("gemini-race-two") == 2, "a rescue was asked on another key of the raced-in model"
    assert "gemini-race-three" in call.models(), "and a second one on the next model, silent too"
    noted = set(guard._busy_until)
    assert not any(name.endswith("gemini-race-two") or name.endswith("gemini-race-three") for name in noted), noted
    assert any(name.endswith("gemini-race") for name in noted), "the silent home model is noted, as in 1.8.4.2"


# 5 - L2 Responses wire ----------------------------------------------------------------------------------

class TestTheResponsesWire:
    @staticmethod
    def ev(kind, **kw):
        return dict({"type": kind}, **kw)

    def run(self, guard, plans):
        opened = []
        queue = list(plans)

        class ResponsesCall(R.FakeCall):
            wire = wires.RESPONSES
            response = None

            def attach_response(self, response):
                self.response = response

            def client_for(self, key, attempt):
                def create(**request):
                    s = R.FakeStream(list(queue.pop(0)) if queue else [30.0])
                    opened.append(s)
                    return s
                return SimpleNamespace(responses=SimpleNamespace(create=create))

        started = time.monotonic()
        seen = []
        for event in guard.stream(ResponsesCall(R.Pool({}))):
            kind = wires._field(event, "type")
            if kind == "kame.keepalive":
                continue
            seen.append((time.monotonic() - started, kind, event))
        return seen, opened

    def test_live_predicate(self):
        live = wires.RESPONSES.live
        assert live(wires._view(self.ev("response.reasoning_summary_text.delta", delta="x"), 1))
        assert live(wires._view(self.ev("response.reasoning_text.delta", delta="x"), 1))
        assert not live(wires._view(self.ev("response.reasoning_summary_text.done"), 1))
        assert not live(wires._view(self.ev("response.created"), 1))
        assert not live(wires._view(self.ev("response.output_item.added", item={"type": "reasoning"}), 1))
        assert not live(wires._view(self.ev("response.output_item.done", item={"type": "message"}), 1))
        assert live(SimpleNamespace(choices=[]))      # a transport keep-alive carries no native event
        assert wires.CHAT.live(object()) and wires.MESSAGES.live(object())

    def test_only_reasoning_deltas_go_out_early_and_the_rest_follows_the_win_in_native_order(self, guard):
        ev = self.ev
        # 0.8 s, well after the backup trigger (0.2 s): a leg still ALONE passes its events as an unraced stream does,
        # and CI macOS 3.12 once started the backup after a 0.25 s first chunk.
        plan = [0.8, ev("response.created"), ev("response.output_item.added", item={"type": "reasoning", "id": "r"}),
                ev("response.reasoning_summary_text.delta", delta="t1"), 0.4,
                ev("response.output_text.delta", delta="hi"), ev("response.output_item.done", item={"type": "message"}),
                ev("response.completed", response={"output": []})]
        seen, opened = self.run(guard, [plan, [30.0]])      # the backup (asked at 0.2 s) stays silent
        kinds_ = [k for _, k, _ in seen]
        assert kinds_ == ["response.reasoning_summary_text.delta", "response.created", "response.output_item.added",
                          "response.output_text.delta", "response.output_item.done", "response.completed"]
        assert kinds_.count("response.reasoning_summary_text.delta") == 1, "deltas are not repeated"
        delta_at, created_at = seen[0][0], seen[1][0]
        assert created_at - delta_at > 0.25, "the structural events wait for the first visible event"

    def test_a_take_over_never_hands_hermes_a_buffered_event_of_the_quiet_leg(self, guard):
        ev = self.ev
        quiet = [0.02, ev("response.created"), ev("response.reasoning_summary_text.delta", delta="t1"),
                 ev("response.output_item.added", item={"type": "message", "id": "quiet-item"}), 30.0]
        rescue = [0.05, ev("response.created"), ev("response.output_item.added", item={"type": "message", "id": "rescue-item"}),
                  ev("response.output_text.delta", delta="rescued"),
                  ev("response.output_item.done", item={"type": "message"}),
                  ev("response.completed", response={"output": []})]
        seen, opened = self.run(guard, [quiet, rescue])
        joined = repr([e for _, _, e in seen])
        assert "quiet-item" not in joined and "rescue-item" in joined
        assert [k for _, k, _ in seen if k == "response.reasoning_summary_text.delta"] == [
            "response.reasoning_summary_text.delta"], "the quiet leg's reasoning delta was live"
        assert opened[0].response.closed.is_set()


# 6 - L4 ---------------------------------------------------------------------------------------------------

class TestTheBoundFollowsTheModel:
    @staticmethod
    def bound(samples):
        tracker = R.hedging_mod.HedgeTracker()
        for s in samples:
            tracker.record_quiet(IDENTITY, s)
        return tracker.quiet_bound(IDENTITY, False)

    @pytest.mark.parametrize("samples, expected", [
        ([], 20.0),
        ([50.0], 20.0),
        ([40.0, 40.0], 50.0),
        ([2.0] * 30, 8.0),
        ([4.0] * 25 + [12.0] * 5, 20.0),
        ([2.0] * 30 + [124.0], 8.0),
        ([2.0] * 20 + [40.0] * 10, 50.0),
        ([100.0] * 20, 90.0),
    ])
    def test_the_table(self, samples, expected):
        assert self.bound(samples) == pytest.approx(expected)

    def test_constants(self):
        h = R.hedging_mod
        assert (h._QUIET_COLD_S, h._QUIET_MIN_S, h._QUIET_MAX_S) == (20.0, 8.0, 20.0)
        assert (h._QUIET_CEILING_S, h._QUIET_FOLLOW, h._QUIET_MIN_SAMPLES) == (90.0, 1.25, 10)

    def test_the_setting_still_overrides_and_zero_turns_the_guard_off(self, guard, monkeypatch):
        monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "7")
        settings.forget()
        assert guard._quiet_bound(IDENTITY, False) == 7.0
        monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0")
        settings.forget()
        assert guard._quiet_bound(IDENTITY, False) is None


# 7 - L5 ---------------------------------------------------------------------------------------------------

def test_l5_a_silent_rescue_one_does_not_make_rescue_two_wait_twice_the_bound(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0.6")
    settings.forget()
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)       # no first-word statistics: this is `late`
    call = SeqCall(by_model={
        "gemini-race": [[0.02, think("a"), 30.0], [30.0, chunk("silent rescue")]],
        "gemini-race-two": [[0.02, chunk("next model", finish="stop")]],
    })
    started = time.monotonic()
    out = list(guard.stream(call))
    took = time.monotonic() - started
    assert R.shown(out) == "next model"
    assert call.models() == ["gemini-race", "gemini-race", "gemini-race-two"]
    assert took < 1.3, f"rescue 2 at rescue 1's late point ({took:.2f}s), not at twice the bound after it (1.8 s)"


def test_l5_a_thinking_rescue_one_keeps_every_other_leg_away(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0.4")
    settings.forget()
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.1)
    thoughts = []
    for n in range(9):
        thoughts += [think(f"r{n}"), 0.1]
    call = SeqCall(by_model={
        "gemini-race": [[0.02, think("a"), 30.0], [0.02] + thoughts + [chunk("rescue text", finish="stop")]],
        "gemini-race-two": [[0.02, chunk("never asked", finish="stop")]],
    })
    out = list(guard.stream(call))
    assert R.shown(out) == "rescue text"
    assert call.models() == ["gemini-race", "gemini-race"], "no second rescue while the first one thinks"


# 8 - L6 ---------------------------------------------------------------------------------------------------

def _spy_latency(monkeypatch):
    calls = []
    tracker = R.hedging_mod.HEDGE_TRACKER
    real = tracker.record_latency

    def spy(identity, seconds, **kw):
        calls.append({"identity": identity, "seconds": seconds, "censored": bool(kw.get("censored"))})
        return real(identity, seconds, **kw)

    monkeypatch.setattr(tracker, "record_latency", spy)
    return calls


def test_l6_a_leg_that_worked_and_lost_teaches_its_real_first_word(guard, monkeypatch):
    calls = _spy_latency(monkeypatch)
    call = SeqCall([[0.05, think("first"), 30.0, chunk("late")],
                    [0.05, chunk("rescued", finish="stop")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "rescued"
    assert not [c for c in calls if c["censored"]], "the stalled leg is not taught as a censored 'first word'"
    assert len(calls) == 2
    quiet_leg = min(calls, key=lambda c: c["seconds"])
    assert 0.03 <= quiet_leg["seconds"] < 0.5, "its real first word, not the 0.3 s+ it sat there"


def test_l6_a_silent_leg_that_lost_is_still_censored(guard, monkeypatch):
    calls = _spy_latency(monkeypatch)
    call = SeqCall([[30.0, chunk("silent")], [0.02, chunk("backup", finish="stop")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "backup"
    assert any(c["censored"] for c in calls), "a leg that never worked keeps the censored sample"


# 9 - L7 ---------------------------------------------------------------------------------------------------

def test_l7_the_speed_view_counts_rescues_won_and_keeps_them_out_of_extra(guard):
    for _ in range(10):
        guard._tally(transport._SPEED_REPLIES)
    guard._tally(transport._SPEED_RESCUE)
    guard._tally(transport._SPEED_RESCUE_WON)
    view = guard.speed_view()
    for part in ("hour", "since_start"):
        assert view[part]["rescue"] == 1 and view[part]["rescue_won"] == 1
        assert view[part]["extra_pct"] == 10.0, "the win is a readout of the request, not a second request"
    assert transport._SPEED_RESCUE_WON == 6 and transport._SPEED_COLUMNS == 7


def test_l7_rescues_asked_is_in_the_row_only_above_zero(guard, monkeypatch, tmp_path):
    path = tmp_path / "calls.jsonl"
    monkeypatch.setattr(timings_mod, "_destination", lambda: str(path))
    monkeypatch.setattr(timings_mod, "_silenced", False)
    timings_mod.record(identity="p:m", outcome="answered", started_at=1.0, ended_at=2.0, rescues_asked=2)
    timings_mod.record(identity="p:m", outcome="answered", started_at=1.0, ended_at=2.0)
    timings_mod.record(identity="p:m", outcome="answered", started_at=1.0, ended_at=2.0, rescues_asked=0)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["rescues_asked"] == 2
    assert "rescues_asked" not in rows[1] and "rescues_asked" not in rows[2]


def test_l7_the_panel_says_it_and_reads_the_fields():
    js = (HERE / "hermes-kame-api-rotation" / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "of them re-asked a reply that stopped mid-thought;" in js
    assert "answered first." in js and "hour.rescue_won ?? 0" in js
    assert "'speed.hour.rescue'," in js and "'speed.hour.rescue_won'," in js
    fields = js[js.index("'speed.cap_pct'"):js.index("'totals.physical_keys'")]
    names = [line.strip().strip(",").strip("'") for line in fields.splitlines() if line.strip().startswith("'")]
    assert names == sorted(names), "the field list stays sorted"


def test_l7_every_panel_fixture_carries_the_new_fields():
    folder = HERE / "tools" / "panel_preview" / "fixtures"
    files = sorted(folder.glob("*.json"))
    assert files
    for path in files:
        speed = json.loads(path.read_text(encoding="utf-8")).get("speed") or {}
        if speed.get("hour"):
            assert "rescue" in speed["hour"] and "rescue_won" in speed["hour"], path.name


# 10 - guard off ------------------------------------------------------------------------------------------

def test_guard_off_hedging_off_and_background_jobs_keep_the_1842_stream(guard, monkeypatch):
    # (the older files assert the timing of each; this one asserts the chunk sequence and that no rescue is asked)
    for env, value in (("KAME_THOUGHT_QUIET_S", "0"), ("KAME_HEDGING_DISABLED", "1")):
        monkeypatch.setenv(env, value)
        settings.forget()
        call = SeqCall([[0.02, think("a"), 0.3, chunk("answer", finish="stop")]])
        out = list(guard.stream(call))
        assert no_k(out) == ["T:a", "X:answer"]
        assert len(call.opened) == 1 and getattr(call, "_kame_rescues_asked", 0) == 0
        monkeypatch.delenv(env)
    call = SeqCall([[0.02, think("a"), 0.6, chunk("answer", finish="stop")], [0.05, chunk("rescue", finish="stop")]])
    call.channel, call.task, call.task_known = "secondary", "title_generation", True
    settings.forget()
    out = list(guard.stream(call))
    assert no_k(out)[:2] == ["T:a", "X:answer"] and len(call.opened) == 1
