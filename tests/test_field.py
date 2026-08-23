"""The Settings key field: several keys typed where the host expects one.

The pool reads ``GOOGLE_API_KEY=k1,k2,k3`` as three credentials. Getting that
value onto disk from the desktop means passing the check the SPA runs before
it saves, and that check sends the whole field to the provider as one key —
so the shape the pool exists to read is the shape the field refuses.

These tests pin what the wrapper must get right: leave a single key alone,
probe a list key by key, let one working key carry the paste, refuse a paste
where nothing works, and never turn a fault of its own into a refusal.
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
PACKAGE = "kame_field_under_test"


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
field_binding = importlib.import_module(f"{PACKAGE}.field_binding")
settings = importlib.import_module(f"{PACKAGE}.settings")

FieldBinding = field_binding.FieldBinding
Incompatible = field_binding.Incompatible

GOOD = "AIzaSyGOODkey" + "0" * 26
ALSO_GOOD = "AIzaSyOTHERkey" + "0" * 25
DEAD = "AIzaSyDEADkey" + "0" * 26


class Body:
    """``EnvVarUpdate``, reduced to the two fields the wrapper reads and the
    copy-with-an-update every pydantic version offers under some name."""

    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value

    def model_copy(self, *, update):
        return Body(self.key, update.get("value", self.value))


class Dependant:
    def __init__(self, call):
        self.call = call


class Route:
    """One FastAPI route, reduced to what the binding touches."""

    def __init__(self, handler, path="/api/providers/validate"):
        self.path = path
        self.methods = {"POST"}
        self.endpoint = handler
        self.dependant = Dependant(handler)


class App:
    def __init__(self, routes):
        self.routes = routes


class Host:
    """The host's own validate handler, with the one behaviour that matters:
    it judges whatever string it is handed, as a single key."""

    def __init__(self, *, accepts=(GOOD,), reachable=True):
        self.accepts = set(accepts)
        self.reachable = reachable
        self.seen: list = []

    async def validate_provider_credential(self, body, request):
        self.seen.append(body.value)
        if not self.reachable:
            return {"ok": False, "reachable": False,
                    "message": "Could not reach the provider to verify the key."}
        if body.value in self.accepts:
            return {"ok": True, "reachable": True, "message": ""}
        return {"ok": False, "reachable": True,
                "message": "That API key was rejected. Double-check it and try again."}


def _install(host):
    route = Route(host.validate_provider_credential)
    binding = FieldBinding()
    assert binding.install(App([route])) is True
    return binding, route


def _ask(route, value, key="GEMINI_API_KEY"):
    return asyncio.run(route.dependant.call(body=Body(key, value), request=object()))


# -- the untouched path ----------------------------------------------------


def test_one_key_reaches_the_host_unchanged():
    host = Host()
    _, route = _install(host)

    assert _ask(route, GOOD) == {"ok": True, "reachable": True, "message": ""}
    assert host.seen == [GOOD]


def test_one_bad_key_is_still_refused():
    host = Host()
    _, route = _install(host)

    answer = _ask(route, DEAD)

    assert answer["ok"] is False
    assert answer["reachable"] is True


def test_an_empty_field_is_the_hosts_problem():
    host = Host()
    _, route = _install(host)

    _ask(route, "")

    assert host.seen == [""]


# -- the list path ---------------------------------------------------------


def test_each_key_is_probed_on_its_own():
    host = Host(accepts=(GOOD, ALSO_GOOD))
    _, route = _install(host)

    answer = _ask(route, f"{GOOD},{ALSO_GOOD}")

    assert answer["ok"] is True
    # The whole field is never sent as one key — that is the bug being fixed.
    assert sorted(host.seen) == sorted([GOOD, ALSO_GOOD])


def test_one_working_key_carries_the_paste():
    host = Host(accepts=(ALSO_GOOD,))
    _, route = _install(host)

    answer = _ask(route, f"{DEAD},{ALSO_GOOD},{DEAD}2")

    assert answer["ok"] is True
    assert answer["reachable"] is True
    # A pool with a revoked key in it is a working pool, and the user is told.
    assert "1 of 3 keys accepted, 2 rejected." == answer["message"]


def test_the_same_key_typed_twice_is_one_key():
    """The split rules dedupe, so the count is keys and not commas."""
    host = Host(accepts=(GOOD,))
    _, route = _install(host)

    answer = _ask(route, f"{GOOD},{GOOD},{DEAD}")

    assert host.seen.count(GOOD) == 1
    assert answer["message"] == "1 of 2 keys accepted, 1 rejected."


def test_a_paste_where_nothing_works_is_refused():
    host = Host(accepts=())
    _, route = _install(host)

    answer = _ask(route, f"{DEAD},{DEAD}2")

    assert answer["ok"] is False
    assert answer["reachable"] is True
    assert "None of all 2 keys" in answer["message"]
    # The provider's own words survive the summary.
    assert "rejected" in answer["message"]


def test_all_accepted_says_so():
    host = Host(accepts=(GOOD, ALSO_GOOD))
    _, route = _install(host)

    answer = _ask(route, f"{GOOD},{ALSO_GOOD}")

    assert answer["message"] == "All 2 keys accepted."


def test_separators_follow_the_import_rules():
    """Whatever ``/kame-keys add`` accepts, the field accepts."""
    host = Host(accepts=(GOOD, ALSO_GOOD))
    _, route = _install(host)

    _ask(route, f"{GOOD}; {ALSO_GOOD}")

    assert sorted(host.seen) == sorted([GOOD, ALSO_GOOD])


# -- the answers that are not verdicts -------------------------------------


def test_an_unreachable_probe_keeps_the_hosts_answer():
    """No probe configured, or no network: the host's answer is the answer.

    ``reachable=False`` is how the host says *I could not check this*, and the
    SPA has its own handling for it. Turning that into a verdict here would
    invent a decision nobody asked for.
    """
    host = Host(reachable=False)
    _, route = _install(host)

    answer = _ask(route, f"{GOOD},{ALSO_GOOD}")

    assert answer["reachable"] is False
    assert answer["ok"] is False
    assert "Could not reach" in answer["message"]


def test_a_no_probe_variable_passes_through_as_ok():
    """The variable with no probe answers ok/unreachable for one key. A list
    must get exactly that answer too, not a stricter one."""

    class NoProbeHost(Host):
        async def validate_provider_credential(self, body, request):
            self.seen.append(body.value)
            return {"ok": True, "reachable": False, "message": ""}

    host = NoProbeHost()
    _, route = _install(host)

    answer = _ask(route, f"{GOOD},{ALSO_GOOD}", key="GOOGLE_API_KEY")

    assert answer == {"ok": True, "reachable": False, "message": ""}


# -- failing safely --------------------------------------------------------


def test_a_body_that_cannot_be_copied_falls_back_to_the_host():
    class Uncopyable(Body):
        def model_copy(self, *, update):
            raise RuntimeError("no")

        copy = model_copy

    host = Host(accepts=(GOOD,))
    _, route = _install(host)

    answer = asyncio.run(
        route.dependant.call(
            body=Uncopyable("GEMINI_API_KEY", f"{GOOD},{ALSO_GOOD}"), request=object()
        )
    )

    # Every per-key probe failed to even start, so the host judged the field
    # exactly as it would have without this plugin.
    assert host.seen == [f"{GOOD},{ALSO_GOOD}"]
    assert answer["ok"] is False


def test_a_probe_that_raises_does_not_decide_the_paste():
    class HalfBrokenHost(Host):
        async def validate_provider_credential(self, body, request):
            self.seen.append(body.value)
            if body.value == DEAD:
                raise RuntimeError("probe exploded")
            return {"ok": True, "reachable": True, "message": ""}

    host = HalfBrokenHost()
    _, route = _install(host)

    answer = _ask(route, f"{DEAD},{GOOD}")

    assert answer["ok"] is True


def test_only_the_first_keys_are_probed_and_the_answer_says_so():
    keys = [f"AIzaSyBULK{i:029d}" for i in range(40)]
    host = Host(accepts=())
    _, route = _install(host)

    answer = _ask(route, ",".join(keys))

    assert len(host.seen) == field_binding._MAX_PROBED
    assert f"the first {field_binding._MAX_PROBED} of 40" in answer["message"]


# -- installation ----------------------------------------------------------


def test_it_declines_a_route_it_does_not_recognise():
    async def wrong_shape(payload):  # no body/request
        return {}

    binding = FieldBinding()
    assert binding.install(App([Route(wrong_shape)])) is False
    assert "missing body" in binding.reason


def test_it_declines_when_there_is_no_such_route():
    binding = FieldBinding()
    assert binding.install(App([])) is False
    assert "no POST" in binding.reason


def test_it_declines_a_synchronous_handler():
    def sync_handler(body, request):
        return {}

    binding = FieldBinding()
    assert binding.install(App([Route(sync_handler)])) is False
    assert "not async" in binding.reason


def test_a_second_install_is_refused_not_stacked():
    host = Host()
    _, route = _install(host)

    second = FieldBinding()
    assert second.install(App([route])) is False
    assert "already wrapped" in second.reason


def test_uninstall_puts_the_host_back():
    host = Host()
    route = Route(host.validate_provider_credential)
    # Captured from the route rather than re-read off the host: a bound method
    # is a new object on every attribute access, so ``is`` would never hold.
    original = route.dependant.call
    binding = FieldBinding()
    assert binding.install(App([route])) is True

    binding.uninstall()

    assert route.dependant.call is original
    assert route.endpoint is original
    assert binding.installed is False


def test_the_switch_keeps_it_out():
    settings.forget()
    try:
        assert field_binding.install(App([Route(Host().validate_provider_credential)])) is not None
        monkey = pytest.MonkeyPatch()
        monkey.setenv("KAME_FIELD_PROBE_DISABLED", "1")
        try:
            assert field_binding.install(App([Route(Host().validate_provider_credential)])) is None
        finally:
            monkey.undo()
    finally:
        settings.forget()


def test_the_kill_switch_keeps_it_out_too():
    monkey = pytest.MonkeyPatch()
    monkey.setenv("KAME_ROTATION_DISABLED", "1")
    try:
        assert field_binding.install(App([Route(Host().validate_provider_credential)])) is None
    finally:
        monkey.undo()


def test_no_gateway_in_this_process_is_not_a_failure():
    saved = sys.modules.pop("hermes_cli.web_server", None)
    try:
        assert field_binding.install() is None
    finally:
        if saved is not None:
            sys.modules["hermes_cli.web_server"] = saved
