"""1.8.0.0, decisions/0006-1800-one-pool-three-profiles.md: one pool-health
file, read and written by every Hermes profile that shares it.

The owner runs three profiles (``base``, ``profiles/k``, ``profiles/lo1``)
whose ``.env`` files hold the SAME seventeen physical keys. Before this,
``core.carousel.Carousel`` kept its health memory entirely in the process
that constructed it, so ``base`` benching a key for twenty seconds was
invisible to ``k`` calling the same key the same second — both refused, both
learned the same wrong lesson independently — and a restart threw every hold
away, so the next turn re-spent calls on keys the previous process already
knew were out.

``core.shared_health.SharedHealth`` is the fix: a file outside any one
profile's own ``plugin-data``, at the root every profile can reach, that
``Carousel.select``/``healthy_count``/``next_recovery_seconds`` read
*alongside* local memory and that ``Carousel.mark`` writes to after every
outcome. Two layers, tested separately:

* ``TestReconciliationAtTheFileLevel`` — ``SharedHealth`` on its own, no
  ``Carousel`` involved: does the file itself resolve two writers correctly?
* Everything else — ``Carousel`` wired to a real (temp-file) ``SharedHealth``,
  the same style every other ``test_v1_8_0_0_*`` module uses to test
  ``mark``/``select`` as the pure functions they are.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_8_0_0_shared_health_under_test"


def _load_package():
    """Import the plugin as a package, the way the Hermes loader does.

    Self-contained, like every other test module in this suite: its own
    ``PACKAGE`` name, its own load, so this file can be read and run without
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
shared_health = importlib.import_module(f"{PACKAGE}.core.shared_health")
quota_mod = importlib.import_module(f"{PACKAGE}.core.quota")

Carousel = carousel_mod.Carousel
fingerprint = carousel_mod.fingerprint
SharedHealth = shared_health.SharedHealth
QuotaScope = quota_mod.QuotaScope

NOW = 1_000_000.0
HOUR = 3600.0
IDENTITY = "gemini:gemini-3.7-flash"


def _store(path: Path, profile: str = "base", *, active: bool = True) -> SharedHealth:
    """A ``SharedHealth`` with no dependence on the environment or ``HERMES_HOME``.

    Every test controls ``active`` and ``profile`` directly instead of relying
    on ``KAME_SHARE_POOL_HEALTH``/``HERMES_HOME`` — the switch's own env-var
    parsing is exercised separately, in ``TestTheEnvironmentSwitch``.
    """
    return SharedHealth(path=path, profile=profile, enabled_fn=lambda: active)


# --- 1. reconciliation, at the file level, no Carousel involved -------------


class TestReconciliationAtTheFileLevel:
    def test_a_write_is_readable_back_and_carries_the_writer_profile(self, tmp_path):
        path = tmp_path / "pool-health.json"
        store = _store(path, "k")
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=NOW + 20, kind="per_minute", at=NOW)
        until, at = store.model_entry(IDENTITY, "key:aaa", now=NOW + 1)
        assert until == NOW + 20
        assert at == NOW
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["model"][IDENTITY]["key:aaa"]["profile"] == "k"

    def test_the_newest_event_wins_even_when_it_is_written_first(self, tmp_path):
        # decisions/0006: "evento mais recente vence, pelo instante do evento,
        # não pela ordem de escrita" — write the NEWER event first, then try
        # to overwrite it with an OLDER one, and confirm the older write is
        # refused.
        path = tmp_path / "pool-health.json"
        store = _store(path, "base")
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=0.0, kind="", at=NOW + 500)
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=HOUR, kind="daily", at=NOW)
        until, at = store.model_entry(IDENTITY, "key:aaa", now=NOW + 501)
        assert (until, at) == (0.0, NOW + 500)

    def test_a_genuinely_newer_event_always_overwrites_an_older_one(self, tmp_path):
        path = tmp_path / "pool-health.json"
        store = _store(path, "base")
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=HOUR, kind="daily", at=NOW)
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=0.0, kind="", at=NOW + 500)
        until, at = store.model_entry(IDENTITY, "key:aaa", now=NOW + 501)
        assert (until, at) == (0.0, NOW + 500)

    def test_account_scope_and_model_scope_do_not_collide(self, tmp_path):
        path = tmp_path / "pool-health.json"
        store = _store(path, "base")
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=20.0, kind="per_minute", at=NOW)
        store.record(scope="account", subject="gemini", fingerprint_key="key:aaa", until=HOUR, kind="usage_limit_reached", at=NOW)
        assert store.model_entry(IDENTITY, "key:aaa", now=NOW + 1)[0] == 20.0
        assert store.account_entry("gemini", "key:aaa", now=NOW + 1)[0] == HOUR


