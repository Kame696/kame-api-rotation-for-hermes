"""1.9.0.6 (decisions/0037 C2) - KAME counts Hermes' model switches per conversation and never spends the last one."""
from __future__ import annotations

import contextvars
import importlib
import os
import sys
import types

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_9_0_6_switch_budget as SBT

transport = R.transport
settings = R.settings
chain_mod = P.chain_mod
sb = importlib.import_module(f"{R.PACKAGE}.core.switch_budget")
pkg = sys.modules[R.PACKAGE]

PRIMARY = "gemini:gemini-race"
SECOND = "gemini:gemini-race-two"

MIXED = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: groq
    model: llama-x
  - provider: gemini
    model: gemini-race-two
"""

#: [gemini A (main), openai B, anthropic C, gemini D]
FOUR = """\
model:
  provider: gemini
  default: gemini-a
fallback_providers:
  - provider: openai
    model: gpt-b
  - provider: anthropic
    model: claude-c
  - provider: gemini
    model: gemini-d
"""


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(MIXED, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    sb.reset_all()
    transport._HOST_WALK_PERMITS.clear()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    sb.reset_all()
    transport._HOST_WALK_PERMITS.clear()


@pytest.fixture()
def events(monkeypatch):
    said = []
    real = transport._emit

    def spy(call, kind, **fields):
        said.append((kind, str(fields.get("reason") or "")))
        return real(call, kind, **fields)

    monkeypatch.setattr(transport, "_emit", spy)
    return said


def rest_all(t, identity, seconds=3600.0, kind="daily"):
    for key in R.KEYS:
        t.engine.mark(identity, key, False, seconds, kind)


def ask(t, call, identity=PRIMARY, **kw):
    t._handover_until.clear()
    return t._maybe_hand_over(call, identity, R.KEYS, 0.0, False, **kw)


def budget_events(events):
    return [r for k, r in events if k == "switch" and "Hermes switches model at most" in r]


# --- the budget in the hand-over ----------------------------------------------------------------------
def test_the_0027_case_two_hand_overs_then_kame_waits_and_an_answer_gives_them_back(fresh, events):
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    first = ask(fresh, call)
    second = ask(fresh, call)
    third = ask(fresh, call)
    assert isinstance(first, transport.KamePoolResting) and isinstance(second, transport.KamePoolResting)
    assert third is None, "the last switch is never spent"
    assert sb.used("") == 2
    assert fresh.budget_held == 1
    sb._COUNTS[""][1] -= 500          # idle: an answer may reset the shared key
    sb.note_answer("")
    assert isinstance(ask(fresh, call), transport.KamePoolResting)


def test_mixed_list_two_hand_overs_then_the_third_waits(fresh, tmp_path, events):
    (tmp_path / "config.yaml").write_text(FOUR, encoding="utf-8")
    chain_mod.forget()
    A, B, C = "gemini:gemini-a", "openai:gpt-b", "anthropic:claude-c"
    callA = R.FakeCall(P.pool()); callA.identity = A
    # A slow: Hermes is handed to B (count 1)
    one = ask(fresh, callA, A, busy=True, slow=45.0)
    assert isinstance(one, transport.KamePoolResting) and sb.used("") == 1
    # B busy: handed to C (count 2)
    callB = R.FakeCall(P.pool()); callB.identity = B
    callB._kame_origin = A
    two = ask(fresh, callB, B, busy=True)
    assert isinstance(two, transport.KamePoolResting) and sb.used("") == 2
    # C slow: the budget is spent, C has no sibling in the list: nothing is handed over, C keeps waiting
    callC = R.FakeCall(P.pool()); callC.identity = C
    callC._kame_origin = A
    assert ask(fresh, callC, C, busy=True, slow=45.0, stand_in_ok=True) is None
    assert ask(fresh, callC, C, busy=True, slow=45.0, stand_in_ok=True) is None
    assert len(budget_events(events)) == 1, "one event per call"
    assert "3 times per answer" in budget_events(events)[0]
    assert sb.used("") == 2, "a refused hand-over is not a switch"


def test_a_spent_budget_stands_in_with_a_sibling_anywhere_in_the_list(fresh):
    rest_all(fresh, PRIMARY)
    sb.note_switch(""); sb.note_switch("")
    call = R.FakeCall(P.pool())
    # the first usable entry is groq (another provider); the sibling is the second one
    chosen = ask(fresh, call, stand_in_ok=True)
    assert isinstance(chosen, transport._StandIn) and chosen.identity == SECOND


def test_with_budget_left_the_first_usable_entry_is_still_handed_to_hermes(fresh):
    rest_all(fresh, PRIMARY)
    assert isinstance(ask(fresh, R.FakeCall(P.pool()), stand_in_ok=True), transport.KamePoolResting)


def test_a_stand_in_is_never_counted(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(P.CHAIN, encoding="utf-8")
    chain_mod.forget()
    rest_all(fresh, PRIMARY)
    for _ in range(4):
        assert isinstance(ask(fresh, R.FakeCall(P.pool()), stand_in_ok=True), transport._StandIn)
    assert sb.used("") == 0


def test_two_conversations_with_different_ids_keep_separate_counts(fresh, monkeypatch):
    sid, _ = SBT._install_session_context(monkeypatch)
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    a, b = contextvars.copy_context(), contextvars.copy_context()
    a.run(sid.set, "A"); b.run(sid.set, "B")
    assert isinstance(a.run(ask, fresh, call), transport.KamePoolResting)
    assert isinstance(a.run(ask, fresh, call), transport.KamePoolResting)
    assert a.run(ask, fresh, call) is None
    assert isinstance(b.run(ask, fresh, call), transport.KamePoolResting), "B still has its own budget"
    assert sb.used("A") == 2 and sb.used("B") == 1


def test_without_a_session_id_conversations_share_one_count(fresh):
    rest_all(fresh, PRIMARY)
    c1, c2 = R.FakeCall(P.pool()), R.FakeCall(P.pool())
    assert isinstance(ask(fresh, c1), transport.KamePoolResting)
    assert isinstance(ask(fresh, c2), transport.KamePoolResting)
    assert ask(fresh, c1) is None and ask(fresh, c2) is None


def test_a_background_job_never_touches_the_budget(fresh):
    rest_all(fresh, PRIMARY)
    call = R.FakeCall(P.pool())
    call.channel = "secondary"
    assert ask(fresh, call) is None
    assert sb.used("") == 0


# --- note_host_fallback ------------------------------------------------------------------------------
def test_the_classification_hook_counts_a_failure_once_and_never_a_kame_hand_over(fresh):
    hook = pkg._on_api_error_classification
    hook(provider="gemini", model="gemini-race", status_code=503, error_message="overloaded",
         error_body={"error": {"message": "overloaded"}})
    assert sb.used("") == 1
    resting = transport.KamePoolResting("kame: x", 0.0)
    hook(provider="gemini", model="gemini-race", status_code=429, error_message="kame", error=resting,
         error_body={"error": {"code": "kame_pool_resting"}})
    assert sb.used("") == 1, "KAME's own hand-over returns before the count: no double count"


# --- resets ------------------------------------------------------------------------------------------
def _answer(fresh, call, ctx=None):
    run = (lambda: list(fresh.stream(call)))
    return ctx.run(run) if ctx else run()


def test_an_answer_resets_the_conversation_that_got_it(fresh, monkeypatch):
    sid, _ = SBT._install_session_context(monkeypatch)
    sb.note_switch("s1"); sb.note_switch("s1"); sb.note_switch("s2")
    ctx = contextvars.copy_context()
    ctx.run(sid.set, "s1")
    pool = R.Pool({k: [[0.01, R.chunk("ok", finish="stop")]] for k in R.KEYS})
    out = _answer(fresh, R.FakeCall(pool), ctx)
    assert R.shown(out) == "ok"
    assert sb.used("s1") == 0 and sb.used("s2") == 1


def test_a_background_answer_never_resets(fresh, monkeypatch):
    sid, _ = SBT._install_session_context(monkeypatch)
    sb.note_switch("s1")
    ctx = contextvars.copy_context()
    ctx.run(sid.set, "s1")
    pool = R.Pool({k: [[0.01, R.chunk("ok", finish="stop")]] for k in R.KEYS})
    call = R.FakeCall(pool)
    call.channel = "secondary"
    _answer(fresh, call, ctx)
    assert sb.used("s1") == 1


def _ctx_with_hooks():
    hooks = {}

    def register_hook(name, fn):
        if name == "pre_llm_call" and hooks.get("_deny"):
            raise ValueError("unknown hook")
        hooks[name] = fn

    return hooks, types.SimpleNamespace(register_hook=register_hook, register_command=lambda *a, **k: None,
                                        on_unload=lambda f: None)


def test_pre_llm_call_resets_and_injects_nothing(fresh):
    sb.note_switch("s1"); sb.note_switch("s2")
    assert pkg._on_turn_start(session_id="s1", user_message="hi", is_first_turn=True) is None
    assert sb.used("s1") == 0 and sb.used("s2") == 1


def test_register_adds_the_pre_llm_call_hook(fresh, monkeypatch):
    hooks, ctx = _ctx_with_hooks()
    monkeypatch.setattr(pkg, "_REGISTERED_CTX", None)
    monkeypatch.setattr(pkg, "_start_state_heartbeat", lambda: None)
    try:
        pkg.register(ctx)
    finally:
        monkeypatch.setattr(pkg, "_REGISTERED_CTX", None)
    assert hooks.get("pre_llm_call") is pkg._on_turn_start


def test_a_host_without_pre_llm_call_still_registers_and_resets_on_answer(fresh, monkeypatch):
    hooks, ctx = _ctx_with_hooks()
    hooks["_deny"] = True
    monkeypatch.setattr(pkg, "_REGISTERED_CTX", None)
    monkeypatch.setattr(pkg, "_start_state_heartbeat", lambda: None)
    try:
        pkg.register(ctx)
    finally:
        monkeypatch.setattr(pkg, "_REGISTERED_CTX", None)
    assert "transform_api_error_classification" in hooks and "pre_llm_call" not in hooks


def test_reset_pool_also_clears_the_budget(fresh):
    sb.note_switch("s1")
    control = importlib.import_module(f"{R.PACKAGE}.control")
    control._apply("clear_pool", "", None)
    assert sb.used("s1") == 0


def test_budget_held_is_in_the_panel_snapshot(fresh):
    state = importlib.import_module(f"{R.PACKAGE}.state")
    fresh.budget_held = 3
    assert state.snapshot(fresh)["counters"]["budget_held"] == 3


# --- the owner's case, replayed (2026-11-23) ----------------------------------------------------------------
from tests.test_1_9_0_6_slow_standin import fresh as storm, T as _T      # noqa: E402,F401  (fixture + helpers)


def test_the_owners_case_five_gemini_models_all_silent_never_reach_hermes(storm):
    """Five models of one provider and endpoint, every leg silent past the slow bound. Before 1.9.0.6 each slow
    moment handed the reply to Hermes; the fourth ended the turn. Now nothing reaches Hermes, the reply circles
    inside KAME and answers when a leg finally speaks."""
    import tempfile  # noqa: F401
    five = ("model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
            + "".join(f"  - provider: gemini\n    model: gemini-race-{n}\n" for n in ("two", "three", "four", "five")))
    home = os.environ["HERMES_HOME"]
    with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write(five)
    chain_mod.forget()
    call = _T.ByModel(_T.silent_pool(3.0))                       # nobody speaks before 3 s, well past the bound
    resting = 0
    try:
        out = list(storm.stream(call))
    except transport.KamePoolResting:
        resting += 1
        out = []
    assert resting == 0, "no KamePoolResting reached Hermes"
    assert R.shown(out) == "late", "the reply answered when a leg finally spoke"
    assert sb.used("") == 0 and storm.handed_over == 0
