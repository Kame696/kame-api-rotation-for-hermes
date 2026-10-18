"""Research harness controls; these do not certify the 1.8.1.8 runtime."""
import importlib.util
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location("research_test_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOLS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(TOOLS))
    return module


def test_exact_quality_rejects_wrong_types_and_extra_fields():
    m = load("latency_research_1818")
    assert m.exact_answer('{"value": true}', {"value": True})
    assert not m.exact_answer('{"value": 1}', {"value": True})
    assert not m.exact_answer('{"value": true, "extra": 3}', {"value": True})
    assert not m.exact_answer('', {"value": True})


def test_task_expectations_and_context_preservation():
    m = load("latency_research_1818")
    tasks = m.tasks()
    assert tasks[0][2]["total"] == 37 * 48 + 19 * 27 - 83
    for long in (False, True):
        prefix = m.prefix(long)
        record = prefix.split("ACTIVE_RECORD: ")[1].splitlines()[0]
        assert json.loads(record) == tasks[3][2]
    assert len(m.prefix(True)) > 80000


def test_offline_selector_controls_and_broken_shortcut():
    m = load("selection_research_1818")
    result = m.replay()
    assert result["passed"]
    assert result["rejected_global_shortcut"]["negative_control_detected"]
    assert not result["rejected_global_shortcut"]["answer_reached"]


def test_expired_penalty_does_not_exclude_a_recovered_key():
    m = load("selection_research_1818")
    s = m.Selector()
    s.note("google:M", "K1", status=503, now=10)
    assert s.choose("google:M", ["K1"], now=41) == ("K1", "ready")
    assert s.choose("google:M", [], now=41) == (None, "explicit_rest")


def test_failed_requests_do_not_form_successful_speed_pairs():
    m = load("summarize_research_1818")
    base = {"source": "batch", "replicate": 0, "key_alias": "K01", "task": "task", "context": "compact"}
    healthy = dict(base, thinking_level="medium", exact_quality=True, output_complete=True,
                   first_visible_seconds=10, total_seconds=20)
    failed = dict(base, thinking_level="low", exact_quality=False, output_complete=False,
                  first_visible_seconds=None, total_seconds=0.1, error_class="GeminiAPIError", status=503)
    assert m.paired_effect([healthy, failed], "effort", "medium", "low")["complete_valid_pairs"] == 0


def test_pairing_never_crosses_keys_tasks_or_batches():
    m = load("summarize_research_1818")
    base = {"source": "batch", "replicate": 0, "key_alias": "K01", "task": "task", "context": "compact",
            "exact_quality": True, "output_complete": True, "first_visible_seconds": 10, "total_seconds": 20}
    a = dict(base, thinking_level="medium")
    for field, value in (("source", "other"), ("key_alias", "K02"), ("task", "other")):
        b = dict(base, thinking_level="low", **{field: value})
        assert m.paired_effect([a, b], "effort", "medium", "low")["complete_valid_pairs"] == 0


def test_successful_small_pairs_still_do_not_certify_a_speedup():
    m = load("summarize_research_1818")
    base = {"source": "batch", "replicate": 0, "key_alias": "K01", "task": "task", "context": "compact",
            "exact_quality": True, "output_complete": True}
    a = dict(base, thinking_level="medium", first_visible_seconds=10, total_seconds=20)
    b = dict(base, thinking_level="low", first_visible_seconds=5, total_seconds=10)
    result = m.paired_effect([a, b], "effort", "medium", "low")
    assert result["complete_valid_pairs"] == 1
    assert result["median_relative_reduction"]["total_seconds"] == 0.5
    assert not result["measured_speedup_established"]
