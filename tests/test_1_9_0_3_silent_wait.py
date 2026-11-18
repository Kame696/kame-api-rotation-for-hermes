"""1.9.0.3 (C2) - the status row keeps saying what is happening while the model is silent.

Evidence: Hermes Desktop erases the provider-wait row on every reasoning/message delta, and KAME drew its line once
per attempt, so a reply with no first word for 85-218 s showed only Hermes' own clock. The real transport, a fake
provider with real time (the interval is shortened to one second here; production is five).
"""
from __future__ import annotations

import re
import threading
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests.test_1_8_2_5_stream_race import (  # noqa: F401  (fresh is a fixture)
    FakeCall, KEYS, Pool, chunk, fresh, settings, shown, transport)

SILENT = re.compile(r"no first word yet \((\d+)s\)")


@pytest.fixture()
def rail(monkeypatch):
    said = []
    monkeypatch.setattr(transport, "_NOTIFY", lambda message, *, kind="lifecycle": said.append((kind, message)) or True)
    transport._Spinner.reset()
    monkeypatch.setattr(transport._Spinner, "_CHANGED_FLOOR_S", 0.0)
    monkeypatch.setattr(transport, "_SILENT_LINE_EVERY_S", 1.0)
    yield said
    transport._Spinner.reset()


def _silent(rail):
    return [m for k, m in rail if k == "activity" and SILENT.search(m)]


def _answer(first_after):
    return [first_after, chunk("hello "), chunk("there", finish="stop")]


def test_a_silent_leg_gets_a_line_every_interval_with_the_seconds_going_up(fresh, rail):
    pool = Pool({KEYS[0]: [_answer(3.3)]})
    out = list(fresh.stream(FakeCall(pool, keys=KEYS[:1])))
    assert shown(out) == "hello there"
    lines = _silent(rail)
    assert len(lines) >= 2, rail
    seconds = [int(SILENT.search(m).group(1)) for m in lines]
    assert seconds == sorted(seconds) and len(set(seconds)) == len(seconds), seconds
    for line in lines:
        assert transport.passes_desktop_status_gate(line), line
        assert line.startswith("⏳ waiting on gemini-race — API Rotation 1/1 keys ready, no first word yet (")
        assert "KAME" not in line


def test_no_line_after_the_first_word(fresh, rail):
    pool = Pool({KEYS[0]: [_answer(1.4)]})
    list(fresh.stream(FakeCall(pool, keys=KEYS[:1])))
    count = len(_silent(rail))
    time.sleep(1.3)
    assert len(_silent(rail)) == count
    assert all(int(SILENT.search(m).group(1)) <= 2 for m in _silent(rail))


def test_a_reply_that_answers_at_once_says_nothing_extra(fresh, rail):
    pool = Pool({KEYS[0]: [[chunk("quick", finish="stop")]]})
    list(fresh.stream(FakeCall(pool, keys=KEYS[:1])))
    assert _silent(rail) == []


def test_a_backup_key_is_named_on_the_line(fresh, rail):
    pool = Pool({KEYS[0]: [_answer(3.3)], KEYS[1]: [_answer(3.3)], KEYS[2]: [_answer(3.3)], KEYS[3]: [_answer(3.3)]})
    list(fresh.stream(FakeCall(pool)))
    lines = _silent(rail)
    assert lines and any("; a backup key is asking too" in m for m in lines), rail


def test_another_model_of_the_list_is_named_on_the_line():
    # the wording alone: the extras the race collects are joined with "; " in this order
    line = transport.silent_wait_line(3, 4, "gemini-3.8-flash", 12.4, ["a backup key is asking too",
                                                                        "gemini-3.7-flash is asking too"])
    assert line == ("⏳ waiting on gemini-3.8-flash — API Rotation 3/4 keys ready, no first word yet (12s); "
                    "a backup key is asking too; gemini-3.7-flash is asking too")
    assert transport.passes_desktop_status_gate(line)
    assert transport.silent_wait_line(1, 1, "m", 5.0, []) == "⏳ waiting on m — API Rotation 1/1 keys ready, no first word yet (5s)"


def test_a_background_job_gets_no_line(fresh, rail):
    pool = Pool({KEYS[0]: [_answer(2.3)]})
    call = FakeCall(pool, keys=KEYS[:1])
    call.channel, call.task, call.task_known = "secondary", "title_generation", True
    list(fresh.stream(call))
    assert _silent(rail) == []


def test_a_cancelled_call_stops_the_lines(fresh, rail):
    pool = Pool({KEYS[0]: [_answer(8.0)]})
    call = FakeCall(pool, keys=KEYS[:1])
    threading.Timer(1.5, lambda: setattr(call, "closed", True)).start()
    with pytest.raises(Exception):
        list(fresh.stream(call))
    count = len(_silent(rail))
    time.sleep(1.5)
    assert len(_silent(rail)) == count
    assert all(int(SILENT.search(m).group(1)) <= 2 for m in _silent(rail))


def test_the_live_status_switch_silences_it(fresh, rail, monkeypatch):
    monkeypatch.setenv("KAME_LIVE_STATUS_DISABLED", "1")
    settings.forget()
    pool = Pool({KEYS[0]: [_answer(2.3)]})
    list(fresh.stream(FakeCall(pool, keys=KEYS[:1])))
    assert _silent(rail) == []


def test_the_stream_that_is_not_raced_is_covered_too(fresh, rail, monkeypatch):
    monkeypatch.setenv("KAME_HEDGE_MODE", "off")      # Saver: the plain stream, no race at all
    settings.forget()
    pool = Pool({KEYS[0]: [_answer(3.3)]})
    out = list(fresh.stream(FakeCall(pool, keys=KEYS[:1])))
    assert shown(out) == "hello there"
    lines = _silent(rail)
    assert len(lines) >= 2, rail
    assert all(transport.passes_desktop_status_gate(m) for m in lines)
