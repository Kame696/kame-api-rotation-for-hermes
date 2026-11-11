"""1.8.4.0 round 2 (R1) - a reserve key is never a race leg on another model either.

E4 says a reserve key is never used for a backup request. The same-model backup and the burst filter it
(``_backup_keys``). The legs that ask ANOTHER model of the list - the cross-model race (1.8.2.8), the
widened race (1.8.2.9) and the start probe (1.8.2.9, E8 in the storm tail) - picked their key with
``select`` over every key, and ``select`` offers a reserve key when no other key of that model is
healthy. A race leg is a backup request, so it must not take one. Every key reserved still reads the
setting as not set, as everywhere else.
"""
from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

from tests import test_1_8_2_9_start_probe as S
from tests import test_1_8_4_0_storm_tail as T
from tests.test_1_8_2_9_start_probe import PRIMARY, Scripted, fresh, pool  # noqa: F401  (fixture by name)

R = S.R
settings = S.settings
transport = S.transport


def label(key: str) -> str:
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:6]


def reserve(monkeypatch, *labels):
    monkeypatch.setenv("KAME_KEY_RESERVE", ", ".join(labels))
    settings.forget()
    R.pacing_mod.PACER._synced_at.clear()


class Keyed(Scripted):
    """``Scripted``, and which key each model was asked with."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.keys_for = {}

    def client_for(self, key, attempt):
        inner = super().client_for(key, attempt)
        create = inner.chat.completions.create

        def wrapped(**request):
            self.keys_for.setdefault(request.get("model"), []).append(key)
            return create(**request)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=wrapped)))


def only_reserve_left(t, identity, reserve_key):
    """Every key of ``identity`` but the reserve one rests for a reason that is not quota pressure."""
    with t.engine._lock:
        t.engine._pool_for(identity, list(R.KEYS), time.time())   # the whole pool is known, the reserve key too
    for k in R.KEYS:
        if k != reserve_key:
            t.engine.mark(identity, k, False, 600.0, "server")


def test_no_start_probe_when_only_a_reserve_key_of_the_busy_model_is_free(fresh, monkeypatch):
    reserve(monkeypatch, label(R.KEYS[0]))
    T.ended_ago(fresh)                                   # E8: the tail, the probe is due
    only_reserve_left(fresh, PRIMARY, R.KEYS[0])
    assert fresh._start_probe_plan(R.FakeCall(pool()), PRIMARY, True, R.KEYS) is None
    call = Keyed(pool(), {"gemini-race": ("fast", 0.05), "gemini-race-two": ("fast", 0.3)})
    out = list(fresh.stream(call))
    # the tail as before E8: one sequential knock, the reply's own request - the only place E4
    # lets a reserve key answer when every other key of the model rests
    assert call.models_asked == ["gemini-race"], "no race leg went out"
    assert R.shown(out) == "gemini-race here"


def test_a_cross_model_leg_never_takes_a_reserve_key(fresh, monkeypatch):
    # the constants of test_1_8_2_9_silent_two_models: the next model joins at 0.3 s, slow bound 1.0 s
    monkeypatch.setattr(transport, "_SLOW_S", {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    reserve(monkeypatch, label(R.KEYS[0]))
    only_reserve_left(fresh, S.SECOND, R.KEYS[0])        # the next model: only its reserve key is free
    call = Keyed(pool(), {"gemini-race-two": ("fast", 0.05)})   # the best model is silent
    started = time.monotonic()
    try:
        list(fresh.stream(call))
    except Exception:
        pass
    early = [k for k in call.keys_for.get("gemini-race-two", [])
             if call.asked_at["gemini-race-two"] - started < 0.9]
    assert R.KEYS[0] not in early, "the next model was raced on its reserve key while the best one was asked"


def test_a_widen_past_the_slow_bound_joins_on_the_reserve_key_and_no_leg_is_cut(fresh, monkeypatch):
    """Round 2 review F1: R1 stays on the cross leg and the start probe, but the widen joins as it
    did before R1 - a refused widen fell through to the hand-over, which cut every running leg,
    marked the best model slow, and then took the same model's reserve key anyway."""
    monkeypatch.setattr(transport, "_SLOW_S", {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    reserve(monkeypatch, label(R.KEYS[0]))
    only_reserve_left(fresh, S.SECOND, R.KEYS[0])        # the next model: only its reserve key is free
    call = Keyed(pool(), {"gemini-race-two": ("fast", 0.05)})   # the best model is silent
    started = time.monotonic()
    out = list(fresh.stream(call))
    early = [k for k in call.keys_for.get("gemini-race-two", [])
             if call.asked_at["gemini-race-two"] - started < 0.6]
    assert not early, "the cross leg (before the slow bound) still never takes the reserve key"
    assert call.keys_for.get("gemini-race-two") == [R.KEYS[0]], "the widen joined, on the only key free"
    assert R.shown(out) == "gemini-race-two here"
    # the reply is the widened leg's own, in the same stream: no hand-over to Hermes (which raised
    # KamePoolResting here before F1) and every model was asked once, by its race leg
    assert sorted(call.models_asked) == ["gemini-race", "gemini-race-three", "gemini-race-two"], call.models_asked


def test_the_start_probe_still_runs_on_an_ordinary_key(fresh, monkeypatch):
    reserve(monkeypatch, label(R.KEYS[0]))
    T.ended_ago(fresh)
    call = Keyed(pool(), {"gemini-race-two": ("fast", 0.3)})
    list(fresh.stream(call))
    asked = call.keys_for.get("gemini-race", [])
    assert asked and R.KEYS[0] not in asked, "the probe goes out, on a key that is not the reserve"


def test_every_key_reserved_reads_the_setting_as_not_set_in_a_race_too(fresh, monkeypatch):
    reserve(monkeypatch, *[label(k) for k in R.KEYS])
    T.ended_ago(fresh)
    call = Keyed(pool(), {"gemini-race-two": ("fast", 0.3)})
    list(fresh.stream(call))
    assert call.keys_for.get("gemini-race"), "all reserved: the probe goes out as it did before E4"


def test_the_race_key_helper_filters_reserve_only():
    keys = list(R.KEYS)
    R.pacing_mod.PACER.clear()
    R.pacing_mod.PACER._setting_reserve = frozenset({label(keys[1]).lower()})
    try:
        got = transport._race_keys(keys)
        every = transport._race_keys(keys[1:2])
    finally:
        R.pacing_mod.PACER._setting_reserve = frozenset()
        R.pacing_mod.PACER.clear()
    assert got == [keys[0], keys[2], keys[3]], got
    assert every == [keys[1]], "only reserve keys left: the setting reads as not set"


# --- round 2 review F3: retirement is read over the whole pool -------------------------------------------------

def retire(t, identity, keys):
    """Retired for good (a revoked key), its rest long over."""
    with t.engine._lock:
        pool = t.engine._pool_for(identity, list(R.KEYS), time.time())
        for k in keys:
            pool[k]["retired"] = True
            pool[k]["sick_until"] = 0.0


def test_standing_drops_retired_keys_and_keeps_every_key_when_all_are_retired(fresh):
    retire(fresh, S.SECOND, R.KEYS[1:3])
    got = fresh.engine.standing(S.SECOND, list(R.KEYS))
    assert got == [R.KEYS[0], R.KEYS[3]], got
    retire(fresh, S.SECOND, [R.KEYS[0], R.KEYS[3]])
    assert fresh.engine.standing(S.SECOND, list(R.KEYS)) == list(R.KEYS), "the escape select has"
    assert fresh.engine.standing("gemini:never-seen", list(R.KEYS)) == list(R.KEYS)


def test_race_ready_is_false_when_every_ordinary_key_is_retired_and_only_the_reserve_stands(fresh, monkeypatch):
    reserve(monkeypatch, label(R.KEYS[0]))
    retire(fresh, S.SECOND, R.KEYS[1:])                   # the ordinary keys: retired, their rest is over
    assert fresh._race_ready(S.SECOND, R.KEYS) is False
    T.ended_ago(fresh)
    # the best model too: its ordinary keys are retired, so no probe leg is planned
    retire(fresh, S.PRIMARY, R.KEYS[1:])
    assert fresh._start_probe_plan(R.FakeCall(pool()), S.PRIMARY, True, R.KEYS) is None


def test_race_ready_stays_true_with_a_standing_ordinary_key(fresh, monkeypatch):
    reserve(monkeypatch, label(R.KEYS[0]))
    retire(fresh, S.SECOND, R.KEYS[1:3])                  # one ordinary key (the last) still stands
    assert fresh._race_ready(S.SECOND, R.KEYS) is True


# --- round 2 review F4: every race leg says what kind it is (the chaos tool's I3b reads it) -----------------

def record_legs(monkeypatch):
    legs = []
    real = transport._StreamLeg.start

    def start(self, wire, request):
        legs.append((request.get("model"), getattr(self, "race_kind", "unset")))
        return real(self, wire, request)

    monkeypatch.setattr(transport._StreamLeg, "start", start)
    return legs


def test_legs_carry_their_kind_cross_probe_widen_and_none_for_the_replys_own(fresh, monkeypatch):
    legs = record_legs(monkeypatch)
    # cross: the next model joins a silent reply at 0.3 s
    monkeypatch.setattr(transport, "_SLOW_S", {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)})
    monkeypatch.setattr(transport, "_CROSS_COLD_S", 0.3)
    monkeypatch.setattr(transport, "_CROSS_STEP_S", 0.4)
    monkeypatch.setattr(transport, "_CROSS_MIN_S", 0.3)
    list(fresh.stream(Keyed(pool(), {"gemini-race-two": ("fast", 0.05)})))
    assert ("gemini-race", None) in legs and ("gemini-race-two", "cross") in legs, legs
    # probe: the tail of a window asks the best model beside the next one
    del legs[:]
    T.ended_ago(fresh)
    list(fresh.stream(Keyed(pool(), {"gemini-race-two": ("fast", 0.3)})))
    assert ("gemini-race-two", None) in legs and ("gemini-race", "probe") in legs, legs
    # widen: the cross launch of the next model was refused (R1), the widen takes it at the slow bound
    del legs[:]
    fresh2 = transport.KameTransport(engine=R.carousel_mod.Carousel())
    reserve(monkeypatch, label(R.KEYS[0]))
    only_reserve_left(fresh2, S.SECOND, R.KEYS[0])
    list(fresh2.stream(Keyed(pool(), {"gemini-race-two": ("fast", 0.05)})))
    assert ("gemini-race-two", "widen") in legs and ("gemini-race-three", "cross") in legs, legs
    assert ("gemini-race", None) in legs, legs
