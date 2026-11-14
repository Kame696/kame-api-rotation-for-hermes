"""1.8.4.3 (H4) - a cosmetic background job on a failing or busy model moves in place.

Live 2026-11-12: the chat title was asked of gemini-3.8-flash and gemini-3.7-flash, both remembered as
failing, and the host walked the list to a timeout. KAME now moves such a job ONCE to the first model of the
owner's list on the same provider that is not remembered as failing, not busy and has a ready key; with no such
model it refuses exactly as 1.8.4.2 did. A job that is not cosmetic is never moved, and "never switch" keeps
today's refusal.
"""
from __future__ import annotations

import time

import pytest

from tests import test_1_8_2_9_aux_budget as B
from tests.test_1_8_2_9_aux_budget import Plan, fresh, title, compression  # noqa: F401  (fixture by name)

transport = B.transport
settings = B.settings
chain_mod = B.chain_mod
events_mod = B.events_mod

BEST = "gemini:gemini-3.8-flash"
SECOND = "gemini:gemini-3.7-flash"
THIRD = "gemini:gemini-3.6-flash"
CONFIG3 = ("model:\n  provider: gemini\n  default: gemini-3.8-flash\n"
           "fallback_providers:\n  - provider: gemini\n    model: gemini-3.7-flash\n"
           "  - provider: gemini\n    model: gemini-3.6-flash\n")


@pytest.fixture()
def three(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG3, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    return fresh


def sicken(identity, failures=2, ago=3600.0):
    now = time.time()
    for n in range(failures):
        transport.HEALTH.note(identity, False, now=now - ago - (failures - n))


def models(plan):
    return [k.get("model") for k in plan.kwargs_seen]


def test_a_title_on_a_failing_model_moves_to_the_healthy_sibling(three):
    sicken(BEST)
    sicken(SECOND, ago=60.0)  # 1.8.4.3 (L12): failing NOW, not due a retest
    plan = Plan(["A title"], three.clock)
    out = three.complete(title(plan))
    assert out.choices[0].message.content == "A title"
    assert models(plan) == ["gemini-3.6-flash"], "exactly one request, on the healthy model"
    rows = [r["reason"] for r in events_mod.SECONDARY_EVENTS.recent()]
    assert any("moved to" in r and "gemini-3.6-flash" in r and "gemini-3.8-flash keeps failing" in r
               for r in rows), rows


def test_a_title_on_a_busy_model_moves_too(three):
    three._note_busy(BEST)
    plan = Plan(["A title"], three.clock)
    three.complete(title(plan))
    assert models(plan) == ["gemini-3.7-flash"]


def test_no_healthy_sibling_is_todays_refusal(three):
    sicken(BEST)
    sicken(SECOND, ago=60.0)  # 1.8.4.3 (L12): failing NOW, not due a retest
    sicken(THIRD, ago=60.0)  # 1.8.4.3 (L12): failing NOW, not due a retest
    plan = Plan(["never"], three.clock)
    with pytest.raises((transport.KameAuxTimeout, transport.KameAuxStopped)):
        three.complete(title(plan))
    assert plan.asked == [], "no request at all, as in 1.8.4.2"


def test_never_switch_keeps_todays_refusal(three, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()
    three._note_busy(BEST)
    plan = Plan(["never"], three.clock)
    with pytest.raises((transport.KameAuxTimeout, transport.KameAuxStopped)):
        three.complete(title(plan))
    assert plan.asked == []


def test_a_job_that_is_not_cosmetic_is_never_moved(three):
    sicken(BEST)
    plan = Plan(["summary"], three.clock)
    three.complete(compression(plan))
    assert models(plan) == ["gemini-3.8-flash"], "compression stays where Hermes put it"


def test_the_move_happens_once(three):
    sicken(BEST)
    sicken(SECOND, ago=60.0)  # 1.8.4.3 (L12): failing NOW, not due a retest
    sicken(THIRD, failures=0)
    plan = Plan([B.busy(), B.busy()], three.clock)
    with pytest.raises((transport.KameAuxTimeout, transport.KameAuxStopped)):
        three.complete(title(plan))
    assert len(plan.asked) <= 3
    assert set(models(plan)) == {"gemini-3.6-flash"}, "never walked on to another model in this call"
