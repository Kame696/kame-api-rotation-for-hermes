"""Three shelves instead of one list.

Nothing in the rotation loop changed in 1.2.0. What changed is the first thing
a person sees: twelve settings in one column, all rendered identically, reading
as twelve equal knobs. They are not equal, and the difference is the only thing
a first-time reader needs to know.

* One setting **adds** a behaviour and is off until somebody turns it on — the
  silent-key timeout. Its default of "off" is a real choice, not "KAME working
  normally", and it is the only one of which that is true.
* Two are **numbers** whose defaults are already right for the providers this
  plugin was built against.
* The other nine **take something away**. Every one is named ``*_disabled`` and
  every one hands a job back to Hermes. They are there so a person who suspects
  KAME of breaking their agent can prove it in one switch — which is worth
  having and is not worth browsing.

What is pinned here is that the split exists, that it is described where a
reader can see it, that the panel and ``/kame get`` agree about it because they
read it from the same place, and — the clause that matters most — that a
setting the table has never heard of still appears. The failure mode of every
hand-kept list is a knob added later that quietly stops showing up.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
MANIFEST = PLUGIN_DIR / "plugin.yaml"
DESKTOP_PLUGIN = PLUGIN_DIR / "desktop-ui" / "plugin.js"
PACKAGE = "kame_v120_under_test"


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


plugin = _load_package()
core = importlib.import_module(f"{PACKAGE}.core")
menu = importlib.import_module(f"{PACKAGE}.menu")
settings = importlib.import_module(f"{PACKAGE}.settings")
state = importlib.import_module(f"{PACKAGE}.state")

UI = DESKTOP_PLUGIN.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    yield
    settings.forget()


# --- 1. the shelves themselves ----------------------------------------------


class TestTheShelves:
    def test_the_optional_extra_is_alone_on_its_own_shelf(self):
        # The whole point of the split: the one setting that is off on a
        # correct install must not sit next to nine that are off because
        # turning them on breaks something.
        extra = [group for group in settings.groups() if group["id"] == "extra"]
        assert len(extra) == 1
        assert extra[0]["keys"] == [settings.STREAM_SILENCE_TIMEOUT]

    def test_the_optional_extra_really_is_off_by_default(self):
        assert settings.ALL_NUMBERS[settings.STREAM_SILENCE_TIMEOUT] == 0.0
        assert settings.number(
            settings.STREAM_SILENCE_TIMEOUT, settings.ALL_NUMBERS[settings.STREAM_SILENCE_TIMEOUT]
        ) == 0.0

    def test_every_escape_hatch_is_on_the_shelf_that_warns_about_it(self):
        for key in settings.ALL_FLAGS:
            assert settings.group_of(key) == "off", key

    def test_no_escape_hatch_leaked_onto_another_shelf(self):
        for group in settings.groups():
            if group["id"] == "off":
                continue
            for key in group["keys"]:
                assert key not in settings.ALL_FLAGS, key

    def test_the_tuning_shelf_holds_the_numbers_that_are_not_the_extra(self):
        tuning = {
            key
            for group in settings.groups()
            if group["id"] == "tuning"
            for key in group["keys"]
        }
        assert tuning == set(settings.ALL_NUMBERS) - {settings.STREAM_SILENCE_TIMEOUT}

    def test_a_setting_belongs_to_exactly_one_shelf(self):
        seen = [key for group in settings.groups() for key in group["keys"]]
        assert len(seen) == len(set(seen))

    def test_every_shelf_says_what_it_is_for(self):
        # A heading with no sentence under it is the list we already had, with
        # extra whitespace.
        for group in settings.groups():
            assert group["id"]
            assert group["title"]
            assert len(str(group["note"])) > 40

    def test_the_shelves_come_out_in_the_order_they_are_shown(self):
        assert [group["id"] for group in settings.groups()] == ["extra", "tuning", "off"]


# --- 2. nothing is lost -----------------------------------------------------


class TestNothingIsLost:
    def test_every_setting_is_on_a_shelf(self):
        shelved = {key for group in settings.groups() for key in group["keys"]}
        assert shelved == set(settings.ALL_FLAGS) | set(settings.ALL_NUMBERS)

    def test_a_setting_the_table_never_heard_of_is_shown_last_not_dropped(self, monkeypatch):
        # Simulates the release that adds a knob and forgets the table. The
        # forgotten knob has to be in the wrong place, never missing.
        forgotten = settings.STREAM_RESUME_LIMIT
        trimmed = tuple(
            (group, title, note, tuple(key for key in keys if key != forgotten))
            for group, title, note, keys in settings.GROUPS
        )
        monkeypatch.setattr(settings, "GROUPS", trimmed)
        rows = [row["key"] for row in settings.describe_all()]
        assert forgotten in rows
        assert sorted(rows) == sorted(list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS))
        assert rows[-1] == forgotten

    def test_a_setting_the_table_never_heard_of_lands_on_the_gentler_shelf(self):
        # Not "off": a knob on the wrong shelf is a small confusion, one filed
        # among the escape hatches reads as a warning it may not deserve.
        assert settings.group_of("a_setting_that_does_not_exist") == "tuning"

    def test_the_description_of_a_setting_carries_its_shelf(self):
        for row in settings.describe_all():
            assert row["group"] == settings.group_of(str(row["key"]))


# --- 3. the panel reads it from the plugin ----------------------------------


class TestThePanelIsToldRatherThanTaught:
    def test_the_snapshot_carries_the_shelves(self):
        document = state.snapshot()
        assert document["setting_groups"] == [
            dict(group) for group in settings.groups()
        ]

    def test_the_snapshot_still_survives_a_round_trip(self):
        document = state.snapshot()
        assert json.loads(json.dumps(document))["setting_groups"]

    def test_the_two_halves_agree_on_the_schema(self):
        assert state.SCHEMA == 3
        assert re.search(r"const SCHEMA = 3\b", UI)

    def test_the_panel_lays_the_screen_out_from_the_snapshot(self):
        # The grouping must not be spelled a second time in JavaScript, where
        # it could disagree with the plugin about which shelf a key is on.
        assert "settingCards(" in UI
        assert "snap.setting_groups" in UI
        assert "setting.group === group.id" in UI
        for group in settings.groups():
            assert f"'{group['id']}'" not in UI.replace("'_rest'", "")

    def test_the_panel_shows_a_setting_no_shelf_claimed(self):
        assert "'Other'" in UI
        assert "Not sorted into any of the groups above." in UI

    def test_the_panel_says_the_settings_are_optional_before_showing_any(self):
        assert "KAME works with none of these touched." in UI


# --- 4. the chat command says the same thing --------------------------------


class TestTheChatCommandAgrees:
    def test_it_prints_every_heading_and_its_sentence(self):
        text = menu.MenuCommand().handle("get")
        for group in settings.groups():
            assert str(group["title"]) in text
            # The note is wrapped, so the first few words are what can be
            # asserted without pinning the wrap width.
            opening = " ".join(str(group["note"]).split()[:4])
            assert opening in " ".join(text.split())

    def test_the_optional_extra_is_printed_before_the_escape_hatches(self):
        text = menu.MenuCommand().handle("get")
        first_flag = min(text.index(key) for key in settings.ALL_FLAGS)
        assert text.index(settings.STREAM_SILENCE_TIMEOUT) < first_flag

    def test_it_prints_every_setting(self):
        text = menu.MenuCommand().handle("get")
        for key in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS):
            assert key in text, key

    def test_it_still_prints_a_setting_no_shelf_claimed(self, monkeypatch):
        forgotten = settings.DAILY_COOLDOWN
        trimmed = tuple(
            (group, title, note, tuple(key for key in keys if key != forgotten))
            for group, title, note, keys in settings.GROUPS
        )
        monkeypatch.setattr(settings, "GROUPS", trimmed)
        monkeypatch.setattr(
            settings,
            "_GROUP_OF",
            {key: group for group, _t, _n, keys in trimmed for key in keys},
        )
        text = menu.MenuCommand().handle("get")
        assert forgotten in text
        assert "Other" in text

    def test_no_line_it_prints_is_wider_than_a_chat_bubble(self):
        text = menu.MenuCommand().handle("get")
        for line in text.splitlines():
            assert len(line) <= 78, line

    def test_the_wrapper_never_breaks_a_word(self):
        word = "stream_resume_limit"
        lines = menu._wrap(f"before {word} after", 12)
        assert word in lines
        assert " ".join(lines) == f"before {word} after"


# --- 5. the setting a person is most likely to read -------------------------


class TestTheSilentKeyTimeoutExplainsItself:
    def test_it_is_titled_for_what_it_does_not_for_what_it_is_called(self):
        row = settings.describe(settings.STREAM_SILENCE_TIMEOUT)
        assert row["title"] == "Give up on a silent key after"

    def test_the_help_says_what_zero_means(self):
        help_text = settings.explain(settings.STREAM_SILENCE_TIMEOUT).lower()
        assert "0" in help_text
        assert "120" in help_text

    def test_the_help_says_why_a_small_value_is_raised(self):
        help_text = settings.explain(settings.STREAM_SILENCE_TIMEOUT).lower()
        assert "5" in help_text

    def test_a_value_under_the_floor_is_still_refused_with_a_reason(self):
        value, why = settings.parse(settings.STREAM_SILENCE_TIMEOUT, "2")
        assert value is None
        assert "0" in why


# --- 6. the release ---------------------------------------------------------


class TestTheRelease:
    def test_the_manifest_and_the_core_agree(self):
        text = MANIFEST.read_text(encoding="utf-8")
        assert core.__version__ == "1.2.0"
        assert 'version: "1.2.0"' in text

    def test_the_manifest_version_the_installer_accepts_is_unchanged(self):
        # 1.1.2 found that Hermes' installer refuses anything above 1, and a
        # minor bump is exactly the sort of release that would move it.
        assert "manifest_version: 1" in MANIFEST.read_text(encoding="utf-8")

    def test_the_changelog_has_an_entry(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "## [1.2.0]" in changelog

    def test_nothing_the_reader_sees_is_written_in_another_language(self):
        # The plugin ships in English, headings and shelf notes included.
        accented = re.compile(r"[áàâãéêíóôõúçñ]")
        for group in settings.groups():
            for text in (group["title"], group["note"]):
                assert not accented.search(str(text)), text
