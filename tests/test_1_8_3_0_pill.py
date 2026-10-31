"""1.8.3.0 (PLAN_1.8.3.0 D2 b, decisions/0033) - the model selector does not stay on a fallback.

Two halves. The Python half: what Hermes is told when KAME hands a call over (the primary is "back in a
moment", never the keys' real rest, so the host does not hold the fallback for a minute or an hour), and
what the reply row carries so the desktop page can put the main model back. The page half is a node check
(``tests/ui_pill_restore.mjs``) run from here, skipped when the machine has no node.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_8_3_0_sibling_standin as S

transport = R.transport
settings = R.settings
chain_mod = P.chain_mod

fresh = S.fresh
notices = S.notices


def test_hermes_is_told_the_main_model_is_back_in_moments_not_when_its_keys_are(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(S.CROSS_PROVIDER, encoding="utf-8")
    chain_mod.forget()
    S.rest_all(fresh, S.PRIMARY, seconds=3 * 3600.0)
    before = time.time()
    caught = fresh._maybe_hand_over(R.FakeCall(P.pool()), S.PRIMARY, R.KEYS, 0.0, False)
    assert isinstance(caught, transport.KamePoolResting)
    reset_at = caught.body["error"]["reset_at"]
    assert before < reset_at <= time.time() + transport._HANDOVER_HOST_BACK_S + 1
    assert reset_at - before < 60, "the keys rest for hours; the host is told seconds"


def test_a_stand_in_reply_names_the_main_model_for_the_page(fresh, notices):
    S.rest_all(fresh, S.PRIMARY)
    list(fresh.stream(S.scripted(gemini_race_two=1)))
    row = fresh.replies[-1]
    assert row["for"] == "gemini-race" and row["main"] == {"provider": "gemini", "model": "gemini-race"}
    assert row["switched"] is False, "a sibling answered inside KAME: Hermes never left the main model"


def _row_for(fresh, position, answered, origin):
    """The reply row of a call Hermes made on ``position`` that ``answered`` answered, for a reply that
    STARTED on ``origin`` (1.8.3.1: the reply's origin, not "is it the main model")."""
    call = R.FakeCall(P.pool())
    call.identity = position
    call._kame_origin = origin
    call._kame_answered_by = answered
    call._kame_started = time.monotonic() - 1.0
    fresh._after_answer(call, answered, transport._Progress())
    return fresh.replies[-1]


def test_a_reply_hermes_moved_to_another_provider_is_marked_switched(fresh, tmp_path, notices):
    (tmp_path / "config.yaml").write_text(S.CROSS_PROVIDER, encoding="utf-8")
    chain_mod.forget()
    row = _row_for(fresh, "groq:llama-x", "groq:llama-x", S.PRIMARY)
    assert row["for"] == "gemini-race" and row["switched"] is True


def test_a_hand_picked_model_that_answers_itself_answered_for_nobody(fresh, tmp_path, notices):
    # 1.8.3.0 (D1): the user picked another model; the selector must stay on that pick. 1.8.3.1: the pick
    # that answers ITSELF is answered for nobody (no back_to, nothing to put back).
    (tmp_path / "config.yaml").write_text(S.CROSS_PROVIDER, encoding="utf-8")
    chain_mod.forget()
    row = _row_for(fresh, "groq:llama-x", "groq:llama-x", "groq:llama-x")
    assert row["for"] == "" and "main" not in row and "back_to" not in row and "switched" not in row


def test_the_main_model_is_named_as_the_user_wrote_it(fresh, tmp_path, notices):
    (tmp_path / "config.yaml").write_text(P.CHAIN.replace("default: gemini-race", "default: Gemini-Race"),
                                          encoding="utf-8")
    chain_mod.forget()
    S.rest_all(fresh, S.PRIMARY)
    list(fresh.stream(S.scripted(gemini_race_two=1)))
    assert fresh.replies[-1]["main"]["model"] == "Gemini-Race"


def test_a_reply_the_main_model_answered_has_no_main_field(fresh, notices):
    list(fresh.stream(S.scripted(gemini_race=1)))
    assert fresh.replies[-1]["for"] == "" and "main" not in fresh.replies[-1] and "back_to" not in fresh.replies[-1]


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_desktop_page_puts_the_main_model_back_for_this_conversation_only():
    script = Path(__file__).with_name("ui_pill_restore.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# --- 1.8.3.1 (H4): the row names the model the reply STARTED on -------------------------------------------
def test_a_stand_in_row_carries_back_to_and_keeps_main_as_its_alias(fresh, notices):
    S.rest_all(fresh, S.PRIMARY)
    list(fresh.stream(S.scripted(gemini_race_two=1)))
    row = fresh.replies[-1]
    assert row["back_to"] == {"provider": "gemini", "model": "gemini-race"}
    assert row["main"] == row["back_to"], "the alias of 1.8.3.0, kept for one more version"


def test_a_row_of_a_picked_model_that_hermes_left_goes_back_to_the_pick(fresh, tmp_path, notices):
    (tmp_path / "config.yaml").write_text(S.CROSS_PROVIDER, encoding="utf-8")
    chain_mod.forget()
    # the user picked gemini-race-two; Hermes walked it to groq: the selector should return to the PICK
    row = _row_for(fresh, "groq:llama-x", "groq:llama-x", S.SECOND)
    assert row["for"] == "gemini-race-two"
    assert row["back_to"] == {"provider": "gemini", "model": "gemini-race-two"}
    assert row["switched"] is True


def test_back_to_is_written_as_the_user_wrote_it(fresh, tmp_path, notices):
    (tmp_path / "config.yaml").write_text(P.CHAIN.replace("model: gemini-race-two", "model: Gemini-Race-Two", 1),
                                          encoding="utf-8")
    chain_mod.forget()
    row = _row_for(fresh, "gemini:gemini-race", "gemini:gemini-race", S.SECOND)
    assert row["back_to"]["model"] == "Gemini-Race-Two"
