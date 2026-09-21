"""TokenHub-style source/type evidence belongs to the upstream, not relay key."""
import pytest
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel


@pytest.mark.parametrize("status", [401, 429, 502])
def test_explicit_upstream_owner_is_not_charged_to_relay(status):
    exc = Exception("Upstream request failed")
    exc.status_code = status
    exc.body = {"error":{"type":"upstream_error", "source":"upstream",
                         "upstream_status":status, "message":str(exc)}}
    engine = carousel.Carousel()
    result = dispatch.DispatchBinding(engine=engine)._on_failure("custom:m", "a", exc, "test", 1, False)
    assert result == ("raise", "upstream_error", status)
    assert engine.rotations == 0


def test_platform_invalid_key_is_not_mistaken_for_upstream_failure():
    exc = Exception("API key is invalid")
    exc.status_code = 401
    exc.body = {"error":{"type":"authentication_error", "source":"platform", "message":str(exc)}}
    engine = carousel.Carousel()
    result = dispatch.DispatchBinding(engine=engine)._on_failure("custom:m", "a", exc, "test", 1, False)
    assert result[0] == "rotate"
    assert engine.is_retired("custom:m", "a")
