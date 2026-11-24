"""1.9.0.6 (owner's first live test, 2026-11-23) - a reply walks the whole list when its models fail.

The owner's Hermes: main gemini-4-hydrogen, list 3.8, 3.7, 3.6, 3.5 (all Gemini, one key pool), "oi". Every
model but one answered 503 or 429. 1.9.0.6 kept the reply on 3.7 for 100 s, rotating its keys (each 429 rests a
key 30 s, so a key was always about to come back), and never asked 3.6 or 3.5; 1.9.0.5 handed the reply to Hermes,
which walked the list. Whatever the switch limit, a reply reaches the model of the list that answers.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P

transport = R.transport
settings = R.settings
chain_mod = P.chain_mod

OWNER = """\
model:
  provider: gemini
  default: gemini-4-hydrogen
fallback_providers:
  - provider: gemini
    model: gemini-3.8-flash
  - provider: gemini
    model: gemini-3.7-flash
  - provider: gemini
    model: gemini-3.6-flash
  - provider: gemini
    model: gemini-3.5-flash
"""

KEYS14 = [f"AIzaSyOWNR{i:02d}" + "0" * 28 for i in range(14)]
REQUEST = {"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "oi"}]}


class Owner(P.Scripted):
    """``503`` overloaded, ``429`` rate limited, ``fast`` answers."""

    def __init__(self, script):
        super().__init__(R.Pool({k: [[3.0, R.chunk("late", finish="stop")]] * 40 for k in KEYS14}), script)
        self._keys = list(KEYS14)
        self.kwargs = dict(REQUEST)
        self.identity = "gemini:gemini-4-hydrogen"

    def client_for(self, key, attempt):
        inner = super().client_for(key, attempt)
        script = self.script

        def create(**request):
            how = script.get(request.get("model"), ("silent",))
            if how[0] == "429":
                self.models_asked.append(request.get("model"))
                raise P.Refusal(429, "Resource has been exhausted (e.g. check quota).")
            return inner.chat.completions.create(**request)
        from types import SimpleNamespace
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(OWNER, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    transport._HOST_WALK_PERMITS.clear()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    transport._HOST_WALK_PERMITS.clear()


def run(t, call, limit=25.0):
    out, err = [], []

    def go():
        try:
            out.extend(t.stream(call))
        except BaseException as exc:  # noqa: BLE001 - the test reports what ended the reply
            err.append(exc)
    th = threading.Thread(target=go, daemon=True)
    started = time.monotonic()
    th.start()
    th.join(limit)
    return out, err, th.is_alive(), time.monotonic() - started


def test_every_model_busy_or_rate_limited_but_one_the_one_answers(fresh):
    call = Owner({"gemini-4-hydrogen": ("503",), "gemini-3.8-flash": ("503",), "gemini-3.7-flash": ("429",),
                  "gemini-3.6-flash": ("503",), "gemini-3.5-flash": ("fast", 0.05)})
    out, err, stuck, took = run(fresh, call)
    asked = list(dict.fromkeys(call.models_asked))
    assert not stuck, f"the reply was still going after {took:.0f}s; models asked: {asked}"
    assert not err or isinstance(err[0], transport.KamePoolResting), err
    if not err:
        assert R.shown(out) == "gemini-3.5-flash here", asked


def test_a_rate_limited_model_does_not_hold_the_reply(fresh):
    call = Owner({"gemini-4-hydrogen": ("429",), "gemini-3.8-flash": ("fast", 0.05)})
    out, err, stuck, took = run(fresh, call)
    asked = list(dict.fromkeys(call.models_asked))
    assert not stuck, f"the reply was still going after {took:.0f}s; models asked: {asked}"
    assert not err, err
    assert R.shown(out) == "gemini-3.8-flash here", asked


@pytest.mark.parametrize("third", ["429", "503"])
def test_the_owners_turn_a_silent_main_model_raced_while_the_list_fails(fresh, monkeypatch, third):
    """The live turn: the main model thinks in silence, the race asks 3.6 (503) and then 3.7 (429 on every key);
    3.8 is busy. At the slow bound the reply must leave for the model of the list that answers (3.5)."""
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (0.3, 1.5))
    call = Owner({"gemini-3.8-flash": ("503",), "gemini-3.7-flash": (third,),
                  "gemini-3.6-flash": ("503",), "gemini-3.5-flash": ("fast", 0.05)})
    call.pool = R.Pool({k: [[60.0, R.chunk("late", finish="stop")]] * 40 for k in KEYS14})
    out, err, stuck, took = run(fresh, call, limit=40.0)
    asked = list(dict.fromkeys(call.models_asked))
    assert not stuck, f"the reply was still going after {took:.0f}s; models asked: {asked}"
    assert not err or isinstance(err[0], transport.KamePoolResting), err
    if not err:
        assert R.shown(out) == "gemini-3.5-flash here", asked


def test_a_429_trickle_moves_on_instead_of_rotating_one_model(fresh, monkeypatch):
    """The live 3.7: each 429 rests its key 30 s and the keys came back one by one, so "every key resting" was never
    reached. Here a key rests 0.6 s and each refusal takes 0.1 s: fourteen keys never all rest at once."""
    carousel = R.carousel_mod
    monkeypatch.setattr(carousel, "UNSIZED_THROTTLE_REST_S", 0.6)
    monkeypatch.setattr(carousel, "UNSIZED_BACKOFF_MAX_S", 0.6)
    monkeypatch.setattr(carousel, "RL_BACKOFF_CAP_S", 0.6)
    call = Owner({"gemini-4-hydrogen": ("429",), "gemini-3.8-flash": ("fast", 0.05)})
    real = call.client_for

    def slow_refusals(key, attempt):
        client = real(key, attempt)
        create = client.chat.completions.create

        def wrapped(**request):
            if request.get("model") == "gemini-4-hydrogen":
                time.sleep(0.1)
            return create(**request)
        client.chat.completions.create = wrapped
        return client
    call.client_for = slow_refusals
    out, err, stuck, took = run(fresh, call, limit=20.0)
    asked = call.models_asked
    assert not stuck, f"the reply was still going after {took:.0f}s; hydrogen asked {asked.count('gemini-4-hydrogen')} times"
    assert not err, err
    assert R.shown(out) == "gemini-3.8-flash here"
    assert asked.count("gemini-4-hydrogen") <= 14, "half the pool refusing moves the reply; never a second lap"


def test_one_throttled_key_is_still_that_keys_own(fresh):
    """Review M5 stands: one 429 rotates to the next key of the same model."""
    t = fresh
    assert not t._refused_widely({"k1": ("rate_limit", 429)}, [f"k{i}" for i in range(14)])
    assert t._refused_widely({f"k{i}": ("rate_limit", 429) for i in range(7)}, [f"k{i}" for i in range(14)])
    assert not t._refused_widely({f"k{i}": ("rate_limit", 429) for i in range(6)}, [f"k{i}" for i in range(14)])
    assert not t._refused_widely({"a": ("server", 503), "b": ("rate_limit", 429)}, ["a", "b"]), "a small pool rests whole"
    four = ["a", "b", "c", "d"]
    assert t._refused_widely({k: ("server", 503) for k in "abc"}, four)
    assert not t._refused_widely({k: ("timeout", None) for k in "abcd"}, four), "timeouts keep their own rule (C9)"


def test_a_declined_hand_over_lets_kame_circle_itself(fresh, monkeypatch):
    """The two rules that waited on each other: the circle stood back because a later entry was free for Hermes,
    and the hand-over to Hermes was declined (the switch budget). Declined, KAME circles itself."""
    call = Owner({})
    call.identity = "gemini:gemini-4-hydrogen"
    monkeypatch.setattr(fresh, "_hermes_has_free_after", lambda *a, **k: True)
    assert fresh._wrap_target(call, "gemini:gemini-3.7-flash", seen=False) is None, "1.8.2.9 rule kept"
    target = fresh._wrap_target(call, "gemini:gemini-3.7-flash", seen=False, hand_over_declined=True,
                                visited=["gemini:gemini-4-hydrogen"])
    assert target is not None and not target[0].startswith("gemini:gemini-3.7-flash"), target


def test_a_raced_model_refused_429_is_not_written_busy_when_the_next_one_wins(fresh, monkeypatch):
    """1.8.4.2 (L1) stands when the next model of the list joins and wins: the raced model whose keys answered 429
    failed - it was not silent - so the winner does not write it in the record as busy or failing."""
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (0.3, 30.0))
    call = Owner({"gemini-3.8-flash": ("429",), "gemini-3.7-flash": ("fast", 0.05)})
    call.pool = R.Pool({k: [[60.0, R.chunk("late", finish="stop")]] * 40 for k in KEYS14})
    before = transport.HEALTH.summary("gemini:gemini-3.8-flash")
    out, err, stuck, took = run(fresh, call, limit=20.0)
    asked = list(dict.fromkeys(call.models_asked))
    assert not stuck and not err, (err, asked)
    assert R.shown(out) == "gemini-3.7-flash here", asked
    # the next model is raced at 12 s (a cold first-word bound); 3.7 joins right after 3.8's two 429s, not at 30 s
    assert took < 16.0, "the next model joined long before the slow bound"
    assert transport.HEALTH.summary("gemini:gemini-3.8-flash") == before
    assert "gemini:gemini-3.8-flash" not in fresh._busy_until


def test_a_raced_model_refused_503_is_remembered_busy_so_the_next_reply_skips_it(fresh, monkeypatch):
    """The other half of the rule above (fake-provider comparison, storm): a raced model whose keys answered 503 said
    BUSY. The first fix left every failed leg out, so no busy model of a walk was remembered and each reply of a storm
    walked the list again from the top (5-10 s a reply, 2 s in 1.9.0.5). A 503 leg is noted busy again."""
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (0.3, 30.0))
    call = Owner({"gemini-3.8-flash": ("503",), "gemini-3.7-flash": ("fast", 0.05)})
    call.pool = R.Pool({k: [[60.0, R.chunk("late", finish="stop")]] * 40 for k in KEYS14})
    out, err, stuck, took = run(fresh, call, limit=20.0)
    asked = list(dict.fromkeys(call.models_asked))
    assert not stuck and not err, (err, asked)
    assert R.shown(out) == "gemini-3.7-flash here", asked
    assert fresh._entry_busy("gemini:gemini-3.8-flash"), "the model that answered 503 is remembered busy"


def test_a_main_model_that_said_busy_is_not_beaten_early(fresh):
    """1.9.0.4 R3 ("answered inside the main model's usual time, so it is not slow") is about a main model still
    thinking. A main model whose own request answered 503 is busy, and the next reply starts past it."""
    call = Owner({"gemini-4-hydrogen": ("503",), "gemini-3.8-flash": ("fast", 0.05)})
    out, err, stuck, took = run(fresh, call, limit=20.0)
    assert not stuck and not err, err
    assert R.shown(out) == "gemini-3.8-flash here", call.models_asked
    assert fresh._entry_busy("gemini:gemini-4-hydrogen")
    second = Owner({"gemini-4-hydrogen": ("503",), "gemini-3.8-flash": ("fast", 0.05)})
    out, err, stuck, took = run(fresh, second, limit=20.0)
    assert R.shown(out) == "gemini-3.8-flash here"
    assert second.models_asked[0] == "gemini-3.8-flash", f"the next reply starts on 3.8: {second.models_asked}"


def test_a_throttled_pool_moves_the_reply_without_writing_the_model_busy(fresh, monkeypatch):
    """Half the pool answering 429 moves the reply (the trickle above) but is not a busy model: nothing is written
    in its record, so the next reply starts on it again. 503 on most keys still is (the provider's side)."""
    carousel = R.carousel_mod
    monkeypatch.setattr(carousel, "UNSIZED_THROTTLE_REST_S", 0.6)
    monkeypatch.setattr(carousel, "UNSIZED_BACKOFF_MAX_S", 0.6)
    monkeypatch.setattr(carousel, "RL_BACKOFF_CAP_S", 0.6)
    before = transport.HEALTH.summary("gemini:gemini-4-hydrogen")
    call = Owner({"gemini-4-hydrogen": ("429",), "gemini-3.8-flash": ("fast", 0.05)})
    out, err, stuck, took = run(fresh, call, limit=20.0)
    assert not stuck and not err, err
    assert R.shown(out) == "gemini-3.8-flash here"
    assert transport.HEALTH.summary("gemini:gemini-4-hydrogen") == before
    assert "gemini:gemini-4-hydrogen" not in fresh._busy_until
