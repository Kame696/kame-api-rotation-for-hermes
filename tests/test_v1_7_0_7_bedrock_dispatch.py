"""A Bedrock model-readiness429 must not become credential quota in fallback."""
from .test_v1_7_0_7_dispatch_ownership import dispatch,carousel


def test_model_not_ready_preserves_host_ownership():
    error_class=type("ModelNotReadyException",(Exception,),{})
    exc=error_class("The model specified in the request is not ready to serve inference requests.")
    exc.status_code=429
    exc.body={"message":str(exc)}
    engine=carousel.Carousel()
    before=engine.snapshot()
    result=dispatch.DispatchBinding(engine=engine)._on_failure("bedrock:m","a",exc,"test",1,False)
    assert result[0] == "raise"
    assert engine.snapshot() == before


def test_ordinary_bedrock_throttle_still_rotates():
    exc=Exception("Rate limit exceeded")
    exc.status_code=429
    exc.body={"error":{"type":"rate_limit_exceeded"}}
    engine=carousel.Carousel()
    result=dispatch.DispatchBinding(engine=engine)._on_failure("bedrock:m","a",exc,"test",1,False)
    assert result[0] == "rotate"
    assert engine.rotations == 1


def test_generic_aws_exception_preserves_structured_readiness_code():
    exc=Exception("Request declined")
    exc.status_code=429
    exc.response={"Error":{"Code":"ModelNotReadyException","Message":"Request declined"},
                  "ResponseMetadata":{"HTTPStatusCode":429}}
    engine=carousel.Carousel()
    before=engine.snapshot()
    result=dispatch.DispatchBinding(engine=engine)._on_failure("bedrock:m","a",exc,"test",1,False)
    assert result[:2] == ("raise","model_not_ready")
    assert engine.snapshot() == before


def test_aws_throttle_code_does_not_trigger_readiness_exception():
    exc=Exception("Rate limit exceeded");exc.status_code=429
    exc.response={"Error":{"Code":"ThrottlingException"}}
    engine=carousel.Carousel()
    result=dispatch.DispatchBinding(engine=engine)._on_failure("bedrock:m","a",exc,"test",1,False)
    assert result[0] == "rotate"
