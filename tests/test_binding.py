"""The binding layer: what the plugin adds to a live credential pool.

These tests run against a stand-in pool rather than a Hermes install, so they
state the *contract* the wrappers rely on. That contract is checked against
the real thing separately by ``inspect_module`` at every start, and by the
sandbox script in ``tools/``. Splitting it this way is deliberate: the rules
below must stay runnable on a machine with no Hermes on it, and the shape
check must fail loudly on a machine where Hermes has moved.

The stand-in reproduces the parts of ``CredentialPool._available_entries``
that matter to a release decision — the empty-key guard, the DEAD skip, the
cooldown skip, and the clearing write that happens when a cooldown has
elapsed. That last one is the reason the plugin wraps this method instead of
the smaller ``_exhausted_until``: any answer of "this key is free" flows into
a persisted write that erases the bench, and erasing the bench erases the
evidence that KAME wrote it.
"""

from __future__ import annotations

import importlib
import importlib.util
from contextlib import contextmanager
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_binding_under_test"


def _load_package():
    """Import the plugin as a package, the way the Hermes loader does.

    The directory name is hyphenated, so it cannot be imported by name. The
    submodules under test use relative imports, so loading them individually
    by path would not work either — the package has to exist first.
    """
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
runtime = importlib.import_module(f"{PACKAGE}.runtime")
store_module = importlib.import_module(f"{PACKAGE}.store")
pool_binding = importlib.import_module(f"{PACKAGE}.pool_binding")

journal_module = importlib.import_module(f"{PACKAGE}.core.journal")
probe_module = importlib.import_module(f"{PACKAGE}.core.probe")
multikey = importlib.import_module(f"{PACKAGE}.core.multikey")
dispersion_module = importlib.import_module(f"{PACKAGE}.core.dispersion")
DAY = 86400.0

LedgerStore = store_module.LedgerStore
JournalStore = store_module.JournalStore
PoolBinding = pool_binding.PoolBinding
Incompatible = pool_binding.Incompatible

NOW = 1_000_000.0
HOUR = 3600.0
MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
STATUS_DEAD = "dead"
AUTH_TYPE_API_KEY = "api_key"
STRATEGY_FILL_FIRST = "fill_first"


# --------------------------------------------------------------------------
# A pool that behaves like the host's, in the ways a release decision touches
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeCredential:
    id: str
    label: str = ""
    auth_type: str = AUTH_TYPE_API_KEY
    runtime_api_key: str = "sk-test"
    # Present because the host's class has them and the wrappers now read
    # them. ``source`` is where a derived part is marked, ``access_token`` is
    # where the host keeps the key that ``runtime_api_key`` computes from, and
    # ``provider`` is how the one provider whose runtime key is not its access
    # token is recognised.
    provider: str = "gemini"
    source: str = "manual"
    access_token: str = ""
    request_count: int = 0
    last_status: str = STATUS_OK
    last_status_at: Optional[float] = None
    last_error_code: Optional[int] = None
    last_error_reason: Optional[str] = None
    last_error_message: Optional[str] = None
    last_error_reset_at: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def three_healthy_keys() -> List["FakeCredential"]:
    """Three credentials, a key each.

    The key matters as much as the id: the load counter names a credential by
    the key it holds, because that is what the provider meters. Three rows
    sharing one key string would be three rows holding *one* credential —
    a real arrangement, and a different one, asserted on its own below.
    """
    return [
        FakeCredential(id=f"k{i}", label=f"key-{i}", runtime_api_key=f"sk-test-{i}")
        for i in range(3)
    ]


class FakePool:
    """The parts of ``CredentialPool`` the wrappers interact with."""

    def __init__(self, provider: str, entries: List[FakeCredential], *, now: float = NOW):
        self.provider = provider
        self._entries = list(entries)
        self.now = now
        # The host's default, and the only one KAME re-orders. Named here
        # because a stand-in without it would silently exercise the
        # "leave the order alone" branch for every test in this file.
        self._strategy = STRATEGY_FILL_FIRST
        self.cleared: List[str] = []
        self.needs_refresh: set = set()
        self._lock = threading.RLock()
        # Every id the pool has ever written, one list per write. The host
        # writes to auth.json; what matters for a test is which entries were
        # in the list when it did.
        self.persisted: List[List[str]] = []

    def _persist(self, *, removed_ids: Optional[List[str]] = None) -> None:
        self.persisted.append([entry.id for entry in self._entries])

    def entries(self) -> List[FakeCredential]:
        return list(self._entries)

    def by_id(self, credential_id: str) -> FakeCredential:
        return next(e for e in self._entries if e.id == credential_id)

    def _entry_needs_refresh(self, entry: FakeCredential) -> bool:
        return entry.id in self.needs_refresh

    def _replace(self, old: FakeCredential, new: FakeCredential) -> None:
        self._entries = [new if e.id == old.id else e for e in self._entries]

    def _mark_exhausted(
        self,
        entry: FakeCredential,
        status_code=None,
        error_context=None,
        *,
        persist: bool = True,
        failure_reason: Optional[str] = None,
    ) -> FakeCredential:
        context = error_context or {}
        updated = replace(
            entry,
            last_status=STATUS_DEAD if status_code == 401 else STATUS_EXHAUSTED,
            last_status_at=self.now,
            last_error_code=status_code,
            last_error_reason=context.get("reason"),
            last_error_reset_at=context.get("reset_at"),
        )
        self._replace(entry, updated)
        return updated

    def _available_entries(
        self, *, clear_expired: bool = False, refresh: bool = False
    ) -> Tuple[List[FakeCredential], List[tuple]]:
        available: List[FakeCredential] = []
        for entry in list(self._entries):
            if entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
                continue
            if entry.last_status == STATUS_DEAD:
                continue
            if entry.last_status == STATUS_EXHAUSTED:
                until = entry.last_error_reset_at
                if until is not None and self.now < until:
                    continue
                if clear_expired:
                    cleared = replace(
                        entry,
                        last_status=STATUS_OK,
                        last_status_at=None,
                        last_error_code=None,
                        last_error_reason=None,
                        last_error_reset_at=None,
                    )
                    self._replace(entry, cleared)
                    self.cleared.append(entry.id)
                    entry = cleared
            if refresh and self._entry_needs_refresh(entry):
                continue
            available.append(entry)
        return available, []

    def _select_unlocked(self, *, refresh: bool = True):
        """The host's selection, reduced to the part the plugin watches.

        Only the return shape matters here — the wrapper reads the chosen
        entry out of it and changes nothing — but the ``clear_expired=True``
        is faithful and load-bearing: it is what makes selection the path
        where a lapsed bench gets erased.
        """
        available, pending = self._available_entries(clear_expired=True, refresh=refresh)
        if not available:
            return None, pending
        return available[0], pending


class FakeModule:
    """The ``agent.credential_pool`` namespace, reduced to what is inspected."""

    CredentialPool = FakePool
    PooledCredential = FakeCredential
    STATUS_EXHAUSTED = STATUS_EXHAUSTED
    STATUS_DEAD = STATUS_DEAD
    AUTH_TYPE_API_KEY = AUTH_TYPE_API_KEY
    STATUS_OK = STATUS_OK
    STRATEGY_FILL_FIRST = STRATEGY_FILL_FIRST


class FakeState:
    """``ctx.state``, in memory, with the failure modes that matter."""

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}
        self.reads = 0
        self.writes = 0
        self.fail_read = False
        self.fail_write: Optional[Exception] = None

    def get(self, key: str, default: Any = None) -> Any:
        self.reads += 1
        if self.fail_read:
            raise OSError("state unreadable")
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.writes += 1
        if self.fail_write is not None:
            raise self.fail_write
        self.data[key] = value


@pytest.fixture(autouse=True)
def _clean_context():
    _reset_runtime()
    yield
    _reset_runtime()


def _reset_runtime() -> None:
    runtime.forget_call()
    runtime.forget_judgement()
    # These two are module-level dicts rather than ContextVars, so without an
    # explicit reset one test's selection would answer another test's question.
    runtime.forget_selections()
    runtime.forget_probes()


def _fresh_module():
    module = type("Module", (), dict(FakeModule.__dict__))
    module.CredentialPool = type("Pool", (FakePool,), {})
    return module


@pytest.fixture
def bound():
    """A pool with three healthy keys and the binding installed on it.

    No journal: this is the v0.0.5 arrangement, and every rule below must
    hold with the observation half absent.
    """
    module = _fresh_module()
    state = FakeState()
    binding = PoolBinding(LedgerStore(state, ttl_seconds=0.0), clock=lambda: NOW)
    assert binding.install(module) is True
    pool = module.CredentialPool(
        "gemini",
        three_healthy_keys(),
    )
    yield binding, pool, state
    binding.uninstall()


