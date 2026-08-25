# v1.2.2 — the pool as a mirror of the config, not its archive.
#
# Fixed and pinned here:
#
#   1. A pool row that holds a comma-joined list must never reach the
#      dispatcher as a key — the whole list sent whole is the malformed
#      credential the multikey split exists to replace (the `key:d48bbb`
#      incident, state.json on 2026-08-24).
#   2. The carousel is derived from what the config resolves to *now*: a
#      key edited out of the list stops being selected instead of being
#      retried into an auth failure and quarantined forever.
#   3. A credential the carousel only ever heard about through mark() is
#      never handed out, and retires on its own.
#   4. One physical key declared in two config blocks counts once.
#   5. A pool of one keeps the cooldown a provider asked for. The host caps
#      a sole credential at 60s; KAME does not, because 1.1.3 already drops
#      the only cooldowns that a pool of one makes meaningless.

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_122_under_test"


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
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
dispatch_mod = importlib.import_module(f"{PACKAGE}.dispatch_binding")
multikey_mod = importlib.import_module(f"{PACKAGE}.core.multikey")
settings_mod = importlib.import_module(f"{PACKAGE}.settings")
state_mod = importlib.import_module(f"{PACKAGE}.state")
core_mod = importlib.import_module(f"{PACKAGE}.core")

Carousel = carousel_mod.Carousel
candidates = dispatch_mod.candidates

ID = "nvidia:moonshotai/kimi-k3"


class FakeEntry:
    """The one attribute candidates() reads, plus a marker for splitting."""

    def __init__(self, key, source="manual"):
        self.runtime_api_key = key
        self.source = source


class FakeAgent:
    def __init__(self, api_key="", pool=None):
        self.api_key = api_key
        self._credential_pool = pool


class FakePoolEntries:
    def __init__(self, entries):
        self._entries = list(entries)

    def entries(self):
        return list(self._entries)


# --- 1. The blob is never a candidate ------------------------------------


KEY1 = "sk-test-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
KEY2 = "sk-test-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
KEY3 = "sk-test-cccccccccccccccccccccccccccccccc"


def test_comma_row_in_pool_yields_the_parts_not_the_list():
    pool = FakePoolEntries([FakeEntry(f"{KEY1},{KEY2}"), FakeEntry(KEY3)])
    keys, _ = candidates(FakeAgent(pool=pool))
    assert sorted(keys) == sorted([KEY1, KEY2, KEY3])
    assert all("," not in k for k in keys)


def test_carousel_pool_never_learns_a_comma_joined_key():
    c = Carousel()
    candidates_keys, _ = candidates(
        FakeAgent(pool=FakePoolEntries([FakeEntry(f"{KEY1},{KEY2}")]))
    )
    c.select(ID, candidates_keys)
    snap = c.snapshot()[ID]
    assert snap["keys"] == 2


# --- 2. The pool mirrors the config --------------------------------------


GRACE = carousel_mod.MIRROR_GRACE_S


def test_key_removed_from_config_is_dropped_from_the_pool():
    c = Carousel()
    t = 1_000.0
    c.select(ID, [KEY1, KEY2], now=t)
    # The user edits the list down to one key. The dropped row is not deleted
    # on sight — one call is not evidence — but nothing offers it again.
    c.select(ID, [KEY1], now=t + GRACE + 1)
    key, _status = c.select(ID, [KEY1], now=t + GRACE + 2)
    assert key == KEY1
    assert c.snapshot(now=t + GRACE + 2)[ID]["keys"] == 1


def test_a_key_absent_for_one_call_keeps_its_cooldown():
    # Two agents, one provider:model, different lists — the second one must
    # not erase what the first one learned.
    c = Carousel()
    t = 1_000.0
    c.select(ID, [KEY1, KEY2], now=t)
    c.mark(ID, KEY2, ok=False, delay=300.0, kind="per_minute", now=t)
    c.select(ID, [KEY1], now=t + 1)
    snap = c.snapshot(now=t + 2)[ID]
    assert snap["keys"] == 2 and snap["resting"] == 1


