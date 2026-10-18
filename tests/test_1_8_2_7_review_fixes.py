"""1.8.2.7: regression tests for the adversarial code review of 2026-10-16.

Each test names the finding it pins. They pin BEHAVIOUR THAT 1.8.2.6 HAD AND MUST KEEP
(a mistyped model id ends the turn, a dead key is retired, a stated delay is obeyed...),
which the answer-key tests alone cannot see.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_review_1827_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pkg = _load_package()
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
chain_mod = importlib.import_module(f"{PACKAGE}.core.fallback_chain")
quirks = importlib.import_module(f"{PACKAGE}.core.provider_quirks")
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"AIzaSyREV{i}" + "0" * 29 for i in range(3)]
CHAIN_CONFIG = ("model:\n  provider: gemini\n  default: gemini-3.8-flash\n"
                "fallback_providers:\n  - provider: gemini\n    model: gemini-3.7-flash\n")


class Refusal(Exception):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status_code = status
        self.body = body if body is not None else {"error": {"message": message}}


def answer(text):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


class Plan:
    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.asked = []

    def run(self, key):
        self.asked.append(key)
        outcome = self.plan[key].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return answer(outcome)


class FakeCall(transport.Call):
    def __init__(self, plan, identity="gemini:gemini-3.8-flash", keys=KEYS, channel="main"):
        self.plan, self._keys, self.identity, self.channel = plan, list(keys), identity, channel
        self.kwargs = {"model": identity, "messages": [{"role": "user", "content": "hi"}]}
        self.provider = identity.split(":", 1)[0]

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        def create(**_):
            return self.plan.run(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return False


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CHAIN_CONFIG, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()


# B1 ---------------------------------------------------------------------------
def test_B1_a_mistyped_model_id_still_ends_the_turn(fresh):
    text = "The model `llama-3.3-70b-versatil` does not exist or you do not have access to it."
    assert quirks.reinterpret(404, "other", text, {"error": {"message": text}}) is None
    verdict, kind, status = fresh._on_failure("groq:llama-3.3-70b-versatil", KEYS[0],
                                              Refusal(404, text), "t", 1, False)
    assert verdict == "raise"


# H1 ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["Authentication error", "Invalid credentials", "Not authenticated"])
def test_H1_words_that_say_the_credential_is_bad_keep_a_403_retirable(text):
    assert quirks.reinterpret(403, "auth", text, {"errors": [{"code": 10000, "message": text}]}) is None


def test_H1_every_key_refused_the_same_way_ends_the_turn_instead_of_rotating_for_ever(fresh):
    plan = Plan({k: [Refusal(403, "Forbidden")] * 3 for k in KEYS})
    with pytest.raises(Refusal):
        fresh.complete(FakeCall(plan))
    assert len(plan.asked) == len(KEYS)  # each key once, then the turn ends


# H2 ---------------------------------------------------------------------------
def test_H2_a_stated_delay_outranks_a_text_rule(fresh):
    before = time.time()
    fresh._on_failure("openai:x", KEYS[0],
                      Refusal(429, "Usage limit exceeded. Please retry after 30 seconds.",
                              {"error": {"message": "Usage limit exceeded. Please retry after 30 seconds."}}),
                      "t", 1, False)
    sick = fresh.engine._pools["openai:x"][KEYS[0]]["sick_until"]
    assert sick - before < 120.0, sick - before  # not the hour a "billing" reading would have cost


# H3 ---------------------------------------------------------------------------
def test_H3_mixed_case_model_ids_still_find_their_pool(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: sambanova\n  default: Meta-Llama-3.3-70B-Instruct\n"
        "fallback_providers:\n  - provider: sambanova\n    model: Qwen/Qwen3-235B-A22B\n", encoding="utf-8")
    chain_mod.forget()
    assert chain_mod.later_entries("sambanova:meta-llama-3.3-70b-instruct") == ["sambanova:qwen/qwen3-235b-a22b"]
    for key in KEYS:  # the later entry's pool exists under KAME's lower-case name
        fresh.engine.mark("sambanova:qwen/qwen3-235b-a22b", key, False, 3600.0, "daily")
    assert fresh._entry_usable("sambanova:qwen/qwen3-235b-a22b") is False


# H4 ---------------------------------------------------------------------------
def test_H4_background_jobs_are_never_handed_over(fresh, monkeypatch):
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "0")
    settings.forget()
    for key in KEYS:
        fresh.engine.mark("gemini:gemini-3.8-flash", key, False, 3600.0, "daily")
    main = FakeCall(Plan({}), channel="main")
    side = FakeCall(Plan({}), channel="secondary")
    assert isinstance(fresh._maybe_hand_over(main, main.identity, KEYS, 0.0, False), transport.KamePoolResting)
    fresh._handover_until.clear()
    assert fresh._maybe_hand_over(side, side.identity, KEYS, 0.0, False) is None


# H5 ---------------------------------------------------------------------------
def test_H5_with_a_chain_and_never_switch_ON_a_closed_door_503_keeps_rotating(fresh, monkeypatch):
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")  # off by default since decisions/0027
    settings.forget()
    body = {"error": {"code": "maintenance", "message": "down"}}
    verdict, _, _ = fresh._on_failure("gemini:gemini-3.8-flash", KEYS[0], Refusal(503, "down", body), "t", 1, False)
    assert verdict == "rotate"  # the user asked to ignore the list and a chain is set


def test_H5_without_a_chain_the_closed_door_503_goes_back_to_hermes(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-3.8-flash\n", encoding="utf-8")
    chain_mod.forget()
    body = {"error": {"code": "maintenance", "message": "down"}}
    verdict, _, _ = fresh._on_failure("gemini:gemini-3.8-flash", KEYS[0], Refusal(503, "down", body), "t", 1, False)
    assert verdict == "raise"


def test_H5_two_timeouts_do_not_reach_hermes_while_it_could_switch_model(fresh, monkeypatch):
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")  # off by default since decisions/0027
    settings.forget()
    plan = Plan({KEYS[0]: [Refusal(504, "Error")], KEYS[1]: [Refusal(504, "Error")], KEYS[2]: ["ok"]})
    result = fresh.complete(FakeCall(plan))
    assert result.choices[0].message.content == "ok"
    assert plan.asked == KEYS  # the 1.8.2.6 behaviour: keep rotating, nothing handed back


# M1 / M2 ----------------------------------------------------------------------
def test_M1_a_text_rule_never_demotes_a_revoked_key():
    assert quirks.reinterpret(401, "revoked", "Invalid API key: the key does not exist or you do not have access", None) is None


def test_M2_a_daily_throttle_is_not_made_per_minute():
    text = "ThrottlingException: Too many tokens per day, please wait before trying again."
    assert quirks.reinterpret(429, "daily", text, None) is None
    assert quirks.reinterpret(429, "per_minute", text, None) is None
    assert quirks.reinterpret(429, "per_minute", "ThrottlingException: Too many requests, please wait", None) == "per_minute"


# M4 ---------------------------------------------------------------------------
def test_M4_the_hook_recognises_a_wrapped_handover_by_its_code():
    class Wrapped(Exception):
        body = {"error": {"code": "kame_pool_resting", "reset_at": 1_900_000_000.0}}
    result = pkg._on_api_error_classification(provider="gemini", model="m", status_code=429,
                                              error_message="x", error=Wrapped("x"), error_type="Wrapped")
    assert result["reason"] == "upstream_rate_limit" and result["error_context"]["reset_at"] == 1_900_000_000.0
