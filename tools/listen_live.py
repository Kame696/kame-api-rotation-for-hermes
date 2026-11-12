"""Live check of the Gemini listening provider (1.8.4.2 V2), outside the owner's Hermes config.

Loads the plugin from this source tree (never the installed Hermes: the host's two base classes are small
stand-ins, the same shape the offline tests use), points KAME's state at a scratch ``HERMES_HOME`` so nothing
of the owner's pool is touched, speaks a Portuguese sentence with the plugin's own speech provider, then
transcribes that recording with the listening provider. The caller puts the keys in this process's
environment from where they live (``GOOGLE_API_KEY`` / ``GEMINI_API_KEY``); nothing here prints, logs or
writes a key. It prints the seconds of each step, the transcript, and whether the words match.

    GOOGLE_API_KEY=... python tools/listen_live.py [--sentence TEXT] [--model MODEL]

Exit 0 when the transcript has at least 80 percent of the spoken words.
"""

from __future__ import annotations

import abc
import argparse
import importlib
import importlib.util
import os
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_listen_live"

SENTENCE = "Bom dia, quero saber como esta o tempo hoje em Sao Paulo e se vai chover a tarde."


class _TTSProvider(abc.ABC):
    streams_pcm = False
    stream_sample_rate = None

    @property
    @abc.abstractmethod
    def name(self): ...

    def is_available(self):
        return True

    def list_models(self):
        return []

    def default_model(self):
        return None

    def list_voices(self):
        return []

    def default_voice(self):
        return None

    @abc.abstractmethod
    def synthesize(self, text, output_path, *, voice=None, model=None, speed=None, format="mp3", **extra): ...

    @property
    def voice_compatible(self):
        return False


class _TranscriptionProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self): ...

    @property
    def display_name(self):
        return self.name

    def is_available(self):
        return True

    def list_models(self):
        return []

    def default_model(self):
        return None

    @abc.abstractmethod
    def transcribe(self, file_path, *, model=None, language=None, **extra): ...


def words(text: str) -> list:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", plain)


def load_plugin() -> ModuleType:
    tts = ModuleType("agent.tts_provider")
    tts.TTSProvider = _TTSProvider
    stt = ModuleType("agent.transcription_provider")
    stt.TranscriptionProvider = _TranscriptionProvider
    sys.modules["agent.tts_provider"] = tts
    sys.modules["agent.transcription_provider"] = stt
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sentence", default=SENTENCE)
    parser.add_argument("--model", default="", help="listening model (default: the plugin's own default)")
    parser.add_argument("--voice", default="Kore")
    args = parser.parse_args()

    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        print("no GOOGLE_API_KEY / GEMINI_API_KEY in this process's environment")
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="kame-listen-live-"))
    os.environ["HERMES_HOME"] = str(scratch / "home")
    (scratch / "home").mkdir()

    load_plugin()
    voice = importlib.import_module(f"{PACKAGE}.voice")
    listen = importlib.import_module(f"{PACKAGE}.listen")
    speaker = voice.make_provider()
    listener = listen.make_provider()
    if speaker is None or listener is None:
        print("no provider (speaker %s, listener %s)" % (speaker is not None, listener is not None))
        return 1
    print(f"listener {listener.name}  keys {len(voice._keys())}  model {args.model or listener.default_model()}  "
          f"available {listener.is_available()}")

    began = time.monotonic()
    try:
        path = speaker.synthesize(args.sentence, str(scratch / "listen_live.mp3"), voice=args.voice)
    except Exception as error:
        print(f"speak       FAILED  {time.monotonic() - began:.2f}s  {type(error).__name__}: {error}")
        return 1
    print(f"speak       ok  {time.monotonic() - began:.2f}s  {Path(path).stat().st_size} bytes  {Path(path).name}")

    began = time.monotonic()
    answer = listener.transcribe(path, model=args.model or None, language="pt")
    took = time.monotonic() - began
    if not answer.get("success"):
        print(f"listen      FAILED  {took:.2f}s  {answer.get('error')}")
        return 1
    transcript = answer["transcript"]
    wanted = words(args.sentence)
    heard = set(words(transcript))
    matched = sum(1 for word in wanted if word in heard)
    share = matched / max(1, len(wanted))
    print(f"listen      ok  {took:.2f}s  {len(transcript)} chars")
    print(f"transcript  {transcript}")
    print(f"words       {matched} of {len(wanted)} ({100 * share:.0f} percent)")
    ok = share >= 0.8
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
