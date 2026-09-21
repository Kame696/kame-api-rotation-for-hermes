"""Offline sequential clock tests: no network, sockets, sleeps or paid models."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"))
from core.carousel import Carousel, HARD_DELAY_CAP_S, SERVER_BASE_S
from core.escalate import factor_for, MAX_FACTOR


def test_denial_escalation_survives_thousands_of_reprobes():
    engine = Carousel()
    now = 1000000.0
    for _ in range(2100):
        hold = engine.mark("provider:model", "key", False, 0, "denied", now=now)
        assert math.isfinite(hold) and 0 < hold <= engine.daily_cooldown_s
        now += hold + 0.001
    assert not engine.is_retired("provider:model", "key")


def test_factor_saturates_for_long_running_counter():
    assert factor_for(100000) == MAX_FACTOR


@pytest.mark.parametrize("count", [1, 2, 14])
def test_sequential_independent_deadlines_offer_first_available_key(count):
    engine = Carousel()
    keys = [f"account-{i}" for i in range(count)]
    now = 1000000.0
    identity = "provider:model"
    deadlines = {}
    # Deliberately make the first offered key recover last.
    for i, key in enumerate(keys):
        hold = (count-i)*7.0
        deadlines[key] = now+hold
        engine.mark(identity, key, False, hold, "rate_limit", stated=True, now=now)
    chosen, status = engine.select(identity, keys, now=now)
    assert status == "EXHAUSTED"
    assert deadlines[chosen] == min(deadlines.values())
    chosen, status = engine.select(identity, keys, now=min(deadlines.values()))
    assert status == "SUCCESS" and deadlines[chosen] == min(deadlines.values())
    # Success is per account/model: it must not wipe another account's quota.
    engine.mark(identity, chosen, True, now=min(deadlines.values()))
    for key in keys:
        if key != chosen:
            assert engine._pools[identity][key]["sick_until"] == deadlines[key]


@pytest.mark.parametrize("count", [1, 2, 14])
@pytest.mark.parametrize("kind", ["server", "rate_limit", "daily"])
def test_recovery_after_days_does_not_require_pool_reset(count, kind):
    engine = Carousel()
    keys = [f"account-{i}" for i in range(count)]
    identity, now = "provider:model", 1000000.0
    turns = 0
    while turns < count*20:
        key, status = engine.select(identity, keys, now=now)
        if status == "EXHAUSTED":
            deadline = engine._pools[identity][key]["sick_until"]
            assert deadline > now
            now = deadline
            continue
        assert status == "SUCCESS"
        hold = engine.mark(identity, key, False, 0, kind, now=now)
        assert 0 < hold <= HARD_DELAY_CAP_S
        now += 0.01  # sequential response latency, no simultaneous calls
        turns += 1
    now += 3*86400
    key, status = engine.select(identity, keys, now=now)
    assert status == "SUCCESS"
    engine.mark(identity, key, True, now=now)
    assert not engine.is_retired(identity, key)
