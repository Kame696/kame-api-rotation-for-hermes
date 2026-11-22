"""1.9.0.5 (PLAN C5, C4) - the chip says what API Rotation is doing; the keys help is three lines.

The sentences are the shared table's (``fixtures/live_words_1831.json``), run through the page's own pure helper
``chipText`` with node (skipped when the machine has none). The page's structure is pinned from its source.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation" / "desktop" / "plugin.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_chip_says_the_shared_words_while_it_acts():
    script = Path(__file__).with_name("ui_chip_text.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_chip_shows_the_sentence_and_the_tooltip_carries_it_whole():
    source = PAGE.read_text(encoding="utf-8")
    chip = source[source.index("function KameChip()"):source.index("function KameChipPopover")]
    assert "const saying = chipText(snap, now)" in chip
    assert "${saying || lines[0]}" in chip           # the full sentence in the tooltip
    assert "'min-w-0 truncate'" in chip              # ellipsis inside max-w-[22rem]
    assert "max-w-[22rem]" in chip
    assert "chipText" in source[source.index("export const panelHelpers"):]


def test_the_keys_help_is_three_lines_and_the_panel_still_takes_no_key():
    source = PAGE.read_text(encoding="utf-8")
    block = source[source.index("function KeysHowTo()"):source.index("function FallbackList")]
    assert block.count("line(") == 3
    assert "Paste all your keys, separated by commas" in block
    assert "/kame-keys add <provider> KEY1,KEY2" in block
    assert "/kame-keys" in block and "API Rotation checks them" in block
    assert "type: 'password'" not in block and "Input" not in block