# --- 2. cross-profile visibility, through Carousel ---------------------------


class TestCrossProfileVisibility:
    def test_a_bench_in_one_profile_is_seen_by_selects_healthy_count_and_eta_in_another(self, tmp_path):
        path = tmp_path / "pool-health.json"
        car_a = Carousel(shared_health_store=_store(path, "base"))
        car_b = Carousel(shared_health_store=_store(path, "k"))
        keys = ["key-A", "key-B"]

        car_a.mark(IDENTITY, "key-A", ok=False, delay=HOUR, kind="daily", now=NOW, stated=True)

        # car_b has never seen key-A locally at all, yet avoids it.
        chosen, status = car_b.select(IDENTITY, keys, now=NOW + 1)
        assert chosen == "key-B"
        assert status == "SUCCESS"
        assert car_b.healthy_count(IDENTITY, keys, now=NOW + 1) == 1
        eta = car_b.next_recovery_seconds(IDENTITY, ["key-A"], now=NOW + 1)
        assert eta is not None and eta > HOUR - 100

    def test_a_success_in_one_profile_releases_a_bench_another_profile_holds(self, tmp_path):
        path = tmp_path / "pool-health.json"
        car_a = Carousel(shared_health_store=_store(path, "base"))
        car_b = Carousel(shared_health_store=_store(path, "k"))

        car_a.mark(IDENTITY, "key-A", ok=False, delay=HOUR, kind="daily", now=NOW, stated=True)
        assert car_b.healthy_count(IDENTITY, ["key-A"], now=NOW + 1) == 0

        # car_b learns, independently, that key-A actually works.
        car_b.mark(IDENTITY, "key-A", ok=True, now=NOW + 2)

        # car_a's own LOCAL memory still says key-A is benched until NOW+3600
        # — it never called mark() again — yet it now reads the key as
        # healthy, because car_b's success is a NEWER event than car_a's own
        # bench (NOW+2 > NOW), so it is believed outright rather than merely
        # maxed against the stale local hold.
        assert car_a.healthy_count(IDENTITY, ["key-A"], now=NOW + 3) == 1
        chosen, status = car_a.select(IDENTITY, ["key-A"], now=NOW + 3)
        assert chosen == "key-A"
        assert status == "SUCCESS"

    def test_a_newer_mark_written_after_an_older_one_still_wins(self, tmp_path):
        path = tmp_path / "pool-health.json"
        car_a = Carousel(shared_health_store=_store(path, "base"))
        car_b = Carousel(shared_health_store=_store(path, "k"))

        # car_b's event happens LATER in (virtual) time but is written FIRST.
        car_b.mark(IDENTITY, "key-A", ok=True, now=NOW + 500)
        # car_a's event happened EARLIER but is written SECOND.
        car_a.mark(IDENTITY, "key-A", ok=False, delay=HOUR, kind="daily", now=NOW, stated=True)

        fresh = Carousel(shared_health_store=_store(path, "base"))
        assert fresh.healthy_count(IDENTITY, ["key-A"], now=NOW + 501) == 1

    def test_an_account_wide_refusal_shared_by_one_profile_reaches_a_sibling_model_in_another(self, tmp_path):
        path = tmp_path / "pool-health.json"
        car_a = Carousel(shared_health_store=_store(path, "base"))
        car_b = Carousel(shared_health_store=_store(path, "k"))

        car_a.mark(
            "openai-codex:gpt-5.6-luna", "key-A", ok=False, delay=HOUR,
            kind="usage_limit_reached", now=NOW, stated=True, scope=QuotaScope.ACCOUNT,
        )
        # car_b has never touched THIS identity at all — a sibling model of
        # the same provider — yet the account-wide hold still applies.
        assert car_b.healthy_count("openai-codex:gpt-6-astra", ["key-A"], now=NOW + 1) == 0

    def test_profiles_with_disjoint_fingerprints_do_not_affect_each_other(self, tmp_path):
        path = tmp_path / "pool-health.json"
        car_a = Carousel(shared_health_store=_store(path, "base"))
        car_b = Carousel(shared_health_store=_store(path, "k"))

        car_a.mark(IDENTITY, "key-ONLY-A", ok=False, delay=HOUR, kind="daily", now=NOW, stated=True)

        assert car_b.healthy_count(IDENTITY, ["key-ONLY-B"], now=NOW + 1) == 1
        chosen, status = car_b.select(IDENTITY, ["key-ONLY-B"], now=NOW + 1)
        assert (chosen, status) == ("key-ONLY-B", "SUCCESS")


