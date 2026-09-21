"""The switches, and which source outranks which.

The knobs were environment-only, which is correct and invisible: Hermes has
a config surface for exactly this and a knob nobody finds is a knob nobody
has. The order matters more than the plumbing — ``KAME_ROTATION_DISABLED``
is the thing somebody reaches for when they suspect this plugin of breaking
their agent, so a config file must never be able to overrule it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_settings_under_test"


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
settings = importlib.import_module(f"{PACKAGE}.settings")


class FakeContext:
    """Stands in for the host's plugin context."""

    def __init__(self, values=None, raises=False):
        self._values = values or {}
        self._raises = raises
        self.asked = []

    def get_config(self, key, default=None):
        self.asked.append(key)
        if self._raises:
            raise ValueError("rejected config path")
        return self._values.get(key, default)


class NoConfigContext:
    """A Hermes with no config surface, or an older one."""


class TestReadingTheConfig:
    def teardown_method(self):
        settings.forget()

    def test_nothing_configured_leaves_everything_running(self):
        settings.load(FakeContext())
        assert settings.is_on(settings.ROTATION_DISABLED) is False
        assert settings.is_on(settings.SPREAD_DISABLED) is False

    def test_a_switch_set_in_the_config_is_read(self):
        settings.load(FakeContext({"spread_disabled": True}))
        assert settings.is_on(settings.SPREAD_DISABLED) is True
        assert settings.is_on(settings.ROTATION_DISABLED) is False

    def test_a_quoted_value_still_counts(self):
        # YAML users quote things. "true" and true mean the same to a reader.
        settings.load(FakeContext({"disabled": "yes"}))
        assert settings.is_on(settings.ROTATION_DISABLED) is True

    def test_a_value_nobody_can_read_leaves_the_default(self):
        settings.load(FakeContext({"disabled": "maybe"}))
        assert settings.is_on(settings.ROTATION_DISABLED) is False

    def test_every_switch_is_asked_for(self):
        # Plus the names a setting used to carry: since 1.1.1 a renamed setting
        # is looked up under its old name too, and only when the current one
        # says nothing — so a config written against an older release keeps
        # working instead of silently reading as the default.
        ctx = FakeContext()
        settings.load(ctx)
        assert sorted(ctx.asked) == sorted(
            list(settings._ENV_FOR)
            + list(settings._NUMBER_ENV_FOR)
            + list(settings._LEGACY_KEYS)
        )

    def test_every_switch_is_declared_in_the_manifest(self):
        # A switch the manifest does not declare is one the host will not
        # serve from config.yaml, so the config half of it silently does
        # nothing while the environment half keeps working.
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        for key in list(settings._ENV_FOR) + list(settings._NUMBER_ENV_FOR):
            assert f"\n  {key}:\n" in manifest


