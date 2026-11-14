"""1.9.0.1 (H2, R2) - a rescue is never impossible for lack of a second key or model.

1.9.0.0: a user with ONE key got no rescue at all when a reply stopped mid-thought (there was no "other key"),
and a user with no list (or "Never switch") got rescue 1 only. Now rescue 1 is another key of the same model, or
the SAME key again (a fresh request) when there is no other; and when rescue 2 cannot go to the next model of
the list and the stall hand-over (H1) has nowhere to go either, rescue 2 is another key of the same model (or the
same key). Never a subscription credential. At most two extra requests per reply.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.h1901 import (  # noqa: F401
    G, G2, GROQ, KEYS, MIXED, NO_LIST, SAME_ONLY, SeqCall, answer, chain_mod, chunk, guard, opened_models, reasons,
    run, settings, setup, stall, think, transport)


# scenario 3 -------------------------------------------------------------------------------------------------------

def test_one_key_no_list_the_same_key_is_asked_again(guard, tmp_path):
    setup(tmp_path, NO_LIST)
    call = SeqCall(by_model={G: [stall(), answer("second ask")]}, keys=KEYS[:1])
    res = run(guard, call, cap=6.0)
    assert res.err is None and res.shown == "second ask done", (res.err, res.shown)
    assert res.took < 1.5, res.took                      # 1.9.0.0: 30 s, the stalled stream's own end
    assert [k for _, k, _ in call.opened] == [KEYS[0], KEYS[0]]
    assert any("stopped mid-thought" in r for r in reasons())
    assert call.opened[0][2].response.closed.is_set(), "the quiet request was cut"


# scenario 4 -------------------------------------------------------------------------------------------------------

def test_four_keys_no_list_a_third_key_answers_when_rescue_one_is_silent_too(guard, tmp_path):
    setup(tmp_path, NO_LIST)
    call = SeqCall(by_model={G: [stall(), stall(), answer("third")]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert res.err is None and res.shown == "third done", (res.err, res.shown)
    assert res.took < 2.0, res.took
    keys = [k for _, k, _ in call.opened]
    assert len(keys) == 3 and len(set(keys)) == 3, keys   # three different keys
    assert getattr(call, "_kame_rescues_asked", None) == 2


# "Never switch" with a sibling -----------------------------------------------------------------------------------

def test_never_switch_with_a_sibling_rescue_two_is_another_key_never_the_sibling(guard, tmp_path, monkeypatch):
    setup(tmp_path, SAME_ONLY)
    monkeypatch.setattr(settings, "model_switch_mode", lambda: "never")
    call = SeqCall(by_model={G: [stall(), stall(), answer("third key")], G2: [answer("sibling")]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert res.err is None and res.shown == "third key done", (res.err, res.shown)
    assert G2 not in opened_models(call)
    assert len({k for _, k, _ in call.opened}) == 3


def test_never_switch_one_key_with_a_sibling_asks_the_same_key_twice_never_the_sibling(guard, tmp_path, monkeypatch):
    setup(tmp_path, SAME_ONLY)
    monkeypatch.setattr(settings, "model_switch_mode", lambda: "never")
    call = SeqCall(by_model={G: [stall(), stall(), answer("third ask")], G2: [answer("sibling")]}, keys=KEYS[:1])
    res = run(guard, call, cap=6.0)
    assert res.err is None and res.shown == "third ask done", (res.err, res.shown)
    assert opened_models(call) == [G, G, G]


# a subscription credential --------------------------------------------------------------------------------------

def test_one_key_that_is_a_subscription_credential_gets_no_same_key_rescue(guard, tmp_path):
    setup(tmp_path, NO_LIST)

    class Subs(SeqCall):
        def may_back_up(self, key):
            return False

    call = Subs(by_model={G: [[0.02, think("a"), 0.7, chunk("answer", finish="stop")], answer("rescue")]},
                keys=KEYS[:1])
    res = run(guard, call, cap=6.0)
    assert res.shown == "answer", (res.shown, res.err)    # the stream was left to finish on its own
    assert len(call.opened) == 1


# at most two, cost counted ---------------------------------------------------------------------------------------

def test_at_most_two_extra_requests_and_each_is_counted_as_a_rescue(guard, tmp_path):
    setup(tmp_path, NO_LIST)
    before = guard._speed_total[transport._SPEED_RESCUE] if len(guard._speed_total) > transport._SPEED_RESCUE else 0
    call = SeqCall(by_model={G: [stall(), stall(), stall(), stall(), stall()]}, keys=KEYS)
    res = run(guard, call, cap=2.5)
    assert res.cut
    assert len(call.opened) == 3, len(call.opened)         # the primary and two rescues
    assert guard._speed_total[transport._SPEED_RESCUE] - before == 2
    assert getattr(call, "_kame_rescues_asked", None) == 2


def test_the_event_for_a_same_key_rescue_does_not_claim_another_key(guard, tmp_path):
    setup(tmp_path, NO_LIST)
    call = SeqCall(by_model={G: [stall(), answer("x")]}, keys=KEYS[:1])
    run(guard, call, cap=4.0)
    said = [r for r in reasons() if "stopped mid-thought" in r]
    assert said and "same key" in said[0], said
    assert "another key" not in said[0]


# rescue 2 is still the next model of the list when the list can be used ---------------------------------------------

def test_rescue_two_is_still_the_next_model_of_the_list_when_it_may_be_asked(guard, tmp_path):
    setup(tmp_path, SAME_ONLY)
    call = SeqCall(by_model={G: [stall(), stall()], G2: [answer("g2")]}, keys=KEYS)
    res = run(guard, call, cap=6.0)
    assert res.shown == "g2 done" and res.err is None
    assert G2 in opened_models(call)


def test_a_subscription_credential_is_handed_over_not_asked_twice(guard, tmp_path):
    """A stalled reply on a subscription credential gets no second request on it (E3); a later entry of the
    list is still better than waiting, and that is no request on the subscription."""
    from tests.h1901 import MIXED  # noqa: F811

    setup(tmp_path, MIXED)

    class Subs(SeqCall):
        def may_back_up(self, key):
            return False

    call = Subs(by_model={G: [stall(), answer("never asked")]}, keys=KEYS[:1])
    res = run(guard, call, cap=6.0)
    assert isinstance(res.err, transport.KamePoolResting), (res.cut, res.shown, res.err)
    assert len(call.opened) == 1, "no request was made on the subscription beside the quiet one"