# --- 3. restart ---------------------------------------------------------------


class TestRestart:
    def test_a_fresh_carousel_over_the_same_file_still_sees_a_live_hold(self, tmp_path):
        path = tmp_path / "pool-health.json"
        original = Carousel(shared_health_store=_store(path, "base"))
        original.mark(IDENTITY, "key-A", ok=False, delay=HOUR, kind="daily", now=NOW, stated=True)

        restarted = Carousel(shared_health_store=_store(path, "base"))
        assert restarted.healthy_count(IDENTITY, ["key-A"], now=NOW + 1) == 0
        eta = restarted.next_recovery_seconds(IDENTITY, ["key-A"], now=NOW + 1)
        assert eta is not None and eta > 0

    def test_a_fresh_carousel_does_not_see_a_hold_that_has_already_expired(self, tmp_path):
        path = tmp_path / "pool-health.json"
        original = Carousel(shared_health_store=_store(path, "base"))
        original.mark(IDENTITY, "key-A", ok=False, delay=HOUR, kind="daily", now=NOW, stated=True)

        restarted = Carousel(shared_health_store=_store(path, "base"))
        # Long past the hour the bench was for.
        assert restarted.healthy_count(IDENTITY, ["key-A"], now=NOW + HOUR + 100) == 1


# --- 4. degrades to local-only, never raises ---------------------------------


