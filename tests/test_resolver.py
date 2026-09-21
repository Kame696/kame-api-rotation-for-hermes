"""The key a turn actually carries, when the variable holds several.

Storing fifteen keys in one variable and reading them as fifteen credentials
were both already true, and the user still saw *API inválida* on the first
message. Between the stored value and the outbound request sits Hermes' own
resolution, which reads the environment variable and calls the whole thing
the key — so the comma-joined list went out as one Bearer token, and the
runtime it built carried no credential pool for the agent to rotate with.

These tests pin what the repair must get right: leave one key alone, take the
key from the pool when there is one, attach that pool, never hand back the
parent row whose value *is* the list, fall back to the first key rather than
to the list, and never let a fault of its own cost the turn.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_resolver_under_test"


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
resolver_binding = importlib.import_module(f"{PACKAGE}.resolver_binding")
settings = importlib.import_module(f"{PACKAGE}.settings")

ResolverBinding = resolver_binding.ResolverBinding
Incompatible = resolver_binding.Incompatible

# Long enough to survive the split's minimum-length floor, which is the same
# floor a real key clears and a typo does not.
ONE = "AIzaSyFIRSTkey" + "0" * 25
TWO = "AIzaSySECONDkey" + "0" * 24
THREE = "AIzaSyTHIRDkey" + "0" * 25
BLOB = f"{ONE},{TWO},{THREE}"


class Entry:
    """One pooled credential, reduced to the field the repair reads."""

    def __init__(self, key, *, via="runtime_api_key"):
        self.runtime_api_key = key if via == "runtime_api_key" else None
        self.access_token = key if via == "access_token" else ""


class Pool:
    """The credential pool, reduced to what the repair asks of it."""

    def __init__(self, offers=None, *, credentials=True):
        self._offers = list(offers or [])
        self._credentials = credentials
        self.selected = 0
        self.peeked = 0

    def has_credentials(self):
        return self._credentials

    def select(self):
        self.selected += 1
        return self._offers.pop(0) if self._offers else None

    def peek(self):
        self.peeked += 1
        return self._offers[0] if self._offers else None


class Module:
    """``hermes_cli.runtime_provider``, reduced to the one name wrapped.

    The resolver is set as an attribute holding a plain function rather than
    written as a method, because that is what it is on a module — and because
    a bound method is a new object on every attribute access, so identity
    checks against a method would never hold whatever the binding did.
    """

    def __init__(self, answer):
        self.calls = []

        def resolve_runtime_provider(*args, **kwargs):
            self.calls.append((args, kwargs))
            return dict(answer) if isinstance(answer, dict) else answer

        self.resolve_runtime_provider = resolve_runtime_provider


def _runtime(api_key=BLOB, **extra):
    runtime = {
        "provider": "gemini",
        "api_mode": "chat_completions",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": api_key,
        "source": "GOOGLE_API_KEY",
        "requested_provider": "gemini",
    }
    runtime.update(extra)
    return runtime


def _bound(binding, pool):
    """Point the binding's pool lookup at a stand-in, with no Hermes present."""
    binding._load_pool = lambda provider: pool
    return binding


class TestTheCommonCase:
    """One key is what almost everybody has, and it must cost nothing."""

    def test_one_key_is_returned_untouched(self):
        binding = _bound(ResolverBinding(), Pool([Entry(TWO)]))
        runtime = _runtime(api_key=ONE)
        assert binding.repair(runtime) is runtime
        assert binding.repaired == 0

    def test_an_empty_key_is_returned_untouched(self):
        # A provider with no credential at all is the host's error to raise,
        # with the host's own message naming the variable to set.
        binding = _bound(ResolverBinding(), Pool([Entry(TWO)]))
        runtime = _runtime(api_key="")
        assert binding.repair(runtime) is runtime

    def test_something_that_is_not_a_runtime_is_returned_untouched(self):
        binding = ResolverBinding()
        assert binding.repair(None) is None
        assert binding.repair("no") == "no"

    def test_the_pool_is_never_asked_for_a_single_key(self):
        pool = Pool([Entry(TWO)])
        binding = _bound(ResolverBinding(), pool)
        binding.repair(_runtime(api_key=ONE))
        assert pool.selected == 0


