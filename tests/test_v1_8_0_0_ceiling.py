"""G8 (``PLAN_1.8.0.0.md``, settled 2026-09-15): no credential is ever held
out of rotation longer than a configurable ceiling, default 3600s.

Before this release the only bound on a hold was ``carousel.HARD_DELAY_CAP_S``
— a day, fixed, unconfigurable — and ``research/1.8.0.0/gate/1707.json``'s
``real-17`` is what that cost in practice: five real ``openai-codex`` 429
``usage_limit_reached`` refusals, each carrying a ``resets_in_seconds`` near
12,000, holding a key for 3h+ on the provider's word alone. The owner closed
this on 2026-09-15: past the ceiling the key returns to selection and is
probed; a refusal that repeats is held again, so a genuinely long outage
costs about one refused request per interval rather than a healthy key
sitting out silently for however long a provider claimed.

Two chokepoints, because there are two places this plugin ever holds a
credential:

* ``core.carousel.Carousel.mark`` — the in-turn rotation. One clamp, in
  ``mark`` itself, catches every branch of ``_escalate`` (the
  stated/calendar-reset branch, the server branch, the invented ladder) and
  the never-shorten rule's retained-prior-hold case, because all of them
  funnel through the same assignment to ``sick_until``.
* ``pool_binding.PoolBinding`` — the host's own credential pool, wrapped.
  ``_carry_deadline`` bounds the deadline KAME injects when the host derived
  none of its own; ``_remember`` bounds what ``core.escalate.stretch`` widens
  a bench to on the host-block path. Neither can shorten a deadline the host
  derived on its own (``core/ledger.py``'s ``Bench.until`` is a maximum of
  the fingerprinted ``reset_at`` and KAME's own extension, and cannot go
  below the fingerprint — see the comment at ``_remember``'s clamp); that is
  a real, acknowledged limit of this change's scope, not a case this file
  pretends to cover.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_ceiling_under_test"


def _load_package():
    """Import the plugin as a package, the way the Hermes loader does.

    Self-contained on purpose, like every other test module here — its own
    ``PACKAGE`` name, its own load — so this file can be read and run without
    knowing what any other test file did to the module cache.
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
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
settings = importlib.import_module(f"{PACKAGE}.settings")
runtime = importlib.import_module(f"{PACKAGE}.runtime")
pool_binding = importlib.import_module(f"{PACKAGE}.pool_binding")
store_module = importlib.import_module(f"{PACKAGE}.store")

Carousel = carousel_mod.Carousel
PoolBinding = pool_binding.PoolBinding
LedgerStore = store_module.LedgerStore
JournalStore = store_module.JournalStore