class TestDegradesToLocalOnly:
    def test_a_corrupt_file_is_ignored(self, tmp_path):
        path = tmp_path / "pool-health.json"
        path.write_text("{not valid json at all", encoding="utf-8")
        store = _store(path, "base")
        assert store.model_entry(IDENTITY, "key:aaa", now=NOW) == (0.0, 0.0)

    def test_an_unknown_schema_version_is_ignored(self, tmp_path):
        path = tmp_path / "pool-health.json"
        path.write_text(
            json.dumps({
                "schema": shared_health.SCHEMA + 999,
                "model": {IDENTITY: {"key:aaa": {"until": NOW + 999, "kind": "daily", "at": NOW, "profile": "future"}}},
            }),
            encoding="utf-8",
        )
        store = _store(path, "base")
        assert store.model_entry(IDENTITY, "key:aaa", now=NOW) == (0.0, 0.0)

    def test_a_file_that_vanishes_between_reads_is_read_as_nothing_shared(self, tmp_path):
        path = tmp_path / "pool-health.json"
        store = _store(path, "base")
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=NOW + 999, kind="daily", at=NOW)
        assert store.model_entry(IDENTITY, "key:aaa", now=NOW + 1)[0] == NOW + 999

        path.unlink()
        # Past the read-cache window so the vanished file is actually re-stat'd.
        assert store.model_entry(IDENTITY, "key:aaa", now=NOW + 10) == (0.0, 0.0)

    def test_a_directory_that_cannot_be_created_does_not_raise_on_write(self, tmp_path):
        # A file standing where a directory needs to be makes ``mkdir`` fail
        # portably, without OS-specific permission bits that Windows often
        # ignores for directories anyway.
        blocker = tmp_path / "not_a_directory"
        blocker.write_text("x", encoding="utf-8")
        path = blocker / "pool-health.json"
        store = _store(path, "base")
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:aaa", until=NOW + 20, kind="per_minute", at=NOW)
        assert store.model_entry(IDENTITY, "key:aaa", now=NOW + 1) == (0.0, 0.0)

    def test_carousel_mark_and_select_never_raise_when_the_shared_write_is_impossible(self, tmp_path):
        blocker = tmp_path / "not_a_directory"
        blocker.write_text("x", encoding="utf-8")
        path = blocker / "pool-health.json"
        car = Carousel(shared_health_store=_store(path, "base"))

        applied = car.mark(IDENTITY, "key-A", ok=False, delay=20.0, kind="per_minute", now=NOW)
        assert applied == 20.0
        chosen, status = car.select(IDENTITY, ["key-A", "key-B"], now=NOW + 1)
        assert status in ("SUCCESS", "EXHAUSTED")


# --- 5. never a raw key -------------------------------------------------------


class TestNeverARawKey:
    def test_the_file_never_contains_anything_that_looks_like_the_raw_key(self, tmp_path):
        path = tmp_path / "pool-health.json"
        car = Carousel(shared_health_store=_store(path, "base"))
        secret = "sk-THIS-IS-A-VERY-REAL-LOOKING-SECRET-1234567890"

        car.mark(IDENTITY, secret, ok=False, delay=20.0, kind="per_minute", now=NOW)

        raw_bytes = path.read_bytes()
        assert secret.encode("utf-8") not in raw_bytes
        assert b"sk-THIS" not in raw_bytes
        assert b"SECRET" not in raw_bytes
        # The fingerprint IS present — proof the file actually recorded the
        # event rather than silently writing nothing.
        assert fingerprint(secret).encode("utf-8") in raw_bytes


# --- 6. the switch --------------------------------------------------------------


class TestTheEnvironmentSwitch:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " on "])
    def test_true_like_values_enable(self, monkeypatch, value):
        monkeypatch.setenv(shared_health.ENV_ENABLE, value)
        assert shared_health.enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe", ""])
    def test_everything_else_disables(self, monkeypatch, value):
        monkeypatch.setenv(shared_health.ENV_ENABLE, value)
        assert shared_health.enabled() is False

    def test_completely_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(shared_health.ENV_ENABLE, raising=False)
        assert shared_health.enabled() is False


class TestTheSwitchOff:
    def test_off_creates_no_file(self, tmp_path):
        path = tmp_path / "pool-health.json"
        store = SharedHealth(path=path, profile="base", enabled_fn=lambda: False)
        car = Carousel(shared_health_store=store)

        car.mark(IDENTITY, "key-A", ok=False, delay=20.0, kind="per_minute", now=NOW)

        assert not path.exists()

    def test_off_never_reads_a_file_that_already_exists(self, tmp_path):
        path = tmp_path / "pool-health.json"
        path.write_text(
            json.dumps({
                "schema": shared_health.SCHEMA,
                "model": {IDENTITY: {fingerprint("key-A"): {
                    "until": NOW + 999999, "kind": "daily", "at": NOW, "profile": "other",
                }}},
                "account": {},
            }),
            encoding="utf-8",
        )
        before = path.stat().st_mtime
        store = SharedHealth(path=path, profile="base", enabled_fn=lambda: False)
        car = Carousel(shared_health_store=store)

        chosen, status = car.select(IDENTITY, ["key-A"], now=NOW + 1)
        assert (chosen, status) == ("key-A", "SUCCESS")
        assert path.stat().st_mtime == before


