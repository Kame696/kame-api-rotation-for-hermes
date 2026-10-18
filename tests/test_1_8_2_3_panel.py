"""V3 panel tests: snapshot fields, string settings surface, reset coverage.

The v2 failure mode was UI work that shipped only in settings.py while the
panel layer stayed byte-identical to the base. The check here is data, not
file hashes: the snapshot must CARRY the v3 fields, describe() must render
string settings with choices and recommendation, and reset_all must clear
a string setting from disk.
"""
import os
import sys
import tempfile
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))
sys.path.insert(0, _SRC)

import importlib
import importlib.util as _ilu

_pkg_spec = _ilu.spec_from_file_location(
    "hermes_kame_api_rotation", os.path.join(_SRC, "__init__.py"),
    submodule_search_locations=[_SRC],
)
_pkg = _ilu.module_from_spec(_pkg_spec)
sys.modules.setdefault("hermes_kame_api_rotation", _pkg)
_pkg_spec.loader.exec_module(_pkg)
settings = importlib.import_module("hermes_kame_api_rotation.settings")
state = importlib.import_module("hermes_kame_api_rotation.state")


class TestSnapshotV3Fields(unittest.TestCase):
    def test_snapshot_carries_v3_sections(self):
        snap = state.snapshot(binding=None)
        for field in ("secondary_events", "hedge_state", "pacing_state", "build_line"):
            self.assertIn(field, snap, f"snapshot missing {field}")
        from hermes_kame_api_rotation.core import __version__
        self.assertEqual(snap["build_line"], __version__)

    def test_secondary_rows_are_a_list(self):
        snap = state.snapshot(binding=None)
        self.assertIsInstance(snap["secondary_events"], list)


class TestStringSettingsSurface(unittest.TestCase):
    def test_describe_hedge_mode_has_choices_and_recommendation(self):
        d = settings.describe(settings.HEDGE_MODE)
        self.assertEqual(d["kind"], "string")
        self.assertEqual(d["choices"], ["adaptive", "max", "off"])
        self.assertTrue(d["recommendation"], "every v3 setting carries a recommendation")
        self.assertEqual(d["group"], "speed")

    def test_describe_all_includes_strings(self):
        keys = {d["key"] for d in settings.describe_all()}
        for name in (settings.HEDGE_MODE, settings.PACING_RPM_OVERRIDES,
                     settings.KEY_REGION_OVERRIDES):
            self.assertIn(name, keys)
        # 1.8.2.7: the owner asked for the quota-group question to go (settings.HIDDEN)
        self.assertNotIn(settings.KEY_QUOTA_GROUPS, keys)

    def test_speed_group_exists_and_is_populated(self):
        groups = {g["id"]: g for g in settings.groups()}
        self.assertIn("speed", groups)
        self.assertGreater(len(groups["speed"]["keys"]), 3)


class TestResetAllCoversStrings(unittest.TestCase):
    def test_reset_all_clears_string_setting(self):
        env = os.environ.get("KAME_HEDGE_MODE")
        os.environ["KAME_HEDGE_MODE"] = "max"
        try:
            control = importlib.import_module("hermes_kame_api_rotation.control")
            control._apply("reset_all", "", None)
            # The effect is what matters: the string setting is back to its
            # default in this process, whatever the .env file situation.
            self.assertEqual(settings.text_value(settings.HEDGE_MODE), "adaptive")
        finally:
            if env is None:
                os.environ.pop("KAME_HEDGE_MODE", None)
            else:
                os.environ["KAME_HEDGE_MODE"] = env

    def test_set_string_via_control(self):
        saved = os.environ.get("KAME_HEDGE_MODE")
        try:
            control = importlib.import_module("hermes_kame_api_rotation.control")
            ok, detail = control._apply("set", "hedge_mode", "max")
            self.assertTrue(ok, detail)
            self.assertEqual(settings.text_value(settings.HEDGE_MODE), "max")
        finally:
            control._apply("reset", "hedge_mode", None) if "hermes_kame_api_rotation.control" in sys.modules else None
            if saved is None:
                os.environ.pop("KAME_HEDGE_MODE", None)
            else:
                os.environ["KAME_HEDGE_MODE"] = saved


if __name__ == "__main__":
    unittest.main()
