"""Synthetic replays of StepFun documented HTTP ownership, not live bodies."""
import pytest
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel

@pytest.mark.parametrize("status,message", [(451,"Content filtered"), (400,"Invalid parameter"), (404,"Request path not found")])
def test_request_failure_does_not_damage_credential(status,message):
    exc=Exception(message)
    exc.status_code=status
    engine=carousel.Carousel()
    result=dispatch.DispatchBinding(engine=engine)._on_failure("stepfun:m","a",exc,"test",1,False)
    assert result[0] == "raise"
    assert engine.rotations == 0
    assert not engine.is_retired("stepfun:m","a")
