"""Learned retry numbers belong to one credential/model, not other accounts."""
from .test_v1_7_0_7_long_running import Carousel
from core.carousel import UNSIZED_THROTTLE_REST_S, MIRROR_GRACE_S
import pytest


def test_independent_account_does_not_borrow_a_retry_number():
    engine = Carousel()
    engine.mark("p:m", "a", False, 180, "rate_limit", now=100, stated=True)
    assert engine.mark("p:m", "b", False, 0, "rate_limit", now=101) == UNSIZED_THROTTLE_REST_S


def test_invented_wait_does_not_become_provider_evidence():
    engine = Carousel()
    engine.mark("p:m", "a", False, 180, "rate_limit", now=100)
    assert engine.mark("p:m", "a", False, 0, "rate_limit", now=300) == UNSIZED_THROTTLE_REST_S


@pytest.mark.parametrize("reset", ["identity", "all", "success", "removed"])
def test_learned_number_ends_with_its_evidence_episode(reset):
    engine = Carousel()
    engine.mark("p:m", "a", False, 180, "rate_limit", now=100, stated=True)
    engine.mark("q:m", "b", False, 90, "rate_limit", now=100, stated=True)
    if reset == "identity":
        engine.forget("p:m")
    elif reset == "all":
        engine.forget()
    elif reset == "success":
        engine.mark("p:m", "a", True, now=300)
    else:
        engine.select("p:m", ["c"], now=100+MIRROR_GRACE_S+1)
    now = 100+MIRROR_GRACE_S+2
    assert engine.mark("p:m", "a", False, 0, "rate_limit", now=now) == UNSIZED_THROTTLE_REST_S
    if reset != "all":
        assert engine.mark("q:m", "b", False, 0, "rate_limit", now=now) == 90
