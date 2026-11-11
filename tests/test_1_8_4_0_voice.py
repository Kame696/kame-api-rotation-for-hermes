"""1.8.4.0 V1: Gemini speech through KAME's keys (the Hermes speech provider).

Offline: ``httpx.MockTransport`` only, the host modules stubbed in ``sys.modules`` (never imported
from the install), fake keys. Every test uses its own model name, so the process-wide engine never
carries one test's rests into another.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import importlib
import importlib.util
import itertools
import os
import sys
import threading
import wave
from pathlib import Path
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_voice_184_under_test"


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
voice_mod = importlib.import_module(f"{PACKAGE}.voice")
facade = importlib.import_module(f"{PACKAGE}.facade")
settings = importlib.import_module(f"{PACKAGE}.settings")
pacing_mod = importlib.import_module(f"{PACKAGE}.core.pacing")
integrity = importlib.import_module(f"{PACKAGE}.integrity")

K1 = "AIzaSyVOICEfake0001" + "a" * 20
K2 = "AIzaSyVOICEfake0002" + "b" * 20
K3 = "AIzaSyVOICEfake0003" + "c" * 20
PCM = bytes(range(256)) * 20          # 5120 bytes of "audio"
BUILTIN_NAMES = {"edge", "elevenlabs", "openai", "minimax", "xai", "mistral", "gemini", "neutts",
                 "kittentts", "piper", "deepinfra"}

_models = itertools.count(1)


def label(key: str) -> str:
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:6]


# --- the host, stubbed -------------------------------------------------------------------------

class _TTSProvider(abc.ABC):
    """The host base class's shape (agent/provider_base.py + agent/tts_provider.py)."""

    streams_pcm = False
    stream_sample_rate = None

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

    def list_voices(self):
        return []

    def default_voice(self):
        voices = self.list_voices()
        return voices[0].get("id") if voices else None

    @abc.abstractmethod
    def synthesize(self, text, output_path, *, voice=None, model=None, speed=None, format="mp3", **extra): ...

    def stream(self, text, *, voice=None, model=None, format="opus", **extra):
        raise NotImplementedError

    @property
    def voice_compatible(self):
        return False


def _stub(monkeypatch, name, **attrs):
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture()
def host(monkeypatch, tmp_path):
    """Host modules stubbed, environment clean, one key line holding two fake keys."""
    _stub(monkeypatch, "agent.tts_provider", TTSProvider=_TTSProvider)
    _stub(monkeypatch, "agent.provider_base", CatalogProviderBase=_TTSProvider)
    _stub(monkeypatch, "tools.tts_tool_providers", DEFAULT_GEMINI_TTS_MODEL="gemini-2.5-flash-preview-tts")

    def resolve(env_var, provider_id, config_value="", env_getter=None):
        return os.environ.get(env_var, "")

    _stub(monkeypatch, "tools.tool_backend_helpers", resolve_provider_secret=resolve)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_") or name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", f"{K1},{K2}")
    settings.forget()
    pacing_mod.PACER.clear()
    sleeps = []
    monkeypatch.setattr(voice_mod, "_sleep", sleeps.append)
    yield sleeps
    settings.forget()
    pacing_mod.PACER.clear()


