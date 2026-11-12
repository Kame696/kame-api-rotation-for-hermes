"""1.8.4.2 (S1a, S1f) - remembered trouble: the record of which models keep failing.

``core/model_health.py`` keeps the last judged outcomes of each model across processes and restarts.
A model is risky when its latest judged outcome is a failure and at least two of its last ten outcomes
within seven days are failures.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import time

import pytest

from tests import test_1_8_2_5_stream_race as R

mh = importlib.import_module(f"{R.PACKAGE}.core.model_health")
ID = "gemini:gemini-3.8-flash:global"
NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)


def _wait(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def make(tmp_path, **kw):
    return mh.ModelHealth(directory=tmp_path, calls_path=kw.get("calls_path", tmp_path / "absent-calls.jsonl"))


def test_empty_is_not_risky(tmp_path):
    assert make(tmp_path).risky(ID, now=NOW) is False


def test_one_failure_is_not_risky(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, now=NOW)
    assert h.risky(ID, now=NOW + 1) is False


def test_two_failures_latest_failure_is_risky(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, now=NOW)
    h.note(ID, False, now=NOW + 5)
    assert h.risky(ID, now=NOW + 6) is True
    assert h.summary(ID, now=NOW + 6) == (2, 2)


def test_two_failures_then_a_success_is_not_risky(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, now=NOW)
    h.note(ID, False, now=NOW + 5)
    h.note(ID, True, now=NOW + 9)
    assert h.risky(ID, now=NOW + 10) is False


def test_failures_older_than_seven_days_are_ignored(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, now=NOW)
    h.note(ID, False, now=NOW + 5)
    assert h.risky(ID, now=NOW + 6) is True
    assert h.risky(ID, now=NOW + mh.MAX_AGE_S + 10) is False
    # one old failure and one fresh failure: only one counts
    h.note(ID, False, now=NOW + mh.MAX_AGE_S + 20)
    assert h.risky(ID, now=NOW + mh.MAX_AGE_S + 21) is False


def test_only_the_last_ten_count(tmp_path):
    h = make(tmp_path)
    for i in range(2):
        h.note(ID, False, now=NOW + i)
    for i in range(9):
        h.note(ID, True, now=NOW + 10 + i)
    h.note(ID, False, now=NOW + 30)
    # the window is the last ten: eight successes... the two old failures have slid out; one failure left
    assert h.summary(ID, now=NOW + 31) == (1, 10)
    assert h.risky(ID, now=NOW + 31) is False


def test_a_call_that_saw_a_503_then_answered_is_one_success(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, call_id="c1", now=NOW)
    h.note(ID, True, call_id="c1", now=NOW + 4)
    assert h.summary(ID, now=NOW + 5) == (0, 1)
    h.flush()
    assert make(tmp_path).summary(ID, now=NOW + 5) == (0, 1), "the replaced failure does not come back from the file"


def test_answered_then_a_late_failure_is_still_a_success(tmp_path):
    h = make(tmp_path)
    h.note(ID, True, call_id="c1", now=NOW)
    h.note(ID, False, call_id="c1", now=NOW + 2)
    assert h.summary(ID, now=NOW + 3) == (0, 1)


def test_another_call_is_another_event(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, call_id="c1", now=NOW)
    h.note(ID, False, call_id="c2", now=NOW + 3)
    assert h.risky(ID, now=NOW + 4) is True


def test_persistence_round_trip(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, now=NOW)
    h.note(ID, False, now=NOW + 5)
    h.flush()
    document = json.loads((tmp_path / mh.FILE_NAME).read_text(encoding="utf-8"))
    assert document["schema"] == 1 and ID in document["models"]
    again = make(tmp_path)
    assert again.risky(ID, now=NOW + 6) is True


def test_a_second_writers_events_are_merged(tmp_path):
    a, b = make(tmp_path), make(tmp_path)
    a.note(ID, False, now=NOW)
    a.flush()
    b.note("gemini:gemini-3.7-flash:global", False, now=NOW + 1)
    b.note(ID, False, now=NOW + 2)
    b.flush()
    c = make(tmp_path)
    assert c.risky(ID, now=NOW + 3) is True, "the first writer's failure and the second's are both there"
    assert c.summary("gemini:gemini-3.7-flash:global", now=NOW + 3) == (1, 1)
    # the first process notices the file changed and re-reads it
    a.ensure_loaded()
    assert a.summary(ID, now=NOW + 3) == (2, 2)


def test_a_corrupt_file_is_empty_and_never_raises(tmp_path):
    (tmp_path / mh.FILE_NAME).write_text("{not json", encoding="utf-8")
    h = make(tmp_path)
    assert h.risky(ID, now=NOW) is False
    h.note(ID, False, now=NOW)
    h.note(ID, False, now=NOW + 1)
    assert h.risky(ID, now=NOW + 2) is True
    (tmp_path / mh.FILE_NAME).write_text(json.dumps({"schema": 99, "models": {ID: [[NOW, 0]]}}), encoding="utf-8")
    assert make(tmp_path).risky(ID, now=NOW + 2) is False, "an unknown schema is treated as empty"


def test_a_read_only_directory_is_memory_only(tmp_path):
    target = tmp_path / "blocked"
    target.write_text("a file where the directory should be", encoding="utf-8")    # mkdir / mkstemp cannot work
    h = mh.ModelHealth(directory=target / "sub", calls_path=tmp_path / "none.jsonl")
    h.note(ID, False, now=NOW)
    h.note(ID, False, now=NOW + 1)
    h.flush()
    assert h.risky(ID, now=NOW + 2) is True, "kept in memory"


def test_the_kill_switch(tmp_path, monkeypatch):
    h = make(tmp_path)
    monkeypatch.setenv("KAME_NO_REMEMBERED_TROUBLE", "1")
    h.note(ID, False, now=NOW)
    h.note(ID, False, now=NOW + 1)
    h.flush()
    assert h.risky(ID, now=NOW + 2) is False
    assert not (tmp_path / mh.FILE_NAME).exists(), "nothing written"
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE")
    assert h.risky(ID, now=NOW + 2) is False, "nothing was kept while it was off"


def test_forget_drops_the_model_here_and_in_the_file(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, now=time.time() - 50)
    h.note(ID, False, now=time.time() - 40)
    h.note("gemini:other:global", False, now=time.time() - 30)
    h.flush()
    other = make(tmp_path)
    assert other.risky(ID) is True
    assert h.forget(lambda ident: "3.8" in ident) == 1
    assert h.risky(ID) is False
    other.ensure_loaded()
    assert other.risky(ID) is False, "another process that holds the old events sees the tombstone"
    assert other.summary("gemini:other:global") == (1, 1)
    h.note(ID, False)
    assert h.summary(ID) == (1, 1), "an outcome after the forget counts again"


def test_the_save_runs_off_the_callers_thread_at_most_once_per_two_seconds(tmp_path):
    h = make(tmp_path)
    h.note(ID, False, now=NOW)
    assert h.writes == 0, "the caller never waits for the disk"
    assert _wait(lambda: h.writes == 1, 2.0), "a timer writes it a moment later"
    h.note(ID, False, now=NOW + 1)
    h.note(ID, True, now=NOW + 2)
    assert h.writes == 1, "two more notes inside two seconds are not written yet"
    h.flush()
    assert h.writes == 2
    assert make(tmp_path).summary(ID, now=NOW + 3) == (2, 3)


def test_risky_entry_reads_every_identity_of_the_entry(tmp_path):
    h = make(tmp_path)
    h.note("gemini:gemini-3.8-flash:global", False, now=NOW)
    h.note("gemini:gemini-3.8-flash:global", False, now=NOW + 1)
    same = importlib.import_module(f"{R.PACKAGE}.core.fallback_chain")._same
    assert h.risky_entry("gemini:gemini-3.8-flash", same) is True
    assert h.risky_entry("gemini:gemini-3.7-flash", same) is False


# --- S1f: seeding ----------------------------------------------------------------------------------

def _row(identity, outcome, kind="", at=None):  # each row a distinct instant
    _row.n += 1
    return json.dumps({"at": at + _row.n if at is not None else time.time() - 3600 + _row.n, "identity": identity,
                       "outcome": outcome, "kind": kind})


_row.n = 0


def test_seeded_from_calls_jsonl_on_first_run(tmp_path):
    calls = tmp_path / "calls.jsonl"
    old = time.time() - 9 * 86400
    calls.write_text("\n".join([
        _row("gemini:a:global", "failed", "server"),
        _row("gemini:a:global", "failed", "server"),
        _row("gemini:a:global", "failed", "timeout"),
        _row("gemini:b:global", "answered"),
        _row("gemini:c:global", "failed", "server", at=old),       # too old
        _row("gemini:c:global", "failed", "server", at=old),
        _row("gemini:d:global", "failed", "rate_limit"),             # a 429: neutral
        _row("gemini:d:global", "failed", "rate_limit"),
        "not json at all",
    ]) + "\n", encoding="utf-8")
    h = mh.ModelHealth(directory=tmp_path, calls_path=calls)
    assert h.risky("gemini:a:global") is True
    assert h.risky("gemini:b:global") is False and h.summary("gemini:b:global") == (0, 1)
    assert h.risky("gemini:c:global") is False and h.summary("gemini:c:global") == (0, 0)
    assert h.summary("gemini:d:global") == (0, 0)
    assert _wait(lambda: (tmp_path / mh.FILE_NAME).exists(), 2.0), "the seeded record is written"


def test_no_seeding_when_the_file_exists(tmp_path):
    calls = tmp_path / "calls.jsonl"
    calls.write_text(_row("gemini:a:global", "failed", "server") + "\n" + _row("gemini:a:global", "failed", "server")
                     + "\n", encoding="utf-8")
    (tmp_path / mh.FILE_NAME).write_text(json.dumps({"schema": 1, "models": {}}), encoding="utf-8")
    h = mh.ModelHealth(directory=tmp_path, calls_path=calls)
    assert h.risky("gemini:a:global") is False
