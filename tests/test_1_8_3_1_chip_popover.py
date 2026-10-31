"""1.8.3.1 (PLAN_1.8.3.1 D3) - the chip's popover and the "Last replies" sentences.

Two halves. The page (JavaScript) is checked with node over the shared table and snapshots
(``tests/ui_chip_popover.mjs``, skipped without node). The backend half is checked here: what a
reply row says about how the reply went (a retry, a backup key, a wait, a cut answer, a stand-in and
why, a failure) is what the page turns into its sentence.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests import test_1_8_2_5_stream_race as R

R_fresh = R.fresh  # the fixture: a fresh transport

transport = R.transport


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_popover_and_its_sentences_are_the_shared_words():
    script = Path(__file__).with_name("ui_chip_popover.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_page_ships_the_popover_without_a_second_copy_of_the_words():
    source = (Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation" / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "function chipPopoverModel" in source
    assert "function KameChipPopover" in source
    assert "Open API Rotation" in source
    # the chip opens the popover; it no longer navigates on a click
    chip_start = source.index("data-kame-chip")
    assert "$chipOpen.set(!$chipOpen.get())" in source[chip_start - 2000: chip_start + 3000]


# --- what a reply row carries ------------------------------------------------------------------------------
def _rows(binding):
    return list(binding.replies)


def test_a_plain_reply_row_has_no_extra_facts(R_fresh):
    pool = R.Pool({R.KEYS[0]: [[0.01, R.chunk("hello", finish="stop")]]})
    R.run(R_fresh, R.FakeCall(pool))
    row = _rows(R_fresh)[-1]
    for extra in ("attempts", "cut", "waited_s", "backup", "failed", "why"):
        assert extra not in row, row


def test_a_reply_on_the_second_key_says_so(R_fresh):
    pool = R.Pool({R.KEYS[0]: [R.Refusal(429, "rate limited")], R.KEYS[1]: [[0.01, R.chunk("ok", finish="stop")]]})
    R.run(R_fresh, R.FakeCall(pool))
    row = _rows(R_fresh)[-1]
    assert row.get("attempts", 1) >= 2, row


def test_a_failed_reply_has_a_row_with_a_fixed_sentence_reason(R_fresh):
    class Boom(Exception):
        status_code = 503

    call = R.FakeCall(R.Pool({}))
    R_fresh._note_failed(call, Boom("secret text from the provider that must not be shown"))
    row = _rows(R_fresh)[-1]
    assert row["failed"] is True
    assert row["reason"] == "the provider answered 503"
    assert "secret" not in str(row)


def test_a_timeout_and_an_unknown_failure_get_their_own_fixed_sentences(R_fresh):
    call = R.FakeCall(R.Pool({}))
    R_fresh._note_failed(call, TimeoutError("took too long"))
    assert _rows(R_fresh)[-1]["reason"] == "it timed out"
    R_fresh._note_failed(call, ValueError("anything"))
    assert _rows(R_fresh)[-1]["reason"] == "the request failed"


def test_an_interrupt_and_a_hand_over_are_not_failed_replies(R_fresh):
    call = R.FakeCall(R.Pool({}))
    before = len(_rows(R_fresh))

    class InterruptedError_(Exception):
        pass

    InterruptedError_.__name__ = "Interrupted"
    R_fresh._note_failed(call, InterruptedError_("stop"))
    assert len(_rows(R_fresh)) == before


def test_the_short_why_is_one_of_four_words():
    assert transport._short_why("the model is busy") == "busy"
    assert transport._short_why("slow today") == "slow"
    assert transport._short_why("every key is resting") == "resting"
    assert transport._short_why("something else") == ""
    assert transport._short_why("") == ""
