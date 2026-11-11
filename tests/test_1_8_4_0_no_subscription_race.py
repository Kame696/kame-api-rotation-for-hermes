"""1.8.4.0 E3: a subscription credential is never raced as a backup.

The rule is on the credential's type (what the host records as ``auth_type``), never on
the provider. The primary key of a call is never filtered: a subscription answers its
own reply. Driven through the real transport with the stream-race fakes.
"""
from __future__ import annotations

import importlib
import os
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
settings = R.settings
KEYS = R.KEYS
PACKAGE = R.PACKAGE
chunk = R.chunk
facade = importlib.import_module(f"{PACKAGE}.facade")


class TypedCall(R.FakeCall):
    """The facade's rule, over entries the test hands in (``None`` = a key set by hand)."""

    def __init__(self, pool, types, keys=KEYS):
        super().__init__(pool, keys=keys)
        self.types = types

    def entry(self, key):
        kind = self.types.get(key)
        return None if kind is None else SimpleNamespace(id=key[-4:], auth_type=kind)

    def may_back_up(self, key):
        return facade._FacadeCall.may_back_up(self, key)


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (0.1, 0.2))
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "may_start_backup", lambda *a, **k: True)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()


def slow_primary(extra=None):
    plan = {KEYS[0]: [[0.8, chunk("slow", finish="stop")]]}
    for k in KEYS[1:]:
        plan[k] = [[0.02, chunk("fast", finish="stop")]]
    plan.update(extra or {})
    return R.Pool(plan)


def opened(pool):
    return [k for k, _ in pool.opened]


def test_two_subscriptions_get_no_backup(fresh):
    pool = slow_primary()
    types = {k: "oauth" for k in KEYS[:2]}
    types.update({k: "oauth" for k in KEYS[2:]})
    out, _ = R.run(fresh, TypedCall(pool, types))
    assert R.shown(out) == "slow"
    assert opened(pool) == [KEYS[0]]


def test_subscription_and_api_keys_only_the_api_key_is_raced(fresh):
    pool = slow_primary()
    types = {KEYS[0]: "api_key", KEYS[1]: "oauth", KEYS[2]: "api_key", KEYS[3]: "oauth"}
    out, _ = R.run(fresh, TypedCall(pool, types))
    assert R.shown(out) == "fast"
    assert KEYS[1] not in opened(pool) and KEYS[3] not in opened(pool)
    assert KEYS[2] in opened(pool)


def test_a_subscription_primary_still_answers_its_own_reply(fresh):
    pool = slow_primary()
    types = {KEYS[0]: "oauth", KEYS[1]: "api_key", KEYS[2]: "api_key", KEYS[3]: "api_key"}
    out, _ = R.run(fresh, TypedCall(pool, types))
    assert opened(pool)[0] == KEYS[0]
    assert R.shown(out) == "fast"          # the api keys beat it, the primary was never refused the right to start


def test_a_key_set_by_hand_is_raced(fresh):
    pool = slow_primary()
    out, _ = R.run(fresh, TypedCall(pool, {}))
    assert R.shown(out) == "fast"
    assert len(opened(pool)) >= 2


def test_empty_type_counts_as_a_plain_key():
    call = object.__new__(facade._FacadeCall)
    call.entry = lambda key: SimpleNamespace(auth_type="")
    assert call.may_back_up("k") is True
    call.entry = lambda key: SimpleNamespace(auth_type="oauth")
    assert call.may_back_up("k") is False
    call.entry = lambda key: None
    assert call.may_back_up("k") is True


def test_the_burst_path_skips_subscriptions(fresh, monkeypatch):
    pool = R.Pool({})
    types = {KEYS[0]: "api_key", KEYS[1]: "oauth", KEYS[2]: "api_key", KEYS[3]: "oauth"}
    call = TypedCall(pool, types)
    asked = []

    def client_for(key, attempt):
        asked.append(key)
        return key

    call.client_for = client_for
    monkeypatch.setattr(call.wire, "create", lambda client, request: "ok", raising=False)
    fresh._burst_create(call, R.IDENTITY, "x", 0, KEYS[0], list(KEYS), None, {},
                        legs=4, started=time.monotonic())
    assert asked and KEYS[1] not in asked and KEYS[3] not in asked
    assert KEYS[2] in asked
