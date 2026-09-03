"""What 1.1.0 promised, pinned one promise per class.

1.0.10 answered a question nobody had asked. The user's own words for what was
wrong: ``/kame`` "esta aparecendo como se fosse uma mensagem de texto", the
status line shows health but "nao mostra ETA ou a rotação para deixar mais ao
vivo", and a turn nowhere near a length limit failed with "Response truncated
due to output length limit". Three complaints, three shapes of fix, and this
file is the shape of each one held in place.

1. The panel is text, because the surface it lands on renders text.
2. The snapshot is the door to a real panel, and it carries no key material.
3. The Gemini repair only ever fires on the exact corruption it was built for.
4. The Desktop half is a file with a contract, and the contract is checkable
   without a running Hermes.
5. The version is 1.1.0 everywhere it is written down.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
DESKTOP_PLUGIN = PLUGIN_DIR / "desktop-ui/plugin.js"
PACKAGE = "kame_v110_under_test"


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
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
core = importlib.import_module(f"{PACKAGE}.core")
desktop_ui = importlib.import_module(f"{PACKAGE}.desktop_ui")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
gemini_slots = importlib.import_module(f"{PACKAGE}.gemini_slots")
menu = importlib.import_module(f"{PACKAGE}.menu")
settings = importlib.import_module(f"{PACKAGE}.settings")
state = importlib.import_module(f"{PACKAGE}.state")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel

#: Key-shaped strings. Long enough to be recognisable in a haystack, which is
#: the point of the leak test below.
KEYS = [f"AIzaSyLEAKCANARY{i}" + "9" * 24 for i in range(3)]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    carousel.ENGINE.forget()
    state.clear()
    yield
    settings.forget()
    carousel.ENGINE.forget()
    state.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A Hermes home that is a directory in the test's own temp tree."""
    monkeypatch.setattr(state, "_hermes_home", lambda: tmp_path)
    return tmp_path


# --- 1. the panel is text ---------------------------------------------------


class TestThePanelIsTextBecauseTheRowIsText:
    """1.0.10 wrote markdown into a row that has no markdown renderer.

    Desktop matches a plugin command's reply with ``SLASH_STATUS_RE`` and
    paints it with ``LinkifiedText ... pretty={false}`` inside a
    ``whitespace-pre-wrap`` block, so a heading arrives as a hash and a table
    arrives as pipes. The user read exactly that and said so.
    """

    def _panel(self):
        binding = DispatchBinding(engine=Carousel())
        binding.calls = 7
        binding.rotations = 2
        return menu.MenuCommand(binding).handle()

    def test_no_line_is_a_markdown_heading(self):
        for line in self._panel().splitlines():
            assert not line.lstrip().startswith("#"), line

    def test_no_line_is_a_markdown_table(self):
        for line in self._panel().splitlines():
            assert "|" not in line, line

    def test_no_bold_markers_survive(self):
        assert "**" not in self._panel()

    def test_a_fact_is_a_label_and_a_value_on_one_line(self):
        assert "  calls: 7" in self._panel()
        assert "  rotations: 2" in self._panel()

    def test_it_points_at_the_panel_that_is_not_text(self):
        # The command's job since 1.1.0 is partly to say where the real one is.
        panel = self._panel()
        assert "Sidebar" in panel and "KAME" in panel

    def test_the_settings_listing_is_text_too(self):
        text = menu.MenuCommand().handle("get")
        for line in text.splitlines():
            assert "|" not in line
            assert not line.lstrip().startswith("#")

    def test_it_still_never_raises(self):
        assert menu.MenuCommand().handle("set nonsense 3").startswith("Unknown setting")
        assert "Usage:" in menu.MenuCommand().handle("set")


# --- 2. the snapshot --------------------------------------------------------


