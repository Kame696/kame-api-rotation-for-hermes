"""1.9.0.5 (PLAN C3) - the Desktop half is off: the chat says so once, and only when nothing reads the panel's door.

``dashboard/plugin_api.py`` ``/state`` stamps the time of the panel's last read in a file beside the snapshot (the
door can live in another process than the one that talks to the model); ``chat_line`` reads that stamp on the
first reply of a Desktop session.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests import h1905
from tests import test_1_8_2_7_panel_reads as P
from tests.test_1_9_0_5_chat_line import FakeCtx, make_call

chat_line = h1905.sub("chat_line")
settings = h1905.sub("settings")
state = h1905.sub("state")
NOTICE = "Turn on API Rotation in Capabilities -> Plugins to see its status bar and panel."


@pytest.fixture()
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("KAME_LIVE_CHAT_LINE", raising=False)
    settings.forget()
    folder = tmp_path / "plugin-data" / "hermes-kame-api-rotation"
    folder.mkdir(parents=True)
    monkeypatch.setattr(state, "state_dir", lambda: folder)
    monkeypatch.setattr(chat_line, "_session_key", lambda: "desk-1")
    monkeypatch.setattr(h1905.sub("provider_profiles"), "notices", lambda: [])
    chat_line.reset()
    api = P._load_plugin_api()
    monkeypatch.setattr(api, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_READ_PAUSE_S", 0.0)
    monkeypatch.setattr(api, "_last_stamp", 0.0)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api.router)
    yield SimpleNamespace(api=api, http=TestClient(app), folder=folder)
    chat_line.flush(2)
    chat_line.detach()
    settings.forget()


def reply(ctx):
    chat_line.attach(ctx)
    call = make_call()
    chat_line.note(call, "sent", "3 of 3 key(s) ready", {"identity": "gemini:m", "key": "k1"})
    chat_line.note(call, "answered", "", {"identity": "gemini:m", "seconds": 1.0})
    chat_line.flush()


def test_no_read_ever_means_one_notice(world):
    ctx = FakeCtx("desktop")
    reply(ctx)
    reply(ctx)
    assert ctx.posts == [NOTICE]
    assert chat_line.desktop_read_age() is None


def test_a_read_of_the_door_means_no_notice(world):
    world.http.get("/state")      # 404 (no snapshot yet) still counts: the panel asked
    assert 0 <= chat_line.desktop_read_age() < 5
    ctx = FakeCtx("desktop")
    reply(ctx)
    assert ctx.posts == []


def test_the_stamp_is_written_at_most_once_per_interval(world):
    world.http.get("/state")
    first = (world.folder / world.api.READ_STAMP_FILE).read_text(encoding="utf-8")
    time.sleep(0.02)
    world.http.get("/state")
    assert (world.folder / world.api.READ_STAMP_FILE).read_text(encoding="utf-8") == first


def test_an_old_read_counts_as_off(world):
    (world.folder / world.api.READ_STAMP_FILE).write_text(repr(time.time() - 11 * 60), encoding="utf-8")
    ctx = FakeCtx("desktop")
    reply(ctx)
    assert ctx.posts == [NOTICE]


def test_no_stamp_is_written_before_kame_has_a_data_directory(world, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(world.api, "get_hermes_home", lambda: other)
    world.http.get("/state")
    assert not (other / "plugin-data").exists()


def test_the_notice_is_the_desktop_only_notice(world):
    for surface in ("tui", "telegram", "cli"):
        chat_line.reset()
        ctx = FakeCtx(surface)
        reply(ctx)
        assert NOTICE not in ctx.said, surface


def test_the_setting_off_silences_the_notice_too(world, monkeypatch):
    monkeypatch.setenv("KAME_LIVE_CHAT_LINE", "0")
    settings.forget()
    ctx = FakeCtx("desktop")
    reply(ctx)
    assert ctx.said == []


def test_a_desktop_reply_with_a_rotation_still_posts_only_the_notice(world):
    ctx = FakeCtx("desktop")
    chat_line.attach(ctx)
    call = make_call()
    chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
    chat_line.note(call, "rotation", "x", {"identity": "gemini:m", "key": "k1"})
    chat_line.note_status(call.status_owner(), "↻ waiting on m — API Rotation 2/3 keys ready, trying the next key")
    chat_line.note(call, "recovery", "", {"identity": "gemini:m", "seconds": 2})
    chat_line.flush()
    assert ctx.posts == [NOTICE] and ctx.edits == []


def test_a_stamp_that_cannot_be_read_is_unknown_and_owes_no_notice(world):
    (world.folder / world.api.READ_STAMP_FILE).write_text("not-a-number", encoding="utf-8")
    age = chat_line.desktop_read_age()
    assert age is not None and age != age     # nan: unknown, not "never"
    ctx = FakeCtx("desktop")
    reply(ctx)
    assert ctx.posts == []


def test_only_a_missing_stamp_means_never_read(world):
    assert not (world.folder / world.api.READ_STAMP_FILE).exists()
    assert chat_line.desktop_read_age() is None
