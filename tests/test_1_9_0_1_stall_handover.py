"""1.9.0.1 (H1, R1) - an exhausted stall moves to a later provider.

1.9.0.0 raced a reply that stopped mid-thought with a rescue on another key and, at twice the bound, one more on
the next model of the list - and then WAITED for the provider's own timeout, even when a later entry of the
owner's list (another provider) was free. Now, once both rescues have had their turn (each could not open,
ended, is past its own late point with no sign of work, or worked and is itself quiet), the reply is handed over
to that entry, exactly like the slow-bound hand-over of a silent reply. With no such entry it keeps waiting.

Real time, real threads, the quiet bound is 0.2 s (``guard`` of ``test_1_8_4_3_quiet_guard``): rescue 1 opens
at ~0.2 s, rescue 2 at ~0.4-0.6 s, and a rescue that shows nothing is late 0.4 s after its own start.
"""
from __future__ import annotations

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.h1901 import (  # noqa: F401
    G, G2, GROQ, KEYS, MAIN_ONLY_LATER, MIXED, MIXED_SAME_FIRST, NO_LIST, SAME_ONLY, SeqCall, answer, busy, chain_mod,
    chunk, guard, opened_models, reasons, run, settings, setup, silent, stall, think, transport)

HOME = "gemini:gemini-race"
HOME2 = "gemini:gemini-race-two"
HAND_BOUND = 1.6        # rescue points (0.2 + 0.4 + 0.4 late) + 0.5 s


def hands_to_groq():
    return [r for r in reasons() if "-> Hermes answers with groq:llama-x" in r]


def hand_overs():
    """Any stall hand-over event. With a sibling first in the list the event names it (``later[0]``): Hermes walks
    its list from there, and a model that was silent in this reply is skipped at the call start (noted slow)."""
    return [r for r in reasons() if "stopped mid-thought" in r and "-> Hermes answers with" in r]


# scenario 1 -------------------------------------------------------------------------------------------------------

