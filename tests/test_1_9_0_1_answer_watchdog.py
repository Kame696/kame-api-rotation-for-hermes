"""1.9.0.1 (H4, R4) - a visible answer that goes silent is cut and continued.

Live, the read timeout never fires when the provider keeps sending keep-alive comments: the owner's history has a
reply silent 300 s after its first text and one 600 s. 1.9.0.0 waited for the stream's own end. Now, after the
first visible chunk, a reply with no working chunk (text, tool-call delta, reasoning) for ``answer_quiet_s``
(default 45 s; ``KAME_ANSWER_QUIET_S``; 0 = off) is cut by KAME and goes down the existing mid-answer cut path:
the answer is continued on another key (the same key when it is the only one), a tool call in flight is asked
again whole. Real time, real threads; the bound here is 0.5 s.
"""
from __future__ import annotations

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.h1901 import (  # noqa: F401
    G, G2, KEYS, NO_LIST, SAME_ONLY, SeqCall, answer, chain_mod, chunk, guard, opened_models, reasons, run, settings,
    setup, think, transport, visible_then_silent)


@pytest.fixture(autouse=True)
def _answer_bound(guard, monkeypatch):   # after ``guard``, which clears every KAME_ variable
    monkeypatch.setenv("KAME_ANSWER_QUIET_S", "0.5")
    settings.forget()
    yield
    settings.forget()


def resume():
    return [0.05, chunk("resumed "), chunk("done", finish="stop")]


# the setting -----------------------------------------------------------------------------------------------------

def test_the_setting_exists_with_its_default_range_and_words(monkeypatch):
    monkeypatch.delenv("KAME_ANSWER_QUIET_S", raising=False)
    settings.forget()
    name = settings.ANSWER_QUIET_S
    assert name == "answer_quiet_s" and settings.ANSWER_QUIET == name
    assert settings.ALL_NUMBERS[name] == 45.0
    assert settings.number(name, settings.ALL_NUMBERS[name]) == 45.0
    assert settings.env_name(name) == "KAME_ANSWER_QUIET_S"
    assert settings.UNITS[name] == "seconds"
    assert name in settings.META and name in settings.IMPACT
    monkeypatch.setenv("KAME_ANSWER_QUIET_S", "0")
    settings.forget()
    assert settings.number(name, -1.0) == 0.0
    monkeypatch.setenv("KAME_ANSWER_QUIET_S", "9999")
    settings.forget()
    assert settings.number(name, -1.0) <= 600.0         # clamped, never an hour


# scenario 14 -----------------------------------------------------------------------------------------------------

def test_text_then_silence_is_cut_and_continued_on_another_key(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)
    call = SeqCall(by_model={G: [visible_then_silent("visible "), resume()]}, keys=KEYS)
    res = run(guard, call, cap=8.0)
    assert res.err is None, res.err
    assert res.shown == "visible resumed done", res.shown        # nothing repeated, nothing lost
    assert res.took < 2.5, res.took                              # 1.9.0.0: the stream's own end (30 s here)
    assert len(call.opened) == 2
    assert len({k for _, k, _ in call.opened}) == 2, "another key continued it"
    assert call.opened[0][2].response.closed.is_set(), "the silent stream was cut"


def test_text_then_silence_with_one_key_is_continued_on_the_same_key(guard, tmp_path):
    setup(tmp_path, NO_LIST)
    call = SeqCall(by_model={G: [visible_then_silent("visible "), resume()]}, keys=KEYS[:1])
    res = run(guard, call, cap=8.0)
    assert res.err is None, res.err
    assert res.shown == "visible resumed done", res.shown
    assert res.took < 3.0, res.took
    assert [k for _, k, _ in call.opened] == [KEYS[0], KEYS[0]]


def test_the_cut_is_recorded_as_a_timeout_attempt(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)
    call = SeqCall(by_model={G: [visible_then_silent("visible "), resume()]}, keys=KEYS)
    run(guard, call, cap=8.0)
    cut = [r for r in guard.rows if r.get("kind") == "timeout"]
    assert cut, [(r.get("outcome"), r.get("kind")) for r in guard.rows]
    assert any(r.get("chars_seen") == len("visible ") for r in cut), cut


