"""1.8.2.9 (decisions/0031 J1): the speed modes in words a non-programmer understands.

Stored values never change (off / adaptive / max). The words say what each mode costs, as a
multiplier computed from the live ``hedge_budget_pct`` so they cannot lie when that is changed.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_speed_words.py -q
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from tests import test_1_8_2_5_stream_race as R

settings = importlib.import_module(f"{R.PACKAGE}.settings")
PLUGIN = Path(R.PLUGIN_DIR)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    yield monkeypatch
    settings.forget()


def labels():
    return settings.describe(settings.HEDGE_MODE)["choice_labels"]


def test_the_labels_say_what_each_mode_costs(env):
    assert labels() == {"off": "Saver · 1x", "adaptive": "Balanced · up to 1.1x", "max": "Faster · up to 1.3x"}


def test_the_numbers_follow_the_live_budget(env):
    env.setenv("KAME_HEDGE_BUDGET_PCT", "20")
    settings.forget()
    assert labels()["adaptive"] == "Balanced · up to 1.2x" and labels()["max"] == "Faster · up to 1.6x"
    env.setenv("KAME_HEDGE_BUDGET_PCT", "40")                    # 3 x 40 is capped at 100%
    settings.forget()
    assert labels()["adaptive"] == "Balanced · up to 1.4x" and labels()["max"] == "Faster · up to 2x"


def test_the_stored_values_and_their_order_are_unchanged(env):
    row = settings.describe(settings.HEDGE_MODE)
    assert row["choice_order"] == ["off", "adaptive", "max"]
    assert row["choices"] == ["adaptive", "max", "off"]
    assert row["default"] == "adaptive"


def test_each_choice_has_one_plain_sentence_with_the_live_percentage(env):
    helped = settings.describe(settings.HEDGE_MODE)["choice_help"]
    assert set(helped) == {"off", "adaptive", "max"}
    assert "10%" in helped["adaptive"] and "30%" in helped["max"]
    assert "Balanced" in helped["max"] and "10 minutes" in helped["max"]
    assert settings.describe(settings.HEDGE_MODE)["choice_footnote"].startswith("Balanced and Faster may also")


def test_other_choice_settings_have_no_speed_words(env):
    row = settings.describe(settings.MODEL_SWITCH)
    assert row["choice_help"] == {} and row["choice_footnote"] == ""
    assert row["choice_labels"] == {"auto": "Automatic", "timed": "Set time", "never": "Never"}


def test_no_text_in_the_package_still_says_fastest():
    offenders = []
    for path in PLUGIN.rglob("*"):
        if path.suffix not in {".py", ".js", ".md", ".yaml", ".json"} or "__pycache__" in path.parts:
            continue
        if "Fastest" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(path.relative_to(PLUGIN).as_posix())
    assert offenders == []
