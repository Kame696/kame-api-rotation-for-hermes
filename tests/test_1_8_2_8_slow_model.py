"""1.8.2.8 (decisions/0028 D11): a model that is slow right now hands the reply on.

The owner's live test (2026-10-18): gemini-3.8-flash took about two minutes to a
one-word answer, on every key, while gemini-3.7-flash answered in three seconds.
Nothing failed, so nothing switched. Now a reply with no sign of work by the
model's slow bound falls back to the next model of the list - only when one can
take it - and the slow model is left alone for a while.

Driven through ``KameTransport.stream`` with the stream-race fakes.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_8_slow_model.py -q
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
settings = R.settings
chain_mod = __import__(f"{R.PACKAGE}.core.fallback_chain", fromlist=["x"])

CHAIN = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: groq
    model: llama-x
"""
# 1.8.2.9 (decisions/0032 B3): the list ends with a model of ANOTHER provider, the only kind the
# race cannot reach. When every model Hermes could be handed is already in the race, nothing is cut
# any more (tests/test_1_8_2_9_silent_two_models.py); the hand-over these tests pin is the one to a
# model that is not racing.
SILENT = 3.0
QUICK = {False: (0.3, 0.3, 0.3), True: (0.3, 0.3, 0.3)}


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    # no backup race in these tests: the slow watch alone decides
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    monkeypatch.setattr(transport, "_SLOW_S", QUICK)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


def silent_pool():
    return R.Pool({k: [[SILENT, R.chunk("late", finish="stop")]] * 3 for k in R.KEYS})


def test_a_silent_model_falls_back_at_its_slow_bound_and_every_leg_is_cut(fresh):
    pool = silent_pool()
    started = time.monotonic()
    with pytest.raises(transport.KamePoolResting) as caught:
        list(fresh.stream(R.FakeCall(pool)))
    assert time.monotonic() - started < 1.5
    assert "slow right now" in str(caught.value)
    assert all(stream.response.closed.is_set() for _, stream in pool.opened)
    assert fresh._busy_until[R.IDENTITY] > time.time() + 20      # left alone for a while
    assert fresh.handed_over == 1


def test_the_next_reply_skips_the_slow_model_without_a_request(fresh):
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(R.FakeCall(silent_pool())))
    fresh._handover_until.clear()          # Hermes reached the next model, then a new reply
    # The raced-in next model was silent too and is marked slow as well; the model after it
    # (another provider) is free, so Hermes is handed the reply (1.8.2.9, decisions/0030 M6).
    # 1.8.3.0 (D2 a): were the same-provider sibling free, KAME would answer with it itself and
    # Hermes would hear nothing (tests/test_1_8_3_0_sibling_standin.py), so it stays marked slow.
    assert fresh._entry_busy("gemini:gemini-race-two")
    pool = silent_pool()
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(R.FakeCall(pool)))
    assert pool.opened == []


def test_when_every_later_model_is_known_slow_too_the_next_reply_stays_on_hermes_model(fresh):
    # 1.8.2.9 (decisions/0030, M6, owner: stay with the best model): handing Hermes a model
    # known slow too only walks the reply down to the last one. The slow bound is not armed
    # (no free model could take it), so the reply waits on the model Hermes asked for.
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(R.FakeCall(silent_pool())))
    fresh._handover_until.clear()
    assert fresh._entry_busy("gemini:gemini-race-two")      # the raced-in model was marked slow
    fresh._note_busy("groq:llama-x")                         # ... and the model after it is known slow too
    pool = R.Pool({k: [[0.3, R.chunk("still here", finish="stop")]] for k in R.KEYS})
    out = list(fresh.stream(R.FakeCall(pool)))
    assert R.shown(out) == "still here" and len(pool.opened) == 1


def test_without_a_fallback_list_a_slow_model_is_waited_for(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-race\n",
                                          encoding="utf-8")
    chain_mod.forget()
    pool = R.Pool({R.KEYS[0]: [[0.8, R.chunk("late", finish="stop")]]})
    out = list(fresh.stream(R.FakeCall(pool)))
    assert R.shown(out) == "late" and fresh.handed_over == 0


def test_never_waits_for_a_slow_model(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    pool = R.Pool({R.KEYS[0]: [[0.8, R.chunk("late", finish="stop")]]})
    out = list(fresh.stream(R.FakeCall(pool)))
    assert R.shown(out) == "late" and fresh.handed_over == 0


def test_a_model_that_is_thinking_is_working_not_slow(fresh):
    thinking = SimpleNamespace(id="c", model="m", usage=None, choices=[SimpleNamespace(
        index=0, delta=SimpleNamespace(role=None, content=None, tool_calls=None,
                                       reasoning_content="let me think"), finish_reason=None)])
    pool = R.Pool({R.KEYS[0]: [[0.05, thinking, 0.8, R.chunk("answer", finish="stop")]]})
    out = list(fresh.stream(R.FakeCall(pool)))
    assert R.shown(out) == "answer" and fresh.handed_over == 0


def test_when_the_next_model_is_known_slow_too_the_first_is_waited_for(fresh):
    fresh._note_busy("gemini:gemini-race-two:global")
    fresh._note_busy("groq:llama-x")             # the list's other-provider tail is known slow too
    pool = R.Pool({R.KEYS[0]: [[0.8, R.chunk("late", finish="stop")]]})
    out = list(fresh.stream(R.FakeCall(pool)))
    assert R.shown(out) == "late" and fresh.handed_over == 0


def test_set_time_is_the_users_number(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "timed")
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "45")
    settings.forget()
    assert fresh._slow_after(R.FakeCall(silent_pool()), R.IDENTITY, False) == 45.0


