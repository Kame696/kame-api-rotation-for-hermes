"""The evidence-audit harness must surface gaps, not certify them away."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kame_707_case_audit", ROOT / "tools/audit_error_cases.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def sample():
    return json.loads((ROOT / "research/1.7.0.7/initial-cases.json").read_text(encoding="utf-8"))[0]


def test_report_exposes_contradiction_without_rewriting_case():
    case = sample()
    case["expected"] = {"reason": "deliberately-incompatible-test-expectation"}
    before = copy.deepcopy(case)
    row = audit.replay([case])[0]
    assert row["assessment"] == "needs-review"
    assert row["differences"]["reason"]["observed"] == "rate_limit"
    assert case == before


def test_documented_minimal_throttle_case_matches():
    row = audit.replay([sample()])[0]
    assert row["assessment"] == "matches-declared-expectations"
    assert row["observed"]["reason"] == "rate_limit"


def test_source_is_required():
    case = sample()
    case.pop("source_url")
    with pytest.raises(ValueError, match="source URL"):
        audit.replay([case])
