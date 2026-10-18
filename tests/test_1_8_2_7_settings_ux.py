"""1.8.2.7: the settings page a first-time user sees (notes/2026-10-16-settings-ux-review.md)."""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_ux_1827_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
settings = importlib.import_module(f"{PACKAGE}.settings")
events = importlib.import_module(f"{PACKAGE}.core.events")
state = importlib.import_module(f"{PACKAGE}.state")


def rows():
    return {row["key"]: row for row in settings.describe_all()}


def test_four_settings_are_visible_to_every_user_and_the_rest_are_advanced():
    advanced = [key for key, row in rows().items() if row["advanced"]]
    assert len(advanced) == 19   # 1.8.2.8 (owner): 'Give up after' joined them
    visible_main = [key for key, row in rows().items()
                    if not row["advanced"] and row["group"] != "off"]
    # switch-after, stop-waiting, speed boost, silent-key skip (owner, 2026-10-16/17:
    # the fallback list is automatic and every key is its own account)
    # 1.8.2.8 (decisions/0028): three choices replace guessed numbers; each
    # number is shown only under its "my time" answer (depends_on).
    assert set(visible_main) == {
        settings.MODEL_SWITCH, settings.FAILOVER_AFTER,
        settings.HEDGE_MODE, settings.FIRST_TOKEN_WAIT, settings.STREAM_SILENCE_TIMEOUT,
        settings.REPLY_NOTICE,
    }
    assert rows()[settings.FAILOVER_AFTER]["depends_on"]["value"] == "timed"
    assert rows()[settings.STREAM_SILENCE_TIMEOUT]["depends_on"]["key"] == settings.FIRST_TOKEN_WAIT


def test_the_keys_shelf_always_shows_how_to_add_keys():
    groups = {g["id"]: g for g in settings.groups()}
    assert groups["keyring"]["shows_keys"] is True
    assert [g["id"] for g in settings.groups() if g["shows_keys"]] == ["keyring"]


def test_the_escape_switches_are_never_marked_advanced_they_have_their_own_folded_card():
    for key in settings.DISABLE_FLAGS:
        assert rows()[key]["advanced"] is False


def test_shelves_come_out_in_reading_order_and_only_one_shows_the_fallback_chain():
    groups = settings.groups()
    assert [g["id"] for g in groups] == ["extra", "speed", "keyring", "tuning", "off"]
    assert [g["id"] for g in groups if g["shows_fallback"]] == ["extra"]
    assert [g["id"] for g in groups if g["folded"]] == ["off"]


def test_every_title_is_short_and_the_two_cross_references_name_titles_that_exist():
    titles = {row["title"] for row in settings.describe_all()}
    assert all(len(t) <= 40 for t in titles)
    detail = rows()[settings.FAILOVER_AFTER]["detail"] + rows()[settings.MAX_TOTAL_WAIT]["help"]
    assert "Hand over to Hermes' fallback models after" not in detail
    assert "Never fall back to another model" not in detail
    assert "Ignore my fallback list" in titles


def test_the_long_text_is_kept_as_the_tooltip():
    row = rows()[settings.STREAM_SILENCE_TIMEOUT]
    assert len(row["help"]) < len(row["detail"])
    assert "120" in row["detail"]


def test_a_handover_is_a_planned_switch_not_a_failure():
    assert events.HANDOVER in events.GOOD_KINDS
    assert events.HANDOVER in events._KINDS


def test_the_fallback_view_states_are_in_plain_words():
    view = state._fallback_view([])
    assert view["state"] in {"no_chain", "off_by_flag", "off_by_zero", "on", "unknown"}
    assert view["why"].startswith("Switching:") or view["state"] == "unknown"
