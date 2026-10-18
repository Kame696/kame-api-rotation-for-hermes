"""1.8.2.8 (decisions/0028): adaptive speed and switching.

The owner's case: four fallback models x 14 keys, the primary answering
"the model is overloaded" (503) for well over his 10 s switch time, and Hermes
never moved to the next model. Driven through ``KameTransport.complete``, the
real path, plus the pieces it is made of.

Run:  uv run --with pytest python -m pytest tests/test_1_8_2_8_adaptive.py -q
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
PACKAGE = "kame_adaptive_1828_under_test"


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
hedging = importlib.import_module(f"{PACKAGE}.core.hedging")
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"AIzaSyADP{i}" + "0" * 29 for i in range(4)]
PRIMARY = "gemini:gemini-3.8-flash"
REQUEST = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]}
CHAIN = """\
model:
  provider: gemini
  default: gemini-3.8-flash
fallback_providers:
  - provider: gemini
    model: gemini-3.7-flash
"""


class Refusal(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message}}


def overloaded() -> Refusal:
    return Refusal(503, "The model is overloaded. Please try again later.")


def answer(text: str):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


class Plan:
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
    def __init__(self, plan, keys=KEYS, identity=PRIMARY):
        self.plan = plan
        self._keys = list(keys)
        self.kwargs = dict(REQUEST)
        self.provider = "gemini"
        self.identity = identity

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
    chain_mod.forget()
    hedging.HEDGE_TRACKER.clear()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    hedging.HEDGE_TRACKER.clear()


@pytest.fixture()
def with_chain(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    chain_mod.forget()
    return fresh


# --- the owner's case -------------------------------------------------------


def test_an_overloaded_model_hands_over_after_two_keys_not_after_the_whole_pool(with_chain):
    plan = Plan({k: [overloaded(), overloaded(), "late"] for k in KEYS})
    with pytest.raises(transport.KamePoolResting) as caught:
        with_chain.complete(FakeCall(plan))
    assert len(plan.asked) == 2                       # two keys, then the next model
    assert len(set(plan.asked)) == 2
    assert "busy" in str(caught.value)
    # The fallback lasts one reply: Hermes is told the primary is back in moments,
    # so the next reply starts on it again; KAME's own busy window is what
    # leaves the busy model alone meanwhile (the next test).
    assert caught.value.body["error"]["reset_at"] <= time.time() + transport._HANDOVER_HOST_BACK_S + 1
    assert with_chain._busy_until[PRIMARY] >= time.time() + 25
    assert with_chain.handed_over == 1


def test_without_a_fallback_list_an_overloaded_model_keeps_rotating_one_key_at_a_time(fresh):
    plan = Plan({KEYS[0]: [overloaded()], KEYS[1]: [overloaded()], KEYS[2]: [overloaded()],
                 KEYS[3]: ["ok"]})
    result = fresh.complete(FakeCall(plan))
    assert result.choices[0].message.content == "ok"
    assert plan.asked == KEYS
    assert fresh.handed_over == 0


def test_never_keeps_the_overloaded_model(with_chain, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    plan = Plan({KEYS[0]: [overloaded()], KEYS[1]: [overloaded()], KEYS[2]: ["ok"], KEYS[3]: ["x"]})
    result = with_chain.complete(FakeCall(plan))
    assert result.choices[0].message.content == "ok"
    assert with_chain.handed_over == 0


def test_inside_the_busy_window_the_next_reply_is_handed_over_without_a_request(with_chain):
    with_chain._note_busy(PRIMARY)
    plan = Plan({k: [overloaded(), "x"] for k in KEYS})
    with pytest.raises(transport.KamePoolResting) as caught:
        with_chain.complete(FakeCall(plan))
    assert plan.asked == []
    assert "busy" in str(caught.value)


def test_after_the_busy_window_one_knock_is_enough(with_chain):
    with_chain._note_busy(PRIMARY)
    with_chain._busy_until[PRIMARY] = time.time() - 1   # the window ran out just now
    plan = Plan({k: [overloaded(), "x"] for k in KEYS})
    with pytest.raises(transport.KamePoolResting):
        with_chain.complete(FakeCall(plan))
    assert len(plan.asked) == 1


def test_an_answer_clears_the_busy_memory(fresh):
    fresh._note_busy(PRIMARY)   # no fallback list: the busy model is still asked
    plan = Plan({KEYS[0]: ["ok"]})
    fresh.complete(FakeCall(plan))
    assert PRIMARY not in fresh._busy_until


def test_the_busy_window_doubles_while_the_storm_repeats_and_stops_at_five_minutes(fresh):
    for _ in range(12):
        fresh._note_busy(PRIMARY)
    assert fresh._busy_window[PRIMARY] == transport._BUSY_MAX_S
    fresh._busy_window.clear()
    fresh._busy_until.clear()
    fresh._note_busy(PRIMARY)
    assert fresh._busy_window[PRIMARY] == transport._BUSY_FIRST_S


# --- automatic numbers --------------------------------------------------------


THINKING = hedging.reasoning_model(REQUEST["model"], REQUEST)   # Gemini 3.x thinks first


def _learn(identity, seconds, n=40, reasoning=THINKING):
    for _ in range(n):
        hedging.HEDGE_TRACKER.record_latency(identity, seconds, stream=True, reasoning=reasoning)


def test_the_automatic_switch_time_follows_the_models_usual_first_word(fresh):
    call = FakeCall(Plan({}))
    assert fresh._auto_switch_s(PRIMARY, call) == transport._AUTO_SWITCH_COLD_S
    _learn(PRIMARY, 1.0)
    assert fresh._auto_switch_s(PRIMARY, call) == transport._AUTO_SWITCH_MIN_S   # 3 x 1 s -> 6 s floor
    hedging.HEDGE_TRACKER.clear()
    _learn(PRIMARY, 4.0)
    assert fresh._auto_switch_s(PRIMARY, call) == 12.0
    hedging.HEDGE_TRACKER.clear()
    _learn(PRIMARY, 30.0)
    assert fresh._auto_switch_s(PRIMARY, call) == transport._AUTO_SWITCH_MAX_S


def test_the_automatic_first_token_wait_is_far_past_the_usual_and_never_short(fresh):
    # Never later than Hermes' own 120 s; a thinking model never sooner.
    assert transport.auto_first_token_wait(PRIMARY, False) == 120.0           # cold = Hermes' own
    assert transport.auto_first_token_wait(PRIMARY, True) == 120.0           # thinking: never sooner
    _learn(PRIMARY, 1.0, reasoning=False)
    assert transport.auto_first_token_wait(PRIMARY, False) == 30.0            # floor
    assert transport.auto_first_token_wait(PRIMARY, False, 80_000) == 70.0    # a long prompt reads longer
    hedging.HEDGE_TRACKER.clear()
    _learn(PRIMARY, 10.0, reasoning=False)
    assert transport.auto_first_token_wait(PRIMARY, False) == 40.0            # 4 x median
    hedging.HEDGE_TRACKER.clear()
    _learn(PRIMARY, 100.0, reasoning=False)
    assert transport.auto_first_token_wait(PRIMARY, False) == 120.0           # ceiling
    hedging.HEDGE_TRACKER.clear()
    _learn(PRIMARY, 5.0, reasoning=True)
    assert transport.auto_first_token_wait(PRIMARY, True) == 120.0            # thinking floor


def test_the_first_token_wait_modes(fresh, monkeypatch):
    remote = "https://generativelanguage.googleapis.com/v1beta/openai"
    # automatic: streams only, never a local model, halved from the third try (never under the floor)
    cold = 120.0
    floor = 120.0 if THINKING else 30.0
    assert transport.attempt_read_timeout(remote, 1, identity=PRIMARY, kwargs=REQUEST, streaming=True) == cold
    assert transport.attempt_read_timeout(remote, 3, identity=PRIMARY, kwargs=REQUEST,
                                          streaming=True) == max(floor, cold / 2)
    assert transport.attempt_read_timeout(remote, 1, identity=PRIMARY, kwargs=REQUEST, streaming=False) is None
    assert transport.attempt_read_timeout("http://localhost:11434", 1, identity=PRIMARY,
                                          kwargs=REQUEST, streaming=True) is None
    # a number set before 1.8.2.8 is "My time", with its 1.8.1.8 rules
    monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "40")
    assert settings.first_token_mode() == "timed"
    assert transport.attempt_read_timeout(remote, 1) == 40.0
    assert transport.attempt_read_timeout(remote, 3) == 10.0
    monkeypatch.setenv("KAME_FIRST_TOKEN_WAIT", "off")
    assert transport.attempt_read_timeout(remote, 1, streaming=True) is None


# --- pressure is quota pressure ---------------------------------------------


def test_keys_resting_for_a_503_are_not_429_pressure(fresh):
    for key in KEYS[:3]:
        fresh.engine.mark(PRIMARY, key, False, 1.0, "server")
    assert not fresh._quota_pressure(PRIMARY, KEYS)
    for key in KEYS[:2]:
        fresh.engine.mark(PRIMARY, key, False, 60.0, "per_minute")
    assert fresh._quota_pressure(PRIMARY, KEYS)


# --- the reply is named -------------------------------------------------------


def test_each_reply_is_named_with_its_model_and_time(fresh, monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_turn_notice", lambda text: said.append(text) or True)
    plan = Plan({KEYS[0]: ["ok"]})
    fresh.complete(FakeCall(plan))
    assert said and said[0].startswith("gemini-3.8-flash answered")
    assert "total" in said[0]
    assert fresh.replies[-1]["model"] == "gemini-3.8-flash"
    assert fresh.replies[-1]["for"] == ""


def test_a_fallback_model_says_which_model_it_answered_for(with_chain, monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_turn_notice", lambda text: said.append(text) or True)
    plan = Plan({KEYS[0]: ["ok"]})
    with_chain.complete(FakeCall(plan, identity="gemini:gemini-3.7-flash"))
    assert said[0].startswith("gemini-3.7-flash answered for gemini-3.8-flash")
    assert with_chain.replies[-1]["for"] == "gemini-3.8-flash"


@pytest.mark.parametrize("mode, notices", [("never", 0), ("switches", 0), ("always", 1)])
def test_the_reply_notice_setting(fresh, monkeypatch, mode, notices):
    said = []
    monkeypatch.setattr(transport, "_turn_notice", lambda text: said.append(text) or True)
    monkeypatch.setenv("KAME_REPLY_NOTICE", mode)
    fresh.complete(FakeCall(Plan({KEYS[0]: ["ok"]})))
    assert len(said) == notices
    assert len(fresh.replies) == 1          # the panel keeps the reply whatever the notice says


def test_a_background_job_is_never_named(fresh, monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_turn_notice", lambda text: said.append(text) or True)
    call = FakeCall(Plan({KEYS[0]: ["ok"]}))
    call.channel = "aux"
    fresh.complete(call)
    assert said == [] and len(fresh.replies) == 0


# --- the bridge only draws where a notice is drawn ----------------------------


def test_the_bridge_notice_only_reaches_the_desktop():
    spec = importlib.util.spec_from_file_location(
        "kame_bridge_1828_under_test", ROOT / "hermes-kame-bridge" / "__init__.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    def desktop(notice):
        return None

    desktop.__module__ = "tui_gateway.agent_callbacks"

    def telegram(notice):
        return None

    telegram.__module__ = "gateway.run_turn_runner"
    assert bridge._draws_notices(SimpleNamespace(notice_callback=desktop))
    assert not bridge._draws_notices(SimpleNamespace(notice_callback=telegram))
    assert not bridge._draws_notices(SimpleNamespace(notice_callback=None))


def test_the_bridge_installs_the_notice_seam_after_the_status_seam(monkeypatch):
    """Fake Hermes modules: both seams wrap perform_api_call in order, the call still
    runs once, and a notice reaches the agent only while its call is in flight."""
    import types as _types
    status_output = _types.ModuleType("agent.status_output")
    turn_api_call = _types.ModuleType("agent.turn_api_call")
    conversation_loop = _types.ModuleType("agent.conversation_loop")
    credits = _types.ModuleType("agent.credits_tracker")

    class AgentNotice:
        def __init__(self, text, level="info", kind="sticky", ttl_ms=None, key=None, id=None):
            self.text, self.level, self.kind, self.ttl_ms, self.key = text, level, kind, ttl_ms, key

    credits.AgentNotice = AgentNotice
    ran = []

    def perform_api_call(agent, *args, **kwargs):
        ran.append(agent)
        return status_output.notify_turn_notice("gemini-3.8-flash answered · 1.2 s total")

    turn_api_call.perform_api_call = perform_api_call
    conversation_loop.perform_api_call = perform_api_call
    agent_pkg = _types.ModuleType("agent")
    for name, mod in (("agent", agent_pkg), ("agent.status_output", status_output),
                      ("agent.turn_api_call", turn_api_call),
                      ("agent.conversation_loop", conversation_loop),
                      ("agent.credits_tracker", credits)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location(
        "kame_bridge_1828_seams", ROOT / "hermes-kame-bridge" / "__init__.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    outcomes = bridge.install()
    assert outcomes[0].startswith("status line: installed")
    assert outcomes[1].startswith("reply notice: installed")
    assert bridge.install()[1] == "reply notice: already installed"     # a second registration

    shown = []

    def desktop_notice(notice):
        shown.append(notice)

    desktop_notice.__module__ = "tui_gateway.agent_callbacks"
    desktop = SimpleNamespace(notice_callback=desktop_notice,
                              _emit_notice=lambda notice: desktop_notice(notice))
    assert conversation_loop.perform_api_call(desktop) is True
    assert ran == [desktop]
    assert shown[0].kind == "ttl" and shown[0].key == "kame.reply" and "answered" in shown[0].text
    telegram = SimpleNamespace(notice_callback=lambda n: shown.append(n),
                               _emit_notice=lambda n: shown.append(n))
    assert conversation_loop.perform_api_call(telegram) is False      # would become a chat message
    assert len(shown) == 1
    assert status_output.notify_turn_notice("outside a call") is False


def test_hermes_switch_line_is_quiet_only_for_kames_own_hand_over(monkeypatch):
    """Owner, live test: "⚠️ Model fallback … recovery is not guaranteed" showed in
    the chat after a planned hand-over. Only that line (and its "restored" undo)
    is kept off; a switch KAME did not make is shown as Hermes wrote it."""
    import types as _types
    status_output = _types.ModuleType("agent.status_output")
    turn_api_call = _types.ModuleType("agent.turn_api_call")
    credits = _types.ModuleType("agent.credits_tracker")
    credits.AgentNotice = object
    lines = []

    class StatusOutputMixin:
        def _emit_diagnostic_status(self, message):
            lines.append(message)

    status_output.StatusOutputMixin = StatusOutputMixin
    handover = transport.KamePoolResting("kame: gemini-3.8-flash is busy.", time.time() + 2)

    def perform_api_call(agent, *args, **kwargs):
        raise handover

    turn_api_call.perform_api_call = perform_api_call
    for name, mod in (("agent", _types.ModuleType("agent")), ("agent.status_output", status_output),
                      ("agent.turn_api_call", turn_api_call), ("agent.credits_tracker", credits)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location(
        "kame_bridge_1828_quiet", ROOT / "hermes-kame-bridge" / "__init__.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    assert bridge.install()[2].startswith("quiet switch line: installed")
    assert bridge.install()[2] == "quiet switch line: already installed"

    class Agent(StatusOutputMixin):
        model = "gemini-3.8-flash"

    agent = Agent()
    agent._emit_diagnostic_status("⚠️ Model fallback: gemini-3.8-flash via gemini unavailable (x); "
                                  "using gemini-3.6 via gemini.")
    assert len(lines) == 1                       # before any hand-over: shown
    with pytest.raises(transport.KamePoolResting):
        turn_api_call.perform_api_call(agent)
    agent._emit_diagnostic_status("⚠️ Model fallback: gemini-3.8-flash via gemini unavailable "
                                  "(rate limit); using gemini-3.6 via gemini. Primary retry eligible "
                                  "in ~2 s; recovery is not guaranteed.")
    agent._emit_diagnostic_status("⚠️ Model fallback: gemini-3.6 via gemini unavailable (auth); "
                                  "using gpt-oss via groq.")
    assert len(lines) == 2 and lines[1].startswith("⚠️ Model fallback: gemini-3.6")
    agent._emit_diagnostic_status("✅ Primary model restored: gemini-3.8-flash via gemini; fallback "
                                  "gemini-3.6 via gemini is no longer active.")
    assert len(lines) == 2                       # the undo of a planned switch is quiet too
    agent._emit_diagnostic_status("✅ Primary model restored: gemini-3.8-flash via gemini; …")
    agent._emit_diagnostic_status("Context compressed")
    assert len(lines) == 4                       # nothing KAME did: shown



# --- review fixes (2026-10-17) -------------------------------------------------


@pytest.mark.parametrize("kind, status, busy", [
    ("server", 503, True), ("server", 500, True),
    ("timeout", 504, True), ("timeout", 408, True),
    ("timeout", None, False),             # our own cut or Hermes' read timeout: says nothing of the provider
    ("per_minute", 429, False),           # one key's quota, not a busy model
    ("rate_limit", 429, False),
    ("daily", 429, False),
])
def test_busy_is_the_provider_saying_so(fresh, kind, status, busy):
    assert fresh._busy_failure(PRIMARY, KEYS[0], kind, status) is busy


def test_after_my_time_still_moves_a_busy_model_at_once(with_chain, monkeypatch):
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "60")
    assert settings.model_switch_mode() == "timed"
    plan = Plan({k: [overloaded(), "late"] for k in KEYS})
    with pytest.raises(transport.KamePoolResting):
        with_chain.complete(FakeCall(plan))
    assert len(plan.asked) == 2


def test_never_also_keeps_hermes_from_switching_on_its_own(with_chain, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    assert with_chain._chain_would_switch(PRIMARY)


def test_a_value_outside_the_choices_is_the_default(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "off")
    assert settings.model_switch_mode() == "auto"
    monkeypatch.setenv("KAME_REPLY_NOTICE", "off")
    assert settings.reply_notice_mode() == "always"


def test_an_empty_answer_is_not_named_and_does_not_end_the_busy_state(fresh, monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_turn_notice", lambda text: said.append(text) or True)
    fresh._note_busy(PRIMARY)
    plan = Plan({k: [""] * 6 for k in KEYS})
    fresh.complete(FakeCall(plan))
    assert said == [] and not fresh.replies
    assert PRIMARY in fresh._busy_until
