"""1.8.4.0 round 2 (R5): a true continuation does not repeat the whitespace already shown.

The continuation request carries the shown text with its trailing whitespace stripped (a provider
refuses a prefill that ends in it), so a model that continues opens with that whitespace again.
Before R5 the stitcher let it through whenever the continuation shared no text with the shown one
("new" mode): "fox " then " jumps" showed "fox  jumps", and in a code block a newline and four spaces
then a newline and "    return x" doubled the line, on the screen and in the answer kept in the history. Found by
Agent Zero ``tools/race_chaos.py`` (seed 32), whose stand-in model continues a cut answer half the
time. Only whitespace identical to what was shown is consumed. Same table in the Agent Zero port
(``tests/test_v1_8_4_0_stitch_boundary.py``).
"""
from __future__ import annotations

import importlib

import pytest

from test_transport_1819 import PACKAGE  # noqa: F401  (loads the package under test)

stitch = importlib.import_module(f"{PACKAGE}.core.stitch")

# (seen, fresh, text that must reach the screen after the seam)
TABLE = [
    ("the quick brown fox ", " jumps", "jumps"),
    ("def f():\n    ", "\n    return x", "return x"),           # the indent is not doubled
    ("def f():\n    ", "    return x", "    return x"),         # not the shown whitespace: kept whole
    ("First paragraph.\n\n", "\n\nSecond.", "Second."),
    ("First paragraph.\n", "\n\nSecond.", "\nSecond."),          # one more newline than shown stays
    ("the quick brown fox ", "jumps", "jumps"),                  # nothing to consume
    ("the quick brown fox ", "\n    next", "\n    next"),        # a different separator stays whole
    ("The", " cat sat", " cat sat"),                             # nothing was shown after the word
]


def _run(seen: str, fresh: str) -> "tuple[str, str]":
    stitcher = stitch.Stitcher(seen)
    out = stitcher.feed(fresh) + stitcher.flush()
    return out, stitcher.mode


@pytest.mark.parametrize("seen,fresh,expected", TABLE)
def test_boundary_table(seen, fresh, expected):
    out, mode = _run(seen, fresh)
    assert mode == "new"
    assert out == expected


def test_the_boundary_across_several_deltas():
    stitcher = stitch.Stitcher("def f():\n    ")
    shown = "".join(stitcher.feed(c) for c in ["\n  ", "  ret", "urn x"]) + stitcher.flush()
    assert shown == "return x"


def test_a_long_continuation_keeps_its_own_text():
    seen = "x" * 30 + " "
    fresh = " " + "y" * 500
    stitcher = stitch.Stitcher(seen)
    assert stitcher.feed(fresh) + stitcher.flush() == "y" * 500