ID = "gemini:gemini-3.6-flash"
NOW = 1_000_000.0
HOUR = 3600.0
MAIN = "gemini-3.6-flash"

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
STATUS_DEAD = "dead"
AUTH_TYPE_API_KEY = "api_key"
STRATEGY_FILL_FIRST = "fill_first"


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts with no leaked config, env override or runtime memo.

    ``settings.number`` reads ``KAME_MAX_HOLD`` straight out of
    ``os.environ``, so a dial test that forgets to clean up would leak into
    whichever test runs next in the same process — the exact failure mode
    this fixture exists to rule out.
    """
    settings.forget()
    os.environ.pop("KAME_MAX_HOLD", None)
    runtime.forget_call()
    runtime.forget_judgement()
    runtime.forget_bench_model()
    runtime.forget_selections()
    runtime.forget_probes()
    yield
    settings.forget()
    os.environ.pop("KAME_MAX_HOLD", None)
    runtime.forget_call()
    runtime.forget_judgement()
    runtime.forget_bench_model()
    runtime.forget_selections()
    runtime.forget_probes()


# ---------------------------------------------------------------------------
# 1. core.carousel.Carousel.mark — the in-turn rotation, every path G8 names
# ---------------------------------------------------------------------------


class TestTheCeilingBoundsEveryCarouselPath:
    """One test per path ``PLAN_1.8.0.0.md`` names as able to exceed an hour
    before this release, all against ``core.carousel.Carousel`` directly —
    no host, no pool, just the pure function the rest of the plugin already
    trusts to be testable on its own.
    """

    def test_stated_daily_deadline_openrouter_nine_hours(self):
        # core/carousel.py _escalate, stated/calendar-reset branch
        # (~line 1466-1472): "stated and delay >= daily_cooldown_s".
        engine = Carousel()
        applied = engine.mark(ID, "a", False, 9 * HOUR, "daily", now=NOW, stated=True)
        assert applied == engine.max_hold_s == 3600.0

    def test_stated_codex_resets_in_seconds_real_17_shape(self):
        # The exact measured shape behind research/1.8.0.0/gate/1707.json's
        # real-17: a real openai-codex usage_limit_reached 429 whose
        # resets_in_seconds sized an 11,525-12,660s hold.
        engine = Carousel()
        applied = engine.mark(ID, "a", False, 12660.0, "daily", now=NOW, stated=True)
        assert applied == engine.max_hold_s

    def test_a_24h_calendar_reset(self):
        # core/carousel.py _escalate, the same branch, its calendar_reset=True
        # arm rather than its stated=True one — a provider-reported rollover
        # instant a full day out.
        engine = Carousel()
        applied = engine.mark(ID, "a", False, 24 * HOUR, "daily", now=NOW, calendar_reset=True)
        assert applied == engine.max_hold_s

    def test_a_stated_wait_on_a_5xx(self):
        # core/carousel.py _escalate, server branch with a stated wait
        # (~line 1645): "if stated and delay > 0: return min(delay, HARD_DELAY_CAP_S)".
        engine = Carousel()
        applied = engine.mark(ID, "a", False, 2 * HOUR, "server", now=NOW, stated=True)
        assert applied == engine.max_hold_s

    def test_a_long_escalation_streak_on_the_denied_ladder(self):
        # core/carousel.py mark() final clamp (~line 1348): the ladder this
        # module invents for denied/auth/revoked, not a number any provider
        # stated, climbing on repetition until it saturates at
        # min(daily_cooldown_s, HARD_DELAY_CAP_S).
        engine = Carousel(daily_cooldown_s=86400.0)
        applied = 0.0
        t = NOW
        for _ in range(15):
            applied = engine.mark(ID, "a", False, 0.0, "denied", now=t)
            t += 1.0
        assert applied == engine.max_hold_s

    def test_the_daily_cooldown_setting_itself_cannot_exceed_the_ceiling(self):
        # Path 5: the owner's own daily_quota_cooldown_seconds setting
        # accepts up to 86400 in its own right. Set it there directly and
        # confirm the (tighter, default) ceiling still wins over it.
        engine = Carousel(daily_cooldown_s=86400.0)
        engine.mark(ID, "a", False, 0.0, "daily", now=NOW)  # opens the doubt window
        later = NOW + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
        applied = engine.mark(ID, "a", False, 0.0, "daily", now=later)
        assert applied == engine.max_hold_s

    def test_a_retained_prior_hold_is_trimmed_to_the_ceiling(self):
        # The never-shorten rule ("mark stores max(existing, now + delay)")
        # must not defeat the ceiling. A hold stored while the ceiling was
        # wide (or before this release existed) has to be trimmed on the very
        # next mark, whatever that refusal's own number is -- that is the
        # whole point of the rule, per the comment in carousel.py's mark().
        engine = Carousel(max_hold_s=7200.0)
        engine.mark(ID, "a", False, 7200.0, "daily", now=NOW, calendar_reset=True)
        assert engine._pools[ID]["a"]["sick_until"] == NOW + 7200.0

        engine.max_hold_s = 300.0  # the owner tightens the dial mid-run
        applied = engine.mark(ID, "a", False, 1.0, "server", now=NOW + 10.0)
        assert applied.retained
        assert applied.held_until == pytest.approx(NOW + 10.0 + 300.0)
        assert engine._pools[ID]["a"]["sick_until"] == pytest.approx(NOW + 310.0)


# ---------------------------------------------------------------------------
# 2. the ceiling is the owner's dial, not a constant
# ---------------------------------------------------------------------------


class TestTheCeilingIsADialNotAConstant:
    def test_lowering_the_ceiling_lowers_the_real_hold(self):
        engine = Carousel(max_hold_s=300.0)
        applied = engine.mark(ID, "a", False, 9 * HOUR, "daily", now=NOW, stated=True)
        assert applied == 300.0

    def test_raising_the_ceiling_raises_the_real_hold(self):
        engine = Carousel(max_hold_s=7200.0)
        applied = engine.mark(ID, "a", False, 9 * HOUR, "daily", now=NOW, stated=True)
        assert applied == 7200.0

    def test_the_default_sits_between_the_two(self):
        engine = Carousel()
        applied = engine.mark(ID, "a", False, 9 * HOUR, "daily", now=NOW, stated=True)
        assert applied == 3600.0

    def test_the_ceiling_never_raises_a_short_hold(self):
        # A 1s server rest stays 1s, whichever way the dial is set: the
        # ceiling is a maximum, never a floor.
        for max_hold_s in (60.0, 300.0, 3600.0, 86400.0):
            engine = Carousel(max_hold_s=max_hold_s)
            applied = engine.mark(ID, "a", False, 1.0, "server", now=NOW, stated=True)
            assert applied == 1.0

    def test_after_the_ceiling_the_key_is_selectable_again(self):
        engine = Carousel()
        engine.mark(ID, "a", False, 9 * HOUR, "daily", now=NOW, stated=True)

        # Still resting just before the ceiling lapses -- offered only as the
        # last resort (EXHAUSTED), not as a healthy pick.
        key, status = engine.select(ID, ["a"], now=NOW + engine.max_hold_s - 1.0)
        assert (key, status) == ("a", "EXHAUSTED")

        # Selectable as healthy again once max_hold_s has actually passed --
        # not the nine hours the provider originally stated.
        key, status = engine.select(ID, ["a"], now=NOW + engine.max_hold_s + 1.0)
        assert (key, status) == ("a", "SUCCESS")

    def test_a_second_refusal_after_the_ceiling_re_applies_a_bounded_hold(self):
        engine = Carousel()
        engine.mark(ID, "a", False, 9 * HOUR, "daily", now=NOW, stated=True)
        later = NOW + engine.max_hold_s + 1.0
        engine.select(ID, ["a"], now=later)  # the probe the owner accepted paying for

        applied = engine.mark(ID, "a", False, 9 * HOUR, "daily", now=later, stated=True)
        assert applied == engine.max_hold_s
        assert engine._pools[ID]["a"]["sick_until"] == later + engine.max_hold_s


# ---------------------------------------------------------------------------
# 3. settings.py — declared the way daily_quota_cooldown_seconds is declared
# ---------------------------------------------------------------------------


class TestTheSettingItself:
    def test_declared_like_daily_cooldown(self):
        assert settings.MAX_HOLD in settings.ALL_NUMBERS
        assert settings.ALL_NUMBERS[settings.MAX_HOLD] == 3600.0
        assert settings.bounds(settings.MAX_HOLD) == (60.0, 86400.0)
        assert settings.env_name(settings.MAX_HOLD) == "KAME_MAX_HOLD"
        assert settings.UNITS[settings.MAX_HOLD] == "seconds"
        assert settings.group_of(settings.MAX_HOLD) == "tuning"
        assert settings.known(settings.MAX_HOLD)

    def test_default_with_nothing_configured(self):
        assert settings.number(settings.MAX_HOLD, settings.ALL_NUMBERS[settings.MAX_HOLD]) == 3600.0
        assert settings.provenance(settings.MAX_HOLD) == "default"

    def test_the_environment_variable_is_read_and_clamped_to_range(self, monkeypatch):
        monkeypatch.setenv("KAME_MAX_HOLD", "10")  # below the 60s floor
        assert settings.number(settings.MAX_HOLD, 3600.0) == 60.0

        monkeypatch.setenv("KAME_MAX_HOLD", "999999")  # above the 86400 ceiling
        assert settings.number(settings.MAX_HOLD, 3600.0) == 86400.0

        monkeypatch.setenv("KAME_MAX_HOLD", "300")
        assert settings.number(settings.MAX_HOLD, 3600.0) == 300.0
        assert settings.provenance(settings.MAX_HOLD) == "environment"

    def test_parse_validates_the_same_way_daily_cooldown_does(self):
        value, error = settings.parse(settings.MAX_HOLD, "10")
        assert value is None
        assert "60" in error and "86400" in error

        value, error = settings.parse(settings.MAX_HOLD, "1800")
        assert error == ""
        assert value == "1800"


# ---------------------------------------------------------------------------
# 4. pool_binding.py — the host's own credential pool, wrapped
# ---------------------------------------------------------------------------
#
# A trimmed copy of tests/test_binding.py's own stand-in pool: this file
# loads its own independent copy of the plugin package (see _load_package
# above), and importing classes bound to *that* file's package would mix two
# separate copies of the same module tree. Keeping this self-contained, like
# every test module in this suite already is, is the deliberate trade.


@dataclass(frozen=True)
class FakeCredential:
    id: str
    label: str = ""
    auth_type: str = AUTH_TYPE_API_KEY
    runtime_api_key: str = "sk-test"
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


def three_healthy_keys() -> List[FakeCredential]:
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
        self._strategy = STRATEGY_FILL_FIRST
        self.cleared: List[str] = []
        self.needs_refresh: set = set()
        self._lock = threading.RLock()
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
        available, pending = self._available_entries(clear_expired=True, refresh=refresh)
        if not available:
            return None, pending
        return available[0], pending


class FakeModule:
    CredentialPool = FakePool
    PooledCredential = FakeCredential
    STATUS_EXHAUSTED = STATUS_EXHAUSTED
    STATUS_DEAD = STATUS_DEAD
    AUTH_TYPE_API_KEY = AUTH_TYPE_API_KEY
    STATUS_OK = STATUS_OK
    STRATEGY_FILL_FIRST = STRATEGY_FILL_FIRST


class FakeState:
    def __init__(self) -> None:
        self.data: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _fresh_module():
    module = type("Module", (), dict(FakeModule.__dict__))
    module.CredentialPool = type("Pool", (FakePool,), {})
    return module


def _pool():
    """A pool with three healthy keys, journalled, on a clock a test can move."""
    module = _fresh_module()
    state = FakeState()
    clock = Clock()
    binding = PoolBinding(
        LedgerStore(state, ttl_seconds=0.0, clock=clock),
        journal=JournalStore(state, ttl_seconds=0.0, clock=clock),
        clock=clock,
    )
    assert binding.install(module) is True
    pool = module.CredentialPool("gemini", three_healthy_keys())
    return binding, pool, clock


class TestTheHostBlockPathIsBounded:
    """The other place this plugin ever holds a credential: the host's own
    pool, wrapped by ``pool_binding.PoolBinding``.
    """

    def test_kames_own_injected_deadline_is_capped(self):
        # pool_binding._carry_deadline: the host derived no reset_at of its
        # own -- an empty error_context, which is what the real host actually
        # passes (see that method's docstring) -- so KAME's own
        # judgement.reset_at is what lands on last_error_reset_at. A 9h
        # OpenRouter-style judgement must not survive that trip whole.
        binding, pool, clock = _pool()
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=clock.now + 9 * HOUR, now=clock.now,
        )
        updated = pool._mark_exhausted(pool.by_id("k0"), 429, {})
        assert updated.last_error_reset_at == pytest.approx(clock.now + 3600.0)

    def test_kames_own_injected_deadline_respects_a_lowered_ceiling(self, monkeypatch):
        monkeypatch.setattr(pool_binding, "_ceiling_s", lambda: 300.0)
        binding, pool, clock = _pool()
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=clock.now + 9 * HOUR, now=clock.now,
        )
        updated = pool._mark_exhausted(pool.by_id("k0"), 429, {})
        assert updated.last_error_reset_at == pytest.approx(clock.now + 300.0)

    def test_a_deadline_the_host_derived_itself_is_outside_this_methods_reach(self):
        # The documented limit, pinned rather than left implicit:
        # _carry_deadline only adds where the host found nothing (see its own
        # docstring, "Only when the host has none"). When error_context
        # already carries a reset_at -- the shape every existing binding test
        # uses -- that number is the provider's own header, not KAME's, and
        # this method returns it untouched.
        binding, pool, clock = _pool()
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=clock.now + 30.0, now=clock.now,
        )
        updated = pool._mark_exhausted(
            pool.by_id("k0"), 429, {"reset_at": clock.now + 9 * HOUR}
        )
        assert updated.last_error_reset_at == clock.now + 9 * HOUR

    @staticmethod
    def _refuse(pool, clock, key, *, seconds, window="per_hour"):
        """One complete refusal, staged the way the real hook stages it —
        the same shape as ``tests/test_binding.py``'s own ``_refuse``.
        """
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window=window, source="headers",
            reset_at=clock.now + seconds, now=clock.now,
        )
        pool._mark_exhausted(
            pool.by_id(key), 429, {"reset_at": clock.now + seconds, "reason": "rate_limit"}
        )
        pool.now = clock.now

    def _prove_it_short(self, pool, clock, key, *, seconds):
        """Refuse, wait exactly the deadline, refuse again, twice over --
        the sequence ``escalate.stretch``'s ``STRIKES_BEFORE_STRETCHING = 2``
        is built to read as "this deadline keeps proving short."
        """
        self._refuse(pool, clock, key, seconds=seconds)
        for _ in range(2):
            clock.advance(seconds + 1.0)
            pool.now = clock.now
            self._refuse(pool, clock, key, seconds=seconds)

    def test_escalate_stretch_widening_is_capped_on_the_host_block_path(self):
        # pool_binding._remember: a deadline proven short twice in a row
        # earns the widening escalate.stretch produces. Unwidened, this
        # deadline (60s) sits well inside the ceiling; the widening on its
        # own (2x, then the ceiling) must not carry it past 3600s --
        # escalate.MAX_HOLD_SECONDS alone would allow a full day.
        binding, pool, clock = _pool()
        self._prove_it_short(pool, clock, "k0", seconds=60.0)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.is_extended is True
        assert bench.until == clock.now + 120.0  # 2x, unaffected: well under the ceiling

    def test_escalate_stretch_widening_respects_a_lowered_ceiling(self, monkeypatch):
        # Same run, with the owner's ceiling turned down far enough that the
        # 2x widening above (120s) would have crossed it.
        monkeypatch.setattr(pool_binding, "_ceiling_s", lambda: 90.0)
        binding, pool, clock = _pool()
        self._prove_it_short(pool, clock, "k0", seconds=60.0)

        bench = binding._store.load(force=True).find("k0", MAIN)
        assert bench.until <= clock.now + 90.0 + 1.0
