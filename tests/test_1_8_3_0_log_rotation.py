"""1.8.3.0 (PLAN_1.8.3.0 D6, decisions/0033) - the instrument logs rotate instead of going silent.

Until 1.8.2.9 ``calls.jsonl`` stopped writing at 4 MB and ``refusals.jsonl`` at 8 MB and said nothing: from
that day on nothing was timed and nobody knew (found 2026-10-27, the stop had held for 13 days). Now the
current file plus ONE previous generation are kept (``calls.1.jsonl``), 2 MB and 4 MB each, so the disk
total is what it was. The size is checked on the first write and every 64 writes; each rotation and each
failure is said once in the log. The same scheme and sizes as the Agent Zero port (its decision 0011).
"""
from __future__ import annotations

import importlib
import json
import logging
import time

import pytest

from tests import test_1_8_2_7_handover as H

PACKAGE = H.PACKAGE
timings = importlib.import_module(f"{PACKAGE}.timings")
recorder = importlib.import_module(f"{PACKAGE}.recorder")
state_mod = importlib.import_module(f"{PACKAGE}.state")
settings = importlib.import_module(f"{PACKAGE}.settings")

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    monkeypatch.delenv("KAME_CALL_TIMINGS_DISABLED", raising=False)
    monkeypatch.delenv("KAME_RECORDER_DISABLED", raising=False)
    settings.forget()
    timings.forget()
    recorder.forget_rotation()
    yield tmp_path
    settings.forget()
    timings.forget()
    recorder.forget_rotation()


def names(folder):
    return sorted(p.name for p in folder.iterdir())


def row(i, pad=0):
    return {"n": i, "pad": "x" * pad}


def put(path, ceiling, i, pad):
    return recorder.append_line(path, ceiling, json.dumps(row(i, pad)))


def test_the_ceilings_are_half_the_old_totals():
    assert timings.CEILING_BYTES == 2 * MB
    assert recorder.CEILING_BYTES == 4 * MB


def test_the_previous_generation_is_named_beside_the_log(folder):
    assert recorder.previous_path(str(folder / "calls.jsonl")) == str(folder / "calls.1.jsonl")
    assert recorder.previous_path(str(folder / "refusals.jsonl")) == str(folder / "refusals.1.jsonl")


