"""An opaque authentication label is an ordinary dead credential, on every
surface except the one where the host can genuinely refresh it.

RED_TEAM.md F1 (2026-09-19): this file originally claimed the opposite for
every surface — "an authentication family does not establish permanent
credential death" — by routing these codes through core.catalog's generic
table into the new AUTH_REFRESH family. Measured cost: a real Gemini 401
("Request had invalid authentication credentials.", Google's own standard
UNAUTHENTICATED wording) stopped rotating on a plain API-key surface and
ended the turn instead, with sixteen healthy keys left untried — a
regression against invariant I3 (nothing KAME does may end worse than not
trying) that no decisions/ entry recorded.

The AUTH_REFRESH family itself is real and stays: a surface where the host
can genuinely refresh an OAuth session, not replace a key, should go back to
it instead of retiring a credential. That is now scoped to exactly one
place, core/provider_rules.py's route-scoped google_vertex rule (covered by
test_v1_7_0_7_oauth_ownership.py) — unreachable from the generic identity
used below ("custom:m", no request URL attached). Everywhere else, these
codes are back to being an ordinary dead credential: rotate immediately, the
same as 1.7.0.5 before this family existed.
"""
import pytest
from .test_v1_7_0_7_dispatch_ownership import dispatch, carousel


@pytest.mark.parametrize("code", ["authentication_error", "authentication", "invalid_authentication", "UNAUTHENTICATED", "gemini_unauthorized"])
def test_opaque_auth_family_on_a_generic_surface_rotates_and_retires(code):
    exc = Exception("Authentication failed")
    exc.status_code = 401
    exc.body = {"error":{"type":code, "message":"Authentication failed"}}
    engine = carousel.Carousel()
    result = dispatch.DispatchBinding(engine=engine)._on_failure("custom:m", "a", exc, "test", 1, False)
    assert result[0] == "rotate"
    assert engine.is_retired("custom:m", "a")


def test_explicit_invalid_key_still_rotates_as_permanent():
    exc = Exception("API key is invalid.")
    exc.status_code = 401
    exc.body = {"error":{"type":"authentication_error", "message":str(exc)}}
    engine = carousel.Carousel()
    result = dispatch.DispatchBinding(engine=engine)._on_failure("custom:m", "a", exc, "test", 1, False)
    assert result[0] == "rotate"
    assert engine.is_retired("custom:m", "a")


@pytest.mark.parametrize("code,kind", [("invalid_api_key", "authentication_error"), ("authentication_error", "invalid_api_key")])
def test_explicit_invalid_key_code_outranks_opaque_auth_type(code, kind):
    exc = Exception("Request declined")
    exc.status_code = 401
    exc.body = {"error":{"type":kind, "code":code, "message":str(exc)}}
    engine = carousel.Carousel()
    result = dispatch.DispatchBinding(engine=engine)._on_failure("custom:m", "a", exc, "test", 1, False)
    assert result[0] == "rotate"
    assert engine.is_retired("custom:m", "a")
