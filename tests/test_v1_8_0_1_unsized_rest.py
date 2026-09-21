"""1.8.0.1 moved the flat unsized-throttle rest from 20s to 45s; 1.8.0.2
corrects that to 30s (``unsized_throttle_rest_seconds`` / ``KAME_UNSIZED_REST``,
default 30.0, range 0.0-300.0).

1.8.0.1's 45 came from a curve built on the owner's real 1.8.0.0 session,
2026-09-19 22:29-22:40 local, that double-counted retries: a credential
retried several times inside one window had its same retry counted once per
earlier refusal it followed. An independent reviewer, who never read this
module, recounted each retry exactly once over the whole real corpus (471
retries that followed a bare 429) and found 30 the argmax, not 45 — 388 of
413 avoidable refused calls avoided at the cost of delaying 6 successes, the
best net return of any rest tried (``research/1.8.0.1/expected/
rest-decision.md``). The original session numbers still hold: a refused
credential retried again inside 30s answered 0 of 73 times; between 30 and
60s it answered 2 of 16; the fastest any refused credential ever answered
again was 34s. The full measurement lives beside the constants it produced:
``core.carousel.UNSIZED_THROTTLE_REST_S`` and ``core.quota.
DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS`` — this file pins the numbers and
proves the dial actually reaches a real hold, through the same
``dispatch_binding.install()`` path RED_TEAM.md F3 found ``KAME_MAX_HOLD``
missing from, not only a hand-built ``Carousel``.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_8_0_1_unsized_rest_under_test"


def _load_package():
    """Import the plugin as a package, the way the Hermes loader does.

    Self-contained, like every other test module in this suite: its own
    ``PACKAGE`` name, its own load, so the module-level ``core.carousel.
    ENGINE`` singleton this file pokes at is this file's own copy, never one
    shared with another test module's import of the same plugin.
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
quota_mod = importlib.import_module(f"{PACKAGE}.core.quota")
settings = importlib.import_module(f"{PACKAGE}.settings")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")

Carousel = carousel_mod.Carousel

NOW = 1_800_000_000.0
IDENTITY = "gemini:gemini-3.6-flash"


@pytest.fixture(autouse=True)
def _clean_settings_state():
    """One test's ``settings.load``/env var must not leak into the next."""
    settings.forget()
    yield
    settings.forget()


# ---------------------------------------------------------------------------
# The two constants that must move together
# ---------------------------------------------------------------------------


class TestTheTwinConstantsMovedTogether:
    """``core/carousel.py:UNSIZED_THROTTLE_REST_S`` and ``core/quota.py:
    DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS`` are the same number by design —
    one refusal must not produce two different waits depending on which half
    of the plugin is asked. ``test_v1_6_0_3.py`` already pins their equality
    generation over generation; this pins the actual 1.8.0.1 value so a
    change to only one side, or to neither, is caught here directly.
    """

    def test_both_are_thirty(self):
        assert carousel_mod.UNSIZED_THROTTLE_REST_S == 30.0
        assert quota_mod.DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS == 30.0

    def test_they_still_agree_with_each_other(self):
        assert (
            carousel_mod.UNSIZED_THROTTLE_REST_S
            == quota_mod.DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS
        )

    def test_thirty_is_not_zero_not_twenty_and_not_the_old_forty_five(self):
        # The measurement's own conclusion: erring short helped (twenty was
        # too short), erring to zero would have been strictly worse (0 of 73
        # answered under 30s), and forty-five over-corrected on a
        # double-counted curve — so the new default is none of those.
        assert carousel_mod.UNSIZED_THROTTLE_REST_S not in (0.0, 20.0, 45.0)


# ---------------------------------------------------------------------------
# The Carousel honours the (instance) dial, default and custom
# ---------------------------------------------------------------------------


