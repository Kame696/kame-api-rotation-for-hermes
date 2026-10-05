"""1.8.1.9 — the carousel at the client: what only a stream can show.

``test_dispatch`` and the rest of the 1.8.1.8 suite run against the transport
through ``legacy_dispatch`` and pin its *decisions*. They call it the way
1.8.1.8 was called, with a finished response. What changed in 1.8.1.9 is that
Hermes now reads the transport chunk by chunk, so these pin what the user sees
on screen while a key fails under it:

* a refusal before any text rotates without a trace in the stream;
* a stream cut mid-answer is continued on another key, without the words
  already shown appearing twice;
* a tool call cut halfway never reaches Hermes — the whole request is asked of
  another key, and only a complete call is released;
* a wait on a fully resting pool sends keep-alive chunks, so Hermes' silence
  watchdog (``HERMES_STREAM_STALE_TIMEOUT``) never mistakes it for a hung
  provider;
* closing the client stops the wait.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_transport_1819_under_test"


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
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"AIzaSyKEY{i}" + "0" * 29 for i in range(3)]
IDENTITY = "gemini:gemini-test"
REQUEST = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}


class Clock:
    """Wall and monotonic time that only move when the transport sleeps."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.slept = 0.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def time_ns(self) -> int:
        return int(self.now * 1e9)

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    fake = SimpleNamespace(time=c.time, monotonic=c.monotonic, time_ns=c.time_ns, sleep=c.sleep,
                           strftime=__import__("time").strftime, localtime=__import__("time").localtime,
                           gmtime=__import__("time").gmtime)
    monkeypatch.setattr(carousel_mod, "time", fake)
    monkeypatch.setattr(transport, "time", fake)
    settings.forget()
    yield c
    settings.forget()


def _transport(clock) -> "transport.KameTransport":
    return transport.KameTransport(engine=carousel_mod.Carousel(), sleep=clock.sleep)


class Refusal(Exception):
    """A provider refusal with Google's own body, as ``core.evidence`` reads one."""

    def __init__(self, status: int, message: str, details=()):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message,
                               "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE",
                               "details": list(details)}}


def per_minute(retry_s: int = 30) -> Refusal:
    return Refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{retry_s}s"},
    ])


def text(t: str, finish=None):
    delta = SimpleNamespace(role="assistant", content=t, tool_calls=None)
    return SimpleNamespace(id="c", model="m", choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)],
                           usage=None)


def tool(name: str = "", args: str = "", finish=None):
    call = SimpleNamespace(index=0, id="call_1" if name else None, type="function",
                           function=SimpleNamespace(name=name or None, arguments=args))
    delta = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
    return SimpleNamespace(id="c", model="m", choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)],
                           usage=None)


def finish(reason="stop"):
    delta = SimpleNamespace(role=None, content=None, tool_calls=None)
    return SimpleNamespace(id="c", model="m", choices=[SimpleNamespace(index=0, delta=delta, finish_reason=reason)],
                           usage=None)


class Script:
    """What each key does when asked: a list of chunks, possibly ending in an exception."""

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.asked = []
        self.requests = []

    def run(self, key, request):
        self.asked.append(key)
        self.requests.append(request)
        steps = self.plan[key].pop(0) if self.plan.get(key) else [finish()]
        if isinstance(steps, BaseException):
            raise steps

        def gen():
            for step in steps:
                if isinstance(step, BaseException):
                    raise step
                yield step
        return gen()


class FakeCall(transport.Call):
    def __init__(self, script, keys=KEYS, kwargs=REQUEST):
        self.script = script
        self._keys = list(keys)
        self.kwargs = dict(kwargs)
        self.provider = "gemini"
        self.identity = IDENTITY
        self.closed = False

    def keys(self):
        return list(self._keys)

    def client_for(self, key, attempt):
        def create(**request):
            return self.script.run(key, request)
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


def tool_chunks(chunks):
    return [c for c in chunks if (getattr(c, "choices", None) or [None])[0] is not None
            and getattr(c.choices[0].delta, "tool_calls", None)]


class TestARefusalBeforeAnyText:
    def test_rotates_and_the_stream_shows_only_the_answer(self, clock):
        script = Script({KEYS[0]: [per_minute()], KEYS[1]: [[text("Hello"), text(" world", "stop")]]})
        t = _transport(clock)
        out = list(t.stream(FakeCall(script)))
        assert shown(out) == "Hello world"
        assert script.asked == [KEYS[0], KEYS[1]]
        assert t.engine.healthy_count(IDENTITY, [KEYS[0]]) == 0
        assert t.rotations == 1

    def test_the_rested_key_is_not_asked_again_while_it_rests(self, clock):
        script = Script({KEYS[0]: [per_minute()], KEYS[1]: [[text("a", "stop")], [text("b", "stop")]]})
        t = _transport(clock)
        list(t.stream(FakeCall(script)))
        list(t.stream(FakeCall(script)))
        assert script.asked.count(KEYS[0]) == 1


