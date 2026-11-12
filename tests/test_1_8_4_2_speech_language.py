"""1.8.4.2 V3 - the speech language is a setting, empty by default.

``tts.kame-gemini.language`` (else ``tts.language``) becomes ``speechConfig.languageCode``; nothing set, or
anything that is not a language tag, sends nothing and the model picks, as before.
"""
from __future__ import annotations

import json

import pytest

from tests import test_1_8_4_0_voice as V
from tests.test_1_8_4_0_voice import Gemini, host, make, model  # noqa: F401  (fixture by name)


def configure(monkeypatch, tts):
    V._stub(monkeypatch, "hermes_cli.config", load_config_readonly=lambda: {"tts": tts})


def speech_config(monkeypatch, tts):
    configure(monkeypatch, tts)
    gemini = Gemini()
    list(make(monkeypatch, gemini).stream("Ola.", model=model(), format="pcm"))
    return json.loads(gemini.requests[0].content)["generationConfig"]["speechConfig"]


def test_absent_sends_no_language_code(host, monkeypatch):
    assert "languageCode" not in speech_config(monkeypatch, {})


def test_the_provider_key_is_sent(host, monkeypatch):
    cfg = speech_config(monkeypatch, {"kame-gemini": {"language": "pt-BR"}})
    assert cfg["languageCode"] == "pt-BR"
    assert cfg["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]


def test_tts_language_is_the_fallback(host, monkeypatch):
    assert speech_config(monkeypatch, {"language": " en-US "})["languageCode"] == "en-US"


def test_the_provider_key_beats_tts_language(host, monkeypatch):
    cfg = speech_config(monkeypatch, {"language": "en-US", "kame-gemini": {"language": "pt-BR"}})
    assert cfg["languageCode"] == "pt-BR"


@pytest.mark.parametrize("junk", ["", "   ", "pt BR", "pt_BR", "a" * 36, "pt-BR;drop", 5, None, ["pt-BR"], "pt-BRé"])
def test_junk_sends_nothing(host, monkeypatch, junk):
    assert "languageCode" not in speech_config(monkeypatch, {"kame-gemini": {"language": junk}})


def test_junk_in_the_provider_key_does_not_fall_through_to_tts_language(host, monkeypatch):
    assert "languageCode" not in speech_config(monkeypatch, {"language": "en-US", "kame-gemini": {"language": "x y"}})


def test_payload_default_is_unchanged():
    assert "languageCode" not in V.voice_mod._payload("hi", "Kore")["generationConfig"]["speechConfig"]
    assert V.voice_mod._payload("hi", "Kore", "pt-BR")["generationConfig"]["speechConfig"]["languageCode"] == "pt-BR"