class TestTheSnapshotIsTheDoorToARealPanel:
    """A file, because the API door needs a restart the user has not agreed to.

    ``plugin_api.py`` is real but lives behind ``dashboard/plugin.json``
    discovery and an ``plugins.enabled`` entry; the desktop bridge already
    reads files, and a status readout is exactly the kind of thing a file is
    good at.
    """

    def test_the_document_says_what_it_is(self, home):
        document = state.snapshot(None)
        assert document["schema"] == state.SCHEMA
        assert document["version"] == core.__version__
        assert document["installed"] is False

    def test_a_binding_puts_its_counters_in(self, home):
        binding = DispatchBinding(engine=Carousel())
        binding.calls = 9
        binding.rotations = 4
        binding.waited_s = 12.5
        document = state.snapshot(binding)
        assert document["installed"] is True
        assert document["counters"]["calls"] == 9
        assert document["counters"]["rotations"] == 4
        assert document["counters"]["waited_s"] == 12.5

    def test_the_totals_sum_every_pool(self, home):
        carousel.ENGINE.select("gemini:a", KEYS)
        carousel.ENGINE.select("gemini:b", KEYS[:2])
        totals = state.snapshot(None)["totals"]
        assert totals["keys"] == 5
        assert totals["healthy"] == 5
        assert totals["soonest_s"] is None

    def test_an_eta_appears_only_when_nothing_is_usable(self, home):
        carousel.ENGINE.select("gemini:a", KEYS)
        for key in KEYS:
            carousel.ENGINE.mark("gemini:a", key, False, delay=90.0, kind="daily")
        totals = state.snapshot(None)["totals"]
        assert totals["healthy"] == 0
        assert 0 < totals["soonest_s"] <= 90.0

    def test_it_carries_no_key_material(self, home):
        carousel.ENGINE.select("gemini:a", KEYS)
        carousel.ENGINE.mark("gemini:a", KEYS[0], False, delay=30.0, kind="per_minute")
        document = json.dumps(state.snapshot(None))
        for key in KEYS:
            assert key not in document
            # Not even a prefix long enough to be worth anything.
            assert key[:12] not in document

    def test_publish_writes_where_the_desktop_half_looks(self, home):
        assert state.publish(None, force=True) is True
        path = home / "plugin-data" / state.PLUGIN_ID / "state.json"
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["schema"] == state.SCHEMA
        assert state.disabled_reason() == ""

    def test_publish_leaves_no_temporary_files_behind(self, home):
        state.publish(None, force=True)
        directory = home / "plugin-data" / state.PLUGIN_ID
        assert [p.name for p in directory.iterdir()] == ["state.json"]

    def test_a_temporary_a_killed_run_left_behind_is_swept(self, home):
        # 1.6.0.0. The write is atomic and unlinks its own temporary on every
        # exception — but ``os.replace`` is the last statement, and a process
        # killed before it reaches that line leaves the file with nobody left
        # to remove it. The owner's plugin-data directory had six, 40 KB in
        # total, one per mid-write restart, and nothing would ever have
        # cleaned them up.
        directory = home / "plugin-data" / state.PLUGIN_ID
        directory.mkdir(parents=True, exist_ok=True)
        orphan = directory / "tmpdeadbeef.tmp"
        orphan.write_text("{}", encoding="utf-8")
        os.utime(orphan, (time.time() - 3600, time.time() - 3600))

        state._swept = False
        assert state.publish(None, force=True) is True
        assert not orphan.exists()
        assert sorted(p.name for p in directory.iterdir()) == ["state.json"]

    def test_a_temporary_another_process_may_still_be_writing_is_left_alone(self, home):
        # The age threshold. A second Hermes writing its snapshot right now
        # is between ``mkstemp`` and ``os.replace``; deleting that file would
        # turn a tidy-up into the very failure it is cleaning up after.
        directory = home / "plugin-data" / state.PLUGIN_ID
        directory.mkdir(parents=True, exist_ok=True)
        live = directory / "tmpinflight.tmp"
        live.write_text("{}", encoding="utf-8")

        state._swept = False
        assert state.publish(None, force=True) is True
        assert live.exists()
        live.unlink()

    def test_nothing_but_a_snapshot_temporary_is_touched(self, home):
        directory = home / "plugin-data" / state.PLUGIN_ID
        directory.mkdir(parents=True, exist_ok=True)
        keepers = [directory / "state.json.bak", directory / "notes.tmp.json"]
        for keeper in keepers:
            keeper.write_text("{}", encoding="utf-8")
            os.utime(keeper, (time.time() - 3600, time.time() - 3600))

        state._swept = False
        assert state.publish(None, force=True) is True
        for keeper in keepers:
            assert keeper.exists(), keeper.name
            keeper.unlink()

    def test_an_unchanged_document_is_not_rewritten(self, home):
        assert state.publish(None, force=True) is True
        # Same numbers, one moment later: the only field that moved is the
        # clock, which is not a reason to write.
        assert state.publish(None) is False

    def test_a_changed_document_is_written_immediately(self, home):
        binding = DispatchBinding(engine=Carousel())
        assert state.publish(binding, force=True) is True
        binding.rotations += 1
        assert state.publish(binding) is True

    def test_force_writes_even_when_nothing_changed(self, home):
        assert state.publish(None, force=True) is True
        assert state.publish(None, force=True) is True

    def test_a_home_that_cannot_be_found_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(state, "_hermes_home", lambda: None)
        state.clear()
        assert state.publish(None, force=True) is False
        assert "home" in state.disabled_reason()

    def test_a_profile_home_is_preserved_for_isolation(self, tmp_path, monkeypatch):
        # 1.2.4: Each profile keeps its own plugin-data directory. Do NOT walk
        # back to the base home — that collapsed all profiles to the same
        # state.json and caused races between concurrent profiles.
        base = tmp_path / "hermes"
        profile = base / "profiles" / "work"
        profile.mkdir(parents=True)

        class Constants:
            @staticmethod
            def get_hermes_home():
                return str(profile)

        monkeypatch.setitem(sys.modules, "hermes_constants", Constants)
        assert state._hermes_home() == profile

    def test_every_setting_is_in_the_document(self, home):
        keys = {row["key"] for row in state.snapshot(None)["settings"]}
        assert keys == set(settings.ALL_FLAGS) | set(settings.ALL_NUMBERS)

    def test_a_setting_carries_its_provenance(self, home, monkeypatch):
        monkeypatch.setenv("KAME_LIVE_STATUS_DISABLED", "1")
        settings.forget()
        row = next(
            r for r in state.snapshot(None)["settings"] if r["key"] == settings.LIVE_STATUS_DISABLED
        )
        assert row["value"] is True
        assert "env" in row["source"] or "KAME_" in row["source"]


