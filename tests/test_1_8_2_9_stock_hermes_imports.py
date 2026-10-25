"""1.8.2.9: the fallback list is read on a STOCK Hermes (catalog review of 1.8.2.8, 2026-10-19).

Core Hermes does not ship PyYAML. 1.8.2.8 did ``import yaml  # the host ships it`` inside an
``except Exception``, so on a stock install the user's fallback list was silently never read
and the hand-over, the cross-model race and the switch limit did nothing. The suite missed
it because the test environment has PyYAML. These tests take it away.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_stock_hermes_imports.py -q
"""
from __future__ import annotations

import logging
import sys
import types

import pytest

from tests import test_1_8_2_5_stream_race as R

chain_mod = __import__(f"{R.PACKAGE}.core.fallback_chain", fromlist=["x"])

CONFIG = """\
model:
  provider: gemini
  default: gemini-race
agent:
  api_max_retries: 5
fallback_providers:
  - provider: gemini
    model: gemini-race-two
"""


@pytest.fixture()
def home(monkeypatch, tmp_path):
    import yaml as real_yaml

    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "yaml", None)           # a stock Hermes: no PyYAML
    monkeypatch.setattr(chain_mod, "_yaml_said", False)
    chain_mod.forget()
    yield types.SimpleNamespace(real_yaml=real_yaml, path=tmp_path)
    chain_mod.forget()


def test_the_list_is_read_through_hermes_own_yaml_when_pyyaml_is_absent(home, monkeypatch):
    module = types.ModuleType("hermes_yaml")
    module.safe_load = home.real_yaml.safe_load
    monkeypatch.setitem(sys.modules, "hermes_yaml", module)
    assert chain_mod.read() == ("gemini:gemini-race", ("gemini:gemini-race-two",))
    assert chain_mod.entry_source("gemini:gemini-race-two") == ("gemini-race-two", "")
    assert chain_mod.switch_limit() == 5


def test_with_no_yaml_at_all_it_says_so_once_instead_of_acting_as_if_there_were_no_list(
        home, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    with caplog.at_level(logging.WARNING):
        assert chain_mod.read() == (None, ())
        chain_mod.forget()
        assert chain_mod.read() == (None, ())
    said = [r for r in caplog.records if "neither hermes_yaml nor PyYAML" in r.getMessage()]
    assert len(said) == 1


def test_pyyaml_still_works_where_hermes_yaml_is_missing(home, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", home.real_yaml)
    monkeypatch.setitem(sys.modules, "hermes_yaml", None)
    assert chain_mod.read()[1] == ("gemini:gemini-race-two",)
