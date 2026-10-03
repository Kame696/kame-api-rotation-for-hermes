"""Adversarial ownership checks, independent of the real-manager fixture gate."""
import asyncio
import contextvars
import importlib
import importlib.util
import inspect
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAME = "kame_profile_1818_under_test"
spec = importlib.util.spec_from_file_location(NAME, ROOT / "hermes-kame-api-rotation/__init__.py",
    submodule_search_locations=[str(ROOT / "hermes-kame-api-rotation")])
plugin = importlib.util.module_from_spec(spec)
sys.modules[NAME] = plugin
spec.loader.exec_module(plugin)
scope = importlib.import_module(NAME + ".scope")


@pytest.fixture
def homes(monkeypatch):
    current = contextvars.ContextVar("fixture_home", default="base")
    monkeypatch.setitem(sys.modules, "hermes_constants", NS(get_hermes_home=current.get))
    return current


def lease(homes, home, target, name="call"):
    token = homes.set(home)
    try:
        return scope.Patches()
    finally:
        homes.reset(token)


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1), (1, 2, 0)])
def test_three_homes_unload_and_reload_without_stack(homes, order):
    def original(value=0):
        return "host", value
    target = NS(call=original)
    patches = []
    for h in ("base", "k", "lo1"):
        p = lease(homes, h, target)
        assert p.original(target, "call") is original
        p.bind(target, "call", lambda value=0, h=h: (h, value))
        patches.append(p)
    router = target.call
    for h in ("base", "k", "lo1", "outside"):
        homes.set(h)
        assert target.call(37) == ("host" if h == "outside" else h, 37)
    for i in order:
        patches[i].release()
        patches[i].release()
        h = ("base", "k", "lo1")[i]
        homes.set(h)
        assert target.call(5) == ("host", 5)
        for j in range(3):
            if j not in order[:order.index(i)+1]:
                homes.set(("base", "k", "lo1")[j])
                assert target.call(5)[0] == ("base", "k", "lo1")[j]
    assert target.call is original
    homes.set("k")
    p = scope.Patches()
    p.bind(target, "call", lambda value=0: ("reload", value))
    assert target.call() == ("reload", 0)
    p.release()
    assert router(9) == ("host", 9)  # a cached old reference is inert


def test_foreign_replacement_is_never_unwrapped(homes):
    target = NS(call=lambda: "host")
    p = lease(homes, "base", target)
    p.bind(target, "call", lambda: "plugin")
    foreign = lambda: "foreign"
    target.call = foreign
    p.release()
    assert target.call is foreign


def test_inherited_method_and_async_signature_are_restored(homes):
    class Parent:
        async def call(self, value=4):
            return value
    class Child(Parent):
        pass
    homes.set("base")
    p = scope.Patches()
    original = p.original(Child, "call")
    async def handler(self, value=4):
        return await original(self, value) + 1
    p.bind(Child, "call", handler)
    assert inspect.iscoroutinefunction(Child.call)
    assert inspect.signature(Child.call) == inspect.signature(original)
    assert asyncio.run(Child().call()) == 5
    p.release()
    assert "call" not in vars(Child)
    assert asyncio.run(Child().call()) == 4


def test_network_callback_does_not_hold_registry_lock(homes):
    target = NS(call=lambda: "host")
    entered, release = threading.Event(), threading.Event()
    def delayed():
        entered.set()
        assert release.wait(2)
        return "done"
    p = lease(homes, "base", target)
    p.bind(target, "call", delayed)
    homes.set("base")
    context = contextvars.copy_context()
    result = []
    worker = threading.Thread(target=context.run, args=(lambda: result.append(target.call()),))
    worker.start()
    assert entered.wait(1)
    p.release()  # must finish while host handler is still running
    release.set()
    worker.join(2)
    assert result == ["done"]
    assert target.call() == "host"


def test_partial_registration_failure_releases_all_leases(homes):
    target = NS(call=lambda: "host")
    cleanup = []
    namespace = {"scope": scope, "target": target, "_cleanup_runtime": lambda: cleanup.append(True)}
    exec("@scope.lifecycle\ndef register(ctx):\n p = scope.Patches()\n p.bind(target, 'call', lambda: 'plugin')\n raise ValueError('fixture failure')", namespace)
    unload = []
    ctx = NS(on_unload=unload.append)
    with pytest.raises(ValueError, match="fixture failure"):
        namespace["register"](ctx)
    assert target.call() == "host"
    assert cleanup == [True]
    unload[0]()
    assert cleanup == [True]


def test_repeated_registration_and_abandonment(homes):
    target = NS(call=lambda: "host")
    starts, stops = [], []
    namespace = {"scope": scope, "target": target, "starts": starts,
                 "_cleanup_runtime": lambda: stops.append(True)}
    exec("@scope.lifecycle\ndef register(ctx):\n starts.append(True)\n p = scope.Patches()\n p.bind(target, 'call', lambda: 'plugin')", namespace)
    unload = []
    ctx = NS(on_unload=unload.append)
    namespace["register"](ctx)
    namespace["register"](ctx)
    assert starts == [True]
    unload[0]()
    assert target.call() == "host"
    namespace["register"](ctx)
    assert starts == [True, True]
    unload[-1]()
    assert stops == [True, True]
    # A host which abandons registration immediately must prevent later patches.
    with pytest.raises(RuntimeError, match="unloaded"):
        namespace["register"](NS(on_unload=lambda callback: callback()))
    assert target.call() == "host"


def test_concurrent_profile_dispatches_do_not_mix_handlers(homes):
    target = NS(call=lambda: "host")
    owners = []
    for h in ("base", "k", "lo1"):
        p = lease(homes, h, target)
        p.bind(target, "call", lambda h=h: h)
        owners.append(p)
    failures = []
    def run(h):
        homes.set(h)
        for _ in range(1000):
            if target.call() != h:
                failures.append(h)
    workers = [threading.Thread(target=run, args=(h,)) for h in ("base", "k", "lo1")]
    for w in workers: w.start()
    for w in workers: w.join()
    for p in owners: p.release()
    assert failures == []


def test_legacy_context_replaces_only_its_own_registration(homes):
    target = NS(call=lambda: "host")
    stops = []
    namespace = {"scope": scope, "target": target,
                 "_cleanup_runtime": lambda: stops.append(True)}
    exec("@scope.lifecycle\ndef register(ctx):\n p = scope.Patches()\n p.bind(target, 'call', lambda: ctx.answer)", namespace)
    namespace["register"](NS(answer="first"))
    assert target.call() == "first"
    namespace["register"](NS(answer="replacement"))
    assert target.call() == "replacement"
    assert stops == [True]
    # Explicit cleanup to avoid keeping this synthetic legacy owner alive.
    entry = scope._shared.entries[(id(target), "call")]
    entry["handlers"][scope.home()][0].session.close()
    assert target.call() == "host"
