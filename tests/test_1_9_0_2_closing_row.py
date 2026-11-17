"""1.9.0.2 (H5) - every call that ends without an answer leaves one closing row.

Live (audit/2026-11-15-pre-1902-bug-hunt.md, F6): ``calls.jsonl`` held only ``rotate`` and ``answered``
rows, so a call stopped by the owner, handed to Hermes' fallback or given up on ended on a ``rotate``
row - indistinguishable from one still running. The Stop at 10:02:31 on 2026-11-15 could only be dated
from the host's log.

Pinned: one ``record_type: close`` row per such call, with the right ``outcome``; never one after an
answer; never two; and the row carries no key and no failure class, so the readers that count a key's
or a model's failures never count it.
"""
from __future__ import annotations

import importlib
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_9_0_2_cancel as C

transport = R.transport
fresh = R.fresh
timings = importlib.import_module(f"{R.PACKAGE}.timings")


@pytest.fixture()
def rows(monkeypatch):
    seen = []
    monkeypatch.setattr(timings, "record", lambda **kw: seen.append(kw))
    return seen


def closes(rows):
    return [r for r in rows if r.get("record_type") == "close"]


def _called(identity=R.IDENTITY):
    call = C.RefusingCall()
    call._kame_call_id = "test-1"
    call.identity = identity
    return call


class TestTheOutcome:
    def test_a_stopped_call_closes_as_cancelled(self, fresh, rows):
        call = C.RefusingCall()
        C._cancel_after(call, 0.5)
        C._drive(fresh, call)
        assert [r["outcome"] for r in closes(rows)] == ["cancelled"]

    def test_a_stopped_non_streaming_call_closes_as_cancelled(self, fresh, rows):
        call = C.RefusingCall(stream=False)
        C._cancel_after(call, 0.5)
        C._drive(fresh, call, stream=False)
        assert [r["outcome"] for r in closes(rows)] == ["cancelled"]

    def test_a_hand_over_closes_as_handed_over(self, fresh, rows):
        fresh._close_row(_called(), transport.KamePoolResting("kame: busy. Handing the call over.", 0.0))
        assert [r["outcome"] for r in closes(rows)] == ["handed_over"]

    def test_a_total_wait_bound_closes_as_gave_up(self, fresh, rows):
        fresh._close_row(_called(), ConnectionError(
            "kame: every key for x stayed resting for 5m, past max_total_wait_seconds"))
        assert [r["outcome"] for r in closes(rows)] == ["gave_up"]

    def test_anything_else_closes_as_raised(self, fresh, rows):
        fresh._close_row(_called(), ValueError("a request the provider called invalid"))
        assert [r["outcome"] for r in closes(rows)] == ["raised"]


class TestOneRowAtMost:
    def test_never_two_rows_for_one_call(self, fresh, rows):
        call = _called()
        fresh._close_row(call, ValueError("x"))
        fresh._close_row(call, ValueError("y"))
        assert len(closes(rows)) == 1

    def test_no_row_after_an_answer(self, fresh, rows):
        pool = R.Pool({R.KEYS[0]: [[R.chunk("hello "), R.chunk("world", finish="stop")]]})
        call = R.FakeCall(pool, keys=R.KEYS[:1])
        list(fresh.stream(call))
        assert [r["outcome"] for r in rows if r.get("outcome") == "answered"] == ["answered"]
        assert closes(rows) == []

    def test_no_cancel_row_when_the_reader_stops_after_content_reached_it(self, fresh, rows):
        pool = R.Pool({R.KEYS[0]: [[R.chunk("hello "), R.chunk("world", finish="stop")]]})
        call = R.FakeCall(pool, keys=R.KEYS[:1])
        gen = fresh.stream(call)
        for item in gen:
            if R.shown([item]) == "world":
                break
        gen.close()                                   # the host stops reading: GeneratorExit
        assert closes(rows) == []

    def test_no_row_for_a_call_that_never_reached_a_key(self, fresh, rows):
        call = C.RefusingCall()
        fresh._close_row(call, ValueError("x"))       # no call id: _drive never started a key loop
        assert closes(rows) == []


class TestTheReadersIgnoreIt:
    def test_the_row_names_no_key_and_no_failure_class(self, fresh, rows):
        fresh._close_row(_called(), ValueError("x"))
        row = closes(rows)[0]
        assert not row.get("key_fingerprint")
        assert not row.get("kind")
        assert row.get("call_id") == "test-1"
