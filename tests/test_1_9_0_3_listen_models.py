"""1.9.0.3 (C6) - voice is not lost when the voice model's keys rest (audit V1).

Evidence: 17/11 20:26-21:19, 397 recordings, 0 transcripts, each "every Gemini key is resting", while the chat
answered on another Gemini model of the same list. Offline, fake keys; helpers come from the 1.8.4.2 listen test.
"""
from __future__ import annotations

import logging

import pytest

from tests.test_1_8_4_2_listen import (  # noqa: F401  (host is a fixture)
    AUDIO, K1, K2, Gemini, clip, engine, facade, host, listen_mod, make, model, ok, overloaded, plugin, settings)

import importlib

fallback_chain = importlib.import_module(f"{plugin.__name__}.core.fallback_chain")


def _list(monkeypatch, primary, chain):
    monkeypatch.setattr(fallback_chain, "read", lambda home=None: (primary, tuple(chain)))
    monkeypatch.setattr(fallback_chain, "entry_source", lambda entry, home=None: None)


def _rest(name, back=600.0):
    ident = "gemini:" + name + ":global"
    for key in (K1, K2):
        engine().select(ident, [K1, K2])
        engine().mark(ident, key, False, back, "rate_limit", stated=True)


def _asked_models(gemini):
    return [r.url.path.split("/models/")[1].split(":")[0] for r in gemini.requests]


def test_the_chosen_model_resting_falls_to_the_next_gemini_model_of_the_list(host, monkeypatch, tmp_path, caplog):
    chosen, hydrogen = model(), model()
    _rest(chosen)
    _list(monkeypatch, "gemini:" + hydrogen, ["openai:gpt-9", "gemini:" + chosen])
    gemini = Gemini()
    with caplog.at_level(logging.INFO):
        out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is True and out["transcript"] == "olá, tudo bem?"
    assert _asked_models(gemini) == [hydrogen]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"on {hydrogen} instead of {chosen} (its keys rest)" in text
    assert K1 not in text and K2 not in text


def test_non_gemini_entries_are_never_tried(host, monkeypatch, tmp_path):
    chosen = model()
    _rest(chosen)
    _list(monkeypatch, "openai:gpt-9", ["nvidia:some/model"])
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is False and gemini.asked == []
    assert out["error"].startswith(f"every key of {chosen} is resting; the first is back in ")
    assert "Gemini key" not in out["error"]


def test_never_mode_means_the_chosen_model_only_and_the_failure_is_logged(host, monkeypatch, tmp_path, caplog):
    chosen, other = model(), model()
    _rest(chosen, 600.0)
    _list(monkeypatch, "gemini:" + other, [])
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    gemini = Gemini()
    with caplog.at_level(logging.WARNING):
        out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is False and gemini.asked == []
    assert out["error"].startswith(f"every key of {chosen} is resting; the first is back in ")
    assert "other model" not in out["error"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(w.startswith(f"kame: listening failed on {chosen}: every key of {chosen} is resting") for w in warnings)
    assert all(K1 not in w and K2 not in w for w in warnings)


def test_the_no_fallback_switch_also_keeps_the_chosen_model_only(host, monkeypatch, tmp_path):
    chosen, other = model(), model()
    _rest(chosen)
    _list(monkeypatch, "gemini:" + other, [])
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is False and gemini.asked == []


def test_every_candidate_resting_names_the_count_and_the_soonest_return(host, monkeypatch, tmp_path):
    chosen, a, b = model(), model(), model()
    _rest(chosen, 600.0)
    _rest(a, 90.0)
    _rest(b, 1200.0)
    _list(monkeypatch, "gemini:" + a, ["gemini:" + b])
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is False and gemini.asked == []
    assert out["error"].startswith(f"every key of {chosen} and of 2 other models of your list is resting; "
                                   "the first is back in 1m")
    _rest(chosen, 600.0)


def test_one_other_model_is_singular(host, monkeypatch, tmp_path):
    chosen, a = model(), model()
    _rest(chosen, 600.0)
    _rest(a, 600.0)
    _list(monkeypatch, "gemini:" + a, [])
    out = make(monkeypatch, Gemini()).transcribe(clip(tmp_path), model=chosen)
    assert f"and of 1 other model of your list is resting" in out["error"]


def test_the_chosen_model_is_not_asked_twice_when_it_is_in_the_list(host, monkeypatch, tmp_path):
    chosen = model()
    _rest(chosen)
    _list(monkeypatch, "gemini:" + chosen, [])
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is False and gemini.asked == []
    assert "other model" not in out["error"]


def test_a_model_that_refuses_on_every_key_is_skipped_to_the_next(host, monkeypatch, tmp_path):
    chosen, second = model(), model()
    _list(monkeypatch, "gemini:" + second, [])

    def endpoint(request):
        name = request.url.path.split("/models/")[1].split(":")[0]
        return overloaded() if name == chosen else ok("segunda")

    out = make(monkeypatch, endpoint).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is True and out["transcript"] == "segunda"


def test_one_budget_for_the_whole_recording(host, monkeypatch, tmp_path):
    chosen, second = model(), model()
    _rest(chosen)
    _list(monkeypatch, "gemini:" + second, [])
    clock = iter([0.0, 31.0] + [31.0] * 30)
    monkeypatch.setattr(listen_mod, "_monotonic", lambda: next(clock))
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is False and "in time" in out["error"] and gemini.asked == []


def test_a_404_still_ends_it_and_is_logged_with_the_chosen_model(host, monkeypatch, tmp_path, caplog):
    chosen = model()
    _list(monkeypatch, None, [])
    from tests.test_1_8_4_2_listen import refusal
    gone = refusal(404, "models/" + chosen + " is not found", code="NOT_FOUND")
    with caplog.at_level(logging.WARNING):
        out = make(monkeypatch, Gemini({K1: [gone], K2: [gone]})).transcribe(clip(tmp_path), model=chosen)
    assert out["success"] is False and "not found" in out["error"]
    assert any(r.getMessage().startswith(f"kame: listening failed on {chosen}:") for r in caplog.records)