# --- 3. the Gemini repair ---------------------------------------------------


def _merging_original(event, model, indices):
    """A stand-in for the host adapter, with the host's bug in it.

    Reproduces ``translate_stream_event``'s slot key: part index, name and
    thought signature. Two parallel calls to one tool share all three, so they
    share a slot and their arguments are concatenated.
    """
    part = event["candidates"][0]["content"]["parts"][0]["functionCall"]
    slot = f"0:{part['name']}:"
    if slot not in indices:
        indices[slot] = {"index": len(indices), "id": f"call_host{len(indices)}"}
    known = indices[slot]
    call = SimpleNamespace(
        index=known["index"],
        id=known["id"],
        function=SimpleNamespace(name=part["name"], arguments=json.dumps(part["args"])),
    )
    return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[call]))])]


def _correct_original(event, model, indices):
    """The same adapter with the bug fixed — one slot per call."""
    part = event["candidates"][0]["content"]["parts"][0]["functionCall"]
    slot = f"{len(indices)}:{part['name']}:"
    indices[slot] = {"index": len(indices), "id": f"call_host{len(indices)}"}
    known = indices[slot]
    call = SimpleNamespace(
        index=known["index"],
        id=known["id"],
        function=SimpleNamespace(name=part["name"], arguments=json.dumps(part["args"])),
    )
    return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[call]))])]


def _arguments(chunks):
    return [
        getattr(getattr(call, "function", None), "arguments", "")
        for chunk in chunks
        for call in gemini_slots._tool_call_deltas(chunk)
    ]


