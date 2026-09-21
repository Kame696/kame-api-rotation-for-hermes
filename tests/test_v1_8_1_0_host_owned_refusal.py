"""A payload the host has its own contract for is left to the host.

Hermes 0.21.3 added the Nous welcome tier: an anonymous, single-credential
route whose 429 carries a structured body (``reason``, ``retry_after``,
``alternates``, ``upgrade_url``). The host parses it with
``hermes_cli.anon_auth.parse_welcome_refusal`` and rides the parsed refusal on
``error_context`` so the terminal can tell the user what happened and offer the
alternates.

Plugin hooks run *before* that pipeline and the first verdict wins, so when
KAME claimed the 429 the host's own reading never ran: measured with
``tools/host_corpus.py`` against upstream main, two of the host's corpus cases
lost ``welcome_refusal``. There is no pool to rotate on that route anyway.

The deferral is by evidence, not by name: the host's own parser decides whether
the body is its contract. On a Hermes without that parser nothing changes.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_host_owned_under_test"


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


pkg = _load_package()

WELCOME = {"status": 429, "message": "refused", "reason": "admission_closed", "retry_after": 30}
GEMINI = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Resource has been exhausted"}}


def _parse(body):
    if isinstance(body, dict) and body.get("reason") in {"at_capacity", "admission_closed", "rate_limited",
                                                          "model_not_free", "feature_not_free"}:
        return {"reason": body["reason"], "retry_after": int(body.get("retry_after") or 0)}
    return None


@pytest.fixture
def host_with_welcome_tier(monkeypatch):
    anon = types.ModuleType("hermes_cli.anon_auth")
    anon.parse_welcome_refusal = _parse
    parent = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.anon_auth", anon)
    monkeypatch.setattr(pkg, "_WELCOME_PARSER", pkg._UNRESOLVED, raising=False)
    yield
    monkeypatch.setattr(pkg, "_WELCOME_PARSER", pkg._UNRESOLVED, raising=False)


def _classify(body, message="HTTP 429"):
    return pkg._on_api_error_classification(
        provider="nous", model="nous/welcome", status_code=429, error_type="APIStatusError",
        error_code="", error_message=message, error_body=body, error=None,
    )


def test_a_payload_the_host_parses_as_its_own_is_left_to_it(host_with_welcome_tier):
    assert _classify(WELCOME, "Error code: 429 - rate_limited") is None


def test_every_other_429_is_still_read(host_with_welcome_tier):
    result = _classify(GEMINI, "HTTP 429 (RESOURCE_EXHAUSTED)")
    assert result is not None and result["reason"] == "rate_limit"


def test_a_host_without_the_parser_changes_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.anon_auth", None)
    monkeypatch.setattr(pkg, "_WELCOME_PARSER", pkg._UNRESOLVED, raising=False)
    result = _classify(WELCOME, "Error code: 429 - rate_limited")
    assert result is not None, "older Hermes: the body is an ordinary sized throttle, read as before"
