"""Ownership must survive the dispatch fallback, not only the classifier hook."""
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"
NAME = "kame707ownership"
spec = importlib.util.spec_from_file_location(NAME, ROOT / "__init__.py",
                                            submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules[NAME] = pkg
spec.loader.exec_module(pkg)
dispatch = importlib.import_module(NAME + ".dispatch_binding")
carousel = importlib.import_module(NAME + ".core.carousel")


@pytest.mark.parametrize("location", ["body", "response", "details"])
@pytest.mark.parametrize("status,message", [(401, "API key not valid"),
    (429, "Rate limit exceeded, try again in 30s"), (402, "Insufficient credits")])
def test_dispatch_never_charges_relayed_failure_to_aggregator_key(location, status, message):
    engine = carousel.Carousel()
    binding = dispatch.DispatchBinding(engine=engine)
    identity, key = "openrouter:model", "synthetic-key"
    payload = {"error": {"code": status, "message": "Provider returned error",
        "metadata": {"provider_name": "upstream", "raw": json.dumps({
            "error": {"message": message}})}}}
    exc = Exception(message)
    exc.status_code = status
    if location == "response":
        exc.response = SimpleNamespace(text=json.dumps(payload))
    else:
        setattr(exc, location, payload)
    before = engine.snapshot()
    for i in range(4):
        action, kind, code = binding._on_failure(identity, key, exc, "test", i+1, False)
        assert action == "raise"  # original exception reaches host model fallback
        assert kind == "upstream_error"
        assert code == status
    assert not engine.is_retired(identity, key)
    assert engine.snapshot() == before


def test_real_invalid_aggregator_key_still_retires():
    engine = carousel.Carousel()
    binding = dispatch.DispatchBinding(engine=engine)
    exc = Exception("API key not valid")
    exc.status_code = 401
    exc.body = {"error": {"code": "invalid_api_key", "message": str(exc)}}
    result = binding._on_failure("openrouter:model", "synthetic-key", exc, "test", 1, False)
    assert result[:2] == ("rotate", "revoked")
    assert engine.is_retired("openrouter:model", "synthetic-key")
