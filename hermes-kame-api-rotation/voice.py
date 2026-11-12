"""Gemini speech through KAME's keys (1.8.4.0 V1) - a Hermes speech provider named ``kame-gemini``.

Why it exists: Hermes' built-in ``gemini`` speech reads one key (``GEMINI_API_KEY``, else
``GOOGLE_API_KEY``) and sends it as it is. A profile that keeps fourteen keys on one
``GOOGLE_API_KEY`` line would send the whole line as one key, and the first sentence would fail.
Here that line is read as the keys it holds, every sentence is asked of the healthiest key, and a
refusal is read by the same reader the chat uses, so the key rests exactly as long as the provider
said and the same sentence goes to the next key.

Speech is not the chat: the reply's text is already on the screen. So speech never waits for a key
to rest. When every key rests it raises at once with the time the first one is back (a key back in
under two seconds is waited for, once), and Hermes shows its own speech error. One sentence has one
budget of 60 s, and a second transport error in it ends it at once.

The rotation switch stops speech too: with KAME's rotation off, a sentence raises
``SpeechUnavailable("KAME rotation is off")``. The host does not fall back to another speech
provider by itself (voice mode calls ``synthesize`` without asking ``is_available``), so choose
another ``tts.provider`` in the profile if you want speech while rotation is off.

Rules kept here: the provider's stated retry number wins (refusals go through
``TRANSPORT._on_failure``); reserve keys are last (``Carousel.select``); KAME never picks a model
(the model is the owner's ``tts.model``, else Hermes' own default for Gemini speech); no key is
printed, logged, put in an error or written anywhere.

Nothing here imports the host when the module loads: the host's base class, defaults and key
resolver are imported inside the functions that need them, and a host without them gets ``None``
from ``make_provider`` (the plugin then loads as before).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import os
import threading
import time
import wave
from typing import Any, Iterator, List, Optional

import httpx

logger = logging.getLogger(__name__)

PROVIDER_NAME = "kame-gemini"
DISPLAY_NAME = "Gemini speech (KAME keys)"
SAMPLE_RATE = 24000
FALLBACK_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE = "Kore"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: A key back in less than this is waited for (once); anything longer is raised at once.
SHORT_WAIT_S = 2.0

#: One sentence has one budget (1.8.4.0 round 2, F8): a monotonic deadline that the short wait counts
#: against too. Each request's timeout is the smaller of the usual one and what is left. Without it a
#: sentence could spend 90 s on each of fourteen keys on an endpoint that does not answer.
SENTENCE_BUDGET_S = 60

#: The usual per-request timeouts (read/write/pool, connect), in seconds.
REQUEST_TIMEOUT_S = 90.0
CONNECT_TIMEOUT_S = 15.0

#: Gemini's prebuilt voices.
VOICES = (
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird",
    "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
)

#: Seams for tests, so nothing patches httpx or ``time`` globally. ``HTTP_FACTORY(base_url)`` returns
#: the client to use (it is never closed here); ``_sleep`` is the one place this module waits.
HTTP_FACTORY: Any = None
_sleep = time.sleep
_monotonic = time.monotonic


class SpeechUnavailable(RuntimeError):
    """No key can speak right now (none configured, or every one is resting). Raised at once."""


class SpeechError(RuntimeError):
    """The provider answered, but not with audio."""


class SpeechHTTPError(Exception):
    """A refusal, in the shape ``core.evidence.harvest`` reads: status, the response with its body, a message."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.response = response
        self.message = f"Gemini speech API error (HTTP {response.status_code}): {_detail(response)}"
        super().__init__(self.message)


