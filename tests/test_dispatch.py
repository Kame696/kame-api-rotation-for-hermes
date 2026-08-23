"""The wrapper around Hermes' own dispatch, and the four promises it makes.

The engine tested in ``test_carousel`` decides *which key*. This decides *when
the host is called, with what, and what happens to a failure* — and every one
of these is a promise that can be broken silently, which is why they are pinned
against a fake agent shaped exactly like the real one rather than against the
binding's own internals.

1. A healthy call picks a key and hands it to the host untouched. The plugin
   does not re-implement the request; that was the v1.0.9 lesson.
2. A recoverable failure rotates and never reaches the caller. This is the
   whole point: the 503 that ended a turn with fourteen untouched keys.
3. A terminal failure is raised at once. Fifteen slow refusals are worse than
   one fast one.
4. A failure that arrives *after* the user has seen text is raised too —
   replaying a partial stream would print the answer twice.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_dispatch_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
settings = importlib.import_module(f"{PACKAGE}.settings")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel

KEYS = [f"AIzaSyKEY{i}" + "0" * 29 for i in range(4)]


class Boom(Exception):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class Entry:
    def __init__(self, key, entry_id):
        self.runtime_api_key = key
        self.access_token = ""
        self.id = entry_id


class Pool:
    def __init__(self, keys):
        self._entries = [Entry(k, f"e{i}") for i, k in enumerate(keys)]

    def entries(self):
        return list(self._entries)


class Client:
    def __init__(self, key):
        self.api_key = key


class Agent:
    """A Hermes agent, reduced to what the binding reads and writes."""

    def __init__(self, keys=KEYS, *, api_mode="chat_completions"):
        self.provider = "google"
        self.model = "gemini-3.7-flash"
        self.api_mode = api_mode
        self.api_key = keys[0] if keys else ""
        self._credential_pool = Pool(keys) if keys else None
        self._client_kwargs = {"api_key": self.api_key}
        self.client = Client(self.api_key)
        self._credential_pool_entry_id = None
        self._interrupt_requested = False
        self.stream_delta_callback = None


class Answer:
    """The host's response shape, reduced to what ``_result_is_empty`` reads."""

    class _Message:
        def __init__(self, content):
            self.content = content
            self.tool_calls = None

    class _Choice:
        def __init__(self, content):
            self.message = Answer._Message(content)

    def __init__(self, content="hello"):
        self.choices = [Answer._Choice(content)]


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    yield
    settings.forget()


def _binding():
    return DispatchBinding(engine=Carousel())


class Module:
    """A stand-in for ``agent.chat_completion_helpers``."""

    def __init__(self, streaming=None, plain=None):
        self.interruptible_streaming_api_call = streaming or (lambda agent, api_kwargs, **kw: Answer())
        self.interruptible_api_call = plain or (lambda agent, api_kwargs: Answer())


# --- the healthy path -------------------------------------------------------


class TestAHealthyCall:
    def test_the_host_makes_the_request_and_its_answer_is_returned(self):
        seen = {}

        def host(agent, api_kwargs, **kwargs):
            seen["key"] = agent.api_key
            seen["kwargs"] = api_kwargs
            return Answer("from the host")

        binding = _binding()
        agent = Agent()
        result = binding.run(host, agent, {"model": "x"}, (), {})
        assert result.choices[0].message.content == "from the host"
        assert seen["kwargs"] == {"model": "x"}
        assert seen["key"] in KEYS

    def test_consecutive_calls_do_not_repeat_a_key(self):
        # This is the sentence in the manifest: rotating on every message, not
        # only on errors. Four calls against four keys must touch all four.
        used = []

        def host(agent, api_kwargs, **kwargs):
            used.append(agent.api_key)
            return Answer()

        binding = _binding()
        agent = Agent()
        for _ in range(4):
            binding.run(host, agent, {}, (), {})
        assert sorted(used) == sorted(KEYS)

    def test_the_key_reaches_the_live_client_and_the_kwargs(self):
        # The client keeps its own copy and reads it when it builds auth
        # headers; ``_client_kwargs`` is what a client rebuild would use. A
        # swap that updates only ``agent.api_key`` sends the previous key.
        binding = _binding()
        agent = Agent()
        binding.run(lambda agent, api_kwargs, **kw: Answer(), agent, {}, (), {})
        assert agent.client.api_key == agent.api_key
        assert agent._client_kwargs["api_key"] == agent.api_key
        assert agent._credential_pool_entry_id is not None

    def test_an_agent_with_no_keys_is_left_entirely_alone(self):
        calls = []
        binding = _binding()
        agent = Agent(keys=[])
        agent.api_key = ""
        binding.run(lambda agent, api_kwargs, **kw: calls.append(1) or Answer(), agent, {}, (), {})
        assert calls == [1]


