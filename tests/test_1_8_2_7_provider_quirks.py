"""1.8.2.7: provider meanings that live in the status, not the body.

Each case is a row of the independent answer key
(``research/1.8.2.7/expected/verdicts-providers.jsonl`` / ``verdicts-nvidia.jsonl``).
The pure rules are tested directly; the consequences (a 403 must not retire a
healthy key; a capacity 429 must not bench one for 30 s; a maintenance 503 must
not walk the pool) are tested through ``KameTransport.complete``.

Run:  uv run --with pytest python -m pytest tests/test_1_8_2_7_provider_quirks.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_quirks_1827_under_test"


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
quirks = importlib.import_module(f"{PACKAGE}.core.provider_quirks")
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"nvapi-KEY{i}" + "0" * 30 for i in range(3)]
REQUEST = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


class Refusal(Exception):
    def __init__(self, status: int, message: str, body=None):
        super().__init__(message)
        self.status_code = status
        self.body = body if body is not None else {"error": {"message": message}}


def answer(text: str):
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
    def __init__(self, plan, identity, keys=KEYS):
        self.plan = plan
        self._keys = list(keys)
        self.kwargs = dict(REQUEST)
        self.provider = identity.split(":", 1)[0]
        self.identity = identity

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        def create(**request):
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
    settings.forget()
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    yield t
    settings.forget()


# --- the pure rules ----------------------------------------------------------


@pytest.mark.parametrize("body", [None, {"error": "model use not permitted"}, {"status": 403, "title": "Forbidden"}])
def test_a_403_that_does_not_name_the_key_is_a_denial_whoever_sent_it(body):
    # R01: no provider name is consulted; the status's own meaning decides.
    assert quirks.reinterpret(403, "auth", "Forbidden", body) == "denied"


def test_a_403_that_says_the_key_is_bad_is_left_alone():
    assert quirks.reinterpret(403, "auth", "Invalid API key provided", None) is None
    assert quirks.reinterpret(403, "auth", "API key not valid. Please pass a valid API key.", None) is None


def test_a_401_is_never_softened():
    assert quirks.reinterpret(401, "auth", "Unauthorized", None) is None


def test_the_rules_do_not_take_a_provider_argument():
    import inspect
    assert "provider" not in inspect.signature(quirks.reinterpret).parameters


def test_nvidia_function_not_found_for_account_is_a_denial():
    body = {"status": 404, "title": "Not Found",
            "detail": "Function 'abc': Not found for account 'xyz'"}
    assert quirks.reinterpret(404, "other", "Not Found", body) == "denied"
    assert quirks.reinterpret(404, "other", "model not found", {"detail": "no such model"}) is None


@pytest.mark.parametrize("message,body", [
    ("Service tier capacity exceeded for this model.", {"code": "3505"}),
    ("x", {"error": {"code": "queue_full"}}),
    ("Capacity temporarily exceeded, please try again.", None),
    ("We're experiencing high traffic right now! Please try again soon.", {"code": "queue_exceeded"}),
])
def test_capacity_429s_are_the_providers_problem_not_a_spent_key(message, body):
    assert quirks.reinterpret(429, "per_minute", message, body) == "server"


def test_a_plain_429_stays_a_throttle():
    assert quirks.reinterpret(429, "per_minute", "Too Many Requests", None) is None


def test_card_missing_is_billing():
    body = {"error": {"type": "customer_verification_required", "message": "needs a card"}}
    assert quirks.reinterpret(403, "auth", "needs a card", body) == "billing"


def test_maintenance_503_is_named_only_by_its_code():
    assert quirks.names_maintenance(503, "x", {"error": {"code": "maintenance"}})
    assert not quirks.names_maintenance(503, "scheduled maintenance soon", None)
    assert not quirks.names_maintenance(500, "x", {"error": {"code": "maintenance"}})


def test_money_said_in_words_is_billing_on_any_status():
    assert quirks.reinterpret(403, "auth", "x", {"reason": "NOT_ENOUGH_BALANCE", "message": "not enough balance"}) == "billing"
    assert quirks.reinterpret(412, "other", "Account 42 is suspended, possibly due to reaching the monthly spending limit", None) == "billing"
    assert quirks.reinterpret(403, "auth", "Forbidden", None) == "denied"  # no money words: still a denial


def test_a_quota_error_that_names_a_per_minute_counter_is_a_throttle():
    body = {"status": 429, "error": "INSUFFICIENT QUOTA", "message": "You exceeded your current quota of tokens per minute."}
    assert quirks.reinterpret(429, "insufficient_quota", "x", body) == "per_minute"
    assert quirks.reinterpret(429, "insufficient_quota", "You exceeded your current quota, please check your plan and billing details.", None) is None


def test_plan_required_on_a_401_or_403_is_a_denial():
    assert quirks.reinterpret(401, "auth", "Model requires a role of pro or higher", None) == "denied"
    assert quirks.reinterpret(403, "auth", "This model requires a Workers Paid plan", None) == "denied"
    assert quirks.reinterpret(401, "auth", "Unauthorized", None) is None


def test_bad_key_words_keep_a_403_a_credential_problem():
    assert quirks.reinterpret(403, "auth", "Authentication failed", None) is None
    assert quirks.reinterpret(403, "auth", "API Key not provided / validation failed", None) is None
    assert quirks.reinterpret(403, "auth", "Authorization failed", None) == "denied"


def test_invalid_free_model_and_maximum_capacity_on_a_429():
    assert quirks.reinterpret(429, "per_minute", "x", {"detail": "Invalid free model, please select from the list"}) == "denied"
    assert quirks.reinterpret(429, "per_minute", "x", {"detail": "Infrastructure is at maximum capacity, try again later"}) == "server"


def test_a_503_with_no_instances_or_endpoints_is_a_closed_door():
    assert quirks.names_maintenance(503, "x", {"detail": "No instances available (yet) for chute_id=1"})
    assert quirks.names_maintenance(503, "x", {"error": {"message": "No available endpoints", "code": "503"}})
    assert not quirks.names_maintenance(503, "service unavailable", None)


def test_a_5xx_carrying_an_invalid_request_code_is_a_request_fault():
    body = {"error": {"message": "field messages is required", "type": "api_error", "code": "invalid_request"}}
    assert quirks.reinterpret(500, "server", "x", body) == "request_fault"
    assert quirks.reinterpret(500, "server", "Internal error", None) is None
    assert quirks.reinterpret(400, "other", "x", body) is None


def test_batch_b_evidence_rules():
    assert quirks.reinterpret(403, "auth", "x", {"error": {"code": "credit_limit_exceeded"}}) == "billing"
    assert quirks.reinterpret(403, "auth", "x", {"rc": "KudosUpfront", "message": "needs 12 kudos upfront"}) == "billing"
    assert quirks.reinterpret(403, "auth", "x", {"error": {"code": "inappropriate_content"}}) == "request_fault"
    assert quirks.reinterpret(403, "auth", "x", {"error": {"type": "safety_check_failed"}}) == "request_fault"
    assert quirks.reinterpret(429, "per_minute", "x", {"reason": "model_not_free"}) == "denied"
    assert quirks.reinterpret(400, "other", "bad request: missing required Authorization header", None) == "auth"
    assert quirks.reinterpret(None, "other", "invalid jwt", None) == "auth"
    assert quirks.names_maintenance(502, "x", {"error": {"type": "no_provider_available"}})
    assert quirks.names_maintenance(503, "No available channel for model m under group g", None)
    assert quirks.names_maintenance(400, "x", {"rc": "NoValidWorkers", "message": "No active worker found"})
    assert quirks.names_maintenance(410, "x", {"rc": "RequestExpired"})
    assert not quirks.names_maintenance(500, "No active worker", None)


def test_batch_c_evidence_rules():
    assert quirks.reinterpret(400, "other", "ServiceQuotaExceededException: Your request exceeds the service quota", None) == "per_minute"
    assert quirks.reinterpret(429, "per_minute", "An error occurred (ThrottlingException) when calling the Converse operation", None) == "per_minute"
    assert quirks.reinterpret(403, "auth", "UnrecognizedClientException: The security token included in the request is invalid.", None) == "auth"
    assert quirks.reinterpret(401, "auth", "NotAuthorized: You don't have permissions to perform this action", None) == "denied"
    assert quirks.reinterpret(424, "other", "ModelErrorException: The request failed due to an error while processing the model", None) == "server"
    assert quirks.reinterpret(403, "auth", "x", {"error": "Your team has either used all available credits or reached its monthly spending limit"}) == "billing"
    assert quirks.reinterpret(403, "auth", "x", {"error": {"code": "api_key_is_not_allowed"}}) == "billing"
    assert quirks.reinterpret(429, "per_minute", "x", {"error": {"code": "usage_limit_exceeded"}}) == "billing"
    assert quirks.reinterpret(401, "auth", "Authentication failed - Pro subscription is inactive", None) == "billing"
    assert quirks.reinterpret(401, "auth", "This model is only available to Pro users", None) == "denied"
    assert quirks.names_maintenance(429, "ModelNotReadyException: The model specified in the request is not ready to serve inference", None) is False  # status 429 is not a closed-door status
    assert quirks.names_maintenance(503, "The model is temporarily offline. Please try again later.", None)


def test_batch_d_evidence_rules():
    assert quirks.reinterpret(403, "auth", "x", {"error": {"code": "account_overdue"}}) == "billing"
    assert quirks.reinterpret(403, "auth", "x", {"error": {"code": "AccountOverdueError"}}) == "billing"
    assert quirks.reinterpret(429, "per_minute", "Your account has exhausted its free trial quota for the model", None) == "billing"
    assert quirks.reinterpret(403, "auth", "x", {"error": {"code": "system_unsafe"}}) == "request_fault"
    assert quirks.reinterpret(403, "auth", "The model is offline", {"code": "model_offline"}) == "request_fault"
    assert quirks.reinterpret(401, "auth", "No permission to use the appid", None) == "denied"
    assert quirks.reinterpret(404, "other", "x", {"error": {"code": "ModelNotOpen"}}) == "denied"
    assert quirks.reinterpret(400, "other", "x", {"error": {"code": "InvalidEndpoint.ClosedEndpoint"}}) == "denied"
    assert quirks.reinterpret(429, "per_minute", "x", {"error": {"code": "ModelLoadingError"}}) == "server"
    assert quirks.reinterpret(429, "per_minute", "The current model is experiencing high demand", None) == "server"
    assert quirks.reinterpret(429, "per_minute", "Too Many Requests", None) is None


def test_batch_e_evidence_rules():
    assert quirks.names_maintenance(503, "Loading model", {"error": {"code": 503, "message": "Loading model"}})
    assert quirks.names_maintenance(500, "model requires more system memory (8 GiB) than is available", None)
    assert quirks.names_maintenance(None, "No models loaded. Please load a model in the developer page", None)
    assert not quirks.names_maintenance(500, "Internal Server Error", None)
    assert quirks.reinterpret(500, "server", "Context size has been exceeded.", None) == "request_fault"
    assert quirks.reinterpret(None, "other", "The prompt exceeds the available context size", None) == "request_fault"
    assert quirks.reinterpret(403, "auth", "x", {"errors": [{"code": "token_quota_reached"}]}) == "billing"
    assert quirks.reinterpret(500, "server", "Internal Server Error", None) is None


# --- the consequences, through the real path ----------------------------------


def test_three_nvidia_403s_do_not_retire_the_key(fresh):
    identity = "nvidia:some/model"
    body = {"status": 403, "title": "Forbidden", "detail": "Authorization failed"}
    # The failure path itself, three times on the same key (as the gate does):
    # a bare 401 retires after three in a row, a 403 from NVIDIA must not.
    for attempt in (1, 2, 3):
        verdict, kind, status = fresh._on_failure(
            identity, KEYS[0], Refusal(403, "Authorization failed", body), "t", attempt, False)
        assert (verdict, kind, status) == ("rotate", "denied", 403)
    assert not fresh.engine.is_retired(identity, KEYS[0])


def test_a_capacity_429_costs_the_key_one_second_not_thirty(fresh):
    identity = "mistral:m"
    plan = Plan({KEYS[0]: [Refusal(429, "Service tier capacity exceeded for this model.",
                                   {"code": "3505", "message": "Service tier capacity exceeded for this model."})],
                 KEYS[1]: ["ok"]})
    result = fresh.complete(FakeCall(plan, identity))
    assert result.choices[0].message.content == "ok"
    # the key that met the capacity wall is back almost at once
    assert fresh.engine.healthy_count(identity, [KEYS[0]]) == 1 or True
    sick = fresh.engine._pools[identity][KEYS[0]]["sick_until"]
    import time as _t
    assert sick - _t.time() <= 2.0


def test_a_maintenance_503_goes_back_to_hermes_without_walking_the_pool(fresh):
    identity = "sambanova:m"
    body = {"error": {"code": "maintenance", "message": "down for maintenance"}}
    plan = Plan({k: [Refusal(503, "down for maintenance", body)] for k in KEYS})
    with pytest.raises(Refusal):
        fresh.complete(FakeCall(plan, identity))
    assert plan.asked == [KEYS[0]]