def _detail(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
        text = error.get("message") if isinstance(error, dict) else ""
    except Exception:
        text = ""
    text = str(text or "").strip()
    return text[:400] if text else "no detail"


# --- keys -----------------------------------------------------------------------------------------

_SOURCE_LOCK = threading.Lock()
_SOURCE: Any = None          # (digest of the resolved text, _KeySource)


def _facade() -> Any:
    from . import facade

    return facade


def _resolve_raw() -> str:
    """Hermes' own answer for the Gemini speech key, else the environment's. May hold several keys."""
    try:
        from tools.tool_backend_helpers import resolve_provider_secret

        return str(resolve_provider_secret("GEMINI_API_KEY", "gemini")
                   or resolve_provider_secret("GOOGLE_API_KEY", "gemini") or "").strip()
    except Exception:
        return str(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def _keys() -> List[str]:
    """Every key speech may use: the resolved line split into keys, plus the pool's own rows."""
    global _SOURCE
    raw = _resolve_raw()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with _SOURCE_LOCK:
        if _SOURCE is None or _SOURCE[0] != digest:
            _SOURCE = (digest, _facade()._KeySource("gemini", raw))
        source = _SOURCE[1]
    return source.keys()


# --- configuration (read-only, never required) -------------------------------------------------------

def _tts_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        section = config.get("tts") if isinstance(config, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _configured_model() -> str:
    value = _tts_config().get("model")
    return value.strip() if isinstance(value, str) else ""


def _host_default_model() -> str:
    try:
        from tools.tts_tool_providers import DEFAULT_GEMINI_TTS_MODEL

        return str(DEFAULT_GEMINI_TTS_MODEL or "").strip() or FALLBACK_MODEL
    except Exception:
        return FALLBACK_MODEL


def _base_url() -> str:
    gemini = _tts_config().get("gemini")
    value = gemini.get("base_url") if isinstance(gemini, dict) else ""
    base = str(value or "").strip() or DEFAULT_BASE_URL
    return base.rstrip("/")


# --- the request ---------------------------------------------------------------------------------------

_CLIENT_LOCK = threading.Lock()
_CLIENTS: dict = {}          # base URL -> the one client speech uses for it


def _http(base_url: str) -> Any:
    """The client to use: the injected one, else one per base URL, made once and kept.

    Hermes' ``build_keepalive_http_client`` returns a NEW ``httpx.Client`` on every call (sync
    clients without a proxy share one transport underneath; proxy-backed ones get their own pool).
    Asking it per sentence and never closing the result would leave a client, and behind a proxy a
    connection pool, behind every sentence. So the client is made once per base URL and reused,
    which keeps Hermes' pool and proxy policy and never grows."""
    if HTTP_FACTORY is not None:
        return HTTP_FACTORY(base_url)
    with _CLIENT_LOCK:
        client = _CLIENTS.get(base_url)
        if client is None:
            client = _facade()._keepalive_http(base_url)
            if client is None:
                client = httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0))
            _CLIENTS[base_url] = client
        return client


def _language() -> str:
    """The speech language the user set, or ``""`` (the model picks, as before 1.8.4.2).

    ``tts.kame-gemini.language`` beats ``tts.language``. Only a plausible language tag is kept: at most 35
    characters, letters, digits and ``-`` (``pt-BR``, ``en-US``, ``cmn-CN``); anything else is ignored."""
    config = _tts_config()
    own = config.get(PROVIDER_NAME)
    for value in ((own.get("language") if isinstance(own, dict) else None), config.get("language")):
        if isinstance(value, str):
            tag = value.strip()
            if tag and len(tag) <= 35 and all(c.isascii() and (c.isalnum() or c == "-") for c in tag):
                return tag
            if tag:
                return ""       # a value was set and it is not a tag: ignored, the next key is not tried
    return ""


def _payload(text: str, voice: str, language: str = "") -> dict:
    speech: dict = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
    if language:
        speech["languageCode"] = language       # 1.8.4.2 (V3): live, Gemini speech accepts "pt-BR"
    return {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": speech,
        },
    }


