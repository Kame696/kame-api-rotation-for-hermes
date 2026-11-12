"""1.8.4.2 (A1) - a stop on a lane of Hermes' own walk is not timeout-shaped.

A background job KAME stopped (title, summary...) is moved down the user's list by the host. Every later model
of that walk asks KAME again within the same minute, and answering each with the timeout-shaped error made the
host hold the whole provider out of every background task for ten minutes (live 2026-11-08 and 2026-11-11).
The first stop keeps its shape (the host moves the job once); a later stop of the same task in the same context
within a minute is a plain failure of that lane.

The host's predicates are the text replica in ``tests/fixtures`` (``tools/host_assumptions.py`` keeps it honest).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from tests import test_1_8_2_9_aux_budget as B
from tests.test_1_8_2_9_aux_budget import Plan, busy, fresh, title, compression  # noqa: F401  (fixture by name)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
import host_aux_predicates_20261015 as host  # noqa: E402

transport = B.transport
aux_budget = __import__(f"{B.PACKAGE}.core.aux_budget", fromlist=["x"])
SECOND_MODEL = "gemini:gemini-3.7-flash"


def stop_once(t, task_call, plan):
    with pytest.raises((transport.KameAuxTimeout, transport.KameAuxStopped)) as caught:
        t.complete(task_call(plan))
    return caught.value


def test_the_first_stop_is_a_timeout_the_host_moves_on_from(fresh):
    exc = stop_once(fresh, title, Plan([busy(), busy()], fresh.clock))
    assert type(exc) is transport.KameAuxTimeout
    assert host.fallback_reason(exc) == "request timed out", "the walk goes on to the next model, once"


def test_a_second_stop_of_the_same_task_in_the_same_context_is_a_plain_lane_failure(fresh):
    stop_once(fresh, title, Plan([busy(), busy()], fresh.clock))
    exc = stop_once(fresh, lambda plan: title(plan, identity=SECOND_MODEL), Plan([busy(), busy()], fresh.clock))
    assert type(exc) is transport.KameAuxStopped
    assert host.fallback_reason(exc) is None, "no capacity reason: the host re-raises, nothing is quarantined"
    assert not host.is_timeout_error(exc) and not host.is_connection_error(exc)
    assert not host.credential_rung_accepts(exc) and not host.is_transient_transport_error(exc)
    assert not host.should_skip_same_provider_retry("title_generation", exc)


def test_the_hosts_own_retry_of_the_same_model_inside_the_memo_is_a_lane_too(fresh):
    stop_once(fresh, title, Plan([busy(), busy()], fresh.clock))
    again = stop_once(fresh, title, Plan(["never"], fresh.clock))         # the 30 s memo: no request at all
    assert type(again) is transport.KameAuxStopped


def test_another_task_is_a_first_stop(fresh):
    stop_once(fresh, title, Plan([busy(), busy()], fresh.clock))
    other = stop_once(fresh, compression, Plan([busy(), busy()], fresh.clock))
    assert type(other) is transport.KameAuxTimeout


def test_after_a_minute_it_is_a_first_stop_again(fresh):
    stop_once(fresh, title, Plan([busy(), busy()], fresh.clock))
    fresh.clock.now += transport.WALK_WINDOW_S + 1.0
    again = stop_once(fresh, lambda plan: title(plan, identity=SECOND_MODEL), Plan([busy(), busy()], fresh.clock))
    assert type(again) is transport.KameAuxTimeout


def test_a_fresh_context_is_a_first_stop(fresh):
    stop_once(fresh, title, Plan([busy(), busy()], fresh.clock))
    seen = []

    def other_thread():            # a thread started without copy_context: another job, another walk
        seen.append(stop_once(fresh, lambda plan: title(plan, identity=SECOND_MODEL),
                              Plan([busy(), busy()], fresh.clock)))

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join(10)
    assert type(seen[0]) is transport.KameAuxTimeout


def test_the_lane_message_holds_none_of_the_hosts_words():
    forbidden = tuple(aux_budget._HOST_WORDS) + (
        "timed out", "timeout", "time out", "rate", "limit", "quota", "overload", "capacity", "unavailable",
        "connection", "server error", "429", "503", "busy", "unauthorized", "invalid api key")
    for task, model in (("title_generation", "gemini-3.7-flash"), ("compression", "z-ai/glm-5.3"),
                        ("weird task\n402 payment required", "x credits y"), ("", "")):
        text = aux_budget.lane_message(task, model).lower()
        for word in forbidden + tuple(host.PAYMENT_KEYWORDS) + tuple(host.RATE_LIMIT_KEYWORDS):
            assert word not in text, (word, text)
    assert "title_generation" in aux_budget.lane_message("title_generation", "gemini-3.7-flash")


def test_the_lane_error_has_no_status_response_or_body_and_no_telling_name():
    exc = transport.KameAuxStopped(aux_budget.lane_message("title_generation", "gemini-3.7-flash"))
    for attribute in ("status_code", "response", "body"):
        assert getattr(exc, attribute, None) is None
    assert not any(word in type(exc).__name__ for word in ("Timeout", "Connection", "DNS", "SSL"))
    assert not isinstance(exc, TimeoutError)
