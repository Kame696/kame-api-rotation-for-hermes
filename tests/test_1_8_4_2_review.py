"""1.8.4.2 review fixes - the four places the adversarial review changed the build.

(a) A start probe is credited "answered again" only for a WORKING chunk (text, a tool call, reasoning) or a
    normal end - never for a role chunk alone: a model can open its stream and hang, and the race itself
    never counts a word-less chunk.
(b) A cosmetic background job on a model remembered as failing is refused before any request, so Hermes
    moves it down the user's list at once; an important job is still sent; the kill switch restores the old
    behaviour.
(c) The first-run seed from ``calls.jsonl`` keeps one event per (model, call), as ``note`` does live.
(d) Every write runs on a timer thread, never on the thread that noted the outcome (a failure is noted on the
    way to the reply's first word; one write took 18 ms median, 163 ms at worst on the owner's PC), and a note
    the throttle held back is written by that timer, not only by the next note.
"""
from __future__ import annotations

import importlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_9_aux_budget as A
from tests import test_1_8_2_9_start_probe as S
from tests.test_1_8_2_9_aux_budget import fresh as aux_fresh  # noqa: F401  (fixture by name)
from tests.test_1_8_2_9_start_probe import PRIMARY, Scripted, busy_and_due, fresh, pool  # noqa: F401

R = S.R
transport = R.transport
EVENTS = __import__(f"{R.PACKAGE}.core.events", fromlist=["x"]).EVENTS
mh = importlib.import_module(f"{R.PACKAGE}.core.model_health")
NOW = 1_800_000_000.0