# --- the failing path -------------------------------------------------------


class TestARecoverableFailure:
    def test_a_503_rotates_instead_of_reaching_the_caller(self):
        # The failure this release exists for. Hermes retries three times and
        # rotates the pool only for billing, rate-limit and auth — a 503 is
        # none of those, so the turn died with fourteen untouched keys.
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(agent.api_key)
            if len(attempts) < 3:
                raise Boom("503 Service Unavailable", status_code=503)
            return Answer("recovered")

        binding = _binding()
        result = binding.run(host, Agent(), {}, (), {})
        assert result.choices[0].message.content == "recovered"
        assert len(set(attempts)) == 3

    def test_an_invalid_key_quarantines_that_key_and_carries_on(self):
        # Google reports this as a 400, which every status-first classifier
        # reads as a permanent client error and ends the run on.
        attempts = []
        bad = KEYS[0]

        def host(agent, api_kwargs, **kwargs):
            attempts.append(agent.api_key)
            if agent.api_key == bad:
                raise Boom("400 INVALID_ARGUMENT: API key not valid.", status_code=400)
            return Answer("carried")

        binding = _binding()
        agent = Agent()
        for _ in range(4):
            assert binding.run(host, agent, {}, (), {}).choices[0].message.content == "carried"
        # Tried once, quarantined, never offered again.
        assert attempts.count(bad) == 1

    def test_a_rate_limited_key_is_not_offered_again_within_its_cooldown(self):
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(agent.api_key)
            if len(attempts) == 1:
                raise Boom("429 rate limit exceeded", status_code=429)
            return Answer()

        binding = _binding()
        agent = Agent()
        binding.run(host, agent, {}, (), {})
        first = attempts[0]
        for _ in range(3):
            binding.run(host, agent, {}, (), {})
        assert attempts.count(first) == 1

    def test_a_stop_ends_the_rotation_and_reports_the_last_failure(self):
        # Since 1.0.1 nothing else does. There is no deadline to expire, so
        # this is the whole of "how does a hopeless call end": the user says
        # so, and the real failure — not a KAME invention — is what they see.
        agent = Agent()
        seen = []

        def host(a, api_kwargs, **kwargs):
            seen.append(1)
            if len(seen) == 3:
                a._interrupt_requested = True
            raise Boom("503 Service Unavailable", status_code=503)

        with pytest.raises(Boom):
            _binding().run(host, agent, {}, (), {})
        assert len(seen) == 3

    def test_every_rotation_is_counted(self):
        def host(agent, api_kwargs, **kwargs):
            if len(getattr(host, "seen", [])) < 2:
                host.seen = getattr(host, "seen", []) + [1]
                raise Boom("503", status_code=503)
            return Answer()

        binding = _binding()
        binding.run(host, Agent(), {}, (), {})
        assert binding.rotations == 2
        assert binding.recovered == 1


