"""1.8.1.4 -- a refusal of any shape is read, never crashed on.

A differential fuzz (26,164 runs: every refusal this suite hands the
classifier, each field swapped for odd types, through both ports' whole
failure path) found one place that raised: ``core/evidence.py`` walked
``error.details`` with ``for member in inner.get("details") or []``, so a body
carrying ``"details": 1`` (or ``true``, ``1.5``, ``Infinity``) raised TypeError
inside the dispatch failure handler. That handler runs in the ``except`` around
the host's API call, so the user's turn died on a KAME TypeError instead of the
key being rotated. ``classify`` already tolerated those shapes; the harvest in
front of it did not.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1814_robustness_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
evidence = importlib.import_module(f"{PACKAGE}.core.evidence")
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")

ODD_DETAILS = [1, -1, 0, 1.5, float("inf"), True, False, "some text", {"reason": 3}, None]


class _Refusal(Exception):
    status_code = 429

    def __init__(self, details):
        super().__init__("Rate limit exceeded")
        self.body = {"error": {"type": "rate_limit_exceeded", "details": details}}


@pytest.mark.parametrize("details", ODD_DETAILS, ids=repr)
def test_harvest_reads_any_details_shape(details):
    ev = evidence.harvest(_Refusal(details))
    assert ev.status_code == 429


@pytest.mark.parametrize("details", ODD_DETAILS, ids=repr)
def test_the_failure_handler_rotates_instead_of_raising(details):
    binding = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    verdict, kind, status = binding._on_failure(
        "openai:gpt-x", "sk-robustness-0001", _Refusal(details), "x", 1, False
    )
    assert verdict == "rotate"
    assert status == 429


def test_an_error_info_reason_is_still_found_in_a_real_details_list():
    exc = _Refusal([
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"},
    ])
    notes = []
    _details, reason = evidence._read_details(exc, exc.body, notes)
    assert reason == "RATE_LIMIT_EXCEEDED"
    assert "reason:body" in notes


# ---------------------------------------------------------------------------
# A moderation refusal named in the provider's own code field is the request's
# fault on any status -- never a key to rest and a prompt to resend.
# ---------------------------------------------------------------------------


class _Coded(Exception):
    def __init__(self, message, status, code):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": code, "message": message}}


@pytest.mark.parametrize("status,code", [
    (403, "content_policy_violation"),   # OpenAI moderation (the case that rotated)
    (400, "content_policy_violation"),
    (400, "content_filter"),             # Azure OpenAI
    (403, "ContentFilter"),
])
def test_a_coded_content_policy_refusal_is_handed_back_not_rotated(status, code):
    binding = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    verdict, kind, got = binding._on_failure(
        "openai:gpt-x", "sk-robustness-0002", _Coded("Your request was flagged", status, code),
        "x", 1, False,
    )
    assert (verdict, kind, got) == ("raise", "content_filter", status)


def test_a_key_denial_that_merely_says_blocked_by_still_rotates():
    # The prose indicators ("blocked by", "safety") also appear in key
    # denials; only a structured field may call the request the fault.
    binding = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    verdict, kind, _ = binding._on_failure(
        "openai:gpt-x", "sk-robustness-0003",
        _Coded("API key blocked by admin", 403, "permission_denied"), "x", 1, False,
    )
    assert verdict == "rotate"


class _Worded(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        if status is not None:
            self.status_code = status


@pytest.mark.parametrize("message,status", [
    ("The prompt was blocked by the safety filter", 403),   # rotated as auth before 1.8.1.4
    ("The response was blocked by the content filter", 403),
    ("response blocked by safety filter", None),
])
def test_a_worded_block_of_the_request_is_handed_back_even_on_a_403(message, status):
    binding = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    verdict, _kind, _ = binding._on_failure(
        "openai:gpt-x", "sk-robustness-0004", _Worded(message, status), "x", 1, False,
    )
    assert verdict == "raise"


@pytest.mark.parametrize("message", [
    "API key blocked by admin",
    "Your access is blocked by your organization's policy",
    "Your API key was suspended for violating our content policy",
    "Organization safety settings prevent this key from calling the model",
])
def test_a_bare_403_key_denial_in_moderation_words_still_rotates(message):
    binding = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    verdict, _kind, _ = binding._on_failure(
        "openai:gpt-x", "sk-robustness-0005", _Worded(message, 403), "x", 1, False,
    )
    assert verdict == "rotate"
