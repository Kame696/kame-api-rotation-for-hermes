"""Sequential pool recovery with explicit retry hints and simulated latency."""
import pytest
from .test_v1_7_0_7_dispatch_ownership import carousel
from .test_v1_7_0_7_dispatch_ownership import dispatch

@pytest.mark.parametrize("count",[1,2,14])
@pytest.mark.parametrize("latency",[0.1,1.0])
def test_all_keys_rest_then_resume_at_first_deadline(count,latency):
    engine=carousel.Carousel();identity="gemini:model"
    keys=[str(i) for i in range(count)];now=100.0;attempts=[]
    for _ in keys:
        key,status=engine.select(identity,keys,now=now)
        assert status == "SUCCESS" and key not in attempts
        attempts.append(key)
        now+=latency  # A reply finishes before another attempt starts.
        engine.mark(identity,key,False,30,"rate_limit",now=now,stated=True)
    first_deadline=100+latency+30
    key,status=engine.select(identity,keys,now=now)
    assert status == "EXHAUSTED"
    assert key == attempts[0]
    assert engine.next_recovery_seconds(identity,keys,now=now) == pytest.approx(first_deadline-now)
    assert engine.select(identity,keys,now=first_deadline-0.001)[1] == "EXHAUSTED"
    key,status=engine.select(identity,keys,now=first_deadline)
    assert status == "SUCCESS" and key == attempts[0]
    engine.mark(identity,key,True,now=first_deadline)
    assert not engine.is_retired(identity,key)


@pytest.mark.parametrize("count", [2, 14])
def test_recovery_eta_ignores_retired_key_when_others_can_serve(count):
    engine = carousel.Carousel()
    identity = "gemini:model"
    keys = [str(i) for i in range(count)]
    engine.mark(identity, keys[0], False, 20, "revoked", now=100)
    for key in keys[1:]:
        engine.mark(identity, key, False, 60, "rate_limit", now=100, stated=True)
    assert engine.is_retired(identity, keys[0])
    assert engine.select(identity, keys, now=130)[1] == "EXHAUSTED"
    assert engine.next_recovery_seconds(identity, keys, now=130) == 30


def test_recovery_eta_keeps_all_retired_fallback_and_new_key():
    engine = carousel.Carousel()
    identity = "gemini:model"
    engine.mark(identity, "old", False, 20, "revoked", now=100)
    assert engine.next_recovery_seconds(identity, ["old"], now=101) == 19
    assert engine.next_recovery_seconds(identity, ["old"], now=120) is None
    assert engine.next_recovery_seconds(identity, ["old", "new"], now=101) is None


def test_available_count_matches_selection_with_retired_credentials():
    engine = carousel.Carousel()
    identity = "gemini:model"
    engine.mark(identity, "retired", False, 20, "revoked", now=100)
    engine.mark(identity, "valid", False, 60, "rate_limit", now=100, stated=True)
    assert engine.healthy_count(identity, ["retired", "valid"], now=130) == 0
    assert engine.healthy_count(identity, ["retired", "valid"], now=160) == 1
    assert engine.healthy_count(identity, ["retired"], now=130) == 1
    assert engine.healthy_count(identity, ["retired", "new"], now=130) == 1
    assert engine.healthy_count(identity, [""], now=130) == 0


def test_retired_peer_does_not_force_drop_rest_on_last_usable_key():
    engine = carousel.Carousel()
    identity = "gemini:model"
    engine.mark(identity, "retired", False, 20, "revoked", now=100)
    rest = dispatch._rest_unless_it_is_the_only_one(
        engine, identity, ["retired", "valid"], "valid", 30, "timeout"
    )
    assert rest == 0