class TestTheRepairFiresOnlyOnTheCorruption:
    """"Response truncated due to output length limit", on a short turn.

    Two parallel ``web_search`` calls arrive as the same part index with no
    thought signature, share one slot, and produce ``{"query":"A"}{"query":"B"}``
    — which Hermes cannot repair, so it substitutes ``{}``, reads the empty
    call as truncated, retries four times and reports a length limit. The
    repair is a post-process on the chunks the host already returned, with the
    narrowest rule that can describe the corruption.
    """

    def test_the_bug_is_reproduced_by_the_stand_in(self):
        indices = {}
        chunks = _merging_original({"candidates": [{"content": {"parts": [{"functionCall": {"name": "web_search", "args": {"query": "a"}}}]}}]}, "m", indices)
        chunks += _merging_original({"candidates": [{"content": {"parts": [{"functionCall": {"name": "web_search", "args": {"query": "b"}}}]}}]}, "m", indices)
        assert "".join(_arguments(chunks)) == '{"query": "a"}{"query": "b"}'

    def test_the_self_check_proves_the_repair_on_a_buggy_host(self):
        assert gemini_slots._self_check(_merging_original) == (True, "")

    def test_the_self_check_refuses_a_host_that_has_no_bug(self):
        ok, reason = gemini_slots._self_check(_correct_original)
        assert ok is False
        assert "apart" in reason or "own index" in reason

    def test_the_repair_gives_the_second_call_its_own_index_and_id(self):
        indices = {}
        first = _merging_original(gemini_slots._two_calls_event("a"), "m", indices)
        assert gemini_slots._repair(first, indices, True) == 0
        second = _merging_original(gemini_slots._two_calls_event("b"), "m", indices)
        assert gemini_slots._repair(second, indices, False) == 1
        calls = [
            call
            for chunk in [*first, *second]
            for call in gemini_slots._tool_call_deltas(chunk)
        ]
        assert len({call.index for call in calls}) == 2
        assert len({call.id for call in calls}) == 2

    def test_a_genuine_continuation_is_left_alone(self):
        # The real streaming shape: one object arriving in fragments. Neither
        # half parses on its own, so the rule cannot fire.
        indices = {}
        chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        tool_calls=[SimpleNamespace(index=0, id="call_x", function=SimpleNamespace(arguments='{"query":'))]
                    )
                )
            ]
        )
        assert gemini_slots._repair([chunk], indices, True) == 0
        chunk2 = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        tool_calls=[SimpleNamespace(index=0, id="call_x", function=SimpleNamespace(arguments='"a"}'))]
                    )
                )
            ]
        )
        assert gemini_slots._repair([chunk2], indices, False) == 0
        # The two fragments joined into the one object they always were.
        assert gemini_slots._stream_state(indices, False)["acc"][0] == '{"query":"a"}'

    def test_a_second_object_on_a_different_index_is_not_touched(self):
        indices = {}
        chunk = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        tool_calls=[
                            SimpleNamespace(index=0, id="a", function=SimpleNamespace(arguments='{"q":1}')),
                            SimpleNamespace(index=1, id="b", function=SimpleNamespace(arguments='{"q":2}')),
                        ]
                    )
                )
            ]
        )
        assert gemini_slots._repair([chunk], indices, True) == 0

    def test_a_new_stream_starts_from_a_clean_slate(self):
        indices = {}
        gemini_slots._repair(_merging_original(gemini_slots._two_calls_event("a"), "m", indices), indices, True)
        # The host creates the dict fresh per stream; reusing the same object
        # with `fresh=True` must not carry the previous stream's accumulator.
        indices.clear()
        second = _merging_original(gemini_slots._two_calls_event("b"), "m", indices)
        assert gemini_slots._repair(second, indices, True) == 0

    def test_the_setting_exists_and_defaults_to_on(self):
        assert settings.known(gemini_slots.SETTING)
        assert settings.is_on(gemini_slots.SETTING) is False
        assert settings.env_name(gemini_slots.SETTING) == "KAME_GEMINI_TOOL_CALL_FIX_DISABLED"

    def test_the_switch_stops_it(self, monkeypatch):
        monkeypatch.setenv("KAME_GEMINI_TOOL_CALL_FIX_DISABLED", "1")
        settings.forget()
        assert gemini_slots.apply() is False
        assert "off" in gemini_slots.report()["reason"] or "disabled" in gemini_slots.report()["reason"]

    def test_it_refuses_a_host_it_cannot_recognise(self, monkeypatch):
        # No Hermes in this interpreter, so the import inside apply() fails —
        # which is itself the case that must not raise.
        settings.forget()
        assert gemini_slots.apply() is False
        assert gemini_slots.report()["applied"] is False
        assert gemini_slots.report()["reason"]


# --- 4. the Desktop half ----------------------------------------------------


