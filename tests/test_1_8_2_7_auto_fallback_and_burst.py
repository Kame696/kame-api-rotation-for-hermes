"""1.8.2.7, decisions/0027: the fallback list is automatic, and a pool that runs dry is
probed several keys at once.

The owner's own Hermes chain (base profile, read 2026-10-16): gemini 3.8 -> 3.7 -> 3.6
-> 3.5 -> 3-flash-preview, 14 keys each (14 projects). His question: if only the last
model still has quota, does the agent still answer quickly? These tests drive the real
``KameTransport`` with a stand-in for Hermes' side (the rule read from the live host:
a ``KamePoolResting`` moves Hermes to the next model in the list) and with refusals
shaped like the real Gemini daily refusal, each taking ``LATENCY`` seconds - the
owner's measured 0.76 s scaled down so the suite stays fast. What is compared is the
wall time with the probe burst on versus asking the keys one by one.
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
PACKAGE = "kame_auto_burst_1827_under_test"


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
hedging_mod = importlib.import_module(f"{PACKAGE}.core.hedging")
pacing_mod = importlib.import_module(f"{PACKAGE}.core.pacing")
settings = importlib.import_module(f"{PACKAGE}.settings")

MODELS = ["gemini:gemini-3.8-flash", "gemini:gemini-3.7-flash", "gemini:gemini-3.6-flash",
          "gemini:gemini-3.5-flash", "gemini:gemini-3-flash-preview"]
KEYS = {m: [f"AIzaSy{abs(hash(m)) % 997:03d}K{i:02d}" + "0" * 26 for i in range(14)] for m in MODELS}
CONFIG = "model:\n  provider: gemini\n  default: gemini-3.8-flash\nfallback_providers:\n" + "".join(
    f"  - provider: gemini\n    model: {m.split(':', 1)[1]}\n" for m in MODELS[1:])
LATENCY = 0.05


class Refusal(Exception):
    def __init__(self, model):
        message = ("You exceeded your current quota, please check your plan and billing details.\n"
                   "* Quota exceeded for metric: generativelanguage.googleapis.com/"
                   f"generate_content_free_tier_requests, limit: 20, model: {model}\n"
                   "Please retry in 6h7m43.710866981s.")
        super().__init__(message)
        self.status_code = 429
        self.body = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                "quotaDimensions": {"location": "global", "model": model}, "quotaValue": "20"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "22063s"}]}}


def answer(text):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


def chunk(text=None, finish=None):
    delta = SimpleNamespace(role=None, content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)])


class Response:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class Stream:
    def __init__(self, steps):
        self.steps, self.response = steps, Response()

    def __iter__(self):
        for step in self.steps:
            if isinstance(step, float):
                end = time.monotonic() + step
                while time.monotonic() < end:
                    if self.response.closed.is_set():
                        raise ConnectionError("response closed")
                    time.sleep(0.002)
                continue
            if isinstance(step, BaseException):
                raise step
            yield step

    def close(self):
        self.response.close()


class World:
    """The provider: which keys still have quota, and every request it received."""

    def __init__(self, alive):
        self.alive = {m: set(keys) for m, keys in alive.items()}
        self.asked = []
        self.stamps = []
        self.lock = threading.Lock()

    def rounds(self):
        """Requests that went out together count once: the round trips the user waited."""
        count, last = 0, None
        for at in sorted(self.stamps):
            if last is None or at - last > LATENCY / 2:
                count += 1
            last = at
        return count

    def ask(self, model, key):
        with self.lock:
            self.asked.append((model, key))
            self.stamps.append(time.monotonic())
        time.sleep(LATENCY)
        if key not in self.alive.get(model, ()):
            raise Refusal(model.split(":", 1)[1])
        return model


class FakeCall(transport.Call):
    def __init__(self, world, identity, stream=False):
        self.world, self.identity, self.stream_mode = world, identity, stream
        self.kwargs = {"model": identity.split(":", 1)[1], "messages": [{"role": "user", "content": "hi"}]}
        self.provider = "gemini"
        self.channel = "main"
        self.closed = False

    def keys(self):
        return list(KEYS[self.identity])

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        world, ident = self.world, self.identity

        def create(**request):
            if request.get("stream"):
                try:
                    served = world.ask(ident, key)
                except Refusal as exc:
                    return Stream([exc])
                return Stream([chunk(served), chunk(finish="stop")])
            return answer(world.ask(ident, key))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return self.closed


class Host:
    """Hermes' side: a turn walks the list; a hand-over moves to the next model."""

    def __init__(self, t, world):
        self.t, self.world = t, world
        self.cooldown = {}
        self.handovers = 0

    def turn(self, stream=False):
        started = time.monotonic()
        for model in MODELS:
            if time.time() < self.cooldown.get(model, 0.0):
                continue
            try:
                if stream:
                    out = list(self.t.stream(FakeCall(self.world, model, stream=True)))
                    text = "".join(getattr(c.choices[0].delta, "content", None) or ""
                                   for c in out if getattr(c, "choices", None))
                else:
                    text = self.t.complete(FakeCall(self.world, model)).choices[0].message.content
            except transport.KamePoolResting as exc:
                self.handovers += 1
                self.cooldown[model] = exc.body["error"]["reset_at"]
                continue
            return text, time.monotonic() - started
        raise AssertionError("every model was skipped")


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    hedging_mod.HEDGE_TRACKER.clear()
    pacing_mod.PACER.clear()
    yield tmp_path
    settings.forget()
    chain_mod.forget()
    hedging_mod.HEDGE_TRACKER.clear()
    pacing_mod.PACER.clear()


