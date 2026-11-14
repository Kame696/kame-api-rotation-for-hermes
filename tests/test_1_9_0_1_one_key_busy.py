"""1.9.0.1 (H3, R3) - the only key busy is the pool busy.

1.9.0.0 needed TWO busy keys before a 503 meant "the model is busy"; a user with one key (or one key per
provider) retried the same key every second and never reached the next provider. A busy refusal now counts as
"the model is busy" when it came from every key the pool has: ``len(busy_keys) >= min(2, len(keys))``.
"""
from __future__ import annotations

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.h1901 import (  # noqa: F401
    G, G2, GROQ, KEYS, MIXED, NO_LIST, SAME_ONLY, SeqCall, answer, busy, chain_mod, guard, reasons, run, settings,
    setup, silent, transport)


# -- the rule itself --------------------------------------------------------------------------------------------

def test_is_busy_counts_every_key_the_pool_has(guard):
    assert guard._is_busy("p:m", {"k1"}, ["k1"]) is True            # the only key
    assert guard._is_busy("p:m", {"k1"}, ["k1", "k2"]) is False     # one of two: not yet
    assert guard._is_busy("p:m", {"k1", "k2"}, ["k1", "k2"]) is True
    assert guard._is_busy("p:m", {"k1"}, ["k1", "k2", "k3", "k4"]) is False
    assert guard._is_busy("p:m", {"k1", "k2"}, ["k1", "k2", "k3", "k4"]) is True
    assert guard._is_busy("p:m", set(), ["k1"]) is False
    # without the keys the old reading stays: two
    assert guard._is_busy("p:m", {"k1"}) is False
    assert guard._is_busy("p:m", {"k1", "k2"}) is True


# -- scenario 10: 503 | [G, groq] | 1 key ------------------------------------------------------------------------

def test_one_key_503_hands_over_to_the_next_provider_at_once(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [busy(), busy(), busy(), answer("late")]}, keys=KEYS[:1])
    shown, err, took, _ = run(guard, call, cap=3.0)
    assert isinstance(err, transport.KamePoolResting), (shown, err)
    assert took < 1.0, took                                         # 1.9.0.0: 5.1 s of 1 s retries, then answered
    assert any("-> Hermes answers with groq:llama-x" in r for r in reasons()), reasons()
    assert len(call.opened) == 1, "the only key was asked once; nothing else was asked"


# -- one key + a same-provider sibling ------------------------------------------------------------------------------

def test_one_key_503_with_a_sibling_model_is_answered_by_the_sibling_fast(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)
    call = SeqCall(by_model={G: [busy(), silent()], G2: [answer("g2")]}, keys=KEYS[:1])
    shown, err, took, _ = run(guard, call, cap=3.0)
    assert err is None, err
    assert shown == "g2 done"
    assert took < 0.8, took
    assert set(m for m, _, _ in call.opened) == {G, G2}


# -- one key, no list ------------------------------------------------------------------------------------------------

def test_one_key_503_without_a_list_retries_the_key_as_before(guard, tmp_path):
    setup(tmp_path, NO_LIST)
    call = SeqCall(by_model={G: [busy(), answer("again")]}, keys=KEYS[:1])
    shown, err, took, _ = run(guard, call, cap=6.0)
    assert err is None, err
    assert shown == "again done"
    assert len(call.opened) == 2, "nothing to hand over to: the key is asked again"


# -- a 429 on the only key is NOT busy -----------------------------------------------------------------------------

def test_a_429_on_the_only_key_is_not_the_model_busy(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [[0.05, R.per_minute()], answer("later")]}, keys=KEYS[:1])
    shown, err, took, _ = run(guard, call, cap=2.0)
    # the key rests 30 s (a quota, not a busy model): if Hermes is handed the reply it is for that reason
    assert "busy" not in str(err), err
    assert not any("is busy" in r and "Hermes answers" in r for r in reasons()), reasons()
    assert not guard._busy_until.get("gemini:gemini-race"), "a quota is not a window of a busy model"


# -- two keys still need two -----------------------------------------------------------------------------------------

def test_two_keys_one_busy_is_not_yet_busy(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [busy(), answer("second key")]}, keys=KEYS[:2])
    shown, err, took, _ = run(guard, call, cap=4.0)
    assert err is None, err
    assert shown == "second key done"
    assert len(call.opened) == 2


def test_two_keys_both_busy_hands_over(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [busy(), busy(), answer("no")]}, keys=KEYS[:2])
    shown, err, took, _ = run(guard, call, cap=4.0)
    assert isinstance(err, transport.KamePoolResting), (shown, err)
    assert took < 1.5, took


def test_four_keys_two_busy_hands_over_and_one_busy_does_not(guard, tmp_path):
    setup(tmp_path, MIXED)
    call = SeqCall(by_model={G: [busy(), answer("fine")]}, keys=KEYS)
    shown, err, took, _ = run(guard, call, cap=4.0)
    assert err is None and shown == "fine done"
    # a fresh transport state for the second half
    guard._busy_until.clear()
    guard._busy_window.clear()
    call2 = SeqCall(by_model={G: [busy(), busy(), answer("no")]}, keys=KEYS)
    shown2, err2, took2, _ = run(guard, call2, cap=4.0)
    assert isinstance(err2, transport.KamePoolResting), (shown2, err2)
