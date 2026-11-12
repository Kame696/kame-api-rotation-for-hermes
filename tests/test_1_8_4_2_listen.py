"""1.8.4.2 V2: listening on the user's Gemini keys (the Hermes transcription provider ``kame-gemini``).

Offline: ``httpx.MockTransport`` only, the host modules stubbed in ``sys.modules`` (never imported from the
install), fake keys. Every test uses its own model name, so the process-wide engine never carries one test's
rests into another. Modelled on ``test_1_8_4_0_voice.py``.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import importlib
import importlib.util
import itertools
import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_listen_1842_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_package()
listen_mod = importlib.import_module(f"{PACKAGE}.listen")
voice_mod = importlib.import_module(f"{PACKAGE}.voice")
facade = importlib.import_module(f"{PACKAGE}.facade")
settings = importlib.import_module(f"{PACKAGE}.settings")
pacing_mod = importlib.import_module(f"{PACKAGE}.core.pacing")

K1 = "AIzaSyLISTENfake0001" + "a" * 19
K2 = "AIzaSyLISTENfake0002" + "b" * 19
AUDIO = bytes(range(256)) * 8
_models = itertools.count(1)


# --- the host, stubbed ---------------------------------------------------------------------------

class _CatalogProviderBase(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self): ...

    @property
    def display_name(self):
        return self.name.title()

    def is_available(self):
        return True

    def list_models(self):
        return []

    def default_model(self):
        models = self.list_models()
        return models[0].get("id") if models else None


class _TranscriptionProvider(_CatalogProviderBase):
    """agent/transcription_provider.py: ``transcribe`` is abstract; ``streaming_capable`` is False."""

    @abc.abstractmethod
    def transcribe(self, file_path, *, model=None, language=None, **extra): ...

    @property
    def streaming_capable(self):
        return False


def _stub(monkeypatch, name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture()
def host(monkeypatch, tmp_path):
    _stub(monkeypatch, "agent.provider_base", CatalogProviderBase=_CatalogProviderBase)
    _stub(monkeypatch, "agent.transcription_provider", TranscriptionProvider=_TranscriptionProvider)

    def resolve(env_var, provider_id, config_value="", env_getter=None):
        return os.environ.get(env_var, "")

    _stub(monkeypatch, "tools.tool_backend_helpers", resolve_provider_secret=resolve)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)         # not importable: the fallback model
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)              # no config: no model, no language
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_") or name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_NO_REMEMBERED_TROUBLE", "1")
    monkeypatch.setenv("GOOGLE_API_KEY", f"{K1},{K2}")
    settings.forget()
    pacing_mod.PACER.clear()
    sleeps = []
    monkeypatch.setattr(listen_mod, "_sleep", sleeps.append)
    yield sleeps
    settings.forget()
    pacing_mod.PACER.clear()


class Gemini:
    """A scripted Gemini endpoint: per key, a list of responses used in order (the last repeats)."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.asked = []
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, request):
        key = request.headers.get("x-goog-api-key", "")
        with self.lock:
            self.asked.append(key)
            self.requests.append(request)
            steps = self.script.get(key) or [ok()]
            return steps.pop(0) if len(steps) > 1 else steps[0]

    def body(self, index=0):
        return json.loads(self.requests[index].content)


def ok(text="olá, tudo bem?"):
    return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]})


def silence():
    return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "index": 0}]})


def refusal(status, message, *, quota_id="", retry="", code="RESOURCE_EXHAUSTED"):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]})
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return httpx.Response(status, json={"error": {"code": status, "message": message, "status": code,
                                                   "details": details}})


def overloaded():
    return refusal(503, "The model is overloaded. Please try again later.", code="UNAVAILABLE")


def per_minute():
    return refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests",
                   quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", retry="30s")


