"""Do not accept an infrastructure crash as a detected negative mutation."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


probes = load_file("runtime707probes", ROOT/"tools/host_assumptions.py")
runner = load_file("runtime707runner", ROOT/"tools/host_runtime_contracts.py")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(probes, "_runtime_contract_report", None)


def test_all_runtime_checks_use_one_cached_runner(monkeypatch):
    result = {"checks":{name:{"passed":True} for name in runner.CONTRACTS},
              "mutation_detected":{name:True for name in runner.CONTRACTS}}
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return NS(returncode=0, stdout=json.dumps(result))
    monkeypatch.setattr("subprocess.run", run)
    for name in runner.CONTRACTS:
        assert probes._runtime_executed_contract(name) == (True, True)
    assert len(calls) == 1


@pytest.mark.parametrize("passed,negative", [(True, False),(False,True),(False,False)])
def test_both_conditions_required(monkeypatch, passed, negative):
    monkeypatch.setattr(probes, "_runtime_contract_report", {
        "checks":{"empty_retry":{"passed":passed}}, "mutation_detected":{"empty_retry":negative}})
    a,b = probes._runtime_executed_contract("empty_retry")
    assert a != b


@pytest.mark.parametrize("stdout,rc", [("{}",0),("[]",0),("not-json",1),("{}",9)])
def test_invalid_runner_fails_closed(monkeypatch, stdout, rc):
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs:NS(returncode=rc,stdout=stdout))
    a,b = probes._runtime_executed_contract("empty_retry")
    assert a != b


def test_import_failure_is_not_contract_violation():
    def bad(_):
        raise ModuleNotFoundError("test dependency")
    assert runner.evaluate(bad, True)["kind"] == "ModuleNotFoundError"


def test_named_assertion_is_contract_violation():
    def bad(_):
        runner.require(False, "broken test contract")
    assert runner.evaluate(bad, True)["kind"] == "ContractViolation"
