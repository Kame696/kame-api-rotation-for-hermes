"""1.9.0.3 (C10) - "Never" keeps background jobs on the chat's model too.

Evidence: "Never" in force 13:06-17:28 on 17/11; the title job went to gemini-3.8-flash at 16:54:05 because the
host's own 30 s timeout on ``auto`` made it try its fallback entry - a hop that never passes the classification
hook. Built on the harness of the 1.8.2.9 aux-budget test.
"""
from __future__ import annotations

import pytest

from tests.test_1_8_2_9_aux_budget import (  # noqa: F401  (fresh is a fixture)
    CONFIG, IDENTITY, Plan, FakeCall, answer, chain_mod, fresh, settings, title, transport)

MAIN = "gemini:gemini-3.8-flash"
OTHER = "gemini:gemini-3.7-flash"

AUTO = CONFIG + "auxiliary:\n  title_generation:\n    provider: auto\n    model: ''\n"
SET_TO_OTHER = CONFIG + "auxiliary:\n  title_generation:\n    provider: gemini\n    model: gemini-3.7-flash\n"


def _set(fresh, monkeypatch, text, never=True):
    (__import__("pathlib").Path(__import__("os").environ["HERMES_HOME"]) / "config.yaml").write_text(text, encoding="utf-8")
    chain_mod.forget()
    if never:
        monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    settings.forget()


def _title_on(plan, identity):
    call = title(plan, identity=identity)
    return call


def _first_went_unanswered(fresh, identity=MAIN):
    """Hermes' first request of the title job went to ``identity`` and did not answer (its 30 s timeout)."""
    fresh._aux_recent["title_generation"] = (identity.lower(), fresh.clock.now)


def test_never_and_task_auto_and_a_hop_to_another_model_is_stopped_and_nothing_is_sent(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO)
    _first_went_unanswered(fresh)
    plan = Plan(["a title"], fresh.clock)
    with pytest.raises(transport.KameAuxStopped) as stop:
        fresh.complete(_title_on(plan, OTHER))
    assert plan.asked == []
    text = str(stop.value)
    assert text == ('kame: background job title_generation not sent to gemini-3.7-flash - '
                    '"Never" keeps background jobs on your model')
    assert fresh._aux_stopped.get("title_generation") == 1, "counted like the other stops"


def test_the_measured_sequence_first_fails_then_the_hop_is_not_sent(fresh, monkeypatch):
    # 17/11 16:53-16:54: the title on the chat's model failed, Hermes tried its fallback entry next.
    from tests.test_1_8_2_9_aux_budget import busy
    _set(fresh, monkeypatch, AUTO)
    first = Plan([busy(), busy(), busy()], fresh.clock)
    with pytest.raises(Exception):
        fresh.complete(_title_on(first, MAIN))
    assert first.asked, "the first request of the job was sent"
    hop = Plan(["a title"], fresh.clock)
    with pytest.raises(transport.KameAuxStopped):
        fresh.complete(_title_on(hop, OTHER))
    assert hop.asked == []


def test_the_first_request_of_a_job_is_never_stopped_whatever_its_model(fresh, monkeypatch):
    # design review 2026-11-18: Hermes may send a title first to the conversation's own model or to the
    # provider's fast model; only its fallback HOP is a switch.
    _set(fresh, monkeypatch, AUTO)
    plan = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(plan, OTHER)).choices[0].message.content == "a title"
    assert len(plan.asked) == 1


def test_a_hop_after_an_answer_is_a_new_job_and_is_sent(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO)
    first = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(first, MAIN)).choices[0].message.content == "a title"
    plan = Plan(["another title"], fresh.clock)
    assert fresh.complete(_title_on(plan, OTHER)).choices[0].message.content == "another title"


def test_a_hop_long_after_the_first_request_is_sent(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO)
    _first_went_unanswered(fresh)
    fresh.clock.now += transport._AUX_HOP_WINDOW_S + 1.0
    plan = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(plan, OTHER)).choices[0].message.content == "a title"


def test_the_message_holds_none_of_the_hosts_words(fresh, monkeypatch):
    from tests.fixtures import host_aux_predicates_20261015 as host
    _set(fresh, monkeypatch, AUTO)
    _first_went_unanswered(fresh)
    with pytest.raises(transport.KameAuxStopped) as stop:
        fresh.complete(_title_on(Plan(["x"], fresh.clock), OTHER))
    low = str(stop.value).lower()
    for word in host.PAYMENT_KEYWORDS + host.RATE_LIMIT_KEYWORDS:
        assert word not in low, word
    assert not host.is_timeout_error(stop.value)


def test_never_and_the_same_model_again_is_sent(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO)
    _first_went_unanswered(fresh)
    plan = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(plan, MAIN)).choices[0].message.content == "a title"
    assert len(plan.asked) == 1


def test_a_task_configured_to_that_model_is_never_touched(fresh, monkeypatch):
    _set(fresh, monkeypatch, SET_TO_OTHER)
    _first_went_unanswered(fresh)
    plan = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(plan, OTHER)).choices[0].message.content == "a title"
    assert len(plan.asked) == 1


def test_auto_mode_sends_the_hop(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO, never=False)
    _first_went_unanswered(fresh)
    plan = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(plan, OTHER)).choices[0].message.content == "a title"


def test_the_no_fallback_switch_counts_as_never(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO, never=False)
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    _first_went_unanswered(fresh)
    plan = Plan(["a title"], fresh.clock)
    with pytest.raises(transport.KameAuxStopped):
        fresh.complete(_title_on(plan, OTHER))
    assert plan.asked == []


def test_an_unreadable_config_fails_open(fresh, monkeypatch):
    _set(fresh, monkeypatch, "model: [unclosed\n  : bad")
    _first_went_unanswered(fresh)
    plan = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(plan, OTHER)).choices[0].message.content == "a title"


def test_no_config_at_all_fails_open(fresh, monkeypatch):
    import os
    _set(fresh, monkeypatch, AUTO)
    (__import__("pathlib").Path(os.environ["HERMES_HOME"]) / "config.yaml").unlink()
    chain_mod.forget()
    _first_went_unanswered(fresh)
    plan = Plan(["a title"], fresh.clock)
    assert fresh.complete(_title_on(plan, OTHER)).choices[0].message.content == "a title"


def test_the_chat_is_never_touched(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO)
    fresh._aux_recent["chat"] = (MAIN, fresh.clock.now)
    plan = Plan(["hello"], fresh.clock)
    call = FakeCall(plan, channel="main", task="chat", identity=OTHER)
    assert fresh.complete(call).choices[0].message.content == "hello"


def test_the_task_model_reader(fresh, monkeypatch):
    _set(fresh, monkeypatch, AUTO, never=False)
    assert chain_mod.auxiliary_model("title_generation") == ""          # auto: not configured
    assert chain_mod.auxiliary_model("compression") == ""               # not in the file: not configured
    _set(fresh, monkeypatch, SET_TO_OTHER, never=False)
    assert chain_mod.auxiliary_model("title_generation") == "gemini-3.7-flash"
    _set(fresh, monkeypatch, CONFIG + "auxiliary:\n  title_generation:\n    provider: nvidia\n    model: ''\n",
         never=False)
    assert chain_mod.auxiliary_model("title_generation") == "nvidia"   # a provider written is a choice too
    _set(fresh, monkeypatch, "model: [unclosed : bad", never=False)
    assert chain_mod.auxiliary_model("title_generation") is None        # unreadable: the caller fails open