def make(monkeypatch, endpoint):
    client = httpx.Client(transport=httpx.MockTransport(endpoint))
    monkeypatch.setattr(voice_mod, "HTTP_FACTORY", lambda base: client)
    provider = listen_mod.make_provider()
    assert provider is not None
    return provider


def model():
    return f"gemini-3.{next(_models)}-flash"


def clip(tmp_path, name="a.webm", data=AUDIO):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def engine():
    return facade.TRANSPORT.engine


# --- the envelope --------------------------------------------------------------------------------

def test_a_transcript_comes_back_in_the_hosts_envelope(host, monkeypatch, tmp_path):
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    out = provider.transcribe(clip(tmp_path), model=model())
    assert out == {"success": True, "transcript": "olá, tudo bem?", "provider": "kame-gemini"}
    sent = gemini.requests[0]
    assert sent.url.path.endswith(":generateContent") and "key=" not in str(sent.url)
    body = gemini.body()
    parts = body["contents"][0]["parts"]
    assert body["contents"][0]["role"] == "user"
    assert parts[0]["text"] == listen_mod.BASE_PROMPT
    assert parts[1]["inlineData"] == {"mimeType": "audio/webm", "data": base64.b64encode(AUDIO).decode()}
    assert body["generationConfig"]["temperature"] == 0


def test_silence_is_a_success_with_no_text(host, monkeypatch, tmp_path):
    provider = make(monkeypatch, Gemini({K1: [silence()], K2: [silence()]}))
    out = provider.transcribe(clip(tmp_path), model=model())
    assert out["success"] is True and out["transcript"] == ""


def test_the_text_parts_are_joined_and_stripped(host, monkeypatch, tmp_path):
    response = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": " um "}, {"text": "dois "}]}}]})
    provider = make(monkeypatch, Gemini({K1: [response], K2: [response]}))
    assert provider.transcribe(clip(tmp_path), model=model())["transcript"] == "um dois"


# --- keys ----------------------------------------------------------------------------------------

def test_a_503_on_the_first_key_moves_the_same_recording_to_the_other(host, monkeypatch, tmp_path):
    m = model()
    gemini = Gemini({K1: [overloaded()], K2: [ok("segunda chave")]})
    provider = make(monkeypatch, gemini)
    out = provider.transcribe(clip(tmp_path), model=m)
    assert out["success"] and out["transcript"] == "segunda chave"
    assert gemini.asked == [K1, K2]


def test_a_429_rotates_and_the_key_rests_as_the_chat_reader_decides(host, monkeypatch, tmp_path):
    m = model()
    gemini = Gemini({K1: [per_minute()], K2: [ok()]})
    provider = make(monkeypatch, gemini)
    assert provider.transcribe(clip(tmp_path), model=m)["success"]
    assert gemini.asked == [K1, K2]
    rest = engine().next_recovery_seconds("gemini:" + m, [K1])
    assert rest is not None and 10 < rest <= 40, rest


def test_a_key_resting_for_the_chat_rests_for_listening(host, monkeypatch, tmp_path):
    m = model()
    engine().select("gemini:" + m, [K1, K2])
    engine().mark("gemini:" + m, K1, False, 600.0, "rate_limit", stated=True)
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    assert provider.transcribe(clip(tmp_path), model=m)["success"]
    assert gemini.asked == [K2]


def test_every_key_refused_is_an_error_envelope_not_a_hang(host, monkeypatch, tmp_path):
    m = model()
    gemini = Gemini({K1: [overloaded()], K2: [overloaded()]})
    provider = make(monkeypatch, gemini)
    out = provider.transcribe(clip(tmp_path), model=m)
    assert out["success"] is False and out["transcript"] == "" and out["provider"] == "kame-gemini"
    assert "refused" in out["error"] and len(gemini.asked) == 2
    assert K1 not in out["error"] and K2 not in out["error"]