class TestTheRepair:
    def test_a_list_becomes_the_key_the_pool_chose(self):
        pool = Pool([Entry(TWO)])
        binding = _bound(ResolverBinding(), pool)
        repaired = binding.repair(_runtime())
        assert repaired["api_key"] == TWO
        assert binding.repaired == 1
        assert binding.pooled == 1

    def test_and_the_pool_comes_with_it(self):
        # Without this the agent has no pool at all for an API-key provider,
        # so a 429 on the chosen key has nowhere to rotate to — which is half
        # of what this plugin is for.
        pool = Pool([Entry(TWO)])
        binding = _bound(ResolverBinding(), pool)
        assert binding.repair(_runtime())["credential_pool"] is pool

    def test_the_host_s_own_dict_is_not_modified(self):
        # Callers hold this dict; some cache it. The repair returns a copy so
        # a later read of the original still says what the host resolved.
        pool = Pool([Entry(TWO)])
        binding = _bound(ResolverBinding(), pool)
        runtime = _runtime()
        repaired = binding.repair(runtime)
        assert runtime["api_key"] == BLOB
        assert repaired is not runtime

    def test_everything_else_is_carried_over(self):
        pool = Pool([Entry(TWO)])
        binding = _bound(ResolverBinding(), pool)
        repaired = binding.repair(_runtime(max_output_tokens=8192))
        for key in ("provider", "api_mode", "base_url", "source", "requested_provider"):
            assert repaired[key] == _runtime()[key]
        assert repaired["max_output_tokens"] == 8192

    def test_a_key_stored_as_an_access_token_is_read_too(self):
        pool = Pool([Entry(TWO, via="access_token")])
        binding = _bound(ResolverBinding(), pool)
        assert binding.repair(_runtime())["api_key"] == TWO

    def test_a_pool_the_host_already_attached_is_the_one_asked(self):
        # A provider Hermes already pool-routes must not have a second pool
        # loaded behind its back: rotating one while the agent holds the other
        # would bench keys in a pool nobody is using.
        host_pool = Pool([Entry(TWO)])
        other = Pool([Entry(THREE)])
        binding = _bound(ResolverBinding(), other)
        repaired = binding.repair(_runtime(credential_pool=host_pool))
        assert repaired["api_key"] == TWO
        assert repaired["credential_pool"] is host_pool
        assert other.selected == 0


class TestWhenThePoolCannotHelp:
    """Still one key. Never the list."""

    def test_no_pool_at_all_falls_back_to_the_first_key(self):
        binding = _bound(ResolverBinding(), None)
        repaired = binding.repair(_runtime())
        assert repaired["api_key"] == ONE
        assert "credential_pool" not in repaired
        assert binding.repaired == 1
        assert binding.pooled == 0

    def test_a_pool_with_nothing_available_falls_back_to_the_first_key(self):
        binding = _bound(ResolverBinding(), Pool([]))
        assert binding.repair(_runtime())["api_key"] == ONE

    def test_the_parent_row_is_refused(self):
        # The pool entry whose value *is* the list. It exists on any host
        # where the persist guard could not install, and handing it back
        # would reintroduce the exact bug this module removes.
        pool = Pool([Entry(BLOB)])
        binding = _bound(ResolverBinding(), pool)
        repaired = binding.repair(_runtime())
        assert repaired["api_key"] == ONE
        assert "credential_pool" not in repaired

    def test_a_pool_that_raises_falls_back_to_the_first_key(self):
        class Angry:
            def has_credentials(self):
                return True

            def select(self):
                raise RuntimeError("auth.json is unreadable")

        binding = _bound(ResolverBinding(), Angry())
        assert binding.repair(_runtime())["api_key"] == ONE

    def test_an_entry_that_raises_on_its_key_falls_back(self):
        class Landmine:
            @property
            def runtime_api_key(self):
                raise RuntimeError("decryption failed")

        binding = _bound(ResolverBinding(), Pool([Landmine()]))
        assert binding.repair(_runtime())["api_key"] == ONE

    def test_a_pool_lookup_that_raises_is_not_the_turn_s_problem(self):
        def explode(provider):
            raise RuntimeError("no such provider")

        binding = ResolverBinding()
        binding._load_pool = explode
        with pytest.raises(RuntimeError):
            binding.repair(_runtime())
        # ...but the wrapper is what the host calls, and it swallows.
        module = Module(_runtime())
        binding2 = ResolverBinding()
        assert binding2.install(module) is True
        binding2._load_pool = explode
        assert module.resolve_runtime_provider()["api_key"] == BLOB


