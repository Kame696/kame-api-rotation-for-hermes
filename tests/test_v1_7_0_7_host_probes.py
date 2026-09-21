"""Probe migration requires positive evidence AND a working negative control."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kame_707_host_assumptions", ROOT / "tools/host_assumptions.py")
probes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probes)


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(probes, "_gemini_contract_report", None)


def response(passed=True, mutation=True):
    names = ["assistant_mapping", "refusal_fields", "guidance_separation", "factory_paths"]
    return {"checks": {name: {"passed": passed} for name in names},
            "mutation_detected": {name: mutation for name in names}}


def test_all_four_probes_share_one_subprocess(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(response()))
    monkeypatch.setattr("subprocess.run", run)
    for probe in [probes.gemini_still_reads_an_assistant_turn_as_a_model_turn,
                  probes.a_refusal_still_arrives_with_the_provider_words_in_it,
                  probes.the_host_still_appends_its_own_guidance_to_a_429,
                  probes.the_error_factory_is_still_one_function]:
        assert probe() == (True, True)
    assert len(calls) == 1
    assert "--host" in calls[0]


@pytest.mark.parametrize("passed,mutation", [(True, False), (False, True), (False, False)])
def test_no_green_without_both_controls(monkeypatch, passed, mutation):
    monkeypatch.setattr(probes, "_gemini_contract_report", response(passed, mutation))
    got, expected = probes.the_error_factory_is_still_one_function()
    assert got != expected


@pytest.mark.parametrize("stdout,code", [("not-json", 1), ("{}", 0), ("[]", 0),
                                        (json.dumps(response()), 9)])
def test_failed_or_empty_runner_is_not_a_pass(monkeypatch, stdout, code):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=code, stdout=stdout))
    got, expected = probes.the_error_factory_is_still_one_function()
    assert got != expected