class TestTheCarouselAppliesTheDial:
    def test_the_class_default_matches_the_module_constant(self):
        engine = Carousel()
        assert engine.unsized_throttle_rest_s == carousel_mod.UNSIZED_THROTTLE_REST_S

    def test_an_unsized_rate_limit_rests_for_the_dial(self):
        engine = Carousel()
        applied = engine.mark(IDENTITY, "a", False, 0.0, "rate_limit", now=NOW)
        assert applied == 30.0

    def test_per_minute_and_rate_limit_agree(self):
        engine = Carousel()
        a = engine.mark(IDENTITY, "a", False, 0.0, "rate_limit", now=NOW)
        b = engine.mark(IDENTITY, "b", False, 0.0, "per_minute", now=NOW)
        assert a == b == 30.0

    def test_a_custom_value_passed_at_construction_is_what_is_applied(self):
        engine = Carousel(unsized_throttle_rest_s=90.0)
        applied = engine.mark(IDENTITY, "a", False, 0.0, "rate_limit", now=NOW)
        assert applied == 90.0

    def test_zero_means_zero_the_key_is_offered_again_immediately(self):
        """The 'spin' the owner asked to be able to try, end to end.

        Zero is not clamped up, not treated as "unset", and not routed to a
        different branch — an unsized throttle with the dial at zero rests
        for nothing at all, same as ``kind == "timeout"`` does by design.
        """
        engine = Carousel(unsized_throttle_rest_s=0.0)
        applied = engine.mark(IDENTITY, "a", False, 0.0, "rate_limit", now=NOW)
        assert applied == 0.0
        # And the key really is selectable again at the same instant.
        chosen, status = engine.select(IDENTITY, ["a"], now=NOW)
        assert chosen == "a"
        assert status in ("SUCCESS", "EXHAUSTED")

    def test_lowering_the_dial_mid_run_is_read_fresh_like_max_hold_s(self):
        engine = Carousel()
        assert engine.mark(IDENTITY, "a", False, 0.0, "rate_limit", now=NOW) == 30.0
        engine.unsized_throttle_rest_s = 10.0
        assert engine.mark(IDENTITY, "b", False, 0.0, "rate_limit", now=NOW) == 10.0

    def test_a_number_the_provider_actually_stated_still_outranks_the_dial(self):
        # The dial only ever fills the "nobody said anything" gap -- a real
        # stated delay is obeyed whatever the dial says, same as before.
        engine = Carousel(unsized_throttle_rest_s=0.0)
        applied = engine.mark(
            IDENTITY, "a", False, 7.0, "rate_limit", now=NOW, stated=True
        )
        assert applied == 7.0


# ---------------------------------------------------------------------------
# The setting itself, declared exactly the way settings.MAX_HOLD is
# ---------------------------------------------------------------------------