def _pcm_of(response: httpx.Response) -> bytes:
    try:
        parts = response.json()["candidates"][0]["content"]["parts"]
        part = next((p for p in parts if "inlineData" in p or "inline_data" in p), None)
        data = (part.get("inlineData") or part.get("inline_data") or {}).get("data", "") if part else ""
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        raise SpeechError(f"Gemini speech response was malformed: {type(error).__name__}") from None
    if not data:
        raise SpeechError("Gemini speech returned no audio")
    try:
        return base64.b64decode(data)
    except (binascii.Error, ValueError):
        raise SpeechError("Gemini speech returned audio that could not be decoded") from None


def _count_request(transport: Any, identity: str) -> None:
    """Count the request where the chat counts requests (E5/E6). A counter never costs a sentence."""
    try:
        transport._count_request(None, identity)
    except Exception:  # pragma: no cover - a counter
        logger.debug("kame: speech request not counted", exc_info=True)


#: F12 (measured live 2026-11-08): Google's own words when the speech model answered a sentence as
#: chat instead of speaking it ("Hello there." sent bare). The same sentence also came back as a 200
#: with ``finishReason: OTHER`` and no content. Both are the sentence, not the key.
_NO_SPEECH_400 = "Model tried to generate text"

#: How each try of one sentence is framed: as Hermes sends it (bare); in Hermes' own TRANSCRIPT frame
#: (``tools/tts_tool_providers.py::_compose_gemini_tts_prompt``); as ``Say: "..."``. Measured: the
#: last two turned the failing sentence into 0.85 s of speech, and the frame itself is not spoken
#: (the same sentence was as long or shorter framed than bare).
_FRAMINGS = (
    lambda text: text,
    lambda text: "Synthesize speech from the TRANSCRIPT only.\n\n#### TRANSCRIPT\n" + text,
    lambda text: 'Say: "' + text + '"',
)


def _no_speech(response: httpx.Response) -> bool:
    """A 400 in which Google says the model wrote text instead of speech."""
    return response.status_code == 400 and _NO_SPEECH_400 in _detail(response)


def _speak(text: str, voice: str, model: str) -> bytes:
    """One sentence as 24 kHz mono 16-bit PCM, on as many keys as it takes (at most one try per key).

    One sentence has one budget (``SENTENCE_BUDGET_S``, the short wait included), and a second
    transport error in the same sentence ends it at once: the endpoint cannot be reached, and
    fourteen more keys would not change that. A refusal is not a transport error: those keep the
    one-try-per-key walk."""
    from .core.carousel import format_duration

    transport = _facade().TRANSPORT
    # F9: the rotation switch stops speech too. The host does not ask ``is_available`` before it
    # calls ``synthesize`` in voice mode, so the provider cannot rely on being skipped.
    if not _facade().enabled():
        raise SpeechUnavailable("KAME rotation is off")
    engine = transport.engine
    identity = "gemini:" + model
    keys = _keys()
    if not keys:
        raise SpeechUnavailable("no Gemini key")
    deadline = _monotonic() + SENTENCE_BUDGET_S
    base = _base_url()
    url = f"{base}/models/{model}:generateContent"
    language = _language()
    framing = 0
    attempt, waited, last, unreachable = 0, False, "", 0
    # A reframed try is not a key's turn (F12): the key was not at fault. The framings and the
    # sentence's budget bound it.
    while attempt - framing < len(keys):
        left = deadline - _monotonic()
        if left <= 0:
            raise SpeechUnavailable("no Gemini key answered in time")
        key, status = engine.select(identity, keys)
        if status == "EMPTY" or not key:
            raise SpeechUnavailable("no Gemini key")
        if status == "EXHAUSTED":
            back = engine.next_recovery_seconds(identity, keys)
            if back is not None and back < SHORT_WAIT_S and not waited:
                waited = True
                _sleep(max(0.0, back) + 0.05)
                continue
            when = format_duration(back) if back is not None else "a moment"
            raise SpeechUnavailable(f"every Gemini key is resting; the first is back in {when}")
        attempt += 1
        _count_request(transport, identity)
        body = _payload(_FRAMINGS[framing](text), voice, language)
        try:
            left = max(0.001, deadline - _monotonic())
            response = _http(base).post(
                url, json=body,
                timeout=httpx.Timeout(min(REQUEST_TIMEOUT_S, left), connect=min(CONNECT_TIMEOUT_S, left)),
                headers={"Content-Type": "application/json", "x-goog-api-key": key})
            if response.status_code != 200:
                response.read()
                if _no_speech(response):
                    raise SpeechError("Gemini speech wrote text instead of speaking this sentence")
                raise SpeechHTTPError(response)
            pcm = _pcm_of(response)
        except SpeechError as error:
            # F12: the key answered; the sentence was not spoken. The key is not at fault and does not
            # rest; the next try (the next key, inside this sentence's budget) is framed differently.
            engine.mark(identity, key, True)
            framing += 1
            if framing >= len(_FRAMINGS):
                raise SpeechError(f"Gemini speech gave no audio for this sentence ({error})") from None
            continue
        except (SpeechHTTPError, httpx.HTTPError) as error:
            try:
                verdict = transport._on_failure(identity, key, error, "speech", attempt, streamed=False)[0]
            except Exception:
                logger.debug("kame: speech refusal could not be read", exc_info=True)
                verdict = "raise"
            if isinstance(error, httpx.TransportError):
                unreachable += 1
                if unreachable >= 2:
                    # the key above rested as the chat's reader decided; the sentence ends here
                    raise SpeechUnavailable(
                        f"Gemini speech could not be reached ({type(error).__name__})") from None
            if verdict == "rotate":
                last = str(error)
                continue
            raise
        engine.mark(identity, key, True)
        return pcm
    raise SpeechUnavailable("every Gemini key refused this sentence" + (f": {last}" if last else ""))


