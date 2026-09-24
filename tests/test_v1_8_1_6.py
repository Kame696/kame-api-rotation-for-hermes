"""1.8.1.6 -- no hold outlives ``max_hold_seconds``, whoever set it.

Owner decision (2026-09-24): the ceiling the owner sets (one hour by default)
bounds every hold, the provider's own deadline included. Until 1.8.1.5 a
deadline Hermes derived itself -- a ``Retry-After``, a reset named in the
message, Codex's ``resets_in_seconds`` -- was obeyed past the ceiling on
purpose ("the provider outranks anything inferred"), which made the README's
"no key is held longer than one hour, whatever set the hold" untrue. Measured on
the real CredentialPool of Hermes 0.21.5 (experiments/ceiling_all_paths.py):
four of six paths held a key 24h under a 1h ceiling; all six now return it at
the ceiling. ``tools/sandbox_binding.py`` [18c] witnesses the same against the
real pool on 0.21.3-0.21.5.

Three pieces, each tested here without Hermes:

* ``PoolBinding._within_ceiling`` -- as a hold is written, the deadline the
  host would derive (its own parser, or its own TTL) is cut to the ceiling;
* ``PoolBinding._release_past_ceiling`` -- a hold written before the rule, or
  running when the owner lowers the dial, is handed back once it has lasted a
  ceiling, anchored at when the host benched the key;
* ``Ledger.within`` -- KAME's own record of a bench is read through the same
  ceiling, without touching the number that proves the bench is KAME's.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1816_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
pool_binding = importlib.import_module(f"{PACKAGE}.pool_binding")
ledger_module = importlib.import_module(f"{PACKAGE}.core.ledger")
store_module = importlib.import_module(f"{PACKAGE}.store")

NOW = 2_000_000_000.0
HOUR = 3600.0
EXHAUSTED, DEAD, API_KEY = "exhausted", "dead", "api_key"


def _host_module(*, ttl=None, parse=True):
    """The two host helpers the ceiling reads, with Hermes' own semantics."""

    def _normalize_error_context(context):
        context = context if isinstance(context, dict) else {}
        reset = context.get("reset_at") or context.get("resets_at") or context.get("retry_until")
        if reset is None and "try again in 86400s" in str(context.get("message", "")):
            reset = NOW + 24 * HOUR
        return {"reset_at": float(reset)} if reset is not None else {}

    def _exhausted_ttl(error_code, *, sole_credential=False, failure_reason=None):
        if ttl is not None:
            return ttl
        return 5 * 60 if error_code == 401 else HOUR

    namespace = dict(STATUS_EXHAUSTED=EXHAUSTED, STATUS_DEAD=DEAD, AUTH_TYPE_API_KEY=API_KEY)
    if parse:
        namespace.update(_normalize_error_context=_normalize_error_context, _exhausted_ttl=_exhausted_ttl)
    return SimpleNamespace(**namespace)


class _State(dict):
    def set(self, key, value):
        self[key] = value


@pytest.fixture
def binding(monkeypatch):
    monkeypatch.delenv("KAME_MAX_HOLD", raising=False)
    made = pool_binding.PoolBinding(store_module.LedgerStore(_State(), ttl_seconds=0.0), clock=lambda: NOW)
    made._module = _host_module()
    return made


POOL = SimpleNamespace(provider="gemini", _is_sole_credential=lambda: False)


class TestAsTheHoldIsWritten:
    def test_a_providers_24h_is_cut_to_the_ceiling(self, binding):
        carried = binding._within_ceiling(POOL, {"reset_at": NOW + 24 * HOUR}, NOW, status_code=429)
        assert carried["reset_at"] == NOW + HOUR

    def test_a_reset_named_in_the_message_is_cut_too(self, binding):
        carried = binding._within_ceiling(
            POOL, {"message": "Quota exceeded. Please try again in 86400s."}, NOW, status_code=429
        )
        assert carried["reset_at"] == NOW + HOUR

    def test_a_deadline_inside_the_ceiling_is_passed_on_untouched(self, binding):
        context = {"reset_at": NOW + 30.0, "reason": "rate_limit"}
        assert binding._within_ceiling(POOL, context, NOW, status_code=429) is context

    def test_the_hosts_own_ttl_is_cut_when_the_owner_set_a_lower_ceiling(self, binding, monkeypatch):
        monkeypatch.setenv("KAME_MAX_HOLD", "600")
        carried = binding._within_ceiling(POOL, {"reason": "server_error"}, NOW, status_code=503)
        assert carried["reset_at"] == NOW + 600.0

    def test_a_short_ttl_is_never_lengthened(self, binding, monkeypatch):
        monkeypatch.setenv("KAME_MAX_HOLD", "600")
        context = {"reason": "auth"}
        assert binding._within_ceiling(POOL, context, NOW, status_code=401) is context

    def test_a_host_without_the_helpers_is_left_alone(self, binding):
        binding._module = _host_module(parse=False)
        context = {"reset_at": NOW + 24 * HOUR}
        assert binding._within_ceiling(POOL, context, NOW, status_code=429) is context

    def test_kames_own_reading_still_goes_through_the_same_door(self, binding):
        # No model in flight: ``_carry_deadline`` hands straight to the ceiling.
        carried = binding._carry_deadline(POOL, {"reset_at": NOW + 24 * HOUR}, status_code=429)
        assert carried["reset_at"] == NOW + HOUR


