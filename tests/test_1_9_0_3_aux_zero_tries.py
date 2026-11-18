"""1.9.0.3 (C8) - a background job stopped before any request was sent no longer says "timed out after 0 tries".

Evidence: audit 2026-11-17 P3, "timed out after 0 tries in 0s".
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import host_aux_predicates_20261015 as host  # noqa: E402

PACKAGE = "kame_aux_zero_pkg"


def _load():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, ROOT / "hermes-kame-api-rotation" / "__init__.py",
            submodule_search_locations=[str(ROOT / "hermes-kame-api-rotation")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
    return (__import__(PACKAGE + ".transport", fromlist=["x"]),
            __import__(PACKAGE + ".core.aux_budget", fromlist=["x"]))


def test_zero_tries_says_it_was_never_sent():
    _, aux = _load()
    text = aux.stop_message("title_generation", "gemini-3.8-flash", 0, 0.0)
    assert text == ("kame: background job title_generation on gemini-3.8-flash was stopped before it was sent"
                    " - its keys are kept for your chat")
    assert "timed out" not in text and "0 tries" not in text


def test_zero_tries_text_still_has_the_shape_the_host_wants():
    transport, aux = _load()
    exc = transport.KameAuxTimeout(aux.stop_message("title_generation", "gemini-3.8-flash", 0, 0.0))
    assert host.is_timeout_error(exc)
    assert not host.credential_rung_accepts(exc)
    assert not host.is_payment_error(exc) and not host.is_rate_limit_error(exc) and not host.is_auth_error(exc)
    text = str(exc).lower()
    for word in host.PAYMENT_KEYWORDS + host.RATE_LIMIT_KEYWORDS:
        assert word not in text, word


@pytest.mark.parametrize("tries", [1, 3])
def test_some_tries_keeps_the_old_words(tries):
    _, aux = _load()
    text = aux.stop_message("title_generation", "gemini-3.8-flash", tries, 29.6)
    assert f"timed out after {tries} tries in 29s" in text