class Clock:
    """A hand-wound clock, so a test can state when things happened."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def journaling():
    """The same pool, with the journal wired in and a clock a test can move."""
    module = _fresh_module()
    state = FakeState()
    clock = Clock()
    binding = PoolBinding(
        LedgerStore(state, ttl_seconds=0.0, clock=clock),
        journal=JournalStore(state, ttl_seconds=0.0, clock=clock),
        clock=clock,
    )
    assert binding.install(module) is True
    pool = module.CredentialPool(
        "gemini",
        three_healthy_keys(),
    )
    yield binding, pool, state, clock
    binding.uninstall()


def journal_of(binding):
    return binding._journal.load(force=True)


def ids(entries) -> List[str]:
    return [e.id for e in entries]


def available(pool, **kwargs) -> List[str]:
    entries, _pending = pool._available_entries(**kwargs)
    return ids(entries)


# --------------------------------------------------------------------------


class TestRefusingToInstall:
    """A wrapper that does not recognise its target must not attach."""

    def test_installs_against_the_expected_shape(self):
        binding = PoolBinding(LedgerStore(FakeState()))
        module = type("Module", (), dict(FakeModule.__dict__))
        module.CredentialPool = type("Pool", (FakePool,), {})
        assert binding.install(module) is True
        assert binding.reason == "active"
        binding.uninstall()

    def test_a_missing_method_refuses_rather_than_raises(self):
        class Stripped(FakePool):
            _available_entries = None

        module = type("Module", (), dict(FakeModule.__dict__))
        module.CredentialPool = Stripped
        binding = PoolBinding(LedgerStore(FakeState()))
        assert binding.install(module) is False
        assert "_available_entries" in binding.reason

    def test_a_renamed_parameter_refuses(self):
        # The wrapper forwards by keyword. A pool whose method took the flag
        # positionally, or under another name, would silently lose it.
        class Renamed(FakePool):
            def _available_entries(self, *, clear_stale=False, refresh=False):
                return [], []

        module = type("Module", (), dict(FakeModule.__dict__))
        module.CredentialPool = Renamed
        binding = PoolBinding(LedgerStore(FakeState()))
        assert binding.install(module) is False
        assert "clear_expired" in binding.reason

    def test_a_missing_entry_field_refuses(self):
        @dataclass(frozen=True)
        class Thin:
            id: str

        module = type("Module", (), dict(FakeModule.__dict__))
        module.CredentialPool = type("Pool", (FakePool,), {})
        module.PooledCredential = Thin
        binding = PoolBinding(LedgerStore(FakeState()))
        assert binding.install(module) is False
        assert "last_error_reset_at" in binding.reason

    def test_a_missing_status_constant_refuses(self):
        module = type("Module", (), dict(FakeModule.__dict__))
        module.CredentialPool = type("Pool", (FakePool,), {})
        del module.STATUS_DEAD
        binding = PoolBinding(LedgerStore(FakeState()))
        assert binding.install(module) is False

    def test_refusing_leaves_the_host_untouched(self):
        class Stripped(FakePool):
            _available_entries = None

        original = Stripped._mark_exhausted
        module = type("Module", (), dict(FakeModule.__dict__))
        module.CredentialPool = Stripped
        PoolBinding(LedgerStore(FakeState())).install(module)
        assert Stripped._mark_exhausted is original

    def test_a_second_binding_does_not_stack(self, bound):
        # Two wrappers on one method double-count every bench and every
        # release. A reload that did not tear down must be a no-op, not a
        # second layer.
        binding, _pool, _state = bound
        second = PoolBinding(LedgerStore(FakeState()))
        assert second.install(binding._module) is False
        assert "already wrapped" in second.reason

    def test_uninstall_restores_the_originals(self):
        module = type("Module", (), dict(FakeModule.__dict__))
        module.CredentialPool = type("Pool", (FakePool,), {})
        before = (module.CredentialPool._mark_exhausted, module.CredentialPool._available_entries)
        binding = PoolBinding(LedgerStore(FakeState()))
        binding.install(module)
        binding.uninstall()
        after = (module.CredentialPool._mark_exhausted, module.CredentialPool._available_entries)
        assert after == before

    def test_uninstall_is_safe_when_never_installed(self):
        PoolBinding(LedgerStore(FakeState())).uninstall()


class TestRecordingWhatSpentTheQuota:
    def test_a_bench_is_filed_against_the_model_in_flight(self, bound):
        binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(
            pool.by_id("k0"), 429, {"reason": "rate_limit", "reset_at": NOW + HOUR}
        )
        ledger = binding._store.load(force=True)
        assert ledger.benched_until("k0", MAIN, NOW) == NOW + HOUR
        assert ledger.benched_until("k0", AUX, NOW) is None

    def test_the_recorded_deadline_is_the_one_the_host_stored(self, bound):
        # Ownership is proven later by comparing the two. If KAME filed the
        # number it *asked* for rather than the one the host *kept*, every
        # release would fail its own fingerprint check and the feature would
        # be silently inert.
        binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        updated = pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        ledger = binding._store.load(force=True)
        assert ledger.find("k0", MAIN).reset_at == updated.last_error_reset_at

    def test_nothing_is_recorded_without_a_model(self, bound):
        binding, pool, _state = bound
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        assert len(binding._store.load(force=True)) == 0

    def test_nothing_is_recorded_for_another_providers_call(self, bound):
        binding, pool, _state = bound
        runtime.note_call("openai", "gpt-5")
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        assert len(binding._store.load(force=True)) == 0

    def test_a_default_ttl_bench_is_not_claimed(self, bound):
        # No reset_at means the host sized this one itself. Claiming it would
        # let KAME release a bench it never reasoned about.
        binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reason": "rate_limit"})
        assert len(binding._store.load(force=True)) == 0

    def test_a_dead_credential_is_not_recorded(self, bound):
        # A revoked key is broken for every model; a per-model note would
        # invite releasing it.
        binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 401, {"reset_at": NOW + HOUR})
        assert len(binding._store.load(force=True)) == 0

    def test_the_host_result_is_returned_unchanged(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        updated = pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        assert updated.last_status == STATUS_EXHAUSTED
        assert updated.last_error_reset_at == NOW + HOUR

    def test_a_broken_store_does_not_break_the_bench(self, bound):
        binding, pool, state = bound
        state.fail_write = OSError("disk full")
        runtime.note_call("gemini", MAIN)
        updated = pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        assert updated.last_error_reset_at == NOW + HOUR
        assert pool.by_id("k0").last_status == STATUS_EXHAUSTED


class TestTheRegressionThisFixes:
    """The case that made per-model memory mandatory rather than optional.

    A daily cap sized correctly at 24 hours benches the key provider-wide.
    The auxiliary model, which spent nothing, then loses that key for a full
    day — where the host's own inaccurate one-hour default would have given
    it back in an hour. This is the whole reason the binding exists.
    """

    def test_a_key_spent_on_one_model_still_serves_another(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + 24 * HOUR})

        assert available(pool) == ["k1", "k2"]
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0", "k1", "k2"]

    def test_the_whole_pool_can_be_spent_on_one_model_and_free_on_another(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        for key in ("k0", "k1", "k2"):
            pool._mark_exhausted(pool.by_id(key), 429, {"reset_at": NOW + 24 * HOUR})

        assert available(pool) == []
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0", "k1", "k2"]

    def test_coming_back_to_the_spent_model_finds_it_still_spent(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + 24 * HOUR})
        runtime.note_call("gemini", AUX)
        available(pool, clear_expired=True)

        runtime.note_call("gemini", MAIN)
        assert available(pool) == ["k1", "k2"]

    def test_the_release_does_not_erase_the_bench(self, bound):
        # The host clears a cooldown it considers elapsed, and persists that.
        # If a release ran through the host's own "is it expired" branch, the
        # bench — and with it the proof of ownership — would be wiped on the
        # first auxiliary call.
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + 24 * HOUR})

        runtime.note_call("gemini", AUX)
        available(pool, clear_expired=True)

        assert pool.cleared == []
        assert pool.by_id("k0").last_status == STATUS_EXHAUSTED
        assert pool.by_id("k0").last_error_reset_at == NOW + 24 * HOUR

    def test_priority_order_survives_a_release(self, bound):
        # The pool picks available[0] under the default strategy, so a
        # released key must land back in its own slot, not at the end.
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k1"), 429, {"reset_at": NOW + HOUR})
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0", "k1", "k2"]


class TestNotTouchingWhatIsNotOurs:
    def test_a_bench_kame_did_not_write_is_never_released(self, bound):
        # Host-written benches have a deadline no ledger row matches. Releasing
        # one would resurrect a key another subsystem deliberately retired.
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        # Somebody else re-benches it with a different deadline.
        pool._replace(
            pool.by_id("k0"), replace(pool.by_id("k0"), last_error_reset_at=NOW + 9 * HOUR)
        )
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k1", "k2"]

    def test_a_dead_credential_is_never_released(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        pool._replace(pool.by_id("k0"), replace(pool.by_id("k0"), last_status=STATUS_DEAD))
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k1", "k2"]

    def test_a_key_with_no_usable_secret_is_never_released(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        pool._replace(pool.by_id("k0"), replace(pool.by_id("k0"), runtime_api_key=""))
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k1", "k2"]

    def test_a_key_awaiting_token_refresh_is_never_released(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        pool.needs_refresh.add("k0")
        runtime.note_call("gemini", AUX)
        assert available(pool, refresh=True) == ["k1", "k2"]

    def test_an_expired_bench_is_left_to_the_host(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW - HOUR})
        assert available(pool, clear_expired=True) == ["k0", "k1", "k2"]
        assert pool.cleared == ["k0"]


class TestStayingOutOfTheWay:
    def test_an_unannounced_call_gets_the_hosts_answer(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        runtime.forget_call()
        assert available(pool) == ["k1", "k2"]

    def test_another_providers_call_gets_the_hosts_answer(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        runtime.note_call("openai", "gpt-5")
        assert available(pool) == ["k1", "k2"]

    def test_an_empty_ledger_costs_nothing(self, bound):
        _binding, pool, state = bound
        runtime.note_call("gemini", MAIN)
        before = state.writes
        assert available(pool) == ["k0", "k1", "k2"]
        assert state.writes == before

    def test_an_unreadable_store_degrades_to_the_hosts_answer(self, bound):
        _binding, pool, state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        state.fail_read = True
        state.data.clear()
        runtime.note_call("gemini", AUX)
        # In-memory copy still serves; the point is that nothing raises and
        # the pool keeps answering.
        assert set(available(pool)) >= {"k1", "k2"}

    def test_a_pool_that_raises_inside_the_wrapper_still_answers(self, bound):
        binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})

        def boom():
            raise RuntimeError("snapshot failed")

        pool.entries = boom
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k1", "k2"]

    def test_the_wrapper_never_writes_to_the_pool(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + 24 * HOUR})
        before = pool.entries()
        runtime.note_call("gemini", AUX)
        available(pool)
        assert pool.entries() == before


class TestModelSpelling:
    def test_a_routing_prefix_does_not_hide_a_bench(self, bound):
        # litellm sends ``gemini/gemini-3.6-flash``, Google's native API sends
        # ``models/gemini-3.6-flash``, config sends the bare name. Treated as
        # three models, a key spent under one spelling looks free under the
        # next — the exact bug the ledger exists to prevent.
        _binding, pool, _state = bound
        runtime.note_call("gemini", f"models/{MAIN}")
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        runtime.note_call("gemini", f"gemini/{MAIN}")
        assert available(pool) == ["k1", "k2"]

    def test_a_variant_is_a_different_quota(self, bound):
        _binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        runtime.note_call("gemini", AUX)
        assert "k0" in available(pool)


class TestCustomEndpoints:
    def test_the_agents_generic_label_matches_a_named_pool(self):
        assert runtime.providers_match("custom:my-box", "custom") is True

    def test_two_different_providers_do_not_match(self):
        assert runtime.providers_match("gemini", "openai") is False

    def test_an_unnamed_side_never_matches(self):
        assert runtime.providers_match("", "gemini") is False
        assert runtime.providers_match("gemini", "") is False

    def test_case_and_padding_do_not_matter(self):
        assert runtime.providers_match(" Gemini ", "GEMINI") is True


# --------------------------------------------------------------------------
# v0.0.6 — the journal: recording what happened, and what it cost
# --------------------------------------------------------------------------


class TestWritingDownWhatHappened:
    def test_a_block_is_recorded_with_the_model_that_earned_it(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        blocks = journal_of(binding).blocks()
        assert len(blocks) == 1
        assert (blocks[0].credential_id, blocks[0].model) == ("k0", MAIN)
        assert blocks[0].status_code == 429
        assert blocks[0].reset_at == clock.now + HOUR

    def test_a_bench_the_host_sized_is_still_recorded(self, journaling):
        # The ledger deliberately ignores these: with no deadline of KAME's
        # own there is no fingerprint, so the bench can never be proved ours
        # and must never be released. It is still a real refusal, and the
        # journal is where refusals go regardless of who sized them.
        binding, pool, _state, _clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {})

        assert len(binding._store.load(force=True)) == 0
        blocks = journal_of(binding).blocks()
        assert len(blocks) == 1
        assert blocks[0].reset_at is None
        assert blocks[0].sized_by == journal_module.SIZED_BY_HOST

    def test_the_classifiers_reasoning_reaches_the_record(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini",
            MAIN,
            window="per_day",
            source="body",
            reset_at=clock.now + HOUR,
            now=clock.now,
        )
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        block = journal_of(binding).blocks()[0]
        assert (block.window, block.source) == ("per_day", "body")
        assert block.sized_by == journal_module.SIZED_BY_KAME

    def test_a_verdict_the_pool_ignored_is_not_claimed_as_ours(self, journaling):
        # KAME proposed one deadline and something else stored another. The
        # row must say the cooldown in force was not KAME's, or a later
        # accuracy check would be grading the wrong prediction.
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_minute", source="headers",
            reset_at=clock.now + 30.0, now=clock.now,
        )
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        block = journal_of(binding).blocks()[0]
        assert block.window == "per_minute"
        assert block.sized_by != journal_module.SIZED_BY_KAME

    def test_and_it_is_not_confused_with_kame_having_stayed_out_of_it(
        self, journaling
    ):
        # The reason this is its own value. "KAME sized it and the number was
        # dropped" and "KAME declined to size it" were both recorded as
        # `host`, so a plugin whose every deadline was being discarded looked
        # exactly like a quiet, healthy install — classifying everything,
        # changing nothing, and saying nothing about it.
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_minute", source="headers",
            reset_at=clock.now + 30.0, now=clock.now,
        )
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k1"), 429, {"reset_at": clock.now + HOUR})

        dropped, declined = journal_of(binding).blocks()
        assert dropped.sized_by == journal_module.SIZED_BY_DROPPED
        assert declined.sized_by == journal_module.SIZED_BY_HOST

    def test_a_verdict_is_claimed_once(self, journaling):
        # Two keys failing in one turn. The second must not inherit the
        # first's reasoning just because it arrived after it.
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=clock.now + HOUR, now=clock.now,
        )
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})
        pool._mark_exhausted(pool.by_id("k1"), 429, {"reset_at": clock.now + HOUR})

        windows = [b.window for b in journal_of(binding).blocks()]
        assert windows == ["per_day", "unknown"]

    def test_a_stale_verdict_is_not_attached(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=clock.now + HOUR, now=clock.now,
        )
        clock.advance(runtime.JUDGEMENT_TTL_SECONDS + 1)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        assert journal_of(binding).blocks()[0].window == "unknown"

    def test_a_verdict_about_another_model_is_not_attached(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", AUX, window="per_day", source="body",
            reset_at=clock.now + HOUR, now=clock.now,
        )
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        assert journal_of(binding).blocks()[0].window == "unknown"

    def test_a_dead_credential_is_not_a_quota_fact(self, journaling):
        binding, pool, _state, _clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 401, {})
        assert journal_of(binding).blocks() == []

    def test_nothing_is_recorded_without_a_model(self, journaling):
        binding, pool, _state, clock = journaling
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})
        assert journal_of(binding).blocks() == []

    def test_a_broken_journal_does_not_cost_the_bench(self, journaling):
        # The ledger is what the next selection reads. A failure in the half
        # that only watches must never reach it.
        binding, pool, state, clock = journaling
        runtime.note_call("gemini", MAIN)
        binding._journal.load(force=True)
        state.fail_write = OSError("disk gone")
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        state.fail_write = None
        pool.now = clock.now
        assert available(pool) == ["k1", "k2"]


class TestTimingTheRecovery:
    def test_a_success_closes_the_block_it_followed(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + 60.0})
        blocked_at = clock.now

        # The pool hands k0 back out once its cooldown has lapsed; that
        # selection is how the success side learns which key was in play.
        clock.advance(90.0)
        pool.now = clock.now
        pool._select_unlocked()
        binding.note_success("gemini", MAIN)

        recovery = journal_of(binding).recovery_for("k0", MAIN)
        assert recovery is not None
        assert recovery.blocked_at == blocked_at
        assert recovery.observed_seconds == 90.0
        assert recovery.was_early is False

    def test_a_key_that_came_back_early_is_marked_early(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})

        clock.advance(120.0)
        runtime.note_selection("gemini", "k0")
        binding.note_success("gemini", MAIN)

        assert journal_of(binding).recovery_for("k0", MAIN).was_early is True

    def test_an_ordinary_success_writes_nothing(self, journaling):
        # Successes are the normal state of the world. If each one were a
        # write, the journal would cost a locked file update per API call to
        # record that nothing happened.
        binding, _pool, state, _clock = journaling
        runtime.note_selection("gemini", "k0")
        before = state.writes
        for _ in range(50):
            binding.note_success("gemini", MAIN)
        assert state.writes == before

    def test_only_the_first_success_is_the_measurement(self, journaling):
        binding, pool, state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + 60.0})
        runtime.note_selection("gemini", "k0")

        clock.advance(90.0)
        binding.note_success("gemini", MAIN)
        writes = state.writes
        clock.advance(30.0)
        binding.note_success("gemini", MAIN)

        assert state.writes == writes
        assert journal_of(binding).recovery_for("k0", MAIN).observed_seconds == 90.0

    def test_a_success_on_another_model_answers_nothing(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})
        runtime.note_selection("gemini", "k0")

        clock.advance(60.0)
        binding.note_success("gemini", AUX)

        assert journal_of(binding).recovery_for("k0", MAIN) is None

    def test_without_a_known_key_nothing_is_recorded(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})
        runtime.forget_selections()

        clock.advance(60.0)
        binding.note_success("gemini", MAIN)
        assert journal_of(binding).recoveries() == []


class TestWatchingSelection:
    def test_selection_is_mirrored_for_the_success_side(self, journaling):
        binding, pool, _state, _clock = journaling
        assert binding.watching_selection is True
        pool._select_unlocked()
        assert runtime.selected_for("gemini") == "k0"

    def test_selection_still_returns_what_the_host_chose(self, journaling):
        _binding, pool, _state, _clock = journaling
        entry, pending = pool._select_unlocked()
        assert entry.id == "k0"
        assert pending == []

    def test_an_empty_pool_records_no_selection(self, journaling):
        _binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        for entry in pool.entries():
            pool._mark_exhausted(entry, 429, {"reset_at": clock.now + HOUR})
        pool.now = clock.now
        assert pool._select_unlocked()[0] is None
        assert runtime.selected_for("gemini") == ""

    def test_a_pool_without_selection_still_binds(self):
        # The other two wrappers are the plugin working; this one is only the
        # plugin watching. A Hermes that renamed it must cost the statistic
        # and nothing else.
        class NoSelect(FakePool):
            _select_unlocked = None

        module = _fresh_module()
        module.CredentialPool = NoSelect
        state = FakeState()
        binding = PoolBinding(LedgerStore(state), journal=JournalStore(state))
        assert binding.install(module) is True
        assert binding.watching_selection is False
        binding.uninstall()

    def test_uninstall_puts_selection_back(self, journaling):
        binding, pool, _state, _clock = journaling
        pool_type = type(pool)
        binding.uninstall()
        assert getattr(pool_type._select_unlocked, "__kame_wrapped__", False) is False


class TestTheJournalChangesNothing:
    """The observation half must be invisible to every decision."""

    def test_a_recorded_block_still_releases_for_another_model(self, journaling):
        _binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})
        pool.now = clock.now

        runtime.note_call("gemini", AUX)
        assert "k0" in available(pool)

    def test_a_recorded_block_is_still_held_for_its_own_model(self, journaling):
        _binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + HOUR})
        pool.now = clock.now
        assert available(pool) == ["k1", "k2"]

    def test_a_binding_without_a_journal_behaves_as_before(self, bound):
        binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        binding.note_success("gemini", MAIN)
        assert available(pool) == ["k1", "k2"]


# --------------------------------------------------------------------------
# The escape hatch
# --------------------------------------------------------------------------


@pytest.fixture
def sole():
    """One key, one pool, a hand-wound clock — the lockout arrangement.

    A single credential is the case where KAME's precision is most dangerous:
    a daily cap read as twenty-four hours means twenty-four hours with no
    agent at all, and the host's own sole-credential shortening is switched
    off by the very ``reset_at`` KAME supplied.
    """
    module = _fresh_module()
    state = FakeState()
    clock = Clock()
    binding = PoolBinding(
        LedgerStore(state, ttl_seconds=0.0, clock=clock),
        journal=JournalStore(state, ttl_seconds=0.0, clock=clock),
        clock=clock,
    )
    assert binding.install(module) is True
    pool = module.CredentialPool("gemini", [FakeCredential(id="k0", label="only-key")])
    yield binding, pool, state, clock
    binding.uninstall()


def _lock_out(pool, clock, *, model=MAIN, seconds=DAY, reason="rate_limit", scope=None):
    """Spend every key in the pool on one model, for a long time.

    ``scope`` stages the classifier's verdict the way the real hook does, so
    the bench lands in the ledger carrying what the provider said about how
    far the refusal reaches.
    """
    runtime.note_call("gemini", model)
    for entry in pool.entries():
        if scope is not None:
            runtime.note_judgement(
                "gemini", model, window="per_day", source="body",
                reset_at=clock.now + seconds, now=clock.now, scope=scope,
            )
        pool._mark_exhausted(
            entry, 429, {"reset_at": clock.now + seconds, "reason": reason}
        )
    pool.now = clock.now


def _tick(pool, clock, seconds):
    clock.advance(seconds)
    pool.now = clock.now


class TestNotLockingTheUserOut:
    def test_a_lockout_is_respected_at_first(self, sole):
        # The deadline is believed by default. Testing it immediately would
        # make the whole cooldown pointless.
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        assert available(pool) == []
        _tick(pool, clock, 120)
        assert available(pool) == []

    def test_the_last_key_comes_back_for_a_try(self, sole):
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == ["k0"]

    def test_the_try_is_written_down_before_the_key_is_handed_over(self, sole):
        # If the call never comes back, the attempt still has to have counted:
        # an unrecorded probe repeats on the very next query.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        available(pool)
        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.probes == 1
        assert bench.last_probe_at == clock.now

    def test_repeated_questions_in_one_turn_agree(self, sole):
        # ``_available_entries`` is asked several times per request. All the
        # answers have to name the same key, and only one attempt may be spent.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        answers = [available(pool) for _ in range(5)]
        assert answers == [["k0"]] * 5
        assert binding._store.load(force=True).find("k0", MAIN).probes == 1

    def test_the_window_closes_and_the_lockout_returns(self, sole):
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == ["k0"]
        _tick(pool, clock, probe_module.PROBE_WINDOW_SECONDS)
        assert available(pool) == []

    def test_the_schedule_widens(self, sole):
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == ["k0"]

        # Ten minutes after the first attempt, not five.
        _tick(pool, clock, 300)
        assert available(pool) == []
        _tick(pool, clock, 300)
        assert available(pool) == ["k0"]

    def test_a_refused_probe_does_not_restart_the_schedule(self, sole):
        # The refusal re-benches the key through ``_mark_exhausted``. If that
        # wiped the attempt count, the backoff would never widen and the
        # feature would become a way to hammer a spent key all day.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == ["k0"]

        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + DAY})
        pool.now = clock.now
        assert binding._store.load(force=True).find("k0", MAIN).probes == 1

        _tick(pool, clock, 400)
        assert available(pool) == []

    def test_a_successful_probe_is_recorded_as_an_early_recovery(self, sole):
        # This is the whole point: with the key never tried, a deadline read
        # too long could never be discovered. The probe is what makes the
        # measurement possible at all.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == ["k0"]

        runtime.note_selection("gemini", "k0")
        binding.note_success("gemini", MAIN)
        recovery = journal_of(binding).recovery_for("k0", MAIN)
        assert recovery is not None
        assert recovery.was_early is True


class TestWhenItStaysShut:
    def test_never_while_another_key_is_usable(self, journaling):
        # A probe is a last resort, not a shortcut. With a healthy key in the
        # pool there is nothing to rescue.
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + DAY})
        pool.now = clock.now
        _tick(pool, clock, 3 * probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == ["k1", "k2"]
        assert binding._store.load(force=True).find("k0", MAIN).probes == 0

    def test_never_a_bench_kame_did_not_write(self, sole):
        # Ownership is the invariant the whole design rests on. Somebody
        # else's bench is not KAME's guess to second-guess.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        held = replace(pool.by_id("k0"), last_error_reset_at=clock.now + 9 * HOUR)
        pool._replace(pool.by_id("k0"), held)
        _tick(pool, clock, 3 * probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == []
        assert binding._store.load(force=True).find("k0", MAIN).probes == 0

    def test_never_a_dead_credential(self, sole):
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        dead = replace(pool.by_id("k0"), last_status=STATUS_DEAD)
        pool._replace(pool.by_id("k0"), dead)
        _tick(pool, clock, 3 * probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == []

    def test_never_a_key_with_no_usable_secret(self, sole):
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        empty = replace(pool.by_id("k0"), runtime_api_key="")
        pool._replace(pool.by_id("k0"), empty)
        _tick(pool, clock, 3 * probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == []

    def test_never_a_key_awaiting_a_token_refresh(self, sole):
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        pool.needs_refresh.add("k0")
        _tick(pool, clock, 3 * probe_module.FIRST_PROBE_SECONDS)
        assert available(pool, refresh=True) == []

    def test_never_an_exhausted_balance(self, sole):
        # Out of credits is a depletion, not a clock. Hermes keeps the full
        # bench for exactly this case and so does KAME.
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock, reason="billing")
        _tick(pool, clock, 3 * probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == []

    def test_never_a_short_cooldown(self, sole):
        # Waiting out a five-minute throttle is cheaper than a call that fails.
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock, seconds=probe_module.MIN_BENCH_SECONDS - 60)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == []

    def test_never_for_a_model_that_did_not_spend_it(self, sole):
        # The auxiliary model still has its whole allowance, so it gets the
        # key outright — no probe involved, and no attempt spent.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0"]
        assert binding._store.load(force=True).find("k0", MAIN).probes == 0

    def test_an_unwritable_store_still_counts_the_probe(self, sole):
        # A probe that cannot be counted has no backoff, and one with no
        # backoff repeats on every query — which would hammer a spent key all
        # day. Storage that refuses the write must not cost the count; the
        # copy in hand is the newer one either way.
        binding, pool, state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        state.fail_write = OSError("read-only state")

        assert available(pool) == ["k0"]
        _tick(pool, clock, probe_module.PROBE_WINDOW_SECONDS)
        assert available(pool) == []
        assert binding._store.load().find("k0", MAIN).probes == 1

    def test_the_probe_never_writes_to_the_pool(self, sole):
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        before = [replace(entry) for entry in pool.entries()]
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        assert available(pool) == ["k0"]
        assert pool.entries() == before
        assert pool.cleared == []


class TestALimitThatCoversTheWholeKey:
    """When the provider says the key is spent everywhere, believe it.

    Per-model release is right where the quota is per-model. Where it is not,
    handing the key to a second model buys a second refusal, a slower turn,
    and a journal row about a limit that was never this model's. The provider
    is the one who knows which it is, and v0.0.8 reads what it said.
    """

    def test_the_key_is_not_offered_to_a_model_it_never_spent(self, journaling):
        binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=clock.now + DAY, now=clock.now, scope="account",
        )
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + DAY})
        pool.now = clock.now

        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k1", "k2"]
        assert binding._store.load(force=True).find("k0", MAIN).covers_every_model

    def test_the_same_bench_without_that_evidence_is_still_released(self, journaling):
        # The control for the test above, and the guard for every provider
        # that says nothing: silence must keep behaving as it did in v0.0.7.
        _binding, pool, _state, clock = journaling
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + DAY})
        pool.now = clock.now

        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0", "k1", "k2"]

    def test_a_key_wide_lockout_is_still_tested_not_simply_obeyed(self, sole):
        # The escape hatch has to reach this case too. A provider that says
        # "spent everywhere for a day" would otherwise bench the only key for
        # every model with no way to ever check the claim — which is the exact
        # lockout v0.0.7 exists to prevent, re-entered through the new door.
        _binding, pool, _state, clock = sole
        _lock_out(pool, clock, scope="account")
        runtime.note_call("gemini", AUX)
        assert available(pool) == []

        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0"]

    def test_the_try_is_counted_against_the_bench_that_blocks(self, sole):
        # Counting it under the model in flight would leave the bench that is
        # actually holding the key untested forever, and the backoff would
        # never widen.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock, scope="account")
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", AUX)
        available(pool)

        ledger = binding._store.load(force=True)
        assert ledger.find("k0", MAIN).probes == 1
        assert ledger.find("k0", AUX) is None

    def test_a_hold_of_our_own_is_a_prediction_and_gets_tested_too(self, sole):
        # The host is holding nothing: its cooldown was a short throttle that
        # has since lapsed and been cleared. The only thing standing between
        # the user and their agent is KAME's own ledger — which is exactly the
        # situation the escape hatch exists for, whether the guess is spelled
        # as a bench or as a hold.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock, scope="account", seconds=12 * HOUR)

        runtime.note_call("gemini", AUX)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + 60.0})
        _tick(pool, clock, 90)
        pool._replace(
            pool.by_id("k0"),
            replace(pool.by_id("k0"), last_status=STATUS_OK, last_error_reset_at=None),
        )

        runtime.note_call("gemini", AUX)
        assert available(pool) == []
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0"]
        assert binding._store.load(force=True).find("k0", MAIN).probes == 1

    def test_a_key_wide_bench_somebody_else_wrote_is_left_alone(self, sole):
        binding, pool, _state, clock = sole
        _lock_out(pool, clock, scope="account")
        held = replace(pool.by_id("k0"), last_error_reset_at=clock.now + 9 * HOUR)
        pool._replace(pool.by_id("k0"), held)
        _tick(pool, clock, 3 * probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", AUX)
        assert available(pool) == []
        assert binding._store.load(force=True).find("k0", MAIN).probes == 0


class TestBelievingTheAnswer:
    """A probe that comes back clean has to end the lockout, not repeat it.

    v0.0.7 asked the question and then discarded the answer. The key was
    withheld again on the very next selection, tested again five minutes
    later, and the user got one call every widening interval instead of their
    agent back.
    """

    def _probe_then_succeed(self, binding, pool, clock, *, model=MAIN):
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", model)
        assert available(pool) == ["k0"]
        binding.note_success("gemini", model)

    def test_a_key_that_works_comes_back_for_good(self, sole):
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        self._probe_then_succeed(binding, pool, clock)

        # Not five minutes later — immediately, and every time after.
        for _ in range(3):
            runtime.note_call("gemini", MAIN)
            assert available(pool) == ["k0"]
            _tick(pool, clock, 30)

    def test_the_bench_is_marked_rather_than_deleted(self, sole):
        # The deadline in the row is what proves the host's cooldown is ours
        # to unwind. Without it the key would be stranded behind a bench no
        # longer claimed by anybody.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        self._probe_then_succeed(binding, pool, clock)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench is not None
        assert bench.is_refuted is True
        assert bench.reset_at == NOW + DAY

    def test_the_pool_is_still_never_written_to(self, sole):
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        before = pool.by_id("k0")
        self._probe_then_succeed(binding, pool, clock)
        after = pool.by_id("k0")
        assert after.last_status == before.last_status
        assert after.last_error_reset_at == before.last_error_reset_at

    def test_a_success_with_no_probe_outstanding_moves_nothing(self, sole):
        # Ordinary traffic. The selection mirror is best-effort and is not
        # allowed to release anything on its own.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        runtime.note_selection("gemini", "k0")
        binding.note_success("gemini", MAIN)
        assert binding._store.load(force=True).find("k0", MAIN).is_refuted is False
        runtime.note_call("gemini", MAIN)
        assert available(pool) == []

    def test_a_refusal_from_the_tested_key_leaves_the_bench_standing(self, sole):
        # The other half of the answer. This one has always worked; it is here
        # so a change to the success path cannot quietly break it.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", MAIN)
        available(pool)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + DAY})
        pool.now = clock.now

        ledger = binding._store.load(force=True)
        assert ledger.find("k0", MAIN).is_refuted is False
        assert ledger.find("k0", MAIN).probes == 1

    def test_a_later_success_is_not_credited_to_an_answered_probe(self, sole):
        # The probe failed. Whatever succeeds afterwards succeeded on its own
        # terms, and must not be read as the tested key having recovered.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", MAIN)
        available(pool)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + DAY})
        pool.now = clock.now

        binding.note_success("gemini", MAIN)
        assert binding._store.load(force=True).find("k0", MAIN).is_refuted is False

    def test_a_probe_that_is_never_answered_expires(self, sole):
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", MAIN)
        available(pool)

        _tick(pool, clock, runtime.PROBE_TTL_SECONDS + 1)
        binding.note_success("gemini", MAIN)
        assert binding._store.load(force=True).find("k0", MAIN).is_refuted is False

    def test_a_key_wide_claim_survives_only_as_far_as_the_evidence(self, sole):
        # The probe went out for the auxiliary model because a bench filed
        # under the main one claimed to cover the whole key. The auxiliary
        # model answered: the reach is disproved, the main model's own
        # deadline is not.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock, scope="account")
        self._probe_then_succeed(binding, pool, clock, model=AUX)

        ledger = binding._store.load(force=True)
        assert ledger.find("k0", MAIN).covers_every_model is False
        assert ledger.is_spent_for("k0", MAIN, clock.now) is True

        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0"]
        # Past the window in which a probe already in flight keeps being
        # offered, so the answer below is about the bench and not about
        # keeping one turn's answers consistent.
        _tick(pool, clock, probe_module.PROBE_WINDOW_SECONDS + 1)
        runtime.note_call("gemini", MAIN)
        assert available(pool) == []

    def test_a_shorter_refusal_does_not_free_a_key_held_for_the_day(self, sole):
        # Reachable only through the escape hatch: the daily bench is probed,
        # the probe fails with a per-minute complaint, and the sixty-second
        # answer would replace the daily one. The daily counter is still spent.
        binding, pool, _state, clock = sole
        _lock_out(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", MAIN)
        available(pool)
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": clock.now + 60.0})
        pool.now = clock.now

        assert binding._store.load(force=True).benched_until(
            "k0", MAIN, clock.now
        ) == NOW + DAY
        _tick(pool, clock, 120)
        runtime.note_call("gemini", MAIN)
        assert available(pool) == []


def _refuse(pool, clock, key, *, seconds, window="per_minute", model=MAIN, reason="rate_limit"):
    """One complete refusal, staged the way the real hook stages it."""
    runtime.note_call("gemini", model)
    runtime.note_judgement(
        "gemini", model, window=window, source="headers",
        reset_at=clock.now + seconds, now=clock.now,
    )
    pool._mark_exhausted(
        pool.by_id(key), 429, {"reset_at": clock.now + seconds, "reason": reason}
    )
    pool.now = clock.now


def _prove_it_short(pool, clock, key, *, seconds, window="per_minute", times=2):
    """Refuse, wait exactly the deadline, refuse again — ``times`` over.

    This is the only sequence the journal can read as "the deadline was too
    short" without knowing anything about the provider, and it is what the
    widening is built on.
    """
    _refuse(pool, clock, key, seconds=seconds, window=window)
    for _ in range(times):
        _tick(pool, clock, seconds + 1)
        _refuse(pool, clock, key, seconds=seconds, window=window)


class TestHoldingLonger:
    """A deadline measured too short, twice in a row, is widened.

    The one number in the plugin that is learned rather than read. Everything
    here is about the shape of the evidence required, and about the price the
    widening must not pay: the host's own stored deadline stays untouched, so
    the fingerprint keeps proving this bench is KAME's and the per-model
    release keeps working underneath it.
    """

    def test_a_deadline_proved_short_twice_is_doubled(self, journaling):
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is True
        assert bench.until == clock.now + 120.0

    def test_one_repeat_is_a_coincidence(self, journaling):
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0, times=1)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is False
        assert bench.until == clock.now + 60.0

    def test_the_widening_widens_again(self, journaling):
        # The journal stores the deadline the key was *actually* held to, so a
        # widened bench that is still too short is measured as still too short.
        # Storing the host's shorter number here would cap the escalation at
        # one step and then drop it back — which is why the gap between the two
        # numbers below is wider than the journal's under-prediction grace: a
        # narrower one would pass either way and prove nothing.
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=600.0, window="per_hour")
        _tick(pool, clock, 1201.0)
        _refuse(pool, clock, "k0", seconds=600.0, window="per_hour")

        assert binding._store.load(force=True).find("k0", MAIN).until == clock.now + 2400.0

    def test_an_ordinary_refusal_clears_the_evidence(self, journaling):
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)
        # Long after the deadline this time: the key worked in between, or
        # nothing asked for it. Either way nothing was measured.
        _tick(pool, clock, 2 * HOUR)
        _refuse(pool, clock, "k0", seconds=60.0)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is False
        assert bench.until == clock.now + 60.0

    def test_the_host_is_never_told_a_different_number(self, journaling):
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)

        assert pool.by_id("k0").last_error_reset_at == clock.now + 60.0
        assert binding._store.load(force=True).find("k0", MAIN).reset_at == clock.now + 60.0

    def test_the_key_stays_out_after_the_hosts_cooldown_lapses(self, journaling):
        _binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)
        _tick(pool, clock, 61.0)

        runtime.note_call("gemini", MAIN)
        assert available(pool) == ["k1", "k2"]

    def test_and_is_still_free_for_every_other_model(self, journaling):
        # The whole plugin in one line. A longer hold on the model that spent
        # the quota must not cost the model that did not.
        _binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)
        _tick(pool, clock, 61.0)

        runtime.note_call("gemini", AUX)
        assert available(pool) == ["k0", "k1", "k2"]

    def test_it_comes_back_when_the_longer_deadline_lapses(self, journaling):
        _binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)
        _tick(pool, clock, 121.0)

        runtime.note_call("gemini", MAIN)
        assert available(pool) == ["k0", "k1", "k2"]

    def test_a_different_key_is_not_charged_for_this_ones_tuition(self, journaling):
        # A free key and a paid one on the same model do not share a window.
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)
        _refuse(pool, clock, "k1", seconds=60.0)

        assert binding._store.load(force=True).find("k1", MAIN).is_extended is False

    def test_a_different_window_is_a_different_question(self, journaling):
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)
        _tick(pool, clock, 121.0)
        _refuse(pool, clock, "k0", seconds=HOUR, window="per_day")

        assert binding._store.load(force=True).find("k0", MAIN).is_extended is False

    def test_without_a_journal_nothing_is_widened(self, bound):
        # The v0.0.5 arrangement. No measurements, so no learned numbers —
        # and the plugin behaves exactly as it did before this version.
        binding, pool, _state = bound
        clock = Clock()
        _prove_it_short(pool, clock, "k0", seconds=60.0)

        assert binding._store.load(force=True).find("k0", MAIN).is_extended is False

    def test_the_pool_is_still_never_written_to(self, journaling):
        _binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=60.0)
        _tick(pool, clock, 61.0)
        runtime.note_call("gemini", MAIN)
        available(pool)

        entry = pool.by_id("k0")
        assert entry.last_status == STATUS_EXHAUSTED
        assert entry.last_error_reset_at == clock.now - 1.0
        assert pool.cleared == []


class TestAWidenedBenchIsStillAPrediction:
    """The safety net, and the reason escalation shipped after refutation.

    A learned number is exactly as fallible as a read one. If the only key for
    a model is held on a deadline KAME widened itself, that deadline has to be
    testable and one clean call has to end it — otherwise the widening is a way
    to lock the user out of their own agent with the plugin's own arithmetic.
    """

    def _widen_the_only_key(self, pool, clock):
        _prove_it_short(pool, clock, "k0", seconds=10 * 60.0, window="per_hour")

    def test_the_only_key_is_held_for_the_widened_deadline(self, sole):
        binding, pool, _state, clock = sole
        self._widen_the_only_key(pool, clock)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.until == clock.now + 20 * 60.0
        runtime.note_call("gemini", MAIN)
        assert available(pool) == []

    def test_but_it_is_tested_like_any_other(self, sole):
        _binding, pool, _state, clock = sole
        self._widen_the_only_key(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)

        runtime.note_call("gemini", MAIN)
        assert available(pool) == ["k0"]

    def test_and_a_clean_call_ends_it_for_good(self, sole):
        binding, pool, _state, clock = sole
        self._widen_the_only_key(pool, clock)
        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS)
        runtime.note_call("gemini", MAIN)
        available(pool)
        binding.note_success("gemini", MAIN)

        assert binding._store.load(force=True).find("k0", MAIN).is_refuted is True
        _tick(pool, clock, 30)
        runtime.note_call("gemini", MAIN)
        assert available(pool) == ["k0"]


class TestAKeyThatAnsweredIsNotEvidence:
    """The widening needs a bench that was actually served.

    ``_prove_it_short`` is the sequence that earns a longer hold: refused,
    held to the deadline, refused again the moment it lapsed. If a call
    succeeded anywhere in that stretch, the key was not on the bench and the
    second refusal is a fresh limit, not a measurement. On a small pool that
    is the common case, not the exotic one — v0.0.9 releases keys early on
    purpose, the moment a probe answers.
    """

    def test_a_success_in_between_stops_the_widening(self, journaling):
        binding, pool, _state, clock = journaling
        _refuse(pool, clock, "k0", seconds=60.0)
        _tick(pool, clock, 61.0)
        _refuse(pool, clock, "k0", seconds=60.0)      # strike one, so far

        # The key answers before the second deadline lapses — a probe came
        # back clean, or the host simply used it.
        _tick(pool, clock, 30.0)
        runtime.note_selection("gemini", "k0")
        binding.note_success("gemini", MAIN)

        _tick(pool, clock, 31.0)
        _refuse(pool, clock, "k0", seconds=60.0)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is False
        assert bench.until == clock.now + 60.0

    def test_and_the_run_can_be_earned_again_afterwards(self, journaling):
        # Suppression is not a permanent pardon: the next two refusals that do
        # bracket a served bench count, and the widening comes back.
        binding, pool, _state, clock = journaling
        _refuse(pool, clock, "k0", seconds=60.0)
        _tick(pool, clock, 30.0)
        runtime.note_selection("gemini", "k0")
        binding.note_success("gemini", MAIN)

        _tick(pool, clock, 31.0)
        _prove_it_short(pool, clock, "k0", seconds=60.0)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is True
        assert bench.until == clock.now + 120.0


class TestTheDailyAnchorThatRepeatsForever:
    """The failure this plugin was least able to fix, end to end.

    Google's free tier meters per day and rolls at midnight US/Pacific, so
    that is what KAME benches to. If the real rollover is five minutes later,
    the key is handed back at 00:00, refused at 00:00:30 — and the refusal is
    still a *daily* one, so it is benched until the **next** midnight. Five
    minutes of error costs a full day of that key, and it repeats every day,
    forever, because nothing about it ever changes.

    Doubling could not touch it: the next anchor is already a day away and the
    24-hour ceiling eats the whole multiplier, so ``stretch`` returned None.
    Moving the anchor half an hour ends it after the second day.
    """

    @staticmethod
    def _refuse_at_the_anchor(pool, clock, key, *, anchor_in):
        """A daily refusal whose deadline is the calendar, not a stopwatch."""
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="anchor",
            reset_at=clock.now + anchor_in, now=clock.now,
        )
        pool._mark_exhausted(
            pool.by_id(key), 429,
            {"reset_at": clock.now + anchor_in, "reason": "rate_limit"},
        )
        pool.now = clock.now

    def test_the_anchor_is_moved_after_the_second_wasted_day(self, journaling):
        binding, pool, _state, clock = journaling
        day = 24 * HOUR

        # Benched mid-afternoon, until tonight's midnight.
        self._refuse_at_the_anchor(pool, clock, "k0", anchor_in=10 * HOUR)

        # Handed back at midnight, refused thirty seconds later — and the
        # refusal is daily, so the deadline is tomorrow's midnight.
        _tick(pool, clock, 10 * HOUR + 30.0)
        self._refuse_at_the_anchor(pool, clock, "k0", anchor_in=day - 30.0)
        first = binding._store.load(force=True).find("k0", MAIN)
        assert first.is_extended is False       # one repeat is a coincidence

        # The same thing again the next night. Now it is a pattern.
        _tick(pool, clock, day)
        self._refuse_at_the_anchor(pool, clock, "k0", anchor_in=day - 30.0)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is True
        assert bench.until == bench.reset_at + 30 * 60.0
        # And the host's own number is untouched, as always — it is the
        # fingerprint, and moving it would strand the key on every other model.
        assert bench.reset_at == clock.now + day - 30.0

    def test_a_stopwatch_daily_cap_is_still_scaled(self, journaling):
        # Same window name, different provider, no known rollover: the
        # deadline is an hourly re-probe, which is a length and doubles.
        binding, pool, _state, clock = journaling
        _prove_it_short(pool, clock, "k0", seconds=HOUR, window="per_day")

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is True
        assert bench.until == clock.now + 2 * HOUR


# --------------------------------------------------------------------------


GOOGLE_BILLING_SENTENCE = (
    "You exceeded your current quota, please check your plan and billing "
    "details. For more information on this error, read the docs: "
    "https://ai.google.dev/gemini-api/docs/rate-limits."
)


def _google_free_tier(quota_id, retry_delay="21s"):
    return {
        "error": {
            "code": 429,
            "message": GOOGLE_BILLING_SENTENCE,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": quota_id}],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": retry_delay,
                },
            ],
        }
    }


class TestTheRealGooglePayloadEndToEnd:
    """The sentence, the verdict and the pool, with nothing staged by hand.

    Everywhere else in this file the judgement is written straight into the
    runtime with the numbers the test wants. That is the right way to state a
    binding rule, and it is exactly why the defect in v0.1.2 and earlier
    survived four versions: every binding test asserted what should happen
    *given* a correct verdict, and the verdict was wrong.

    So this one classifies Google's real payload and feeds whatever comes
    out — right or wrong — into the same runtime the hook uses. If the two
    halves ever disagree again, this fails.
    """

    @staticmethod
    def _refuse_for_real(pool, clock, key, body, *, model=MAIN):
        classify = importlib.import_module(f"{PACKAGE}.core.classify").classify
        verdict = classify(
            provider="gemini", model=model, status_code=429,
            error_message=GOOGLE_BILLING_SENTENCE, error_body=body,
            now_epoch=clock.now,
        )
        assert verdict is not None, "the hook would have deferred to the host"
        runtime.note_call("gemini", model)
        runtime.note_judgement(
            "gemini", model,
            window=verdict.quota_window, source=verdict.source,
            reset_at=verdict.reset_at, now=clock.now, scope=verdict.quota_scope,
        )
        # The host stores whatever the verdict said, which is the number the
        # ledger will have to match to prove the bench is KAME's.
        pool._mark_exhausted(
            pool.by_id(key), 429,
            {"reset_at": verdict.reset_at, "reason": verdict.reason},
        )
        pool.now = clock.now
        return verdict

    def test_a_twenty_one_second_throttle_costs_twenty_one_seconds(self, journaling):
        binding, pool, _state, clock = journaling
        verdict = self._refuse_for_real(
            pool, clock, "k0",
            _google_free_tier("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
        )
        assert verdict.reason == "rate_limit"

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench is not None, "the ledger has to own this bench to unwind it"
        assert bench.until == clock.now + 21.0

        # Twenty-two seconds later the key is back, with no probe, no rescue
        # and nothing clever — it simply waited the wait the provider asked
        # for. Read as billing this was a day.
        _tick(pool, clock, 22.0)
        runtime.note_call("gemini", MAIN)
        assert "k0" in available(pool)

    def test_and_the_other_models_on_that_key_never_went_down(self, journaling):
        binding, pool, _state, clock = journaling
        self._refuse_for_real(
            pool, clock, "k0",
            _google_free_tier("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
        )
        # ``account`` scope would have taken the auxiliary model with it, which
        # is the whole thing the per-model ledger exists to prevent.
        runtime.note_call("gemini", AUX)
        assert "k0" in available(pool)

    def test_the_daily_cap_reaches_the_anchor_the_plugin_wrote_for_it(self, journaling):
        binding, pool, _state, clock = journaling
        verdict = self._refuse_for_real(
            pool, clock, "k0",
            _google_free_tier(
                "GenerateRequestsPerDayPerProjectPerModel-FreeTier", retry_delay="37s"
            ),
        )
        # Not the 37 seconds Google attaches to a spent daily counter, and not
        # Pacific midnight either: the standard daily quota floor of 3600s.
        assert verdict.source == "window"
        assert verdict.reset_at - clock.now == pytest.approx(3600.0, abs=1)

    def test_the_last_key_is_still_rescuable(self, journaling):
        # ``billing`` sits in NEVER_PROBE_REASONS, so under the old reading a
        # single-key user had no escape hatch at all: benched a day, never
        # retried, and no success could ever be observed to prove it wrong.
        binding, pool, _state, clock = journaling
        body = _google_free_tier(
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier", retry_delay="37s"
        )
        for key in ("k0", "k1", "k2"):
            self._refuse_for_real(pool, clock, key, body)

        runtime.note_call("gemini", MAIN)
        assert available(pool) == []

        _tick(pool, clock, probe_module.FIRST_PROBE_SECONDS + 1)
        runtime.note_call("gemini", MAIN)
        assert available(pool) != [], "no key was ever offered for a probe"


# The epoch has to be a real one: ``X-RateLimit-Reset`` is milliseconds, and
# it is read as milliseconds *because of its magnitude*. See the note beside
# the same constant in ``test_core.py``.
OPENROUTER_NOW = 1_786_829_619.0


def _openrouter_free_tier(bucket, reset_in_seconds):
    """OpenRouter's 429, with its rate-limit headers where it really puts them.

    Not in the headers: in the body, under ``error.metadata.headers``.
    """
    return {
        "error": {
            "code": 429,
            "message": f"Rate limit exceeded: {bucket}",
            "metadata": {
                "headers": {
                    "X-RateLimit-Limit": "50",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(
                        int((OPENROUTER_NOW + reset_in_seconds) * 1000)
                    ),
                }
            },
        }
    }


@contextmanager
def _journaling_pool(provider, now):
    """The journaling fixture, under a named provider and a real epoch.

    Shared rather than restated once a second provider needed it — the same
    reason ``_RETRY_KEY`` shares the header pattern. Two copies of a fixture
    drift exactly like two copies of a regex.
    """
    module = _fresh_module()
    state = FakeState()
    clock = Clock(now)
    binding = PoolBinding(
        LedgerStore(state, ttl_seconds=0.0, clock=clock),
        journal=JournalStore(state, ttl_seconds=0.0, clock=clock),
        clock=clock,
    )
    assert binding.install(module) is True
    pool = module.CredentialPool(
        provider,
        three_healthy_keys(),
    )
    try:
        yield binding, pool, state, clock
    finally:
        binding.uninstall()


@pytest.fixture
def openrouter():
    with _journaling_pool("openrouter", OPENROUTER_NOW) as fixture:
        yield fixture


@pytest.fixture
def openai_daily():
    with _journaling_pool("openai", OPENROUTER_NOW) as fixture:
        yield fixture


class TestTheRealOpenRouterPayloadEndToEnd:
    """The other half of the v0.1.3 lesson, applied to the other free tier.

    Google's defect was a fixture quoting a sentence the provider no longer
    sends. OpenRouter's was a fixture putting the rate-limit headers in the
    headers — the one place OpenRouter does not put them. Both fixtures were
    green, and both described a provider that does not exist.

    So this classifies the payload OpenRouter actually returns and feeds
    whatever comes out into the pool.
    """

    @staticmethod
    def _refuse_for_real(pool, clock, key, bucket, seconds, *, model=MAIN):
        classify = importlib.import_module(f"{PACKAGE}.core.classify").classify
        message = f"Rate limit exceeded: {bucket}"
        verdict = classify(
            provider="openrouter", model=model, status_code=429,
            error_message=message, error_body=_openrouter_free_tier(bucket, seconds),
            now_epoch=clock.now,
        )
        assert verdict is not None, "the hook would have deferred to the host"
        runtime.note_call("openrouter", model)
        runtime.note_judgement(
            "openrouter", model,
            window=verdict.quota_window, source=verdict.source,
            reset_at=verdict.reset_at, now=clock.now, scope=verdict.quota_scope,
        )
        pool._mark_exhausted(
            pool.by_id(key), 429,
            {"reset_at": verdict.reset_at, "reason": verdict.reason},
        )
        pool.now = clock.now
        return verdict

    def test_the_daily_cap_is_benched_to_the_moment_openrouter_named(self, openrouter):
        binding, pool, _state, clock = openrouter
        verdict = self._refuse_for_real(pool, clock, "k0", "free-models-per-day", 9 * 3600)
        assert verdict.reason == "rate_limit"

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench is not None, "the ledger has to own this bench to unwind it"
        # Nine hours, because the payload said nine hours. The hourly re-probe
        # this used to be would have spent eight more refusals getting here.
        assert bench.until == pytest.approx(clock.now + 9 * 3600, abs=1)

    def test_and_the_shared_bucket_takes_every_model_with_it(self, openrouter):
        # The mirror image of the Google case, and correct for the same
        # reason: the provider said which one it is. OpenRouter meters its
        # free tier per account, so releasing this key for another model
        # would spend a call to be told the same thing.
        _binding, pool, _state, clock = openrouter
        self._refuse_for_real(pool, clock, "k0", "free-models-per-day", 9 * 3600)
        runtime.note_call("openrouter", AUX)
        assert "k0" not in available(pool)

    def test_the_per_minute_bucket_is_the_same_bucket(self, openrouter):
        _binding, pool, _state, clock = openrouter
        verdict = self._refuse_for_real(pool, clock, "k0", "free-models-per-min", 41)
        assert verdict.reset_at == pytest.approx(clock.now + 41, abs=1)
        runtime.note_call("openrouter", AUX)
        assert "k0" not in available(pool), "the per-minute bucket is account-wide too"

        _tick(pool, clock, 42.0)
        runtime.note_call("openrouter", MAIN)
        assert "k0" in available(pool)


class TestTheDailyWaitTheProviderSpelledOut:
    """A daily 429 carrying both kinds of number, all the way to the pool.

    The header names the per-minute counter; the message names the daily
    wait. The cascade picks the header, the long-window rule discards it as
    misleading, and what used to replace it was the flat hourly re-probe —
    six wasted refusals per key on a cap the provider said resets in six
    hours.
    """

    DAILY_MESSAGE = (
        "Rate limit reached for gpt-4o in organization org-x on requests per day "
        "(RPD): Limit 200, Used 200, Requested 1. Please try again in 6h12m."
    )

    def test_the_bench_is_the_wait_the_message_stated(self, openai_daily):
        binding, pool, _state, clock = openai_daily
        classify = importlib.import_module(f"{PACKAGE}.core.classify").classify
        verdict = classify(
            provider="openai", model=MAIN, status_code=429,
            error_message=self.DAILY_MESSAGE,
            headers={"x-ratelimit-reset-requests": "58s",
                     "x-ratelimit-reset-tokens": "12s"},
            now_epoch=clock.now,
        )
        assert verdict is not None
        runtime.note_call("openai", MAIN)
        runtime.note_judgement(
            "openai", MAIN,
            window=verdict.quota_window, source=verdict.source,
            reset_at=verdict.reset_at, now=clock.now, scope=verdict.quota_scope,
        )
        pool._mark_exhausted(
            pool.by_id("k0"), 429,
            {"reset_at": verdict.reset_at, "reason": verdict.reason},
        )
        pool.now = clock.now

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench is not None
        assert bench.until == pytest.approx(clock.now + 6 * 3600 + 12 * 60)

        # Not back at the hour, which is where the flat re-probe would have
        # put it — and where it would have been refused again.
        _tick(pool, clock, 3600.0 + 60.0)
        runtime.note_call("openai", MAIN)
        assert "k0" not in available(pool)


class TestOneCredentialHoldingSeveralKeys:
    """Hermes reads a provider env var and stores what it finds as one key.

    ``GOOGLE_API_KEY=k1,k2,k3`` is therefore a single credential whose key is
    the string ``k1,k2,k3``. Measured against the installed host before any of
    this existed: three keys in, one entry out, two commas inside it. Every
    request sends the whole string, the provider rejects it, and the pool has
    nothing to rotate to — the rotation this plugin exists for has one
    credential to work with and no way to say so.

    That list is not a mistake by the person who typed it. It is the format
    Agent Zero's key pool accepts, the format ``/kame-keys add`` accepts, and
    the obvious way to write down a set of keys.
    """

    # Long enough to be read as credentials rather than as stray words.
    KEYS = [f"AIzaSyPartNumber{n}" + "x" * 22 for n in range(3)]

    def _pool(self, module, *, value=None, **kwargs):
        return module.CredentialPool(
            "gemini",
            [FakeCredential(
                id="row", label="GOOGLE_API_KEY", source="env:GOOGLE_API_KEY",
                runtime_api_key=value if value is not None else ",".join(self.KEYS),
                **kwargs,
            )],
        )

    def test_three_keys_in_one_row_become_three_credentials(self, bound):
        binding, _pool, _state = bound
        pool = self._pool(binding._module)
        assert sorted(e.runtime_api_key for e in pool.entries()
                      if multikey.is_child_source(e.source)) == sorted(self.KEYS)

    def test_the_row_itself_is_never_selected(self, bound):
        """Its key is a comma-joined list. No provider accepts that."""
        binding, _pool, _state = bound
        pool = self._pool(binding._module)
        assert "row" not in available(pool)
        assert len(available(pool)) == 3

    def test_rotation_now_has_somewhere_to_go(self, bound):
        binding, _pool, _state = bound
        pool = self._pool(binding._module)
        tried = []
        for _ in range(4):
            entry, _pending = pool._select_unlocked()
            if entry is None:
                break
            tried.append(entry.runtime_api_key)
            # With a deadline, so the bench holds. Marked without one the pool
            # clears it again on the next selection — correctly, and it would
            # hand back the same key forever, which tests nothing.
            pool._mark_exhausted(
                entry, 429, {"reason": "rate_limit", "reset_at": NOW + DAY},
            )
        assert sorted(tried) == sorted(self.KEYS)
        assert pool._select_unlocked()[0] is None

    def test_a_part_is_never_written_to_disk(self, bound):
        """The guard that makes the whole thing safe to have.

        A written part is a second copy of a key list whose only correct
        source is the env var behind it — one that does not update when that
        var is edited, and that would be split again on the next load.
        """
        binding, _pool, _state = bound
        pool = self._pool(binding._module)
        pool._persist()
        assert pool.persisted == [["row"]]

    def test_and_the_pool_still_holds_them_afterwards(self, bound):
        """Hidden for the write, not removed by it."""
        binding, _pool, _state = bound
        pool = self._pool(binding._module)
        before = ids(pool.entries())
        pool._persist()
        assert ids(pool.entries()) == before

    def test_marking_a_part_spent_does_not_write_it_either(self, bound):
        binding, _pool, _state = bound
        pool = self._pool(binding._module)
        part = next(e for e in pool.entries() if multikey.is_child_source(e.source))
        pool._mark_exhausted(part, 429, {"reason": "rate_limit"})
        pool._persist()
        assert all("row" == written for row in pool.persisted for written in row)

    def test_a_single_key_is_left_exactly_as_it_was(self, bound):
        binding, _pool, _state = bound
        pool = self._pool(binding._module, value=self.KEYS[0])
        assert ids(pool.entries()) == ["row"]
        assert available(pool) == ["row"]

    def test_an_oauth_row_is_not_split(self, bound):
        """Its access token is not a key list and cutting it up is nonsense."""
        binding, _pool, _state = bound
        pool = self._pool(binding._module, auth_type="oauth")
        assert ids(pool.entries()) == ["row"]

    def test_a_part_keeps_its_identity_across_a_reload(self, bound):
        """The ledger remembers what is spent by id."""
        binding, _pool, _state = bound
        first = ids(self._pool(binding._module).entries())
        second = ids(self._pool(binding._module).entries())
        assert first == second

    def test_removing_a_key_removes_exactly_that_part(self, bound):
        binding, _pool, _state = bound
        full = {e.runtime_api_key: e.id
                for e in self._pool(binding._module).entries()
                if multikey.is_child_source(e.source)}
        fewer = self._pool(binding._module, value=",".join(self.KEYS[1:]))
        kept = [e for e in fewer.entries() if multikey.is_child_source(e.source)]
        assert [e.id for e in kept] == [full[self.KEYS[1]], full[self.KEYS[2]]]

    def test_a_part_starts_with_no_history(self, bound):
        """The parent's status described its whole malformed value."""
        binding, _pool, _state = bound
        pool = self._pool(
            binding._module,
            last_status=STATUS_EXHAUSTED,
            last_error_reset_at=NOW + 86400,
            request_count=91,
        )
        parts = [e for e in pool.entries() if multikey.is_child_source(e.source)]
        assert [e.last_status for e in parts] == [None, None, None]
        assert [e.request_count for e in parts] == [0, 0, 0]
        assert len(available(pool)) == 3

    def test_expanding_twice_adds_nothing(self, bound):
        binding, _pool, _state = bound
        pool = self._pool(binding._module)
        before = ids(pool.entries())
        binding._expand(pool)
        binding._expand(pool)
        assert ids(pool.entries()) == before

    def test_a_pool_built_before_the_plugin_loaded_still_gains_its_parts(self):
        """The main conversation holds one of those for its whole life."""
        module = _fresh_module()
        pool = module.CredentialPool(
            "gemini",
            [FakeCredential(id="row", label="GOOGLE_API_KEY",
                            source="env:GOOGLE_API_KEY",
                            runtime_api_key=",".join(self.KEYS))],
        )
        assert ids(pool.entries()) == ["row"]
        binding = PoolBinding(LedgerStore(FakeState(), ttl_seconds=0.0), clock=lambda: NOW)
        assert binding.install(module) is True
        try:
            assert len(available(pool)) == 3
        finally:
            binding.uninstall()

    def test_without_a_persist_to_guard_nothing_is_split(self):
        """No way to keep a part off disk is no permission to make one."""
        module = _fresh_module()
        # Shadowed rather than deleted: the fake pool class is a subclass, so
        # the attribute belongs to its parent and cannot be removed from it.
        module.CredentialPool._persist = None
        binding = PoolBinding(LedgerStore(FakeState(), ttl_seconds=0.0), clock=lambda: NOW)
        assert binding.install(module) is True
        try:
            assert binding.splitting_multikey is False
            pool = self._pool(module)
            assert ids(pool.entries()) == ["row"]
        finally:
            binding.uninstall()

    def test_a_part_does_not_inherit_the_row_s_fingerprint(self, bound):
        """The host stores ``secret_fingerprint`` for a borrowed credential —
        it is the only trace of the key in the live install's auth.json. It
        is a digest of the whole list, so on a part it would name something
        that part is not."""
        binding, _pool, _state = bound
        pool = self._pool(binding._module, extra={"secret_fingerprint": "sha256:abc"})
        parts = [e for e in pool.entries() if multikey.is_child_source(e.source)]
        assert parts
        assert all("secret_fingerprint" not in e.extra for e in parts)

    def test_and_the_row_itself_keeps_it(self, bound):
        binding, _pool, _state = bound
        pool = self._pool(binding._module, extra={"secret_fingerprint": "sha256:abc"})
        assert pool.by_id("row").extra["secret_fingerprint"] == "sha256:abc"

    def test_an_entry_with_nowhere_to_mark_a_part_is_not_split(self, bound):
        """``source`` is where a derived part is marked, and that marker is
        what keeps it off disk. An entry shape without one would produce
        parts indistinguishable from stored credentials — at exactly the
        moment that distinction is protecting a key."""
        binding, _pool, _state = bound

        @dataclass(frozen=True)
        class Sourceless:
            id: str
            label: str = "GOOGLE_API_KEY"
            auth_type: str = AUTH_TYPE_API_KEY
            runtime_api_key: str = ""
            last_status: str = STATUS_OK
            last_status_at: Optional[float] = None
            last_error_code: Optional[int] = None
            last_error_reason: Optional[str] = None
            last_error_message: Optional[str] = None
            last_error_reset_at: Optional[float] = None
            extra: Dict[str, Any] = field(default_factory=dict)

        pool = binding._module.CredentialPool(
            "gemini", [Sourceless(id="row", runtime_api_key=",".join(self.KEYS))]
        )
        assert ids(pool.entries()) == ["row"]

    def test_and_it_does_not_stop_the_rest_of_the_pool_being_split(self, bound):
        """Skipped one at a time, not by abandoning the pool.

        Letting the failure raise instead would abort the whole expansion, so
        an ordinary comma-separated row sharing a pool with an unfamiliar one
        would quietly go back to being a single unusable credential — the
        exact bug this version exists to fix, reintroduced by an entry that
        has nothing to do with it.
        """
        binding, _pool, _state = bound

        @dataclass(frozen=True)
        class Sourceless:
            id: str
            label: str = "odd"
            auth_type: str = AUTH_TYPE_API_KEY
            runtime_api_key: str = ""
            last_status: str = STATUS_OK
            last_status_at: Optional[float] = None
            last_error_code: Optional[int] = None
            last_error_reason: Optional[str] = None
            last_error_message: Optional[str] = None
            last_error_reset_at: Optional[float] = None
            extra: Dict[str, Any] = field(default_factory=dict)

        pool = binding._module.CredentialPool("gemini", [
            Sourceless(id="odd", runtime_api_key=",".join(self.KEYS)),
            FakeCredential(id="row", label="GOOGLE_API_KEY",
                           source="env:GOOGLE_API_KEY",
                           runtime_api_key=",".join(self.KEYS)),
        ])
        parts = [
            e for e in pool.entries()
            if multikey.is_child_source(getattr(e, "source", ""))
        ]
        assert len(parts) == 3


