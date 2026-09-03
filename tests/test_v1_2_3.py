"""1.2.3 — the panel stopped rebuilding itself under the cursor.

Three complaints arrived together and turned out to be one defect with three
faces:

* the status chip "restarting" on its own,
* the Settings tab visibly refreshing over and over,
* a number typed into *Wait for the first token* reverting to ``0`` on Save.

The third is the one worth stating precisely, because the obvious suspect was
innocent. The save path is correct and this file proves it: ``control._apply``
parses the value, puts it in the process environment, and ``settings.effective``
reports it back immediately. Nothing about the backend loses a setting.

What lost it was ``desktop-ui/plugin.js``. Its ``h`` helper drops falsy children
before React sees the list, so a list holding a conditional element physically
grows and shrinks. React reconciles a **keyless** child list by position. The
settings cards sat at one index, and the instant Save was pressed a "Saving…"
paragraph appeared above them and pushed them to the next — where the previous
render had an element of a different type. React resolves that by unmounting the
old subtree and mounting a new one, so every ``NumberSetting`` was destroyed and
rebuilt mid-save, losing the ``useState`` holding what had just been typed and
the record that a write was in flight. The rebuilt field initialised from the
snapshot, which still carried ``0`` because the backend had not published yet.

The same file also handed ``$snapshot`` a brand-new object on every one-second
read — over a file the backend rewrites every twenty — and ``KamePage``
subscribed to the one-second clock for the benefit of a countdown that only
exists on the Overview tab. Together that is a settings form re-rendering once a
second for ever, which is the "refreshing" that was reported and the reason a
half-typed value was so easy to lose.

The structural half of the fix is checked by ``tests/ui_reconcile.mjs``, which
renders the real file with React and the SDK stubbed and asserts that no
variadic child list anywhere on any tab is keyless. It runs from here so that
``pytest tests/`` covers it, and skips when the machine has no node.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
UI = PLUGIN_DIR / "desktop-ui" / "plugin.js"
PACKAGE = "kame_123_under_test"


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
control_mod = importlib.import_module(f"{PACKAGE}.control")
settings_mod = importlib.import_module(f"{PACKAGE}.settings")


@pytest.fixture
def clean_env():
    """The setting unset everywhere, and put back however the test left it."""
    names = settings_mod._env_names(settings_mod.STREAM_SILENCE_TIMEOUT)
    before = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    try:
        yield names
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# --- the backend was never the problem --------------------------------------


def test_saving_the_first_token_wait_takes_effect_immediately(clean_env):
    """The exact path the panel's Save button drives, end to end.

    This is the regression guard for the complaint, stated against the half
    that was accused: type 30, press Save, and the value in force is 30. If
    this ever fails the bug really is in the backend, and the UI checks below
    are looking in the wrong place.
    """
    key = settings_mod.STREAM_SILENCE_TIMEOUT

    assert settings_mod.effective(key) == 0.0
    assert settings_mod.provenance(key) == "default"

    ok, detail = control_mod._apply("set", key, "30")

    assert ok, detail
    assert settings_mod.effective(key) == 30.0
    assert settings_mod.provenance(key) == "environment"
    # And the panel renders from `describe`, not from `effective` directly.
    assert settings_mod.describe(key)["value"] == 30.0


def test_a_saved_value_survives_being_described_repeatedly(clean_env):
    """Reading the setting does not disturb it.

    The panel calls ``describe_all`` once a second through the snapshot. A
    reader with a side effect would produce exactly the reported symptom — a
    value that goes back to its default shortly after being set — so the
    absence of one is worth pinning.
    """
    key = settings_mod.STREAM_SILENCE_TIMEOUT
    control_mod._apply("set", key, "45")

    for _ in range(20):
        rows = {row["key"]: row for row in settings_mod.describe_all()}
        assert rows[key]["value"] == 45.0

    assert settings_mod.effective(key) == 45.0


def test_reset_puts_the_first_token_wait_back_to_off(clean_env):
    """The environment variable is cleared even when there is no .env to write.

    ``_forget_one`` pops ``os.environ`` before it ever touches the file, so
    the setting is back to its default in this process regardless of whether
    persistence succeeds — which it cannot, in a test process with no real
    Hermes to ask for an ``.env`` path. ``ok`` reflects the file half only.
    """
    key = settings_mod.STREAM_SILENCE_TIMEOUT
    control_mod._apply("set", key, "30")
    assert settings_mod.effective(key) == 30.0

    control_mod._apply("reset", key, None)

    assert settings_mod.effective(key) == 0.0
    assert settings_mod.provenance(key) == "default"


def test_the_floor_is_reported_as_a_refusal_not_as_a_silent_change(clean_env):
    """A value between 0 and the floor is refused with a sentence, not rounded.

    The panel validates before writing and this validates again; both have to
    agree, or a number accepted on screen turns into a different number in
    force — which is its own version of "it changed by itself".
    """
    key = settings_mod.STREAM_SILENCE_TIMEOUT
    parsed, error = settings_mod.parse(key, "2")

    assert parsed is None
    assert "0 (off) or at least 5" in error
    assert settings_mod.effective(key) == 0.0


# --- the UI, rendered for real ----------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_no_variadic_child_list_in_the_panel_is_keyless():
    """``tests/ui_reconcile.mjs``, run as part of the suite.

    It loads the real ``plugin.js`` with the SDK and React stubbed, opens all
    three tabs, walks every rendered tree and fails on any list of two or more
    children where a child carries no key. That is the property that makes the
    mid-save remount impossible, and it is checked structurally rather than by
    reproducing the remount, because a check that needs a real React to fail is
    a check nobody runs.
    """
    script = Path(__file__).with_name("ui_reconcile.mjs")
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_panel_does_not_republish_an_unchanged_reading():
    """The one-second read stops when what this panel shows has not changed.

    Without this the panel hands every subscriber a new object once a second
    over a document the backend rewrites every twenty, and the Settings form
    rebuilds itself under whoever is typing into it.

    From 1.6.0.1 the comparison is against *this process's section* rather than
    the whole file, and the difference is not cosmetic: the file now holds a
    section per Hermes sharing the home, so on a machine running the Desktop
    and the gateway the bytes change every time either writes. Comparing the
    file there is the same as not comparing at all — which is how the fault
    this test was written for came back, with the test still passing.
    """
    source = UI.read_text(encoding="utf-8")

    assert "let lastText" in source
    assert "if (mineText === lastText" in source
    assert "lastText = mineText" in source
    # And the thing compared is one section, not the document.
    assert "const mineText = JSON.stringify(snap)" in source
    assert "if (text === lastText" not in source


def test_the_page_does_not_subscribe_to_the_clock():
    """Only the countdowns tick.

    ``KamePage`` reading ``$now`` re-renders every tab once a second, including
    the two that have no countdown on them at all.
    """
    source = UI.read_text(encoding="utf-8")
    body = source.split("function KamePage()", 1)[1].split("\n\n", 3)[0]

    assert "useValue($now)" not in body
    # The components that genuinely need it still have it.
    for owner in ("function HeaderStatus", "function StaleNote", "function RightNow", "function PoolRow"):
        after = source.split(owner, 1)[1][:400]
        assert "useValue($now)" in after, f"{owner} lost its clock"


def test_only_one_reader_can_be_running():
    """A second ``register()`` without a dispose must not add a second timer."""
    source = UI.read_text(encoding="utf-8")

    assert "let activeTimer" in source
    assert "if (activeTimer !== null)" in source
