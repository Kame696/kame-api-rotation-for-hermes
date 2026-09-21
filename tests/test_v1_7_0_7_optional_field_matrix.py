"""Malformed optional JSON values must not crash a recognized rate response."""
import pytest
from .test_v1_7_0_7_surfaces import classify,NOW

@pytest.mark.parametrize("field", ["details","metadata","retry_after","reset_at","quotaDimensions"])
@pytest.mark.parametrize("value", [None,True,False,0,-1,{},[],[None,{"x":[]}]])
def test_optional_json_shapes_preserve_rate_recovery(field,value):
    body={"error":{"type":"rate_limit_exceeded","message":"Rate limit exceeded",field:value}}
    result=classify(provider="unknown-new-provider",status_code=429,error_body=body,
        error_message="Rate limit exceeded",now_epoch=NOW)
    assert result is not None
    assert result.reason == "rate_limit"
    assert result.should_rotate_credential


@pytest.mark.parametrize("field", ["retry_after","reset_at"])
@pytest.mark.parametrize("value", [True,False,{},[],[None]])
def test_nonnumeric_timing_does_not_create_a_deadline(field,value):
    common=dict(provider="unknown-new-provider",status_code=429,
        error_message="Rate limit exceeded",now_epoch=NOW)
    base={"type":"rate_limit_exceeded","message":"Rate limit exceeded"}
    expected=classify(**common,error_body={"error":base})
    actual=classify(**common,error_body={"error":dict(base,**{field:value})})
    assert actual.reset_at == expected.reset_at
