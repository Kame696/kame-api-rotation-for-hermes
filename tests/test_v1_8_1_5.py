"""1.8.1.5 -- a setting typed as ``nan`` is refused, not crashed on.

``float("nan")`` parses, and every comparison with NaN is false, so it slipped
past the range check in ``settings.parse`` and then raised ValueError at
``int(number)``: ``/kame set max_hold_seconds nan`` answered with a traceback.
The environment reader clamped the same value to whichever bound ``max`` and
``min`` happened to return (``KAME_MAX_HOLD_SECONDS=nan`` became 60s), which is
not a reading of anything the person wrote.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1815_under_test"


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
settings = importlib.import_module(f"{PACKAGE}.settings")
control = importlib.import_module(f"{PACKAGE}.control")

NUMBERS = sorted(settings.ALL_NUMBERS)


@pytest.mark.parametrize("key", NUMBERS)
@pytest.mark.parametrize("raw", ["nan", "NaN", " -nan "])
def test_parse_refuses_nan_with_a_sentence(key, raw):
    value, error = settings.parse(key, raw)
    assert value is None
    assert "is not one" in error


@pytest.mark.parametrize("key", NUMBERS)
def test_parse_still_refuses_infinity_by_range(key):
    value, error = settings.parse(key, "inf")
    assert value is None
    assert "outside" in error


@pytest.mark.parametrize("key", NUMBERS)
def test_an_environment_nan_says_nothing_so_the_default_decides(key):
    assert settings._as_number("nan", key) is None


def test_the_environment_reader_falls_back_to_the_default(monkeypatch):
    for variable in settings._env_names(settings.MAX_HOLD):
        monkeypatch.setenv(variable, "nan")
    assert settings.number(settings.MAX_HOLD, 3600.0) == 3600.0


def test_ordinary_numbers_are_untouched():
    assert settings.parse(settings.MAX_HOLD, "1800") == ("1800", "")
    assert settings._as_number("999999", settings.MAX_HOLD) == 86400.0


class TestAPanelRequestThatCrashesIsStillAnswered:
    """``control.poll`` promises never to raise; the panel waits on the id."""

    def test_the_request_is_recorded_as_failed(self, tmp_path, monkeypatch):
        path = tmp_path / "control.json"
        path.write_text(json.dumps({
            "schema": control.SCHEMA, "id": "req-1", "action": "set",
            "key": settings.MAX_HOLD, "value": "1800",
        }), encoding="utf-8")
        monkeypatch.setattr(control, "control_path", lambda: path)
        recorded = []
        monkeypatch.setattr(control, "_record", recorded.append)

        def boom(*_a, **_k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(control, "_apply", boom)
        assert control.poll() is True
        assert recorded[-1]["id"] == "req-1"
        assert recorded[-1]["ok"] is False
