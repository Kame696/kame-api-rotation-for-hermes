"""1.8.2.7: timeouts, owner's policy C (2026-10-16).

A provider timeout (HTTP 408 / 499 / 504 / 524, or a client-side one) says
nothing about the key. So:

* the key is not benched and earns no strike;
* the FIRST timeout rotates to the next key at once;
* the SECOND timeout in a row hands the call to Hermes instead of asking the
  rest of the pool (13/10: 57 minutes, 294 attempts through a Google overload).

Until 1.8.2.6, 408/499/524 benched a healthy key for 20 s, 504 benched it for
the flat 5xx second, and "3 timeouts in a row" benched every key 5 s and kept
rotating. Driven through ``KameTransport.complete``, the real path.

Run:  uv run --with pytest python -m pytest tests/test_1_8_2_7_timeouts.py -q
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
PACKAGE = "kame_timeouts_1827_under_test"


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

KEYS = [f"AIzaSyTMO{i}" + "0" * 29 for i in range(4)]
IDENTITY = "gemini:gemini-test"
REQUEST = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}


class Refusal(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message}}


def timeout(status: int = 504) -> Refusal:
    # A neutral message on purpose: the status alone must be enough.
    return Refusal(status, "Error")


def throttle() -> Refusal:
    return Refusal(429, "Too Many Requests")


def answer(text: str):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


class Plan:
    """Per key: one outcome per request (an exception to raise, or answer text)."""

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.asked = []

    def run(self, key):
        self.asked.append(key)
        outcome = self.plan[key].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return answer(outcome)


class FakeCall(transport.Call):
    def __init__(self, plan, keys=KEYS):
        self.plan = plan
        self._keys = list(keys)
        self.kwargs = dict(REQUEST)
        self.provider = "gemini"
        self.identity = IDENTITY

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        def create(**request):
            return self.plan.run(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return False


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    yield t
    settings.forget()


@pytest.mark.parametrize("status", [408, 499, 504, 524])
def test_the_providers_timeout_statuses_are_timeouts_that_bench_nothing(status):
    delay, kind, got = carousel_mod.classify(timeout(status), "Error", status_code=status)
    assert (kind, delay, got) == ("timeout", 0.0, status)


def test_a_5xx_that_is_not_a_timeout_keeps_its_flat_second():
    delay, kind, _ = carousel_mod.classify(Refusal(502, "Bad Gateway"), "Bad Gateway", status_code=502)
    assert (kind, delay) == ("server", carousel_mod.SERVER_BASE_S)


@pytest.mark.parametrize("status", [408, 499, 504, 524])
def test_the_first_timeout_rotates_at_once_and_the_key_stays_healthy(fresh, status):
    plan = Plan({KEYS[0]: [timeout(status)], KEYS[1]: ["ok"]})
    result = fresh.complete(FakeCall(plan))
    assert result.choices[0].message.content == "ok"
    assert plan.asked == [KEYS[0], KEYS[1]]
    assert fresh.engine.healthy_count(IDENTITY, [KEYS[0]]) == 1


def test_the_second_timeout_in_a_row_goes_back_to_hermes(fresh):
    plan = Plan({KEYS[0]: [timeout(504)], KEYS[1]: [timeout(524)],
                 KEYS[2]: ["never asked"], KEYS[3]: ["never asked"]})
    with pytest.raises(Refusal):
        fresh.complete(FakeCall(plan))
    assert plan.asked == [KEYS[0], KEYS[1]]
    assert fresh.surfaced == 1
    for key in KEYS[:2]:
        assert fresh.engine.healthy_count(IDENTITY, [key]) == 1


def test_one_key_that_times_out_twice_is_handed_back_not_looped(fresh):
    plan = Plan({KEYS[0]: [timeout(504), timeout(504), "never"]})
    with pytest.raises(Refusal):
        fresh.complete(FakeCall(plan, keys=KEYS[:1]))
    assert plan.asked == [KEYS[0], KEYS[0]]


def test_timeouts_that_are_not_in_a_row_do_not_hand_the_call_back(fresh):
    plan = Plan({KEYS[0]: [timeout(504)], KEYS[1]: [throttle()],
                 KEYS[2]: [timeout(504)], KEYS[3]: ["ok"]})
    result = fresh.complete(FakeCall(plan))
    assert result.choices[0].message.content == "ok"
    assert plan.asked == KEYS
    assert fresh.surfaced == 0
