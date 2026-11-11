"""1.8.4.0 E2: a very short cut answer is not repeated.

Measured by ``probes/dup_repro.py``: with fewer than ``MIN_OVERLAP`` comparable characters
shown, a model that starts the answer again ("Hello" then "Hello world") was shown twice,
because neither the tail overlap nor the long-head restart test can ask with so little text.
The same table is in the Agent Zero port (``tests/test_v1_8_4_0_stitch_short.py``).
"""
from __future__ import annotations

import importlib

import pytest

from test_transport_1819 import PACKAGE  # noqa: F401  (loads the package under test)

stitch = importlib.import_module(f"{PACKAGE}.core.stitch")

# (seen, fresh, text that must reach the screen after the seam)
TABLE = [
    ("Hello", "Hello world", " world"),
    ("I", "I think so", " think so"),
    ("Hello", "hello world", " world"),          # case rules stay the module's
    ("The", " cat sat", " cat sat"),              # a continuation, not a restart
    ("Hi", "Hi", ""),                             # the whole answer again
    ("Hello", "Goodbye now", "Goodbye now"),      # new text
]


def _run(seen: str, fresh: str) -> "tuple[str, str]":
    stitcher = stitch.Stitcher(seen)
    out = stitcher.feed(fresh) + stitcher.flush()
    return out, stitcher.mode


@pytest.mark.parametrize("seen,fresh,expected", TABLE)
def test_short_seen_table(seen, fresh, expected):
    out, _mode = _run(seen, fresh)
    assert out == expected


def test_restart_mode_is_named():
    assert _run("Hello", "Hello world")[1] == "restart"
    assert _run("The", " cat sat")[1] == "new"


def test_restart_across_several_deltas():
    stitcher = stitch.Stitcher("Hello")
    shown = ""
    for chunk in ["Hel", "lo wor", "ld, and more"]:
        shown += stitcher.feed(chunk)
    shown += stitcher.flush()
    assert shown == " world, and more"


def test_empty_seen_is_untouched():
    assert _run("", "Hello world")[0] == "Hello world"


def test_long_seen_rules_unchanged():
    seen = "The quick brown fox jumps over"
    out, mode = _run(seen, "jumps over the lazy dog")
    assert mode == "continuation"
    assert out == " the lazy dog"