def test_every_key_resting_for_minutes_is_an_error_at_once_and_does_not_sleep(host, monkeypatch, tmp_path):
    m = model()
    for key in (K1, K2):
        engine().select("gemini:" + m, [K1, K2])
        engine().mark("gemini:" + m, key, False, 600.0, "rate_limit", stated=True)
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path), model=m)
    assert out["success"] is False and "back in" in out["error"] and gemini.asked == [] and not host


def test_a_key_back_within_two_seconds_is_waited_for_once(host, monkeypatch, tmp_path):
    m = model()
    for key in (K1, K2):
        engine().select("gemini:" + m, [K1, K2])
        engine().mark("gemini:" + m, key, False, 1.0, "rate_limit", stated=True)
    import time as _time
    monkeypatch.setattr(listen_mod, "_sleep", lambda seconds: (host.append(seconds), _time.sleep(seconds + 0.05)))
    out = make(monkeypatch, Gemini()).transcribe(clip(tmp_path), model=m)
    assert out["success"] and len(host) == 1 and host[0] <= 2.0


def test_the_budget_ends_a_transcription(host, monkeypatch, tmp_path):
    m = model()
    clock = iter([0.0] + [31.0] * 20)
    monkeypatch.setattr(listen_mod, "_monotonic", lambda: next(clock))
    out = make(monkeypatch, Gemini()).transcribe(clip(tmp_path), model=m)
    assert out["success"] is False and "in time" in out["error"]


def test_a_missing_key_is_an_error_envelope(host, monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_API_KEY")
    out = make(monkeypatch, Gemini()).transcribe(clip(tmp_path), model=model())
    assert out["success"] is False and out["error"] == "no Gemini key"


def test_rotation_off_is_an_error_envelope(host, monkeypatch, tmp_path):
    monkeypatch.setattr(facade, "enabled", lambda: False)
    out = make(monkeypatch, Gemini()).transcribe(clip(tmp_path), model=model())
    assert out["success"] is False and "rotation is off" in out["error"]


# --- refusals that are not the key's -------------------------------------------------------------

def test_a_400_naming_thinking_is_retried_on_the_same_key_without_it(host, monkeypatch, tmp_path):
    m = model()
    bad = refusal(400, "Thinking level is not supported for this model.", code="INVALID_ARGUMENT")
    gemini = Gemini({K1: [bad, ok("sem pensar")], K2: [ok("sem pensar")]})
    provider = make(monkeypatch, gemini)
    out = provider.transcribe(clip(tmp_path), model=m)
    assert out["success"] and out["transcript"] == "sem pensar"
    assert gemini.asked == [K1, K1]
    assert "thinkingConfig" in gemini.body(0)["generationConfig"]
    assert "thinkingConfig" not in gemini.body(1)["generationConfig"]
    assert engine().healthy_count("gemini:" + m, [K1, K2]) == 2, "the key was not at fault"


def test_a_404_is_an_error_envelope_with_the_providers_detail(host, monkeypatch, tmp_path):
    m = model()
    gone = refusal(404, "models/" + m + " is not found for API version v1beta", code="NOT_FOUND")
    out = make(monkeypatch, Gemini({K1: [gone], K2: [gone]})).transcribe(clip(tmp_path), model=m)
    assert out["success"] is False and "not found" in out["error"]
    assert K1 not in out["error"] and K2 not in out["error"]


def test_another_400_is_an_error_envelope(host, monkeypatch, tmp_path):
    bad = refusal(400, "Unable to process input audio.", code="INVALID_ARGUMENT")
    out = make(monkeypatch, Gemini({K1: [bad], K2: [bad]})).transcribe(clip(tmp_path), model=model())
    assert out["success"] is False and "audio" in out["error"]


# --- the recording -------------------------------------------------------------------------------

@pytest.mark.parametrize("suffix,mime", [
    (".webm", "audio/webm"), (".ogg", "audio/ogg"), (".oga", "audio/ogg"), (".opus", "audio/ogg"),
    (".wav", "audio/wav"), (".mp3", "audio/mp3"), (".flac", "audio/flac"), (".aac", "audio/aac"),
    (".m4a", "audio/mp4"), (".mp4", "audio/mp4"), (".aiff", "audio/aiff"), (".AIF", "audio/aiff")])
def test_the_mime_type_comes_from_the_suffix(host, monkeypatch, tmp_path, suffix, mime):
    gemini = Gemini()
    make(monkeypatch, gemini).transcribe(clip(tmp_path, "x" + suffix), model=model())
    assert gemini.body()["contents"][0]["parts"][1]["inlineData"]["mimeType"] == mime


def test_an_unknown_suffix_is_an_error_and_nothing_is_sent(host, monkeypatch, tmp_path):
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path, "x.txt"), model=model())
    assert out["success"] is False and "unsupported audio type .txt" in out["error"] and gemini.asked == []


