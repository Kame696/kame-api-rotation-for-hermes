"""Another account answering does not rescind a server retry instruction."""
import pytest
from .test_v1_7_0_7_dispatch_ownership import carousel


@pytest.mark.parametrize("count", [2, 14])
def test_peer_success_preserves_stated_server_deadline(count):
    engine = carousel.Carousel()
    engine.select("p:m", [str(n) for n in range(count)], now=100)
    engine.mark("p:m", "1", False, 60, "server", now=100, stated=True)
    engine.mark("p:m", "0", True, now=101)
    engine.thaw_server_cooled("p:m", "0", now=101)
    assert engine._pools["p:m"]["1"]["sick_until"] == 160


def test_own_success_clears_previous_server_instruction():
    engine = carousel.Carousel()
    engine.mark("p:m", "a", False, 60, "server", now=100, stated=True)
    engine.mark("p:m", "a", True, now=101)
    assert engine.next_recovery_seconds("p:m", ["a"], now=101) is None


def test_server_instruction_is_not_truncated_to_ninety_seconds():
    engine = carousel.Carousel()
    assert engine.mark("p:m", "a", False, 300, "server", now=100, stated=True) == 300
