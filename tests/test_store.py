"""The ledger's storage: fast on the hot path, silent when the disk isn't.

``ctx.state`` takes a cross-process file lock and re-reads the whole document
on every access. The pool asks "which keys are available" before every API
call, so the ledger cannot be read that way. These tests pin the cache that
sits in between — how long it trusts itself, when it writes, and what it does
when storage refuses.

The rule underneath all of them: the ledger is an optimisation over cooldowns
the host already gets right. Losing it must cost precision, never a request.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_store_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
store_module = importlib.import_module(f"{PACKAGE}.store")
ledger_module = importlib.import_module(f"{PACKAGE}.core.ledger")

LedgerStore = store_module.LedgerStore
STATE_KEY = store_module.STATE_KEY
Ledger = ledger_module.Ledger

NOW = 1_000_000.0
HOUR = 3600.0
MODEL = "gemini-3.6-flash"


class FakeState:
    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}
        self.reads = 0
        self.writes = 0
        self.fail_read: Optional[Exception] = None
        self.fail_write: Optional[Exception] = None

    def get(self, key: str, default: Any = None) -> Any:
        self.reads += 1
        if self.fail_read is not None:
            raise self.fail_read
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.writes += 1
        if self.fail_write is not None:
            raise self.fail_write
        self.data[key] = value


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def a_ledger(reset_at: float = NOW + HOUR, *, credential_id: str = "k0") -> Ledger:
    ledger = Ledger()
    ledger.record(
        credential_id=credential_id,
        provider="gemini",
        model=MODEL,
        reset_at=reset_at,
        now=NOW,
    )
    return ledger


class TestNotHittingDiskOnEveryCall:
    def test_repeated_reads_inside_the_window_touch_storage_once(self):
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=15.0, clock=Clock())
        for _ in range(50):
            store.load()
        assert state.reads == 1

    def test_the_window_expires(self):
        clock = Clock()
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=15.0, clock=clock)
        store.load()
        clock.now += 16
        store.load()
        assert state.reads == 2

    def test_a_forced_read_ignores_the_window(self):
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=15.0, clock=Clock())
        store.load()
        store.load(force=True)
        assert state.reads == 2

    def test_a_save_refreshes_the_window(self):
        # A process that just wrote knows what the file says; re-reading it
        # would be a lock acquisition to learn nothing.
        clock = Clock()
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=15.0, clock=clock)
        store.save(a_ledger())
        before = state.reads
        store.load()
        assert state.reads == before


class TestRoundTrip:
    def test_what_is_saved_comes_back(self):
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=0.0, clock=Clock())
        store.save(a_ledger())
        assert store.load().benched_until("k0", MODEL, NOW) == NOW + HOUR

    def test_a_second_store_reads_what_the_first_wrote(self):
        state = FakeState()
        LedgerStore(state, ttl_seconds=0.0, clock=Clock()).save(a_ledger())
        other = LedgerStore(state, ttl_seconds=0.0, clock=Clock())
        assert other.load().benched_until("k0", MODEL, NOW) == NOW + HOUR

    def test_it_writes_under_its_own_key(self):
        state = FakeState()
        LedgerStore(state, clock=Clock()).save(a_ledger())
        assert list(state.data) == [STATE_KEY]

    def test_an_absent_key_reads_as_empty(self):
        assert len(LedgerStore(FakeState(), clock=Clock()).load()) == 0

    def test_expired_rows_are_dropped_before_writing(self):
        # The document should not accumulate benches nobody can act on.
        clock = Clock()
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=0.0, clock=clock)
        store.save(a_ledger())
        clock.now = NOW + 2 * HOUR
        store.save(store.load())
        assert state.data[STATE_KEY]["benches"] == []

    def test_clear_forgets_everything(self):
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=0.0, clock=Clock())
        store.save(a_ledger())
        store.clear()
        assert len(store.load()) == 0


class TestWhenStorageRefuses:
    def test_an_unreadable_store_yields_an_empty_ledger(self):
        state = FakeState()
        state.fail_read = OSError("locked")
        assert len(LedgerStore(state, clock=Clock()).load()) == 0

    def test_an_unreadable_store_keeps_what_this_session_learned(self):
        # Losing the file is not a reason to forget a bench recorded a second
        # ago; that would re-hammer a limit this process knows is spent.
        clock = Clock()
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=0.0, clock=clock)
        store.save(a_ledger())
        state.fail_read = OSError("locked")
        clock.now += 100
        assert store.load().benched_until("k0", MODEL, clock.now) == NOW + HOUR

    def test_a_failed_write_is_reported_not_raised(self):
        state = FakeState()
        state.fail_write = OSError("disk full")
        assert LedgerStore(state, clock=Clock()).save(a_ledger()) is False

    def test_a_failed_write_still_updates_the_session(self):
        state = FakeState()
        state.fail_write = OSError("disk full")
        store = LedgerStore(state, ttl_seconds=60.0, clock=Clock())
        store.save(a_ledger())
        assert store.load().benched_until("k0", MODEL, NOW) == NOW + HOUR

    def test_a_failed_write_is_not_undone_by_the_next_read(self):
        # Storage still holds the older copy, and re-reading it would resurrect
        # benches this session already released and lose probe attempts it
        # already spent. A store that cannot persist must degrade to
        # non-durable, never to wrong.
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=0.0, clock=Clock())
        store.save(a_ledger())
        state.fail_write = OSError("disk full")

        newer = store.load()
        newer.record(
            credential_id="k9", provider="gemini", model=MODEL,
            reset_at=NOW + 2 * HOUR, now=NOW,
        )
        store.save(newer)

        assert store.load().benched_until("k9", MODEL, NOW) == NOW + 2 * HOUR
        # Even a forced read: "ignore the cache" means "do not serve a stale
        # copy", and while a write is outstanding the cache is the fresh one.
        assert store.load(force=True).benched_until("k9", MODEL, NOW) == NOW + 2 * HOUR
        assert state.data[store._key]["benches"][0]["credential_id"] == "k0"

    def test_storage_is_trusted_again_once_a_write_lands(self):
        state = FakeState()
        store = LedgerStore(state, ttl_seconds=0.0, clock=Clock())
        state.fail_write = OSError("disk full")
        store.save(a_ledger())
        state.fail_write = None
        assert store.save(store.load()) is True

        state.data.clear()
        assert len(store.load()) == 0

    def test_a_quota_rejection_is_reported_not_raised(self):
        state = FakeState()
        state.fail_write = ValueError("Plugin state quota exceeded")
        assert LedgerStore(state, clock=Clock()).save(a_ledger()) is False

    def test_a_broken_store_is_logged_once_not_per_call(self, caplog):
        state = FakeState()
        state.fail_write = OSError("disk full")
        store = LedgerStore(state, ttl_seconds=0.0, clock=Clock())
        with caplog.at_level("WARNING"):
            for _ in range(20):
                store.save(a_ledger())
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    def test_no_state_at_all_behaves_like_an_empty_one(self):
        # ``ctx.state`` is absent when the plugin is loaded outside a profile
        # — the doctor, a packaging check. Nothing should raise.
        store = LedgerStore(None, clock=Clock())
        assert len(store.load()) == 0
        assert store.save(a_ledger()) is False
        assert store.load().benched_until("k0", MODEL, NOW) == NOW + HOUR


class TestGarbageInStorage:
    def test_a_non_dict_document_reads_as_empty(self):
        state = FakeState()
        state.data[STATE_KEY] = ["not", "a", "ledger"]
        assert len(LedgerStore(state, clock=Clock()).load()) == 0

    def test_an_unknown_schema_version_reads_as_empty(self):
        # A ledger written by a future KAME must not be half-understood: a
        # misread row un-benches the wrong key.
        state = FakeState()
        state.data[STATE_KEY] = {"version": 99, "benches": [{"credential_id": "k0"}]}
        assert len(LedgerStore(state, clock=Clock()).load()) == 0

    def test_a_hand_edited_row_is_dropped_not_defaulted(self):
        state = FakeState()
        state.data[STATE_KEY] = {
            "version": 1,
            "benches": [{"credential_id": "k0", "model": MODEL}],
        }
        assert len(LedgerStore(state, clock=Clock()).load()) == 0