def test_writing_past_the_ceiling_rotates_and_new_rows_keep_landing(folder):
    path = str(folder / "calls.jsonl")
    for i in range(70):      # ~40 KB rows: the size check at the 65th write finds the file past 2 MB
        assert put(path, 2 * MB, i, 40000) is True
    assert names(folder) == ["calls.1.jsonl", "calls.jsonl"]
    assert (folder / "calls.1.jsonl").stat().st_size >= 2 * MB
    current = [json.loads(line) for line in (folder / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert current and current[-1]["n"] == 69


def test_the_first_write_after_start_checks_the_size(folder):
    big = folder / "calls.jsonl"
    big.write_text(("y" * 999 + "\n") * 3000, encoding="utf-8")        # ~3 MB left by the last run
    assert put(str(big), 2 * MB, 1, 0) is True
    assert (folder / "calls.1.jsonl").stat().st_size > 2 * MB
    assert json.loads(big.read_text(encoding="utf-8")) == row(1)


def test_no_third_generation_and_the_older_rows_are_the_ones_dropped(folder):
    path = str(folder / "calls.jsonl")
    for i in range(400):
        put(path, 1000, i, 50)
    assert names(folder) == ["calls.1.jsonl", "calls.jsonl"]
    old = [json.loads(line)["n"] for line in (folder / "calls.1.jsonl").read_text(encoding="utf-8").splitlines()]
    new = [json.loads(line)["n"] for line in (folder / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert old and new and old[-1] < new[0]
    assert new[-1] == 399 and len(old) + len(new) < 400


def test_each_rotation_is_said_once_in_the_log(folder, caplog):
    path = str(folder / "calls.jsonl")
    with caplog.at_level(logging.INFO):
        for i in range(130):
            put(path, 1000, i, 40)
    said = [r.getMessage() for r in caplog.records if "rotated" in r.getMessage()]
    assert said and all("calls.jsonl" in line for line in said), said


def test_timings_record_rotates_through_the_real_writer(folder, monkeypatch):
    monkeypatch.setattr(timings, "CEILING_BYTES", 2000)
    for i in range(140):
        timings.record(identity="p:m", key_fingerprint="k", attempt=i, outcome="answered",
                       started_at=1.0, ended_at=2.0, first_sign_at=1.5)
    assert names(folder) == ["calls.1.jsonl", "calls.jsonl"]
    lines = (folder / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["attempt"] == 139, "the writer is not silenced at the ceiling any more"


def test_the_refusal_recorder_rotates_through_the_real_writer(folder, monkeypatch):
    monkeypatch.setattr(recorder, "CEILING_BYTES", 3000)
    for i in range(140):
        recorder.record(provider="p", model="m", status_code=429, error_message=f"quota {i}",
                        error_body={"error": {"message": f"quota {i}"}}, error_type="E")
    assert names(folder) == ["refusals.1.jsonl", "refusals.jsonl"]
    last = json.loads((folder / "refusals.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["message"] == "quota 139"


def test_first_word_history_reads_the_previous_generation_then_the_current(folder):
    ident = "gemini:gemini-3.8-flash"

    def line(seconds):
        return json.dumps({"at": time.time(), "identity": ident, "outcome": "answered",
                           "ms_to_first_sign": int(seconds * 1000)}) + "\n"

    (folder / "calls.1.jsonl").write_text(line(1.0) + line(2.0), encoding="utf-8")
    (folder / "calls.jsonl").write_text(line(3.0), encoding="utf-8")
    assert timings.first_word_history() == {ident: [1.0, 2.0, 3.0]}


def test_first_word_history_works_with_only_the_previous_generation(folder):
    ident = "p:m"
    (folder / "calls.1.jsonl").write_text(json.dumps(
        {"at": time.time(), "identity": ident, "outcome": "answered", "ms_to_first_sign": 1500}) + "\n",
        encoding="utf-8")
    assert timings.first_word_history() == {ident: [1.5]}


def test_a_rotation_that_fails_is_said_once_and_never_raises(folder, monkeypatch, caplog):
    (folder / "calls.jsonl").write_text(("y" * 999 + "\n") * 3000, encoding="utf-8")

    def refuse(*_a, **_k):
        raise OSError("file is in use")

    monkeypatch.setattr(recorder.os, "replace", refuse)
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            timings.record(identity="p:m", key_fingerprint="k", attempt=i, outcome="answered",
                           started_at=1.0, ended_at=2.0)
    said = [r.getMessage() for r in caplog.records if "rotate" in r.getMessage()]
    assert len(said) == 1 and "in use" in said[0]


def test_a_failing_write_is_said_once_and_the_log_recovers(folder, monkeypatch, caplog):
    real = recorder.private_append
    state = {"fail": True}

    def flaky(path):
        if state["fail"]:
            raise OSError("disk full")
        return real(path)

    monkeypatch.setattr(recorder, "private_append", flaky)
    with caplog.at_level(logging.WARNING):
        for i in range(3):
            timings.record(identity="p:m", key_fingerprint="k", attempt=i, outcome="answered",
                           started_at=1.0, ended_at=2.0)
    said = [r.getMessage() for r in caplog.records if "disk full" in r.getMessage()]
    assert len(said) == 1
    state["fail"] = False
    timings.record(identity="p:m", key_fingerprint="k", attempt=9, outcome="answered", started_at=1.0, ended_at=2.0)
    assert (folder / "calls.jsonl").stat().st_size > 0, "not silenced for the rest of the process"


def test_a_log_that_cannot_rotate_stops_at_twice_its_ceiling_not_before(folder, monkeypatch):
    monkeypatch.setattr(recorder.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    path = str(folder / "calls.jsonl")
    ceiling, row_bytes = 50_000, 1000
    wrote = [put(path, ceiling, i, row_bytes - 20) for i in range(400)]
    assert all(wrote[:100]), "below twice the ceiling the rows keep landing although the rotation fails"
    assert False in wrote, "past twice the ceiling a row is dropped: the disk stays bounded"
    # the size is looked at every 64 writes, so the log can pass twice its ceiling by at most one window
    assert (folder / "calls.jsonl").stat().st_size < 2 * ceiling + 64 * (row_bytes + 20)


def test_the_setting_sentences_say_what_is_kept():
    texts = {row_["key"]: str(row_.get("help", "")) for row_ in settings.describe_all()}
    refusals = texts[settings.RECORDER_DISABLED]
    calls = texts[settings.CALL_TIMINGS_DISABLED]
    assert "keeps the last ~8 MB" in refusals and "stops at" not in refusals
    assert "keeps the last ~4 MB" in calls and "stops at" not in calls