class TestTheDesktopHalfHasACheckableContract:
    """It cannot be unit-tested without an Electron renderer. It can be read.

    Everything below is a fact the file must satisfy for the chip to appear at
    all, and every one of them is a mistake that would otherwise be found by
    the user, after a deploy and a restart.
    """

    def _source(self):
        assert DESKTOP_PLUGIN.is_file(), "the Desktop half is missing"
        return DESKTOP_PLUGIN.read_text(encoding="utf-8")

    def test_it_imports_only_what_the_loader_allows(self):
        specifiers = set(re.findall(r"""from\s+['"]([^'"]+)['"]""", self._source()))
        assert specifiers <= {"@hermes/plugin-sdk", "react", "react/jsx-runtime", "react/jsx-dev-runtime"}

    def test_it_default_exports_a_plugin_the_loader_will_accept(self):
        source = self._source()
        assert "export default" in source
        assert re.search(r"register\s*\(\s*ctx\s*\)", source)

    def test_its_id_is_the_plugin_id(self):
        assert f"'{state.PLUGIN_ID}'" in self._source()

    def test_it_reads_the_schema_this_release_writes(self):
        match = re.search(r"const SCHEMA = (\d+)", self._source())
        assert match and int(match.group(1)) == state.SCHEMA

    def test_it_looks_where_the_python_half_writes(self):
        # 1.1.1 builds the directory once and joins the file names onto it,
        # because it now reads one file and writes another in the same place.
        source = self._source()
        assert "plugin-data/${PLUGIN_ID}" in source
        assert "/state.json" in source
        # And the Python half must agree about that shape.
        assert state.state_path() is None or state.state_path().parent.name == state.PLUGIN_ID

    def test_it_contributes_the_chip_the_page_and_the_way_in(self):
        source = self._source()
        assert "STATUSBAR_AREAS.right" in source
        assert "ROUTES_AREA" in source
        assert "SIDEBAR_NAV_AREA" in source
        assert "PALETTE_AREA" in source

    def test_the_countdown_is_recomputed_against_the_snapshot_age(self):
        # The whole complaint was that the wait looks frozen. A countdown that
        # only moves when the file is rewritten is a frozen countdown.
        source = self._source()
        assert "ageSeconds" in source
        assert re.search(r"value\s*-\s*ageSeconds", source)

    def test_it_disposes_its_timer(self):
        # A runtime plugin is hot-reloaded on every save; a timer left behind
        # would multiply on each one.
        assert "ctx.onDispose(startReading())" in self._source()


class TestTheDesktopHalfInstallsItself:
    """One install, one copy, no toggle.

    The file ships inside the package under ``desktop-ui/``, a name neither
    runtime door scans, and the Python half copies it into
    ``desktop-plugins/<name>/plugin.js`` -- the door that loads default-on.
    Shipping it at ``<name>/desktop/plugin.js`` instead would have installed
    it through the unified door, which caps ``defaultEnabled`` to false.
    """

    def test_it_lands_in_the_door_that_loads_default_on(self, home):
        assert desktop_ui.install() is True
        landed = home / "desktop-plugins" / state.PLUGIN_ID / "plugin.js"
        assert landed.is_file()
        assert landed.read_text(encoding="utf-8") == DESKTOP_PLUGIN.read_text(encoding="utf-8")

    def test_it_is_not_shipped_at_the_path_the_unified_door_scans(self):
        # `plugins/<name>/desktop/plugin.js` would be found and disabled.
        assert not (PLUGIN_DIR / "desktop" / "plugin.js").exists()

    def test_an_upgrade_replaces_an_older_copy(self, home):
        landed = home / "desktop-plugins" / state.PLUGIN_ID / "plugin.js"
        landed.parent.mkdir(parents=True)
        landed.write_text("// an older release\n", encoding="utf-8")
        assert desktop_ui.install() is True
        assert "an older release" not in landed.read_text(encoding="utf-8")

    def test_installing_twice_is_a_no_op_that_still_reports_success(self, home):
        assert desktop_ui.install() is True
        assert desktop_ui.install() is True
        assert desktop_ui.report()["installed"] is True
        assert desktop_ui.report()["reason"] == ""

    def test_it_leaves_no_temporary_file_behind(self, home):
        desktop_ui.install()
        directory = home / "desktop-plugins" / state.PLUGIN_ID
        assert [p.name for p in directory.iterdir()] == ["plugin.js"]

    def test_no_home_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(state, "_hermes_home", lambda: None)
        assert desktop_ui.install() is False
        assert "home" in str(desktop_ui.report()["reason"])

    def test_the_snapshot_carries_the_install_state(self, home):
        desktop_ui.install()
        assert state.snapshot(None)["desktop_ui"]["installed"] is True


# --- 5. the version ---------------------------------------------------------


class TestTheVersionSaysOneThing:
    """1.0.10 read as older than 1.0.9 to the person reading it, which is the
    only reader that matters for a version string in a panel."""

    def test_the_code_is_not_older_than_this_release(self):
        # A floor rather than an equality: this file describes 1.1.0, and a
        # later release moving the number on is not a regression in anything
        # it tests. The manifest still has to match the code exactly, below.
        assert tuple(int(part) for part in core.__version__.split(".")) >= (1, 1, 0)

    def test_the_manifest_agrees(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert f'version: "{core.__version__}"' in manifest

    def test_the_manifest_offers_every_setting_kame_has(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        for key in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS):
            assert f"  {key}:" in manifest, key