class Gemini:
    """A scripted Gemini speech endpoint: per key, a list of responses used in order (the last repeats)."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.asked = []              # the key of each request, in order
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, request):
        key = request.headers.get("x-goog-api-key", "")
        with self.lock:
            self.asked.append(key)
            self.requests.append(request)
            steps = self.script.get(key) or [ok()]
            step = steps.pop(0) if len(steps) > 1 else steps[0]
        return step


def ok(pcm=PCM):
    data = base64.b64encode(pcm).decode()
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": data}}]}}]})


def refusal(status, message, *, quota_id="", retry="", code="RESOURCE_EXHAUSTED"):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}]})
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return httpx.Response(status, json={"error": {"code": status, "message": message, "status": code,
                                                   "details": details}})


def per_minute(delay="30s"):
    return refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests",
                   quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", retry=delay)


def per_day():
    return refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests",
                   quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier", retry="29s")


def make(monkeypatch, endpoint):
    """The provider, with its HTTP going to ``endpoint`` (a MockTransport handler)."""
    client = httpx.Client(transport=httpx.MockTransport(endpoint))
    monkeypatch.setattr(voice_mod, "HTTP_FACTORY", lambda base: client)
    provider = voice_mod.make_provider()
    assert provider is not None
    return provider


def model():
    return f"tts-model-{next(_models)}"


def engine():
    return facade.TRANSPORT.engine


def reserve(monkeypatch, *labels):
    monkeypatch.setenv("KAME_KEY_RESERVE", ", ".join(labels))
    settings.forget()
    pacing_mod.PACER._synced_at.clear()


# --- V1.1 ----------------------------------------------------------------------------------------

def test_the_host_client_is_made_once_per_base_url_and_reused(host, monkeypatch):
    # Hermes' build_keepalive_http_client returns a NEW httpx.Client per call; asking it per
    # sentence and never closing the result would leave a client (and, behind a proxy, a pool)
    # behind every sentence.
    made = []

    def builder(base):
        client = httpx.Client(transport=httpx.MockTransport(Gemini()))
        made.append((base, client))
        return client

    monkeypatch.setattr(voice_mod, "HTTP_FACTORY", None)
    monkeypatch.setattr(voice_mod, "_CLIENTS", {})
    monkeypatch.setattr(facade, "_keepalive_http", builder)
    provider = voice_mod.make_provider()
    name = model()
    for _ in range(3):
        assert b"".join(provider.stream("One sentence.", model=name, format="pcm")) == PCM
    assert len(made) == 1
    assert made[0][0] == voice_mod.DEFAULT_BASE_URL


def test_200_writes_a_valid_wav(host, monkeypatch, tmp_path):
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    out = provider.synthesize("Hello there.", str(tmp_path / "speech.mp3"), voice="Puck", model=model())
    assert out.endswith(".wav") and Path(out).is_file()
    assert not (tmp_path / "speech.mp3").exists()
    with wave.open(out, "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
        assert wav.readframes(wav.getnframes()) == PCM
    sent = gemini.requests[0]
    assert sent.url.path.endswith(":generateContent") and "key=" not in str(sent.url)
    body = __import__("json").loads(sent.content)
    assert body["contents"][0]["parts"][0]["text"] == "Hello there."
    assert body["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Puck"


def test_a_wav_path_is_kept_and_speed_and_unknown_keys_are_ignored(host, monkeypatch, tmp_path):
    provider = make(monkeypatch, Gemini())
    out = provider.synthesize("Hi.", str(tmp_path / "a.wav"), speed=1.5, format="wav", whatever=1, model=model())
    assert out == str(tmp_path / "a.wav") and Path(out).is_file()


def test_the_key_line_becomes_two_keys_and_the_joined_string_is_never_sent(host, monkeypatch):
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    m = model()
    for _ in range(4):
        list(provider.stream("A sentence.", model=m, format="pcm"))
    assert set(gemini.asked) == {K1, K2}
    assert all("," not in key for key in gemini.asked)
    assert gemini.asked.count(K1) == gemini.asked.count(K2) == 2


def test_gemini_api_key_is_read_before_google_api_key(host, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", K3)
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    list(provider.stream("One.", model=model(), format="pcm"))
    assert gemini.asked == [K3]


def test_the_host_resolver_failing_falls_back_to_the_environment(host, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no resolver")

    _stub(monkeypatch, "tools.tool_backend_helpers", resolve_provider_secret=broken)
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    list(provider.stream("One.", model=model(), format="pcm"))
    assert set(gemini.asked) <= {K1, K2}


def test_per_minute_429_moves_the_same_sentence_to_the_other_key(host, monkeypatch):
    m = model()
    gemini = Gemini({K1: [per_minute("30s")], K2: [ok()]})
    provider = make(monkeypatch, gemini)
    chunks = list(provider.stream("Same sentence.", model=m, format="pcm"))
    assert chunks == [PCM]
    assert gemini.asked == [K1, K2]
    identity = "gemini:" + m
    assert engine().healthy_count(identity, [K1, K2]) == 1
    rest = engine().next_recovery_seconds(identity, [K1])
    assert rest is not None and 10 < rest <= 40, rest          # about the stated 30 s
    assert not host                                            # nothing slept


def test_daily_429_rests_the_key_long_and_the_next_sentence_goes_to_the_other_first(host, monkeypatch):
    m = model()
    gemini = Gemini({K1: [per_day()], K2: [ok()]})
    provider = make(monkeypatch, gemini)
    assert list(provider.stream("First.", model=m, format="pcm")) == [PCM]
    assert gemini.asked == [K1, K2]
    identity = "gemini:" + m
    rest = engine().next_recovery_seconds(identity, [K1])
    assert rest is not None and rest > 120, rest        # the engine's daily rest, not the stated 29 s
    assert list(provider.stream("Second.", model=m, format="pcm")) == [PCM]
    assert gemini.asked[2] == K2 and K1 not in gemini.asked[2:]


def test_a_400_that_names_the_voice_is_raised_at_once_and_no_key_rests(host, monkeypatch):
    m = model()
    bad = refusal(400, "Invalid voice name 'Nobody': the prebuilt voice does not exist.", code="INVALID_ARGUMENT")
    gemini = Gemini({K1: [bad], K2: [bad]})
    provider = make(monkeypatch, gemini)
    with pytest.raises(Exception) as caught:
        list(provider.stream("Hello.", voice="Nobody", model=m, format="pcm"))
    assert len(gemini.asked) == 1
    assert not isinstance(caught.value, voice_mod.SpeechUnavailable)
    assert "Nobody" in str(caught.value) or "voice" in str(caught.value).lower()
    identity = "gemini:" + m
    assert engine().healthy_count(identity, [K1, K2]) == 2
    assert K1 not in str(caught.value) and K2 not in str(caught.value)


# --- F12: a sentence the model would not speak (measured live 2026-11-08) ----------------------------
# "Hello there." sent bare came back as HTTP 400 "Model tried to generate text, but it should only be
# used for TTS..." on one key and as a 200 with finishReason OTHER and no content on another; the same
# sentence in Hermes' own TRANSCRIPT frame, or as `Say: "..."`, came back as 0.85 s of speech (the
# frame is not spoken). It is the sentence, not the key: no key rests, and the next try is reframed.

NO_SPEECH_400 = ("Model tried to generate text, but it should only be used for TTS. Make sure your "
                 "instructions are clear to only generate audio from a given text transcript.")


def no_speech_400():
    return refusal(400, NO_SPEECH_400, code="INVALID_ARGUMENT")


def no_speech_200():
    return httpx.Response(200, json={"candidates": [{"finishReason": "OTHER", "index": 0}]})


def _sent_text(request):
    import json as _json
    return _json.loads(request.content)["contents"][0]["parts"][0]["text"]


def test_a_sentence_the_model_would_not_speak_is_reframed_on_the_next_key_and_no_key_rests(host, monkeypatch):
    m = model()
    gemini = Gemini({K1: [no_speech_400()], K2: [ok()]})
    provider = make(monkeypatch, gemini)
    pcm = b"".join(provider.stream("Hello there.", model=m, format="pcm"))
    assert pcm == PCM
    assert len(gemini.asked) == 2 and set(gemini.asked) == {K1, K2}
    assert _sent_text(gemini.requests[0]) == "Hello there."
    second = _sent_text(gemini.requests[1])
    assert second != "Hello there." and second.endswith("Hello there.") and "TRANSCRIPT" in second
    assert engine().healthy_count("gemini:" + m, [K1, K2]) == 2


def test_a_200_without_audio_is_reframed_too(host, monkeypatch):
    m = model()
    gemini = Gemini({K1: [no_speech_200()], K2: [ok()]})
    provider = make(monkeypatch, gemini)
    pcm = b"".join(provider.stream("Hello there.", model=m, format="pcm"))
    assert pcm == PCM
    assert len(gemini.asked) == 2
    assert engine().healthy_count("gemini:" + m, [K1, K2]) == 2


def test_with_one_key_the_reframed_sentence_goes_to_the_same_key(host, monkeypatch):
    # The key is not at fault, so reframing is not bounded by how many keys there are.
    m = model()
    monkeypatch.setenv("GOOGLE_API_KEY", K1)
    gemini = Gemini({K1: [no_speech_400(), no_speech_200(), ok()]})
    provider = make(monkeypatch, gemini)
    pcm = b"".join(provider.stream("Hello there.", model=m, format="pcm"))
    assert pcm == PCM
    assert gemini.asked == [K1, K1, K1]


def test_three_framings_without_speech_end_the_sentence_with_a_speech_error(host, monkeypatch):
    m = model()
    monkeypatch.setenv("GOOGLE_API_KEY", f"{K1},{K2},{K3}")
    gemini = Gemini({K1: [no_speech_400()], K2: [no_speech_200()], K3: [no_speech_400()]})
    provider = make(monkeypatch, gemini)
    with pytest.raises(voice_mod.SpeechError):
        list(provider.stream("Hello there.", model=m, format="pcm"))
    texts = [_sent_text(r) for r in gemini.requests]
    assert len(texts) == 3 and len(set(texts)) == 3          # bare, TRANSCRIPT frame, Say:
    assert texts[2].startswith("Say:")
    assert engine().healthy_count("gemini:" + m, [K1, K2, K3]) == 3


def test_every_key_resting_for_minutes_raises_at_once_naming_when_and_does_not_sleep(host, monkeypatch):
    m = model()
    identity = "gemini:" + m
    for key in (K1, K2):
        engine().select(identity, [K1, K2])
        engine().mark(identity, key, False, 600.0, "rate_limit", stated=True)
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    with pytest.raises(voice_mod.SpeechUnavailable) as caught:
        list(provider.stream("Hello.", model=m, format="pcm"))
    assert gemini.asked == [] and not host
    text = str(caught.value)
    assert "back in" in text and ("min" in text), text
    assert K1 not in text and K2 not in text


def test_a_key_back_within_two_seconds_is_waited_for_once(host, monkeypatch):
    m = model()
    identity = "gemini:" + m
    for key in (K1, K2):
        engine().select(identity, [K1, K2])
        engine().mark(identity, key, False, 1.0, "rate_limit", stated=True)
    gemini = Gemini()
    provider = make(monkeypatch, gemini)

    import time as _time
    monkeypatch.setattr(voice_mod, "_sleep", lambda seconds: (host.append(seconds), _time.sleep(seconds + 0.05)))
    assert list(provider.stream("Hello.", model=m, format="pcm")) == [PCM]
    assert len(host) == 1 and host[0] <= 2.0


def test_no_key_at_all_raises_unavailable(host, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    provider = make(monkeypatch, Gemini())
    with pytest.raises(voice_mod.SpeechUnavailable):
        provider.synthesize("Hello.", "x.wav", model=model())


def test_stream_pcm_yields_the_bytes_and_other_formats_are_not_implemented(host, monkeypatch):
    provider = make(monkeypatch, Gemini())
    assert list(provider.stream("Hi.", model=model(), format="pcm")) == [PCM]
    with pytest.raises(NotImplementedError):
        list(provider.stream("Hi.", model=model(), format="opus"))
    with pytest.raises(NotImplementedError):
        provider.stream("Hi.", model=model())                 # the default format is opus
    assert provider.streams_pcm is True and provider.stream_sample_rate == 24000


def test_three_sentences_in_three_threads_land_on_three_keys(host, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", f"{K1},{K2},{K3}")
    m = model()
    gate = threading.Barrier(3)
    gemini = Gemini()

    def endpoint(request):
        response = gemini(request)
        try:
            gate.wait(timeout=5)         # the three requests are in flight together
        except threading.BrokenBarrierError:
            pass
        return response

    provider = make(monkeypatch, endpoint)
    results, errors = [], []

    def speak(n):
        try:
            results.append(list(provider.stream(f"Sentence {n}.", model=m, format="pcm")))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=speak, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors and len(results) == 3
    assert sorted(gemini.asked) == sorted([K1, K2, K3])


def test_a_reserve_key_is_not_asked_while_another_is_healthy(host, monkeypatch):
    reserve(monkeypatch, label(K1))
    gemini = Gemini()
    provider = make(monkeypatch, gemini)
    m = model()
    for n in range(5):
        list(provider.stream(f"Sentence {n}.", model=m, format="pcm"))
    assert gemini.asked and K1 not in gemini.asked and set(gemini.asked) == {K2}


def test_a_timeout_goes_through_the_same_reader_and_moves_to_the_next_key(host, monkeypatch):
    seen = []

    def endpoint(request):
        key = request.headers["x-goog-api-key"]
        seen.append(key)
        if len(seen) == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return ok()

    provider = make(monkeypatch, endpoint)
    assert list(provider.stream("Hi.", model=model(), format="pcm")) == [PCM]
    assert len(seen) == 2 and seen[0] != seen[1]


def test_no_key_appears_in_logs_or_in_the_wav_path(host, monkeypatch, caplog, tmp_path):
    caplog.set_level("DEBUG")
    gemini = Gemini({K1: [per_minute("30s")], K2: [ok()]})
    provider = make(monkeypatch, gemini)
    out = provider.synthesize("Hi.", str(tmp_path / "s.wav"), model=model())
    assert K1 not in caplog.text and K2 not in caplog.text and K1 not in out and K2 not in out


# --- the provider's face ---------------------------------------------------------------------------

def test_the_provider_states_what_the_host_reads(host, monkeypatch):
    provider = make(monkeypatch, Gemini())
    assert provider.name == "kame-gemini"
    assert provider.display_name == "Gemini speech (KAME keys)"
    assert provider.voice_compatible is True
    assert provider.streams_pcm is True and provider.stream_sample_rate == 24000
    voices = provider.list_voices()
    assert len(voices) == 30 and provider.default_voice() == "Kore"
    assert all(set(v) >= {"id", "display"} for v in voices)
    assert {"Zephyr", "Sulafat", "Kore", "Zubenelgenubi"} <= {v["id"] for v in voices}


def test_models_are_the_hosts_default_and_the_owners_choice_only(host, monkeypatch):
    _stub(monkeypatch, "tools.tts_tool_providers", DEFAULT_GEMINI_TTS_MODEL="host-default-tts")
    provider = make(monkeypatch, Gemini())
    assert provider.default_model() == "host-default-tts"
    assert [m["id"] for m in provider.list_models()] == ["host-default-tts"]
    monkeypatch.setattr(voice_mod, "_configured_model", lambda: "owners-model")
    assert [m["id"] for m in provider.list_models()] == ["host-default-tts", "owners-model"]
    monkeypatch.delitem(sys.modules, "tools.tts_tool_providers")
    monkeypatch.setitem(sys.modules, "tools.tts_tool_providers", None)        # not importable
    assert provider.default_model() == "gemini-2.5-flash-preview-tts"


def test_the_rotation_switch_off_makes_the_provider_unavailable(host, monkeypatch):
    provider = make(monkeypatch, Gemini())
    assert provider.is_available() is True
    monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
    assert provider.is_available() is False


def test_no_key_makes_the_provider_unavailable_without_the_network(host, monkeypatch):
    def endpoint(request):
        raise AssertionError("is_available touched the network")

    provider = make(monkeypatch, endpoint)
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert provider.is_available() is False


def test_a_host_without_the_seam_gets_no_provider(host, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.tts_provider", None)
    assert voice_mod.make_provider() is None


def test_the_name_is_not_a_builtin_speech_name(host, monkeypatch):
    provider = make(monkeypatch, Gemini())
    assert provider.name not in BUILTIN_NAMES


def test_nothing_imports_the_host_at_load_time():
    text = (PLUGIN_DIR / "voice.py").read_text(encoding="utf-8")
    top = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in top if "agent" in line.split() or line.startswith(("from agent", "from tools"))]


# --- V1.2 registration -------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, with_tts=True):
        self.hooks, self.commands, self.tts = {}, {}, []
        if with_tts:
            self.register_tts_provider = self.tts.append

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
    assert len(ctx.tts) == 1 and ctx.tts[0].name == "kame-gemini"
    assert "transform_api_error_classification" in ctx.hooks


def test_register_loads_as_before_on_a_host_without_the_seam(host):
    plugin._cleanup_runtime()
    ctx = _Ctx(with_tts=False)
    try:
        plugin.register(ctx)
    finally:
        plugin._cleanup_runtime()
    assert "transform_api_error_classification" in ctx.hooks and not ctx.tts


def test_a_broken_registrar_costs_nothing_else(host):
    plugin._cleanup_runtime()
    ctx = _Ctx()

    def refuse(provider):
        raise RuntimeError("registry closed")

    ctx.register_tts_provider = refuse
    try:
        plugin.register(ctx)
    finally:
        plugin._cleanup_runtime()
    assert "transform_api_error_classification" in ctx.hooks and "kame" in ctx.commands


def test_the_seam_row_is_optional_in_the_integrity_report(host):
    report = integrity.check_host_seams()
    assert any("speech provider" in name for name in report["optional_present"] + report["optional_absent"])
    assert not any("speech provider" in name for name in report["required_missing"])


# --- round 2 review F8: one sentence has one budget --------------------------------------------------

def three_keys(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", f"{K1},{K2},{K3}")


def test_two_connect_timeouts_in_one_sentence_raise_after_two_attempts(host, monkeypatch):
    three_keys(monkeypatch)
    seen = []

    def endpoint(request):
        seen.append(request.headers["x-goog-api-key"])
        raise httpx.ConnectTimeout("connect timed out", request=request)

    provider = make(monkeypatch, endpoint)
    with pytest.raises(voice_mod.SpeechUnavailable) as caught:
        list(provider.stream("Hello.", model=model(), format="pcm"))
    assert len(seen) == 2, "the third key is not asked: the endpoint cannot be reached"
    text = str(caught.value)
    assert "could not be reached" in text and "ConnectTimeout" in text, text
    assert K1 not in text and K2 not in text and K3 not in text


def test_one_connect_timeout_still_moves_to_the_next_key(host, monkeypatch):
    three_keys(monkeypatch)
    seen = []

    def endpoint(request):
        seen.append(request.headers["x-goog-api-key"])
        if len(seen) == 1:
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return ok()

    provider = make(monkeypatch, endpoint)
    assert list(provider.stream("Hello.", model=model(), format="pcm")) == [PCM]
    assert len(seen) == 2


def test_refusals_keep_the_one_try_per_key_walk(host, monkeypatch):
    three_keys(monkeypatch)
    gemini = Gemini({K1: [per_minute()], K2: [per_minute()], K3: [per_minute()]})
    provider = make(monkeypatch, gemini)
    with pytest.raises(voice_mod.SpeechUnavailable) as caught:
        list(provider.stream("Hello.", model=model(), format="pcm"))
    assert sorted(gemini.asked) == sorted([K1, K2, K3])
    assert "refused this sentence" in str(caught.value)


def test_the_sentence_budget_caps_each_request_and_ends_the_walk(host, monkeypatch):
    three_keys(monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(voice_mod, "_monotonic", lambda: clock["now"])
    assert voice_mod.SENTENCE_BUDGET_S == 60
    timeouts, asked = [], []

    def endpoint(request):
        key = request.headers["x-goog-api-key"]
        asked.append(key)
        timeouts.append(dict(request.extensions["timeout"]))
        if len(asked) == 1:
            clock["now"] += 45.0              # a slow refusal: 45 s of the 60 are gone
            return per_minute()
        clock["now"] += 20.0                  # the second one hangs 20 s: the budget is over
        return per_minute()

    provider = make(monkeypatch, endpoint)
    with pytest.raises(voice_mod.SpeechUnavailable) as caught:
        list(provider.stream("Hello.", model=model(), format="pcm"))
    assert len(asked) == 2, "no third request once the budget is spent"
    assert timeouts[0]["read"] == pytest.approx(60.0), timeouts[0]        # the smaller of 90 s and what is left
    assert timeouts[1]["read"] == pytest.approx(15.0), timeouts[1]
    assert timeouts[1]["connect"] <= 15.0
    assert "no Gemini key answered in time" in str(caught.value)


def test_the_first_request_of_a_sentence_is_capped_by_the_sentence_budget(host, monkeypatch):
    monkeypatch.setattr(voice_mod, "_monotonic", lambda: 1000.0)
    seen = []

    def endpoint(request):
        seen.append(dict(request.extensions["timeout"]))
        return ok()

    provider = make(monkeypatch, endpoint)
    list(provider.stream("Hello.", model=model(), format="pcm"))
    assert seen[0]["read"] == pytest.approx(60.0), "the first request is capped by the 60 s sentence budget"
    assert seen[0]["connect"] == pytest.approx(15.0)


# --- round 2 review F9: the switch off stops speech too ------------------------------------------------

def test_the_rotation_switch_off_stops_synthesize_and_stream_before_any_request(host, monkeypatch, tmp_path):
    asked = []

    def endpoint(request):
        asked.append(request)
        return ok()

    provider = make(monkeypatch, endpoint)
    monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
    with pytest.raises(voice_mod.SpeechUnavailable) as caught:
        provider.synthesize("Hello.", str(tmp_path / "x.wav"), model=model())
    assert "KAME rotation is off" in str(caught.value)
    with pytest.raises(voice_mod.SpeechUnavailable):
        list(provider.stream("Hello.", model=model(), format="pcm"))
    assert asked == [] and not (tmp_path / "x.wav").exists()
