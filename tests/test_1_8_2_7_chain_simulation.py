"""1.8.2.7: five models, fourteen keys each - does the agent stay fast, and does it
come back to the smartest model as soon as that is possible?

The owner's picture: gemini 3.8 -> 3.7 -> 3.6 -> 3.5 -> 3, each with a pool of 14
keys (14 projects). Hermes owns the chain; KAME owns the keys. This drives the real
``KameTransport.complete`` for each model with a small stand-in for Hermes' side:
a turn starts on the first model not in cooldown, a ``KamePoolResting`` puts that
model in cooldown until its ``reset_at`` and moves to the next one, a success ends
the turn. The host side is the behaviour read from the live source in
``notes/2026-10-16-hermes-native-fallback-study.md`` s3 (restore the primary at the
top of every turn unless its cooldown is still in the future).

Nothing sleeps: pools are rested and healed by writing the engine's own clock.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_7_chain_simulation.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_chainsim_1827_under_test"


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
chain_mod = importlib.import_module(f"{PACKAGE}.core.fallback_chain")
settings = importlib.import_module(f"{PACKAGE}.settings")

# 1.8.3.0 (D2 a): a fallback on the SAME provider is answered inside KAME and never reaches Hermes
# (tests/test_1_8_3_0_sibling_standin.py). This simulation is of Hermes' walk, so every entry is a
# different provider, the only kind KAME still hands over.
PROVIDERS = ["gemini", "groq", "cerebras", "mistral", "nvidia"]
MODELS = [f"{p}:gemini-3.{n}-flash" for p, n in zip(PROVIDERS, (8, 7, 6, 5, 4))]
KEYS = {m: [f"AIzaSy{m[-9:-6]}{i:02d}" + "0" * 24 for i in range(14)] for m in MODELS}
CONFIG = (f"model:\n  provider: {PROVIDERS[0]}\n  default: gemini-3.8-flash\nfallback_providers:\n" + "".join(
    f"  - provider: {m.split(':', 1)[0]}\n    model: {m.split(':', 1)[1]}\n" for m in MODELS[1:]))


def answer(text):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


class FakeCall(transport.Call):
    def __init__(self, identity):
        self.kwargs = {"model": identity, "messages": [{"role": "user", "content": "hi"}]}
        self.provider = identity.split(":", 1)[0]
        self.identity = identity

    def keys(self):
        return list(KEYS[self.identity])

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        ident = self.identity

        def create(**_):
            return answer(ident)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return False


@pytest.fixture()
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "0")  # the owner's opt-in
    settings.forget()
    chain_mod.forget()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    host = Host(t)
    yield host
    settings.forget()
    chain_mod.forget()


class Host:
    """Hermes' side, reduced to the rule that matters: first model not in cooldown."""

    def __init__(self, t):
        self.t = t
        self.cooldown = {}
        self.handovers = 0

    def rest(self, model, count=14, seconds=3600.0):
        for key in KEYS[model][:count]:
            self.t.engine.mark(model, key, False, seconds, "daily")

    def heal(self, model):
        for key in self.t.engine._pools.get(model, {}):
            self.t.engine._pools[model][key]["sick_until"] = time.time() - 1

    def turn(self):
        started = time.monotonic()
        for model in MODELS:
            if time.time() < self.cooldown.get(model, 0.0):
                continue
            try:
                result = self.t.complete(FakeCall(model))
            except transport.KamePoolResting as exc:
                self.handovers += 1
                self.cooldown[model] = exc.body["error"]["reset_at"]
                continue
            return result.choices[0].message.content, time.monotonic() - started
        raise AssertionError("every model was skipped: the last one would have waited")


def test_healthy_pools_use_the_smartest_model_and_hand_nothing_over(world):
    served, took = world.turn()
    assert served == MODELS[0] and world.handovers == 0 and took < 0.5


def test_one_spent_model_costs_one_hop_not_a_wait(world):
    world.rest(MODELS[0])
    served, took = world.turn()
    assert served == MODELS[1] and world.handovers == 1
    assert took < 0.5  # no key was asked, nothing slept


def test_three_spent_models_reach_the_fourth_in_three_hops(world):
    for model in MODELS[:3]:
        world.rest(model)
    served, took = world.turn()
    assert served == MODELS[3] and world.handovers == 3 and took < 0.5


def test_a_pool_with_one_healthy_key_stays_on_its_model(world):
    world.rest(MODELS[0], count=13)
    served, _ = world.turn()
    assert served == MODELS[0] and world.handovers == 0


def test_the_smartest_model_comes_back_the_moment_its_keys_do(world):
    world.rest(MODELS[0])
    assert world.turn()[0] == MODELS[1]
    # the next turn, primary still resting: Hermes' cooldown keeps it on the fallback
    assert world.turn()[0] == MODELS[1]
    world.heal(MODELS[0])
    world.cooldown[MODELS[0]] = 0.0  # the cooldown ran out (reset_at is the primary's soonest key)
    assert world.turn()[0] == MODELS[0]


def test_every_reply_starts_on_the_smartest_model_and_a_resting_one_costs_no_request(world):
    # 1.8.2.8 (owner, live test): the fallback lasts one reply. Hermes is told
    # the primary is back in moments; the next reply starts on it again, and
    # while its keys still rest KAME passes it down without asking a key.
    world.rest(MODELS[0], seconds=900.0)
    before = time.time()
    assert world.turn()[0] == MODELS[1]
    assert world.cooldown[MODELS[0]] <= before + transport._HANDOVER_HOST_BACK_S + 1
    world.cooldown[MODELS[0]] = 0.0   # Hermes restored the primary for the next reply
    served, took = world.turn()
    assert served == MODELS[1] and world.handovers == 2 and took < 0.5


def test_when_nothing_later_is_usable_the_last_model_waits_instead_of_failing(world):
    for model in MODELS:
        world.rest(model)
    # the last model has nothing after it: KAME must not hand over
    assert world.t._maybe_hand_over(FakeCall(MODELS[-1]), MODELS[-1], KEYS[MODELS[-1]], 0.0, False) is None
    # and an earlier model whose every later entry rests must not hand over either
    assert world.t._maybe_hand_over(FakeCall(MODELS[0]), MODELS[0], KEYS[MODELS[0]], 0.0, False) is None
