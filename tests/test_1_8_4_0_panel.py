"""1.8.4.0 U1 to U7 and the snapshot fields the page reads (E4, E5, E6).

The page half (JavaScript) is checked with node in ``tests/ui_1840.mjs`` and skips without node. The
backend half is checked here: the physical key counts, the one-line text of the quick controls, the
fields of the real snapshot, and the fixtures the preview draws.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import test_1_8_2_5_stream_race as R

PACKAGE = R.PACKAGE
state = importlib.import_module(f"{PACKAGE}.state")
settings = R.settings
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_page_says_one_count_and_the_truth():
    script = Path(__file__).with_name("ui_1840.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def _rows(per_model, models=4):
    """``models`` pools over the same ``per_model`` physical keys, ``(fingerprint, code)`` each."""
    return [{"identity": f"gemini:m{i}", "keys": len(per_model), "healthy": sum(1 for _f, c in per_model if c in "rf"),
             "soonest_s": None, "invalid": 0, "retired": 0,
             "key_states": [[fp, code, None, ""] for fp, code in per_model]} for i in range(models)]


def test_the_totals_count_each_physical_key_once():
    keys = [(f"key:{i:06x}", "s") for i in range(14)]
    totals = state._totals(_rows(keys))
    assert totals["keys"] == 56 and totals["healthy"] == 0          # old readers: unchanged
    assert totals["physical_keys"] == 14 and totals["physical_ready"] == 0


def test_a_key_ready_for_one_model_is_ready():
    keys = [(f"key:{i:06x}", "r" if i < 5 else "s") for i in range(14)]
    rows = _rows(keys, models=2)
    rows[1]["key_states"][13][1] = "r"       # one more key ready, for the other model only
    totals = state._totals(rows)
    assert totals["physical_keys"] == 14 and totals["physical_ready"] == 6


def test_no_rows_no_keys():
    totals = state._totals([])
    assert totals["physical_keys"] == 0 and totals["physical_ready"] == 0


def test_rows_without_key_states_do_not_break_the_totals():
    totals = state._totals([{"keys": 3, "healthy": 3, "invalid": 0, "retired": 0, "soonest_s": None}])
    assert totals["keys"] == 3 and totals["physical_keys"] == 0


def test_only_the_three_quick_controls_have_a_line():
    lines = {key: settings.describe(key)["line"] for key in (settings.HEDGE_MODE, settings.MODEL_SWITCH, settings.REPLY_NOTICE)}
    assert lines == {
        "hedge_mode": "How hard KAME works to answer sooner.",
        "model_switch": "When your model cannot answer, the next one on your list does.",
        "reply_notice": "A short notice after each reply.",
    }
    others = [row for row in settings.describe_all() if row["key"] not in lines]
    assert others and all(row["line"] == "" for row in others)


def test_no_word_of_the_long_text_was_deleted():
    # the line is an addition; the sentence and the "when" of each quick control are still sent for the Why? toggle
    for key in (settings.HEDGE_MODE, settings.MODEL_SWITCH, settings.REPLY_NOTICE):
        row = settings.describe(key)
        assert row["help"] and row["title"]


def test_the_real_snapshot_carries_the_new_fields():
    document = state.snapshot(None, None)
    assert set(document["lifetime"]) == {"since", "replies", "saved", "key_switches", "model_switches"}
    assert "physical_keys" in document["totals"] and "physical_ready" in document["totals"]


def test_a_pool_row_carries_sent_24h_and_the_daily_number(monkeypatch, tmp_path):
    life_mod = importlib.import_module(f"{PACKAGE}.core.lifetime")
    import sys

    life = life_mod.Lifetime(path=lambda: tmp_path / "l.json")
    monkeypatch.setattr(sys.modules[f"{PACKAGE}.core.lifetime"], "LIFETIME", life)
    carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
    engine = carousel.ENGINE
    engine.forget()
    keys = [f"AIzaSyPANEL{i}" + "0" * 29 for i in range(3)]
    engine.select("gemini:gemini-panel", keys)
    life.sent("gemini:gemini-panel")
    life.sent("gemini:gemini-panel")
    life.note_limit("gemini:gemini-panel", {"error": {"details": [{"violations": [{
        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}]}})
    try:
        rows = {r["identity"]: r for r in state._pool_rows()}
    finally:
        engine.forget()
    row = rows["gemini:gemini-panel"]
    assert (row["sent_24h"], row["daily_limit"], row["daily_scope"]) == (2, 20, "project")
    assert row["reserve_keys"] == []


def test_a_reserve_key_is_named_in_its_pool_row(monkeypatch):
    carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
    pacing = importlib.import_module(f"{PACKAGE}.core.pacing")
    engine = carousel.ENGINE
    engine.forget()
    keys = [f"AIzaSyRESV{i}" + "0" * 29 for i in range(3)]
    monkeypatch.setenv("KAME_KEY_RESERVE", carousel.fingerprint(keys[2]))
    settings.forget()
    pacing.PACER._synced_at.clear()
    engine.select("gemini:gemini-reserve", keys)
    try:
        row = {r["identity"]: r for r in state._pool_rows()}["gemini:gemini-reserve"]
    finally:
        engine.forget()
        pacing.PACER.clear()
        settings.forget()
    assert row["reserve_keys"] == [carousel.fingerprint(keys[2])]


# -- U7: fixtures -----------------------------------------------------------------------------------
def _plugin_version() -> str:
    text = (ROOT / "hermes-kame-api-rotation" / "core" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def test_every_fixture_shows_the_plugin_version():
    versions = {path.name: json.loads(path.read_text(encoding="utf-8")).get("version")
                for path in (ROOT / "tools" / "panel_preview" / "fixtures").glob("*.json")}
    assert versions and set(versions.values()) == {_plugin_version()}, versions


def test_the_preview_never_types_the_version_by_hand():
    source = (ROOT / "tools" / "panel_preview.mjs").read_text(encoding="utf-8")
    assert "PLUGIN_VERSION" in source and "version: '1.6.0.1'" not in source


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_a_fixture_drawn_later_keeps_its_clocks_together(tmp_path):
    """'first key back in 0s' beside '1m 10s' was a frozen fixture drawn later, not the page."""
    out = tmp_path / "all-resting.html"
    fixture = ROOT / "tools" / "panel_preview" / "fixtures" / "all-resting.json"
    result = subprocess.run(["node", str(ROOT / "tools" / "panel_preview.mjs"), "--fixture", str(fixture),
                             "--tab", "overview", "--out", str(out)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    text = re.sub(r"<[^>]+>", " ", out.read_text(encoding="utf-8"))
    assert "first key back in 0s" not in text
    assert re.search(r"first key back in 1m", text)
