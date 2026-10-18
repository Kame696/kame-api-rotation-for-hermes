"""1.8.2.5: backup legs for streams, raced to the first word, through the real transport.

Real time and real threads: each fake stream sleeps before its chunks the way a
slow provider does, and closing its response interrupts the read the way httpx
does. The triggers are pinned (0.2 s and 0.4 s) so every test stays under a few
seconds; how the triggers are *learned* is tested on ``HedgeTracker`` directly.

Run:  uv run --with pytest python -m pytest tests/test_1_8_2_5_stream_race.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_race_1825_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
hedging_mod = importlib.import_module(f"{PACKAGE}.core.hedging")
pacing_mod = importlib.import_module(f"{PACKAGE}.core.pacing")
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"AIzaSyRACE{i}" + "0" * 29 for i in range(4)]
IDENTITY = "gemini:gemini-race"
REQUEST = {"model": "gemini-race", "messages": [{"role": "user", "content": "hi"}]}
TRIGGERS = (0.2, 0.4)


class Refusal(Exception):
    def __init__(self, status, message, details=()):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message,
                               "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE",
                               "details": list(details)}}


def per_minute():
    return Refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"}])


def chunk(text=None, tools=None, finish=None, role=None):
    delta = SimpleNamespace(role=role, content=text, tool_calls=tools)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)])


def tool_call(name="", args=""):
    return [SimpleNamespace(index=0, id="call_1" if name else None, type="function",
                            function=SimpleNamespace(name=name or None, arguments=args))]


class Response:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class FakeStream:
    """Steps: a number sleeps that long (interruptible), an exception raises, a chunk is yielded."""

    def __init__(self, steps):
        self.steps = steps
        self.response = Response()

    def __iter__(self):
        for step in self.steps:
            if isinstance(step, (int, float)):
                end = time.monotonic() + step
                while time.monotonic() < end:
                    if self.response.closed.is_set():
                        raise ConnectionError("response closed")
                    time.sleep(0.005)
                continue
            if isinstance(step, BaseException):
                raise step
            yield step

    def close(self):
        self.response.close()


class Pool:
    """Per key, the streams it will serve in order. Records every stream opened."""

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.opened = []
        self.lock = threading.Lock()

    def open(self, key):
        with self.lock:
            steps = self.plan[key].pop(0) if self.plan.get(key) else [chunk("x"), chunk(finish="stop")]
            stream = FakeStream(steps)
            self.opened.append((key, stream))
        if steps and isinstance(steps[0], BaseException) and not isinstance(steps[0], (int, float)):
            pass
        return stream


class FakeCall(transport.Call):
    def __init__(self, pool, keys=KEYS, kwargs=REQUEST):
        self.pool = pool
        self._keys = list(keys)
        self.kwargs = dict(kwargs)
        self.provider = "gemini"
        self.identity = IDENTITY
        self.closed = False

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        def create(**request):
            assert request.get("stream") is True
            return self.pool.open(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return self.closed


def shown(chunks):
    out = ""
    for c in chunks:
        for ch in getattr(c, "choices", None) or []:
            t = getattr(ch.delta, "content", None)
            if t:
                out += t
    return out


def keys_opened(pool):
    return [k for k, _ in pool.opened]


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    hedging_mod.HEDGE_TRACKER.clear()
    pacing_mod.PACER.clear()
    monkeypatch.setattr(hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: TRIGGERS)
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    yield t
    settings.forget()
    hedging_mod.HEDGE_TRACKER.clear()
    pacing_mod.PACER.clear()


def run(t, call):
    started = time.monotonic()
    out = list(t.stream(call))
    return out, time.monotonic() - started


class TestTheRace:
    def test_a_slow_first_word_is_beaten_by_a_backup(self, fresh):
        pool = Pool({KEYS[0]: [[3.0, chunk("slow")]],
                     KEYS[1]: [[0.05, chunk("fast "), chunk("answer", finish="stop")]],
                     KEYS[2]: [[0.05, chunk("fast "), chunk("answer", finish="stop")]],
                     KEYS[3]: [[0.05, chunk("fast "), chunk("answer", finish="stop")]]})
        out, elapsed = run(fresh, FakeCall(pool))
        assert shown(out) == "fast answer"
        assert elapsed < 1.0, elapsed
        primary = pool.opened[0][1]
        assert primary.response.closed.is_set()  # the slow leg was cut, not left running

    def test_a_fast_first_word_never_opens_a_backup(self, fresh):
        pool = Pool({KEYS[0]: [[0.01, chunk("quick", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "quick"
        assert keys_opened(pool) == [KEYS[0]]

    def test_a_reply_that_is_thinking_is_working_and_gets_no_backup(self, fresh):
        # Live on NVIDIA GLM-5.3: replies stream their reasoning long before
        # the first word. That is a reply at work, not a stuck one.
        thinking = SimpleNamespace(id="c", model="m", usage=None, choices=[SimpleNamespace(
            index=0, delta=SimpleNamespace(role=None, content=None, tool_calls=None,
                                           reasoning_content="let me think"), finish_reason=None)])
        pool = Pool({KEYS[0]: [[0.05, thinking, 0.8, chunk("answer", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "answer"
        assert keys_opened(pool) == [KEYS[0]]

    def test_claude_thinking_on_the_messages_wire_counts_as_working(self):
        wires = importlib.import_module(f"{PACKAGE}.wires")
        thinking_start = {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}
        thinking_delta = {"type": "content_block_delta", "index": 0,
                          "delta": {"type": "thinking_delta", "thinking": "hmm"}}
        ping = {"type": "ping"}
        assert transport._carries_answer(wires._view(thinking_start, 1))
        assert transport._carries_answer(wires._view(thinking_delta, 1))
        assert not transport._carries_answer(wires._view(ping, 1))
        assert transport._carries_answer(wires._view({"type": "response.reasoning_summary_text.delta"}, 1))

    def test_a_silent_reply_still_gets_its_backup(self, fresh):
        thinking = SimpleNamespace(id="c", model="m", usage=None, choices=[SimpleNamespace(
            index=0, delta=SimpleNamespace(role=None, content=None, tool_calls=None,
                                           reasoning_content="thinking"), finish_reason=None)])
        pool = Pool({KEYS[0]: [[3.0, chunk("late")]],
                     KEYS[1]: [[0.05, thinking, chunk("ok", finish="stop")]],
                     KEYS[2]: [[0.05, thinking, chunk("ok", finish="stop")]],
                     KEYS[3]: [[0.05, thinking, chunk("ok", finish="stop")]]})
        out, elapsed = run(fresh, FakeCall(pool))
        assert shown(out) == "ok" and elapsed < 1.0

    def test_alone_the_primarys_wordless_chunks_pass_straight_through(self, fresh):
        role = chunk(role="assistant")
        pool = Pool({KEYS[0]: [[role, 0.05, chunk("hi", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert out[0] is role and shown(out) == "hi"

    def test_the_primary_answering_after_a_backup_started_still_wins_whole(self, fresh):
        # The primary speaks at 0.3 s (after the 0.2 s trigger), the backup at 2 s.
        pool = Pool({KEYS[0]: [[chunk(role="assistant"), 0.3, chunk("primary "), chunk("wins", finish="stop")]],
                     KEYS[1]: [[2.0, chunk("late")]], KEYS[2]: [[2.0, chunk("late")]], KEYS[3]: [[2.0, chunk("late")]]})
        out, elapsed = run(fresh, FakeCall(pool))
        assert shown(out) == "primary wins"
        assert elapsed < 1.5
        backups = [s for k, s in pool.opened if k != KEYS[0]]
        assert backups and all(s.response.closed.is_set() for s in backups)

    def test_three_legs_when_the_first_backup_is_slow_too(self, fresh, monkeypatch):
        # A fresh tracker holds one backup token; this test is about the
        # mechanism, so the budget is opened (budget tests are below).
        monkeypatch.setattr(hedging_mod.HEDGE_TRACKER, "may_start_backup", lambda *a, **k: True)
        pool = Pool({KEYS[0]: [[3.0, chunk("a")]], KEYS[1]: [[3.0, chunk("b")]],
                     KEYS[2]: [[0.05, chunk("third", finish="stop")]], KEYS[3]: [[0.05, chunk("third", finish="stop")]]})
        out, elapsed = run(fresh, FakeCall(pool))
        assert len(pool.opened) == 3
        assert elapsed < 1.5
        winner_key = pool.opened[2][0]
        assert shown(out) == "third" and winner_key in (KEYS[2], KEYS[3], KEYS[1])

    def test_max_legs_two_means_one_backup(self, fresh, monkeypatch):
        monkeypatch.setenv("KAME_HEDGE_MAX_LEGS", "2")
        settings.forget()
        pool = Pool({KEYS[0]: [[1.0, chunk("a", finish="stop")]], KEYS[1]: [[1.0, chunk("b", finish="stop")]],
                     KEYS[2]: [[0.01, chunk("c", finish="stop")]], KEYS[3]: [[0.01, chunk("d", finish="stop")]]})
        run(fresh, FakeCall(pool))
        assert len(pool.opened) == 2

    def test_no_budget_no_backup(self, fresh, monkeypatch):
        monkeypatch.setattr(hedging_mod.HEDGE_TRACKER, "may_start_backup", lambda *a, **k: False)
        pool = Pool({KEYS[0]: [[0.5, chunk("only", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "only" and keys_opened(pool) == [KEYS[0]]

    def test_hedging_switched_off_never_races(self, fresh, monkeypatch):
        monkeypatch.setenv("KAME_HEDGING_DISABLED", "1")
        settings.forget()
        pool = Pool({KEYS[0]: [[0.5, chunk("only", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "only" and keys_opened(pool) == [KEYS[0]]


class TestEveryLegIsJudgedOnItsOwnKey:
    def test_a_primary_refused_before_any_backup_rotates_as_before(self, fresh):
        pool = Pool({KEYS[0]: [[per_minute()]], KEYS[1]: [[chunk("next", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "next"
        assert fresh.engine.healthy_count(IDENTITY, [KEYS[0]]) == 0

    def test_a_backup_refused_rests_the_backup_and_cools_backups(self, fresh):
        pool = Pool({KEYS[0]: [[0.6, chunk("primary", finish="stop")]],
                     KEYS[1]: [[per_minute()]], KEYS[2]: [[per_minute()]], KEYS[3]: [[per_minute()]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "primary"
        backup = pool.opened[1][0]
        assert fresh.engine.healthy_count(IDENTITY, [backup]) == 0
        assert fresh.engine.healthy_count(IDENTITY, [KEYS[0]]) == 1
        assert hedging_mod.HEDGE_TRACKER._cool_until.get(IDENTITY, 0) > time.monotonic()

    def test_a_primary_that_fails_after_the_backup_started_rests_only_itself(self, fresh):
        pool = Pool({KEYS[0]: [[0.5, per_minute()]],
                     KEYS[1]: [[0.8, chunk("backup", finish="stop")]], KEYS[2]: [[0.8, chunk("backup", finish="stop")]],
                     KEYS[3]: [[0.8, chunk("backup", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "backup"
        assert fresh.engine.healthy_count(IDENTITY, [KEYS[0]]) == 0
        winner = pool.opened[1][0]
        assert fresh.engine.healthy_count(IDENTITY, [winner]) == 1

    def test_when_both_legs_fail_each_rests_and_the_next_key_answers(self, fresh, monkeypatch):
        # Three keys: the primary and (whichever is the backup) fail; the key
        # left over answers on the next attempt, with no 30 s wait.
        monkeypatch.setenv("KAME_HEDGE_MAX_LEGS", "2")
        settings.forget()
        keys = KEYS[:3]
        pool = Pool({KEYS[0]: [[0.3, per_minute()]], KEYS[1]: [[0.3, per_minute()]],
                     KEYS[2]: [[chunk("recovered", finish="stop")]]})
        out, elapsed = run(fresh, FakeCall(pool, keys=keys))
        assert shown(out) == "recovered"
        assert elapsed < 5.0
        failed = {k for k, s in pool.opened if k != KEYS[2]}
        assert KEYS[0] in failed
        assert all(fresh.engine.healthy_count(IDENTITY, [k]) == 0 for k in failed)

    def test_empty_legs_fall_to_the_empty_answer_rule(self, fresh, monkeypatch):
        monkeypatch.setenv("KAME_HEDGE_MAX_LEGS", "2")
        settings.forget()
        keys = KEYS[:3]
        empty = [0.3, chunk(finish="stop")]
        pool = Pool({KEYS[0]: [list(empty)], KEYS[1]: [list(empty)],
                     KEYS[2]: [[chunk("third", finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool, keys=keys))
        assert shown(out) == "third"

    def test_the_cut_leg_is_counted_by_the_pacer(self, fresh):
        pool = Pool({KEYS[0]: [[3.0, chunk("slow")]], KEYS[1]: [[0.05, chunk("fast", finish="stop")]],
                     KEYS[2]: [[0.05, chunk("fast", finish="stop")]], KEYS[3]: [[0.05, chunk("fast", finish="stop")]]})
        run(fresh, FakeCall(pool))
        logs = {scope for (ident, scope) in pacing_mod.PACER._log if ident == IDENTITY}
        assert KEYS[0] in logs  # the request the cut leg made still counts against its key


class TestTheAnswerAfterTheRace:
    def test_a_tool_call_wins_the_race_and_arrives_whole(self, fresh):
        pool = Pool({KEYS[0]: [[3.0, chunk("slow")]],
                     KEYS[1]: [[0.05, chunk(tools=tool_call("search", '{"q": "par')),
                                chunk(tools=tool_call("", 'is"}'), finish="tool_calls")]]})
        pool.plan[KEYS[2]] = list(pool.plan[KEYS[1]])
        pool.plan[KEYS[3]] = list(pool.plan[KEYS[1]])
        out, _ = run(fresh, FakeCall(pool))
        tools = [c for c in out if getattr(c, "choices", None) and c.choices[0].delta.tool_calls]
        assert tools and len(tools) == 2

    def test_a_winner_cut_mid_answer_is_continued_on_another_key(self, fresh):
        cut = ConnectionError("peer closed connection without sending complete message body")
        pool = Pool({KEYS[0]: [[3.0, chunk("slow")]],
                     KEYS[1]: [[0.05, chunk("The capital of France "), chunk("is Par"), cut], [chunk("is."), chunk(finish="stop")]],
                     KEYS[2]: [[0.05, chunk("The capital of France "), chunk("is Par"), cut], [chunk("is."), chunk(finish="stop")]],
                     KEYS[3]: [[chunk("is."), chunk(finish="stop")]]})
        out, _ = run(fresh, FakeCall(pool))
        assert shown(out) == "The capital of France is Paris."

    def test_stop_during_the_race_aborts_every_leg(self, fresh):
        pool = Pool({k: [[5.0, chunk("never")]] for k in KEYS})
        call = FakeCall(pool)
        threading.Timer(0.6, lambda: setattr(call, "closed", True)).start()
        with pytest.raises(BaseException):
            list(fresh.stream(call))
        time.sleep(0.1)
        assert pool.opened and all(s.response.closed.is_set() for _, s in pool.opened)


class TestTheMessagesWire:
    """The owner talks to Claude over the Anthropic Messages wire: native events,
    a context-manager stream, the response attached through the call."""

    @staticmethod
    def events(text, delay):
        return [delay,
                {"type": "message_start", "message": {"id": "m", "model": "claude", "content": []}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"}]

    def test_a_slow_claude_reply_is_beaten_by_a_backup(self, fresh):
        wires = importlib.import_module(f"{PACKAGE}.wires")
        # The wire imports the SDK on first use (about 2 s cold); the host has it loaded
        # long before a chat starts, so warm it outside the timed region.
        pytest.importorskip("anthropic")
        importlib.import_module("anthropic.lib.streaming._messages")
        importlib.import_module("anthropic.types")
        plans = {KEYS[0]: self.events("slow", 3.0)}
        for k in KEYS[1:]:
            plans[k] = self.events("fast", 0.05)
        opened = []

        class Manager:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self.stream

            def __exit__(self, *exc):
                self.stream.close()

        class MessagesCall(FakeCall):
            wire = wires.MESSAGES
            response = None

            def attach_response(self, response):
                assert self.response is None, "a second response was attached"
                self.response = response

            def client_for(self, key, attempt):
                def stream(**request):
                    s = FakeStream(list(plans[key]))
                    opened.append((key, s))
                    return Manager(s)
                return SimpleNamespace(messages=SimpleNamespace(stream=stream))

        call = MessagesCall(Pool({}))
        started = time.monotonic()
        out = list(fresh.stream(call))
        elapsed = time.monotonic() - started
        texts = [e for e in out if isinstance(e, dict) and e.get("type") == "content_block_delta"]
        joined = "".join(str(e.get("delta", {}).get("text", "")) for e in texts) or repr(out)
        assert "fast" in joined and "slow" not in joined
        assert elapsed < 1.5
        assert opened[0][1].response.closed.is_set()
        assert call.response is not None  # the winner's response, attached once


class TestTheTriggersAreLearned:
    def test_cold_start_uses_the_class_number(self, monkeypatch):
        tracker = hedging_mod.HedgeTracker()
        first, second = tracker.stream_triggers(IDENTITY, reasoning=False)
        assert first == 6.0 and second == 12.0
        first, _ = tracker.stream_triggers(IDENTITY, reasoning=True)
        assert first == 30.0

    def test_learned_trigger_is_twice_the_median_first_word(self):
        tracker = hedging_mod.HedgeTracker()
        now = time.monotonic()
        for i in range(40):
            tracker.record_latency(IDENTITY, 5.0 if i % 4 else 20.0, stream=True, now=now)
        first, second = tracker.stream_triggers(IDENTITY, reasoning=False, now=now)
        assert first == pytest.approx(10.0) and second == pytest.approx(20.0)

    def test_a_fresh_process_starts_from_the_recorded_history(self, monkeypatch, tmp_path):
        timings = importlib.import_module(f"{PACKAGE}.timings")
        monkeypatch.setattr(timings, "first_word_history", lambda: {IDENTITY: [4.0] * 30 + [40.0] * 10})
        hedging_mod.HEDGE_TRACKER.clear()
        t = transport.KameTransport(engine=carousel_mod.Carousel())
        t._seed_race_history(IDENTITY, False)
        first, _ = hedging_mod.HEDGE_TRACKER.stream_triggers(IDENTITY, reasoning=False)
        assert first == pytest.approx(8.0)
        hedging_mod.HEDGE_TRACKER.clear()

    def test_seeding_never_overwrites_live_samples_and_never_refills_the_budget(self):
        tracker = hedging_mod.HedgeTracker()
        tracker.record_latency(IDENTITY, 2.0, stream=True)
        assert tracker.seed_stream(IDENTITY, [9.0] * 50, reasoning=False) == 0
        fresh_tracker = hedging_mod.HedgeTracker()
        assert fresh_tracker.seed_stream(IDENTITY, [9.0] * 50, reasoning=False) == 50
        key = hedging_mod.bucket_key(IDENTITY, True, False)
        assert key not in fresh_tracker._budgets

    def test_the_history_reader_keeps_answered_recent_first_words(self, monkeypatch, tmp_path):
        import json as _json
        timings = importlib.import_module(f"{PACKAGE}.timings")
        path = tmp_path / "calls.jsonl"
        now = time.time()
        rows = [
            {"identity": IDENTITY, "outcome": "answered", "ms_to_first_text": 5000, "at": now - 60},
            {"identity": IDENTITY, "outcome": "answered", "ms_to_first_sign": 2000, "ms_to_first_text": 9000,
             "at": now - 50},
            {"identity": IDENTITY, "outcome": "rotate", "ms_to_first_text": 900, "at": now - 60},
            {"identity": IDENTITY, "outcome": "answered", "ms_to_first_text": 7000, "at": now - 30 * 86400},
            {"identity": IDENTITY, "outcome": "answered", "ms_to_first_text": None, "at": now - 60},
        ]
        path.write_text("\n".join(_json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
        monkeypatch.setattr(timings, "_destination", lambda: str(path))
        assert timings.first_word_history(now=now) == {IDENTITY: [5.0, 2.0]}

    def test_never_below_one_second(self):
        tracker = hedging_mod.HedgeTracker()
        now = time.monotonic()
        for _ in range(40):
            tracker.record_latency(IDENTITY, 0.1, stream=True, now=now)
        first, _ = tracker.stream_triggers(IDENTITY, reasoning=False, now=now)
        assert first == 1.0
