"""1.8.4.0 E4: reserve keys are the last to be used.

``key_reserve`` is a comma list of the short labels the panel shows. A reserve key is a
candidate only when no other key of the model is healthy, and it is never asked as a
backup. A label that matches nothing is ignored. All keys reserved behaves as today.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

settings = R.settings
carousel_mod = R.carousel_mod
pacing_mod = R.pacing_mod
transport = R.transport
KEYS = R.KEYS
IDENTITY = R.IDENTITY


def label(key: str) -> str:
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:6]


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    pacing_mod.PACER.clear()
    R.hedging_mod.HEDGE_TRACKER.clear()
    yield monkeypatch
    settings.forget()
    pacing_mod.PACER.clear()
    R.hedging_mod.HEDGE_TRACKER.clear()


def reserve(monkeypatch, *labels):
    monkeypatch.setenv("KAME_KEY_RESERVE", ", ".join(labels))
    settings.forget()
    pacing_mod.PACER._synced_at.clear()


def picks(car, n, keys=KEYS):
    return [car.select(IDENTITY, keys)[0] for _ in range(n)]


def test_setting_is_declared_with_its_words():
    row = settings.describe(settings.KEY_RESERVE)
    assert row["key"] == "key_reserve"
    assert row["title"] == "Reserve keys"
    assert row["help"] == ("Keys listed here are used only when every other key of that model cannot "
                           "answer, and are never used for a backup request. Put your paid keys here "
                           "to spend your free quota first.")
    assert row["advanced"] is True and row["default"] == ""
    assert row["env"] == "KAME_KEY_RESERVE"


def test_setting_is_read_but_never_shown():
    """Owner, 2026-11-08: a box to fill in before it does anything is not shown (the 1.8.2.7
    ``KEY_QUOTA_GROUPS`` precedent). It is still read, from the config and the environment."""
    assert settings.KEY_RESERVE in settings.HIDDEN
    assert settings.KEY_RESERVE not in {row["key"] for row in settings.describe_all()}
    assert all(settings.KEY_RESERVE not in shelf["keys"] for shelf in settings.groups())
    assert settings.KEY_RESERVE in settings.ALL_STRINGS


def test_never_picked_while_another_is_healthy(fresh):
    reserve(fresh, label(KEYS[0]))
    car = carousel_mod.Carousel()
    got = picks(car, 12)
    assert KEYS[0] not in got


def test_picked_when_the_others_rest(fresh):
    reserve(fresh, label(KEYS[0]))
    car = carousel_mod.Carousel()
    for k in KEYS[1:]:
        car.mark(IDENTITY, k, False, 600.0, "throttle")
    key, status = car.select(IDENTITY, KEYS)
    assert (key, status) == (KEYS[0], "SUCCESS")


def test_the_six_hex_digits_alone_also_name_a_key(fresh):
    reserve(fresh, label(KEYS[0])[4:])
    car = carousel_mod.Carousel()
    assert KEYS[0] not in picks(car, 8)


def test_an_unknown_label_is_harmless(fresh):
    reserve(fresh, "key:ffffff", "nonsense")
    car = carousel_mod.Carousel()
    assert set(picks(car, 12)) == set(KEYS)


def test_all_keys_reserved_behaves_as_today(fresh):
    reserve(fresh, *[label(k) for k in KEYS])
    car = carousel_mod.Carousel()
    got = picks(car, 12)
    assert set(got) == set(KEYS)


def test_an_edit_applies_without_a_restart(fresh):
    car = carousel_mod.Carousel()
    assert KEYS[0] in picks(car, 8)
    reserve(fresh, label(KEYS[0]))
    car2 = carousel_mod.Carousel()
    assert KEYS[0] not in picks(car2, 8)
    reserve(fresh)   # the entry is deleted again
    car3 = carousel_mod.Carousel()
    assert KEYS[0] in picks(car3, 8)


def test_a_reserve_key_serving_a_reply_is_counted_like_any_other(fresh):
    reserve(fresh, label(KEYS[0]))
    car = carousel_mod.Carousel()
    for k in KEYS[1:]:
        car.mark(IDENTITY, k, False, 600.0, "throttle")
    car.select(IDENTITY, KEYS)
    assert car.healthy_count(IDENTITY, KEYS) >= 1
    assert len(car._pool_for(IDENTITY, KEYS, time.time())[KEYS[0]]["request_log"]) == 1


def test_never_a_backup(fresh):
    reserve(fresh, label(KEYS[1]), label(KEYS[2]))
    call = SimpleNamespace(may_back_up=lambda k: True)
    # the pacer has to know the setting: select() syncs it, as every real call does
    carousel_mod.Carousel().select(IDENTITY, KEYS)
    assert transport._backup_keys(call, KEYS[1:], KEYS) == [KEYS[3]]


def test_backup_filter_leaves_everything_when_all_are_reserved(fresh):
    reserve(fresh, *[label(k) for k in KEYS])
    carousel_mod.Carousel().select(IDENTITY, KEYS)
    call = SimpleNamespace(may_back_up=lambda k: True)
    assert transport._backup_keys(call, KEYS[1:], KEYS) == KEYS[1:]


def test_the_race_never_opens_a_reserve_key(fresh):
    reserve(fresh, label(KEYS[1]), label(KEYS[2]))
    monkeypatch = fresh
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (0.1, 0.2))
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "may_start_backup", lambda *a, **k: True)
    # KEYS[0] is the primary; make it slow so backups are wanted
    pool = R.Pool({KEYS[0]: [[0.8, R.chunk("slow", finish="stop")]],
                   KEYS[3]: [[0.02, R.chunk("fast", finish="stop")]]})
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    # the primary must not be a reserve key; pin the pick
    monkeypatch.setattr(t.engine, "select", _pin_first(t.engine.select))
    out, _ = R.run(t, R.FakeCall(pool))
    opened = [k for k, _s in pool.opened]
    assert KEYS[1] not in opened and KEYS[2] not in opened


def _pin_first(real):
    state = {"first": True}

    def select(identity, keys, *a, **k):
        if state["first"]:
            state["first"] = False
            return KEYS[0], "SUCCESS"
        return real(identity, keys, *a, **k)
    return select