class TestAStreamCutMidAnswer:
    def test_is_continued_on_another_key_without_repeating_words(self, clock):
        cut = ConnectionError("peer closed connection without sending complete message body")
        script = Script({
            KEYS[0]: [[text("The capital of France "), text("is Par"), cut]],
            KEYS[1]: [[text("is."), finish()]],
        })
        t = _transport(clock)
        out = list(t.stream(FakeCall(script)))
        assert shown(out) == "The capital of France is Paris."
        # The continuation carried what was already shown.
        resumed = script.requests[1]["messages"]
        assert any("The capital of France is Par" in str(m.get("content")) for m in resumed)
        assert t.resumes == 1

    def test_a_continuation_that_restarts_the_answer_is_trimmed(self, clock):
        cut = ConnectionError("peer closed connection without sending complete message body")
        script = Script({
            KEYS[0]: [[text("The capital of France "), text("is Par"), cut]],
            KEYS[1]: [[text("The capital of France is Paris."), finish()]],
        })
        out = list(_transport(clock).stream(FakeCall(script)))
        assert shown(out) == "The capital of France is Paris."

    def test_a_stream_that_ends_without_a_finish_is_continued_too(self, clock):
        script = Script({
            KEYS[0]: [[text("one two ")]],
            KEYS[1]: [[text("three"), finish()]],
        })
        out = list(_transport(clock).stream(FakeCall(script)))
        assert shown(out) == "one two three"


class TestAToolCallCutHalfway:
    def test_never_reaches_hermes_and_is_asked_again_whole(self, clock):
        cut = ConnectionError("stream reset")
        script = Script({
            KEYS[0]: [[tool("search", '{"q": "par'), cut]],
            KEYS[1]: [[tool("search", '{"q": "paris"}'), finish("tool_calls")]],
        })
        t = _transport(clock)
        out = list(t.stream(FakeCall(script)))
        tools = tool_chunks(out)
        assert len(tools) == 1
        assert tools[0].choices[0].delta.tool_calls[0].function.arguments == '{"q": "paris"}'
        # The replay is the original request, not a continuation.
        assert script.requests[1]["messages"] == REQUEST["messages"]
        assert t.tool_call_retries == 1
        # A complete tool call is an answer: nothing is asked after it.
        assert script.asked == [KEYS[0], KEYS[1]]

    def test_a_tool_call_alone_is_an_answer_not_an_empty_one(self, clock):
        script = Script({KEYS[0]: [[tool("search", '{"q": "x"}'), finish("tool_calls")]]})
        t = _transport(clock)
        out = list(t.stream(FakeCall(script)))
        assert len(tool_chunks(out)) == 1
        assert script.asked == [KEYS[0]]
        assert t.rotations == 0

    def test_text_then_tools_keeps_the_order_hermes_expects(self, clock):
        script = Script({KEYS[0]: [[text("Looking it up."), tool("search", "{}"), finish("tool_calls")]]})
        out = list(_transport(clock).stream(FakeCall(script)))
        kinds = []
        for c in out:
            ch = c.choices[0]
            if ch.delta.content:
                kinds.append("text")
            elif getattr(ch.delta, "tool_calls", None):
                kinds.append("tool")
            if ch.finish_reason:
                kinds.append("finish")
        assert kinds == ["text", "tool", "finish"]


class TestAnEmptyAnswer:
    def test_moves_to_the_next_key(self, clock):
        script = Script({KEYS[0]: [[finish()]], KEYS[1]: [[text("real", "stop")]]})
        out = list(_transport(clock).stream(FakeCall(script)))
        assert shown(out) == "real"


class TestAPoolThatIsAllResting:
    def test_waits_with_keep_alives_then_answers(self, clock):
        t = _transport(clock)
        for key in KEYS:
            t.engine.mark(IDENTITY, key, False, 40.0, "rate_limit")
        script = Script({k: [[text("back", "stop")]] for k in KEYS})
        out = list(t.stream(FakeCall(script)))
        keepalives = [c for c in out if not c.choices]
        assert shown(out) == "back"
        assert len(keepalives) >= 2  # every 15 s of a ~40 s wait
        assert clock.slept >= 40.0
        assert t.waits >= 1

    def test_closing_the_client_ends_the_wait(self, clock):
        t = _transport(clock)
        for key in KEYS:
            t.engine.mark(IDENTITY, key, False, 3000.0, "rate_limit")
        call = FakeCall(Script({}))
        stream = t.stream(call)
        first = next(stream)  # a keep-alive, 15 s in
        assert not first.choices
        call.closed = True
        with pytest.raises(InterruptedError):
            list(stream)
        assert clock.slept < 60


