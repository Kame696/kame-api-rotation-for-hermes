"""1.8.2.9 / decisions/0032 A7 + A9 - how much of each model's requests went to background jobs.

The tally is an integer add where a request starts; the view and the suggestion are read on the
publisher thread. The suggestion only ever names a model of the user's own list and a Hermes config
line; KAME never changes the config.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_quota_use_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
chain_mod = importlib.import_module(f"{PACKAGE}.core.fallback_chain")
aux_budget = importlib.import_module(f"{PACKAGE}.core.aux_budget")
settings = importlib.import_module(f"{PACKAGE}.settings")
state = importlib.import_module(f"{PACKAGE}.state")

PRIMARY = "gemini:gemini-3.8-flash"
CONFIG = ("model:\n  provider: gemini\n  default: gemini-3.8-flash\nfallback_providers:\n"
          "  - provider: gemini\n    model: gemini-3.7-flash\n  - provider: gemini\n    model: gemini-3.6-flash\n"
          "  - provider: nvidia\n    model: z-ai/glm-5.3\n")


@pytest.fixture()
def t(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    yield transport.KameTransport(engine=carousel_mod.Carousel())
    settings.forget()
    chain_mod.forget()


def call(channel="main", task="chat"):
    return SimpleNamespace(channel=channel, task=task)


def test_main_and_side_requests_land_in_their_own_columns(t):
    for _ in range(3):
        t._count_request(call(), PRIMARY)
    for _ in range(2):
        t._count_request(call("secondary", "title_generation"), PRIMARY)
    t._count_request(call("secondary", "compression"), PRIMARY)
    view = t.quota_use_view()
    row = view["models"][PRIMARY]
    assert (row["main"], row["side"]) == (3, 3)
    assert row["tasks"] == {"title_generation": 2, "compression": 1}


def test_a_call_without_a_channel_is_the_chat(t):
    t._count_request(SimpleNamespace(), PRIMARY)
    assert t.quota_use_view()["models"][PRIMARY]["main"] == 1


def test_buckets_older_than_a_day_drop(t, monkeypatch):
    real = time.time
    t._count_request(call("secondary", "title_generation"), PRIMARY)
    monkeypatch.setattr(time, "time", lambda: real() + 26 * 3600)
    t._count_request(call(), PRIMARY)
    row = t.quota_use_view()["models"][PRIMARY]
    assert (row["main"], row["side"]) == (1, 0), "the title from 26 hours ago is gone"


def test_two_views_with_no_traffic_between_are_identical(t):
    t._count_request(call(), PRIMARY)
    first = json.dumps(t.quota_use_view(), sort_keys=True)
    time.sleep(0.02)
    assert json.dumps(t.quota_use_view(), sort_keys=True) == first, "nothing moved, so the file does not either"


def test_the_view_stays_small_for_four_models(t):
    for i in range(4):
        for task in ("title_generation", "compression", "approval", "vision", "web_extract"):
            t._count_request(call("secondary", task), f"gemini:gemini-3.{5 + i}-flash:global")
            t._count_request(call(), f"gemini:gemini-3.{5 + i}-flash:global")
    assert len(json.dumps(t.quota_use_view())) < 2048


def test_stopped_jobs_are_counted_by_task(t):
    t._aux_stopped["title_generation"] = 4
    assert t.quota_use_view()["stopped"] == {"title_generation": 4}


def test_a_real_call_counts_each_request_it_sends(t, monkeypatch, tmp_path):
    keys = [f"AIzaSyQU{i}" + "0" * 30 for i in range(3)]

    class Boom(Exception):
        status_code = 503

    outcomes = [Boom("The model is overloaded. Please try again later."), "ok"]

    def answer(text):
        message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
        return SimpleNamespace(id="c", model="m", usage=None,
                               choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])

    class FakeCall(transport.Call):
        identity, provider = PRIMARY, "gemini"
        channel, task, task_known = "secondary", "compression", True

        def __init__(self):
            self.kwargs = {"model": "gemini-3.8-flash", "messages": []}

        def keys(self):
            return list(keys)

        def entry(self, key):
            return None

        def entry_id(self, key):
            return ""

        def client_for(self, key, attempt):
            def create(**_):
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return answer(outcome)
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        def cancelled(self):
            return False

    monkeypatch.setenv("KAME_RECORDER_DISABLED", "1")
    t.complete(FakeCall())
    row = t.quota_use_view()["models"][PRIMARY]
    assert (row["main"], row["side"]) == (0, 2)
    assert row["tasks"] == {"compression": 2}


# --- the suggestion (A9) ---------------------------------------------------------------------------
CHAIN = ["gemini:gemini-3.7-flash", "gemini:gemini-3.6-flash", "nvidia:z-ai/glm-5.3"]
WRITTEN = {"gemini:gemini-3.8-flash": "gemini-3.8-flash", "gemini:gemini-3.7-flash": "gemini-3.7-flash",
           "gemini:gemini-3.6-flash": "gemini-3.6-flash", "nvidia:z-ai/glm-5.3": "z-ai/GLM-5.3"}


def suggest(models, primary=PRIMARY, chain=CHAIN, since=1_700_000_000.0):
    return aux_budget.suggest(models, primary, chain, lambda e: WRITTEN.get(e, ""), since)


def models(main, side, **tasks):
    return {PRIMARY + ":global": {"main": main, "side": side, "tasks": dict(tasks)}}


def test_no_suggestion_under_fifteen_percent():
    assert suggest(models(90, 10, title_generation=10)) is None


def test_no_suggestion_under_twenty_side_requests():
    assert suggest(models(10, 15, title_generation=15)) is None


def test_a_cosmetic_task_goes_to_the_last_model_of_the_same_provider():
    text = suggest(models(100, 40, title_generation=30, compression=10))
    assert "gemini-3.6-flash" in text and "number 3 of your list" in text
    assert "title_generation" in text and "auxiliary.title_generation.provider: gemini" in text
    assert "auxiliary.title_generation.model: gemini-3.6-flash" in text
    assert "enabled: false" in text, "a title can also simply be turned off"
    assert "KAME never changes this" in text


def test_an_important_task_goes_to_the_first_fallback_of_the_same_provider():
    text = suggest(models(100, 60, compression=50, title_generation=10))
    assert "compression" in text and "gemini-3.7-flash" in text and "number 2 of your list" in text
    assert "enabled: false" not in text


def test_a_list_of_one_model_has_no_suggestion():
    assert suggest(models(10, 60, title_generation=60), chain=[]) is None


def test_a_list_with_no_model_of_the_same_provider_has_no_suggestion():
    assert suggest(models(10, 60, title_generation=60), chain=["nvidia:z-ai/glm-5.3"]) is None


def test_other_models_background_use_is_not_the_primarys():
    other = {"gemini:gemini-3.6-flash:global": {"main": 0, "side": 90, "tasks": {"title_generation": 90}}}
    assert suggest(other) is None


def test_the_named_model_is_always_a_member_of_the_users_list():
    rng = random.Random(7)
    providers = ["gemini", "nvidia", "openai"]
    for _ in range(300):
        chain = [f"{rng.choice(providers)}:model-{rng.randint(0, 9)}" for _ in range(rng.randint(0, 5))]
        written = {e: e.split(":", 1)[1] for e in chain}
        task = rng.choice(["title_generation", "compression", "approval", "x"])
        text = aux_budget.suggest({PRIMARY + ":global": {"main": 10, "side": 50, "tasks": {task: 50}}},
                                  PRIMARY, chain, lambda e, w=written: w.get(e, ""), 1_700_000_000.0)
        if text is None:
            continue
        named = text.split("model: ", 1)[1].split()[0].rstrip(".`")
        assert any(w == named for e, w in written.items() if e.startswith("gemini:")), (chain, text)


def test_the_snapshot_carries_the_readout(t):
    t._count_request(call("secondary", "title_generation"), PRIMARY)
    doc = state.snapshot(t)
    assert doc["quota_use"]["models"][PRIMARY]["side"] == 1
    assert set(doc["quota_use"]) >= {"since", "models", "stopped", "suggestion"}
