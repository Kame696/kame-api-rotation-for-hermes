"""Gemini listening through KAME's keys (1.8.4.2 V2) - a Hermes speech-to-text provider named ``kame-gemini``.

Why it exists: Hermes' voice mode listens with a local Whisper by default. On an 8 GB PC that took 7 s for
5 s of speech with 0.8 GB of memory free. A Gemini model transcribed a 4.8 s Portuguese clip in 1.7 s,
exactly (measured live 2026-11-11, the audio exactly as Desktop records it). This provider sends the
recording to Gemini on the keys the profile already keeps, rotated and rested by the same reader the chat
and the speech provider use, so a key resting for the chat rests for listening too.

Off by default: it is used only when the user sets ``stt.provider: kame-gemini`` in Hermes' config. The
language and a vocabulary hint come from Hermes (``language`` and ``stt.prompt``); the plugin adds no
setting. Listening never waits for a key to rest: a person has just spoken and is waiting, so a key back
in under two seconds is waited for once and anything longer ends the transcription at once with an error
envelope. One transcription has one budget of 30 s.

Rules kept here: the provider's stated retry number wins (refusals go through ``TRANSPORT._on_failure``);
reserve keys are last (``Carousel.select``); KAME never picks a model (the ``model`` argument, else
``stt.kame-gemini.model``, else Hermes' own auxiliary default for Gemini); no key, no audio and no
transcript text is printed, logged, put in an error or written anywhere.

Nothing here imports the host when the module loads: the host's base class and defaults are imported inside
the functions that need them, and a host without them gets ``None`` from ``make_provider`` (the plugin then
loads as before).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from . import voice

logger = logging.getLogger(__name__)

PROVIDER_NAME = "kame-gemini"
DISPLAY_NAME = "Gemini listening (KAME keys)"
FALLBACK_MODEL = "gemini-3.6-flash"

#: One transcription has one budget (the short wait counts against it).
UTTERANCE_BUDGET_S = 30.0
#: The per-request timeouts (read/write/pool, connect), in seconds.
REQUEST_TIMEOUT_S = 25.0
CONNECT_TIMEOUT_S = 10.0
#: A key back in less than this is waited for (once); anything longer is an error at once.
SHORT_WAIT_S = voice.SHORT_WAIT_S
#: Larger files are refused before anything is sent (the request carries the audio inline).
MAX_BYTES = 15 * 1024 * 1024
#: At most this much of the host's vocabulary hint goes into the prompt.
PROMPT_HINT_MAX = 500

BASE_PROMPT = "Transcribe this audio verbatim. Reply with the transcript only, no comments."

_MIME = {
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".mp3": "audio/mp3", ".flac": "audio/flac", ".aac": "audio/aac",
    ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aiff": "audio/aiff", ".aif": "audio/aiff",
}

#: Seams for tests; ``voice.HTTP_FACTORY`` is the HTTP seam (the same client factory speech uses).
_sleep = time.sleep
_monotonic = time.monotonic


class ListenError(RuntimeError):
    """A transcription that cannot succeed; its text is the error envelope's ``error`` (never a key)."""


class ListenHTTPError(Exception):
    """A refusal, in the shape ``core.evidence.harvest`` reads: status, the response with its body, a message."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.response = response
        self.message = f"Gemini API error (HTTP {response.status_code}): {voice._detail(response)}"
        super().__init__(self.message)


# --- configuration (read-only, never required) -------------------------------------------------------

def _stt_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        section = config.get("stt") if isinstance(config, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _configured_model() -> str:
    own = _stt_config().get(PROVIDER_NAME)
    value = own.get("model") if isinstance(own, dict) else ""
    return value.strip() if isinstance(value, str) else ""


def _host_default_model() -> str:
    """Hermes' own auxiliary default for Gemini, else ``FALLBACK_MODEL``. Never fails."""
    try:
        import importlib

        auxiliary_client = importlib.import_module("agent.auxiliary_client")
        table = getattr(auxiliary_client, "_API_KEY_PROVIDER_AUX_MODELS", None) or {}
        return str(table.get("gemini") or "").strip() or FALLBACK_MODEL
    except Exception:
        return FALLBACK_MODEL


