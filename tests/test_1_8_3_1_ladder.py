"""1.8.3.1 (PLAN_1.8.3.1 D1, H1) - the fallback list serves the model in use, whichever it is.

``ladder(X) = [X] + ([main] + list, in the user's order, X removed)``; ``after(origin, position)`` is
what comes after the position in that ladder. Pure reading of ``config.yaml``: nothing here moves a
reply.
"""
from __future__ import annotations

import pytest

from tests import test_1_8_2_7_handover as H

chain_mod = H.chain_mod

MAIN = "gemini:gemini-3.8-flash"
M1 = "gemini:gemini-3.7-flash"
M2 = "gemini:gemini-3.6-flash"
M3 = "gemini:gemini-3.5-flash"
M4 = "gemini:gemini-3-flash-preview"

FOUR = """\
model:
  provider: gemini
  default: gemini-3.8-flash
fallback_providers:
  - provider: gemini
    model: gemini-3.7-flash
  - provider: gemini
    model: gemini-3.6-flash
  - provider: gemini
    model: gemini-3.5-flash
  - provider: gemini
    model: gemini-3-flash-preview
"""


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(FOUR, encoding="utf-8")
    chain_mod.forget()
    yield tmp_path
    chain_mod.forget()


def write(home, text):
    (home / "config.yaml").write_text(text, encoding="utf-8")
    chain_mod.forget()


def test_the_main_model_keeps_the_order_of_1830(home):
    assert chain_mod.ladder(MAIN) == [MAIN, M1, M2, M3, M4]


def test_a_hand_picked_list_model_comes_first_then_main_then_the_list_in_order(home):
    # the owner's words: list 1,2,3,4 with 3 picked -> 3, then 1, 2, 4 (main is asked after the pick)
    assert chain_mod.ladder(M2) == [M2, MAIN, M1, M3, M4]


def test_a_model_outside_the_list_is_followed_by_the_whole_order(home):
    outside = "gemini:gemini-tile-model"
    assert chain_mod.ladder(outside) == [outside, MAIN, M1, M2, M3, M4]


def test_a_suffixed_identity_is_the_same_model(home):
    assert chain_mod.ladder(M2 + ":global") == [M2, MAIN, M1, M3, M4]
    assert chain_mod.ladder(MAIN + ":global")[0] == MAIN


def test_identities_are_lower_case(home):
    assert chain_mod.ladder("Gemini:Gemini-3.6-Flash") == [M2, MAIN, M1, M3, M4]


def test_with_no_list_nothing_follows(home):
    write(home, "model:\n  provider: gemini\n  default: gemini-3.8-flash\n")
    assert chain_mod.ladder(MAIN) == [MAIN]
    assert chain_mod.ladder("gemini:other") == ["gemini:other"], "no list known = no switch"


def test_an_unreadable_config_is_no_switch(home):
    write(home, "model: [unclosed\n  - : :")
    assert chain_mod.ladder(M1) == [M1]


def test_no_config_at_all_is_no_switch(home):
    (home / "config.yaml").unlink()
    chain_mod.forget()
    assert chain_mod.ladder(M1) == [M1]


def test_no_main_model_known_the_list_alone_follows(home):
    write(home, "fallback_providers:\n  - provider: groq\n    model: llama-x\n  - provider: groq\n    model: llama-y\n")
    assert chain_mod.ladder("groq:llama-y") == ["groq:llama-y", "groq:llama-x"]


def test_an_empty_origin_is_still_the_order(home):
    # an empty origin is not a model: the ladder is the user's own order (main first), never [""]
    assert chain_mod.ladder("") == [MAIN, M1, M2, M3, M4]


def test_after_is_what_follows_the_position(home):
    assert chain_mod.after(M2, M2) == [MAIN, M1, M3, M4]
    assert chain_mod.after(M2, MAIN) == [M1, M3, M4]
    assert chain_mod.after(M2, M3) == [M4]
    assert chain_mod.after(M2, M4) == []
    assert chain_mod.after(MAIN, MAIN) == [M1, M2, M3, M4]


def test_after_reads_a_suffixed_position(home):
    assert chain_mod.after(M2, MAIN + ":global") == [M1, M3, M4]


def test_a_position_the_ladder_does_not_hold_starts_its_own_ladder(home):
    assert chain_mod.after(M2, "gemini:elsewhere") == [MAIN, M1, M2, M3, M4]


def test_duplicates_in_the_list_are_dropped(home):
    write(home, FOUR + "  - provider: gemini\n    model: gemini-3.7-flash\n")
    assert chain_mod.ladder(M1) == [M1, MAIN, M2, M3, M4]


def test_the_main_model_listed_twice_is_asked_once(home):
    write(home, FOUR.replace("fallback_providers:\n",
                             "fallback_providers:\n  - provider: gemini\n    model: gemini-3.8-flash\n"))
    assert chain_mod.ladder(M3) == [M3, MAIN, M1, M2, M4]


def test_1830_rule_is_gone():
    assert not hasattr(chain_mod, "starts_on_main")


# --- review round 1 (R13): a suffix is matched only when KAME itself adds it ------------------------------
def test_r13_a_models_own_colon_tag_is_not_a_pool_suffix(home):
    # `ollama:llama3:8b` is another model than the listed `ollama:llama3`: the tag after the colon is the
    # model's own, not a region KAME appends to a pool identity (Carousel.identity(provider, model, region)).
    write(home, "model:\n  provider: ollama\n  default: llama3\n"
                "fallback_providers:\n  - provider: ollama\n    model: llama3:70b\n")
    assert not chain_mod._same("ollama:llama3:8b", "ollama:llama3")
    assert chain_mod.ladder("ollama:llama3:8b") == ["ollama:llama3:8b", "ollama:llama3", "ollama:llama3:70b"]
    assert chain_mod.after("ollama:llama3", "ollama:llama3:8b") == ["ollama:llama3", "ollama:llama3:70b"]


def test_r13_the_region_suffixes_kame_adds_still_match(home):
    for suffix in ("global", "us-central1", "europe-west4", "az-myresource", "xr-eu", "us", "eu"):
        assert chain_mod._same(f"{M1}:{suffix}", M1), suffix
    assert chain_mod._same(M1, M1)
    assert not chain_mod._same(M1 + ":8b", M1)
    assert not chain_mod._same(M1 + ":latest", M1)
