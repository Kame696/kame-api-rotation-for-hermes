"""Classifications must survive the real dispatch terminal/fallback gates."""
from types import SimpleNamespace
import pytest
from tests.test_v1_8_1_0_backoff import dispatch_binding as D, Carousel


def error(status, message, body=None):
    exc = Exception(message)
    exc.status_code = status
    exc.body = body
    return exc


@pytest.mark.parametrize("status,message,body,kind,seconds", [
    (498, "Flex Tier Capacity Exceeded", None, "server", 1),
    (429, "To use Codex with your ChatGPT plan, upgrade to Plus.", {"error": {"type": "usage_not_included"}}, "insufficient_quota", 3600),
    (400, "User location is not supported for the API use without a billing account linked.", {"error": {"status": "FAILED_PRECONDITION"}}, "insufficient_quota", 3600),
    (400, "You have reached your specified API usage limits.", {"error": {"type": "invalid_request_error", "message": "You have reached your specified API usage limits."}}, "insufficient_quota", 3600),
    *[(403, text, {"error": {"message": text}}, "denied", 20) for text in (
        "Forbidden - insufficient permissions.",
        "Forbidden - key(sk-xx) not authorized to access the requested model.",
        "Forbidden - channel has been disabled.")],
    (429, "", None, "per_minute", 30),
])
def test_dispatch_parity(status, message, body, kind, seconds):
    engine = Carousel()
    observed = []
    original = engine.mark
    def mark(*a, **kw):
        value = original(*a, **kw); observed.append(value); return value
    engine.mark = mark
    action, actual, code = D.DispatchBinding(engine=engine)._on_failure(
        "fixture:model", "fixture-key", error(status, message, body), "fixture", 1, False)
    assert action != "raise" and actual == kind and code == status
    assert observed[-1] == pytest.approx(seconds, abs=.01)


def test_explicit_unsized_delay_is_not_replaced_by_dial():
    engine = Carousel()
    engine.unsized_throttle_rest_s = 41
    exc = error(429, "", None); exc.headers = {"Retry-After": "7"}
    seen = []
    real = engine.mark
    engine.mark = lambda *a, **kw: seen.append(real(*a, **kw)) or seen[-1]
    D.DispatchBinding(engine=engine)._on_failure("fixture:model", "key", exc, "fixture", 1, False)
    assert seen[-1] == pytest.approx(7, abs=.02)


def test_relayed_moderation_stays_terminal_without_benching():
    engine = Carousel()
    engine.mark = lambda *a, **kw: pytest.fail("terminal request must not bench a key")
    exc = error(403, "Request blocked: violence", {"error": {"metadata": {
        "provider_name": "OpenAI", "flagged_input": "fixture", "error_type": "content_policy_violation"}}})
    assert D.DispatchBinding(engine=engine)._on_failure("openrouter:model", "key", exc, "fixture", 1, False)[:2] == ("raise", "content_filter")