def test_stall_with_another_provider_later_and_four_keys_hands_over(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [stall(), stall(), stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert isinstance(res.err, transport.KamePoolResting), (res.cut, res.shown, res.err)
    assert res.took < HAND_BOUND, res.took                  # 1.9.0.0: waited for the provider's timeout (15 s+)
    assert len(opened_models(call)) == 2, "one rescue on another key; nothing else was asked"
    assert all(s.response.closed.is_set() for _, _, s in call.opened), "every leg was cut"
    assert hands_to_groq(), reasons()


def test_the_hand_over_event_says_the_stall_in_words(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [stall(), stall()]}, keys=KEYS)
    run(guard, call, cap=6.0)
    words = [r for r in hands_to_groq() if "stopped mid-thought" in r]
    assert words, reasons()
    assert "nothing beside it answered" in words[0], words[0]
    assert "gemini-race" in words[0]


# scenario 2 -------------------------------------------------------------------------------------------------------

def test_stall_with_a_sibling_then_another_provider_asks_the_sibling_then_hands_over(guard, tmp_path):
    setup(tmp_path, MIXED_SAME_FIRST)
    call = SeqCall(by_model={G: [stall(), stall()], G2: [stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert isinstance(res.err, transport.KamePoolResting), (res.cut, res.shown, res.err)
    assert res.took < HAND_BOUND + 0.4, res.took
    assert G2 in opened_models(call), "rescue 2 went to the next model of the list first"
    assert hand_overs(), reasons()


# scenario 6 -------------------------------------------------------------------------------------------------------

def test_stall_with_one_key_and_another_provider_asks_the_same_key_then_hands_over(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [stall(), stall()]}, keys=KEYS[:1])
    res = run(guard, call, cap=6.0)
    assert isinstance(res.err, transport.KamePoolResting), (res.cut, res.shown, res.err)
    assert res.took < HAND_BOUND, res.took
    assert [m for m in opened_models(call)] == [G, G], "rescue 1 = the same key again"
    assert hands_to_groq(), reasons()


def test_stall_with_one_key_and_another_provider_the_same_key_may_answer_first(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [stall(), answer("again")]}, keys=KEYS[:1])
    res = run(guard, call, cap=6.0)
    assert res.err is None and res.shown == "again done", (res.err, res.shown)
    assert res.took < 1.5, res.took
    assert not hands_to_groq()


# "Never switch" --------------------------------------------------------------------------------------------------

def test_never_switch_never_hands_over_it_waits(guard, tmp_path, monkeypatch):
    setup(tmp_path, MIXED)
    monkeypatch.setattr(settings, "model_switch_mode", lambda: "never")
    call = SeqCall(by_model={G: [stall(), stall(), stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=2.5)
    assert res.cut and res.err is None, (res.shown, res.err)        # still waiting when the test ended it
    assert not hands_to_groq()


def test_ignore_my_fallback_list_never_hands_over(guard, tmp_path, monkeypatch):
    setup(tmp_path, MIXED)
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    call = SeqCall(by_model={G: [stall(), stall(), stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=2.5)
    assert res.cut and res.err is None, (res.shown, res.err)
    assert not hands_to_groq()


# a rescue that is working is never cut ---------------------------------------------------------------------------

def test_a_rescue_that_thinks_steadily_is_never_cut(guard, tmp_path):
    setup(tmp_path, MIXED)
    steady = [0.02, think("r")] + [0.1, think("r")] * 14 + [chunk("answered "), chunk("done", finish="stop")]
    call = SeqCall(by_model={G: [stall(), steady]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert res.err is None, res.err
    assert res.shown == "answered done"
    assert not hands_to_groq()


def test_a_rescue_that_answers_just_before_its_late_point_wins(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [stall(), answer("in time", delay=0.2)]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert res.err is None and res.shown == "in time done", (res.err, res.shown)
    assert not hands_to_groq()


# nowhere to go ----------------------------------------------------------------------------------------------------

def test_only_the_main_model_later_is_no_hand_over(guard, tmp_path):
    """Hermes is on its last fallback entry; the only model after it is the main one, which it cannot be handed."""
    setup(tmp_path, MAIN_ONLY_LATER)
    call = SeqCall(by_model={G: [stall(), stall(), answer("third")]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert not isinstance(res.err, transport.KamePoolResting), res.err
    assert res.shown == "third done"                            # rescue 2 = a third key (H2), not a hand-over


def test_no_list_never_hands_over(guard, tmp_path):
    setup(tmp_path, NO_LIST)
    call = SeqCall(by_model={G: [stall(), stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=2.5)
    assert res.cut and res.err is None
    assert len(call.opened) == 3, "the primary and two rescues, no more"


# the provisional leg is itself a cross leg -----------------------------------------------------------------------

def test_a_cross_leg_that_stalls_hands_over_without_asking_its_own_pool_as_the_exit(guard, tmp_path):
    """Key 1 answers 503, the retry and the next model race at once; the next model thinks and stalls."""
    setup(tmp_path, MIXED_SAME_FIRST)
    call = SeqCall(by_model={G: [busy(), silent(), silent(), silent()], G2: [stall(), stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert isinstance(res.err, transport.KamePoolResting), (res.cut, res.shown, res.err)
    assert res.took < HAND_BOUND + 0.6, res.took
    assert hand_overs(), reasons()


# the models that did not answer are left alone for a while -------------------------------------------------------

def test_the_home_model_is_noted_slow_and_so_is_a_never_working_rescue_pool(guard, tmp_path):
    setup(tmp_path, MIXED_SAME_FIRST)
    call = SeqCall(by_model={G: [stall(), stall()], G2: [silent(), silent()]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert isinstance(res.err, transport.KamePoolResting), res.err
    assert guard._busy_until.get(HOME, 0.0) > 0.0, "the home model was silent mid-thought: noted slow"
    # the sibling's rescue leg never showed a sign of work: its model is left alone too
    assert guard._busy_until.get(HOME2, 0.0) > 0.0
    assert guard._busy_kind.get(HOME) == "slow"


def test_a_stall_hand_over_is_not_a_stand_in(guard, tmp_path):
    """The exception goes to Hermes (KamePoolResting); KAME does not answer with a sibling itself here."""
    setup(tmp_path, MIXED_SAME_FIRST)
    call = SeqCall(by_model={G: [stall(), stall()], G2: [stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert isinstance(res.err, transport.KamePoolResting)
    assert not any("answered first" in r for r in reasons())
