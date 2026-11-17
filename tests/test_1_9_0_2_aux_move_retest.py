"""1.9.0.2 (H9) - a chat title is moved to a model that will take it, never to one it then refuses.

Live 2026-11-15 15:36 and 15:55 (installed 1.9.0.2, events tab "Secondary"): the title job on
gemini-3.8-flash (failing 10/10) was "moved to gemini-3.7-flash" and stopped there at once - "its model keeps
failing", 0 requests - while gemini-3.6-flash answered the chat in 4-6 s. The picker (``_aux_sibling``) asks
``_entry_risky`` with the 1.8.4.3 (L12) retest allowance: a failing model whose retest is due counts as
healthy, so a choice may look at it again. The cosmetic refusal that runs right after (``_aux_refusal``) has no
such allowance. A model the picker accepts was then refused, the job raised the timeout-shaped first stop, and
Hermes held Gemini out of every background job for ten minutes ("skipping gemini (request timed out, retry in
599s)"): 141 title failures, no title, in the two latest logs.

Pinned: the picker only takes a model the cosmetic refusal would let start. A failing model due a retest is
not a title's job - a title never spins on a failing model (decisions/0032 A) - so the job goes on to the next
healthy one; with none, today's refusal stands.
"""
from __future__ import annotations

import pytest

from tests import test_1_8_4_3_aux_move as M
from tests.test_1_8_2_9_aux_budget import Plan, fresh, title  # noqa: F401  (fixture by name)
from tests.test_1_8_4_3_aux_move import BEST, SECOND, THIRD, models, sicken, three  # noqa: F401

transport = M.transport
events_mod = M.events_mod


def test_a_title_skips_a_failing_model_whose_retest_is_due_and_goes_to_the_healthy_one(three):
    sicken(BEST)
    sicken(SECOND, ago=3600.0)          # failing, and its retest is due (L12)
    assert transport.HEALTH.retest_due(SECOND + ":global") or transport.HEALTH.retest_due(SECOND)
    plan = Plan(["A title"], three.clock)
    out = three.complete(title(plan))
    assert out.choices[0].message.content == "A title"
    assert models(plan) == ["gemini-3.6-flash"], "one request, on the model that answers"


def test_the_picker_never_names_a_model_the_refusal_then_stops(three):
    sicken(BEST)
    sicken(SECOND, ago=3600.0)
    plan = Plan(["A title"], three.clock)
    three.complete(title(plan))
    rows = [r["reason"] for r in events_mod.SECONDARY_EVENTS.recent()]
    assert not any("gemini-3.7-flash" in r and "moved to" in r for r in rows), rows
    assert not any("stopped" in r for r in rows), rows


def test_with_every_sibling_failing_the_refusal_is_unchanged(three):
    sicken(BEST)
    sicken(SECOND, ago=3600.0)
    sicken(THIRD, ago=3600.0)
    plan = Plan(["never"], three.clock)
    with pytest.raises((transport.KameAuxTimeout, transport.KameAuxStopped)):
        three.complete(title(plan))
    assert plan.asked == [], "no request at all"