def test_automatic_learns_the_models_own_first_word(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_SLOW_S", {False: (20.0, 45.0, 30.0), True: (25.0, 60.0, 45.0)})
    call = R.FakeCall(silent_pool())
    assert fresh._slow_after(call, R.IDENTITY, False) == pytest.approx(30.0, abs=0.01)  # nothing learned
    for _ in range(40):
        R.hedging_mod.HEDGE_TRACKER.record_latency(R.IDENTITY, 4.0, stream=True, reasoning=False)
    assert fresh._slow_after(call, R.IDENTITY, False) == pytest.approx(20.0, abs=0.01)  # floor 20
    for _ in range(40):
        R.hedging_mod.HEDGE_TRACKER.record_latency(R.IDENTITY, 30.0, stream=True, reasoning=False)
    assert fresh._slow_after(call, R.IDENTITY, False) == pytest.approx(45.0, abs=0.01)  # the ceiling


def test_a_background_job_never_falls_back_for_slowness(fresh):
    call = R.FakeCall(silent_pool())
    call.channel = "aux"
    assert fresh._slow_after(call, R.IDENTITY, False) is None


def test_a_slow_model_is_left_alone_for_minutes_and_longer_while_it_stays_slow(fresh):
    # Live: replies a minute apart each paid 26 s on gemini-3.8 and 25 s on 3.7
    # before 3.6 answered in 2 s, because a 30 s window had always ended.
    fresh._note_busy(R.IDENTITY, first=transport._SLOW_FIRST_S, ceiling=transport._SLOW_MAX_S)
    assert fresh._busy_window[R.IDENTITY] == 120.0
    fresh._busy_until[R.IDENTITY] = time.time() - 60      # caught again a minute after it ended
    fresh._note_busy(R.IDENTITY, first=transport._SLOW_FIRST_S, ceiling=transport._SLOW_MAX_S)
    assert fresh._busy_window[R.IDENTITY] == 240.0
    for _ in range(5):
        fresh._note_busy(R.IDENTITY, first=transport._SLOW_FIRST_S, ceiling=transport._SLOW_MAX_S)
    assert fresh._busy_window[R.IDENTITY] == 600.0


def test_the_slow_hand_over_opens_the_long_window(fresh):
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(R.FakeCall(silent_pool())))
    assert fresh._busy_window[R.IDENTITY] == transport._SLOW_FIRST_S


# --- D12: the next model races a silent reply ------------------------------------