class Streams(Scripted):
    """``Scripted`` plus models whose stream is spelled out step by step (``R.FakeStream`` steps)."""

    def __init__(self, pool_, script, streams):
        super().__init__(pool_, script)
        self.streams = dict(streams)

    def client_for(self, key, attempt):
        inner = super().client_for(key, attempt)
        create_inner = inner.chat.completions.create

        def create(**request):
            model = request.get("model")
            if model in self.streams:
                self.models_asked.append(model)
                self.asked_at.setdefault(model, time.monotonic())
                return R.FakeStream(list(self.streams[model]))
            return create_inner(**request)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def wait_for(predicate, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def judges():
    return [t for t in threading.enumerate() if t.name == "kame-probe-judge"]


def answered_again_lines():
    return [r["reason"] for r in EVENTS.recent(50) if "answered again beside the last reply" in r["reason"]]


# (a) --------------------------------------------------------------------------------------------------

def test_a_probe_that_sends_only_a_role_chunk_and_hangs_is_not_credited(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_PROBE_JUDGE_S", 1.0)
    busy_and_due(fresh)
    call = Streams(pool(), {"gemini-race-two": ("fast", 0.3)},
                   {"gemini-race": [0.02, R.chunk(role="assistant"), 6.0, R.chunk("late", finish="stop")]})
    EVENTS.clear()
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    assert wait_for(lambda: len(fresh._probe_verdicts) == 1), "the judge filed a verdict"
    assert fresh._probe_verdicts[0][2] == "silent", "a role chunk is not a model serving again"
    fresh._drain_probe_verdicts()
    assert PRIMARY in fresh._busy_window, "the window stands"
    assert transport.HEALTH.risky(PRIMARY), "the record keeps the failure"
    assert answered_again_lines() == []
    assert wait_for(lambda: not judges(), 8.0)


def test_a_probe_whose_first_word_comes_after_a_role_chunk_is_credited(fresh, monkeypatch):
    monkeypatch.setattr(transport, "_PROBE_JUDGE_S", 3.0)
    busy_and_due(fresh)
    call = Streams(pool(), {"gemini-race-two": ("fast", 0.3)},
                   {"gemini-race": [0.02, R.chunk(role="assistant"), 0.5, R.chunk("back "),
                                    R.chunk("again", finish="stop")]})
    EVENTS.clear()
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here", "the reply was never delayed"
    assert wait_for(lambda: len(fresh._probe_verdicts) == 1)
    assert fresh._probe_verdicts[0][2] == "ok"
    fresh._drain_probe_verdicts()
    assert PRIMARY not in fresh._busy_window and PRIMARY not in fresh._busy_until
    assert not transport.HEALTH.risky(PRIMARY)
    assert len(answered_again_lines()) == 1
    assert wait_for(lambda: not judges(), 8.0)


def test_a_probe_that_ended_normally_before_the_reply_won_is_credited_at_once(fresh):
    busy_and_due(fresh)
    call = Streams(pool(), {"gemini-race-two": ("fast", 0.4)},
                   {"gemini-race": [0.02, R.chunk(role="assistant"), R.chunk(finish="stop")]})
    EVENTS.clear()
    out = list(fresh.stream(call))
    assert R.shown(out) == "gemini-race-two here"
    # no judge: the leg had already ended, so the verdict is known when the race is decided
    assert PRIMARY not in fresh._busy_window and PRIMARY not in fresh._busy_until
    assert len(answered_again_lines()) == 1
    assert not judges()


def test_the_leg_records_its_first_working_chunk_apart_from_its_first_chunk():
    import queue

    class Wire:
        def open(self, client, request, leg):
            return iter([R.chunk(role="assistant"), R.chunk("word", finish="stop")])

    leg = transport._StreamLeg(queue.Queue(), "k", None, is_hedge=False)
    assert leg.word_at is None and leg.first_at is None
    leg._pump(Wire(), {})
    assert leg.first_at is not None and leg.word_at is not None and leg.word_at >= leg.first_at
    role_only = transport._StreamLeg(queue.Queue(), "k", None, is_hedge=False)

    class RoleWire:
        def open(self, client, request, leg):
            return iter([R.chunk(role="assistant")])

    role_only._pump(RoleWire(), {})
    assert role_only.first_at is not None and role_only.word_at is None


# (b) --------------------------------------------------------------------------------------------------

def _sicken_aux(identity, failures=2, ago=3600.0):
    now = time.time()
    for n in range(failures):
        A.transport.HEALTH.note(identity, False, now=now - ago - (failures - n))


def test_a_cosmetic_job_on_a_model_that_keeps_failing_is_refused_without_a_request(aux_fresh):
    _sicken_aux(A.IDENTITY)
    _sicken_aux("gemini:gemini-3.7-flash")      # 1.8.4.3 (H4): with a healthy sibling the job would move there
    assert A.transport.HEALTH.risky(A.IDENTITY)
    plan = A.Plan(["never"], aux_fresh.clock)
    before = aux_fresh.clock.now
    with pytest.raises(A.transport.KameAuxTimeout):
        aux_fresh.complete(A.title(plan))
    assert plan.asked == [], "not one request to the failing model"
    assert aux_fresh.clock.now - before < 1.0


def test_an_important_job_on_a_model_that_keeps_failing_is_still_sent(aux_fresh):
    _sicken_aux(A.IDENTITY)
    plan = A.Plan(["ok"], aux_fresh.clock)
    assert aux_fresh.complete(A.compression(plan)).choices[0].message.content == "ok"
    assert len(plan.asked) == 1


def test_the_kill_switch_sends_a_cosmetic_job_as_before(aux_fresh, monkeypatch):
    _sicken_aux(A.IDENTITY)
    monkeypatch.setenv("KAME_NO_REMEMBERED_TROUBLE", "1")
    plan = A.Plan(["ok"], aux_fresh.clock)
    assert aux_fresh.complete(A.title(plan)).choices[0].message.content == "ok"
    assert len(plan.asked) == 1


def test_a_cosmetic_job_is_sent_when_the_user_never_switches_model(aux_fresh, monkeypatch):
    _sicken_aux(A.IDENTITY)
    monkeypatch.setattr(A.settings, "model_switch_mode", lambda: "never")
    plan = A.Plan(["ok"], aux_fresh.clock)
    assert aux_fresh.complete(A.title(plan)).choices[0].message.content == "ok"
    assert len(plan.asked) == 1


# (c) --------------------------------------------------------------------------------------------------

def _row(identity, outcome, kind="", call_id=None, at=None):
    _row.n += 1
    row = {"at": (at if at is not None else time.time() - 3600) + _row.n, "identity": identity,
           "outcome": outcome, "kind": kind}
    if call_id:
        row["call_id"] = call_id
    return json.dumps(row)


_row.n = 0


def test_the_seed_keeps_one_event_per_model_and_call(tmp_path, monkeypatch):
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)
    calls = tmp_path / "calls.jsonl"
    calls.write_text("\n".join([
        _row("gemini:a", "failed", "server", "c1"),       # three keys refused in one reply: one failure
        _row("gemini:a", "failed", "server", "c1"),
        _row("gemini:a", "failed", "timeout", "c1"),
        _row("gemini:b", "failed", "server", "c2"),       # refused, then answered in the same reply: a success
        _row("gemini:b", "answered", "", "c2"),
        _row("gemini:c", "failed", "server", "c3"),       # two replies, two failures
        _row("gemini:c", "failed", "server", "c4"),
        _row("gemini:d", "failed", "server"),             # rows without a call id count one each
        _row("gemini:d", "failed", "server"),
    ]) + "\n", encoding="utf-8")
    h = mh.ModelHealth(directory=tmp_path, calls_path=calls)
    assert h.summary("gemini:a") == (1, 1) and not h.risky("gemini:a")
    assert h.summary("gemini:b") == (0, 1) and not h.risky("gemini:b")
    assert h.summary("gemini:c") == (2, 2) and h.risky("gemini:c")
    assert h.summary("gemini:d") == (2, 2) and h.risky("gemini:d")


# (d) --------------------------------------------------------------------------------------------------

def test_a_throttled_note_is_written_by_a_timer(tmp_path, monkeypatch):
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)
    monkeypatch.setattr(mh, "SAVE_EVERY_S", 0.3)
    ident = "gemini:gemini-3.8-flash:global"
    h = mh.ModelHealth(directory=tmp_path, calls_path=tmp_path / "none.jsonl")
    h.note(ident, False, now=NOW)
    assert wait_for(lambda: h.writes == 1, 2.0), "the first note is written a moment later"
    first = h.writes
    h.note(ident, False, now=NOW + 1)
    assert h.writes == first, "the second is held back by the throttle"
    assert wait_for(lambda: h.writes > first, 3.0), "and written by the timer, with no further note"
    again = mh.ModelHealth(directory=tmp_path, calls_path=tmp_path / "none.jsonl")
    assert again.summary(ident, now=NOW + 2) == (2, 2)