def owner_scenario():
    """Only the last model of the list still has quota, on every key."""
    return World({MODELS[-1]: set(KEYS[MODELS[-1]])})


# -- the fallback list is automatic ----------------------------------------------
def test_the_switch_is_off_by_default_so_a_list_is_used_without_asking(fresh):
    assert settings.is_on(settings.NO_MODEL_FALLBACK) is False
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    for key in KEYS[MODELS[0]]:
        t.engine.mark(MODELS[0], key, False, 3600.0, "daily")
    call = FakeCall(owner_scenario(), MODELS[0])
    assert isinstance(t._maybe_hand_over(call, MODELS[0], KEYS[MODELS[0]], 0.0, False), transport.KamePoolResting)


def test_with_no_list_nothing_is_handed_over(fresh):
    (fresh / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-3.8-flash\n", encoding="utf-8")
    chain_mod.forget()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    for key in KEYS[MODELS[0]]:
        t.engine.mark(MODELS[0], key, False, 3600.0, "daily")
    assert t._maybe_hand_over(FakeCall(owner_scenario(), MODELS[0]), MODELS[0], KEYS[MODELS[0]], 0.0, False) is None


def test_ignore_my_list_still_keeps_kame_waiting(fresh, monkeypatch):
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    for key in KEYS[MODELS[0]]:
        t.engine.mark(MODELS[0], key, False, 3600.0, "daily")
    assert t._maybe_hand_over(FakeCall(owner_scenario(), MODELS[0]), MODELS[0], KEYS[MODELS[0]], 0.0, False) is None


def test_the_panel_hides_the_switch_among_the_advanced_settings():
    row = settings.describe(settings.NO_MODEL_FALLBACK)
    assert row["advanced"] is True
    assert settings.describe(settings.FAILOVER_AFTER)["advanced"] is False


def test_a_call_on_the_next_model_proves_hermes_switched_and_clears_the_memo(fresh):
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    for key in KEYS[MODELS[0]]:
        t.engine.mark(MODELS[0], key, False, 3600.0, "daily")
    world = World({MODELS[1]: set(KEYS[MODELS[1]])})
    call = FakeCall(world, MODELS[0])
    assert t._maybe_hand_over(call, MODELS[0], KEYS[MODELS[0]], 0.0, False) is not None
    assert t._maybe_hand_over(call, MODELS[0], KEYS[MODELS[0]], 0.0, False) is None  # the memo
    t.complete(FakeCall(world, MODELS[1]))  # Hermes really switched
    assert t._maybe_hand_over(call, MODELS[0], KEYS[MODELS[0]], 0.0, False) is not None


# -- the probe burst ---------------------------------------------------------------
def test_a_dry_pool_is_probed_four_then_eight_keys_at_once(fresh):
    model = MODELS[0]
    world = World({model: {KEYS[model][-1]}})  # only the last key still has quota
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    out = list(t.stream(FakeCall(world, model, stream=True)))
    text = "".join(getattr(c.choices[0].delta, "content", None) or "" for c in out if getattr(c, "choices", None))
    assert text == model
    assert t.probe_bursts == 2
    # a chat: 2 alone, then 4 at once, then 8 at once: 4 round trips instead of 14
    assert world.rounds() == 4, world.rounds()


def test_a_call_that_does_not_stream_asks_at_most_four_at_once(fresh):
    """Review M1: a losing leg of a non-streamed call cannot be cut, so it is capped."""
    model = MODELS[0]
    world = World({model: {KEYS[model][-1]}})
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    assert t.complete(FakeCall(world, model)).choices[0].message.content == model
    assert world.rounds() == 5, world.rounds()  # 2 alone, then 4, 4, 4

def test_one_by_one_when_the_burst_is_off(fresh, monkeypatch):
    monkeypatch.setenv("KAME_PROBE_BURST", "0")
    settings.forget()
    model = MODELS[0]
    world = World({model: {KEYS[model][-1]}})
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    t.complete(FakeCall(world, model))
    assert t.probe_bursts == 0 and len(world.asked) == 14


def test_no_burst_while_keys_answer(fresh):
    model = MODELS[0]
    world = World({model: set(KEYS[model])})
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    t.complete(FakeCall(world, model))
    assert t.probe_bursts == 0 and len(world.asked) == 1


def test_no_burst_when_backups_are_off(fresh, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    model = MODELS[0]
    world = World({model: {KEYS[model][-1]}})
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    t.complete(FakeCall(world, model))
    assert t.probe_bursts == 0


def test_every_refused_leg_of_a_burst_is_rested_on_its_own_key(fresh):
    model = MODELS[0]
    world = World({})  # nothing left anywhere on this model
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    (fresh / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-3.8-flash\n", encoding="utf-8")
    chain_mod.forget()
    t._wait_for_recovery = lambda *a, **k: iter(())  # stop at the wait: the test is the probing
    with pytest.raises(BaseException):
        t.complete(FakeCall(world, model))
    asked = [k for m, k in world.asked]
    assert sorted(set(asked)) == sorted(KEYS[model]) and len(asked) == 14  # each key once
    assert t.engine.healthy_count(model, KEYS[model]) == 0


def test_the_burst_works_on_a_streamed_chat(fresh):
    model = MODELS[0]
    world = World({model: {KEYS[model][-1]}})
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    out = list(t.stream(FakeCall(world, model, stream=True)))
    text = "".join(getattr(c.choices[0].delta, "content", None) or "" for c in out if getattr(c, "choices", None))
    assert text == model and t.probe_bursts >= 1


# -- the owner's question, end to end ------------------------------------------------
@pytest.mark.parametrize("stream", [False, True])
def test_only_the_last_model_has_quota_and_the_agent_still_answers_fast(fresh, monkeypatch, stream):
    timings, rounds = {}, {}
    for burst in ("0", "8"):
        monkeypatch.setenv("KAME_PROBE_BURST", burst)
        settings.forget()
        hedging_mod.HEDGE_TRACKER.clear()
        pacing_mod.PACER.clear()
        world = owner_scenario()
        host = Host(transport.KameTransport(engine=carousel_mod.Carousel()), world)
        served, took = host.turn(stream=stream)
        assert served == MODELS[-1] and host.handovers == 4
        assert len({(m, k) for m, k in world.asked if m != MODELS[-1]}) == 56  # every spent key learned once
        timings[burst] = took
        rounds[burst] = world.rounds()
        # the next turn: everything is known, the answer comes at once - one request, one round trip
        world.stamps.clear()
        world.asked.clear()
        served2, _ = host.turn(stream=stream)
        assert served2 == MODELS[-1] and world.rounds() == 1 and len(world.asked) == 1
    # one by one: 4 models x 14 keys = 56 round trips; the burst: ~4 per model in a
    # chat, ~5 in a call that does not stream (at most 4 at once there)
    assert timings["0"] > LATENCY * 50
    # The claim is "far fewer round trips the user waits through". Wall-clock ratios
    # (2.5-2.8x measured alone) fell to 1.7x when the whole suite loaded the machine
    # (2026-10-18), so the round trips themselves are counted.
    assert rounds["8"] * 2.5 < rounds["0"], rounds


def test_the_hourly_recheck_asks_every_spent_key_at_once(fresh):
    """The owner's hour: every key's rest (capped at 1 h) has run out, the moment
    Google itself named (6 h) has not. One round per spent model, not fourteen."""
    world = owner_scenario()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    host = Host(t, world)
    assert host.turn()[0] == MODELS[-1]
    for model in MODELS[:-1]:
        for key in KEYS[model]:
            t.engine._pools[model][key]["sick_until"] = time.time() - 1
    host.cooldown.clear()
    world.stamps.clear()
    world.asked.clear()
    served, _ = host.turn()
    assert served == MODELS[-1]
    assert world.rounds() == 5, world.rounds()  # 4 spent models at once each, then the answer
    assert len(world.asked) == 4 * 14 + 1


def test_a_key_that_answered_is_never_counted_as_spent(fresh):
    model = MODELS[0]
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    world = World({})
    (fresh / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-3.8-flash\n", encoding="utf-8")
    chain_mod.forget()
    t._wait_for_recovery = lambda *a, **k: iter(())
    with pytest.raises(BaseException):
        t.complete(FakeCall(world, model))
    for key in KEYS[model]:
        t.engine._pools[model][key]["sick_until"] = time.time() - 1
    assert t._known_dry(model, KEYS[model]) == 14
    t.engine.mark(model, KEYS[model][0], True)
    assert t._known_dry(model, KEYS[model]) == 13


class Forbidden(Exception):
    status_code = 403

    def __init__(self):
        super().__init__("Forbidden")
        self.body = {"error": {"message": "Forbidden"}}


def test_every_key_refusing_the_same_way_still_ends_the_turn_when_keys_are_probed_together(fresh):
    """Review H1 (1.8.2.7) on a 14-key pool: the unanimity proof must count the legs of a burst."""
    model = MODELS[0]
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    asked = []

    class Call403(FakeCall):
        def client_for(self, key, attempt):
            def create(**request):
                asked.append(key)
                time.sleep(LATENCY)
                raise Forbidden()
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    t._wait_for_recovery = lambda *a, **k: iter(())
    with pytest.raises(Forbidden):
        t.complete(Call403(World({}), model))
    assert sorted(set(asked)) == sorted(KEYS[model]) and len(asked) == 14


def test_a_known_dry_probe_never_adds_a_key_that_still_has_quota(fresh):
    """Review H2: 2 keys known dry, 12 with quota -> one request, not fourteen."""
    model = MODELS[0]
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    now = time.time()
    for key in KEYS[model][:2]:
        t.engine.mark(model, key, False, 3600.0, "daily")
        t.engine._pools[model][key]["sick_until"] = now - 1
        t._stated_back[(model, key)] = now + 6 * 3600
    world = World({model: set(KEYS[model][2:])})
    t.complete(FakeCall(world, model))
    with_quota = [k for _m, k in world.asked if k in KEYS[model][2:]]
    assert len(with_quota) == 1, world.asked  # the dry pair together, then one key that answers
    assert len(world.asked) <= 3


def test_a_second_chat_is_never_held_back_by_another_chats_hand_over(fresh):
    """Review H1: the memo is per conversation."""
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    for key in KEYS[MODELS[0]]:
        t.engine.mark(MODELS[0], key, False, 3600.0, "daily")
    first, second = FakeCall(owner_scenario(), MODELS[0]), FakeCall(owner_scenario(), MODELS[0])
    assert t._maybe_hand_over(first, MODELS[0], KEYS[MODELS[0]], 0.0, False) is not None
    assert t._maybe_hand_over(second, MODELS[0], KEYS[MODELS[0]], 0.0, False) is not None


def test_background_jobs_never_probe_several_keys(fresh):
    model = MODELS[0]
    world = World({model: {KEYS[model][-1]}})
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    call = FakeCall(world, model)
    call.channel = "secondary"
    t.complete(call)
    assert t.probe_bursts == 0


def test_the_panel_warns_when_hermes_cannot_reach_the_whole_list(fresh):
    """Live on the owner's gateway (2026-10-17): Hermes ends a message with "No reply"
    after agent.api_max_retries model switches (default 3); his list has 4."""
    state_mod = importlib.import_module(f"{PACKAGE}.state")
    view = state_mod._fallback_view([])
    assert view["switch_limit"] == 3 and "api_max_retries: 5" in view["warning"]
    (fresh / "config.yaml").write_text(CONFIG + "agent:\n  api_max_retries: 5\n", encoding="utf-8")
    chain_mod.forget()
    view = state_mod._fallback_view([])
    assert view["switch_limit"] == 5 and view["warning"] == ""


def test_the_one_line_model_form_still_finds_the_primary(fresh):
    """Review L5: ``model: name`` beside ``provider: x`` (the legacy form)."""
    (fresh / "config.yaml").write_text(
        "provider: gemini\nmodel: gemini-3.8-flash\nfallback_providers:\n"
        "  - provider: gemini\n    model: gemini-3.7-flash\n", encoding="utf-8")
    chain_mod.forget()
    assert chain_mod.later_entries("gemini:gemini-3.8-flash") == ["gemini:gemini-3.7-flash"]


def test_the_burst_works_on_the_messages_wire(fresh):
    """The Anthropic Messages wire (Claude, MiniMax, Kimi coding) races the same way."""
    pytest.importorskip("anthropic")
    importlib.import_module("anthropic.lib.streaming._messages")
    wires = importlib.import_module(f"{PACKAGE}.wires")
    model = MODELS[0]
    alive = KEYS[model][-1]
    asked = []

    def events(text):
        return [{"type": "message_start", "message": {"id": "m", "model": "claude", "content": []}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"}]

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
            self.response = response

        def client_for(self, key, attempt):
            def stream(**request):
                asked.append(key)
                time.sleep(LATENCY)
                if key != alive:
                    return Manager(Stream([Refusal(model.split(":", 1)[1])]))
                return Manager(Stream(events("claude-ok")))
            return SimpleNamespace(messages=SimpleNamespace(stream=stream))

    t = transport.KameTransport(engine=carousel_mod.Carousel())
    out = list(t.stream(MessagesCall(World({}), model, stream=True)))
    joined = "".join(str(e.get("delta", {}).get("text", "")) for e in out
                     if isinstance(e, dict) and e.get("type") == "content_block_delta")
    assert "claude-ok" in joined and t.probe_bursts >= 1 and len(asked) == 14
