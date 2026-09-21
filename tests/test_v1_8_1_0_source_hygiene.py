"""No invisible character is written literally anywhere in the plugin source.

The Hermes catalog's admission scanner (``hermes plugins validate``, security
scan) flags literal invisible Unicode as ``caution: invisible_unicode``: it is
the classic way to hide code from a reviewer. ``core/keys.py`` carried two of
them -- the byte-order marks it strips from pasted keys -- while the comment
right above said they were "spelled as escapes". They were not: an editor route
had turned the escapes into the characters themselves.

The value the code strips must not change; only its spelling does.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_source_hygiene_under_test"

INVISIBLE = {
    0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF, 0xFFFE,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069,
}
SKIP_DIRS = {"__pycache__", "graphify-out"}


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


def _sources():
    for path in PLUGIN_DIR.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in {".py", ".js", ".yaml", ".json", ".md"}:
            yield path


def test_no_plugin_source_holds_a_literal_invisible_character():
    found = []
    for path in _sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            chars = sorted({hex(ord(c)) for c in line if ord(c) in INVISIBLE})
            if chars:
                found.append(f"{path.relative_to(PLUGIN_DIR)}:{number} {chars}")
    assert not found, "literal invisible characters: " + "; ".join(found)


def test_the_marks_stripped_from_pasted_keys_are_still_the_same_two():
    _load_package()
    keys = importlib.import_module(f"{PACKAGE}.core.keys")
    assert keys._BOM_CHARS == "\ufeff\ufffe"


def test_a_pasted_key_wrapped_in_a_byte_order_mark_still_comes_out_clean():
    _load_package()
    keys = importlib.import_module(f"{PACKAGE}.core.keys")
    key = "AIzaSy" + "A" * 33
    assert keys._strip_wrapper("\ufeff" + key) == key


def test_the_optional_shelf_does_not_call_itself_off_when_its_settings_ship_on():
    # The shelf note said "Off until you turn it on" while three of its four
    # settings default to on (never_fall_back_to_another_model,
    # share_pool_health, unsized_throttle_backoff). A person reads the note,
    # not the defaults.
    _load_package()
    settings = importlib.import_module(f"{PACKAGE}.settings")
    extra = next(g for g in settings.groups() if g["id"] == "extra")
    rows = {row["key"]: row for row in settings.describe_all()}
    shipped_on = [k for k in extra["keys"] if rows[k]["default"] not in (False, 0, 0.0, None, "")]
    if shipped_on:
        assert "off until" not in extra["note"].lower(), shipped_on


def test_a_switch_that_ships_on_reports_on_as_its_default(monkeypatch):
    # describe() hard-coded ``default: False`` for every flag, so the three in
    # DEFAULTS_ON told the panel their default was off: the Reset button then
    # flipped the switch to off on screen while the plugin put it back on.
    _load_package()
    settings = importlib.import_module(f"{PACKAGE}.settings")
    for key in settings.ALL_FLAGS:
        for variable in settings._env_names(key):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.delitem(settings._FROM_CONFIG, key, raising=False)
        row = settings.describe(key)
        assert row["default"] is (key in settings.DEFAULTS_ON), key
        assert row["value"] is row["default"], key
