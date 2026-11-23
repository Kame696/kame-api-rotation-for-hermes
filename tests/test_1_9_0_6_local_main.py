"""1.9.0.6 (Agent Zero issue #1, reported on 1.9.0.1) - a local main model is never a switch target.

A user ran llama.cpp as the main model and two Gemini models in the fallback list, then picked Gemini by
hand. When Gemini was busy, KAME moved the reply to the main model (the 1.8.3.1 ladder puts the main model
right after the pick), and llama.cpp reloaded its model for minutes. A local main model the user did not
put in the list has no key to rotate and no rate limit: it is never where a reply is moved or raced to.
A reply that STARTS on it still walks the list, and a local model written in the list is used like any other.
"""
from __future__ import annotations

import pytest

from tests import test_1_8_2_7_handover as H

chain_mod = H.chain_mod

G1 = "gemini:gemini-3.1-flash-lite"
G2 = "gemini:gemini-3-flash-preview"

LLAMA_CPP_MAIN = """\
model:
  provider: llama_cpp
  default: qwen3.6-35b-a3b
fallback_providers:
  - provider: gemini
    model: gemini-3.1-flash-lite
  - provider: gemini
    model: gemini-3-flash-preview
"""

CUSTOM_LOCALHOST_MAIN = """\
model:
  provider: custom
  default: qwen3.6-35b-a3b
  base_url: http://127.0.0.1:8080/v1
fallback_providers:
  - provider: gemini
    model: gemini-3.1-flash-lite
  - provider: gemini
    model: gemini-3-flash-preview
"""

CLOUD_MAIN = """\
model:
  provider: openrouter
  default: qwen3.6-35b-a3b
fallback_providers:
  - provider: gemini
    model: gemini-3.1-flash-lite
  - provider: gemini
    model: gemini-3-flash-preview
"""

LOCAL_IN_LIST = """\
model:
  provider: gemini
  default: gemini-3.1-flash-lite
fallback_providers:
  - provider: gemini
    model: gemini-3-flash-preview
  - provider: ollama
    model: llama3
"""


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    chain_mod.forget()
    yield tmp_path
    chain_mod.forget()


def write(home, text):
    (home / "config.yaml").write_text(text, encoding="utf-8")
    chain_mod.forget()


def test_a_hand_picked_list_model_never_moves_to_a_local_main_model(home):
    write(home, LLAMA_CPP_MAIN)
    main = chain_mod.primary_identity()
    assert main and main.startswith("llama_cpp:")
    assert chain_mod.ladder(G1) == [G1, G2]
    assert chain_mod.after(G1, G1) == [G2]
    assert main not in chain_mod.ladder(G2)


def test_a_main_model_on_a_local_address_is_local_too(home):
    write(home, CUSTOM_LOCALHOST_MAIN)
    assert chain_mod.ladder(G1) == [G1, G2]


def test_a_reply_that_starts_on_the_local_main_model_still_walks_the_list(home):
    write(home, LLAMA_CPP_MAIN)
    main = chain_mod.primary_identity()
    assert chain_mod.ladder(main) == [main, G1, G2]


def test_a_cloud_main_model_keeps_the_ladder_of_1831(home):
    write(home, CLOUD_MAIN)
    main = chain_mod.primary_identity()
    assert chain_mod.ladder(G2) == [G2, main, G1]


def test_a_local_model_the_user_put_in_the_list_is_used(home):
    write(home, LOCAL_IN_LIST)
    assert chain_mod.ladder(G1) == [G1, G2, "ollama:llama3"]


def test_no_origin_means_the_users_order_without_a_local_main(home):
    write(home, LLAMA_CPP_MAIN)
    assert chain_mod.ladder("") == [G1, G2]


@pytest.mark.parametrize("provider, base, local", [
    ("ollama", "", True), ("LM_Studio", "", True), ("vllm", "", True), ("llama.cpp", "", True),
    ("custom", "http://localhost:11434/v1", True), ("custom", "http://192.168.1.20:8080", True),
    ("custom", "http://172.20.0.3:8000/v1", True), ("custom", "http://gpu-box.local:8000", True),
    ("custom", "http://host.docker.internal:1234", True), ("custom", "http://[::1]:8080", True),
    ("openrouter", "", False), ("custom", "https://api.together.xyz/v1", False),
    ("custom", "http://172.32.0.1:8000", False), ("gemini", None, False),
])
def test_what_counts_as_local(provider, base, local):
    assert chain_mod.is_local(provider, base) is local


def test_a_reply_that_started_on_the_local_main_may_go_back_to_it(home):
    write(home, LLAMA_CPP_MAIN)
    main = chain_mod.primary_identity()
    assert chain_mod.after(main, "openrouter:other") == [main, G1, G2]
    assert chain_mod.after(G1, "openrouter:other") == [G1, G2]
