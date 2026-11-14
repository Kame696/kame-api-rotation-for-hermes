"""1.8.4.3 (H1-H3) - a reply that stops mid-thought is raced again.

Live 2026-11-12: a backup key "won" the race with a REASONING chunk, every other leg was cut, and that stream
then sat silent for ~109 s. The race ended at the first working chunk and nothing watched the winner after
it. Now the first working chunk only makes a PROVISIONAL winner (the silent legs are cut); its thoughts stay
LIVE (section 6, L1) until it shows something visible (text or a tool call). When it goes quiet past the bound a rescue leg is
asked (another key, then the next model of the owner's list; at most two) and the first VISIBLE chunk wins.

Real time and real threads, short bounds (``KAME_THOUGHT_QUIET_S=0.2``); nothing sleeps more than ~2 s.
Fakes are those of ``test_1_8_2_5_stream_race``; the plans here are handed out in the order streams open.
"""
from __future__ import annotations

import importlib
import os
import threading
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
settings = R.settings
chunk = R.chunk
KEYS = R.KEYS
IDENTITY = R.IDENTITY
EVENTS = importlib.import_module(f"{R.PACKAGE}.core.events").EVENTS
chain_mod = importlib.import_module(f"{R.PACKAGE}.core.fallback_chain")
timings_mod = importlib.import_module(f"{R.PACKAGE}.timings")

BOUND = 0.2
CHAIN = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: gemini
    model: gemini-race-three