def test_a_file_over_fifteen_megabytes_is_an_error_and_nothing_is_sent(host, monkeypatch, tmp_path):
    monkeypatch.setattr(listen_mod, "MAX_BYTES", 100)
    gemini = Gemini()
    out = make(monkeypatch, gemini).transcribe(clip(tmp_path, data=b"x" * 101), model=model())
    assert out["success"] is False and "15 MB" in out["error"] and gemini.asked == []
    assert listen_mod.__dict__["MAX_BYTES"] == 100


def test_a_missing_file_is_an_error_envelope(host, monkeypatch, tmp_path):
    out = make(monkeypatch, Gemini()).transcribe(str(tmp_path / "nope.webm"), model=model())
    assert out["success"] is False and out["error"]


def test_nothing_inside_can_raise(host, monkeypatch, tmp_path):
    def broken(base):
        raise RuntimeError("the client is gone: " + K1)

    monkeypatch.setattr(voice_mod, "HTTP_FACTORY", broken)
    provider = listen_mod.make_provider()
    out = provider.transcribe(clip(tmp_path), model=model())
    assert out["success"] is False and out["transcript"] == ""
    assert K1 not in out["error"], "an unexpected error is named by its type, never by its text"


# --- what is asked -------------------------------------------------------------------------------

def test_the_language_and_the_vocabulary_hint_go_into_the_prompt(host, monkeypatch, tmp_path):
    gemini = Gemini()
    make(monkeypatch, gemini).transcribe(clip(tmp_path), model=model(), language="pt-BR",
                                         prompt="  KAME, Hermes, Gemini  ", ignored="x")
    text = gemini.body()["contents"][0]["parts"][0]["text"]
    assert text.startswith(listen_mod.BASE_PROMPT)
    assert "The speaker's language is probably pt-BR." in text
    assert text.endswith("Words that may appear: KAME, Hermes, Gemini")


def test_the_vocabulary_hint_is_cut_at_five_hundred_characters(host, monkeypatch, tmp_path):
    gemini = Gemini()
    make(monkeypatch, gemini).transcribe(clip(tmp_path), model=model(), prompt="w" * 900)
    text = gemini.body()["contents"][0]["parts"][0]["text"]
    assert text.endswith("w" * 500) and "w" * 501 not in text


def test_no_language_and_no_hint_add_nothing(host, monkeypatch, tmp_path):
    gemini = Gemini()
    make(monkeypatch, gemini).transcribe(clip(tmp_path), model=model(), language=None, prompt="  ")
    assert gemini.body()["contents"][0]["parts"][0]["text"] == listen_mod.BASE_PROMPT