# --- 7. pruning and the entry cap --------------------------------------------


class TestPruningAndCap:
    def test_entries_older_than_the_prune_window_are_dropped_on_the_next_write(self, tmp_path):
        path = tmp_path / "pool-health.json"
        store = _store(path, "base")
        store.record(scope="model", subject=IDENTITY, fingerprint_key="key:old", until=0.0, kind="", at=NOW)
        # A write more than PRUNE_AFTER_S later prunes the old entry.
        store.record(
            scope="model", subject="other:model", fingerprint_key="key:new",
            until=NOW + shared_health.PRUNE_AFTER_S + 50, kind="daily",
            at=NOW + shared_health.PRUNE_AFTER_S + 10,
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        assert IDENTITY not in document["model"]
        assert "key:new" in document["model"]["other:model"]

    def test_the_entry_cap_evicts_the_oldest_events_first(self, tmp_path, monkeypatch):
        path = tmp_path / "pool-health.json"
        monkeypatch.setattr(shared_health, "MAX_ENTRIES", 5)
        store = _store(path, "base")
        for i in range(8):
            store.record(
                scope="model", subject=f"identity-{i}", fingerprint_key="key:x",
                until=NOW + 10, kind="per_minute", at=NOW + i,
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        total = sum(len(rows) for rows in document["model"].values())
        assert total == 5
        assert "identity-7" in document["model"]
        assert "identity-6" in document["model"]
        assert "identity-0" not in document["model"]
        assert "identity-2" not in document["model"]


# --- 8. concurrency -----------------------------------------------------------


class TestConcurrency:
    def test_concurrent_writers_and_readers_never_crash_or_corrupt_the_file(self, tmp_path):
        """Real processes are the honest simulation of three Hermes profiles,
        but threads hammering the file lock alone (no in-process lock shared
        between them — every thread builds its own ``SharedHealth``) already
        exercise the cross-process path: :func:`shared_health._acquire_file_
        lock` is the only thing serialising these writers.
        """
        path = tmp_path / "pool-health.json"
        errors = []

        def hammer(writer_id: int) -> None:
            store = _store(path, f"writer-{writer_id}")
            try:
                for i in range(20):
                    store.record(
                        scope="model", subject=IDENTITY, fingerprint_key="key:aaa",
                        until=NOW + i, kind="per_minute", at=NOW + writer_id * 1000 + i,
                    )
            except Exception as exc:  # pragma: no cover - asserted on below
                errors.append(exc)

        def read_repeatedly() -> None:
            store = _store(path, "reader")
            try:
                for _ in range(40):
                    store.model_entry(IDENTITY, "key:aaa", now=NOW + 2_000_000)
            except Exception as exc:  # pragma: no cover - asserted on below
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(6)]
        threads += [threading.Thread(target=read_repeatedly) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors

        # Never corrupted: it parses, whatever value it landed on.
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema"] == shared_health.SCHEMA

        # The newest event of the whole test, from a store none of the storm
        # threads touch, still wins — proving reconciliation survived the
        # contention rather than merely "some value is present".
        winner = _store(path, "winner")
        winner.record(
            scope="model", subject=IDENTITY, fingerprint_key="key:aaa",
            until=NOW + 555555, kind="daily", at=NOW + 10_000_000,
        )
        until, at = winner.model_entry(IDENTITY, "key:aaa", now=NOW + 10_000_001)
        assert (until, at) == (NOW + 555555, NOW + 10_000_000)
