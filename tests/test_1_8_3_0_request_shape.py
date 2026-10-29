"""1.8.3.0 (PLAN_1.8.3.0 D3, decisions/0033) - a request-shape 400 is not a key fault.

The lines are the real ones NVIDIA sent (``tests/fixtures/nvidia_refusals_20261016.json``, taken from the
recorded refusals): ``400 Validation: Unsupported parameter(s): `reasoning```. No other key would answer
differently, so the first key's answer goes back to Hermes: no rest on the key, no rotation. The NVIDIA
402 "credits exhausted" is a spent account: ``insufficient_quota`` in the carousel as well as in the hook.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests import test_1_8_2_7_handover as H

transport = H.transport
carousel = H.carousel_mod
pkg = H.pkg
classify_mod = __import__(f"{H.PACKAGE}.core.classify", fromlist=["x"])

FIXTURE = Path(__file__).parent / "fixtures" / "nvidia_refusals_20261016.json"
RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))["recorded"]


class BadRequestError(Exception):
    """The shape of the SDK error the recorded lines came from."""

    def __init__(self, status, message, body):
        super().__init__(message)
        self.status_code = status
        self.message = message
        self.body = body


def error_of(row):
    body = row["body"]
    return BadRequestError(row["status"], row["message"], json.loads(body) if isinstance(body, str) else body)


def key_state(t, identity, key):
    return t.engine._pools.get(identity, {}).get(key, {})


def test_the_fixture_holds_the_recorded_lines_and_no_key():
    assert len(RECORDED) == 4 and {row["status"] for row in RECORDED} == {400}
    text = FIXTURE.read_text(encoding="utf-8")
    assert "Unsupported parameter(s)" in text
    assert "nvapi-" not in text and "Bearer" not in text


@pytest.mark.parametrize("index", range(4))
def test_a_recorded_nvidia_400_goes_to_hermes_after_the_first_key(index):
    t = transport.KameTransport(engine=carousel.Carousel())
    verdict, kind, status = t._on_failure("nvidia:z-ai/glm-5.3:global", "key-1", error_of(RECORDED[index]),
                                          "label", 1, False)
    assert (verdict, status) == ("raise", 400), "the request was refused, not the key: no second key is asked"
    assert not key_state(t, "nvidia:z-ai/glm-5.3:global", "key-1").get("sick_until"), "and the key does not rest"


@pytest.mark.parametrize("message,body", [
    ("Unrecognized request argument supplied: foo",
     {"error": {"message": "Unrecognized request argument supplied: foo", "type": "invalid_request_error"}}),
    ("Unknown parameter: 'foo'",
     {"error": {"message": "Unknown parameter: 'foo'", "type": "invalid_request_error", "param": "foo"}}),
    ("Invalid request", {"error": {"message": "Invalid request", "type": "invalid_request_error"}}),
])
def test_other_providers_request_shape_errors_read_the_same_way(message, body):
    t = transport.KameTransport(engine=carousel.Carousel())
    error = BadRequestError(400, f"Error code: 400 - {message}", body)
    verdict, _kind, _status = t._on_failure("openai:gpt-x", "key-1", error, "label", 1, False)
    assert verdict == "raise"
    assert not key_state(t, "openai:gpt-x", "key-1").get("sick_until")


def test_a_400_that_names_the_key_is_still_a_key_fault():
    t = transport.KameTransport(engine=carousel.Carousel())
    error = BadRequestError(400, "API key not valid. Please pass a valid API key. INVALID_ARGUMENT", {})
    verdict, kind, _status = t._on_failure("gemini:g", "key-1", error, "label", 1, False)
    assert verdict == "rotate" and kind == "revoked"


def test_the_hook_leaves_the_recorded_400_to_the_host_and_changes_nothing_about_its_reason():
    row = RECORDED[0]
    out = pkg._on_api_error_classification(
        provider="nvidia", model="z-ai/glm-5.3", status_code=400, error_message=row["message"],
        error_body=json.loads(row["body"]), error=error_of(row), error_type="BadRequestError")
    assert out is None, "KAME has no opinion on a request-shape 400: Hermes' own reading stands"


# --- the 402 -------------------------------------------------------------------------------------------
PAYMENT = [
    (402, "Error code: 402 - {'message': 'Payment Required: credits exhausted', 'type': 'payment_required'}"),
    (402, "Payment Required"),
    (402, "your credits exhausted for this account"),
]


@pytest.mark.parametrize("status,message", PAYMENT)
def test_a_402_payment_required_is_a_spent_account(status, message):
    delay, kind, got = carousel.classify(None, message, status)
    assert kind == "insufficient_quota" and got == 402
    assert delay >= 3600.0, "an account that needs paying does not recover in seconds"


def test_credits_exhausted_without_a_402_status_reads_the_same_in_the_carousel():
    _delay, kind, _status = carousel.classify(None, "Payment Required: credits exhausted", None)
    assert kind == "insufficient_quota"


def test_the_hook_and_the_carousel_now_agree_on_the_402():
    verdict = classify_mod.classify(
        provider="nvidia", status_code=402, error_message="Error code: 402 - Payment Required: credits exhausted",
        error_body={"message": "Payment Required: credits exhausted", "type": "payment_required"})
    assert verdict is not None and verdict.reason == "billing"
    _delay, kind, _status = carousel.classify(None, "Payment Required: credits exhausted", 402)
    assert kind == "insufficient_quota"


def test_a_402_on_the_failure_path_rests_the_key_and_rotates():
    t = transport.KameTransport(engine=carousel.Carousel())
    error = BadRequestError(402, "Error code: 402 - Payment Required: credits exhausted",
                            {"message": "Payment Required: credits exhausted", "type": "payment_required"})
    verdict, kind, status = t._on_failure("nvidia:glm", "key-1", error, "label", 1, False)
    assert (verdict, kind, status) == ("rotate", "insufficient_quota", 402)
    assert key_state(t, "nvidia:glm", "key-1").get("sick_until", 0) > time.time() + 3000


def test_a_429_and_a_503_are_untouched():
    assert carousel.classify(None, "Too many requests", 429)[1] == "per_minute"
    assert carousel.classify(None, "overloaded", 503)[1] == "server"
