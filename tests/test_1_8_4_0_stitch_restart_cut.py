"""1.8.4.0 round 2 (R2): a restart cut before it got past the shown text adds nothing.

Found by ``tools/race_chaos.py`` (seed 0): an answer was cut after "the quick brown ", the
continuation began the answer again and was cut a word in ("the "), and the stitcher let that
word out as new text, so the user read "the quick brown the". The fresh text was shorter than
the tail overlap (``MIN_OVERLAP``) and the restart test (``RESTART_HEAD``) can ask with, so
neither caught it. Every character of a continuation that is a prefix of the shown text is text
the user has already read. The same table is in the Agent Zero port
(``tests/test_v1_8_4_0_stitch_restart_cut.py``).
"""
from __future__ import annotations

import importlib

import pytest

from test_transport_1819 import PACKAGE  # noqa: F401  (loads the package under test)

stitch = importlib.import_module(f"{PACKAGE}.core.stitch")

# (seen, fresh, text that must reach the screen after the seam)
TABLE = [
    ("the quick brown ", "the ", ""),                 # race_chaos seed 0
    ("the quick brown ", "the quick", ""),
    ("the quick brown ", "The Quick", ""),            # case and spacing rules stay the module's
    ("Hello", "Hel", ""),                             # under MIN_OVERLAP too
    ("Hello", "Hello", ""),                           # equal: unchanged
    ("the quick brown ", "fox jumps", "fox jumps"),   # new text still goes out
    ("the quick brown ", "the fox", "the fox"),       # starts the same, then is new text
]


def _run(seen: str, fresh: str, cut: bool = True) -> "tuple[str, str]":
    stitcher = stitch.Stitcher(seen)
    out = stitcher.feed(fresh) + stitcher.flush(cut=cut)
    return out, stitcher.mode


@pytest.mark.parametrize("seen,fresh,expected", TABLE)
def test_restart_cut_table(seen, fresh, expected):
    assert _run(seen, fresh)[0] == expected          # the continuation was cut: R2 reads it as a restart


# Round 2 review F7: R2 never swallows a true short continuation. The continuation ENDED normally
# (flush without a cut) and is shorter than MIN_OVERLAP: it is text, even when it equals the start of
# what was shown ("Yes. Studies ..." then " yes").
SHORT_ENDED = [
    ("Yes. Studies show X, so the short answer:", " yes", " yes"),
    ("the quick brown ", "the", "the"),
    ("Hello", "Hel", "Hel"),
]


@pytest.mark.parametrize("seen,fresh,expected", SHORT_ENDED)
def test_a_short_continuation_that_ended_normally_is_shown(seen, fresh, expected):
    assert _run(seen, fresh, cut=False)[0] == expected


def test_the_same_short_continuation_ended_by_a_cut_is_still_swallowed():
    seen = "Yes. Studies show X, so the short answer:"
    assert _run(seen, " yes", cut=True)[0] == ""


def test_a_long_fresh_text_that_repeats_the_start_is_a_restart_even_without_a_cut():
    # at least MIN_OVERLAP comparable characters: the model began the answer again
    assert _run("the quick brown ", "the quick", cut=False) == ("", "restart")


def test_a_cut_restart_is_named_restart():
    assert _run("the quick brown ", "the ")[1] == "restart"
    assert _run("the quick brown ", "fox jumps")[1] == "new"


def test_the_cut_restart_across_several_deltas():
    stitcher = stitch.Stitcher("the quick brown ")
    shown = "".join(stitcher.feed(chunk) for chunk in ["th", "e qu", "ick"]) + stitcher.flush(cut=True)
    assert shown == ""


def test_a_long_restart_is_unchanged():
    seen = "The quick brown fox jumps over the lazy dog while the cat sleeps"
    out, mode = _run(seen, seen + " and then it rains")
    assert mode == "continuation"
    assert out == " and then it rains"


# --- F7 where the transport calls flush: an error or a cut says so, a normal end does not -----------------

from test_transport_1819 import (  # noqa: E402,F401  (clock is a fixture used by name)
    FakeCall, KEYS, Script, _transport, clock, finish, shown, text,
)

CUT = ConnectionError("peer closed connection without sending complete message body")


def test_a_second_cut_a_word_into_the_restart_shows_nothing_twice_when_the_error_path_says_cut(clock):
    script = Script({
        KEYS[0]: [[text("the quick brown "), CUT]],
        KEYS[1]: [[text("the "), CUT]],                          # began again, cut a word in (error path)
        KEYS[2]: [[text("fox"), finish()]],
    })
    out = list(_transport(clock).stream(FakeCall(script)))
    assert shown(out) == "the quick brown fox"


def test_a_stream_that_ends_without_a_finish_after_a_word_of_restart_shows_nothing_twice(clock):
    script = Script({
        KEYS[0]: [[text("the quick brown "), CUT]],
        KEYS[1]: [[text("the ")]],                               # closed with no finish: the same cut
        KEYS[2]: [[text("fox"), finish()]],
    })
    out = list(_transport(clock).stream(FakeCall(script)))
    assert shown(out) == "the quick brown fox"


def test_a_short_continuation_that_ends_normally_is_shown(clock):
    seen = "Yes. Studies show X, so the short answer:"
    script = Script({
        KEYS[0]: [[text(seen), CUT]],
        KEYS[1]: [[text(" yes", "stop")]],
    })
    out = list(_transport(clock).stream(FakeCall(script)))
    assert shown(out) == seen + " yes"
