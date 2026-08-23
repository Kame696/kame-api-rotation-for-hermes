"""Auxiliary calls: announcing the model of a request that fires no hooks.

Hermes' auxiliary lane runs summarisation, titling and compression on a
smaller model over the *same* credentials as the conversation, and it fires
no hooks at all. Left unannounced it is the lane most damaged by a
provider-wide bench: it spent nothing on the key the main model exhausted and
still loses it.

These tests pin the two things the wrapper must get right — announce for
exactly the duration of the call, and refuse to guess when the request cannot
be attributed to a pool.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_aux_under_test"


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
runtime = importlib.import_module(f"{PACKAGE}.runtime")
aux_binding = importlib.import_module(f"{PACKAGE}.aux_binding")

AuxBinding = aux_binding.AuxBinding

AUX_MODEL = "gemini-3.5-flash-lite"
MAIN_MODEL = "gemini-3.6-flash"


class FakeAuxModule:
    """``agent.auxiliary_client``, reduced to the three relays and the
    provider normaliser the wrapper consults."""

    def __init__(self) -> None:
        self.seen: list = []

    @staticmethod
    def _normalize_aux_provider(provider):
        return str(provider or "").strip().lower().replace("google", "gemini")

    def _relay_sync_completion(self, kwargs, *, provider=None, api_mode=None, create=None):
        raise NotImplementedError

    def _relay_sync_stream(self, kwargs, *, provider=None, api_mode=None):
        raise NotImplementedError

    async def _relay_async_completion(self, kwargs, *, provider=None, api_mode=None, create=None):
        raise NotImplementedError


def build_module(*, record_to: list):
    """A module namespace whose relays record what was announced to them."""

    def _relay_sync_completion(client, kwargs, *, provider=None, api_mode=None, create=None):
        record_to.append(runtime.in_flight())
        return "sync-result"

    def _relay_sync_stream(client, kwargs, *, provider=None, api_mode=None):
        record_to.append(runtime.in_flight())
        return "stream-result"

    async def _relay_async_completion(client, kwargs, *, provider=None, api_mode=None, create=None):
        record_to.append(runtime.in_flight())
        return "async-result"

    module = type("AuxModule", (), {})()
    module._relay_sync_completion = _relay_sync_completion
    module._relay_sync_stream = _relay_sync_stream
    module._relay_async_completion = _relay_async_completion
    module._normalize_aux_provider = FakeAuxModule._normalize_aux_provider
    return module


@pytest.fixture(autouse=True)
def _clean_context():
    runtime.forget_call()
    yield
    runtime.forget_call()


@pytest.fixture
def wired():
    seen: list = []
    module = build_module(record_to=seen)
    binding = AuxBinding()
    assert binding.install(module) is True
    yield binding, module, seen
    binding.uninstall()


class TestRefusingToInstall:
    def test_a_missing_relay_refuses(self):
        seen: list = []
        module = build_module(record_to=seen)
        del module._relay_sync_stream
        binding = AuxBinding()
        assert binding.install(module) is False
        assert "_relay_sync_stream" in binding.reason

    def test_a_relay_that_stopped_being_async_refuses(self):
        seen: list = []
        module = build_module(record_to=seen)

        def _sync_now(client, kwargs, *, provider=None, api_mode=None, create=None):
            return None

        module._relay_async_completion = _sync_now
        binding = AuxBinding()
        assert binding.install(module) is False
        assert "async" in binding.reason

    def test_a_relay_that_lost_its_provider_keyword_refuses(self):
        seen: list = []
        module = build_module(record_to=seen)

        def _no_provider(client, kwargs, *, api_mode=None):
            return None

        module._relay_sync_completion = _no_provider
        binding = AuxBinding()
        assert binding.install(module) is False
        assert "provider" in binding.reason

    def test_a_refusal_leaves_every_relay_untouched(self):
        seen: list = []
        module = build_module(record_to=seen)
        del module._relay_sync_stream
        before = module._relay_sync_completion
        AuxBinding().install(module)
        assert module._relay_sync_completion is before

    def test_a_second_binding_does_not_stack(self, wired):
        _binding, module, _seen = wired
        second = AuxBinding()
        assert second.install(module) is False
        assert "already wrapped" in second.reason

    def test_uninstall_restores_every_relay(self):
        seen: list = []
        module = build_module(record_to=seen)
        before = (
            module._relay_sync_completion,
            module._relay_sync_stream,
            module._relay_async_completion,
        )
        binding = AuxBinding()
        binding.install(module)
        binding.uninstall()
        after = (
            module._relay_sync_completion,
            module._relay_sync_stream,
            module._relay_async_completion,
        )
        assert after == before


class TestAnnouncing:
    def test_a_sync_call_is_announced_while_it_runs(self, wired):
        _binding, module, seen = wired
        module._relay_sync_completion(None, {"model": AUX_MODEL}, provider="gemini")
        assert seen == [("gemini", AUX_MODEL)]

    def test_a_stream_is_announced_too(self, wired):
        _binding, module, seen = wired
        module._relay_sync_stream(None, {"model": AUX_MODEL}, provider="gemini")
        assert seen == [("gemini", AUX_MODEL)]

    def test_an_async_call_is_announced(self, wired):
        _binding, module, seen = wired
        result = asyncio.run(
            module._relay_async_completion(None, {"model": AUX_MODEL}, provider="gemini")
        )
        assert result == "async-result"
        assert seen == [("gemini", AUX_MODEL)]

    def test_the_provider_is_normalised_to_the_pool_name(self, wired):
        # The pool is keyed by the host's normalised provider. Announcing the
        # raw routing label would name a scope no pool answers to, and the
        # announcement would be discarded as belonging elsewhere.
        _binding, module, seen = wired
        module._relay_sync_completion(None, {"model": AUX_MODEL}, provider="Google")
        assert seen == [("gemini", AUX_MODEL)]

    def test_the_result_is_passed_straight_back(self, wired):
        _binding, module, _seen = wired
        got = module._relay_sync_completion(None, {"model": AUX_MODEL}, provider="gemini")
        assert got == "sync-result"


class TestNotLeaking:
    def test_the_announcement_is_undone_afterwards(self, wired):
        # An auxiliary call nests inside a main turn. If its announcement
        # outlived the call, the main model's next failure would be filed
        # against the auxiliary model's quota — a bench on the wrong row.
        _binding, module, _seen = wired
        runtime.note_call("gemini", MAIN_MODEL)
        module._relay_sync_completion(None, {"model": AUX_MODEL}, provider="gemini")
        assert runtime.in_flight() == ("gemini", MAIN_MODEL)

    def test_it_is_undone_even_when_the_call_raises(self, wired):
        _binding, module, _seen = wired

        def _boom(client, kwargs, *, provider=None, api_mode=None, create=None):
            raise RuntimeError("upstream 429")

        binding = AuxBinding()
        module2 = build_module(record_to=[])
        module2._relay_sync_completion = _boom
        binding.install(module2)
        runtime.note_call("gemini", MAIN_MODEL)
        with pytest.raises(RuntimeError):
            module2._relay_sync_completion(None, {"model": AUX_MODEL}, provider="gemini")
        assert runtime.in_flight() == ("gemini", MAIN_MODEL)
        binding.uninstall()

    def test_an_async_failure_also_unwinds(self, wired):
        module2 = build_module(record_to=[])

        async def _boom(client, kwargs, *, provider=None, api_mode=None, create=None):
            raise RuntimeError("upstream 429")

        module2._relay_async_completion = _boom
        binding = AuxBinding()
        binding.install(module2)
        runtime.note_call("gemini", MAIN_MODEL)
        with pytest.raises(RuntimeError):
            asyncio.run(
                module2._relay_async_completion(None, {"model": AUX_MODEL}, provider="gemini")
            )
        assert runtime.in_flight() == ("gemini", MAIN_MODEL)
        binding.uninstall()


class TestDecliningToGuess:
    def test_a_request_without_a_model_is_left_alone(self, wired):
        _binding, module, seen = wired
        runtime.note_call("gemini", MAIN_MODEL)
        module._relay_sync_completion(None, {}, provider="gemini")
        assert seen == [("gemini", MAIN_MODEL)]

    def test_a_request_without_a_provider_is_left_alone(self, wired):
        _binding, module, seen = wired
        module._relay_sync_completion(None, {"model": AUX_MODEL})
        assert seen == [("", "")]

    def test_a_routing_label_is_not_a_pool(self, wired):
        # "auto" and "custom" are resolved by the host from the client's
        # base_url, which is not visible here. Announcing either would
        # attribute a bench to a provider that may not own the key.
        _binding, module, seen = wired
        module._relay_sync_completion(None, {"model": AUX_MODEL}, provider="auto")
        module._relay_sync_completion(None, {"model": AUX_MODEL}, provider="custom")
        assert seen == [("", ""), ("", "")]

    def test_a_hostile_kwargs_object_does_not_break_the_call(self, wired):
        _binding, module, seen = wired

        class Hostile:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("no")

        assert module._relay_sync_completion(None, Hostile(), provider="gemini") == "sync-result"

    def test_a_broken_normaliser_declines_rather_than_guesses(self, wired):
        _binding, module, seen = wired

        def _boom(_provider):
            raise RuntimeError("moved")

        module._normalize_aux_provider = _boom
        module._relay_sync_completion(None, {"model": AUX_MODEL}, provider="gemini")
        assert seen == [("", "")]
