"""1.8.1.2 regressions, fed by payloads recorded on the owner's own traffic."""
import time

import pytest

from tests.test_v1_8_1_0_backoff import dispatch_binding as D, Carousel


def error(status, message, body=None, name="RateLimitError"):
    exc = type(name, (Exception,), {})(message)
    exc.status_code = status
    exc.body = body
    return exc


def applied(exc, identity="fixture:model"):
    engine = Carousel()
    seen = []
    real = engine.mark
    engine.mark = lambda *a, **kw: seen.append(real(*a, **kw)) or seen[-1]
    action = D.DispatchBinding(engine=engine)._on_failure(identity, "fixture-key", exc, "fixture", 1, False)
    return action, seen[-1] if seen else None


# Recorded 2026-09-22 in refusals.jsonl (7 rows), ChatGPT-Plus Codex. The
# provider states when the plan's limit resets; 1.8.1.1 rested it 30s.
CODEX_BODY = {"type": "usage_limit_reached", "message": "The usage limit has been reached",
              "plan_type": "plus", "eligible_promo": None, "resets_in_seconds": 12660}


def test_codex_stated_reset_is_obeyed_up_to_the_ceiling():
    body = dict(CODEX_BODY, resets_at=int(time.time()) + 12660)
    exc = error(429, "Error code: 429 - " + str({"error": body}), body)
    action, rest = applied(exc, "openai-codex:gpt-5.6-luna")
    assert action[0] == "rotate"
    assert rest == pytest.approx(3600, abs=1)  # max_hold caps the stated 12660s, as 1.8.1.0 did


@pytest.mark.parametrize("message,seconds", [
    ("Rate limit exceeded. Please try again in 7s.", 7.0),
    ("Rate limit reached. Please try again in 1.5s.", 1.5),
    ("Too many requests. Please wait 12 seconds.", 12.0),
    ("Retry after 9s", 9.0),
])
def test_a_number_stated_in_prose_beats_the_unsized_dial(message, seconds):
    action, rest = applied(error(429, message))
    assert action[0] == "rotate"
    assert rest == pytest.approx(max(seconds, 1.0), abs=0.05)


def test_an_unsized_throttle_still_uses_the_dial():
    action, rest = applied(error(429, "Too Many Requests"))
    assert rest == pytest.approx(30, abs=0.05)


def test_stated_is_decided_by_the_one_vocabulary_function():
    from tests.test_v1_8_1_0_backoff import dispatch_binding as binding
    vocabulary = binding.vocabulary
    for source in ("body.resets_in_seconds", "header.retry-after", "text", "text.reset_at", "exception.retry_after"):
        assert vocabulary.provider_timed(source), source
    for source in ("window", "catalog", "pattern", "", None):
        assert not vocabulary.provider_timed(source), source


# --- H4: a billing refusal no clock fixes ends the turn on unanimity --------

from tests.test_v1_1_2 import Agent, KEYS, conversation, _binding as _turn_binding, answer  # noqa: E402


class _Refusal(Exception):
    def __init__(self, message, status_code, body):
        super().__init__(message)
        self.message, self.status_code, self.body = message, status_code, body


def _run(refusal, keys=KEYS[:2], answers_on=None):
    agent = Agent(keys)
    calls = []

    def host(agent_, api_kwargs, **kwargs):
        calls.append(agent_.api_key)
        if answers_on is not None and len(calls) == answers_on:
            return answer("fine")
        raise refusal()

    binding = _turn_binding()
    binding._sleep = lambda s: (_ for _ in ()).throw(AssertionError("the turn must not wait"))
    return binding, calls, lambda: binding.run(host, agent, conversation(), (), {})


def test_plan_without_the_service_ends_the_turn_once_every_key_said_so():
    body = {"error": {"type": "usage_not_included", "message": "Upgrade to Plus."}}
    binding, calls, go = _run(lambda: _Refusal("To use Codex with your plan, upgrade to Plus.", 429, body))
    with pytest.raises(_Refusal):
        go()
    assert len(calls) == 2 and binding.surfaced == 1


def test_country_without_billing_ends_the_turn_once_every_key_said_so():
    body = {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                      "message": "User location is not supported for the API use without a billing account linked."}}
    binding, calls, go = _run(lambda: _Refusal(body["error"]["message"], 400, body))
    with pytest.raises(_Refusal):
        go()
    assert len(calls) == 2


def test_an_empty_balance_still_waits_instead_of_ending_the_turn():
    body = {"error": {"type": "insufficient_quota", "message": "You exceeded your current quota, please check your plan and billing details."}}
    binding, calls, go = _run(lambda: _Refusal(body["error"]["message"], 429, body))
    with pytest.raises(AssertionError, match="must not wait"):
        go()  # reaching the wait IS the old, still-correct behaviour
