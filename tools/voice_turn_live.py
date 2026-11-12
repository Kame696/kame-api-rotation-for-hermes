"""Measure one voice turn end to end (1.8.4.2 M1). Run on the installed copy after a deploy.

The steps, each timed:

  1. speak a Portuguese question with the plugin's speech provider (this stands for the user's voice);
  2. transcribe the recording with the listening provider (V2);
  3. send the transcript to the running Hermes gateway (api_server, port 8642) as a streamed chat turn and
     time the first token and the end;
  4. speak the first sentence of the reply and time its first audio.

It prints one table. The plugin is loaded from this source tree with the host's base classes stubbed and a
scratch ``HERMES_HOME`` (so the owner's pool is not touched); the gateway call goes to the real, running
Hermes with the ``API_SERVER_KEY`` it already keeps in its own ``.env``. The Gemini keys come from
``GOOGLE_API_KEY`` / ``GEMINI_API_KEY`` in this process's environment. Nothing here prints, logs or writes a
key.

    GOOGLE_API_KEY=... python tools/voice_turn_live.py [--question TEXT] [--port 8642]

Exit 0 when every step answered.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import listen_live  # noqa: E402  (the stand-ins and the loader live there)

HOME = Path(os.path.expandvars(r"%LOCALAPPDATA%\hermes"))
QUESTION = "Bom dia. Em uma frase curta, o que e um mutex?"


def gateway_key() -> str:
    try:
        from dotenv import dotenv_values
        return dotenv_values(HOME / ".env", interpolate=False).get("API_SERVER_KEY") or ""
    except Exception:
        return ""


def chat(text: str, port: int, key: str, timeout: float = 600.0) -> dict:
    """A streamed chat turn: seconds to the first token and to the end, and the reply."""
    import httpx

    body = {"model": "hermes-agent", "stream": True, "messages": [{"role": "user", "content": text}]}
    began = time.monotonic()
    first = None
    parts = []
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        with client.stream("POST", f"http://127.0.0.1:{port}/v1/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {key}"}) as response:
            if response.status_code != 200:
                return {"ok": False, "error": f"HTTP {response.status_code}", "first": None,
                        "end": time.monotonic() - began, "text": ""}
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                except Exception:
                    continue
                if delta:
                    if first is None:
                        first = time.monotonic() - began
                    parts.append(delta)
    return {"ok": bool(parts), "error": "" if parts else "empty reply", "first": first,
            "end": time.monotonic() - began, "text": "".join(parts)}


def first_sentence(text: str) -> str:
    match = re.search(r"(.+?[.!?])(\s|$)", text.strip(), re.S)
    return (match.group(1) if match else text.strip())[:300]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--voice", default="Kore")
    args = parser.parse_args()

    if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        print("no GOOGLE_API_KEY / GEMINI_API_KEY in this process's environment")
        return 2
    token = gateway_key()
    if not token:
        print("no API_SERVER_KEY in the gateway's .env")
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="kame-voice-turn-"))
    os.environ["HERMES_HOME"] = str(scratch / "home")
    (scratch / "home").mkdir()

    listen_live.load_plugin()
    voice = importlib.import_module(f"{listen_live.PACKAGE}.voice")
    listen = importlib.import_module(f"{listen_live.PACKAGE}.listen")
    speaker = voice.make_provider()
    listener = listen.make_provider()
    if speaker is None or listener is None:
        print("no provider")
        return 1

    rows = []
    failed = False

    def step(label, run):
        nonlocal failed
        began = time.monotonic()
        try:
            value, note = run()
            rows.append((label, time.monotonic() - began, "ok", note))
            return value
        except Exception as error:
            failed = True
            rows.append((label, time.monotonic() - began, "FAILED", f"{type(error).__name__}: {error}"))
            return None

    path = step("speak the question", lambda: (
        speaker.synthesize(args.question, str(scratch / "question.mp3"), voice=args.voice), "the user's voice"))
    transcript = None
    if path:
        def hear():
            answer = listener.transcribe(path, language="pt")
            if not answer.get("success"):
                raise RuntimeError(answer.get("error") or "no transcript")
            return answer["transcript"], f"{len(answer['transcript'])} chars"
        transcript = step("listen (transcribe)", hear)
    reply = None
    if transcript:
        result = chat(transcript, args.port, token)
        if result["ok"]:
            rows.append(("gateway first token", result["first"], "ok", ""))
            rows.append(("gateway reply complete", result["end"], "ok", f"{len(result['text'])} chars"))
            reply = result["text"]
        else:
            failed = True
            rows.append(("gateway reply", result["end"], "FAILED", result["error"]))
    if reply:
        sentence = first_sentence(reply)

        def first_audio():
            began = time.monotonic()
            for chunk in speaker.stream(sentence, voice=args.voice, format="pcm"):
                if chunk:
                    return time.monotonic() - began, f"{len(sentence)} chars spoken"
            raise RuntimeError("no audio")
        got = step("speak the reply (first audio)", first_audio)
        if got is not None:
            rows[-1] = (rows[-1][0], got, rows[-1][2], rows[-1][3])

    print(f"{'step':<34}{'seconds':>9}  {'result':<8} note")
    for label, seconds, result, note in rows:
        print(f"{label:<34}{seconds:>9.2f}  {result:<8} {note}")
    print("PASS" if not failed else "FAIL")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
