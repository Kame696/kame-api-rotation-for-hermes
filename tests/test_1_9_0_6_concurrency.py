"""1.9.0.6 (decisions/0037 C8) - many sessions and subagents at once, no time limit (owner, 2026-11-23).

"2 sessions or several subagents running, without hanging, fast in all of them, and no time limit."
Real threads; every thread has its own ``HERMES_SESSION_ID`` ContextVar (a stand-in of the host's
``gateway.session_context``, never the real Hermes).
"""
from __future__ import annotations

import contextvars
import importlib
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_silent_two_models as T
from tests import test_1_9_0_6_switch_budget as SBT
from tests.test_1_9_0_6_handover_budget import FOUR, ask, rest_all  # noqa: F401
from tests.test_1_9_0_6_slow_standin import fresh as storm  # noqa: F401  (a transport with a quick slow bound)

transport = R.transport
settings = R.settings
chain_mod = T.chain_mod
sb = importlib.import_module(f"{R.PACKAGE}.core.switch_budget")

SESSIONS = 12           # 8 conversations + 4 subagents


def run_all(t, make_call, sessions=SESSIONS, wall=30.0, sid=None):
    """Start ``sessions`` threads, each in its own context with its own session id. Returns results by id."""
    results, errors = {}, {}

    def one(name):
        ctx = contextvars.copy_context()
        if sid is not None:
            ctx.run(sid.set, name)
        call = make_call(name)
        try:
            results[name] = (ctx.run(lambda: list(t.stream(call))), call)
        except BaseException as exc:  # noqa: BLE001
            errors[name] = exc

    threads = [threading.Thread(target=one, args=(f"s{i}",), daemon=True) for i in range(sessions)]
    started = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join(max(0.1, wall - (time.monotonic() - started)))
    assert not any(th.is_alive() for th in threads), "a session hung (deadlock or serialization)"
    return results, errors, time.monotonic() - started


@pytest.fixture()
def host(monkeypatch):
    sid, _ = SBT._install_session_context(monkeypatch)
    sb.reset_all()
    return sid


# --- 2. parallel, not serialized -------------------------------------------------------------------------------------
def test_twelve_sessions_each_waiting_two_seconds_run_in_parallel(storm, host, monkeypatch):
    monkeypatch.setattr(transport, "_SLOW_S", {False: (30.0, 30.0, 30.0), True: (30.0, 30.0, 30.0)})   # 2 s is a normal answer here
    def make(name):
        pool = R.Pool({k: [[2.0, R.chunk("ok ", finish=None), R.chunk("done", finish="stop")]] * 4 for k in R.KEYS})
        return R.FakeCall(pool)

    results, errors, took = run_all(storm, make, sid=host)
    assert not errors, errors
    assert len(results) == SESSIONS and all(R.shown(out) == "ok done" for out, _ in results.values())
    assert took < 2 * 2.0 + 2.5, f"{took:.1f}s: 12 requests of 2 s each were serialized"
    assert sb.used("") == 0


# --- 3. a storm under concurrency ---------------------------------------------------------------------------------------
def test_a_storm_across_twelve_sessions_answers_everyone_and_hands_nothing_to_hermes(storm, host):
    requests = []

    def make(name):
        call = T.ByModel(T.silent_pool(2.5))
        orig = call.client_for

        def counted(key, attempt):
            requests.append(name)
            return orig(key, attempt)

        call.client_for = counted
        return call

    results, errors, took = run_all(storm, make, sid=host, wall=60.0)
    resting = [e for e in errors.values() if isinstance(e, transport.KamePoolResting)]
    assert not resting, "no KamePoolResting reached Hermes"
    assert not errors, errors
    assert len(results) == SESSIONS and all(R.shown(out) for out, _ in results.values())
    assert all(sb.used(f"s{i}") == 0 for i in range(SESSIONS))
    assert storm.handed_over == 0
    print(f"C8 storm: {SESSIONS} sessions, {len(requests)} provider requests, {took:.1f}s")


