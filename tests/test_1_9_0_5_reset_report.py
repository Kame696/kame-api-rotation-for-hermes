"""1.9.0.5 (E3): *Clear pool* says what it cleared - counts only, never key material."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_reset_report_1905_under_test"


def _load():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return tuple(importlib.import_module(f"{PACKAGE}.{name}")
                 for name in ("control", "core.carousel", "core.events", "core.quota", "facade"))


control, carousel, events, quota, facade = _load()
ACCOUNT = quota.QuotaScope.ACCOUNT

KEYS = [f"AIzaSyRESET{i}" + "0" * 28 for i in range(4)]
A, B = "gemini:model-a", "gemini:model-b"


@pytest.fixture()
def engine(monkeypatch):
    fresh = carousel.Carousel()
    monkeypatch.setattr(carousel, "ENGINE", fresh)
    events.EVENTS.clear()
    return fresh


def _populate(engine, now):
    engine._no_answer_since[A] = now - 10_000.0
    engine.mark(A, KEYS[0], False, 0.0, "daily", now=now)                       # a daily label (3600 s)
    engine.mark(A, KEYS[1], False, 600.0, "rate_limit", now=now, stated=True)   # a stated deadline
    engine.mark(A, KEYS[2], False, 30.0, "rate_limit", now=now)                 # unsized
    engine.mark(A, KEYS[3], False, 120.0, "rate_limit", now=now, stated=True, scope=ACCOUNT)
    engine.mark(B, KEYS[3], False, 120.0, "rate_limit", now=now, stated=True, scope=ACCOUNT)  # confirmed: account hold


def test_the_summary_counts_by_kind_and_never_holds_a_key(engine):
    now = time.time()
    _populate(engine, now)
    summary = engine.reset_summary(now + 1.0)
    assert summary["keys_held"] == 4
    assert summary["account_holds"] == 1
    kinds = summary["by_kind"]
    assert kinds["daily"] == 1 and kinds["stated"] == 1 and kinds["account"] >= 1
    assert kinds["unsized"] >= 1
    assert 3000 < summary["longest_hold_s"] <= 3600
    dumped = json.dumps(summary)
    assert all(k not in dumped and k[:10] not in dumped for k in KEYS)
    assert carousel.fingerprint(KEYS[0]) not in dumped


def test_clear_pool_logs_and_emits_one_reset_event_with_counts_before_clearing(engine, caplog):
    now = time.time()
    _populate(engine, now)
    with caplog.at_level("INFO"):
        ok, detail = control._apply("clear_pool", "", None)
    assert ok
    rows = [row for row in events.EVENTS.recent(50) if row["kind"] == "reset"]
    assert len(rows) == 1
    row = rows[0]
    assert "4 key(s) were resting" in row["reason"] and "1 account hold" in row["reason"]
    payload = json.loads(row["detail"]) if row["detail"].startswith("{") else {}
    assert payload.get("keys_held") == 4
    # the log line carries the same sentence
    assert any("Clear pool: 4 key(s) were resting" in r.getMessage() for r in caplog.records)
    # no key material anywhere
    blob = json.dumps(row) + " ".join(r.getMessage() for r in caplog.records)
    assert all(k not in blob for k in KEYS)
    # and the pool really was cleared
    assert engine.healthy_count(A, KEYS) == 4
    assert engine._account_hold == {}


def test_a_report_that_cannot_be_made_never_blocks_the_reset(engine, monkeypatch):
    now = time.time()
    _populate(engine, now)
    monkeypatch.setattr(engine, "reset_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, detail = control._apply("clear_pool", "", None)
    assert ok
    assert engine.healthy_count(A, KEYS) == 4


def test_one_model_wake_does_not_report_a_global_reset(engine):
    now = time.time()
    _populate(engine, now)
    ok, _detail = control._apply("clear_pool", A, None)
    assert ok
    assert [row for row in events.EVENTS.recent(50) if row["kind"] == "reset"] == []


def test_the_panel_and_python_know_the_two_new_kinds():
    assert {"retest", "reset"} <= events.GOOD_KINDS
    source = (PLUGIN_DIR / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "'handover', 'retest', 'reset'])" in source
    for kind in ("retest", "reset"):
        assert f"\n  {kind}: [" in source and f"\n  {kind}: '" in source