def test_an_invalid_key_edited_out_stops_being_counted_invalid():
    # The panel's "1 key refused" must clear when the key is replaced, which
    # is the whole complaint behind this release.
    c = Carousel()
    t = 1_000.0
    c.select(ID, [KEY1, KEY2], now=t)
    c.mark(ID, KEY2, ok=False, delay=3600.0, kind="auth", now=t)
    assert c.snapshot(now=t)[ID]["invalid"] == 1
    c.select(ID, [KEY1, KEY3], now=t + GRACE + 1)
    c.select(ID, [KEY1, KEY3], now=t + GRACE + 2)
    snap = c.snapshot(now=t + GRACE + 2)[ID]
    assert snap["invalid"] == 0
    assert snap["keys"] == 2


def test_select_with_empty_key_list_updates_nothing():
    c = Carousel()
    t = 1_000.0
    c.select(ID, [KEY1, KEY2], now=t)
    key, status = c.select(ID, [], now=t + GRACE + 1)
    assert key is None and status == "EMPTY"
    # An empty candidate set cannot be evidence: the host may have failed to
    # load the pool this call. The pool is left alone.
    assert c.snapshot(now=t + GRACE + 1)[ID]["keys"] == 2


# --- 3. A key only ever marked is never handed out ------------------------


GHOST = "sk-test-ghost" + "0" * 28


def test_mark_on_an_unseen_key_never_makes_it_selectable():
    c = Carousel()
    t = 1_000.0
    # mark() records the outcome — 1.1.3 depends on that — but select() only
    # ever chooses from the candidates it was given, so the row is unreachable.
    c.mark(ID, GHOST, ok=False, delay=60.0, now=t)
    assert c.select(ID, [], now=t) == (None, "EMPTY")
    for _ in range(5):
        key, _status = c.select(ID, [KEY1], now=t)
        assert key == KEY1


def test_a_key_only_ever_marked_retires_itself():
    c = Carousel()
    t = 1_000.0
    c.mark(ID, GHOST, ok=False, delay=60.0, now=t)
    c.select(ID, [KEY1], now=t + GRACE + 1)
    assert c.snapshot(now=t + GRACE + 1)[ID]["keys"] == 1


def test_mark_on_a_seen_key_still_learns():
    c = Carousel()
    c.select(ID, [KEY1])
    c.mark(ID, KEY1, ok=False, delay=60.0)
    assert c.snapshot()[ID]["keys"] == 1


# --- 4. One physical key, one row ----------------------------------------


def test_same_key_declared_twice_counts_once():
    pool = FakePoolEntries(
        [FakeEntry(KEY1), FakeEntry(f"{KEY1},{KEY2}"), FakeEntry(KEY2)]
    )
    keys, _ = candidates(FakeAgent(pool=pool))
    assert sorted(keys) == sorted([KEY1, KEY2])


# --- 5. A pool of one keeps the cooldown that was asked for ---------------


def test_sole_key_keeps_a_provider_stated_cooldown():
    # The host caps a sole credential at 60s. KAME must not: an hour-long
    # quota refusal retried every minute spends the quota it is waiting for.
    c = Carousel()
    c.select(ID, [KEY1])
    applied = c.mark(ID, KEY1, ok=False, delay=3600.0, kind="daily")
    assert applied == 3600.0


def test_sole_key_keeps_no_routing_rest_at_all():
    # The cooldowns a pool of one makes meaningless are dropped one layer up,
    # by 1.1.3's rule, and dropped to nothing rather than to sixty seconds.
    c = Carousel()
    applied = dispatch_mod._rest_unless_it_is_the_only_one(
        c, ID, [KEY1], KEY1, 30.0, "timeout"
    )
    assert applied == 0.0