# --- 4. budgets do not leak across sessions -----------------------------------------------------------------------------
def test_one_sessions_spent_budget_does_not_touch_another(storm, host, monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: groq\n    model: llama-x\n", encoding="utf-8")
    chain_mod.forget()
    rest_all(storm, "gemini:gemini-race")
    a, b = contextvars.copy_context(), contextvars.copy_context()
    a.run(host.set, "A"); b.run(host.set, "B")
    call = R.FakeCall(R.Pool({}))
    got_a = [a.run(ask, storm, call) for _ in range(3)]
    assert [type(x).__name__ for x in got_a] == ["KamePoolResting", "KamePoolResting", "NoneType"]
    assert isinstance(b.run(ask, storm, call), transport.KamePoolResting), "B starts with its own budget"


def test_a_thread_with_no_session_never_reads_another_sessions_id_from_the_environment(host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "A")
    sb.reset_all()
    sb.note_switch("A"); sb.note_switch("A")
    seen = {}

    def bare():
        seen["key"] = sb.conversation_key()
        seen["may"] = sb.may_hand_over(seen["key"])

    th = threading.Thread(target=bare)
    th.start(); th.join()
    assert seen["key"] == "", "the process-wide os.environ id is not this thread's"
    assert seen["may"] is True, "A's spent budget is not counted against the bare thread"
    assert sb.used("A") == 2


# --- 5. a long wait stays alive ---------------------------------------------------------------------------------------
def test_a_two_hour_wait_is_never_cut_and_hermes_hears_every_keepalive(storm, tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "_publish", lambda *a, **k: None)      # the panel file is not what is measured
    monkeypatch.setattr(transport._Spinner, "update", classmethod(lambda cls, *a, **k: None))
    monkeypatch.setattr(transport, "_SLEEP_SLICE_S", 5.0)      # a fake clock: 5 s per step keeps the test under a second or two
    (tmp_path / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-race\n", encoding="utf-8")
    chain_mod.forget()
    rest_all(storm, "gemini:gemini-race", seconds=3 * 3600.0)
    fake = [0.0]
    storm._sleep = lambda s: (fake.__setitem__(0, fake[0] + s), time.sleep(0.0003))[1]
    # a provider that named the time (not a label-held rest): the live re-test of E1 is a different mechanism
    storm.engine.retest_due_in = lambda *a, **k: None
    storm._retest_pick = lambda *a, **k: None
    call = R.FakeCall(R.Pool({}))
    beats, gaps, last = 0, [], 0.0
    box = {}

    def drive():
        nonlocal beats, last
        try:
            for item in storm._drive(call, stream=True):
                if item is transport._KEEPALIVE:
                    beats += 1
                    gaps.append(fake[0] - last)
                    last = fake[0]
                if fake[0] >= 2 * 3600.0:
                    call.closed = True
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    th = threading.Thread(target=drive, daemon=True)
    th.start(); th.join(60.0)
    assert not th.is_alive()
    err = box.get("err")
    assert fake[0] >= 2 * 3600.0, f"KAME gave up after {fake[0]:.0f} fake seconds: {err!r}"
    assert not isinstance(err, transport.KamePoolResting)
    assert beats >= 2 * 3600 / storm.KEEPALIVE_S * 0.9
    assert max(gaps) <= storm.KEEPALIVE_S + 5.0 + 0.5, max(gaps)


def test_the_total_wait_bound_is_zero_for_chat_and_subagents_and_300_only_for_cron(monkeypatch):
    sid, _ = SBT._install_session_context(monkeypatch)
    settings.forget()
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    ctx = contextvars.copy_context()
    ctx.run(sid.set, "a-subagent-session")
    assert settings.total_wait_bound() == 0.0                       # a chat call
    assert ctx.run(settings.total_wait_bound) == 0.0                # a delegated child
    cron_ctx = contextvars.copy_context()
    cron_ctx.run(sys.modules["gateway.session_context"]._table["HERMES_CRON_SESSION"].set, "1")
    assert cron_ctx.run(settings.total_wait_bound) == 300.0
    assert settings.CRON_TOTAL_WAIT_DEFAULT_S == 300.0
    # 1.9.0.6 (C16): the process-wide variable of a cron job running beside the chat never caps the chat
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert settings.total_wait_bound() == 0.0
    assert ctx.run(settings.total_wait_bound) == 0.0


# --- 1. the lock audit, pinned ---------------------------------------------------------------------------------------
def test_no_lock_is_held_across_a_request_a_stream_read_a_sleep_or_a_yield():
    """Static pin of the audit: no ``with <lock>:`` body in the plugin yields or calls the network/sleep."""
    import ast
    root = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"
    lock_name = re.compile(r"(lock|_lk|cond)", re.I)
    banned = re.compile(r"(wire\.(create|open)$|client_for$|time\.sleep$|self\._sleep$|urlopen$|"
                        r"requests\.(get|post)$|httpx\.(get|post|stream)$|_drive$|\.stream$)")
    offences = []
    for path in sorted(list(root.glob("*.py")) + list((root / "core").glob("*.py"))):
        src = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.With):
                continue
            head = " ".join(ast.get_source_segment(src, i.context_expr) or "" for i in node.items)
            if not lock_name.search(head) or head.startswith("open"):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Yield, ast.YieldFrom, ast.Await)):
                    offences.append(f"{path.name}:{node.lineno} yields inside {head}")
                if isinstance(sub, ast.Call) and banned.search(ast.get_source_segment(src, sub.func) or ""):
                    offences.append(f"{path.name}:{sub.lineno} {ast.get_source_segment(src, sub.func)} inside {head}")
    assert not offences, offences
