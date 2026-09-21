"""A successful neighbor does not restore this independent account's quota."""
import pytest
from .test_v1_7_0_7_long_running import Carousel


@pytest.mark.parametrize("kind", ["rate_limit", "daily", "denied", "insufficient_quota"])
@pytest.mark.parametrize("order", ["server_first", "quota_first"])
def test_outage_recovery_cannot_erase_an_active_non_server_hold(kind, order):
    engine = Carousel()
    if order == "server_first":
        engine.mark("p:m", "a", False, 1, "server", now=100)
        engine.mark("p:m", "a", False, 60, kind, now=102)
    else:
        engine.mark("p:m", "a", False, 60, kind, now=100)
        engine.mark("p:m", "a", False, 1, "server", now=102)
    before = engine._pools["p:m"]["a"]["sick_until"]
    engine.mark("p:m", "b", True, now=103)
    assert engine.thaw_server_cooled("p:m", "b", now=103) == 0
    assert engine._pools["p:m"]["a"]["sick_until"] == before


def test_expired_quota_does_not_prevent_later_server_recovery():
    engine = Carousel()
    engine.mark("p:m", "a", False, 10, "rate_limit", now=100)
    engine.mark("p:m", "a", False, 60, "server", now=120, stated=False)
    assert engine.thaw_server_cooled("p:m", "b", now=122) == 1
    assert engine.next_recovery_seconds("p:m", ["a"], now=130) is None


def test_own_success_clears_old_quota_protection():
    engine = Carousel()
    engine.mark("p:m", "a", False, 60, "rate_limit", now=100)
    engine.mark("p:m", "a", True, now=110)
    engine.mark("p:m", "a", False, 60, "server", now=111, stated=False)
    assert engine.thaw_server_cooled("p:m", "b", now=112) == 1
