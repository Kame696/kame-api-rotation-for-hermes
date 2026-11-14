"""1.9.0.1 (H5) - the audit matrix: what a reply does for every kind of user, as a permanent test.

Every scenario of PLAN_1.9.0.1 section 0, with its expected outcome and a time bound, plus the "Never switch"
variants. The users differ in the shape of the owner's list (one provider, a mixed list, no list), the number of
keys (one, four) and the way the provider fails (silent from the start, thinks and stalls, 503, text and then
silence). This file is the regression net for "every user": a rule that serves the owner's 14-key pool and
leaves a one-key user waiting fails here.

Real time and real threads. Quiet bound 0.2 s (``guard``), answer quiet bound 0.5 s, test cap per scenario.
Outcomes: ``answers <text>`` (the reply ends with that text), ``hands over`` (KamePoolResting: Hermes' next
entry answers), ``waits`` (still running at the cap: nothing else exists to ask).
"""
from __future__ import annotations

import pytest

from tests.h1901 import (  # noqa: F401
    G, G2, KEYS, MIXED, MIXED_SAME_FIRST, NO_LIST, SAME_ONLY, SeqCall, answer, busy, chain_mod, chunk, guard,
    opened_models, reasons, run, settings, setup, silent, stall, think, transport, visible_then_silent)


def resumed():
    return [0.05, chunk("resumed "), chunk("done", finish="stop")]


ONE = KEYS[:1]

# name, chain, keys, plans by model, never-switch, outcome, bound (seconds), expected requests (or None)
ROWS = [
    # -- thinks, then stalls ------------------------------------------------------------------------------------
    ("1  stall | [G, groq] | 4 keys", MIXED, KEYS, {G: [stall()] * 4}, False, "hands over", 1.6, 2),
    ("2  stall | [G, G2, groq] | 4 keys", MIXED_SAME_FIRST, KEYS, {G: [stall()] * 2, G2: [stall()] * 2}, False,
     "hands over", 2.0, 3),
    ("3  stall | no list | 1 key", NO_LIST, ONE, {G: [stall(), answer("second ask")]}, False,
     "answers second ask done", 1.5, 2),
    ("4  stall | no list | 4 keys", NO_LIST, KEYS, {G: [stall(), stall(), answer("third")]}, False,
     "answers third done", 2.0, 3),
    # (1.9.0.0 asked G2 at once; the same key is now rescue 1 and G2 rescue 2 - the same key may answer first)
    ("5  stall | [G, G2] | 1 key", SAME_ONLY, ONE, {G: [stall(), stall()], G2: [answer("g2")]}, False,
     "answers g2 done", 1.5, 3),
    ("6a stall | [G, groq] | 1 key, the key answers again", MIXED, ONE, {G: [stall(), answer("again")]}, False,
     "answers again done", 1.5, 2),
    ("6b stall | [G, groq] | 1 key, it stalls again", MIXED, ONE, {G: [stall(), stall()]}, False,
     "hands over", 1.6, 2),
    # -- silent from the start (kept: the slow bound is patched to 0.8 s to keep the test short) ---------------------
    ("7  silent | [G, groq] | 4 keys", MIXED, KEYS, {G: [silent()] * 6}, False, "hands over", 1.6, None),
    ("8  silent | [G, groq] | 1 key", MIXED, ONE, {G: [silent()] * 3}, False, "hands over", 1.6, None),
    ("9  silent | no list | 1 key", NO_LIST, ONE, {G: [silent(), answer("x")]}, False, "waits", 1.5, 1),
    # -- 503 ------------------------------------------------------------------------------------------------------
    ("10 503 | [G, groq] | 1 key", MIXED, ONE, {G: [busy()] * 3 + [answer("late")]}, False, "hands over", 1.0, 1),
    ("11 503 | [G, groq] | 4 keys", MIXED, KEYS, {G: [busy()] * 4 + [answer("late")]}, False, "hands over", 1.0, 2),
    ("12 503 then silent | [G, G2] | 4 keys", SAME_ONLY, KEYS, {G: [busy(), silent()], G2: [answer("g2")]}, False,
     "answers g2 done", 1.0, 3),
    # -- text, then silence ---------------------------------------------------------------------------------------
    ("14a text then silence | [G, G2] | 4 keys", SAME_ONLY, KEYS, {G: [visible_then_silent(), resumed()]}, False,
     "answers visible resumed done", 2.5, 2),
    ("14b text then silence | no list | 1 key", NO_LIST, ONE, {G: [visible_then_silent(), resumed()]}, False,
     "answers visible resumed done", 3.0, 2),
    # -- "Never switch" variants -----------------------------------------------------------------------------------
    ("N1 never: stall | [G, groq] | 4 keys", MIXED, KEYS, {G: [stall()] * 4}, True, "waits", 2.0, 3),
    ("N2 never: stall | [G, G2] | 4 keys", SAME_ONLY, KEYS,
     {G: [stall(), stall(), answer("third key")], G2: [answer("g2")]}, True, "answers third key done", 2.0, 3),
    ("N3 never: stall | no list | 1 key", NO_LIST, ONE, {G: [stall(), stall(), answer("third ask")]}, True,
     "answers third ask done", 2.0, 3),
    ("N4 never: 503 | [G, groq] | 1 key", MIXED, ONE, {G: [busy(), answer("retried")]}, True,
     "answers retried done", 4.0, 2),
    ("N5 never: 503 | [G, groq] | 4 keys", MIXED, KEYS, {G: [busy(), busy(), answer("rotated")]}, True,
     "answers rotated done", 2.0, 3),
    ("N6 never: text then silence | [G, G2] | 4 keys", SAME_ONLY, KEYS, {G: [visible_then_silent(), resumed()]},
     True, "answers visible resumed done", 2.5, 2),
]


@pytest.fixture(autouse=True)
def _bounds(guard, monkeypatch):
    monkeypatch.setenv("KAME_ANSWER_QUIET_S", "0.5")
    settings.forget()


@pytest.mark.parametrize("name,chain,keys,plans,never,outcome,bound,requests", ROWS, ids=[r[0] for r in ROWS])
def test_every_user_every_provider(guard, tmp_path, monkeypatch, name, chain, keys, plans, never, outcome, bound,
                                   requests):
    setup(tmp_path, chain)
    if never:
        monkeypatch.setattr(settings, "model_switch_mode", lambda: "never")
    if name.startswith(("7 ", "8 ")):
        monkeypatch.setattr(transport.KameTransport, "_slow_after", lambda self, call, identity, reasoning: 0.8)
    call = SeqCall(by_model={m: list(p) for m, p in plans.items()}, keys=keys)
    cap = bound + 1.5 if outcome == "waits" else max(bound + 4.0, 6.0)
    res = run(guard, call, cap=bound if outcome == "waits" else cap)
    where = f"{name}: {res.took:.2f}s, shown {res.shown!r}, err {type(res.err).__name__ if res.err else None}, " \
            f"opened {len(call.opened)}"
    if outcome == "hands over":
        assert isinstance(res.err, transport.KamePoolResting), where
        assert res.took < bound, where
    elif outcome == "waits":
        assert res.cut and res.err is None, where
    else:
        text = outcome[len("answers "):]
        assert res.err is None and not res.cut, where
        assert res.shown == text, where
        assert res.took < bound, where
    if requests is not None:
        assert len(call.opened) == requests, where