class TestTheNumbers:
    """A numeric setting fails differently from a switch, so it reads differently.

    An unreadable switch means "off", which is harmless. An unreadable number
    must mean "the default" — reading it as zero would turn a deadline into an
    instant give-up, which is the opposite of what the person who typed it
    wanted.
    """

    def setup_method(self):
        settings.forget()
        for name in settings._NUMBER_ENV_FOR.values():
            os.environ.pop(name, None)

    teardown_method = setup_method

    def test_the_default_survives_a_host_that_says_nothing(self):
        assert settings.number(settings.DAILY_COOLDOWN, 600.0) == 600.0

    def test_a_number_in_the_config_is_used(self):
        settings.load(FakeContext({"daily_quota_cooldown_seconds": 120}))
        assert settings.number(settings.DAILY_COOLDOWN, 600.0) == 120.0

    def test_the_environment_outranks_the_config(self):
        settings.load(FakeContext({"daily_quota_cooldown_seconds": 120}))
        os.environ["KAME_DAILY_COOLDOWN"] = "45"
        assert settings.number(settings.DAILY_COOLDOWN, 600.0) == 45.0

    def test_a_value_nobody_can_read_leaves_the_default(self):
        settings.load(FakeContext({"daily_quota_cooldown_seconds": "soonish"}))
        assert settings.number(settings.DAILY_COOLDOWN, 600.0) == 600.0

    def test_an_absurd_value_is_clamped_rather_than_refused(self):
        # Somebody who wrote 99999 meant "a long time". Refusing the setting
        # would hand them 600, which is shorter than anything they asked for.
        settings.load(FakeContext({"daily_quota_cooldown_seconds": 99999}))
        assert settings.number(settings.DAILY_COOLDOWN, 600.0) == 86400.0
        settings.load(FakeContext({"daily_quota_cooldown_seconds": 0}))
        assert settings.number(settings.DAILY_COOLDOWN, 3600.0) == 1.0

    def test_a_boolean_is_not_a_number(self):
        # ``True`` is 1.0 to float() and a one-second daily cooldown is a
        # busy-loop against a key that is out until midnight. A switch written
        # where a duration belongs is a typo, not a one-second wait.
        settings.load(FakeContext({"daily_quota_cooldown_seconds": True}))
        assert settings.number(settings.DAILY_COOLDOWN, 3600.0) == 3600.0

    def test_a_host_with_no_config_surface_changes_nothing(self):
        settings.load(NoConfigContext())
        assert settings.is_on(settings.ROTATION_DISABLED) is False

    def test_a_host_that_rejects_the_read_changes_nothing(self):
        # The host raises on a key it will not serve. One unusable setting
        # must not cost the other, and neither may cost the plugin.
        settings.load(FakeContext(raises=True))
        assert settings.is_on(settings.SPREAD_DISABLED) is False

    def test_loading_again_replaces_what_was_read(self):
        settings.load(FakeContext({"disabled": True}))
        settings.load(FakeContext())
        assert settings.is_on(settings.ROTATION_DISABLED) is False


class TestWhichSourceWins:
    """The environment is the escape hatch, so the environment is on top."""

    def teardown_method(self):
        settings.forget()

    def test_the_environment_overrides_a_config_that_says_off(self, monkeypatch):
        settings.load(FakeContext({"disabled": False}))
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
        assert settings.is_on(settings.ROTATION_DISABLED) is True

    def test_the_environment_can_also_switch_something_back_on(self, monkeypatch):
        # The direction that matters second: a config file that disabled the
        # plugin must be answerable from a shell, without editing the file.
        settings.load(FakeContext({"disabled": True}))
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "0")
        assert settings.is_on(settings.ROTATION_DISABLED) is False

    def test_an_unset_variable_does_not_count_as_off(self, monkeypatch):
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        settings.load(FakeContext({"disabled": True}))
        assert settings.is_on(settings.ROTATION_DISABLED) is True

    def test_an_empty_variable_does_not_count_as_off(self, monkeypatch):
        # An exported-but-empty variable is the shape a shell script leaves
        # behind, and it is not somebody saying "off".
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "")
        settings.load(FakeContext({"disabled": True}))
        assert settings.is_on(settings.ROTATION_DISABLED) is True

    def test_the_two_switches_do_not_read_each_other(self, monkeypatch):
        monkeypatch.setenv("KAME_SPREAD_DISABLED", "1")
        settings.load(FakeContext())
        assert settings.is_on(settings.SPREAD_DISABLED) is True
        assert settings.is_on(settings.ROTATION_DISABLED) is False


class TestTheManifestDeclaresThem:
    def test_every_switch_is_declared_where_hermes_looks(self):
        # A config key the plugin reads but the manifest never declares is one
        # `hermes plugins doctor` cannot validate and no user can discover.
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        block = manifest.split("config_schema:", 1)[-1]
        for key in (settings.ROTATION_DISABLED, settings.SPREAD_DISABLED):
            assert f"  {key}:" in block