class ModelAwareCall(R.FakeCall):
    """The fallback model (same provider, same keys) answers at once; the first is silent."""

    def __init__(self, pool, fast_model="gemini-race-two"):
        super().__init__(pool)
        self.fast_model = fast_model
        self.models_asked = []

    def client_for(self, key, attempt):
        def create(**request):
            self.models_asked.append(request.get("model"))
            if request.get("model") == self.fast_model:
                return R.FakeStream([0.05, R.chunk("fast "), R.chunk("answer", finish="stop")])
            return self.pool.open(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_the_next_model_races_a_silent_reply_and_its_answer_is_shown(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_SLOW_S", {False: (5.0, 5.0, 5.0), True: (5.0, 5.0, 5.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.2)
    pool = silent_pool()
    call = ModelAwareCall(pool)
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "fast answer"
    assert time.monotonic() - started < 1.5                       # no slow bound, no restart
    assert call.models_asked == ["gemini-race", "gemini-race-two"]
    assert pool.opened[0][1].response.closed.is_set()            # the silent request was cut
    assert getattr(call, "_kame_answered_by") == "gemini:gemini-race-two"
    assert fresh._busy_until[R.IDENTITY] > time.time()           # next replies go straight on
    assert fresh.handed_over == 1


def test_a_first_model_that_answers_in_time_is_never_raced(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.5)
    pool = R.Pool({R.KEYS[0]: [[0.05, R.chunk("mine", finish="stop")]]})
    call = ModelAwareCall(pool)
    out = list(fresh.stream(call))
    assert R.shown(out) == "mine" and call.models_asked == ["gemini-race"]
    assert fresh.handed_over == 0


def test_saver_never_races_another_model(fresh, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")
    settings.forget()
    assert fresh._cross_target(R.FakeCall(silent_pool()), R.IDENTITY, False, 30.0) is None


def test_only_a_model_of_the_same_provider_is_raced(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: groq\n    model: llama-x\n  - provider: gemini\n    model: gemini-race-three\n",
        encoding="utf-8")
    chain_mod.forget()
    target = fresh._cross_target(R.FakeCall(silent_pool()), R.IDENTITY, False, 30.0)
    assert target == [("gemini:gemini-race-three", "gemini-race-three", 12.0)]   # cold: 12 s


def test_when_the_raced_model_is_silent_too_the_one_after_it_joins(fresh, tmp_path, monkeypatch):
    # Live: gemini-3.7 was as slow as 3.8 while 3.6 answered in 2 s.
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: gemini\n    model: gemini-race-two\n"
        "  - provider: gemini\n    model: gemini-race-three\n", encoding="utf-8")
    chain_mod.forget()
    targets = fresh._cross_target(R.FakeCall(silent_pool()), R.IDENTITY, False, 30.0)
    assert [t[2] for t in targets] == [12.0, 18.0]
    monkeypatch.setattr(transport, "_SLOW_S", {False: (5.0, 5.0, 5.0), True: (5.0, 5.0, 5.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.2)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.2)
    call = ModelAwareCall(silent_pool(), fast_model="gemini-race-three")
    started = time.monotonic()
    out = list(fresh.stream(call))
    assert R.shown(out) == "fast answer" and time.monotonic() - started < 1.5
    assert call.models_asked == ["gemini-race", "gemini-race-two", "gemini-race-three"]
    assert getattr(call, "_kame_answered_by") == "gemini:gemini-race-three"
    assert fresh._entry_busy("gemini:gemini-race-two")          # the silent one sits out too


# --- review fixes (2026-10-18) ----------------------------------------------------

def test_after_a_cross_win_the_reply_belongs_to_the_winning_model(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_SLOW_S", {False: (5.0, 5.0, 5.0), True: (5.0, 5.0, 5.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.2)
    call = ModelAwareCall(silent_pool())
    list(fresh.stream(call))
    assert call.kwargs["model"] == "gemini-race-two"        # a continuation would ask that model
    assert fresh.engine.healthy_count(R.IDENTITY, R.KEYS) == len(R.KEYS)   # no mark on the slow pool


def test_a_raced_model_keeps_its_own_whole_bound(fresh, monkeypatch):
    # the slow bound would fire 0.3 s after the request; the next model joined at
    # 0.2 s and answers at 0.45 s - it is not cut at 0.3 s.
    monkeypatch.setattr(transport, "_SLOW_S", {False: (0.3, 0.3, 0.3), True: (0.3, 0.3, 0.3)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.2)

    class LateCross(ModelAwareCall):
        def client_for(self, key, attempt):
            def create(**request):
                self.models_asked.append(request.get("model"))
                if request.get("model") == self.fast_model:
                    return R.FakeStream([0.25, R.chunk("late "), R.chunk("but here", finish="stop")])
                return self.pool.open(key)
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    out = list(fresh.stream(LateCross(silent_pool())))
    assert R.shown(out) == "late but here"


def test_only_the_same_endpoint_is_raced_and_by_its_written_name(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
        "  - provider: gemini\n    model: Gemini-Race-Two\n    base_url: https://elsewhere.example/v1\n"
        "  - provider: gemini\n    model: Gemini-Race-Three\n", encoding="utf-8")
    chain_mod.forget()
    targets = fresh._cross_target(R.FakeCall(silent_pool()), R.IDENTITY, False, 30.0)
    assert [(t[0], t[1]) for t in targets] == [("gemini:gemini-race-three", "Gemini-Race-Three")]


def test_a_busy_hit_never_inherits_a_ten_minute_slow_window(fresh):
    fresh._note_busy(R.IDENTITY, first=transport._SLOW_FIRST_S, ceiling=transport._SLOW_MAX_S)
    for _ in range(4):
        fresh._note_busy(R.IDENTITY, first=transport._SLOW_FIRST_S, ceiling=transport._SLOW_MAX_S)
    assert fresh._busy_window[R.IDENTITY] == 600.0
    fresh._note_busy(R.IDENTITY)                              # a busy hit: its own ceiling
    assert fresh._busy_window[R.IDENTITY] == transport._BUSY_MAX_S


def test_an_unnamed_endpoint_is_the_providers_own(fresh, tmp_path):
    # live: the owner's primary names the Gemini endpoint, his fallbacks do not
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: gemini\n  default: gemini-race\n  base_url: https://g.example/v1beta/openai\n"
        "fallback_providers:\n  - provider: gemini\n    model: gemini-race-two\n", encoding="utf-8")
    chain_mod.forget()
    targets = fresh._cross_target(R.FakeCall(silent_pool()), R.IDENTITY, False, 30.0)
    assert [t[0] for t in targets] == ["gemini:gemini-race-two"]