def _entry(label, *, status=EXHAUSTED, since=NOW - HOUR - 1.0, key="AIza-x", cooldowns=None):
    return SimpleNamespace(
        id=label,
        label=label,
        last_status=status,
        last_status_at=since,
        last_error_reset_at=NOW + 20 * HOUR,
        auth_type=API_KEY,
        runtime_api_key=key,
        model_cooldowns=cooldowns or {},
    )


class TestAHoldWrittenBeforeTheRule:
    def _released(self, binding, *entries, model=None):
        pool = SimpleNamespace(provider="gemini", _entries=list(entries))
        healthy = SimpleNamespace(id="healthy")
        got = binding._release_past_ceiling(pool, [healthy], refresh=False, model=model)
        return [entry.id for entry in got if entry is not healthy]

    def test_is_handed_back_once_it_has_lasted_a_ceiling(self, binding):
        assert self._released(binding, _entry("old")) == ["old"]

    def test_is_kept_while_it_is_younger_than_the_ceiling(self, binding):
        assert self._released(binding, _entry("young", since=NOW - 600.0)) == []

    def test_a_lowered_dial_applies_to_a_hold_already_running(self, binding, monkeypatch):
        monkeypatch.setenv("KAME_MAX_HOLD", "600")
        assert self._released(binding, _entry("young", since=NOW - 601.0)) == ["young"]

    def test_a_dead_credential_is_never_handed_back(self, binding):
        assert self._released(binding, _entry("dead", status=DEAD)) == []

    def test_an_entry_the_host_could_not_use_stays_out(self, binding):
        assert self._released(binding, _entry("empty", key="")) == []

    def test_a_per_model_cooldown_for_the_model_asked_about_is_respected(self, binding):
        cooled = _entry("cooled", cooldowns={"m": NOW + 60.0})
        assert self._released(binding, cooled, model="m") == []
        assert self._released(binding, cooled, model="other") == ["cooled"]


class TestKamesOwnRecord:
    def _bench(self, **overrides):
        values = dict(credential_id="k0", provider="gemini", model="m",
                      reset_at=NOW + 24 * HOUR, recorded_at=NOW)
        values.update(overrides)
        return ledger_module.Bench(**values)

    def test_a_bench_is_read_through_the_ceiling(self):
        ledger = ledger_module.Ledger([self._bench()]).within(HOUR)
        (bench,) = ledger.benches()
        assert bench.until == NOW + HOUR
        assert not bench.holds(NOW + HOUR + 1.0)

    def test_the_fingerprint_stays_what_the_host_stored(self):
        (bench,) = ledger_module.Ledger([self._bench()]).within(HOUR).benches()
        assert bench.reset_at == NOW + 24 * HOUR

    def test_a_widened_bench_is_bounded_too(self):
        wide = self._bench(reset_at=NOW + 600.0, extended_to=NOW + 5 * HOUR)
        (bench,) = ledger_module.Ledger([wide]).within(HOUR).benches()
        assert bench.until == NOW + HOUR

    def test_a_bench_inside_the_ceiling_is_unchanged(self):
        short = self._bench(reset_at=NOW + 60.0)
        (bench,) = ledger_module.Ledger([short]).within(HOUR).benches()
        assert bench == short

    def test_nothing_about_the_ceiling_is_stored(self):
        (bench,) = ledger_module.Ledger([self._bench()]).within(HOUR).benches()
        assert "ceiling_at" not in bench.to_dict()
        assert ledger_module.Bench.from_dict(bench.to_dict()).until == NOW + 24 * HOUR