def _base_url() -> str:
    gemini = _stt_config().get("gemini")
    value = gemini.get("base_url") if isinstance(gemini, dict) else ""
    return (str(value or "").strip() or voice.DEFAULT_BASE_URL).rstrip("/")


# --- the request -------------------------------------------------------------------------------------

def _thinking_config(model: str) -> Optional[dict]:
    """No thinking for a transcript: ``thinkingBudget: 0`` on 2.5, ``thinkingLevel: minimal`` on 3 and later."""
    name = model.lower()
    if "gemini-2.5" in name:
        return {"thinkingBudget": 0}
    try:
        after = name.split("gemini-", 1)[1]
        major = int("".join(ch for ch in after.split(".", 1)[0].split("-", 1)[0] if ch.isdigit()) or "0")
    except (IndexError, ValueError):
        return None
    return {"thinkingLevel": "minimal"} if major >= 3 else None


def _prompt(language: Optional[str], hint: Any) -> str:
    text = BASE_PROMPT
    tag = str(language or "").strip()
    if tag:
        text += f" The speaker's language is probably {tag}."
    words = str(hint or "").strip()[:PROMPT_HINT_MAX].strip()
    if words:
        text += f" Words that may appear: {words}"
    return text


def _payload(prompt: str, mime: str, data: str, thinking: Optional[dict]) -> dict:
    config: Dict[str, Any] = {"temperature": 0}
    if thinking:
        config["thinkingConfig"] = thinking
    return {
        "contents": [{"role": "user", "parts": [
            {"text": prompt}, {"inlineData": {"mimeType": mime, "data": data}}]}],
        "generationConfig": config,
    }


def _text_of(response: httpx.Response) -> str:
    try:
        parts = response.json()["candidates"][0]["content"].get("parts") or []
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        # a candidate with no content is silence (finishReason OTHER / STOP with nothing), not an error
        try:
            candidates = response.json().get("candidates")
        except Exception:
            candidates = None
        if candidates:
            return ""
        raise ListenError("Gemini listening response was malformed") from None
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()


def _names_thinking(response: httpx.Response) -> bool:
    return response.status_code == 400 and "think" in voice._detail(response).lower()


