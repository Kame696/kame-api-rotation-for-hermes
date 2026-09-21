"""1.8.1.0 — the doubling backoff on an unsized throttle, an owner-asked experiment.

The owner believes a refused request does not count against quota, so he asked
for ``1 -> 2 -> 4 -> 8 ...`` in place of the flat 30s rest, to run on his own
traffic and watch. The measurement argues against it (simulated over his own
corpus with call suppression: about 242 more refused calls than the flat 30s,
+13 seconds returned against +193 — decisions/0007), and it ships anyway,
ON, because the premise it tests has never been measured either way and he is
the one who asked to see it.

What has to be true, and is pinned here:

* the rest doubles per consecutive unsized throttle on the SAME credential and
  model, and the owner's ceiling (``max_hold_seconds``) bounds it;
* a success resets it, and so does a number the provider states — a stated
  number is obeyed as stated and never multiplied (closed rules R10/R11);
* another credential's refusals never advance this one's ladder;
* the switch off is exactly the flat rest of every earlier release, and the
  flat-rest dial still works behind it;
* the switch reaches the engine the dispatch path really uses, through
  ``dispatch_binding.install()`` — a dial that does not reach the engine was a
  red-team finding once (RED_TEAM.md F3) and must not be one twice;
* the rung is visible: in the Events row's ``sized_by``, in the log line, in
  ``calls.jsonl``'s ``rest_source``, and the panel has a label for it — a value
  the panel does not know renders as nothing at all (R55, which already
  happened once), so producer and consumer are crossed below.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PANEL = PLUGIN_DIR / "desktop" / "plugin.js"
PACKAGE = "kame_v1_8_1_0_backoff_under_test"


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


pkg = _load_package()
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
core_mod = importlib.import_module(f"{PACKAGE}.core")
doctor_mod = importlib.import_module(f"{PACKAGE}.core.doctor")
events_mod = importlib.import_module(f"{PACKAGE}.core.events")
settings = importlib.import_module(f"{PACKAGE}.settings")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
timings = importlib.import_module(f"{PACKAGE}.timings")
status_mod = importlib.import_module(f"{PACKAGE}.status")

Carousel = carousel_mod.Carousel
EVENTS = events_mod.EVENTS

NOW = 1_800_000_000.0
ID = "gemini:gemini-3.6-flash"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    EVENTS.clear()
    yield
    settings.forget()
    EVENTS.clear()


def _ladder(engine, key="a", identity=ID, count=6):
    """Bare RESOURCE_EXHAUSTED refusals: the only shape the ladder applies to."""
    return [
        engine.mark(identity, key, False, 0.0, "rate_limit", now=NOW,
                    bare_resource_exhausted=True)
        for _ in range(count)
    ]


def _bare(engine, key="a", identity=ID, kind="rate_limit", now=NOW):
    return engine.mark(identity, key, False, 0.0, kind, now=now, bare_resource_exhausted=True)


# ---------------------------------------------------------------------------
# The ladder itself
# ---------------------------------------------------------------------------


class TestTheLadder:
    def test_it_doubles_one_two_four_eight_sixteen_thirty_two_sixty_four_then_holds(self):
        assert _ladder(Carousel(), count=9) == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 64.0, 64.0]

    def test_it_is_the_default(self):
        assert Carousel().unsized_throttle_backoff is True
        assert carousel_mod.UNSIZED_THROTTLE_BACKOFF_DEFAULT is True

    def test_per_minute_and_rate_limit_climb_together(self):
        engine = Carousel()
        assert _bare(engine, kind="per_minute") == 1.0
        assert _bare(engine, kind="rate_limit") == 2.0

    def test_the_owners_global_ceiling_bounds_it(self):
        engine = Carousel(max_hold_s=10.0)
        assert _ladder(engine, count=7) == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]

    def test_a_very_long_streak_saturates_instead_of_overflowing(self):
        engine = Carousel(max_hold_s=3600.0, unsized_backoff_max_s=3600.0)
        rests = _ladder(engine, count=1500)
        assert rests[11] == 2048.0
        assert rests[12:] == [3600.0] * (1500 - 12)

    def test_a_success_resets_it_to_one_second(self):
        engine = Carousel()
        assert _ladder(engine, count=3) == [1.0, 2.0, 4.0]
        engine.mark(ID, "a", True, now=NOW)
        assert engine.unsized_backoff_step(ID, "a") == 0
        assert _bare(engine, now=NOW + 100) == 1.0

    def test_another_credentials_refusals_do_not_advance_this_ladder(self):
        engine = Carousel()
        assert _ladder(engine, "a", count=4) == [1.0, 2.0, 4.0, 8.0]
        assert _bare(engine, "b") == 1.0
        assert _bare(engine, "a") == 16.0
        assert engine.unsized_backoff_step(ID, "b") == 1

    def test_another_model_on_the_same_credential_has_its_own_ladder(self):
        engine = Carousel()
        assert _ladder(engine, "a", count=3) == [1.0, 2.0, 4.0]
        assert _bare(engine, "a", "gemini:gemini-3.5-flash") == 1.0

    def test_a_different_kind_never_advances_it(self):
        engine = Carousel()
        assert _ladder(engine, count=2) == [1.0, 2.0]
        engine.mark(ID, "a", False, 0.0, "server", now=NOW)
        engine.mark(ID, "a", False, 0.0, "auth", now=NOW)
        assert engine.unsized_backoff_step(ID, "a") == 2
        # Well after the auth rest, so the never-shorten rule is not what is read.
        assert _bare(engine, now=NOW + 10_000) == 4.0


class TestTheCap:
    """``unsized_backoff_max_seconds``: where the doubling stops and holds."""

    def test_the_default_is_sixty_four(self):
        assert Carousel().unsized_backoff_max_s == 64.0
        assert carousel_mod.UNSIZED_BACKOFF_MAX_S == 64.0
        assert settings.ALL_NUMBERS[settings.UNSIZED_BACKOFF_MAX] == 64.0

    def test_the_ladder_stops_at_the_cap(self):
        rests = _ladder(Carousel(), count=12)
        assert rests[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
        assert rests[7:] == [64.0] * 5

    def test_a_low_cap_lowers_it(self):
        assert _ladder(Carousel(unsized_backoff_max_s=10.0), count=7) == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]

    def test_thirty_six_hundred_restores_the_uncapped_doubling_up_to_the_global_ceiling(self):
        engine = Carousel(unsized_backoff_max_s=3600.0)
        rests = _ladder(engine, count=14)
        assert rests[:12] == [float(2 ** n) for n in range(12)]
        assert rests[12:] == [3600.0, 3600.0]
        tighter = Carousel(unsized_backoff_max_s=3600.0, max_hold_s=120.0)
        assert _ladder(tighter, count=11)[-3:] == [120.0, 120.0, 120.0]

    def test_the_setting_is_declared_like_the_other_numbers(self):
        key = settings.UNSIZED_BACKOFF_MAX
        assert key == "unsized_backoff_max_seconds"
        assert settings.env_name(key) == "KAME_UNSIZED_BACKOFF_MAX"
        assert settings.bounds(key) == (1.0, 3600.0)
        assert settings.UNITS[key] == "seconds"
        assert settings.known(key)
        assert key in next(g for g in settings.groups() if g["id"] == "tuning")["keys"]

    def test_the_environment_is_clamped_to_range(self, monkeypatch):
        monkeypatch.setenv("KAME_UNSIZED_BACKOFF_MAX", "99999")
        assert settings.number(settings.UNSIZED_BACKOFF_MAX, 64.0) == 3600.0
        monkeypatch.setenv("KAME_UNSIZED_BACKOFF_MAX", "0")
        assert settings.number(settings.UNSIZED_BACKOFF_MAX, 64.0) == 1.0

    def test_the_description_says_what_3600_gives_and_why_the_default_is_not_that(self):
        text = settings.explain(settings.UNSIZED_BACKOFF_MAX)
        assert "3600" in text and "uncapped" in text and "23-34s" in text
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        block = manifest.split("  unsized_backoff_max_seconds:", 1)[1].split("\n  live_status_disabled:", 1)[0]
        assert "default: 64" in block and "3600" in block and "KAME_UNSIZED_BACKOFF_MAX" in block


class TestAStatedNumberIsNeverMultiplied:
    """R10/R11. The ladder only fills the gap when the provider said nothing."""

    def test_a_stated_delay_is_obeyed_as_stated_mid_ladder(self):
        engine = Carousel()
        assert _ladder(engine, count=3) == [1.0, 2.0, 4.0]
        # The ladder's next rung would be 8. The provider said 3.
        assert engine.mark(ID, "a", False, 3.0, "rate_limit", now=NOW + 100, stated=True) == 3.0

    def test_a_stated_delay_resets_the_ladder(self):
        engine = Carousel()
        _ladder(engine, count=3)
        engine.mark(ID, "a", False, 3.0, "rate_limit", now=NOW + 100, stated=True)
        assert engine.unsized_backoff_step(ID, "a") == 0
        assert engine.unsized_backoff_label(ID, "a") == ""

    def test_a_number_stated_earlier_still_beats_the_ladder_for_later_bare_ones(self):
        # ``_stated_rl_ceiling`` predates this release and is the provider's
        # own number read off an earlier refusal. It outranks the ladder, and
        # a bare refusal that lands on it does not advance the ladder.
        engine = Carousel()
        engine.mark(ID, "a", False, 20.0, "rate_limit", now=NOW, stated=True)
        rests = [_bare(engine, now=NOW + 100 * i) for i in range(1, 5)]
        assert rests == [20.0] * 4
        assert engine.unsized_backoff_step(ID, "a") == 0

    def test_the_ladder_never_grows_a_stated_number(self):
        engine = Carousel()
        rests = [
            engine.mark(ID, "a", False, 7.0, "rate_limit", now=NOW + 100 * i, stated=True)
            for i in range(6)
        ]
        assert rests == [7.0] * 6


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


class TestTheSwitchOff:
    def test_off_is_the_flat_rest_of_every_earlier_release(self):
        engine = Carousel(unsized_throttle_backoff=False)
        assert _ladder(engine, count=6) == [carousel_mod.UNSIZED_THROTTLE_REST_S] * 6
        assert engine.unsized_backoff_step(ID, "a") == 0
        assert engine.unsized_backoff_label(ID, "a") == ""

    def test_the_flat_rest_dial_still_works_behind_it(self):
        engine = Carousel(unsized_throttle_backoff=False, unsized_throttle_rest_s=7.0)
        assert _ladder(engine, count=3) == [7.0, 7.0, 7.0]
        zero = Carousel(unsized_throttle_backoff=False, unsized_throttle_rest_s=0.0)
        assert _ladder(zero, count=2) == [0.0, 0.0]

    def test_switching_it_off_mid_run_puts_the_flat_rest_back(self):
        engine = Carousel()
        assert _ladder(engine, count=3) == [1.0, 2.0, 4.0]
        engine.unsized_throttle_backoff = False
        assert _bare(engine, now=NOW + 100) == 30.0

    def test_switching_it_back_on_starts_from_the_first_rung(self):
        engine = Carousel()
        _ladder(engine, count=4)
        engine.unsized_throttle_backoff = False
        _bare(engine, now=NOW + 100)
        engine.unsized_throttle_backoff = True
        assert _bare(engine, now=NOW + 200) == 1.0


class TestTheSettingItself:
    def test_it_is_declared_the_way_a_boolean_is_declared(self):
        key = settings.UNSIZED_THROTTLE_BACKOFF
        assert key == "unsized_throttle_backoff"
        assert key in settings.ALL_FLAGS
        assert key in settings.DEFAULTS_ON
        assert settings.env_name(key) == "KAME_UNSIZED_BACKOFF"
        assert settings.known(key)

    def test_it_is_on_with_nothing_configured(self):
        assert settings.is_on(settings.UNSIZED_THROTTLE_BACKOFF) is True

    @pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
    def test_the_environment_turns_it_off(self, monkeypatch, raw):
        monkeypatch.setenv("KAME_UNSIZED_BACKOFF", raw)
        assert settings.is_on(settings.UNSIZED_THROTTLE_BACKOFF) is False

    def test_a_person_can_read_what_it_is(self):
        text = (settings.title(settings.UNSIZED_THROTTLE_BACKOFF) + " " + settings.explain(settings.UNSIZED_THROTTLE_BACKOFF)).lower()
        assert "one error only" in text
        assert "1s, then 2, 4, 8" in text
        assert "resource_exhausted" in text
        assert "resets" in text
        assert "flat" in text

    def test_it_is_on_the_optional_shelf_and_described_for_the_panel(self):
        extra = next(g for g in settings.groups() if g["id"] == "extra")
        assert settings.UNSIZED_THROTTLE_BACKOFF in extra["keys"]
        rows = {row["key"]: row for row in settings.describe_all()}
        row = rows[settings.UNSIZED_THROTTLE_BACKOFF]
        assert row["kind"] == "flag"
        assert row["env"] == "KAME_UNSIZED_BACKOFF"
        assert row["value"] is True

    def test_the_manifest_declares_it_on_by_default(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        block = manifest.split("  unsized_throttle_backoff:", 1)[1].split("\n  live_status_disabled:", 1)[0]
        assert "type: bool" in block
        assert "default: true" in block
        assert "KAME_UNSIZED_BACKOFF" in block
        assert "one error only" in block.lower()
        assert "resource_exhausted" in block.lower()

    def test_the_version_is_one_eight_one_zero_where_it_is_written(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert 'version: "1.8.1.0"' in manifest
        assert core_mod.__version__ == "1.8.1.0"


class _Host:
    @staticmethod
    def module():
        def interruptible_api_call(agent, api_kwargs, *a, **kw):
            return None

        def interruptible_streaming_api_call(agent, api_kwargs, *a, **kw):
            return None

        return SimpleNamespace(
            interruptible_api_call=interruptible_api_call,
            interruptible_streaming_api_call=interruptible_streaming_api_call,
        )


class TestTheSettingReachesDispatch:
    """Through ``dispatch_binding.install()``, not a hand-built ``Carousel``."""

    def _install(self, monkeypatch, value=None):
        if value is None:
            monkeypatch.delenv("KAME_UNSIZED_BACKOFF", raising=False)
        else:
            monkeypatch.setenv("KAME_UNSIZED_BACKOFF", value)
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        monkeypatch.delenv("KAME_CAROUSEL_DISABLED", raising=False)
        original = carousel_mod.ENGINE.unsized_throttle_backoff
        try:
            binding = dispatch_binding.install(module=_Host.module())
            assert binding is not None, "install() refused the fake module"
            return binding, original
        except BaseException:
            carousel_mod.ENGINE.unsized_throttle_backoff = original
            raise

    def test_the_default_reaches_the_engine_and_a_real_mark_climbs(self, monkeypatch):
        binding, original = self._install(monkeypatch)
        try:
            assert binding.engine.unsized_throttle_backoff is True
            rests = [
                binding.engine.mark(ID, "key-install-on", False, 0.0, "rate_limit", now=NOW,
                                    bare_resource_exhausted=True)
                for _ in range(9)
            ]
            assert rests == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 64.0, 64.0]
            assert binding.engine.unsized_backoff_max_s == 64.0
        finally:
            carousel_mod.ENGINE.unsized_throttle_backoff = original

    def test_kame_unsized_backoff_zero_reaches_the_engine_and_a_real_mark_is_flat(self, monkeypatch):
        binding, original = self._install(monkeypatch, "0")
        try:
            assert binding.engine.unsized_throttle_backoff is False, (
                "KAME_UNSIZED_BACKOFF=0 left the real dispatch engine on the "
                "ladder — the same shape of bug RED_TEAM.md F3 found for KAME_MAX_HOLD"
            )
            rests = [
                binding.engine.mark(ID, "key-install-off", False, 0.0, "rate_limit", now=NOW,
                                    bare_resource_exhausted=True)
                for _ in range(3)
            ]
            assert rests == [30.0, 30.0, 30.0]
        finally:
            carousel_mod.ENGINE.unsized_throttle_backoff = original

    def test_the_owners_ceiling_reaches_the_same_engine_and_bounds_the_ladder(self, monkeypatch):
        monkeypatch.setenv("KAME_MAX_HOLD", "60")
        monkeypatch.setenv("KAME_UNSIZED_BACKOFF_MAX", "3600")
        binding, original = self._install(monkeypatch)
        original_hold = carousel_mod.ENGINE.max_hold_s
        original_cap = carousel_mod.ENGINE.unsized_backoff_max_s
        try:
            assert binding.engine.max_hold_s == 60.0
            rests = [
                binding.engine.mark(ID, "key-install-cap", False, 0.0, "rate_limit", now=NOW,
                                    bare_resource_exhausted=True)
                for _ in range(9)
            ]
            assert rests[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
            assert max(rests) == 60.0
        finally:
            carousel_mod.ENGINE.unsized_throttle_backoff = original
            carousel_mod.ENGINE.max_hold_s = original_hold
            carousel_mod.ENGINE.unsized_backoff_max_s = original_cap

    def test_the_cap_setting_reaches_the_engine_install_actually_uses(self, monkeypatch):
        monkeypatch.setenv("KAME_UNSIZED_BACKOFF_MAX", "10")
        binding, original = self._install(monkeypatch)
        original_cap = carousel_mod.ENGINE.unsized_backoff_max_s
        try:
            assert binding.engine.unsized_backoff_max_s == 10.0, (
                "KAME_UNSIZED_BACKOFF_MAX left the real dispatch engine's cap "
                "alone - the shape of bug RED_TEAM.md F3 found for KAME_MAX_HOLD"
            )
            rests = [
                binding.engine.mark(ID, "key-install-lowcap", False, 0.0, "rate_limit", now=NOW,
                                    bare_resource_exhausted=True)
                for _ in range(6)
            ]
            assert rests == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
        finally:
            carousel_mod.ENGINE.unsized_throttle_backoff = original
            carousel_mod.ENGINE.unsized_backoff_max_s = original_cap


# ---------------------------------------------------------------------------
# The rung is visible
# ---------------------------------------------------------------------------


RECORDED_BODY = {"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}


def _bare_refusal():
    """The owner's real recorded refusal, body and all."""
    exc = Exception("Resource has been exhausted (e.g. check quota).")
    exc.status_code = 429
    exc.body = RECORDED_BODY
    return exc