def test_sole_key_is_returned_exhausted_not_dropped():
    c = Carousel()
    c.select(ID, [KEY1])
    c.mark(ID, KEY1, ok=False, delay=3600.0)
    key, status = c.select(ID, [KEY1])
    assert key == KEY1
    assert status == "EXHAUSTED"


# --- 6. the config file that was edited after Hermes read it --------------


class FakeCtx:
    """A host config surface that can change under the plugin's feet."""

    def __init__(self, values):
        self.values = dict(values)

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def _reset_settings():
    settings_mod.forget()
    for name in list(os.environ):
        if name.startswith("KAME_"):
            del os.environ[name]


@pytest.fixture(autouse=False)
def clean_settings():
    _reset_settings()
    yield
    _reset_settings()


def test_an_untouched_config_reports_no_drift(clean_settings):
    ctx = FakeCtx({settings_mod.DAILY_COOLDOWN: 900})
    settings_mod.load(ctx)
    assert settings_mod.pending_restart(now=10_000.0) == ()


def test_an_edit_after_registration_is_named(clean_settings):
    ctx = FakeCtx({settings_mod.DAILY_COOLDOWN: 900})
    settings_mod.load(ctx)
    settings_mod.pending_restart(now=10_000.0)
    ctx.values[settings_mod.DAILY_COOLDOWN] = 1800
    # Throttled: the same second still answers from the cached reading.
    assert settings_mod.pending_restart(now=10_000.0) == ()
    assert settings_mod.pending_restart(now=10_100.0) == (settings_mod.DAILY_COOLDOWN,)
    # And the value in force is still the one that was read at registration.
    assert settings_mod.number(settings_mod.DAILY_COOLDOWN, 3600.0) == 900.0


def test_a_setting_the_environment_owns_is_never_named(clean_settings):
    ctx = FakeCtx({settings_mod.DAILY_COOLDOWN: 900})
    settings_mod.load(ctx)
    os.environ["KAME_DAILY_COOLDOWN"] = "120"
    ctx.values[settings_mod.DAILY_COOLDOWN] = 1800
    # Restarting would not apply the file either, so sending somebody to do it
    # would be a lie about what it buys.
    assert settings_mod.pending_restart(now=10_100.0) == ()


def test_a_host_without_a_config_surface_reports_nothing(clean_settings):
    settings_mod.load(object())
    assert settings_mod.pending_restart(now=10_100.0) == ()


def test_the_snapshot_carries_the_list(clean_settings):
    ctx = FakeCtx({settings_mod.DAILY_COOLDOWN: 900})
    settings_mod.load(ctx)
    settings_mod.pending_restart(now=0.0)
    assert isinstance(state_mod._settings_pending_restart(), list)


# --- 7. the setting a person reads, retitled ------------------------------


def test_the_silent_stream_timeout_is_titled_for_the_decision():
    row = settings_mod.describe(settings_mod.STREAM_SILENCE_TIMEOUT)
    assert row["title"] == "Wait for the first token"


def test_the_name_a_config_file_holds_did_not_move():
    assert settings_mod.STREAM_SILENCE_TIMEOUT == "stream_silence_timeout_seconds"
    assert settings_mod.canonical("silent_stream_patience_seconds") == (
        settings_mod.STREAM_SILENCE_TIMEOUT
    )
    assert settings_mod.env_name(settings_mod.STREAM_SILENCE_TIMEOUT) == (
        "KAME_STREAM_SILENCE_TIMEOUT"
    )


# --- 8. the release --------------------------------------------------------


def test_the_manifest_the_core_and_the_changelog_agree():
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f'version: "{core_mod.__version__}"' in manifest
    assert "## [1.2.2]" in changelog
    # 1.1.2's finding: the installer refuses anything above 1.
    assert "manifest_version: 1" in manifest


def test_the_snapshot_schema_and_the_panel_agree():
    panel = (PLUGIN_DIR / "desktop-ui" / "plugin.js").read_text(encoding="utf-8")
    assert state_mod.SCHEMA == 4
    assert "const SCHEMA = 4" in panel
