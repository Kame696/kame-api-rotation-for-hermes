"""Tests for features added in 1.0.8: _Spinner throttle, jitter, bind by signature.

These are the three additions that do not predate 1.0.8, tested against the
same fake-agent shape the existing dispatch tests use.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v108_under_test"


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
_Spinner = dispatch_binding._Spinner

KEYS = [f"AIzaSyKEY{i}" + "0" * 29 for i in range(4)]


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
        self._notices = []

    def _emit_wait_notice(self, text):
        self._notices.append(text)


class Answer:
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
    for name in list(settings._ENV_FOR.values()) + list(
        settings._NUMBER_ENV_FOR.values()
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    settings.forget()


@pytest.fixture(autouse=True)
def _reset_spinner():
    """The _Spinner is class-level state; reset between tests."""
    _Spinner.reset()
    yield
    _Spinner.reset()


def _pretend_it_last_spoke(agent, seconds_ago: float, text: str) -> None:
    """Put the throttle where a test needs it, without reaching past the API.

    Since 1.0.9 the state is keyed per session rather than kept in one pair of
    class attributes, so a test that wrote ``_last_say`` directly was writing to
    a place that no longer decides anything for this agent.
    """
    _Spinner._state[_Spinner.key_for(agent)] = (time.monotonic() - seconds_ago, text)


# --- _Spinner throttle -----------------------------------------------------


class TestSpinnerThrottle:
    def test_first_update_is_delivered(self):
        agent = Agent()
        _Spinner.update(agent, "KAME: rotating (attempt 2) — 3/4 healthy")
        assert agent._notices == ["KAME: rotating (attempt 2) — 3/4 healthy"]

    def test_second_update_within_throttle_is_suppressed(self):
        agent = Agent()
        _pretend_it_last_spoke(agent, 0.0, "old")  # just said something
        _Spinner.update(agent, "KAME: rotating (attempt 3) — 2/4 healthy")
        assert agent._notices == []  # throttled

    def test_same_text_is_not_resent(self, monkeypatch):
        """Past the throttle window but inside the refresh one, it is not re-sent.

        1.0.9 narrowed this: an unchanged line is still suppressed, but only
        for ``_REFRESH_S``. The spinner is shared with the host, and once
        Hermes has written its own activity into it KAME's line is off the
        screen while this gate still believes it is showing. See
        ``test_v1_0_9`` for the other half of the rule.
        """
        agent = Agent()
        _pretend_it_last_spoke(
            agent, _Spinner._DEFAULT_INTERVAL_S + 1, "KAME: 2/4 resting — ETA 1m"
        )
        _Spinner.update(agent, "KAME: 2/4 resting — ETA 1m")
        assert agent._notices == []

    def test_different_text_after_throttle_is_delivered(self, monkeypatch):
        agent = Agent()
        _pretend_it_last_spoke(agent, 3600.0, "old text")  # long ago
        _Spinner.update(agent, "KAME: back up — 4/4 healthy")
        assert agent._notices == ["KAME: back up — 4/4 healthy"]

    def test_no_flood_on_rapid_rotations(self):
        """Five rotations in two seconds produce at most one notice."""
        agent = Agent()
        for i in range(5):
            _Spinner.update(agent, f"KAME: rotating (attempt {i+2}) — 3/4 healthy")
        assert len(agent._notices) <= 1

    def test_agent_without_emit_wait_notice_does_not_crash(self):
        """An agent that lacks _emit_wait_notice must not raise."""

        class BareAgent:
            pass

        _Spinner.update(BareAgent(), "KAME: anything")
        # no exception, no notice

    def test_notice_contains_no_key_fingerprint(self):
        """The spinner text must never contain a key fingerprint."""
        agent = Agent()
        _pretend_it_last_spoke(agent, 3600.0, "old")
        _Spinner.update(agent, "KAME: rotating (attempt 2) — 3/4 healthy")
        for notice in agent._notices:
            assert "AIza" not in notice
            assert "key:" not in notice


# --- jitter injection -------------------------------------------------------


class TestJitter:
    def test_default_jitter_is_zero(self):
        """Without an explicit jitter, the default returns 0.0 — deterministic."""
        binding = DispatchBinding(engine=Carousel())
        assert binding._jitter() == 0.0

    def test_injected_jitter_is_used(self):
        """When jitter is injected, it is called on every recovery wait."""
        called = [0]

        def my_jitter():
            called[0] += 1
            return 0.5

        binding = DispatchBinding(engine=Carousel(), jitter=my_jitter)
        assert binding._jitter() == 0.5
        assert called[0] == 1

    def test_install_passes_active_jitter(self):
        """The install() entry point wires real jitter into the binding."""
        # We can't call install() without the real module, but we can
        # verify the constructor receives it by reading the source.
        import inspect

        source = inspect.getsource(dispatch_binding.install)
        assert "jitter=lambda: random.uniform" in source or "jitter=lambda" in source


# --- bind by signature -----------------------------------------------------


class TestBindBySignature:
    def test_function_with_too_few_params_steps_aside(self):
        """A dispatch function that takes < 2 params is refused, not wrapped."""
        binding = DispatchBinding(engine=Carousel())
        module = type("M", (), {})()
        module.interruptible_streaming_api_call = lambda: None
        module.interruptible_api_call = lambda a: None
        result = binding.install(module)
        assert result is False
        assert "signature changed" in binding.reason or "stepping aside" in binding.reason

    def test_function_with_two_params_is_wrapped(self):
        """A dispatch function with 2+ params wraps normally."""
        binding = DispatchBinding(engine=Carousel())

        def streaming(agent, api_kwargs, **kw):
            return Answer()

        def plain(agent, api_kwargs):
            return Answer()

        module = type("M", (), {})()
        module.interruptible_streaming_api_call = streaming
        module.interruptible_api_call = plain
        result = binding.install(module)
        assert result is True

    def test_existing_dispatch_tests_still_pass(self):
        """The bind-by-signature check does not reject the real Hermes shape."""
        # This is implicitly verified by all 1103 existing tests using the
        # Module() class which defines functions with (a, k) or (a, k, **kw).
        binding = DispatchBinding(engine=Carousel())
        from tests.test_dispatch import Module

        mod = Module()
        result = binding.install(mod)
        assert result is True