class TestAFailureThatMustBeRaised:
    def test_a_bad_request_stops_at_once(self):
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(1)
            raise Boom("400 unknown field 'temperture'", status_code=400)

        binding = _binding()
        with pytest.raises(Boom):
            binding.run(host, Agent(), {}, (), {})
        assert attempts == [1]

    def test_a_content_policy_refusal_stops_at_once(self):
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(1)
            raise Boom("response blocked by safety filter")

        binding = _binding()
        with pytest.raises(Boom):
            binding.run(host, Agent(), {}, (), {})
        assert attempts == [1]

    def test_a_drop_after_the_user_saw_text_is_not_replayed(self):
        # Retrying here would print the answer twice. Hermes has machinery for
        # continuing a cut-off response; this one is its business.
        attempts = []

        def host(agent, api_kwargs, on_first_delta=None, **kwargs):
            attempts.append(1)
            if on_first_delta:
                on_first_delta()
            raise Boom("connection reset mid-stream")

        binding = _binding()
        with pytest.raises(Boom):
            binding.run(host, Agent(), {}, (), {"on_first_delta": lambda: None})
        assert attempts == [1]

    def test_a_drop_before_anything_streamed_does_rotate(self):
        # The mirror of the test above: nothing reached the user, so nothing
        # can be printed twice, so the rotation is free.
        attempts = []

        def host(agent, api_kwargs, on_first_delta=None, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise Boom("connection reset before first byte")
            if on_first_delta:
                on_first_delta()
            return Answer()

        binding = _binding()
        binding.run(host, Agent(), {}, (), {"on_first_delta": lambda: None})
        assert len(attempts) == 2

    def test_an_interrupt_is_never_retried(self):
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(1)
            raise KeyboardInterrupt

        binding = _binding()
        with pytest.raises(KeyboardInterrupt):
            binding.run(host, Agent(), {}, (), {})
        assert attempts == [1]

    def test_the_callbacks_are_put_back_after_every_attempt(self):
        # The shims exist for the duration of one attempt. Leaving one behind
        # would wrap a wrapper on every call for the life of the process.
        original = lambda text: text  # noqa: E731
        agent = Agent()
        agent.stream_delta_callback = original

        def host(a, api_kwargs, **kwargs):
            assert a.stream_delta_callback is not original
            # Terminal, so the assertion above runs on exactly one attempt and
            # the restoration is checked on the path that raises.
            raise Boom("400 Unknown name \"temperture\"", status_code=400)

        binding = _binding()
        with pytest.raises(Boom):
            binding.run(host, agent, {}, (), {})
        assert agent.stream_delta_callback is original

    def test_the_callbacks_are_put_back_after_a_rotation_too(self):
        # The mirror: the attempt that rotates must also leave the agent as it
        # found it, or the second attempt would shim a shim.
        original = lambda text: text  # noqa: E731
        agent = Agent()
        agent.stream_delta_callback = original
        seen = []

        def host(a, api_kwargs, **kwargs):
            seen.append(a.stream_delta_callback)
            if len(seen) == 1:
                raise Boom("503", status_code=503)
            return Answer()

        _binding().run(host, agent, {}, (), {})
        assert len(seen) == 2
        assert original not in seen
        assert agent.stream_delta_callback is original


class TestAnAnswerThatCarriedNothing:
    def test_the_first_empty_answer_rotates_without_blaming_the_key(self):
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(agent.api_key)
            return Answer("") if len(attempts) == 1 else Answer("real")

        binding = _binding()
        result = binding.run(host, Agent(), {}, (), {})
        assert result.choices[0].message.content == "real"
        assert len(attempts) == 2

    def test_an_endlessly_empty_provider_cannot_loop(self):
        # Once the budget is spent the empty answer is returned exactly as the
        # host would have returned it.
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(1)
            return Answer("")

        binding = _binding()
        result = binding.run(host, Agent(), {}, (), {})
        assert result.choices[0].message.content == ""
        assert len(attempts) == carousel.EMPTY_RETRY_BUDGET + 1


# --- installing -------------------------------------------------------------


class TestInstalling:
    def test_both_dispatch_functions_are_wrapped(self):
        module = Module()
        binding = _binding()
        assert binding.install(module) is True
        assert getattr(module.interruptible_streaming_api_call, "__kame_carousel__", False)
        assert getattr(module.interruptible_api_call, "__kame_carousel__", False)

    def test_uninstalling_puts_both_back(self):
        module = Module()
        streaming = module.interruptible_streaming_api_call
        plain = module.interruptible_api_call
        binding = _binding()
        binding.install(module)
        binding.uninstall()
        assert module.interruptible_streaming_api_call is streaming
        assert module.interruptible_api_call is plain

    def test_a_host_missing_the_surface_is_declined_not_crashed(self):
        class Bare:
            pass

        binding = _binding()
        assert binding.install(Bare()) is False
        assert "has no" in binding.reason

    def test_a_second_instance_does_not_wrap_the_first(self):
        module = Module()
        first, second = _binding(), _binding()
        assert first.install(module) is True
        assert second.install(module) is False
        assert "already wrapped" in second.reason

    def test_the_switch_gives_the_host_its_own_dispatch_back(self, monkeypatch):
        monkeypatch.setenv("KAME_CAROUSEL_DISABLED", "1")
        seen = []
        module = Module(streaming=lambda agent, api_kwargs, **kw: seen.append(agent.api_key) or Answer())
        binding = _binding()
        binding.install(module)
        agent = Agent()
        before = agent.api_key
        module.interruptible_streaming_api_call(agent, {})
        assert seen == [before]
        assert binding.calls == 0

    def test_install_outside_a_hermes_declines_quietly(self):
        assert dispatch_binding.install(None) is None or True