class TestTheSettingItself:
    def test_declared_like_max_hold(self):
        assert settings.UNSIZED_THROTTLE_REST in settings.ALL_NUMBERS
        assert settings.ALL_NUMBERS[settings.UNSIZED_THROTTLE_REST] == 30.0
        assert settings.bounds(settings.UNSIZED_THROTTLE_REST) == (0.0, 300.0)
        assert settings.env_name(settings.UNSIZED_THROTTLE_REST) == "KAME_UNSIZED_REST"
        assert settings.UNITS[settings.UNSIZED_THROTTLE_REST] == "seconds"
        assert settings.known(settings.UNSIZED_THROTTLE_REST)

    def test_default_with_nothing_configured(self):
        assert settings.number(
            settings.UNSIZED_THROTTLE_REST,
            settings.ALL_NUMBERS[settings.UNSIZED_THROTTLE_REST],
        ) == 30.0
        assert settings.provenance(settings.UNSIZED_THROTTLE_REST) == "default"

    def test_it_is_on_the_tuning_shelf_beside_max_hold(self):
        tuning = next(g for g in settings.groups() if g["id"] == "tuning")
        assert settings.UNSIZED_THROTTLE_REST in tuning["keys"]

    def test_it_is_described_for_a_panel(self):
        rows = {row["key"]: row for row in settings.describe_all()}
        row = rows[settings.UNSIZED_THROTTLE_REST]
        assert row["kind"] == "number"
        assert row["min"] == 0.0
        assert row["max"] == 300.0
        assert row["default"] == 30.0
        assert row["env"] == "KAME_UNSIZED_REST"
        # Unlike stream_silence_timeout_seconds, zero has no floor above it.
        assert row["off_or_at_least"] is None

    def test_the_manifest_offers_it(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert "  unsized_throttle_rest_seconds:" in manifest
        assert "KAME_UNSIZED_REST" in manifest

    def test_the_environment_variable_is_read_and_clamped_to_range(self, monkeypatch):
        monkeypatch.setenv("KAME_UNSIZED_REST", "10000")
        assert settings.number(settings.UNSIZED_THROTTLE_REST, 30.0) == 300.0
        monkeypatch.setenv("KAME_UNSIZED_REST", "-5")
        assert settings.number(settings.UNSIZED_THROTTLE_REST, 30.0) == 0.0

    def test_zero_is_accepted_verbatim_not_bumped_to_a_floor(self, monkeypatch):
        # settings.parse() is the single validator behind /kame set and the
        # panel. Zero must round-trip as zero, not be refused or raised —
        # the floor mechanism that protects STREAM_SILENCE_TIMEOUT must never
        # apply here.
        value, error = settings.parse(settings.UNSIZED_THROTTLE_REST, "0")
        assert error == ""
        assert value == "0"
        monkeypatch.setenv("KAME_UNSIZED_REST", "0")
        assert settings.number(settings.UNSIZED_THROTTLE_REST, 30.0) == 0.0

    def test_out_of_range_is_refused_by_parse(self):
        _value, error = settings.parse(settings.UNSIZED_THROTTLE_REST, "301")
        assert "0" in error and "300" in error


# ---------------------------------------------------------------------------
# The dial reaches the real dispatch engine, not only a hand-built Carousel
# ---------------------------------------------------------------------------


class TestTheDialReachesDispatch:
    """``dispatch_binding.py``, beside ``settings.MAX_HOLD``'s own wiring.

    RED_TEAM.md F3 found that a setting declared in ``settings.py`` and
    offered in ``plugin.yaml`` did nothing until something pushed its
    resolved value onto the one ``Carousel`` the dispatch path actually
    marks and selects against. Proven the same way that finding was proven:
    through ``dispatch_binding.install()`` itself.
    """

    @staticmethod
    def _fake_host_module():
        def interruptible_api_call(agent, api_kwargs, *a, **kw):
            return None

        def interruptible_streaming_api_call(agent, api_kwargs, *a, **kw):
            return None

        return SimpleNamespace(
            interruptible_api_call=interruptible_api_call,
            interruptible_streaming_api_call=interruptible_streaming_api_call,
        )

    def test_the_default_reaches_the_engine_with_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("KAME_UNSIZED_REST", raising=False)
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        monkeypatch.delenv("KAME_CAROUSEL_DISABLED", raising=False)
        original = carousel_mod.ENGINE.unsized_throttle_rest_s
        try:
            binding = dispatch_binding.install(module=self._fake_host_module())
            assert binding is not None, "install() refused the fake module — shape mismatch"
            assert binding.engine.unsized_throttle_rest_s == 30.0
        finally:
            carousel_mod.ENGINE.unsized_throttle_rest_s = original

    def test_setting_kame_unsized_rest_reaches_the_engine_install_actually_uses(
        self, monkeypatch
    ):
        monkeypatch.setenv("KAME_UNSIZED_REST", "5")
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        monkeypatch.delenv("KAME_CAROUSEL_DISABLED", raising=False)
        original = carousel_mod.ENGINE.unsized_throttle_rest_s
        try:
            binding = dispatch_binding.install(module=self._fake_host_module())
            assert binding is not None, "install() refused the fake module — shape mismatch"
            assert binding.engine.unsized_throttle_rest_s == 5.0, (
                "KAME_UNSIZED_REST=5 left the real dispatch engine's rest "
                "unchanged — the same shape of bug RED_TEAM.md F3 found for "
                "KAME_MAX_HOLD"
            )
            # Not just an attribute: a real unsized throttle through this
            # SAME engine, via mark(), is actually held for 5 seconds.
            applied = binding.engine.mark(
                IDENTITY, "key-a", False, 0.0, "rate_limit", now=NOW,
            )
            assert applied == 5.0
        finally:
            carousel_mod.ENGINE.unsized_throttle_rest_s = original

    def test_setting_kame_unsized_rest_to_zero_reaches_the_engine_too(self, monkeypatch):
        """The dial's whole point: the owner can try the opposite of the
        measurement, end to end, through the exact path a real call takes.
        """
        monkeypatch.setenv("KAME_UNSIZED_REST", "0")
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        monkeypatch.delenv("KAME_CAROUSEL_DISABLED", raising=False)
        original = carousel_mod.ENGINE.unsized_throttle_rest_s
        try:
            binding = dispatch_binding.install(module=self._fake_host_module())
            assert binding is not None
            assert binding.engine.unsized_throttle_rest_s == 0.0
            # A key of its own -- the ``ENGINE`` singleton is shared with the
            # rest of this class, and a stale, longer ``sick_until`` on a key
            # reused from another test would win the never-shorten rule and
            # hide the very thing this test checks.
            applied = binding.engine.mark(
                IDENTITY, "key-zero-dial", False, 0.0, "rate_limit", now=NOW,
            )
            assert applied == 0.0
        finally:
            carousel_mod.ENGINE.unsized_throttle_rest_s = original
