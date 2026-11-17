"""1.9.0.2 (H1) - Stop and host aborts reach KAME's key loop.

Live 2026-11-15 (audit/2026-11-15-pre-1902-bug-hunt.md, F1): the owner pressed Stop at 10:02:31 and
KAME's call kept rotating until 10:07:18 - 282 attempts after the turn had ended. Hermes cancels an
in-flight request from a stranger thread by calling ``type(client).cancel()`` when the client has one
(``agent/client_lifecycle.py::_abort_request_slot_client``), else by shutting the client's own sockets.
KAME's clients had no ``cancel`` and their sockets live in per-key inner clients, so the host found 0
sockets and the loop never heard the stop. The same seam serves "background review superseded" and
"cache_evict_in_flight".

Pinned here: the clients expose ``cancel``; it only raises a flag (never closes from the stranger thread)
and never raises; a clone made by ``with_options`` hears a cancel of its parent and not the other way
round; and the transport loop, once cancelled, ends within about a second and sends nothing more -
rotating, waiting for a resting pool, or racing.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
import importlib  # noqa: E402

facade = importlib.import_module(f"{R.PACKAGE}.facade")

fresh = R.fresh          # the race tests' fixture: a fresh transport and carousel, HERMES_HOME in tmp
KEYS = R.KEYS
BARE = "Resource has been exhausted (e.g. check quota)."


def bare_429():
    return R.Refusal(429, BARE)


class _Bare(facade._KameMixin):
    """The mixin with only the fields ``cancel`` and the chain read - no host, no keys."""

    def __init__(self):
        self._kame_cancelled = threading.Event()
        self._kame_inners = {}
        self._kame_lock = threading.Lock()


def _messages_parent():
    primary = SimpleNamespace()
    primary.with_options = lambda **o: primary
    parent = object.__new__(facade.KameMessagesClient)
    parent.__dict__.update(_kame_options={}, _kame_primary=primary, _kame_inners={},
                           _kame_lock=threading.Lock(), _kame_cancelled=threading.Event())
    return parent


# --- the seam ---------------------------------------------------------------------------------------


class TestTheClientsAnswerTheHostsCancel:
    def test_the_mixin_has_a_callable_cancel_so_the_host_takes_that_branch(self):
        assert callable(getattr(facade._KameMixin, "cancel", None))
        assert callable(getattr(facade.KameMessagesClient, "cancel", None))

    def test_cancel_from_another_thread_raises_the_flag_and_returns_at_once(self):
        client = _Bare()
        done = []
        th = threading.Thread(target=lambda: (type(client).cancel(client), done.append(time.monotonic())))
        started = time.monotonic()
        th.start()
        th.join(2.0)
        assert done and done[0] - started < 0.5
        assert client._kame_is_cancelled()

    def test_cancel_never_closes_an_inner_client_from_the_stranger_thread(self):
        client = _Bare()
        closed = []
        client._kame_inners["k"] = SimpleNamespace(close=lambda: closed.append(1))
        client.cancel()
        assert closed == []

    def test_cancel_is_idempotent_and_safe_on_an_idle_closed_or_half_built_client(self):
        client = _Bare()
        client.cancel()
        client.cancel()
        client._kame_close()
        client.cancel()
        half = object.__new__(_Bare)          # setup never ran: no flag at all
        half.cancel()                          # must not raise
        assert client._kame_is_cancelled()


class TestAClonedClientHearsItsParent:
    def test_a_cancel_of_the_parent_reaches_a_clone_made_by_with_options(self):
        parent = _messages_parent()
        clone = parent.with_options(timeout=5)
        parent.cancel()
        assert clone._kame_is_cancelled()
        assert facade._FacadeCall.cancelled(SimpleNamespace(_client=clone)) is True

    def test_a_cancel_of_a_clone_leaves_the_parent_usable(self):
        parent = _messages_parent()
        clone = parent.with_options(timeout=5)
        clone.cancel()
        assert clone._kame_is_cancelled()
        assert not parent._kame_is_cancelled()
        assert not parent.with_options(timeout=7)._kame_is_cancelled()

    def test_closing_the_parent_does_not_cancel_a_clone(self):
        # A close is the host releasing the parent; only an explicit cancel is a stop for its clones.
        parent = _messages_parent()
        clone = parent.with_options(timeout=5)
        parent._kame_close()
        assert not clone._kame_is_cancelled()


# --- the loop ---------------------------------------------------------------------------------------


class RefusingCall(R.FakeCall):
    """Every key answers a bare 429, forever. Records when each request starts."""

    def __init__(self, keys=KEYS, stream=True):
        super().__init__(R.Pool({}), keys=keys)
        self.stop = threading.Event()
        self.starts = []
        self.lock = threading.Lock()
        self.stream_wire = stream

    def client_for(self, key, attempt):
        def create(**request):
            with self.lock:
                self.starts.append(time.monotonic())
            raise bare_429()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return self.stop.is_set()


def _cancel_after(call, seconds):
    stamp = []

    def fire():
        time.sleep(seconds)
        stamp.append(time.monotonic())
        call.stop.set()

    threading.Thread(target=fire, daemon=True).start()
    return stamp


def _drive(t, call, stream=True, limit=8.0):
    """Run the call on a worker thread; return (ended_at, raised) or fail if it outlives ``limit``."""
    out = {}

    def work():
        try:
            if stream:
                for _ in t.stream(call):
                    pass
            else:
                t.complete(call)
        except BaseException as exc:  # the cancel exits raise the last failure or a cancel error
            out["raised"] = exc
        out["ended"] = time.monotonic()

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(limit)
    assert "ended" in out, f"the call was still running {limit}s after it started"
    return out["ended"], out.get("raised")


class TestAStoppedLoopStopsSending:
    @pytest.mark.parametrize("stream", [True, False])
    def test_rotating_on_bare_429s_ends_within_a_second_and_sends_nothing_after(self, fresh, stream):
        call = RefusingCall(stream=stream)
        stamp = _cancel_after(call, 0.6)
        ended, raised = _drive(fresh, call, stream=stream)
        assert stamp, "the cancel never fired"
        assert ended - stamp[0] < 1.5
        assert [s for s in call.starts if s > stamp[0] + 0.05] == []
        assert raised is not None          # nothing was answered: the last failure or a cancel error

    def test_a_wait_for_a_resting_pool_ends_within_a_second_with_no_request(self, fresh):
        call = RefusingCall()
        for key in KEYS:
            fresh.engine.mark(R.IDENTITY, key, False, 3600.0, "daily", stated=True)
        stamp = _cancel_after(call, 0.6)
        ended, _ = _drive(fresh, call)
        assert ended - stamp[0] < 1.5
        assert call.starts == []

    def test_the_cancel_benches_no_key(self, fresh, monkeypatch):
        call = RefusingCall()
        marks = []
        original = fresh.engine.mark
        monkeypatch.setattr(fresh.engine, "mark",
                            lambda *a, **k: (marks.append(time.monotonic()), original(*a, **k))[1])
        stamp = _cancel_after(call, 0.6)
        _drive(fresh, call)
        # every mark belongs to a refusal that started before the stop; none is the stop itself
        late_requests = [s for s in call.starts if s > stamp[0] + 0.05]
        assert late_requests == []
        assert len([m for m in marks if m > stamp[0] + 0.05]) <= 1   # at most the attempt in flight


class TestARaceStopsEveryLeg:
    def test_no_leg_opens_after_the_cancel(self, fresh):
        # Every key is silent for a long time: the hedge opens backups; a cancel must end them all.
        pool = R.Pool({k: [[30.0, R.chunk("late")]] for k in KEYS})
        call = R.FakeCall(pool)
        stop = threading.Event()
        call.cancelled = stop.is_set
        opened_at = []
        original_open = pool.open

        def open_and_stamp(key):
            opened_at.append(time.monotonic())
            return original_open(key)

        pool.open = open_and_stamp

        def fire():
            time.sleep(0.6)
            opened_at.append(("stop", time.monotonic()))
            stop.set()
            for _, s in list(pool.opened):     # the host would shut the sockets: here, close the streams
                s.close()

        threading.Thread(target=fire, daemon=True).start()
        started = time.monotonic()
        try:
            for _ in fresh.stream(call):
                pass
        except BaseException:
            pass
        assert time.monotonic() - started < 3.0
        stop_at = next(x[1] for x in opened_at if isinstance(x, tuple))
        assert [x for x in opened_at if not isinstance(x, tuple) and x > stop_at + 0.05] == []
