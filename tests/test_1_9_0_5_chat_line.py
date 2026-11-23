"""1.9.0.5 (PLAN C1, C2) - the live line in the conversation, through a fake ``ctx.post_message``.

A fake plugin context records every post and edit. Rules pinned here: a clean reply posts nothing; a rotation
posts once and edits; the TUI keeps ONE handle per session; a messaging platform posts only for a wait of more
than ten seconds or another model; Desktop and the classic CLI post nothing live; edits are throttled and the
last text wins; a blocking ``post_message`` never delays the reply thread; ``ok == False`` ends the line silently;
the post runs inside the context captured on the reply thread; the setting turns it all off. The last class runs
the real transport.
"""
from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace

import pytest

from tests import h1905
from tests import test_1_8_2_5_stream_race as R

chat_line = h1905.sub("chat_line")
settings = h1905.sub("settings")

WHO = contextvars.ContextVar("who", default="nobody")


class Handle:
    def __init__(self, ctx, ok=True):
        self.ctx, self.ok = ctx, ok

    def edit(self, text):
        self.ctx.edits.append(text)
        self.ctx.edit_threads.append(threading.current_thread().name)
        return {"ok": self.ctx.edit_ok}


class FakeCtx:
    def __init__(self, surface="tui", ok=True, block=None):
        self.surface, self.ok, self.block = surface, ok, block
        self.posts, self.edits, self.who, self.edit_threads, self.post_threads = [], [], [], [], []
        self.edit_ok = True

    def session_surface(self):
        return self.surface

    def post_message(self, text):
        if self.block is not None:
            self.block.wait(10)
        self.posts.append(text)
        self.who.append(WHO.get())
        self.post_threads.append(threading.current_thread().name)
        return Handle(self, ok=self.ok)

    @property
    def said(self):
        """Every sentence in order, posts and edits alike."""
        return self.posts + self.edits


def make_call(name="client:1", channel="main"):
    return SimpleNamespace(channel=channel, status_owner=lambda: name)