class TestNonStreaming:
    def test_a_503_rotates_and_the_answer_is_returned(self, clock):
        answer = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None),
                                                          finish_reason="stop")])

        class Plain(FakeCall):
            def client_for(self, key, attempt):
                def create(**request):
                    self.script.asked.append(key)
                    if key == KEYS[0]:
                        raise Refusal(503, "The model is overloaded. Please try again later.")
                    return answer
                return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        script = Script({})
        assert _transport(clock).complete(Plain(script)) is answer
        assert script.asked == [KEYS[0], KEYS[1]]

    def test_no_keys_means_hermes_request_untouched(self, clock):
        seen = {}

        class NoKeys(FakeCall):
            def keys(self):
                return []

            def client_for(self, key, attempt):
                seen["key"] = key
                return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                    create=lambda **r: seen.setdefault("request", r) and "answer")))

        assert _transport(clock).complete(NoKeys(Script({}))) == "answer"
        assert seen["key"] == "" and seen["request"] == REQUEST


class TestTheSilenceTimeout:
    """1.8.1.8's ``_SilenceTimeout`` promises, now applied to the request KAME owns."""

    def test_off_by_default_changes_nothing(self, monkeypatch, clock):
        monkeypatch.delenv("KAME_STREAM_SILENCE_TIMEOUT", raising=False)
        monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT", raising=False)
        assert transport.attempt_read_timeout("https://generativelanguage.googleapis.com", 1) is None

    def test_it_leaves_the_key_before_the_cut(self, monkeypatch, clock):
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "30")
        monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT", raising=False)
        settings.forget()
        assert transport.attempt_read_timeout("https://integrate.api.nvidia.com/v1", 1) == 30.0
        # A provider proven slow gets a shorter leash from the third attempt.
        assert transport.attempt_read_timeout("https://integrate.api.nvidia.com/v1", 3) == 7.5

    def test_a_number_the_user_set_themselves_is_never_overruled(self, monkeypatch, clock):
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "30")
        monkeypatch.setenv("HERMES_STREAM_READ_TIMEOUT", "600")
        settings.forget()
        assert transport.attempt_read_timeout("https://integrate.api.nvidia.com/v1", 1) is None

    def test_a_local_model_is_left_alone(self, monkeypatch, clock):
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "30")
        monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT", raising=False)
        settings.forget()
        assert transport.attempt_read_timeout("http://localhost:11434/v1", 1) is None


class TestWhatIsDecidedByEvidence:
    def test_no_provider_is_named_in_the_prefill_decision(self):
        source = (PLUGIN_DIR / "transport.py").read_text(encoding="utf-8")
        body = source[source.index("def _prefill_refused"): source.index("def _resume_kwargs")]
        assert "gemini" not in body.lower().replace("gemini's", "")
        assert transport._prefill_refused("google:gemini-3.7-flash") is False

    def test_a_request_shape_kame_does_not_recognise_is_never_rewritten(self, clock):
        assert transport.KameTransport._resume_budget({"prompt": "no messages here"}) == 0


class TestTheStopRuleOnADropThatArrivesAsAnError:
    """1.6.0.0's progress rule, for drops that are exceptions rather than stubs."""

    def test_continuations_that_add_nothing_end_after_one_lap(self, clock):
        drop = ConnectionError("peer closed connection without sending complete message body")
        script = Script({
            KEYS[0]: [[text("half "), drop], [drop]],
            KEYS[1]: [[drop], [drop]],
            KEYS[2]: [[drop], [drop]],
        })
        t = _transport(clock)
        with pytest.raises(Exception):
            out = []
            for chunk in t.stream(FakeCall(script)):
                out.append(chunk)
        assert shown(out) == "half "
        # The first attempt, then one continuation per key, and not one more.
        assert len(script.asked) <= len(KEYS) + 2
        assert t.mid_stream_cuts == 1

    def test_a_key_that_adds_words_resets_the_lap(self, clock):
        drop = ConnectionError("reset")
        script = Script({
            KEYS[0]: [[text("one "), drop]],
            KEYS[1]: [[text("two "), drop]],
            KEYS[2]: [[text("three"), finish()]],
        })
        out = list(_transport(clock).stream(FakeCall(script)))
        assert shown(out) == "one two three"