class TestChoosing:
    """The rule on its own, with no pool and no host in the way."""

    def test_a_single_candidate_wins(self):
        assert resolver_binding.choose([ONE, TWO], TWO) == (TWO, True)

    def test_a_candidate_that_is_itself_a_list_loses(self):
        assert resolver_binding.choose([ONE, TWO], BLOB) == (ONE, False)

    def test_nothing_offered_falls_back(self):
        assert resolver_binding.choose([ONE, TWO], "") == (ONE, False)
        assert resolver_binding.choose([ONE, TWO], "   ") == (ONE, False)

    def test_a_candidate_from_outside_the_list_is_still_a_key(self):
        # A credential added with ``/kame-keys add`` lives in the pool and not
        # in the variable. It is a real key for this provider and the pool
        # offered it deliberately; refusing it would ignore the rotation.
        assert resolver_binding.choose([ONE, TWO], THREE) == (THREE, True)


class TestTheWrapper:
    def test_the_host_s_arguments_are_passed_through(self):
        module = Module(_runtime(api_key=ONE))
        binding = ResolverBinding()
        assert binding.install(module) is True
        module.resolve_runtime_provider("auto", explicit_api_key=None, target_model="x")
        assert module.calls == [(("auto",), {"explicit_api_key": None, "target_model": "x"})]

    def test_the_wrapper_repairs_what_the_host_returned(self):
        module = Module(_runtime())
        binding = ResolverBinding()
        assert binding.install(module) is True
        _bound(binding, Pool([Entry(TWO)]))
        assert module.resolve_runtime_provider()["api_key"] == TWO

    def test_installing_twice_is_one_wrapper(self):
        module = Module(_runtime(api_key=ONE))
        first = ResolverBinding()
        assert first.install(module) is True
        wrapper = module.resolve_runtime_provider

        second = ResolverBinding()
        assert second.install(module) is False
        assert "already wrapped" in second.reason
        assert module.resolve_runtime_provider is wrapper

    def test_uninstall_puts_the_host_back(self):
        module = Module(_runtime())
        original = module.resolve_runtime_provider
        binding = ResolverBinding()
        assert binding.install(module) is True
        assert module.resolve_runtime_provider is not original
        binding.uninstall()
        assert module.resolve_runtime_provider is original
        assert module.resolve_runtime_provider()["api_key"] == BLOB

    def test_uninstalling_twice_is_harmless(self):
        binding = ResolverBinding()
        binding.uninstall()
        assert binding.installed is False


class TestRefusingToInstall:
    def test_a_module_that_is_not_there(self):
        binding = ResolverBinding()
        assert binding.install(None) is False
        assert "not imported" in binding.reason

    def test_a_module_without_the_resolver(self):
        class Empty:
            pass

        binding = ResolverBinding()
        assert binding.install(Empty()) is False
        assert "resolve_runtime_provider" in binding.reason

    def test_a_resolver_that_is_not_callable(self):
        class Odd:
            resolve_runtime_provider = "not a function"

        binding = ResolverBinding()
        assert binding.install(Odd()) is False

    def test_inspect_module_names_what_is_wrong(self):
        with pytest.raises(Incompatible):
            resolver_binding.inspect_module(None)


class TestTheSwitch:
    def teardown_method(self):
        settings.forget()

    def test_the_switch_stops_the_install(self, monkeypatch):
        monkeypatch.setenv("KAME_RESOLVER_DISABLED", "1")
        assert resolver_binding.install(Module(_runtime())) is None

    def test_the_whole_plugin_being_off_stops_it_too(self, monkeypatch):
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
        assert resolver_binding.install(Module(_runtime())) is None

    def test_otherwise_it_installs(self, monkeypatch):
        monkeypatch.delenv("KAME_RESOLVER_DISABLED", raising=False)
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        module = Module(_runtime(api_key=ONE))
        binding = resolver_binding.install(module)
        assert binding is not None and binding.installed is True
        binding.uninstall()

    def test_it_is_declared_where_hermes_looks(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert f"\n  {settings.RESOLVER_DISABLED}:\n" in manifest
