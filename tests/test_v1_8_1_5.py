"""1.8.1.5 -- a setting typed as ``nan`` is refused, not crashed on.

``float("nan")`` parses, and every comparison with NaN is false, so it slipped
past the range check in ``settings.parse`` and then raised ValueError at
``int(number)``: ``/kame set max_hold_seconds nan`` answered with a traceback.
The environment reader clamped the same value to whichever bound ``max`` and
``min`` happened to return (``KAME_MAX_HOLD_SECONDS=nan`` became 60s), which is
not a reading of anything the person wrote.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1815_under_test"


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


_load_package()
settings = importlib.import_module(f"{PACKAGE}.settings")
control = importlib.import_module(f"{PACKAGE}.control")

NUMBERS = sorted(settings.ALL_NUMBERS)


@pytest.mark.parametrize("key", NUMBERS)
@pytest.mark.parametrize("raw", ["nan", "NaN", " -nan "])
def test_parse_refuses_nan_with_a_sentence(key, raw):
    value, error = settings.parse(key, raw)
    assert value is None
    assert "is not one" in error


@pytest.mark.parametrize("key", NUMBERS)
def test_parse_still_refuses_infinity_by_range(key):
    value, error = settings.parse(key, "inf")
    assert value is None
    assert "outside" in error


@pytest.mark.parametrize("key", NUMBERS)
def test_an_environment_nan_says_nothing_so_the_default_decides(key):
    assert settings._as_number("nan", key) is None


def test_the_environment_reader_falls_back_to_the_default(monkeypatch):
    for variable in settings._env_names(settings.MAX_HOLD):
        monkeypatch.setenv(variable, "nan")
    assert settings.number(settings.MAX_HOLD, 3600.0) == 3600.0


def test_ordinary_numbers_are_untouched():
    assert settings.parse(settings.MAX_HOLD, "1800") == ("1800", "")
    assert settings._as_number("999999", settings.MAX_HOLD) == 86400.0


class TestAPanelRequestThatCrashesIsStillAnswered:
    """``control.poll`` promises never to raise; the panel waits on the id."""

    def test_the_request_is_recorded_as_failed(self, tmp_path, monkeypatch):
        path = tmp_path / "control.json"
        path.write_text(json.dumps({
            "schema": control.SCHEMA, "id": "req-1", "action": "set",
            "key": settings.MAX_HOLD, "value": "1800",
        }), encoding="utf-8")
        monkeypatch.setattr(control, "control_path", lambda: path)
        recorded = []
        monkeypatch.setattr(control, "_record", recorded.append)

        def boom(*_a, **_k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(control, "_apply", boom)
        assert control.poll() is True
        assert recorded[-1]["id"] == "req-1"
        assert recorded[-1]["ok"] is False


# --------------------------------------------------------------------------
# The shared pool-health file heals after damage on disk.
#
# Every write reads the file first. Bytes that were not UTF-8 raised past
# ``except OSError``, and a row the prune or the newest-event comparison could
# not do arithmetic on raised inside the write -- so the damaged file was never
# replaced: sharing between profiles stayed off for good, silently, and *Clear
# pool* reported that it could not write. Measured before the fix: 5 of 11
# damage shapes never healed (experiments/shared_health_corruption.py).
# --------------------------------------------------------------------------

shared_health = importlib.import_module(f"{PACKAGE}.core.shared_health")
_SCHEMA = shared_health.SCHEMA


def _doc(model):
    return json.dumps({"schema": _SCHEMA, "model": model, "account": {}}).encode()


NEVER_HEALED = {
    "invalid utf-8": b"\xff\xfe\x80garbage",
    "latin-1 json": _doc({"café": {}}).replace(b"\\u00e9", b"\xe9"),
    "at is text": _doc({"m": {"fp": {"until": 9e9, "at": "yesterday"}}}),
    "at is an object": _doc({"m": {"other": {"until": 1, "at": {"x": 1}}}}),
    "rows are a list": _doc({"m": [1, 2]}),
    "at is NaN": b'{"schema": ' + json.dumps(_SCHEMA).encode()
    + b', "model": {"m": {"other": {"until": 1, "at": NaN}}}, "account": {}}',
}


def _health(path, profile):
    return shared_health.SharedHealth(path=path, profile=profile, enabled_fn=lambda: True)


@pytest.mark.parametrize("damage", sorted(NEVER_HEALED))
def test_the_next_write_replaces_a_damaged_file(tmp_path, damage):
    path = tmp_path / "pool_health.json"
    path.write_bytes(NEVER_HEALED[damage])
    now = 1_000_000.0
    _health(path, "base").record(
        scope="model", subject="m", fingerprint_key="fp", until=now + 60, kind="rl", at=now
    )
    assert _health(path, "k").model_entry("m", "fp", now=now + 1) == (now + 60, now)


@pytest.mark.parametrize("damage", sorted(NEVER_HEALED))
def test_clear_pool_still_writes_through_damage(tmp_path, damage):
    path = tmp_path / "pool_health.json"
    path.write_bytes(NEVER_HEALED[damage])
    assert _health(path, "base").release_all(now=1_000_000.0) is not None


def test_a_damaged_row_costs_that_row_and_keeps_its_neighbours(tmp_path):
    path = tmp_path / "pool_health.json"
    path.write_bytes(_doc({"m": {"bad": {"until": 1, "at": "x"}, "good": {"until": 50.0, "at": 5.0}}}))
    assert _health(path, "k").model_entry("m", "good", now=10.0) == (50.0, 5.0)
    assert _health(path, "k").model_entry("m", "bad", now=10.0) == (0.0, 0.0)


# --------------------------------------------------------------------------
# A ledger or journal holding an integer too large for a float decodes.
#
# ``_coerce_float`` caught TypeError and ValueError; ``float(10**400)`` raises
# OverflowError, and JSON holds such integers happily, so one hand-edited or
# damaged row raised out of ``LedgerStore.load`` -- which runs on every
# credential selection. Found by experiments/state_decode_fuzz.py (650 runs).
# --------------------------------------------------------------------------

ledger = importlib.import_module(f"{PACKAGE}.core.ledger")
journal = importlib.import_module(f"{PACKAGE}.core.journal")
HUGE = 10 ** 400


def test_a_huge_deadline_drops_that_bench_only():
    good = {"credential_id": "c2", "provider": "openai", "model": "m", "reset_at": 2.0e9, "recorded_at": 1.0e9}
    payload = {
        "version": ledger.SCHEMA_VERSION,
        "benches": [dict(good, credential_id="c1", reset_at=HUGE), good],
    }
    rebuilt = ledger.Ledger.from_dict(payload)
    assert [row["credential_id"] for row in rebuilt.to_dict()["benches"]] == ["c2"]


def test_a_huge_journal_moment_drops_that_block_only():
    payload = {
        "version": journal.SCHEMA_VERSION,
        "blocks": [{"at": HUGE, "provider": "openai", "model": "m", "credential_id": "c1"},
                   {"at": 1.0e9, "provider": "openai", "model": "m", "credential_id": "c2"}],
        "recoveries": [{"credential_id": "c1", "model": "m", "blocked_at": HUGE, "recovered_at": 1.0}],
    }
    rebuilt = journal.Journal.from_dict(payload).to_dict()
    assert [row["credential_id"] for row in rebuilt["blocks"]] == ["c2"]
    assert rebuilt["recoveries"] == []


# --------------------------------------------------------------------------
# A provider number too large for a float is read as no number, not raised.
#
# ``float(10**400)`` raises OverflowError, which none of the retry-hint readers
# caught. A body with ``"retryDelay": <huge int>`` or an ``X-RateLimit-Reset``
# of the same raised out of ``DispatchBinding._on_failure`` -- the handler in
# the ``except`` around the host's API call -- so the turn died on a KAME
# OverflowError instead of the key being rotated. Found by the failure-path
# fuzz with huge integers added (27 crashes in 26,164 runs; 0 after; the
# Agent Zero port had none).
# --------------------------------------------------------------------------

dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
evidence = importlib.import_module(f"{PACKAGE}.core.evidence")
quota = importlib.import_module(f"{PACKAGE}.core.quota")


class _HugeRefusal(Exception):
    status_code = 429

    def __init__(self, body, retry_after=None):
        super().__init__("Rate limit exceeded")
        self.body = body
        if retry_after is not None:
            self.retry_after = retry_after


HUGE_BODIES = {
    "retryDelay": {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": HUGE}]}},
    "X-RateLimit-Reset": {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day",
                                    "metadata": {"headers": {"X-RateLimit-Reset": -HUGE}}}},
    "retry_delay seconds": {"error": {"code": 429, "retry_delay": {"seconds": HUGE}}},
}


@pytest.mark.parametrize("shape", sorted(HUGE_BODIES))
def test_the_failure_handler_rotates_on_a_huge_number(shape):
    binding = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    verdict, kind, status = binding._on_failure(
        "openrouter:m", "sk-huge-0001", _HugeRefusal(HUGE_BODIES[shape]), "x", 1, False
    )
    assert verdict != "raise"
    assert status == 429


def test_a_huge_retry_after_attribute_is_ignored():
    binding = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    verdict, _kind, _status = binding._on_failure(
        "openai:m", "sk-huge-0002", _HugeRefusal({}, retry_after=HUGE), "x", 1, False
    )
    assert verdict != "raise"


def test_the_readers_answer_none():
    assert quota.parse_absolute_timestamp(HUGE) is None
    assert quota._bounded_relative(HUGE) is None
    assert evidence.retry_info_seconds(HUGE_BODIES["retryDelay"]) is None


# --------------------------------------------------------------------------
# A rest never outlasts the ceiling measured from now.
#
# ``mark`` stores every hold at most ``max_hold_s`` ahead of the moment it ran,
# but holds are wall-clock deadlines: a clock that steps back afterwards (NTP,
# a resume, a hand-set clock) moved them further out. Measured, a 30s rest read
# back as 7229s after a two-hour step (experiments/clock_step_back.py; the
# Agent Zero port the same). The shared file's numbers were already re-bounded
# on read (RED_TEAM F6); the local ones now are too.
# --------------------------------------------------------------------------

KEYS = ["sk-clock-aaaaaaaaaaaaaaaa1", "sk-clock-bbbbbbbbbbbbbbbb2"]
T0 = 2_000_000_000.0


def _rested(delay=30.0, **options):
    c = carousel.Carousel(**options)
    c.select("openai:m", KEYS, now=T0)
    c.mark("openai:m", KEYS[0], False, delay=delay, kind="rate_limit", now=T0)
    return c


def test_a_clock_stepped_back_cannot_stretch_a_rest_past_the_ceiling():
    c = _rested()
    later_but_earlier = T0 + 1 - 7200
    assert c.next_recovery_seconds("openai:m", KEYS[:1], now=later_but_earlier) <= c.max_hold_s
    assert c.snapshot(now=later_but_earlier)["openai:m"]["resting"] == 1


def test_the_key_is_offered_once_the_ceiling_from_the_step_has_passed():
    # The bound is stored, not recomputed per read: one recomputed from ``now``
    # moves forward with ``now`` and never releases the key.
    c = _rested()
    stepped = T0 - 7200
    assert c.healthy_count("openai:m", KEYS[:1], now=stepped) == 0
    assert c.healthy_count("openai:m", KEYS[:1], now=stepped + c.max_hold_s + 1) == 1


def test_an_account_hold_is_trimmed_the_same_way():
    c = carousel.Carousel()
    c.select("openai:m", KEYS, now=T0)
    c.mark("openai:m", KEYS[0], False, delay=30.0, kind="rate_limit", now=T0,
           scope=carousel.QuotaScope.ACCOUNT)
    stepped = T0 - 7200
    assert c.healthy_count("openai:m", KEYS[:1], now=stepped) == 0
    assert c.healthy_count("openai:m", KEYS[:1], now=stepped + c.max_hold_s + 1) == 1


def test_a_lowered_dial_applies_to_a_rest_already_running():
    c = _rested(delay=3000.0)
    c.max_hold_s = 600.0
    assert c.next_recovery_seconds("openai:m", KEYS[:1], now=T0 + 1) <= 600.0


def test_an_ordinary_rest_is_untouched():
    c = _rested()
    assert c.next_recovery_seconds("openai:m", KEYS[:1], now=T0 + 1) == pytest.approx(29.0)


# --------------------------------------------------------------------------
# RED_TEAM F6, over time: a sibling's hold is released at the reader's ceiling.
#
# The F6 fix capped a shared deadline at ``now + max_hold_s`` on every read,
# and its tests checked one instant. A cap recomputed from ``now`` moves with
# ``now``: measured, a sibling profile's 9h hold kept the key out for the full
# 9h in a reader whose ceiling is 1h, while every read in between reported
# "back in 3600s" (experiments/shared_ceiling_moving_target.py).
# --------------------------------------------------------------------------

HOUR = 3600.0


def _sibling_hold(tmp_path, scope=None):
    path = tmp_path / "pool_health.json"
    store = lambda profile: shared_health.SharedHealth(path=path, profile=profile, enabled_fn=lambda: True)
    writer = carousel.Carousel(max_hold_s=9 * HOUR, shared_health_store=store("base"))
    options = {"scope": scope} if scope else {}
    writer.mark("gemini:gemini-3.8-flash", KEYS[0], False, 9 * HOUR, "daily", now=T0, stated=True, **options)
    return carousel.Carousel(max_hold_s=HOUR, shared_health_store=store("k"))


def _healthy(reader, now):
    reader._shared._cache_checked_at = 0.0
    return reader.healthy_count("gemini:gemini-3.8-flash", KEYS[:1], now=now)


@pytest.mark.parametrize("scope", [None, carousel.QuotaScope.ACCOUNT])
def test_a_siblings_long_hold_is_released_at_the_readers_ceiling(tmp_path, scope):
    reader = _sibling_hold(tmp_path, scope)
    assert _healthy(reader, T0 + 1) == 0
    assert _healthy(reader, T0 + 0.5 * HOUR) == 0
    assert _healthy(reader, T0 + HOUR + 2) == 1
    # And stays released as the sibling's own deadline draws near.
    assert _healthy(reader, T0 + 8.5 * HOUR) == 1


def test_the_eta_counts_down_instead_of_standing_still(tmp_path):
    reader = _sibling_hold(tmp_path)
    reader._shared._cache_checked_at = 0.0
    first = reader.next_recovery_seconds("gemini:gemini-3.8-flash", KEYS[:1], now=T0 + 1)
    reader._shared._cache_checked_at = 0.0
    later = reader.next_recovery_seconds("gemini:gemini-3.8-flash", KEYS[:1], now=T0 + 1801)
    assert first == pytest.approx(HOUR)
    assert later == pytest.approx(HOUR - 1800)


def test_a_new_event_from_the_sibling_is_capped_afresh(tmp_path):
    reader = _sibling_hold(tmp_path)
    assert _healthy(reader, T0 + 1) == 0
    assert _healthy(reader, T0 + HOUR + 2) == 1
    path = tmp_path / "pool_health.json"
    writer = carousel.Carousel(max_hold_s=9 * HOUR,
                               shared_health_store=shared_health.SharedHealth(
                                   path=path, profile="base", enabled_fn=lambda: True))
    writer.mark("gemini:gemini-3.8-flash", KEYS[0], False, 9 * HOUR, "daily", now=T0 + 2 * HOUR, stated=True)
    assert _healthy(reader, T0 + 2 * HOUR + 1) == 0
    assert _healthy(reader, T0 + 3 * HOUR + 2) == 1
