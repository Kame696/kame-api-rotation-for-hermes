"""1.8.2.9 / decisions/0032 A3-A5 - a background job has a budget; the chat does not lose its wait.

The measured defect (2026-10-23 22:41-22:53): one ``title_generation`` call on the best model made 167
attempts over 744 s because it inherited the chat's ``max_total_wait_seconds: 0``. The fake clock here
moves only when the transport sleeps, so an hour-long scenario runs in milliseconds.
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
PACKAGE = "kame_aux_budget_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pkg = _load_package()
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
chain_mod = importlib.import_module(f"{PACKAGE}.core.fallback_chain")
settings = importlib.import_module(f"{PACKAGE}.settings")
events_mod = importlib.import_module(f"{PACKAGE}.core.events")

IDENTITY = "gemini:gemini-3.8-flash"
KEYS = [f"AIzaSyAUX{i:02d}" + "0" * 28 for i in range(14)]
CONFIG = ("model:\n  provider: gemini\n  default: gemini-3.8-flash\n"
          "fallback_providers:\n  - provider: gemini\n    model: gemini-3.7-flash\n")


class Refusal(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"message": message}}


def busy():
    return Refusal(503, "The model is overloaded. Please try again later.")


def answer(text):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


class Clock:
    def __init__(self):
        self.now = 1_000.0
        self.wall = time.time()

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall + self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class Plan:
    """What each key does when asked. ``script`` is a list of outcomes handed out in order."""

    def __init__(self, script, clock=None, spend=0.0):
        self.script = list(script)
        self.asked = []
        self.kwargs_seen = []
        self.clock, self.spend = clock, spend

    def run(self, key, kwargs):
        self.asked.append(key)
        self.kwargs_seen.append(dict(kwargs))
        if self.clock is not None and self.spend:
            self.clock.now += self.spend
        outcome = self.script.pop(0) if self.script else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        return answer(outcome)


class FakeCall(transport.Call):
    def __init__(self, plan, channel="main", task="chat", task_known=False, identity=IDENTITY,
                 timeout=None):
        self.plan, self._keys, self.identity = plan, list(KEYS), identity
        self.channel, self.task, self.task_known = channel, task, task_known
        self.kwargs = {"model": identity.split(":", 1)[1], "messages": [{"role": "user", "content": "hi"}]}
        if timeout is not None:
            self.kwargs["timeout"] = timeout
        self.provider = identity.split(":", 1)[0]

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        def create(**kwargs):
            return self.plan.run(key, kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return False


def title(plan, **kw):
    return FakeCall(plan, channel="secondary", task="title_generation", task_known=True, **kw)


def compression(plan, **kw):
    return FakeCall(plan, channel="secondary", task="compression", task_known=True, **kw)


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_RECORDER_DISABLED", "1")
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "time", clock.time)
    t = transport.KameTransport(engine=carousel_mod.Carousel(), sleep=clock.sleep)
    t.clock = clock
    events_mod.SECONDARY_EVENTS.clear()
    yield t
    settings.forget()
    chain_mod.forget()


def rest_all(t, seconds, kind="rate_limit"):
    for key in KEYS:
        t.engine.mark(IDENTITY, key, False, seconds, kind)


# --- the measured defect -------------------------------------------------------------------------
def test_the_measured_title_call_stops_at_three_requests(fresh):
    # The live call saw a mix of overloaded answers and refusals on 14 keys. Whatever the mix, a title
    # is stopped within three requests and half a minute, with the error shape the host moves on.
    mix = [busy(), Refusal(429, "Resource has been exhausted (e.g. check quota)."), busy()] * 6
    plan = Plan(mix, fresh.clock, spend=1.0)
    started = fresh.clock.now
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(title(plan))
    assert len(plan.asked) <= 3
    assert fresh.clock.now - started <= 30.0


def test_two_busy_keys_stop_a_background_job(fresh):
    plan = Plan([busy(), busy(), "never"], fresh.clock)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(compression(plan))
    assert len(plan.asked) == 2, "the third key is not asked: the model is busy"


def test_a_dry_key_stops_a_cosmetic_job_but_not_an_important_one(fresh):
    dry = Refusal(429, "Rate limit exceeded. Please retry after 20 seconds.")
    plan = Plan([dry], fresh.clock)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(title(plan))
    assert len(plan.asked) == 1
    plan2 = Plan([dry, "ok"], fresh.clock)
    fresh._aux_memo.clear()
    assert fresh.complete(compression(plan2)).choices[0].message.content == "ok"
    assert len(plan2.asked) == 2


def test_the_request_count_is_the_limit(fresh):
    other = lambda: Refusal(500, "Internal error encountered.")  # noqa: E731
    plan = Plan([other(), other(), other(), other(), other(), other(), other(), other()], fresh.clock, spend=0.5)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(compression(plan))
    assert len(plan.asked) <= 6


# --- refusing to start: zero requests --------------------------------------------------------------
def test_cosmetic_never_waits_on_resting_keys(fresh):
    rest_all(fresh, 3600.0, "daily")
    plan = Plan(["never"], fresh.clock)
    before = fresh.clock.now
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(title(plan))
    assert plan.asked == [] and fresh.clock.now - before < 1.0


def test_cosmetic_refuses_inside_a_busy_window(fresh):
    fresh._note_busy(IDENTITY)
    plan = Plan(["never"], fresh.clock)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(title(plan))
    assert plan.asked == []


def test_cosmetic_refuses_under_quota_pressure(fresh):
    for key in KEYS[:8]:
        fresh.engine.mark(IDENTITY, key, False, 3600.0, "daily")
    plan = Plan(["never"], fresh.clock)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(title(plan))
    assert plan.asked == []


def test_important_waits_within_its_budget(fresh):
    rest_all(fresh, 40.0)
    plan = Plan(["ok"], fresh.clock)
    started = fresh.clock.now
    assert fresh.complete(compression(plan)).choices[0].message.content == "ok"
    assert 35.0 <= fresh.clock.now - started <= 60.0


def test_important_eta_past_the_budget_raises_at_once(fresh):
    rest_all(fresh, 3600.0, "daily")
    plan = Plan(["never"], fresh.clock)
    started = fresh.clock.now
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(compression(plan))
    assert plan.asked == [] and fresh.clock.now - started < 1.0


def test_the_host_timeout_is_the_budget(fresh):
    rest_all(fresh, 40.0)
    plan = Plan(["never"], fresh.clock)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(compression(plan, timeout=12))
    assert plan.asked == []


def test_a_stopped_job_is_not_asked_again_for_thirty_seconds(fresh):
    plan = Plan([busy(), busy(), "ok"], fresh.clock)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(compression(plan))
    seen = len(plan.asked)
    # the host's own retry (1 s later) and the next attempt of the job cost nothing
    fresh.clock.sleep(1.0)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(compression(plan))
    assert len(plan.asked) == seen
    # the chat is not held back by it (the two busy keys opened the model's busy window: clear it)
    fresh._busy_until.clear()
    assert fresh.complete(FakeCall(plan)).choices[0].message.content == "ok"
    fresh.clock.sleep(31.0)
    fresh._busy_until.clear()
    plan.script = ["ok"]
    assert fresh.complete(compression(plan)).choices[0].message.content == "ok"


def test_the_stop_is_written_to_the_background_tab_and_counted(fresh):
    plan = Plan([busy(), busy()], fresh.clock)
    with pytest.raises(transport.KameAuxTimeout):
        fresh.complete(compression(plan))
    assert fresh._aux_stopped.get("compression") == 1
    rows = events_mod.SECONDARY_EVENTS.recent()
    assert any(r.get("kind") == "gave_up" and "compression" in str(r.get("reason")) for r in rows)


# --- trimming the attempt, never mutating Hermes' request ------------------------------------------
def test_the_attempt_timeout_is_trimmed_not_mutated(fresh):
    plan = Plan([busy(), "ok"], fresh.clock, spend=50.0)
    call = compression(plan, timeout=100)
    original = call.kwargs
    assert fresh.complete(call).choices[0].message.content == "ok"
    assert plan.kwargs_seen[0]["timeout"] == 100
    assert 5.0 <= plan.kwargs_seen[1]["timeout"] <= 60.0, "the second attempt cannot outlive what is left"
    assert original["timeout"] == 100 and call.kwargs is original, "Hermes' own dict is untouched"


# --- who decides the class ------------------------------------------------------------------------
def test_a_guessed_label_is_never_cosmetic(fresh):
    rest_all(fresh, 40.0)
    plan = Plan(["ok"], fresh.clock)
    guessed = FakeCall(plan, channel="secondary", task="title_generation", task_known=False)
    assert fresh.complete(guessed).choices[0].message.content == "ok", "important: it waited, not refused"


def test_no_backup_request_for_a_background_job(fresh, monkeypatch):
    calls = []
    monkeypatch.setattr(fresh, "_hedged_create", lambda *a, **k: calls.append(1))
    plan = Plan(["ok"], fresh.clock)
    assert fresh.complete(compression(plan)).choices[0].message.content == "ok"
    assert calls == [] and len(plan.asked) == 1


# --- the way back, and the chat untouched ---------------------------------------------------------
def test_aux_unbounded_restores_1_8_2_8(fresh, monkeypatch):
    monkeypatch.setenv("KAME_AUX_UNBOUNDED", "1")
    rest_all(fresh, 40.0)
    plan = Plan(["ok"], fresh.clock)
    assert fresh.complete(title(plan)).choices[0].message.content == "ok"


def test_the_main_lane_with_total_wait_0_still_waits(fresh, monkeypatch, tmp_path):
    monkeypatch.setenv("KAME_MAX_TOTAL_WAIT_SECONDS", "0")
    # No fallback list: the chat's own wait is what is under test, not the hand-over to Hermes' list.
    (tmp_path / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-3.8-flash\n",
                                          encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    rest_all(fresh, 90.0)
    plan = Plan(["ok"], fresh.clock)
    started = fresh.clock.now
    assert fresh.complete(FakeCall(plan)).choices[0].message.content == "ok"
    assert fresh.clock.now - started >= 85.0
    assert fresh._aux_stopped == {}