def test_the_disk_is_never_touched_on_the_thread_that_notes(tmp_path, monkeypatch):
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)
    h = mh.ModelHealth(directory=tmp_path, calls_path=tmp_path / "none.jsonl")
    writers = []
    original = h._write

    def write(path):
        writers.append(threading.current_thread())
        return original(path)

    monkeypatch.setattr(h, "_write", write)
    for n in range(3):
        h.note("gemini:y", False, now=NOW + n)
    h.risky("gemini:y")
    assert writers == [], "no write on the caller's thread"
    assert wait_for(lambda: len(writers) == 1, 3.0)
    assert writers[0] is not threading.current_thread()


def test_reading_the_record_never_waits_for_a_slow_disk(tmp_path, monkeypatch):
    # The measured regression: a write that held the memory lock made the next reply's start wait for the disk
    # (the storm test under CPU stress lost its 0.15 s start 2 times in 6; 1.8.4.1 never did).
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)
    h = mh.ModelHealth(directory=tmp_path, calls_path=tmp_path / "none.jsonl")
    in_disk = threading.Event()
    original = mh.ModelHealth._put

    def slow_put(path, document):
        in_disk.set()
        time.sleep(0.6)
        return original(path, document)

    monkeypatch.setattr(mh.ModelHealth, "_put", staticmethod(slow_put))
    h.note("gemini:slow", False, now=NOW)
    assert in_disk.wait(2.0), "the timer is writing"
    began = time.perf_counter()
    h.risky("gemini:slow")
    h.note("gemini:slow", False, now=NOW + 1)
    assert time.perf_counter() - began < 0.2, "a read and a note while the disk is busy do not wait for it"
    assert wait_for(lambda: h.writes >= 1, 3.0)


def test_a_timer_does_not_carry_one_directorys_outcomes_into_another(tmp_path, monkeypatch):
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    where = {"dir": first}
    h = mh.ModelHealth(calls_path=tmp_path / "none.jsonl")
    monkeypatch.setattr(h, "_directory", lambda: where["dir"])
    h.note("gemini:z", False, now=NOW)
    h.note("gemini:z", False, now=NOW + 1)
    where["dir"] = second                       # the data directory moves before the timer fires
    h.risky("gemini:z")                         # the next read follows it, and drops what it held
    time.sleep(0.4)
    assert not (second / mh.FILE_NAME).exists(), "the old directory's outcomes did not land in the new one"


def test_configure_cancels_a_pending_timer(tmp_path, monkeypatch):
    monkeypatch.delenv("KAME_NO_REMEMBERED_TROUBLE", raising=False)
    monkeypatch.setattr(mh, "SAVE_EVERY_S", 0.5)
    ident = "gemini:x"
    h = mh.ModelHealth(directory=tmp_path, calls_path=tmp_path / "none.jsonl")
    h.note(ident, False, now=NOW)
    h.note(ident, False, now=NOW + 1)
    assert h._timer is not None
    other = tmp_path / "other"
    other.mkdir()
    h.configure(directory=other, calls_path=tmp_path / "none.jsonl")
    assert h._timer is None
    time.sleep(0.8)
    assert not (other / mh.FILE_NAME).exists(), "nothing pending was carried to the new directory"