def _transcribe(file_path: str, model: str, language: Optional[str], hint: Any) -> str:
    """The transcript of one recording, on as many keys as the budget allows. Raises ``ListenError`` (or the
    provider's own refusal) when it cannot."""
    from .core.carousel import format_duration

    suffix = os.path.splitext(str(file_path))[1].lower()
    mime = _MIME.get(suffix)
    if mime is None:
        raise ListenError(f"unsupported audio type {suffix or 'without an extension'}")
    size = os.path.getsize(file_path)
    if size > MAX_BYTES:
        raise ListenError("audio file is larger than 15 MB")
    with open(file_path, "rb") as handle:
        data = base64.b64encode(handle.read()).decode("ascii")

    transport = voice._facade().TRANSPORT
    if not voice._facade().enabled():
        raise ListenError("KAME rotation is off")
    engine = transport.engine
    identity = "gemini:" + model
    keys = voice._keys()
    if not keys:
        raise ListenError("no Gemini key")
    deadline = _monotonic() + UTTERANCE_BUDGET_S
    base = _base_url()
    url = f"{base}/models/{model}:generateContent"
    prompt = _prompt(language, hint)
    thinking = _thinking_config(model)
    attempt, waited, last, unreachable = 0, False, "", 0
    retry_key: Optional[str] = None
    started = time.perf_counter()
    while attempt < len(keys):
        left = deadline - _monotonic()
        if left <= 0:
            raise ListenError("no Gemini key answered in time")
        if retry_key is not None:
            key, status, retry_key = retry_key, "SUCCESS", None
        else:
            key, status = engine.select(identity, keys)
        if status == "EMPTY" or not key:
            raise ListenError("no Gemini key")
        if status == "EXHAUSTED":
            back = engine.next_recovery_seconds(identity, keys)
            if back is not None and back < SHORT_WAIT_S and not waited:
                waited = True
                _sleep(max(0.0, back) + 0.05)
                continue
            when = format_duration(back) if back is not None else "a moment"
            raise ListenError(f"every Gemini key is resting; the first is back in {when}")
        attempt += 1
        voice._count_request(transport, identity)
        try:
            left = max(0.001, deadline - _monotonic())
            response = voice._http(base).post(
                url, json=_payload(prompt, mime, data, thinking),
                timeout=httpx.Timeout(min(REQUEST_TIMEOUT_S, left), connect=min(CONNECT_TIMEOUT_S, left)),
                headers={"Content-Type": "application/json", "x-goog-api-key": key})
            if response.status_code != 200:
                response.read()
                if thinking and _names_thinking(response):
                    # the model does not take this thinking setting: the same key, once more, without it
                    thinking = None
                    attempt -= 1
                    retry_key = key
                    continue
                raise ListenHTTPError(response)
            text = _text_of(response)
        except (ListenHTTPError, httpx.HTTPError) as error:
            try:
                verdict = transport._on_failure(identity, key, error, "listening", attempt, streamed=False)[0]
            except Exception:
                logger.debug("kame: listening refusal could not be read", exc_info=True)
                verdict = "raise"
            if isinstance(error, httpx.TransportError):
                unreachable += 1
                if unreachable >= 2:
                    raise ListenError(f"Gemini could not be reached ({type(error).__name__})") from None
            if verdict == "rotate":
                last = str(error)
                continue
            raise
        engine.mark(identity, key, True)
        logger.info("kame: listened %.1f s of audio in %.1f s on %s", _seconds_of(size, suffix),
                    time.perf_counter() - started, model)
        return text
    raise ListenError("every Gemini key refused this recording" + (f": {last}" if last else ""))


def _seconds_of(size: int, suffix: str) -> float:
    """A rough length for the log line only (compressed audio is about 2 KB per second)."""
    return size / (176400.0 if suffix == ".wav" else 2000.0)


# --- the provider -------------------------------------------------------------------------------------------

def make_provider() -> Optional[Any]:
    """The listening provider, or ``None`` on a host without ``agent.transcription_provider``."""
    try:
        from agent.transcription_provider import TranscriptionProvider
    except Exception:
        return None

    class KameGeminiListening(TranscriptionProvider):  # type: ignore[misc, valid-type]
        @property
        def name(self) -> str:
            return PROVIDER_NAME

        @property
        def display_name(self) -> str:
            return DISPLAY_NAME

        def is_available(self) -> bool:
            try:
                return bool(voice._facade().enabled() and voice._keys())
            except Exception:
                return False

        def default_model(self) -> Optional[str]:
            return _host_default_model()

        def list_models(self) -> list:
            default = _host_default_model()
            models = [{"id": default, "display": default}]
            chosen = _configured_model()
            if chosen and chosen != default:
                models.append({"id": chosen, "display": chosen})
            return models

        def transcribe(self, file_path, *, model=None, language=None, **extra) -> dict:
            try:
                chosen = (str(model).strip() if model else "") or _configured_model() or self.default_model()
                text = _transcribe(str(file_path), chosen, language, (extra or {}).get("prompt"))
                return {"success": True, "transcript": text, "provider": PROVIDER_NAME}
            except Exception as error:      # never raises: the host expects an envelope
                if isinstance(error, (ListenError, ListenHTTPError)):
                    message = str(error)
                else:
                    message = f"Gemini listening failed ({type(error).__name__})"
                return {"success": False, "transcript": "", "provider": PROVIDER_NAME, "error": message}

    return KameGeminiListening()