class TestAToolCallCutOnEveryKey:
    def test_goes_back_to_hermes_exactly_as_the_last_key_sent_it(self, clock):
        script = Script({k: [[tool("write_file", '{"pa')]] for k in KEYS})
        t = _transport(clock)
        out = list(t.stream(FakeCall(script)))
        tools = tool_chunks(out)
        # Only the last key's half call, never a mix of several.
        assert len(tools) == 1
        assert not any(c.choices[0].finish_reason for c in out if c.choices)
        assert t.tool_call_cuts == 1
        assert t.tool_call_retries == len(KEYS) - 1


class TestWhatTheJournalIsToldAnswered:
    """``post_api_request`` fed 1.8.1.8's pool binding; the client now says which key answered."""

    def _recorded(self, clock, script):
        runtime = importlib.import_module(f"{PACKAGE}.runtime")
        seen = []
        runtime.set_answer_recorder(lambda **row: seen.append(row))
        try:
            list(_transport(clock).stream(FakeCall(script)))
        finally:
            runtime.set_answer_recorder(None)
        return seen

    def test_an_answer_is_filed_against_the_key_that_gave_it(self, clock):
        seen = self._recorded(clock, Script({KEYS[0]: [per_minute()], KEYS[1]: [[text("ok", "stop")]]}))
        assert len(seen) == 1 and seen[0]["model"] == IDENTITY

    def test_a_tool_call_with_no_prose_is_still_believed(self, clock):
        seen = self._recorded(clock, Script({KEYS[0]: [[tool("search", "{}"), finish("tool_calls")]]}))
        assert len(seen) == 1

    def test_an_answer_that_carried_nothing_is_not(self, clock):
        script = Script({k: [[finish()], [finish()]] for k in KEYS})
        assert self._recorded(clock, script) == []


class TestTheStatusLineReachesTheTurn:
    """1.8.1.8's spinner line and wait notices, through Hermes' notify_turn_status seam."""

    @pytest.fixture()
    def rail(self, monkeypatch):
        said = []

        def notify(message, *, kind="lifecycle"):
            said.append((kind, message))
            return True

        monkeypatch.setattr(transport, "_NOTIFY", notify)
        transport._Spinner.reset()
        yield said
        transport._Spinner.reset()

    def test_every_attempt_says_the_pool_health_and_a_rotation_says_so(self, clock, rail):
        script = Script({KEYS[0]: [per_minute()], KEYS[1]: [[text("Hello", "stop")]]})
        list(_transport(clock).stream(FakeCall(script)))
        lines = [m for k, m in rail if k == "activity"]
        assert lines[0] == "⏳ waiting on gemini-test — KAME 3/3 keys healthy"
        assert all(transport.passes_desktop_status_gate(m) for m in lines)
        # A changed line waits at most the 1.8.1.7.2 floor of one second.
        clock.sleep(1.5)
        transport._Spinner.update(f"call:{id(self):x}", "x")
        rail.clear()
        script = Script({KEYS[2]: [per_minute()], KEYS[0]: [[text("Hi", "stop")]], KEYS[1]: [[text("Hi", "stop")]]})
        call = FakeCall(script)
        t = _transport(clock)
        t.engine.mark(IDENTITY, KEYS[0], False, 30.0, "rate_limit")
        list(t.stream(call))
        assert [m for k, m in rail if k == "activity"][0] == (
            "↻ waiting on gemini-test — KAME 2/3 keys healthy, trying the next key")

    def test_a_wait_counts_down_and_a_long_one_is_announced(self, clock, rail):
        t = _transport(clock)
        for key in KEYS:
            t.engine.mark(IDENTITY, key, False, 400.0, "rate_limit")
        list(t.stream(FakeCall(Script({k: [[text("back", "stop")]] for k in KEYS}))))
        activity = [m for k, m in rail if k == "activity"]
        assert any("a key to come back" in m and "next key in" in m for m in activity)
        lifecycle = [m for k, m in rail if k == "lifecycle"]
        assert any(m.startswith("KAME:") and "resting" in m for m in lifecycle)
        assert any("back up after" in m for m in lifecycle)
        # 1.8.1.8's throttle: a line whose text changed waits at most one second.
        assert len(activity) <= 400 + 5

    def test_switched_off_the_spinner_says_nothing(self, clock, rail, monkeypatch):
        monkeypatch.setenv("KAME_LIVE_STATUS_DISABLED", "1")
        settings.forget()
        script = Script({KEYS[0]: [[text("Hello", "stop")]]})
        list(_transport(clock).stream(FakeCall(script)))
        assert [m for k, m in rail if k == "activity"] == []

    def test_a_hermes_without_the_seam_changes_nothing(self, clock, monkeypatch):
        monkeypatch.setattr(transport, "_NOTIFY", False)
        script = Script({KEYS[0]: [[text("Hello", "stop")]]})
        assert shown(list(_transport(clock).stream(FakeCall(script)))) == "Hello"
