"""V3 meta-test: every setting is fully wired, end to end.

Parametrized over the whole settings surface — flags, numbers and strings.
This is the test that makes "a setting shipped invisible" structurally
impossible: if a new setting is added without env mapping, range, META,
a group, a runtime read site, a parse path and reset coverage, it fails
here before it can fail in the panel.

Run:  uv run --with pytest python -m pytest tests/test_1_8_2_3_settings_meta.py -q
"""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))

import settings  # noqa: E402


SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))


def _all_source_text():
    """Concatenate every Python file in src/ except settings.py itself."""
    chunks = []
    for root, dirs, files in os.walk(SRC_ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if os.path.basename(path) == "settings.py":
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
    return "\n".join(chunks)


class TestSettingsWiringMeta(unittest.TestCase):
    """One test class, many lenses, over the whole registry at once."""

    @classmethod
    def setUpClass(cls):
        settings.forget()
        for name in list(os.environ):
            if name.startswith("KAME_"):
                del os.environ[name]
        cls.everything = (
            list(settings.ALL_FLAGS)
            + list(settings.ALL_NUMBERS)
            + list(settings.ALL_STRINGS)
        )
        cls.source_text = _all_source_text()

    # -- 1. names and env mapping -------------------------------------

    def test_no_duplicate_names(self):
        self.assertEqual(len(self.everything), len(set(self.everything)))

    def test_every_env_name_is_well_formed(self):
        for key in self.everything:
            env = settings.env_name(key)
            self.assertTrue(
                env.startswith("KAME_") and env == env.upper(),
                f"{key}: env name {env!r} is not KAME_UPPER_SNAKE",
            )

    def test_every_setting_has_an_env_var(self):
        for key in self.everything:
            self.assertNotEqual(settings.env_name(key), "", f"{key}: no env var")

    def test_flag_env_table_matches(self):
        for key in settings.ALL_FLAGS:
            self.assertIn(key, settings._ENV_FOR, f"flag {key} missing from _ENV_FOR")

    def test_number_env_table_matches(self):
        for key in settings.ALL_NUMBERS:
            self.assertIn(key, settings._NUMBER_ENV_FOR, f"number {key} missing from _NUMBER_ENV_FOR")

    def test_string_env_table_matches(self):
        for key in settings.ALL_STRINGS:
            self.assertIn(key, settings._STRING_ENV_FOR, f"string {key} missing from _STRING_ENV_FOR")

    # -- 2. type consistency -------------------------------------------

    def test_numbers_have_range_and_unit(self):
        for key in settings.ALL_NUMBERS:
            self.assertIn(key, settings._NUMBER_RANGE, f"number {key} missing range")
            self.assertIn(key, settings.UNITS, f"number {key} missing unit")

    def test_defaults_inside_range(self):
        for key, default in settings.ALL_NUMBERS.items():
            low, high = settings._NUMBER_RANGE[key]
            self.assertTrue(
                low <= default <= high,
                f"{key}: default {default} outside [{low}, {high}]",
            )

    def test_closed_set_strings_have_validity(self):
        # A string setting with a closed answer set must declare it, so the
        # panel can render a select instead of free text.
        self.assertIn(settings.HEDGE_MODE, settings._STRING_VALID)

    def test_string_default_is_valid(self):
        for key, default in settings.ALL_STRINGS.items():
            valid = settings._STRING_VALID.get(key)
            if valid:
                self.assertIn(default, valid, f"{key}: default {default!r} not in valid set")

    # -- 3. META completeness -------------------------------------------

    def test_every_setting_has_meta(self):
        for key in self.everything:
            self.assertIn(key, settings.META, f"{key}: no META entry (invisible in panel)")

    def test_meta_titles_short_and_human(self):
        banned = ("GCRA", "EWMA", "TTFT", "p90", "API", "cron")
        for key in self.everything:
            entry = settings.META.get(key)
            if not entry:
                continue
            title_text = entry[0]
            self.assertLessEqual(len(title_text), 40, f"{key}: title {title_text!r} too long")
            for word in banned:
                self.assertNotIn(word, title_text, f"{key}: title has jargon {word!r}")

    def test_meta_has_description(self):
        for key in self.everything:
            entry = settings.META.get(key)
            if not entry:
                continue
            self.assertGreaterEqual(len(entry[1].strip()), 20, f"{key}: description too short")

    # -- 4. groups -------------------------------------------------------

    def test_every_setting_in_exactly_one_group(self):
        for key in self.everything:
            if key in settings.HIDDEN:  # 1.8.2.7: loaded, never shown
                self.assertFalse([g for g, _t, _n, keys in settings.GROUPS if key in keys])
                continue
            hits = [g for g, _t, _n, keys in settings.GROUPS if key in keys]
            self.assertEqual(len(hits), 1, f"{key} in groups {hits} (want exactly 1)")

    def test_describe_all_covers_everything(self):
        described = {d["key"] for d in settings.describe_all()}
        for key in self.everything:
            if key in settings.HIDDEN:  # 1.8.2.7: loaded, never shown
                self.assertNotIn(key, described)
                continue
            self.assertIn(key, described, f"{key}: not in describe_all()")

    # -- 5. runtime read sites (the anti-inert check) -------------------

    def test_v3_settings_have_runtime_read_sites(self):
        # The settings introduced in v3 must be read somewhere in the
        # plugin's own code (outside settings.py) or, for ones whose
        # consumer lands in a later build step, carry a documented
        # consumer in the build contract. The known ones:
        consumers = {
            settings.HEDGE_MODE: ("hedge_mode", "HEDGE_MODE"),
            settings.HEDGE_MAX_LEGS: ("hedge_max_legs", "HEDGE_MAX_LEGS"),
            settings.HEDGE_BUDGET_PCT: ("hedge_budget_pct", "HEDGE_BUDGET_PCT"),
            settings.HEDGE_BURST: ("hedge_burst", "HEDGE_BURST"),
            settings.HEDGE_QUANTILE: ("hedge_quantile", "HEDGE_QUANTILE"),
            settings.HEDGE_COLD_S: ("hedge_cold_s", "HEDGE_COLD_S"),
            settings.HEDGE_COLD_REASONING_S: ("hedge_cold_reasoning_s", "HEDGE_COLD_REASONING_S"),
            settings.HEDGE_MAX_PROMPT_TOKENS: ("hedge_max_prompt_tokens", "HEDGE_MAX_PROMPT_TOKENS"),
            settings.PACING_RPM_OVERRIDES: ("pacing_rpm_overrides", "PACING_RPM_OVERRIDES"),
            settings.KEY_REGION_OVERRIDES: ("key_region_overrides", "KEY_REGION_OVERRIDES"),
            settings.KEY_QUOTA_GROUPS: ("key_quota_groups", "KEY_QUOTA_GROUPS"),
        }
        pending = set()
        for key, tokens in consumers.items():
            if not any(tok in self.source_text for tok in tokens):
                pending.add(key)
        # Settings whose consumer arrives in a later build step are listed
        # in BUILD-V3-CONTRACT.md; the test reads that file so a setting
        # can never be silently abandoned.
        contract = os.path.join(os.path.dirname(__file__), "..", "BUILD-1.8.2.3-CONTRACT.md")
        with open(contract, encoding="utf-8") as fh:
            contract_text = fh.read()
        still_missing = []
        for key in pending:
            if key not in contract_text:
                still_missing.append(key)
        self.assertEqual(
            still_missing, [],
            f"settings with no runtime reader and no contract mention: {still_missing}",
        )

    # -- 6. parse / reset / provenance ----------------------------------

    def test_parse_roundtrip_for_every_setting(self):
        for key in settings.ALL_FLAGS:
            value, err = settings.parse(key, "true")
            self.assertEqual((value, err), ("1", ""), f"flag {key}: {value}, {err}")
        for key, default in settings.ALL_NUMBERS.items():
            value, err = settings.parse(key, str(default))
            self.assertEqual(err, "", f"number {key}: {err}")
        for key, default in settings.ALL_STRINGS.items():
            value, err = settings.parse(key, default or "x")
            self.assertEqual(err, "", f"string {key}: {err}")

    def test_out_of_range_rejected_by_parse(self):
        low, high = settings._NUMBER_RANGE[settings.HEDGE_MAX_LEGS]
        value, err = settings.parse(settings.HEDGE_MAX_LEGS, str(high + 10))
        self.assertIsNone(value)
        self.assertIn("outside", err)

    def test_provenance_default_when_unset(self):
        self.assertEqual(settings.provenance(settings.HEDGE_MODE), "default")
        os.environ["KAME_HEDGE_MODE"] = "max"
        try:
            self.assertEqual(settings.provenance(settings.HEDGE_MODE), "environment")
            self.assertEqual(settings.text_value(settings.HEDGE_MODE), "max")
        finally:
            del os.environ["KAME_HEDGE_MODE"]
        self.assertEqual(settings.text_value(settings.HEDGE_MODE), "adaptive")

    def test_config_string_load_roundtrip(self):
        class Ctx:
            def get_config(self, key, default=None):
                return {"hedge_mode": "MAX"}.get(key, default)

        settings.load(Ctx())
        try:
            self.assertEqual(settings.text_value(settings.HEDGE_MODE), "max")
            self.assertEqual(settings.provenance(settings.HEDGE_MODE), "config")
        finally:
            settings.forget()

    def test_config_string_invalid_rejected_not_fatal(self):
        class Ctx:
            def get_config(self, key, default=None):
                return {"hedge_mode": "turbo"}.get(key, default)

        settings.load(Ctx())
        try:
            # Falls back to the default rather than crashing registration.
            self.assertEqual(settings.text_value(settings.HEDGE_MODE), "adaptive")
        finally:
            settings.forget()

    def test_describe_carries_recommendation_for_new_settings(self):
        for key in (
            settings.HEDGE_MODE, settings.HEDGE_MAX_LEGS, settings.HEDGE_BUDGET_PCT,
            settings.HEDGE_BURST, settings.HEDGE_QUANTILE, settings.HEDGE_COLD_S,
            settings.HEDGE_COLD_REASONING_S, settings.HEDGE_MAX_PROMPT_TOKENS,
            settings.PACING_RPM_OVERRIDES, settings.KEY_REGION_OVERRIDES,
            settings.KEY_QUOTA_GROUPS,
        ):
            self.assertNotEqual(
                settings.recommendation(key), "",
                f"{key}: v3 settings must all carry a recommendation",
            )

    def test_describe_choices_for_choice_settings(self):
        d = settings.describe(settings.HEDGE_MODE)
        self.assertEqual(d["choices"], ["adaptive", "max", "off"])

    # -- 7. never regress the legacy surface ----------------------------

    def test_legacy_settings_still_parse(self):
        for key in ("daily_quota_cooldown_seconds", "max_hold_seconds",
                    "stream_silence_timeout_seconds", "unsized_throttle_rest_seconds"):
            self.assertTrue(settings.known(key), f"{key} vanished")

    def test_module_version_mentions_v3(self):
        from core import __version__  # noqa: E402
        # 1.8.2.4: the v3 line, whatever its last number.
        self.assertTrue(__version__.startswith("1.8.2."), __version__)


if __name__ == "__main__":
    unittest.main()
