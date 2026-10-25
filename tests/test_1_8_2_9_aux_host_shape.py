"""1.8.2.9 / decisions/0032 A5 - the error a stopped background job raises has the shape the host wants.

The predicates are a TEXT copy of the host's (tests/fixtures/host_aux_predicates_20261015.py); the real
host is never imported. ``tools/host_assumptions.py`` keeps the copy honest against the real files.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import host_aux_predicates_20261015 as host  # noqa: E402

PACKAGE = "kame_aux_shape_pkg"


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


def _error(task="title_generation", model="gemini-3.8-flash"):
    transport, aux = _load()
    return transport.KameAuxTimeout(aux.stop_message(task, model, 3, 29.6))


@pytest.mark.parametrize("task", ["title_generation", "compression", "vision", "approval", "profile_describer"])
def test_the_host_reads_it_as_a_timeout_and_nothing_else(task):
    exc = _error(task)
    assert host.is_timeout_error(exc)
    assert not host.credential_rung_accepts(exc), "no credential retry, no pool rotation"
    assert not host.is_payment_error(exc)
    assert not host.is_rate_limit_error(exc)
    assert not host.is_auth_error(exc)
    assert host.is_transient_transport_error(exc)


@pytest.mark.parametrize("task", ["title_generation", "compression", "vision"])
def test_the_three_critical_tasks_skip_the_same_provider_retry(task):
    assert host.should_skip_same_provider_retry(task, _error(task))


def test_other_tasks_may_retry_once_more_and_it_is_free():
    # The host retries a transient error on the same provider (1 s, 2 s); KAME's 30 s memo makes
    # those retries cost no request (tests/test_1_8_2_9_aux_budget.py).
    assert not host.should_skip_same_provider_retry("approval", _error("approval"))


def test_the_error_has_no_status_response_or_body():
    exc = _error()
    assert getattr(exc, "status_code", None) is None
    assert getattr(exc, "response", None) is None
    assert getattr(exc, "body", None) is None
    assert isinstance(exc, TimeoutError)


@pytest.mark.parametrize("task,model", [
    ("title_generation", "gemini-3.8-flash"), ("compression", "z-ai/glm-5.3"),
    ("web_extract", "moonshotai/kimi-k3"), ("weird task\n402 payment required", "x credits y"),
])
def test_the_message_holds_none_of_the_hosts_words(task, model):
    text = str(_error(task, model)).lower()
    for word in host.PAYMENT_KEYWORDS + host.RATE_LIMIT_KEYWORDS + ("unauthorized", "invalid api key", "401", "429"):
        assert word not in text, word


def test_the_message_says_what_happened_in_plain_words():
    text = str(_error())
    assert "title_generation" in text and "gemini-3.8-flash" in text
    assert "3 tries" in text and "30s" not in text.replace("29s", "")  # 29.6 s -> 29s
