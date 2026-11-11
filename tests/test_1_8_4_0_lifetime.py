"""1.8.4.0 E5 and E6: what KAME did over time, sent in the last 24 hours, the provider's own daily number.

Display only. The counters live in ``lifetime.json`` beside the profile's other plugin data.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

PACKAGE = R.PACKAGE
lifetime = importlib.import_module(f"{PACKAGE}.core.lifetime")
evidence = importlib.import_module(f"{PACKAGE}.core.evidence")
state = importlib.import_module(f"{PACKAGE}.state")
transport = R.transport

IDENT = "gemini:gemini-3.8-flash"
NOW = 1_800_000_000.0

# the shape a Google free-tier refusal really has (also used by test_replay_timeline.py)
GOOGLE = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        "quotaDimensions": {"location": "global", "model": "gemini-3.8-flash"},
        "quotaValue": "20"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "5s"}]}}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture()
def box(tmp_path):
    clock = Clock()
    path = tmp_path / "lifetime.json"
    return lifetime.Lifetime(path=lambda: path, clock=clock), path, clock


# -- E5 --------------------------------------------------------------------------
def test_a_clean_reply_adds_one_to_replies_only(box):
    life, path, _ = box
    life.reply()
    snap = life.snapshot()
    assert (snap["replies"], snap["saved"]) == (1, 0)


def test_a_reply_after_a_failed_attempt_is_saved(box):
    life, _, _ = box
    life.reply(saved=True, key_switches=1)
    snap = life.snapshot()
    assert (snap["replies"], snap["saved"], snap["key_switches"]) == (1, 1, 1)


def test_the_write_is_throttled_to_once_a_minute(box):
    life, path, clock = box
    life.reply()
    assert life.writes == 1
    for _ in range(5):
        clock.now += 5
        life.reply()
    assert life.writes == 1                        # 25 s later: still the first write
    clock.now += 60
    life.reply()
    assert life.writes == 2
    assert json.loads(path.read_text(encoding="utf-8"))["replies"] == 7


def test_a_clean_exit_writes_what_is_left(box):
    life, path, clock = box
    life.reply()
    clock.now += 1
    life.reply()
    life.flush(True)
    assert json.loads(path.read_text(encoding="utf-8"))["replies"] == 2


def test_the_file_survives_a_restart_and_two_writers_add_up(box):
    life, path, clock = box
    life.reply()
    other = lifetime.Lifetime(path=lambda: path, clock=clock)
    other.reply(saved=True)
    other.flush(True)
    life.reply()
    life.flush(True)
    final = json.loads(path.read_text(encoding="utf-8"))
    assert (final["replies"], final["saved"]) == (3, 1)
    fresh = lifetime.Lifetime(path=lambda: path, clock=clock)
    assert fresh.snapshot()["replies"] == 3


def test_a_corrupt_file_starts_from_zero(box):
    life, path, _ = box
    path.write_text("{ this is not json", encoding="utf-8")
    assert life.snapshot()["replies"] == 0
    life.reply()
    life.flush(True)
    assert json.loads(path.read_text(encoding="utf-8"))["replies"] == 1


def test_a_store_that_cannot_be_written_loses_the_count_quietly(tmp_path):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x", encoding="utf-8")
    life = lifetime.Lifetime(path=lambda: blocked / "lifetime.json", clock=Clock())
    life.reply()           # must not raise
    assert life.snapshot()["replies"] == 1


def test_no_key_text_in_the_file(box):
    life, path, _ = box
    life.reply(saved=True)
    life.sent(IDENT)
    life.note_limit(IDENT, GOOGLE)
    life.flush(True)
    text = path.read_text(encoding="utf-8")
    assert "AIza" not in text and "key:" not in text
    assert set(json.loads(text)) == {"since", "replies", "saved", "key_switches", "model_switches", "sent", "limits"}


# -- E6 --------------------------------------------------------------------------
def test_buckets_prune_past_25_hours(box):
    life, path, clock = box
    life.sent(IDENT, now=NOW - 30 * 3600)
    life.sent(IDENT, now=NOW - 26 * 3600)
    life.sent(IDENT, now=NOW - 24.5 * 3600)
    life.sent(IDENT, now=NOW - 3600)
    life.sent(IDENT, now=NOW)
    life.flush(True)
    hours = json.loads(path.read_text(encoding="utf-8"))["sent"][IDENT]
    assert len(hours) == 3 and min(int(h) for h in hours) >= int(NOW // 3600) - 25


def test_sent_24h_counts_only_the_last_24_hours(box):
    life, _, _ = box
    for ago in (0, 1, 23):
        life.sent(IDENT, now=NOW - ago * 3600)
    life.sent(IDENT, now=NOW - 24.5 * 3600)       # 25th hour: kept for slack, not shown
    assert life.view(IDENT)["sent_24h"] == 3


def test_a_hedge_leg_counts_as_sent(tmp_path, monkeypatch):
    life = lifetime.Lifetime(path=lambda: tmp_path / "l.json", clock=Clock())
    monkeypatch.setattr(transport, "LIFETIME", life)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    call = SimpleNamespace(channel="main")
    t._count_request(call, IDENT)         # the primary
    t._count_request(call, IDENT)         # a backup leg goes through the same door
    assert life.view(IDENT)["sent_24h"] == 2


def test_the_real_google_shape_gives_20_per_project():
    assert evidence.stated_daily_limit(GOOGLE) == (20, "project")


def test_a_per_key_day_limit_is_the_keys():
    body = {"error": {"details": [{"violations": [{"quotaId": "RequestsPerDay-FreeTier", "quotaValue": "50"}]}]}}
    assert evidence.stated_daily_limit(body) == (50, "key")


@pytest.mark.parametrize("body", [None, "", "text", 5, [], {}, {"error": "x"},
                                  {"error": {"details": [{"violations": [{"quotaId": "PerMinute", "quotaValue": "5"}]}]}},
                                  {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDay", "quotaValue": "many"}]}]}},
                                  {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDay", "quotaValue": "0"}]}]}},
                                  {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDay"}]}]}},
                                  {"error": {"details": [{"violations": "oops"}]}}])
def test_malformed_bodies_give_none(body):
    assert evidence.stated_daily_limit(body) is None


def test_latest_stated_limit_wins_and_persists(box):
    life, path, clock = box
    life.note_limit(IDENT, GOOGLE)
    clock.now += 10
    later = json.loads(json.dumps(GOOGLE))
    later["error"]["details"][0]["violations"][0]["quotaValue"] = "250"
    life.note_limit(IDENT, later)
    life.flush(True)
    again = lifetime.Lifetime(path=lambda: path, clock=clock)
    assert again.view(IDENT)["daily_limit"] == 250 and again.view(IDENT)["daily_scope"] == "project"
    assert again.view("other:model")["daily_limit"] is None


def test_a_body_without_a_stated_limit_changes_nothing(box):
    life, _, _ = box
    life.note_limit(IDENT, GOOGLE)
    life.note_limit(IDENT, {"error": {"message": "slow down"}})
    assert life.view(IDENT)["daily_limit"] == 20


# -- wiring ----------------------------------------------------------------------
def test_the_snapshot_carries_the_numbers(tmp_path, monkeypatch):
    life = lifetime.Lifetime(path=lambda: tmp_path / "l.json", clock=Clock())
    life.reply(saved=True)
    life.sent(IDENT)
    life.note_limit(IDENT, GOOGLE)
    import sys
    monkeypatch.setattr(sys.modules[f"{PACKAGE}.core.lifetime"], "LIFETIME", life)
    totals = state._lifetime_totals()
    assert totals["replies"] == 1 and totals["saved"] == 1 and totals["since"]


def test_clear_pool_and_clear_events_keep_the_counters(tmp_path, monkeypatch):
    control = importlib.import_module(f"{PACKAGE}.control")
    life = lifetime.Lifetime(path=lambda: tmp_path / "l.json", clock=Clock())
    life.reply(saved=True)
    life.flush(True)
    before = (tmp_path / "l.json").read_text(encoding="utf-8")
    import sys
    monkeypatch.setattr(sys.modules[f"{PACKAGE}.core.lifetime"], "LIFETIME", life)
    for action in ("clear_pool", "clear_events", "reset_all"):
        try:
            control._apply(action, "", None)
        except Exception:
            pass
    assert (tmp_path / "l.json").read_text(encoding="utf-8") == before
    assert life.snapshot()["replies"] == 1


def _answered(t, call, identity, facts):
    call.channel = call.channel
    call._kame_facts = facts
    progress = SimpleNamespace(first_text_at=None, first_sign_at=None)
    t._after_answer(call, identity, progress)


def test_a_chat_reply_is_counted_and_a_background_job_is_not(tmp_path, monkeypatch):
    life = lifetime.Lifetime(path=lambda: tmp_path / "l.json", clock=Clock())
    monkeypatch.setattr(transport, "LIFETIME", life)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    chat = SimpleNamespace(channel="main", identity=IDENT, task="")
    aux = SimpleNamespace(channel="title", identity=IDENT, task="title")
    _answered(t, chat, IDENT, {"attempts": 1, "resumes": 0, "waited_s": 0.0})
    _answered(t, aux, IDENT, {"attempts": 3, "resumes": 0, "waited_s": 0.0})
    snap = life.snapshot()
    assert (snap["replies"], snap["saved"]) == (1, 0)
    _answered(t, chat, IDENT, {"attempts": 2, "resumes": 0, "waited_s": 0.0})
    snap = life.snapshot()
    assert (snap["replies"], snap["saved"], snap["key_switches"]) == (2, 1, 1)


def test_a_file_that_cannot_be_read_right_now_keeps_the_counts(box, monkeypatch):
    """Another process can hold the file for a moment: the counts must survive it, not restart from this process's."""
    life, _path, _clock = box
    life.reply()
    life.flush(True)
    life.reply()
    real = Path.read_text

    def busy(self, *a, **k):
        raise PermissionError("held by another process")

    monkeypatch.setattr(Path, "read_text", busy)
    assert life.flush(True) is False
    monkeypatch.setattr(Path, "read_text", real)
    assert life.flush(True)
    assert life.snapshot()["replies"] == 2