"""


def think(text="hmm"):
    """A reasoning chunk (the OpenAI-compatible ``reasoning_content`` field)."""
    delta = SimpleNamespace(role=None, content=None, tool_calls=None, reasoning_content=text)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, delta=delta, finish_reason=None)])


def kinds(out):
    """The visible shape of what was yielded, in order: ``T:<reasoning>``, ``X:<text>``, ``K`` keep-alive."""
    rows = []
    for c in out:
        choices = getattr(c, "choices", None) or []
        if not choices:
            rows.append("K")
            continue
        delta = choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            rows.append("T:" + reasoning)
        elif getattr(delta, "content", None):
            rows.append("X:" + delta.content)
        elif getattr(delta, "tool_calls", None):
            rows.append("C")
        else:
            rows.append("-")
    return rows


class SeqCall(R.FakeCall):
    """Every stream the transport opens takes the next plan, in opening order (or the next of its model)."""

    def __init__(self, seq=None, by_model=None, keys=KEYS):
        super().__init__(R.Pool({}), keys=keys)
        self.seq = list(seq or [])
        self.by_model = {m: list(p) for m, p in (by_model or {}).items()}
        self.opened = []                  # (model, key, stream), in opening order
        self.lock = threading.Lock()

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            with self.lock:
                plans = self.by_model.get(model) if self.by_model else self.seq
                steps = plans.pop(0) if plans else [chunk("extra"), chunk(finish="stop")]
                stream = R.FakeStream(steps)
                self.opened.append((model, key, stream))
            return stream
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def models(self):
        return [m for m, _, _ in self.opened]


@pytest.fixture()
def guard(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_RECORDER_DISABLED", "1")
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", str(BOUND))
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    EVENTS.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (0.2, 0.4))
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "may_start_backup", lambda *a, **k: True)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    rows = []
    original = timings_mod.record
    monkeypatch.setattr(timings_mod, "record", lambda **kw: (rows.append(kw), original(**kw))[1])
    t.rows = rows
    yield t
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


def answered(t):
    return [r for r in t.rows if r.get("outcome") == "answered"]


def reasons():
    return [r["reason"] for r in EVENTS.recent(50)]


# 1 ---------------------------------------------------------------------------------------------------

def test_a_primary_that_thinks_once_and_goes_quiet_is_rescued_by_another_key(guard):
    call = SeqCall([[0.02, think("first thought"), 30.0, chunk("late")],
                    [0.05, chunk("rescued "), chunk("text", finish="stop")]])
    started = time.monotonic()
    out = list(guard.stream(call))
    assert time.monotonic() - started < 3.0
    assert R.shown(out) == "rescued text"
    # 1.8.4.3 (L1, section 6): the quiet leg's thoughts were LIVE (shown as they came), then the rescue's own
    assert [k for k in kinds(out) if k != "K"] == ["T:first thought", "X:rescued ", "X:text"]
    assert len(call.opened) == 2
    assert call.opened[0][2].response.closed.is_set(), "the quiet leg was cut"
    assert any("stopped mid-thought" in r and "another key races it" in r for r in reasons()), reasons()
    assert any("the rescue answered first" in r for r in reasons()), reasons()
    rows = answered(guard)
    assert rows and rows[-1].get("rescued") is True


# 2 ---------------------------------------------------------------------------------------------------

def test_a_model_thinking_with_small_gaps_is_never_raced_and_arrives_in_order(guard):
    steps = [0.02, think("a"), 0.05, think("b"), 0.05, think("c"), 0.05, chunk("ans", finish=None),
             chunk("wer", finish="stop")]
    call = SeqCall([steps])
    out = list(guard.stream(call))
    assert len(call.opened) == 1, "no rescue leg"
    assert kinds(out) == ["T:a", "T:b", "T:c", "X:ans", "X:wer"], "every source chunk, in order"
    rows = answered(guard)
    assert not rows[-1].get("rescued")
    assert rows[-1].get("longest_quiet_s") is not None


# 3 ---------------------------------------------------------------------------------------------------

def test_guard_off_is_1842_reasoning_passes_live(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "0")
    settings.forget()
    call = SeqCall([[0.02, think("a"), 0.5, chunk("answer", finish="stop")]])
    seen = []
    started = time.monotonic()
    for c in guard.stream(call):
        seen.append((time.monotonic() - started, c))
    assert [k for k in kinds([c for _, c in seen])] == ["T:a", "X:answer"]
    # The gap, not the clock of the first chunk: a cold or busy machine delays both, a held chunk closes the gap.
    assert seen[1][0] - seen[0][0] > 0.3, "the reasoning chunk was not held back"
    assert len(call.opened) == 1


def test_hedging_off_never_holds_or_rescues(guard, monkeypatch):
    monkeypatch.setenv("KAME_HEDGING_DISABLED", "1")
    settings.forget()
    call = SeqCall([[0.02, think("a"), 0.6, chunk("answer", finish="stop")]])
    seen = []
    started = time.monotonic()
    for c in guard.stream(call):
        seen.append((time.monotonic() - started, c))
    assert kinds([c for _, c in seen]) == ["T:a", "X:answer"]
    assert seen[1][0] - seen[0][0] > 0.4, "the reasoning chunk went out when it came, 0.6 s before the answer"
    assert len(call.opened) == 1


# 4 ---------------------------------------------------------------------------------------------------

def test_the_provisional_resuming_first_wins_and_the_rescue_is_cut_and_counted(guard):
    call = SeqCall([[0.02, think("a"), 0.4, chunk("primary "), chunk("wins", finish="stop")],
                    [2.0, chunk("late")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "primary wins"
    assert kinds(out)[0] == "T:a", "its own reasoning, held and then released first"
    assert len(call.opened) == 2
    time.sleep(0.1)
    assert call.opened[1][2].response.closed.is_set(), "the rescue was cut"
    logged = {scope for (ident, scope) in R.pacing_mod.PACER._log if ident == IDENTITY}
    assert call.opened[1][1] in logged, "the rescue's request still counts against its key"
    assert not answered(guard)[-1].get("rescued")


# 5 ---------------------------------------------------------------------------------------------------

def test_a_rescue_that_only_thinks_does_not_win(guard):
    call = SeqCall([[0.02, think("a"), 0.42, chunk("primary", finish="stop")],
                    [0.02, think("rescue thought"), 3.0, chunk("late")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "primary"
    assert "T:rescue thought" not in kinds(out)
    assert len(call.opened) == 2


# 6 ---------------------------------------------------------------------------------------------------

def test_both_quiet_opens_the_next_model_at_twice_the_bound_and_never_a_third(guard):
    call = SeqCall(by_model={
        "gemini-race": [[0.02, think("a"), 30.0], [0.02, think("b"), 30.0]],
        "gemini-race-two": [[0.02, think("c"), 0.9, chunk("two answers", finish="stop")]],
        "gemini-race-three": [[0.02, chunk("never asked", finish="stop")]],
    })
    started = time.monotonic()
    out = list(guard.stream(call))
    assert time.monotonic() - started < 3.5
    assert R.shown(out) == "two answers"
    assert call.models() == ["gemini-race", "gemini-race", "gemini-race-two"]
    assert any("(your list) races it" in r and "stopped mid-thought" in r for r in reasons()), reasons()
    assert getattr(call, "_kame_answered_by", None) == "gemini:gemini-race-two"


# 7 ---------------------------------------------------------------------------------------------------

def test_once_a_tool_call_is_out_no_switch_ever(guard):
    first = chunk(tools=R.tool_call("search", '{"q": "par'))
    second = chunk(tools=R.tool_call("", 'is"}'), finish="tool_calls")
    call = SeqCall([[0.02, think("a"), 0.05, first, 0.6, second], [2.0, chunk("late")]])
    out = list(guard.stream(call))
    tools = [c for c in out if getattr(c, "choices", None) and c.choices[0].delta.tool_calls]
    assert len(tools) == 2
    assert len(call.opened) == 1, "the quiet after the tool call is not raced"


# 8 ---------------------------------------------------------------------------------------------------

def test_cancel_while_holding_aborts_every_leg_and_yields_nothing(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "30")
    settings.forget()
    call = SeqCall([[0.02, think("a"), 30.0, chunk("never")]])
    threading.Timer(0.5, lambda: setattr(call, "closed", True)).start()
    out = []
    with pytest.raises(BaseException):
        for c in guard.stream(call):
            out.append(c)
    time.sleep(0.1)
    # 1.8.4.3 (L1): the provisional's thought was already out live; never any text
    assert not [k for k in kinds(out) if k.startswith("X:")], "no text"
    assert call.opened and all(s.response.closed.is_set() for _, _, s in call.opened)


# 9 ---------------------------------------------------------------------------------------------------

class TestTheMessagesWire:
    @staticmethod
    def thinking_events(delay):
        return [delay,
                {"type": "message_start", "message": {"id": "m", "model": "claude", "content": []}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
                {"type": "content_block_stop", "index": 0}]

    @staticmethod
    def text_events(text, delay=0.0, index=1):
        return [delay,
                {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": index},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"}]

    def run(self, guard, plans):
        wires = importlib.import_module(f"{R.PACKAGE}.wires")
        pytest.importorskip("anthropic")
        importlib.import_module("anthropic.lib.streaming._messages")
        importlib.import_module("anthropic.types")
        opened = []
        queue = list(plans)

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
                    s = R.FakeStream(list(queue.pop(0)))
                    opened.append(s)
                    return Manager(s)
                return SimpleNamespace(messages=SimpleNamespace(stream=stream))

        out = list(guard.stream(MessagesCall(R.Pool({}))))
        return out, opened

    @staticmethod
    def shape(out):
        rows = []
        for e in out:
            if not isinstance(e, dict):
                continue
            delta = e.get("delta") or {}
            rows.append(e.get("type") + ("/" + delta["type"] if isinstance(delta, dict) and "type" in delta else ""))
        return rows

    def test_the_winners_thinking_and_signature_are_held_and_flushed_in_order(self, guard):
        plans = [self.thinking_events(0.02) + self.text_events("hello", 0.05)]
        out, opened = self.run(guard, plans)
        shape = self.shape(out)
        assert len(opened) == 1
        order = [s for s in shape if s in ("content_block_delta/thinking_delta", "content_block_delta/signature_delta",
                                           "content_block_delta/text_delta")]
        assert order == ["content_block_delta/thinking_delta", "content_block_delta/signature_delta",
                         "content_block_delta/text_delta"]

    def test_a_quiet_leg_thinking_is_live_then_the_rescue_text_follows(self, guard):
        # 1.8.4.3 (L1/L3, section 6): was "a quiet loser's thinking is never yielded" (holding). Now the
        # provisional's thinking goes out live; the stored message is the rescue's (tests/test_1_8_4_3_live).
        quiet = self.thinking_events(0.02) + [30.0]
        start = {"type": "message_start", "message": {"id": "m2", "model": "claude", "content": []}}
        plans = [quiet, [0.05, start] + self.text_events("rescued", index=0)[1:]]
        out, opened = self.run(guard, plans)
        joined = repr(out)
        assert "rescued" in joined
        assert "thinking_delta" in joined
        assert len(opened) == 2


# 10 --------------------------------------------------------------------------------------------------

def test_keepalives_are_yielded_while_holding(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "30")
    settings.forget()
    monkeypatch.setattr(transport.KameTransport, "KEEPALIVE_S", 0.1)
    call = SeqCall([[0.02, think("a"), 0.8, chunk("answer", finish="stop")]])
    out = list(guard.stream(call))
    shape = kinds(out)
    assert shape.count("K") >= 3, shape
    assert [k for k in shape if k != "K"] == ["T:a", "X:answer"]


def test_keepalives_even_when_thought_chunks_arrive_faster_than_the_poll(guard, monkeypatch):
    monkeypatch.setenv("KAME_THOUGHT_QUIET_S", "30")
    settings.forget()
    monkeypatch.setattr(transport.KameTransport, "KEEPALIVE_S", 0.1)
    steps = [0.01]
    for n in range(30):
        steps += [think(str(n)), 0.02]
    steps += [chunk("done", finish="stop")]
    out = list(guard.stream(SeqCall([steps])))
    # 1.8.4.3 (L1): thoughts are live and ARE the heartbeat now (each one resets the keep-alive clock), so no
    # keep-alive is owed while they flow; every one of them reaches Hermes, in order.
    assert [k for k in kinds(out) if k != "K"] == [f"T:{n}" for n in range(30)] + ["X:done"]


# 11 --------------------------------------------------------------------------------------------------

def test_a_background_job_has_no_holding_and_no_rescue(guard):
    call = SeqCall([[0.02, think("a"), 0.6, chunk("answer", finish="stop")], [0.05, chunk("rescue", finish="stop")]])
    call.channel, call.task, call.task_known = "secondary", "title_generation", True
    seen = []
    started = time.monotonic()
    for c in guard.stream(call):
        seen.append((time.monotonic() - started, c))
    assert kinds([c for _, c in seen])[:2] == ["T:a", "X:answer"]
    assert seen[1][0] - seen[0][0] > 0.4, "reasoning passes live, 0.6 s before the answer"
    assert len(call.opened) == 1


# 12 --------------------------------------------------------------------------------------------------

def test_a_subscription_credential_gets_no_rescue_leg(guard):
    facade = importlib.import_module(f"{R.PACKAGE}.facade")

    class Subs(SeqCall):
        def entry(self, key):
            return SimpleNamespace(id=key[-4:], auth_type="oauth")

        def may_back_up(self, key):
            return facade._FacadeCall.may_back_up(self, key)

    call = Subs([[0.02, think("a"), 0.6, chunk("answer", finish="stop")], [0.05, chunk("rescue", finish="stop")]])
    out = list(guard.stream(call))
    assert R.shown(out) == "answer"
    assert len(call.opened) == 1


# 13 --------------------------------------------------------------------------------------------------

def test_the_1615_replay_a_503_a_silent_try_a_backup_that_thinks_then_goes_quiet(guard):
    """503 on key 1, try 2 silent, the backup at the trigger, reasoning, then silence: a rescue answers."""
    call = SeqCall([[R.Refusal(503, "The model is overloaded. Please try again later.")],
                    [30.0, chunk("silent try 2")],
                    [0.05, think("the backup thinks"), 30.0, chunk("never")],
                    [0.05, chunk("rescued answer", finish="stop")]])
    started = time.monotonic()
    out = list(guard.stream(call))
    elapsed = time.monotonic() - started
    assert R.shown(out) == "rescued answer"
    assert elapsed < 4.0, "the turn no longer waits for the read timeout"
    # 1.8.4.3 (L1): the backup's own thought was shown live before it went quiet
    assert [k for k in kinds(out) if k.startswith("T:")] == ["T:the backup thinks"]
    assert len(call.opened) == 4


# bound learning -------------------------------------------------------------------------------------

class TestTheBoundIsLearned:
    def test_twenty_seconds_before_any_data_and_clamped_after(self):
        tracker = R.hedging_mod.HedgeTracker()
        assert tracker.quiet_bound(IDENTITY, False) == 20.0
        for _ in range(30):
            tracker.record_quiet(IDENTITY, 0.5)
        assert tracker.quiet_bound(IDENTITY, False) == 8.0, "3 x p95 = 1.5 s is clamped up to 8 s"
        for _ in range(30):
            tracker.record_quiet(IDENTITY + "-slow", 30.0)
        # 1.8.4.3 (L4): was "clamped down to 20 s"; a model quiet by nature (30 s in every reply) now
        # keeps 1.25 x that quiet, so it is not asked twice on every long reply
        assert tracker.quiet_bound(IDENTITY + "-slow", False) == 37.5
        for _ in range(30):
            tracker.record_quiet(IDENTITY + "-mid", 3.0)
        assert tracker.quiet_bound(IDENTITY + "-mid", False) == pytest.approx(9.0)

    def test_bad_values_are_ignored(self):
        tracker = R.hedging_mod.HedgeTracker()
        for bad in (-1.0, float("nan"), float("inf")):
            tracker.record_quiet(IDENTITY, bad)
        assert tracker.quiet_bound(IDENTITY, False) == 20.0


# H1 --------------------------------------------------------------------------------------------------

class TestVisible:
    def test_what_is_visible(self):
        wires = importlib.import_module(f"{R.PACKAGE}.wires")
        v = transport._carries_visible
        assert not v(chunk(role="assistant"))
        assert not v(chunk())
        assert not v(think("x"))
        assert v(chunk("text"))
        assert v(chunk(tools=R.tool_call("f", "{}")))
        assert not v(wires._view({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}, 1))
        assert not v(wires._view({"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "thinking_delta", "thinking": "x"}}, 1))
        assert not v(wires._view({"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "signature_delta", "signature": "x"}}, 1))
        assert not v(wires._view({"type": "response.reasoning_summary_text.delta"}, 1))
        assert v(wires._view({"type": "content_block_delta", "index": 0,
                              "delta": {"type": "text_delta", "text": "hi"}}, 1))
        assert v(wires._view({"type": "content_block_start", "index": 0,
                              "content_block": {"type": "tool_use", "id": "t", "name": "f", "input": {}}}, 1))