def test_the_model_argument_wins_then_the_config_then_the_default(host, monkeypatch, tmp_path):
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    provider.transcribe(clip(tmp_path), model="gemini-3.9-asked")
    assert "/models/gemini-3.9-asked:generateContent" in str(gemini.requests[0].url)

    config = {"stt": {"kame-gemini": {"model": "gemini-3.9-config"}, "gemini": {}}}
    _stub(monkeypatch, "hermes_cli.config", load_config_readonly=lambda: config)
    provider.transcribe(clip(tmp_path))
    assert "/models/gemini-3.9-config:generateContent" in str(gemini.requests[1].url)

    config["stt"] = {}
    _stub(monkeypatch, "agent.auxiliary_client", _API_KEY_PROVIDER_AUX_MODELS={"gemini": "gemini-3.9-aux"})
    provider.transcribe(clip(tmp_path))
    assert "/models/gemini-3.9-aux:generateContent" in str(gemini.requests[2].url)

    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", None)
    assert provider.default_model() == "gemini-3.6-flash"


@pytest.mark.parametrize("name,expected", [
    ("gemini-2.5-flash", {"thinkingBudget": 0}),
    ("gemini-3.6-flash", {"thinkingLevel": "minimal"}),
    ("gemini-3-pro", {"thinkingLevel": "minimal"}),
    ("gemini-4.0-flash", {"thinkingLevel": "minimal"}),
    ("gemini-2.0-flash", None),
    ("gemini-1.5-flash", None),
    ("gemma-3-27b", None),
    ("something-else", None),
])
def test_the_thinking_setting_follows_the_model_generation(name, expected):
    assert listen_mod._thinking_config(name) == expected


def test_the_request_carries_no_thinking_setting_for_a_model_that_has_none(host, monkeypatch, tmp_path):
    gemini = Gemini()
    make(monkeypatch, gemini).transcribe(clip(tmp_path), model="gemma-3-27b-it")
    assert "thinkingConfig" not in gemini.body()["generationConfig"]


# --- the provider --------------------------------------------------------------------------------

def test_is_available_needs_a_key(host, monkeypatch):
    provider = make(monkeypatch, Gemini())
    assert provider.is_available() is True
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert provider.is_available() is False


def test_the_name_is_the_one_to_put_in_the_config(host, monkeypatch):
    provider = make(monkeypatch, Gemini())
    assert provider.name == "kame-gemini" and provider.streaming_capable is False


def test_no_host_module_means_no_provider(host, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.transcription_provider", None)
    assert listen_mod.make_provider() is None


def test_the_transcript_is_never_logged(host, monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    make(monkeypatch, Gemini({K1: [ok("segredo da conversa")], K2: [ok("segredo da conversa")]})).transcribe(
        clip(tmp_path), model=model())
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "segredo" not in text and K1 not in text and K2 not in text
    assert "listened" in text


def test_nothing_imports_the_host_at_load_time():
    text = (PLUGIN_DIR / "listen.py").read_text(encoding="utf-8")
    top = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in top if line.startswith(("from agent", "from tools", "from hermes_cli"))]


# --- registration --------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, with_stt=True):
        self.hooks, self.commands, self.stt = {}, {}, []
        if with_stt:
            self.register_transcription_provider = self.stt.append

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler


def test_register_hands_the_provider_to_a_host_that_has_the_seam(host):
    plugin._cleanup_runtime()
    ctx = _Ctx()
    try:
        plugin.register(ctx)
    finally:
        plugin._cleanup_runtime()
    assert len(ctx.stt) == 1 and ctx.stt[0].name == "kame-gemini"
    assert "transform_api_error_classification" in ctx.hooks


def test_register_loads_as_before_on_a_host_without_the_seam(host):
    plugin._cleanup_runtime()
    ctx = _Ctx(with_stt=False)
    try:
        plugin.register(ctx)
    finally:
        plugin._cleanup_runtime()
    assert "transform_api_error_classification" in ctx.hooks and not ctx.stt


def test_a_broken_registrar_costs_nothing_else(host):
    plugin._cleanup_runtime()
    ctx = _Ctx()

    def refuse(provider):
        raise RuntimeError("registry closed")

    ctx.register_transcription_provider = refuse
    try:
        plugin.register(ctx)
    finally:
        plugin._cleanup_runtime()
    assert "transform_api_error_classification" in ctx.hooks and "kame" in ctx.commands
