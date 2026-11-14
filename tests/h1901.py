"""1.9.0.1 - shared plans, chains and the runner for the "every user, every provider" tests.

Imports the fixture ``guard`` and the fakes of ``test_1_8_4_3_quiet_guard`` (real time, real threads, a quiet
bound of 0.2 s). A test file imports ``guard`` from here so pytest finds it as a fixture.
"""
from __future__ import annotations

import time

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_4_3_quiet_guard import (  # noqa: F401
    SeqCall, chain_mod, chunk, guard, reasons, think, transport, settings, KEYS, EVENTS, BOUND)

G = "gemini-race"
G2 = "gemini-race-two"
GROQ = "llama-x"

MIXED = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: groq
    model: llama-x
"""
MIXED_SAME_FIRST = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: groq
    model: llama-x
"""
NO_LIST = """\
model:
  provider: gemini
  default: gemini-race
"""
SAME_ONLY = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
"""
MAIN_ONLY_LATER = """\
model:
  provider: groq
  default: llama-x
fallback_providers:
  - provider: gemini
    model: gemini-race
"""


def stall():
    """Thinks once, then nothing for 30 s (a stream the test cap cuts)."""
    return [0.02, think("thinking"), 30.0, chunk("late")]


def silent():
    return [30.0, chunk("late", finish="stop")]


def answer(text="ok", delay=0.05):
    return [delay, chunk(text + " "), chunk("done", finish="stop")]


def busy():
    return [0.05, R.Refusal(503, "The model is overloaded. Please try again later.")]


def visible_then_silent(text="visible "):
    return [0.05, chunk(text), 30.0, chunk("rest", finish="stop")]


def setup(tmp_path, chain):
    (tmp_path / "config.yaml").write_text(chain, encoding="utf-8")
    chain_mod.forget()
    settings.forget()


class Result:
    """What a driven reply did. Unpacks as ``(shown, error, seconds, chunks)``; ``cut`` is True when the reply
    was still going at the cap and the test ended it (the reply was WAITING)."""

    def __init__(self, out, err, took, cut):
        self.out, self.err, self.took, self.cut = out, err, took, cut
        self.shown = R.shown(out)

    def __iter__(self):
        return iter((self.shown, self.err, self.took, self.out))


def run(t, call, cap=8.0):
    """Drive the transport on a thread for at most ``cap`` seconds; a reply still running then is cancelled
    (``call.closed``) and reported with ``cut=True`` and ``took == cap``."""
    import threading

    out, box = [], {}
    started = time.monotonic()

    def drive():
        try:
            for c in t.stream(call):
                out.append(c)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e
        box["done"] = time.monotonic()

    th = threading.Thread(target=drive, daemon=True, name="h1901-drive")
    th.start()
    th.join(cap)
    cut = th.is_alive()
    if cut:
        call.closed = True
        th.join(5.0)
        return Result(out, None, cap, True)
    return Result(out, box.get("err"), box["done"] - started, False)


def opened_models(call):
    return [m for m, _, _ in call.opened]