class RateLimitError(Exception):
    """The type name decisions.py gives Alibaba's and Z.AI's payloads."""


def _payload(message, *, status=429, body=None, cls=Exception):
    exc = cls(message)
    exc.status_code = status
    if body is not None:
        exc.body = body
    return exc


class TestTheRungIsVisible:
    def _refuse(self, engine, times, key="synthetic-key-a"):
        binding = dispatch_binding.DispatchBinding(engine=engine)
        for attempt in range(1, times + 1):
            action, kind, status = binding._on_failure(
                ID, key, _bare_refusal(), "test", attempt, False
            )
            assert action == "rotate" and status == 429
        return binding

    def test_the_events_row_says_which_rung_produced_the_rest(self):
        self._refuse(Carousel(), 3)
        rows = [r for r in reversed(EVENTS.recent()) if r["kind"] in ("rotation", "quarantine")]
        assert [r["sized_by"] for r in rows] == ["backoff.1", "backoff.2", "backoff.3"]
        assert [r["seconds"] for r in rows] == [1.0, 2.0, 4.0]

    def test_a_rest_of_the_same_size_from_the_flat_dial_is_told_apart(self):
        # A 4-second flat rest and a 4-second rung must not read the same.
        self._refuse(Carousel(unsized_throttle_backoff=False, unsized_throttle_rest_s=4.0), 1, key="synthetic-key-flat")
        flat = [r for r in EVENTS.recent() if r["kind"] == "rotation"][0]
        assert flat["seconds"] == 4.0
        assert not str(flat["sized_by"]).startswith("backoff")

    def test_the_log_line_names_the_rung(self, caplog):
        with caplog.at_level("WARNING"):
            self._refuse(Carousel(), 3, key="synthetic-key-log")
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "[backoff.1]" in text and "[backoff.2]" in text and "[backoff.3]" in text

    def test_the_log_line_is_unchanged_when_the_ladder_did_not_size_it(self, caplog):
        with caplog.at_level("WARNING"):
            self._refuse(Carousel(unsized_throttle_backoff=False), 1, key="synthetic-key-plain")
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "backoff" not in text and "resting 30s, taking the next key" in text

    def test_a_retained_older_hold_is_still_called_retained_not_a_rung(self):
        engine = Carousel()
        engine.mark(ID, "synthetic-key-r", False, 600.0, "rate_limit", now=__import__("time").time(), stated=True)
        binding = dispatch_binding.DispatchBinding(engine=engine)
        binding._on_failure(ID, "synthetic-key-r", _bare_refusal(), "test", 1, False)
        row = [r for r in EVENTS.recent() if r["kind"] in ("rotation", "quarantine")][-1]
        assert row["sized_by"] == "retained"

    def test_calls_jsonl_carries_rest_source_and_nothing_else_changed(self, tmp_path, monkeypatch):
        state = importlib.import_module(f"{PACKAGE}.state")
        monkeypatch.setattr(state, "state_dir", lambda: tmp_path)
        timings.forget()
        timings.record(identity=ID, key_fingerprint="key:ab12cd", attempt=2, outcome="rotate",
                       kind="rate_limit", status_code=429, rest_s=4.0, rest_source="backoff.3")
        timings.record(identity=ID, key_fingerprint="key:ab12cd", attempt=3, outcome="rotate",
                       kind="rate_limit", status_code=429, rest_s=4.0)
        rows = [json.loads(line) for line in (tmp_path / timings.FILENAME).read_text(encoding="utf-8").splitlines()]
        assert rows[0]["rest_source"] == "backoff.3" and rows[0]["rest_s"] == 4.0
        assert rows[1]["rest_source"] is None and rows[1]["rest_s"] == 4.0
        timings.forget()

    def test_dispatch_hands_the_rung_to_the_timings_line(self):
        source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
        assert "rest_source=self.engine.unsized_backoff_label(identity, key)" in source

    def test_the_doctor_table_says_the_bare_case_doubles(self):
        rows = {kind: text for kind, _rest, text in doctor_mod.EXPECTED_RESTS}
        assert "doubles" in rows["per_minute"] and "off = flat 30s" in rows["per_minute"]
        assert "RESOURCE_EXHAUSTED" in rows["per_minute"]

    def test_the_status_surface_says_whether_it_is_on(self, monkeypatch):
        binding = dispatch_binding.DispatchBinding(engine=Carousel())
        monkeypatch.setattr(pkg, "_dispatch_binding", binding, raising=False)
        for flag, expected in ((True, "RESOURCE_EXHAUSTED backoff: ON —"), (False, "RESOURCE_EXHAUSTED backoff: off")):
            binding.engine.unsized_throttle_backoff = flag
            lines = status_mod.QuotaCommand(None)._carousel()
            hits = [line for line in lines if "RESOURCE_EXHAUSTED backoff" in line]
            assert len(hits) == 1 and expected in hits[0]
            # Plain chat text, one short line, and nothing on the spinner.
            assert len(hits[0]) < 130
        spinner = re.search(r"def status_line\(.*?\n(?=\ndef |\nclass )", (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8"), re.S)
        assert spinner and "backoff" not in spinner.group(0)


class TestOnlyTheOwnersErrorClimbs:
    """He wants to test this one error and nothing else. Evidence, never a name (R01)."""

    def _rests(self, exc, times=3, key="synthetic-key-shape", engine=None):
        binding = dispatch_binding.DispatchBinding(engine=engine or Carousel())
        for attempt in range(1, times + 1):
            binding._on_failure(ID, key, exc, "test", attempt, False)
        rows = [r for r in reversed(EVENTS.recent()) if r["kind"] in ("rotation", "quarantine")]
        return [r["seconds"] for r in rows], [r["sized_by"] for r in rows]

    def _is_todays_flat_rest(self, exc, seconds, sized):
        """Whatever the flat rest is for this payload, it did not climb.

        The number itself depends on which classifier answered (30s through
        the catalog, 20s through the table); what matters is that it is the
        same with the switch off, constant, and untagged.
        """
        EVENTS.clear()
        off = self._rests(exc, key="synthetic-key-off", engine=Carousel(unsized_throttle_backoff=False))[0]
        assert seconds == off and len(set(seconds)) == 1 and seconds[0] >= 20.0
        assert not any(str(v).startswith("backoff") for v in sized)

    def test_the_real_recorded_body_engages_the_ladder(self):
        # If the narrowing ever excludes the case it exists for, this fails.
        evidence = importlib.import_module(f"{PACKAGE}.core.evidence").harvest(_bare_refusal())
        assert carousel_mod.is_bare_resource_exhausted(evidence) is True
        seconds, sized = self._rests(_bare_refusal())
        assert seconds == [1.0, 2.0, 4.0]
        assert sized == ["backoff.1", "backoff.2", "backoff.3"]

    def test_the_hosts_own_rendering_of_the_same_refusal_engages_it_too(self):
        # decisions.py's RUN_429 / the replayed corpus carry the status only
        # as the host formats it into the message.
        exc = _payload("Gemini HTTP 429 (RESOURCE_EXHAUSTED): Resource has been exhausted (e.g. check quota).")
        exc.code = "gemini_rate_limited"  # what the host's Gemini adapter sets, as in decisions.py
        seconds, sized = self._rests(exc, key="synthetic-key-host")
        assert seconds == [1.0, 2.0, 4.0] and sized[0] == "backoff.1"

    def test_alibaba_throttling_ratequota_keeps_the_flat_rest(self):
        exc = _payload("Throttling.RateQuota: request denied", cls=RateLimitError)
        seconds, sized = self._rests(exc, key="synthetic-key-alibaba")
        self._is_todays_flat_rest(exc, seconds, sized)

    def test_zai_usage_limit_reached_keeps_the_flat_rest(self):
        exc = _payload("Usage limit reached for your plan", cls=RateLimitError)
        seconds, sized = self._rests(exc, key="synthetic-key-zai")
        self._is_todays_flat_rest(exc, seconds, sized)

    def test_a_bare_429_with_no_structured_status_keeps_the_flat_rest(self):
        # decisions.py's other real shape: "Error code: NNN - {'status': NNN, 'title': 'Too Many Requests'}"
        exc = _payload("Error code: 429 - {'status': 429, 'title': 'Too Many Requests'}", cls=RateLimitError)
        seconds, sized = self._rests(exc, key="synthetic-key-bare")
        self._is_todays_flat_rest(exc, seconds, sized)

    def test_the_gate_itself_by_evidence(self):
        harvest = importlib.import_module(f"{PACKAGE}.core.evidence").harvest
        gate = carousel_mod.is_bare_resource_exhausted
        assert gate(harvest(_bare_refusal())) is True
        # A stated number, from anywhere, takes the refusal out of the shape.
        assert gate(harvest(_bare_refusal()), stated=True) is False
        with_delay = _payload("Resource has been exhausted", body={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "29s"}]}})
        assert gate(harvest(with_delay)) is False
        with_quota_id = _payload("Quota exceeded", body={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}})
        assert gate(harvest(with_quota_id)) is False
        header = _payload("Resource has been exhausted", body=RECORDED_BODY)
        header.headers = {"Retry-After": "12"}
        assert gate(harvest(header)) is False
        # Not a 429, or not RESOURCE_EXHAUSTED.
        assert gate(harvest(_payload("Resource has been exhausted", status=503, body=RECORDED_BODY))) is False
        assert gate(harvest(_payload("nope", body={"error": {"code": 429, "status": "UNAVAILABLE"}}))) is False
        assert gate(None) is False

    def test_panel_status_and_setting_say_bare_resource_exhausted_not_unsized_throttles(self):
        title = settings.title(settings.UNSIZED_THROTTLE_BACKOFF)
        assert "RESOURCE_EXHAUSTED" in title
        assert "RESOURCE_EXHAUSTED" in settings.explain(settings.UNSIZED_THROTTLE_BACKOFF)
        assert "one error only" in settings.explain(settings.UNSIZED_THROTTLE_BACKOFF)
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        block = manifest.split("  unsized_throttle_backoff:", 1)[1].split("\n  unsized_backoff_max_seconds:", 1)[0]
        assert "RESOURCE_EXHAUSTED" in block and "one error only" in block
        assert "bare-429" in PANEL.read_text(encoding="utf-8")