@pytest.fixture()
def line(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("KAME_LIVE_CHAT_LINE", raising=False)
    settings.forget()
    monkeypatch.setattr(chat_line, "EDIT_EVERY_S", 0.05)
    monkeypatch.setattr(chat_line, "_session_key", lambda: "s1")
    monkeypatch.setattr(chat_line, "desktop_read_age", lambda: 5.0)  # the panel is open unless a test says not
    monkeypatch.setattr(h1905.sub("provider_profiles"), "notices", lambda: [])
    chat_line.reset()
    yield chat_line
    chat_line.flush(2)
    chat_line.detach()
    settings.forget()


def attach(ctx):
    chat_line.attach(ctx)
    return ctx


def refused_then_answered(call, spin=True, model="gemini:m"):
    """The events of a reply whose first key was refused and the second answered."""
    chat_line.note(call, "sent", "3 of 3 key(s) ready", {"identity": model, "key": "k1"})
    chat_line.note(call, "rotation", "rate limited", {"identity": model, "key": "k1"})
    chat_line.flush()   # a real refusal and its retry are seconds apart: the post has gone out by then
    if spin:
        chat_line.note_status(call.status_owner(), "↻ waiting on m — API Rotation 2/3 keys ready, trying the next key")
    chat_line.note(call, "switch", "the last key did not answer - asking this one (try 2)",
                   {"identity": model, "key": "k2"})
    chat_line.note(call, "recovery", "answered on attempt 2", {"identity": model, "key": "k2", "seconds": 2.34})


class TestWhatIsSaid:
    def test_a_clean_reply_posts_nothing(self, line):
        ctx = attach(FakeCtx("tui"))
        call = make_call()
        chat_line.note(call, "sent", "3 of 3 key(s) ready", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "answered", "first word after 0.4s", {"identity": "gemini:m", "key": "k1", "seconds": 1.0})
        line.flush()
        assert ctx.posts == [] and ctx.edits == []

    def test_a_rotation_posts_once_then_edits_to_the_final_sentence(self, line):
        ctx = attach(FakeCtx("tui"))
        refused_then_answered(make_call())
        line.flush()
        assert len(ctx.posts) == 1
        assert ctx.posts[0].startswith("↻ m: trying another key")
        assert ctx.edits[-1] == "✓ answered by m in 2.3 s (2 keys tried)"
        assert all("k1" not in t and "k2" not in t for t in ctx.said), "fingerprints stay out of the sentence"

    def test_the_wait_says_the_shared_live_words(self, line):
        ctx = attach(FakeCtx("tui"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "wait", "every key resting", {"identity": "gemini:m", "seconds": 130})
        line.flush()
        assert ctx.posts == ["⏳ Every key of m is resting · next key in 2m 10s"]

    def test_a_reply_that_fails_ends_with_the_reason(self, line):
        ctx = attach(FakeCtx("tui"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "rotation", "x", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "surfaced", "every key was refused", {"identity": "gemini:m"})
        line.flush()
        assert ctx.said[-1] == "✕ no answer: every key was refused"

    def test_background_jobs_say_nothing(self, line):
        ctx = attach(FakeCtx("tui"))
        refused_then_answered(make_call(channel="secondary"))
        line.flush()
        assert ctx.said == []

    def test_the_setting_off_posts_nothing(self, line, monkeypatch):
        monkeypatch.setenv("KAME_LIVE_CHAT_LINE", "0")
        settings.forget()
        ctx = attach(FakeCtx("tui"))
        refused_then_answered(make_call())
        line.flush()
        assert ctx.said == []

    def test_the_setting_is_on_by_default_and_has_its_words(self, line):
        assert settings.is_on(settings.LIVE_CHAT_LINE) is True
        assert settings.LIVE_CHAT_LINE in settings.ALL_FLAGS
        assert settings.title(settings.LIVE_CHAT_LINE) and settings.explain(settings.LIVE_CHAT_LINE)


class TestSurfaces:
    def test_the_tui_keeps_one_handle_per_session(self, line):
        ctx = attach(FakeCtx("tui"))
        refused_then_answered(make_call("client:1"))
        line.flush()
        refused_then_answered(make_call("client:1"))
        line.flush()
        assert len(ctx.posts) == 1, ctx.posts
        assert len(ctx.edits) >= 3

    def test_another_session_gets_its_own_handle(self, line, monkeypatch):
        ctx = attach(FakeCtx("tui"))
        refused_then_answered(make_call("client:1"))
        line.flush()
        monkeypatch.setattr(chat_line, "_session_key", lambda: "s2")
        refused_then_answered(make_call("client:2"))
        line.flush()
        assert len(ctx.posts) == 2

    @pytest.mark.parametrize("surface", ["cli", "", "acp", "api_server"])
    def test_the_classic_cli_and_unknown_surfaces_say_nothing(self, line, surface):
        ctx = attach(FakeCtx(surface))
        refused_then_answered(make_call())
        line.flush()
        assert ctx.said == []

    def test_a_context_without_a_surface_says_nothing(self, line):
        class Old:
            def post_message(self, text):  # pragma: no cover - must not be reached
                raise AssertionError("posted")

        attach(Old())
        refused_then_answered(make_call())
        line.flush()

    def test_messaging_posts_nothing_for_a_plain_rotation(self, line):
        ctx = attach(FakeCtx("telegram"))
        refused_then_answered(make_call())
        line.flush()
        assert ctx.said == []

    def test_messaging_posts_nothing_for_a_short_wait(self, line):
        ctx = attach(FakeCtx("telegram"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "wait", "every key resting", {"identity": "gemini:m", "seconds": 20})
        chat_line.note(call, "recovery", "answered on attempt 2", {"identity": "gemini:m", "seconds": 21})
        line.flush()
        assert ctx.said == []

    def test_messaging_posts_one_message_per_reply_for_a_long_wait_and_edits_it(self, line):
        ctx = attach(FakeCtx("telegram"))
        for name in ("client:1", "client:2"):
            call = make_call(name)
            chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
            chat_line.note(call, "wait", "every key resting", {"identity": "gemini:m", "seconds": 70})
            chat_line.flush()
            chat_line.note_status(name, "⏳ waiting on a key to come back — API Rotation 0/3 keys ready, next key in 00:44")
            chat_line.note(call, "recovery", "answered on attempt 2", {"identity": "gemini:m", "key": "k2", "seconds": 72})
            line.flush()
        assert len(ctx.posts) == 2, ctx.posts
        assert ctx.posts[0].startswith("⏳ Every key of m is resting")
        assert ctx.edits[-1] == "✓ KAME: answered after 72 s"

    def test_messaging_ignores_status_ticks_and_edits_only_on_events(self, line, monkeypatch):
        monkeypatch.setattr(chat_line, "MESSAGING_EDIT_EVERY_S", 5.0)
        ctx = attach(FakeCtx("telegram"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "wait", "every key resting", {"identity": "gemini:m", "seconds": 70})
        line.flush()
        for i in range(10):
            chat_line.note_status(call.status_owner(), f"tick {i}")
        line.flush()
        assert len(ctx.posts) == 1 and ctx.edits == [], ctx.edits

    def test_messaging_edits_are_throttled_but_the_final_edit_is_sent_at_once(self, line, monkeypatch):
        monkeypatch.setattr(chat_line, "MESSAGING_EDIT_EVERY_S", 0.5)
        ctx = attach(FakeCtx("telegram"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "wait", "every key resting", {"identity": "gemini:m", "seconds": 70})
        line.flush()
        started = time.monotonic()
        chat_line.note(call, "wait", "again", {"identity": "gemini:m", "seconds": 40})   # throttled
        chat_line.note(call, "wait", "again", {"identity": "gemini:m", "seconds": 30})   # replaces it
        time.sleep(0.1)
        assert ctx.edits == []
        chat_line.note(call, "recovery", "answered", {"identity": "gemini:m", "key": "k2", "seconds": 72})
        line.flush()
        assert time.monotonic() - started < 0.45, "the final edit must not wait for the throttle"
        assert len(ctx.edits) == 1 and ctx.edits[0].startswith("✓ KAME: answered after")

    def test_a_twenty_second_wait_posts_nothing_and_a_seventy_second_wait_posts_once_and_ends_short(self, line):
        # 1.9.0.6 (decisions/0037 C20)
        ctx = attach(FakeCtx("telegram"))
        short, long_ = make_call("client:1"), make_call("client:2")
        for call, wait, took in ((short, 20, 21), (long_, 70, 72)):
            chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
            chat_line.note(call, "wait", "every key resting", {"identity": "gemini:m", "seconds": wait})
            chat_line.flush()
            chat_line.note(call, "recovery", "answered", {"identity": "gemini:m", "key": "k2", "seconds": took})
            line.flush()
        assert len(ctx.posts) == 1, ctx.posts
        assert ctx.edits == ["✓ KAME: answered after 72 s"], ctx.edits
        assert "keys tried" not in ctx.edits[-1]

    def test_a_failure_on_a_messaging_surface_keeps_its_text(self, line):
        ctx = attach(FakeCtx("telegram"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "wait", "every key resting", {"identity": "gemini:m", "seconds": 70})
        chat_line.flush()
        chat_line.note(call, "gave_up", "every key was refused", {"identity": "gemini:m"})
        line.flush()
        assert ctx.edits[-1] == "✕ no answer: every key was refused"

    def test_messaging_throttle_is_at_least_twenty_seconds_by_default(self, line):
        assert chat_line.MESSAGING_EDIT_EVERY_S >= 20.0 and chat_line.EDIT_EVERY_S <= 1.5

    def test_messaging_posts_when_another_model_answers(self, line):
        ctx = attach(FakeCtx("discord"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "handover", "every key of m is resting; Hermes answers with the next model",
                       {"identity": "gemini:m"})
        line.flush()
        assert len(ctx.posts) == 1 and ctx.posts[0].startswith("↪ ")

    def test_messaging_posts_for_a_model_race(self, line):
        ctx = attach(FakeCtx("slack"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "switch", "no first word after 4.0s - n (your next model) races it",
                       {"identity": "gemini:m", "key": "k2"})
        line.flush()
        assert len(ctx.posts) == 1


class TestDesktop:
    def test_no_live_posts_on_desktop(self, line):
        ctx = attach(FakeCtx("desktop"))
        refused_then_answered(make_call())
        line.flush()
        assert ctx.said == []

    def test_one_notice_when_the_panel_never_read_the_state(self, line, monkeypatch):
        monkeypatch.setattr(chat_line, "desktop_read_age", lambda: None)
        ctx = attach(FakeCtx("desktop"))
        for _ in range(3):
            call = make_call()
            chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
            chat_line.note(call, "answered", "", {"identity": "gemini:m", "seconds": 1})
        line.flush()
        assert ctx.posts == ["Turn on API Rotation in Capabilities -> Plugins to see its status bar and panel."]

    def test_a_stale_read_counts_as_off(self, line, monkeypatch):
        monkeypatch.setattr(chat_line, "desktop_read_age", lambda: 3600.0)
        ctx = attach(FakeCtx("desktop"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        line.flush()
        assert len(ctx.posts) == 1

    def test_a_recent_read_posts_no_notice(self, line):
        ctx = attach(FakeCtx("desktop"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        line.flush()
        assert ctx.said == []

    def test_the_notice_is_once_per_session(self, line, monkeypatch):
        monkeypatch.setattr(chat_line, "desktop_read_age", lambda: None)
        ctx = attach(FakeCtx("desktop"))
        chat_line.note(make_call("client:1"), "sent", "", {"identity": "gemini:m", "key": "k1"})
        monkeypatch.setattr(chat_line, "_session_key", lambda: "s2")
        chat_line.note(make_call("client:2"), "sent", "", {"identity": "gemini:m", "key": "k1"})
        line.flush()
        assert len(ctx.posts) == 2

    def test_the_package_notices_are_posted_once_per_session_on_desktop_and_tui(self, line, monkeypatch):
        text = "hermes-kame-provider is no longer needed: remove it with `hermes plugins remove hermes-kame-provider`"
        monkeypatch.setattr(h1905.sub("provider_profiles"), "notices", lambda: [text])
        for surface in ("desktop", "tui"):
            chat_line.reset()
            ctx = attach(FakeCtx(surface))
            for name in ("client:1", "client:2"):
                chat_line.note(make_call(name), "sent", "", {"identity": "gemini:m", "key": "k1"})
            line.flush()
            assert ctx.posts == [text], (surface, ctx.posts)

    def test_the_stamp_file_is_read_as_an_age(self, line, monkeypatch, tmp_path):
        monkeypatch.undo()  # the real reader, not the fixture's stub
        state = h1905.sub("state")
        monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
        assert chat_line.desktop_read_age() is None
        (tmp_path / chat_line.DESKTOP_READ_FILE).write_text(repr(time.time() - 30), encoding="utf-8")
        assert 25 < chat_line.desktop_read_age() < 60


class TestThreading:
    def test_edits_are_throttled_and_the_last_text_wins(self, line, monkeypatch):
        monkeypatch.setattr(chat_line, "EDIT_EVERY_S", 0.4)
        ctx = attach(FakeCtx("tui"))
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "rotation", "x", {"identity": "gemini:m", "key": "k1"})
        line.flush()
        for i in range(12):
            chat_line.note_status(call.status_owner(), f"status {i}")
        line.flush()
        assert len(ctx.posts) == 1
        assert ctx.edits[-1] == "status 11"
        assert len(ctx.edits) <= 2, ctx.edits   # 12 updates inside one window collapse into the last one

    def test_two_edits_are_never_closer_than_the_throttle(self, line, monkeypatch):
        monkeypatch.setattr(chat_line, "EDIT_EVERY_S", 0.3)
        stamps = []
        ctx = attach(FakeCtx("tui"))
        original = Handle.edit

        def timed(self, text):
            stamps.append(time.monotonic())
            return original(self, text)

        monkeypatch.setattr(Handle, "edit", timed)
        call = make_call()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "rotation", "x", {"identity": "gemini:m", "key": "k1"})
        line.flush()
        for i in range(5):
            chat_line.note_status(call.status_owner(), f"status {i}")
            time.sleep(0.12)
        chat_line.note(call, "recovery", "", {"identity": "gemini:m", "seconds": 1})
        line.flush()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert gaps and min(gaps) >= 0.28, gaps
        assert ctx.edits[-1].startswith("✓ answered by m")

    def test_a_blocking_post_does_not_delay_the_reply_thread(self, line):
        gate = threading.Event()
        ctx = attach(FakeCtx("tui", block=gate))
        call = make_call()
        started = time.monotonic()
        chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
        chat_line.note(call, "rotation", "x", {"identity": "gemini:m", "key": "k1"})
        for i in range(20):
            chat_line.note_status(call.status_owner(), f"status {i}")
        chat_line.note(call, "recovery", "", {"identity": "gemini:m", "seconds": 1})
        assert time.monotonic() - started < 0.05
        assert ctx.posts == []        # still waiting for the gateway, on the worker
        gate.set()
        assert line.flush(5)
        # everything said while the gateway was slow collapses into one post carrying the last sentence
        assert len(ctx.posts) == 1 and ctx.posts[0].startswith("✓ answered by m") and ctx.edits == []

    def test_the_post_runs_in_the_context_of_the_reply_thread_on_the_worker(self, line):
        ctx = attach(FakeCtx("tui"))
        seen = {}

        def reply():
            WHO.set("the-turn")
            call = make_call()
            chat_line.note(call, "sent", "", {"identity": "gemini:m", "key": "k1"})
            chat_line.note(call, "rotation", "x", {"identity": "gemini:m", "key": "k1"})
            seen["thread"] = threading.current_thread().name

        t = threading.Thread(target=reply, name="reply-thread")
        t.start()
        t.join()
        line.flush()
        assert ctx.who == ["the-turn"]
        assert ctx.post_threads == ["kame-chat-line"] and seen["thread"] == "reply-thread"

    def test_a_refused_post_ends_the_line_without_a_retry(self, line):
        ctx = attach(FakeCtx("tui", ok=False))
        refused_then_answered(make_call())
        line.flush()
        refused_then_answered(make_call("client:2"))
        line.flush()
        assert len(ctx.posts) == 1 and ctx.edits == []

    def test_a_refused_edit_ends_the_line_without_a_retry(self, line):
        ctx = attach(FakeCtx("tui"))
        ctx.edit_ok = False
        refused_then_answered(make_call())
        line.flush()
        assert len(ctx.posts) == 1 and len(ctx.edits) == 1

    def test_a_post_that_raises_costs_nothing(self, line):
        class Boom(FakeCtx):
            def post_message(self, text):
                raise RuntimeError("gateway down")

        attach(Boom("tui"))
        refused_then_answered(make_call())
        assert line.flush()


from tests.test_1_8_2_5_stream_race import fresh  # noqa: E402,F401  (a fixture: a transport on a temp home)


@pytest.fixture()
def real(fresh, monkeypatch):
    """The chat line of the package the race tests load (the transport calls THAT module)."""
    cl = R.transport.chat_line
    monkeypatch.setattr(cl, "EDIT_EVERY_S", 0.05)
    monkeypatch.setattr(cl, "_session_key", lambda: "s1")
    cl.reset()
    yield cl
    cl.flush(2)
    cl.detach()


class TestTheRealTransport:
    """The events come from ``transport._emit`` and the spinner's own lines."""

    def test_a_refused_key_is_said_in_the_chat_and_the_answer_closes_it(self, fresh, real):
        ctx = FakeCtx("tui")
        real.attach(ctx)
        pool = R.Pool({R.KEYS[0]: [[R.per_minute()]], R.KEYS[1]: [[R.chunk("next", finish="stop")]]})
        out = list(fresh.stream(R.FakeCall(pool)))
        real.flush(5)
        assert R.shown(out) == "next"
        assert len(ctx.posts) == 1, ctx.said
        assert ctx.said[-1].startswith("✓ answered by gemini-race"), ctx.said
        assert "(2 keys tried)" in ctx.said[-1]

    def test_a_clean_reply_posts_nothing_through_the_real_transport(self, fresh, real):
        ctx = FakeCtx("tui")
        real.attach(ctx)
        pool = R.Pool({R.KEYS[0]: [[R.chunk("hi", finish="stop")]]})
        assert R.shown(list(fresh.stream(R.FakeCall(pool)))) == "hi"
        real.flush(5)
        assert ctx.said == []

    def test_the_emit_and_the_spinner_call_the_adapter(self, real, monkeypatch):
        seen = []
        monkeypatch.setattr(real, "note", lambda call, kind, text="", fields=None: seen.append(("note", kind, text, dict(fields or {}))))
        monkeypatch.setattr(real, "note_status", lambda owner, text: seen.append(("status", owner, text)))
        R.transport._emit(make_call(), "switch", identity="g:m", key="abc", reason="because")
        R.transport._Spinner.reset()
        R.transport._Spinner.update("client:1", "hello")
        assert ("note", "switch", "because", {"identity": "g:m", "key": "abc", "reason": "because"}) in seen
        assert ("status", "client:1", "hello") in seen
