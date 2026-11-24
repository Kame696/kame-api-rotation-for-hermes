"""1.9.0.6 (decisions/0037 C5) - the panel warning no longer promises "no reply"; it counts provider hops."""
from __future__ import annotations

import importlib

import pytest

from tests import test_1_8_2_5_stream_race as R

chain_mod = importlib.import_module(f"{R.PACKAGE}.core.fallback_chain")
state_mod = importlib.import_module(f"{R.PACKAGE}.state")


def write(tmp_path, monkeypatch, providers, extra=""):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    text = f"model:\n  provider: {providers[0]}\n  default: m0\nfallback_providers:\n"
    text += "".join(f"  - provider: {p}\n    model: m{i + 1}\n" for i, p in enumerate(providers[1:]))
    (tmp_path / "config.yaml").write_text(text + extra, encoding="utf-8")
    chain_mod.forget()


def view():
    return state_mod._fallback_view([])


def test_a_one_provider_list_of_twenty_models_gets_no_warning(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, ["gemini"] * 21)
    assert view()["warning"] == ""


def test_few_provider_hops_get_no_warning(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, ["gemini", "gemini", "groq", "groq", "openai"])    # 2 hops, limit 3 -> 2 left
    assert view()["warning"] == ""


def test_more_provider_hops_than_the_switches_left_warns_in_the_new_words(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, ["gemini", "groq", "openai", "anthropic", "gemini"])   # 4 hops, 4 fallbacks
    warning = view()["warning"]
    assert warning == (
        "Hermes switches model at most 3 times per answer. KAME keeps every answer alive and answers with the "
        "models of the same provider itself; models on other providers past the 2nd switch wait for the next "
        "answer. To reach all of them in one answer, set agent.api_max_retries: 5 in Hermes' config.yaml "
        "(Hermes also retries an ordinary error that many times).")
    assert "no reply" not in warning.lower() and "never reached" not in warning


def test_the_warning_goes_with_a_higher_limit(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, ["gemini", "groq", "openai", "anthropic", "gemini"],
          extra="agent:\n  api_max_retries: 5\n")
    assert view()["switch_limit"] == 5 and view()["warning"] == ""