def test_a_tokens_a_day_limit_is_not_shown_as_replies_a_day():
    """`quotaId` with PerDay also names token quotas; the line says "a day" about requests."""
    body = {"error": {"details": [{"violations": [
        {"quotaId": "GenerateContentInputTokensPerDayPerProjectPerModel-FreeTier", "quotaValue": "1000000"}]}]}}
    assert evidence.stated_daily_limit(body) is None


def test_a_race_won_by_being_faster_is_not_saved_from_an_error(tmp_path, monkeypatch):
    life = lifetime.Lifetime(path=lambda: tmp_path / "l.json", clock=Clock())
    monkeypatch.setattr(transport, "LIFETIME", life)
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    other = "gemini:gemini-3.6-flash"
    monkeypatch.setattr(t, "_origin_of", lambda call, identity: IDENT)
    chat = SimpleNamespace(channel="main", identity=other, task="", _kame_standin_why="slow")
    _answered(t, chat, other, {"attempts": 1, "resumes": 0, "waited_s": 0.0})
    snap = life.snapshot()
    assert (snap["replies"], snap["saved"], snap["model_switches"]) == (1, 0, 1)
    failed = SimpleNamespace(channel="main", identity=other, task="", _kame_standin_why="quota")
    _answered(t, failed, other, {"attempts": 1, "resumes": 0, "waited_s": 0.0})
    assert life.snapshot()["saved"] == 1