# a stream that is alive is never cut -------------------------------------------------------------------------------

def test_a_stream_that_keeps_sending_is_never_cut(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)
    steps = [0.05, chunk("a ")] + [0.3, chunk("b ")] * 6 + [chunk("end", finish="stop")]
    call = SeqCall(by_model={G: [steps]}, keys=KEYS)
    res = run(guard, call, cap=8.0)
    assert res.err is None
    assert res.shown == "a " + "b " * 6 + "end"
    assert len(call.opened) == 1


def test_reasoning_between_the_words_keeps_the_stream_alive(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)
    steps = [0.05, chunk("a ")] + [0.3, think("hmm")] * 5 + [0.1, chunk("end", finish="stop")]
    call = SeqCall(by_model={G: [steps]}, keys=KEYS)
    res = run(guard, call, cap=8.0)
    assert res.err is None and res.shown == "a end"
    assert len(call.opened) == 1


# a tool call in flight ----------------------------------------------------------------------------------------------

def test_a_tool_call_that_stalls_is_asked_again_whole_on_another_key(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)
    half = [0.05, chunk(tools=R.tool_call("search", '{"q": "par')), 30.0, chunk(tools=R.tool_call("", 'is"}'))]
    whole = [0.05, chunk(tools=R.tool_call("search", '{"q": "paris"}'), finish="tool_calls")]
    call = SeqCall(by_model={G: [half, whole]}, keys=KEYS)
    res = run(guard, call, cap=8.0)
    assert res.err is None, res.err
    tools = [c for c in res.out if getattr(c, "choices", None) and c.choices[0].delta.tool_calls]
    assert len(tools) == 1, "only the whole call reached Hermes, never the half one"
    assert tools[0].choices[0].delta.tool_calls[0].function.arguments == '{"q": "paris"}'
    assert res.took < 3.0, res.took


# off, local, tail -----------------------------------------------------------------------------------------------------

def test_zero_turns_it_off(guard, tmp_path, monkeypatch):
    setup(tmp_path, SAME_ONLY)
    monkeypatch.setenv("KAME_ANSWER_QUIET_S", "0")
    settings.forget()
    call = SeqCall(by_model={G: [visible_then_silent("visible "), resume()]}, keys=KEYS)
    res = run(guard, call, cap=2.0)
    assert res.cut, "still waiting for the stream when the test ended it"
    assert res.shown == "visible "
    assert len(call.opened) == 1


def test_a_local_endpoint_is_never_cut(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)

    class Local(SeqCall):
        base_url = "http://localhost:11434/v1"

    call = Local(by_model={G: [visible_then_silent("visible "), resume()]}, keys=KEYS)
    res = run(guard, call, cap=2.0)
    assert res.cut and res.shown == "visible "
    assert len(call.opened) == 1


def test_a_silence_before_the_first_visible_chunk_is_not_this_watch(guard, tmp_path):
    """Before the first visible chunk the first-word and quiet-while-thinking rules rule, not this one."""
    setup(tmp_path, SAME_ONLY)
    call = SeqCall(by_model={G: [[0.05, think("x"), 0.0, chunk("answer", finish="stop")]]}, keys=KEYS)
    res = run(guard, call, cap=4.0)
    assert res.err is None and res.shown == "answer"


def test_the_continuation_that_goes_silent_is_cut_too(guard, tmp_path):
    """The continuation is itself a stream that shows text; if it stalls it is continued again (resume limit)."""
    setup(tmp_path, SAME_ONLY)
    call = SeqCall(by_model={G: [visible_then_silent("one "), visible_then_silent("two "), resume()]}, keys=KEYS)
    res = run(guard, call, cap=10.0)
    assert res.err is None, res.err
    assert res.shown == "one two resumed done", res.shown
    assert res.took < 4.0, res.took
