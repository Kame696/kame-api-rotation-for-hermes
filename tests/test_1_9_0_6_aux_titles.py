"""1.9.0.6 (decisions/0037 C18) - a chat title is made during a storm.

A cosmetic job whose model answers busy, is out of quota, rests or is slow is answered by a free sibling
inside its own budget; with none free it stops as before; a compression job never switches.
"""
from __future__ import annotations

import pytest

from tests import test_1_8_2_9_aux_budget as B
from tests.test_1_8_2_9_aux_budget import Plan, fresh, title, compression  # noqa: F401  (fixture by name)
from tests.test_1_8_4_3_aux_move import BEST, SECOND, THIRD, models, sicken, three  # noqa: F401

transport = B.transport


def test_busy_answers_on_the_home_model_move_the_title_to_a_free_sibling(three):
    plan = Plan([B.busy(), B.busy(), "A title"], three.clock)
    out = three.complete(title(plan))
    assert out.choices[0].message.content == "A title"
    assert models(plan) == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.7-flash"]
    assert len(plan.asked) <= 3, "inside the 3-request budget"


def test_a_dry_key_moves_the_title_too(three):
    dry = B.Refusal(429, "Rate limit exceeded. Please retry after 20 seconds.")
    plan = Plan([dry, "A title"], three.clock)
    assert three.complete(title(plan)).choices[0].message.content == "A title"
    assert models(plan)[-1] != "gemini-3.8-flash"


def test_every_key_resting_at_the_start_moves_the_title(three):
    for key in B.KEYS:
        three.engine.mark(BEST, key, False, 3600.0, "daily")
    plan = Plan(["A title"], three.clock)
    assert three.complete(title(plan)).choices[0].message.content == "A title"
    assert models(plan) == ["gemini-3.7-flash"]


def test_no_free_sibling_stops_as_today(three):
    sicken(SECOND, ago=60.0)
    sicken(THIRD, ago=60.0)
    plan = Plan([B.busy(), B.busy(), "never"], three.clock)
    with pytest.raises(transport.KameAuxTimeout):
        three.complete(title(plan))
    assert set(models(plan)) == {"gemini-3.8-flash"}
    with pytest.raises(transport.KameAuxStopped):          # the later lane is still refused
        three.clock.sleep(1.0)
        three.complete(title(plan))


def test_a_compression_job_never_switches(three):
    plan = Plan([B.busy(), B.busy(), "never"], three.clock)
    with pytest.raises(transport.KameAuxTimeout):
        three.complete(compression(plan))
    assert set(models(plan)) == {"gemini-3.8-flash"}