# --------------------------------------------------------------------------
# Which of the healthy keys goes out next
# --------------------------------------------------------------------------


class TestSpreadingTheLoad:
    """The host's default hands out ``available[0]`` every time.

    With one key that is correct. With fifteen it means one key absorbs every
    request until the provider refuses it, then the next one does, and a pool
    of fifteen keys becomes fifteen consecutive walls instead of fifteen keys'
    worth of throughput. The arithmetic of the ordering lives in
    ``test_dispersion.py``; what is asserted here is that the pool consults
    it — and, just as importantly, when it must not.
    """

    def test_consecutive_selections_do_not_return_the_same_key(self, bound):
        binding, pool, _state = bound
        picked = [pool._select_unlocked()[0].id for _ in range(3)]
        assert picked == ["k0", "k1", "k2"]

    def test_and_then_it_comes_back_round(self, bound):
        binding, pool, _state = bound
        first = [pool._select_unlocked()[0].id for _ in range(3)]
        second = [pool._select_unlocked()[0].id for _ in range(3)]
        # Each key has one request against it after the first pass, so the
        # tie-break is least-recently-used and the second pass repeats the
        # first. Even spreading, not a random walk. Spelled out rather than
        # compared to itself: ``first == second`` is also true when nothing
        # spreads at all, which is the one answer this must not accept.
        assert first == second == ["k0", "k1", "k2"]

    def test_the_set_of_usable_keys_is_unchanged(self, bound):
        binding, pool, _state = bound
        pool._select_unlocked()
        available, _pending = pool._available_entries()
        assert sorted(entry.id for entry in available) == ["k0", "k1", "k2"]

    def test_a_stated_strategy_is_left_alone(self, bound):
        # ``round_robin``, ``random`` and ``least_used`` are typed into a
        # config file by a person who decided how their keys should be picked.
        # ``fill_first`` is what the host returns when nobody decided
        # anything. Overriding an unstated default is a correction;
        # overriding a stated choice is ignoring the user.
        binding, pool, _state = bound
        pool._strategy = "least_used"
        picked = [pool._select_unlocked()[0].id for _ in range(3)]
        assert picked == ["k0", "k0", "k0"]

    def test_a_build_that_names_its_strategies_differently_is_left_alone(self, bound):
        binding, pool, _state = bound
        del binding._module.STRATEGY_FILL_FIRST
        try:
            picked = [pool._select_unlocked()[0].id for _ in range(3)]
        finally:
            binding._module.STRATEGY_FILL_FIRST = STRATEGY_FILL_FIRST
        assert picked == ["k0", "k0", "k0"]

    def test_uninstalling_gives_the_host_its_order_back(self, bound):
        binding, pool, _state = bound
        pool._select_unlocked()
        binding.uninstall()
        assert [pool._select_unlocked()[0].id for _ in range(2)] == ["k0", "k0"]

    def test_a_key_the_pool_withholds_is_not_reintroduced_by_the_order(self, bound):
        # Ordering runs after every rule that decides usability, so it can
        # only rearrange what survived them.
        binding, pool, _state = bound
        pool._mark_exhausted(pool.by_id("k0"), 429, {"reset_at": NOW + HOUR})
        available, _pending = pool._available_entries()
        assert sorted(entry.id for entry in available) == ["k1", "k2"]

    def test_the_busiest_key_is_passed_over_not_dropped(self, bound):
        binding, pool, _state = bound
        for _ in range(4):
            binding._dispersion.note(
                "gemini", pool_binding._mark_id(pool.by_id("k0")), NOW
            )
        assert pool._select_unlocked()[0].id == "k1"
        # Passed over, not withheld: it is still one of the usable keys and
        # will be chosen again once the others have caught up.
        available, _pending = pool._available_entries()
        assert sorted(entry.id for entry in available) == ["k0", "k1", "k2"]

    def test_asking_which_keys_are_usable_gets_the_host_s_own_order(self, bound):
        """Only the choice is re-ordered, never the answer to a question.

        A status report, a count, the host's own emptiness check and its own
        test suite all call ``_available_entries``. None of them is deciding
        which key to spend, so none of them gets a different answer because of
        this plugin — re-ordering them would change host behaviour for no
        gain at all.
        """
        binding, pool, _state = bound
        for _ in range(4):
            binding._dispersion.note(
                "gemini", pool_binding._mark_id(pool.by_id("k0")), NOW
            )
        available, _pending = pool._available_entries()
        assert [entry.id for entry in available] == ["k0", "k1", "k2"]

    def test_two_threads_selecting_at_once_get_different_keys(self, bound):
        # The anti-dogpile. A key is counted busy the moment it is handed
        # out, not when its call returns, so a second thread selecting a
        # millisecond later sees it as loaded.
        binding, pool, _state = bound
        picked: List[str] = []
        lock = threading.Lock()

        def select_once():
            entry, _pending = pool._select_unlocked()
            with lock:
                picked.append(entry.id)

        threads = [threading.Thread(target=select_once) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(picked) == ["k0", "k1", "k2"]

    def test_the_auxiliary_model_does_not_inherit_the_main_model_s_load(self, bound):
        # Per-minute quota is metered per key per model. A key hammered on the
        # main model is not a busy key for a smaller one, and treating it as
        # one would spread the auxiliary lane away from keys that are fine.
        binding, pool, _state = bound
        runtime.note_call("gemini", MAIN)
        for _ in range(5):
            binding._dispersion.note(
                dispersion_module.bucket_for("gemini", MAIN),
                pool_binding._mark_id(pool.by_id("k0")),
                NOW,
            )
        runtime.note_call("gemini", AUX)
        available, _pending = pool._available_entries()
        assert [entry.id for entry in available] == ["k0", "k1", "k2"]

    def test_two_rows_holding_one_key_are_one_key(self, bound):
        # Hermes seeds a provider from the environment and from auth.json and
        # can end up with two entries carrying the identical runtime key — its
        # own rotation code has to detect exactly that. Counted by row they
        # look like two idle keys, and alternating between them would hammer
        # one provider counter at twice the intended rate while a genuinely
        # idle third key waited.
        binding, _pool, _state = bound
        module = binding._module
        pool = module.CredentialPool("gemini", [
            FakeCredential(id="twin-a", label="a", runtime_api_key="sk-same"),
            FakeCredential(id="twin-b", label="b", runtime_api_key="sk-same"),
            FakeCredential(id="other", label="c", runtime_api_key="sk-other"),
        ])
        first = pool._select_unlocked()[0].id
        second = pool._select_unlocked()[0].id
        assert first == "twin-a"
        assert second == "other"

    def test_a_key_replaced_in_place_does_not_inherit_the_old_one_s_load(self, bound):
        # Same row, new key: the provider is metering something that has spent
        # nothing. Counting by row would bench a fresh key behind the one it
        # replaced.
        binding, pool, _state = bound
        for _ in range(6):
            binding._dispersion.note(
                dispersion_module.bucket_for("gemini", ""),
                pool_binding._mark_id(pool.by_id("k0")),
                NOW,
            )
        # Stated both ways round, because "k0 is picked" is also what a pool
        # that spreads nothing at all answers. Loaded, k0 goes last; the only
        # thing that changes between the two assertions is the key it holds.
        assert pool._select_unlocked()[0].id == "k1"
        replaced = replace(pool.by_id("k0"), runtime_api_key="sk-rotated")
        pool._replace(pool.by_id("k0"), replaced)
        assert pool._select_unlocked()[0].id == "k0"

    def test_a_key_that_just_came_off_a_bench_waits_its_turn(self, bound):
        # The pool keeps last_error_reset_at until something succeeds on the
        # credential, so a deadline that lapsed a moment ago is still legible
        # there — including one the host wrote, which the ledger never saw.
        binding, pool, _state = bound
        released = replace(pool.by_id("k0"), last_error_reset_at=NOW - 10)
        pool._replace(pool.by_id("k0"), released)
        assert pool._select_unlocked()[0].id == "k1"

    def test_an_old_bench_is_not_held_against_a_key(self, bound):
        # Ninety seconds, not forever. A key benched this morning is a rested
        # key now, and treating it otherwise would leave one credential last in
        # line for the life of the process.
        binding, pool, _state = bound
        stale = replace(
            pool.by_id("k0"),
            last_error_reset_at=NOW - dispersion_module.JUST_RELEASED_SECONDS - 1,
        )
        pool._replace(pool.by_id("k0"), stale)
        assert pool._select_unlocked()[0].id == "k0"

    def test_a_key_still_serving_its_bench_is_not_treated_as_released(self, bound):
        # A deadline in the future is a key that is either withheld already or
        # is the escape hatch's probe — offered precisely because there was
        # nothing rested to prefer. Sorting it last would defeat the hatch.
        binding, pool, _state = bound
        probe = replace(pool.by_id("k0"), last_error_reset_at=NOW + HOUR)
        pool._replace(pool.by_id("k0"), probe)
        assert pool._select_unlocked()[0].id == "k0"

    def test_the_switch_gives_the_host_its_default_back(self, monkeypatch):
        # The one feature able to make a working install pick a different key
        # than stock Hermes would, so it gets a switch of its own — for
        # somebody who wants fill_first back without losing cooldown sizing.
        monkeypatch.setenv("KAME_SPREAD_DISABLED", "1")
        module = _fresh_module()
        binding = PoolBinding(LedgerStore(FakeState(), ttl_seconds=0.0), clock=lambda: NOW)
        assert binding.install(module) is True
        try:
            assert binding.spreading_load is False
            pool = module.CredentialPool("gemini", three_healthy_keys())
            assert [pool._select_unlocked()[0].id for _ in range(3)] == ["k0", "k0", "k0"]
        finally:
            binding.uninstall()


class TestNamingEveryKeyThePoolOffers:
    """One selection has to register every healthy key, not only the winner.

    The counters are written when a key goes out, so a pool where one key
    takes everything would report exactly that one key — and the fourteen
    idle ones, which are the reason somebody opened the report, would not
    appear at all.
    """

    def test_one_selection_gives_every_healthy_key_a_counter(self, bound):
        binding, pool, _state = bound
        pool._select_unlocked()
        counted = binding._dispersion.totals()
        assert len(next(iter(counted.values()))) == 3

    def test_the_ones_that_did_not_go_out_are_counted_at_zero(self, bound):
        binding, pool, _state = bound
        picked = pool._select_unlocked()[0]
        counted = next(iter(binding._dispersion.totals().values()))
        mark = pool_binding._mark_id(picked)
        assert counted[mark] == 1
        assert sorted(counted.values()) == [0, 0, 1]

    def test_every_healthy_key_gets_a_readable_name(self, bound):
        # Without the name the idle rows render as hash fragments, which is
        # the least useful possible way to say "this key of yours is unused".
        binding, pool, _state = bound
        pool._select_unlocked()
        assert len(binding._names) == 3
        assert all(name for name in binding._names.values())

    def test_a_benched_key_is_not_introduced(self, bound):
        # The section reports the keys that are in play. A key the pool is
        # withholding is already accounted for under "Benched right now", and
        # listing it as idle here would read as a second, contradictory claim.
        binding, pool, _state = bound
        pool._mark_exhausted(
            pool.by_id("k2"), 429, {"reset_at": NOW + 3600, "reason": "rate_limit"}
        )
        binding._dispersion = dispersion_module.Dispersion()
        pool._select_unlocked()
        counted = next(iter(binding._dispersion.totals().values()))
        assert len(counted) == 2

    def test_with_the_switch_off_nothing_is_introduced(self, monkeypatch):
        # The switch means "give the host its default back", and a counter
        # still filling up would be the plugin still deciding something.
        monkeypatch.setenv("KAME_SPREAD_DISABLED", "1")
        module = _fresh_module()
        state = FakeState()
        binding = PoolBinding(LedgerStore(state, ttl_seconds=0.0), clock=lambda: NOW)
        assert binding.install(module) is True
        try:
            pool = module.CredentialPool("gemini", three_healthy_keys())
            pool._select_unlocked()
            pool._select_unlocked()
            # The key that went out is still counted — that count is what the
            # report draws, and with the switch off it is the picture of the
            # host hammering one key. What must not happen is the *other* two
            # keys being registered, because registering them is the plugin
            # taking an interest in a selection it was told to stay out of.
            counted = next(iter(binding._dispersion.totals().values()))
            assert len(counted) == 1
        finally:
            binding.uninstall()