class TestTheProducerAndTheConsumerAgree:
    """R55: a ``sized_by`` value the panel has no label for renders as nothing."""

    def _node_labels(self, inputs):
        node = shutil.which("node")
        assert node, "Node is required for the actual panel logic gate"
        source = PANEL.read_text(encoding="utf-8")
        table = source[source.index("const SIZED_BY_LABELS ="):source.index("function EventRow(")]
        result = subprocess.run(
            [node, "-e", table + "\nconsole.log(JSON.stringify(" + json.dumps(inputs) + ".map(sizedByLabel)));"],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def test_every_rung_the_engine_can_write_has_a_real_label(self):
        engine = Carousel(max_hold_s=86400.0)
        produced = set()
        for _ in range(20):
            _bare(engine)
            produced.add(engine.unsized_backoff_label(ID, "a"))
        assert "backoff.1" in produced and "backoff.20" in produced
        labels = self._node_labels(sorted(produced))
        assert labels == [["bare-429 backoff", "weak"]] * len(produced)

    def test_it_is_marked_weak_because_it_is_our_own_number(self):
        source = PANEL.read_text(encoding="utf-8")
        line = next(l for l in source.splitlines() if l.strip().startswith("backoff:"))
        assert "'weak'" in line

    def test_the_label_map_still_knows_every_source_the_vocabulary_lists(self):
        vocabulary = importlib.import_module(f"{PACKAGE}.core.vocabulary")
        source = PANEL.read_text(encoding="utf-8")
        start = source.index("const SIZED_BY_LABELS = {")
        block = source[start:source.index("\n}", start)]
        keys = set(re.findall(r"^\s*(\w+):\s*\['", block, re.M))
        assert not (vocabulary.SIZED_BY_SOURCES - keys)
        assert "backoff" in keys