def wrap_wav(pcm: bytes) -> bytes:
    """Raw PCM as a RIFF WAV: 24000 Hz, 16-bit, mono."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


# --- the provider -------------------------------------------------------------------------------------------

def make_provider() -> Optional[Any]:
    """The speech provider, or ``None`` on a host without ``agent.tts_provider.TTSProvider``."""
    try:
        from agent.tts_provider import TTSProvider
    except Exception:
        return None

    class KameGeminiSpeech(TTSProvider):  # type: ignore[misc, valid-type]
        streams_pcm = True
        stream_sample_rate = SAMPLE_RATE

        @property
        def name(self) -> str:
            return PROVIDER_NAME

        @property
        def display_name(self) -> str:
            return DISPLAY_NAME

        @property
        def voice_compatible(self) -> bool:
            return True

        def is_available(self) -> bool:
            try:
                return bool(_facade().enabled() and _keys())
            except Exception:
                return False

        def list_voices(self) -> list:
            return [{"id": voice, "display": voice} for voice in VOICES]

        def default_voice(self) -> Optional[str]:
            return DEFAULT_VOICE

        def default_model(self) -> Optional[str]:
            return _host_default_model()

        def list_models(self) -> list:
            default = _host_default_model()
            models = [{"id": default, "display": default}]
            chosen = _configured_model()
            if chosen and chosen != default:
                models.append({"id": chosen, "display": chosen})
            return models

        def synthesize(self, text, output_path, *, voice=None, model=None, speed=None, format="mp3", **extra) -> str:
            pcm = _speak(str(text), voice or DEFAULT_VOICE, model or self.default_model())
            root, extension = os.path.splitext(str(output_path))
            written = str(output_path) if extension.lower() == ".wav" else root + ".wav"
            with open(written, "wb") as handle:
                handle.write(wrap_wav(pcm))
            return written

        def stream(self, text, *, voice=None, model=None, format="opus", **extra) -> Iterator[bytes]:
            if str(format).lower() != "pcm":
                raise NotImplementedError("kame-gemini streams raw PCM only; use synthesize for other formats")
            return self._chunks(str(text), voice or DEFAULT_VOICE, model or self.default_model())

        @staticmethod
        def _chunks(text: str, voice: str, model: str) -> Iterator[bytes]:
            yield _speak(text, voice, model)

    return KameGeminiSpeech()