# -- 1.8.4.1 K3: the file is read again when it changes ------------------------------------
def _bump_mtime(path, step=5):
    """Make the file's signature differ even on a coarse clock."""
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + step * 1_000_000_000))


def test_an_idle_process_shows_what_another_process_wrote(tmp_path):
    path = tmp_path / "lifetime.json"
    a = lifetime.Lifetime(path=lambda: path, clock=Clock())
    b = lifetime.Lifetime(path=lambda: path, clock=Clock())
    assert b.snapshot()["replies"] == 0          # B read the (missing) file once
    a.reply()
    a.flush(True)
    assert b.snapshot()["replies"] == 1          # the file changed: B reads it again
    a.reply()
    a.reply()
    a.flush(True)
    _bump_mtime(path)
    assert b.snapshot()["replies"] == 3


def test_a_process_own_unflushed_counts_are_added_once(tmp_path):
    path = tmp_path / "lifetime.json"
    a = lifetime.Lifetime(path=lambda: path, clock=Clock())
    b = lifetime.Lifetime(path=lambda: path, clock=Clock())
    a.reply()
    a.flush(True)
    b.reply()                                     # B's own increment, not in the file
    assert b.snapshot()["replies"] == 2
    assert b.snapshot()["replies"] == 2           # asking twice counts nothing twice
    a.reply()
    a.flush(True)
    _bump_mtime(path)
    assert b.snapshot()["replies"] == 3


def test_a_file_that_vanishes_reads_as_empty(tmp_path):
    path = tmp_path / "lifetime.json"
    a = lifetime.Lifetime(path=lambda: path, clock=Clock())
    b = lifetime.Lifetime(path=lambda: path, clock=Clock())
    a.reply()
    a.flush(True)
    assert b.snapshot()["replies"] == 1
    path.unlink()
    assert b.snapshot()["replies"] == 0


def test_a_read_that_raises_keeps_the_last_good_numbers(tmp_path, monkeypatch):
    path = tmp_path / "lifetime.json"
    a = lifetime.Lifetime(path=lambda: path, clock=Clock())
    b = lifetime.Lifetime(path=lambda: path, clock=Clock())
    a.reply()
    a.flush(True)
    assert b.snapshot()["replies"] == 1
    a.reply()
    a.flush(True)
    _bump_mtime(path)
    real = b._read

    def broken():
        raise PermissionError("held by another process")

    monkeypatch.setattr(b, "_read", broken)
    assert b.snapshot()["replies"] == 1           # the last good numbers, nothing raised
    monkeypatch.setattr(b, "_read", real)
    assert b.snapshot()["replies"] == 2           # the signature was not kept: tried again


def test_flush_keeps_the_signature_so_the_next_call_does_not_read(tmp_path, monkeypatch):
    path = tmp_path / "lifetime.json"
    a = lifetime.Lifetime(path=lambda: path, clock=Clock())
    a.reply()
    a.flush(True)
    reads = []
    real = a._read
    monkeypatch.setattr(a, "_read", lambda: (reads.append(1), real())[1])
    assert a.snapshot()["replies"] == 1
    assert reads == []
