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
