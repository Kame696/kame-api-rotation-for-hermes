"""Proofs for tools/expected_gate.py, on synthetic tiny timelines only.

Mirrors tests/test_replay_timeline.py's own conventions (see that file's
docstring): the real plugin package is loaded for the end-to-end tests
because a fake one would prove nothing about the real decision path, but no
real evidence file is ever read — every calls.jsonl/refusals.jsonl row here
is hand-built, at timestamps chosen far from both MIN_AT's boundary and the
contaminated-window range, and HERMES_HOME isolation is inherited from
tests/conftest.py (set at import time, before any plugin module loads).

Pinned:

1. Matcher precedence — first match in file order wins, and every matching
   shape_id is still reported (multi-match is a finding, never hidden).
2. Zero-match records are reported, not silently dropped.
3. The rest-DSL comparison, including the +-5%/+-2s tolerance floor, for
   every DSL kind the task specifies (fixed, flat_base, obey_stated with and
   without a recoverable stated number, obey_stated+ignore_stated,
   probe_ladder, none, unclear).
4. The one documented kind/verdict -> family/window/scope/action mapping
   table, including the "other"+raise vs "other"+rotate split and the
   defer_host_retry override kinds.
5. End to end against the REAL plugin package: a real shape (a Gemini 503
   "experiencing high demand") graded with its correct expectation shows
   full agreement, and the SAME real record graded against a deliberately
   wrong expectation shows a real disagreement — never a false green.

Run: python -B -m pytest -q -p no:cacheprovider tests/test_expected_gate.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
TOOL_PATH = ROOT / "tools" / "expected_gate.py"

# expected_gate.py imports `replay_timeline` via a relative `sys.path` trick
# (see its own header) that assumes it lives next to that module — load it
# the same way here so the two never fall out of step with each other.
sys.path.insert(0, str(ROOT / "tools"))


def _load_tool():
    spec = importlib.util.spec_from_file_location("expected_gate_under_test", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_tool()

# Safely outside CONTAMINATED_WINDOW (1788719870..1788720100) and above
# MIN_AT (1.75e9) — the exact constant test_replay_timeline.py's own
# terminal-failure test uses.
BASE_AT = 1_800_000_000.0


# ---------------------------------------------------------------------------
# Matcher evaluation: precedence, zero-match, multi-match
# ---------------------------------------------------------------------------


def _matcher(shape_id, match, expected=None, rest=None, origin="real"):
    return {
        "shape_id": shape_id,
        "match": match,
        "expected": expected or {"family": "f", "window": "w", "scope": "s", "action": "a"},
        "rest": rest or {"kind": "none"},
        "origin": origin,
    }


class TestMatcherEvaluation:
    def test_status_and_provider_and_message_regex_all_must_hold(self):
        ev = gate.build_evidence(
            {"identity": "gemini:gemini-3.8-flash", "status": 429},
            {"message": "Resource has been exhausted (e.g. check quota).", "body": None},
        )
        m = _matcher("shape-a", {"status": 429, "provider_in": ["gemini"], "message_regex": r"exhausted"})
        assert gate.matcher_matches(m, ev) is True

        wrong_status = _matcher("shape-b", {"status": 500, "provider_in": ["gemini"]})
        assert gate.matcher_matches(wrong_status, ev) is False

        wrong_provider = _matcher("shape-c", {"status": 429, "provider_in": ["nvidia"]})
        assert gate.matcher_matches(wrong_provider, ev) is False

        wrong_regex = _matcher("shape-d", {"status": 429, "message_regex": r"quota exceeded for metric"})
        assert gate.matcher_matches(wrong_regex, ev) is False

    def test_body_contains_and_body_absent(self):
        ev = gate.build_evidence(
            {"identity": "gemini:x", "status": 429},
            {"message": "quota", "body": {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}},
        )
        has_perday = _matcher("s1", {"body_contains": ["quotaid", "perday"]})
        assert gate.matcher_matches(has_perday, ev) is True

        excludes_inputtoken = _matcher("s2", {"body_contains": ["quotaid"], "body_absent": ["inputtoken"]})
        assert gate.matcher_matches(excludes_inputtoken, ev) is True

        requires_inputtoken = _matcher("s3", {"body_contains": ["quotaid", "inputtoken"]})
        assert gate.matcher_matches(requires_inputtoken, ev) is False

    def test_quota_family_and_retry_hint_detection(self):
        assert gate.detect_quota_family("quotaid generaterequestsperdayperprojectpermodel") == "PerDay"
        assert gate.detect_quota_family("quotaid ...perminute...") == "PerMinute"
        assert gate.detect_quota_family("resource has been exhausted") == "none"
        assert gate.detect_retry_hint('"retrydelay": "29s"') is True
        assert gate.detect_retry_hint("please retry in 12s") is True
        assert gate.detect_retry_hint("resource has been exhausted") is False

    def test_first_match_in_file_order_wins_but_all_matches_are_reported(self):
        # Two matchers that both fire on the same TPM-shaped record: a
        # specific one requiring "inputtoken" and a generic PerMinute one
        # that does not exclude it. Ordering the specific one first is what
        # this project's own matchers.json does for real-04 vs real-21.
        matchers = [
            _matcher("tpm-specific", {"status": 429, "body_contains": ["quotaid", "inputtoken"]}),
            _matcher("generic-permin", {"status": 429, "body_contains": ["quotaid", "permin"]}),
        ]
        record = {
            "at": BASE_AT, "identity": "gemini:g", "key": "key:aaa",
            "outcome": "rotate", "status": 429,
            "_refusal": {"message": "quota", "body": {"quotaId": "...InputTokensPerModelPerMinute-FreeTier"}},
        }
        assigned = gate.assign_shapes([record], matchers)
        assert assigned[0]["matched"] == ["tpm-specific", "generic-permin"]
        assert assigned[0]["assigned"] == "tpm-specific"

    def test_zero_match_record_is_reported_not_dropped(self):
        matchers = [_matcher("only-shape", {"status": 500})]
        record = {
            "at": BASE_AT, "identity": "x:y", "key": "key:aaa",
            "outcome": "raise", "status": 404, "_refusal": None,
        }
        assigned = gate.assign_shapes([record], matchers)
        assert assigned[0]["matched"] == []
        assert assigned[0]["assigned"] is None


# ---------------------------------------------------------------------------
# rest-DSL comparison, including the tolerance floor
# ---------------------------------------------------------------------------


class TestRestDSL:
    def test_fixed_within_and_outside_tolerance(self):
        dsl = {"kind": "fixed", "seconds": 20, "cap": 3600}
        assert gate.rest_agrees(dsl, 20.0, "") is True
        assert gate.rest_agrees(dsl, 21.5, "") is True  # within max(2, 5%) = 2s
        assert gate.rest_agrees(dsl, 25.0, "") is False
        assert gate.rest_agrees(dsl, None, "") is False

    def test_flat_base_tight_tolerance_floor(self):
        dsl = {"kind": "flat_base", "seconds": 1}
        assert gate.rest_agrees(dsl, 1.0, "") is True
        assert gate.rest_agrees(dsl, 2.9, "") is True  # tolerance floors at 2s even for a 1s target
        assert gate.rest_agrees(dsl, 5.0, "") is False

    def test_obey_stated_reads_the_stated_number_from_evidence(self):
        dsl = {"kind": "obey_stated", "cap": 3600, "ignore_stated": False}
        haystack = '"retrydelay": "29s"'
        assert gate.rest_agrees(dsl, 29.0, haystack) is True
        assert gate.rest_agrees(dsl, 100.0, haystack) is False

    def test_obey_stated_caps_at_the_ceiling(self):
        dsl = {"kind": "obey_stated", "cap": 3600, "ignore_stated": False}
        haystack = '"resets_in_seconds": 12660'
        # capped at 3600, not the full 12660
        assert gate.rest_agrees(dsl, 3600.0, haystack) is True
        assert gate.rest_agrees(dsl, 12660.0, haystack) is False

    def test_obey_stated_with_no_recoverable_number_is_not_applicable(self):
        dsl = {"kind": "obey_stated", "cap": 3600, "ignore_stated": False}
        assert gate.rest_agrees(dsl, 45.0, "no number anywhere in here") is None

    def test_obey_stated_ignore_stated_wants_a_different_number(self):
        dsl = {"kind": "obey_stated", "cap": 3600, "ignore_stated": True}
        haystack = '"retrydelay": "29s"'
        assert gate.rest_agrees(dsl, 29.0, haystack) is False  # obeyed exactly what must be ignored
        assert gate.rest_agrees(dsl, 300.0, haystack) is True  # produced something else

    def test_probe_ladder_matches_either_step(self):
        dsl = {"kind": "probe_ladder", "steps": [300, 3600], "condition": "pool_alive_then_silence"}
        assert gate.rest_agrees(dsl, 300.0, "") is True
        assert gate.rest_agrees(dsl, 3600.0, "") is True
        assert gate.rest_agrees(dsl, 900.0, "") is False
        assert gate.rest_agrees(dsl, None, "") is False

    def test_none_kind_wants_no_bench_at_all(self):
        # Rule changed (Change 2, task "Same for rest..."): ``hold_s is None``
        # used to count as an "agreement" (old: True). It now counts as
        # ``not_applicable`` (new: None) — the plugin never called
        # ``engine.mark`` at all (a request_fault/timeout path took the
        # raise/defer branch before any bench could be sized), which is a
        # structural fact the ``action`` field already scores, not a
        # measurement ``rest`` has anything left to check. A REAL number that
        # happens to land near zero, or one that clearly should not exist,
        # are unchanged: both are still scored, because those cases DID
        # produce a checkable number.
        dsl = {"kind": "none"}
        assert gate.rest_agrees(dsl, None, "") is None
        assert gate.rest_agrees(dsl, 0.5, "") is True
        assert gate.rest_agrees(dsl, 20.0, "") is False

    def test_unclear_is_never_checked(self):
        dsl = {"kind": "unclear", "why": "conditional on pool composition"}
        assert gate.rest_agrees(dsl, 3600.0, "") is None
        assert gate.rest_agrees(dsl, None, "") is None


# ---------------------------------------------------------------------------
# The one documented vocabulary-mapping table
# ---------------------------------------------------------------------------


class TestVocabularyMapping:
    def test_family_mapping_for_ordinary_kinds(self):
        assert gate.map_family("server", "rotate") == "server"
        assert gate.map_family("timeout", "rotate") == "timeout"
        assert gate.map_family("auth", "rotate") == "auth_dead"
        assert gate.map_family("revoked", "rotate") == "auth_dead"
        assert gate.map_family("denied", "rotate") == "denial"
        assert gate.map_family("insufficient_quota", "rotate") == "billing"
        assert gate.map_family("daily", "rotate") == "throttle"
        assert gate.map_family("rate_limit", "rotate") == "throttle"
        assert gate.map_family("upstream_error", "raise") == "upstream"
        assert gate.map_family("model_not_ready", "raise") == "model_not_ready"
        assert gate.map_family("auth_refresh", "raise") == "auth_refresh"
        assert gate.map_family("content_filter", "raise") == "request_fault"

    def test_other_kind_splits_on_verdict_exactly_like_is_terminal_would(self):
        # A terminal status (is_terminal()==True) forces verdict="raise" —
        # the plugin's own signal that "other" means request_fault here.
        assert gate.map_family("other", "raise") == "request_fault"
        # A non-terminal status (e.g. 418) stays verdict="rotate" — "other"
        # means genuinely unknown here.
        assert gate.map_family("other", "rotate") == "unknown"
        assert gate.map_family("other", "stitch") == "unknown"

    def test_action_mapping(self):
        assert gate.map_action("rotate", "server") == "rotate_key"
        assert gate.map_action("stitch", "content_filter") == "stitch_or_return_partial"
        assert gate.map_action("raise", "content_filter") == "raise_to_host"
        assert gate.map_action("raise", "upstream_error") == "raise_to_host"
        # The two host-owned-recovery kinds are the only "raise" override.
        assert gate.map_action("raise", "auth_refresh") == "defer_host_retry"
        assert gate.map_action("raise", "host_breaker") == "defer_host_retry"

    def test_window_mapping_prefers_the_captured_quota_window(self):
        assert gate.map_window("rate_limit", "per_minute") == "per_minute"
        assert gate.map_window("rate_limit", "per_day") == "per_day"
        # QuotaWindow has no separate tokens-per-minute member: a captured
        # "per_minute" wins even for a TPM refusal (documented gap).
        assert gate.map_window("rate_limit", "per_minute") != "tokens_per_minute"
        # No captured Verdict (legacy path) falls back to the kind alone.
        assert gate.map_window("daily", None) == "per_day"
        assert gate.map_window("rate_limit", None) == "unknown"
        assert gate.map_window("server", None) == "none"

    def test_window_is_only_meaningful_for_the_throttle_family(self):
        assert gate.window_for_family("throttle", "per_day") == "per_day"
        assert gate.window_for_family("throttle", "unknown") == "unknown"
        assert gate.window_for_family("unknown", "per_day") == "unknown"  # unresolved family, not "none"
        assert gate.window_for_family("server", "per_day") == "none"
        assert gate.window_for_family("auth_dead", "unknown") == "none"
        assert gate.window_for_family("request_fault", "none") == "none"

    def test_scope_mapping_prefers_the_captured_quota_scope(self):
        assert gate.map_scope("rate_limit", "per_model") == "model"
        assert gate.map_scope("rate_limit", "account") == "account"
        assert gate.map_scope("denied", None) == "model"
        assert gate.map_scope("auth", None) == "credential"
        assert gate.map_scope("insufficient_quota", None) == "account"
        assert gate.map_scope("server", None) == "unknown"

    def test_scope_a_merely_generic_captured_unknown_does_not_override_the_kind_default(self):
        # QuotaScope.UNKNOWN == "unknown" is a non-empty string (truthy) but
        # carries no real information — R18's per-kind default must win.
        assert gate.map_scope("rate_limit", "unknown") == "model"
        assert gate.map_scope("auth", "unknown") == "credential"
        # A kind with no documented default still ends up "unknown" either way.
        assert gate.map_scope("server", "unknown") == "unknown"


# ---------------------------------------------------------------------------
# End to end against the REAL plugin package
# ---------------------------------------------------------------------------

#: A real, already-observed shape (real-03 in the answer key): Gemini 503,
#: "experiencing high demand", family=server, action=rotate_key, flat 1s.
#: NOTE on scope: real-03's OWN answer-key row says scope="model" (the
#: message names the model), but 5 of the 7 "server"-family rows in the
#: whole answer key say scope="unknown", and the plugin's kind="server" path
#: never sets a quota_scope at all (it is not a quota-classified failure) —
#: so map_scope's documented default for "server" is "unknown" (majority
#: rule, GATE.md). This end-to-end test checks GATE MECHANICS (a correctly
#: set expectation shows full agreement), so it uses "unknown" here to match
#: what the mapping table actually, defensibly produces; the real
#: matchers.json entry for real-03 keeps the answer key's own "model"
#: verbatim, and the real gate run reports that one field as a genuine,
#: documented disagreement (see GATE.md) rather than silently forcing this
#: table to fit that one row.
_HIGH_DEMAND_MESSAGE = (
    "Gemini HTTP 503 (UNAVAILABLE): This model is currently experiencing "
    "high demand. Spikes in demand are usually temporary. Please try again later."
)


def _high_demand_call_row(at: float) -> dict:
    return {
        "at": at, "identity": "gemini:gemini-3.8-flash", "key": "key:aaa",
        "attempt": 1, "outcome": "rotate", "kind": "server", "status": 503, "rest_s": None,
    }


def _high_demand_refusal_row(at: float) -> dict:
    return {
        "at": at, "provider": "gemini", "model": "gemini:gemini-3.8-flash",
        "status": 503, "type": "GeminiAPIError", "code": "",
        "message": _HIGH_DEMAND_MESSAGE, "body": "", "response": "",
    }


def _write_evidence(tmp_path, at):
    calls_path = tmp_path / "calls.jsonl"
    refusals_path = tmp_path / "refusals.jsonl"
    calls_path.write_text(json.dumps(_high_demand_call_row(at)) + "\n", encoding="utf-8")
    refusals_path.write_text(json.dumps(_high_demand_refusal_row(at + 0.05)) + "\n", encoding="utf-8")
    return calls_path, refusals_path


def _write_matchers(tmp_path, expected, rest, name="matchers.json"):
    matchers = [
        {
            "shape_id": "high-demand",
            "match": {"status": 503, "provider_in": ["gemini"], "message_regex": r"experiencing high demand"},
            "expected": expected,
            "rest": rest,
            "origin": "real",
        }
    ]
    path = tmp_path / name
    path.write_text(json.dumps(matchers), encoding="utf-8")
    return path


_CORRECT_EXPECTED = {"family": "server", "window": "none", "scope": "unknown", "action": "rotate_key"}
_CORRECT_REST = {"kind": "flat_base", "seconds": 1}


class TestEndToEndAgainstRealPlugin:
    def test_correct_expectation_shows_full_agreement(self, tmp_path):
        at = BASE_AT + 1000.0
        calls_path, refusals_path = _write_evidence(tmp_path, at)
        matchers_path = _write_matchers(tmp_path, _CORRECT_EXPECTED, _CORRECT_REST)
        out_path = tmp_path / "out.json"

        result = gate.run_gate(PLUGIN_DIR, calls_path, refusals_path, matchers_path, out_path)

        assert result["matching"]["unmatched"] == 0
        assert result["matching"]["multi_match"] == 0
        row = result["per_shape"][0]
        assert row["shape_id"] == "high-demand"
        assert row["records"] == 1
        assert row.get("disagree", {}) == {}
        assert row["agree"].get("family") == 1
        assert row["agree"].get("action") == 1
        assert row["agree"].get("rest") == 1
        assert out_path.is_file()

    def test_deliberately_wrong_expectation_fails_the_gate(self, tmp_path):
        # Same real record, same real plugin decision — only the answer key
        # entry is now wrong on purpose. A gate that always reports green
        # would pass this; this one must not.
        at = BASE_AT + 2000.0
        calls_path, refusals_path = _write_evidence(tmp_path, at)
        wrong_expected = {"family": "billing", "window": "per_day", "scope": "account", "action": "raise_to_host"}
        wrong_rest = {"kind": "fixed", "seconds": 3600, "cap": 3600}
        matchers_path = _write_matchers(tmp_path, wrong_expected, wrong_rest)
        out_path = tmp_path / "out.json"

        result = gate.run_gate(PLUGIN_DIR, calls_path, refusals_path, matchers_path, out_path)

        row = result["per_shape"][0]
        assert row["records"] == 1
        assert row["disagree"].get("family") == 1
        assert row["disagree"].get("action") == 1
        assert row["disagree"].get("rest") == 1
        assert row["agree"].get("family", 0) == 0
        assert len(row["examples"]) >= 1
        example = row["examples"][0]
        assert example["produced"]["family"] == "server"
        assert example["expected"]["family"] == "billing"
        # Redaction: no digit survives in the stored message.
        assert not any(ch.isdigit() for ch in example["message"])

    def test_a_shape_with_no_matching_record_is_reported_not_exercised(self, tmp_path):
        at = BASE_AT + 3000.0
        calls_path, refusals_path = _write_evidence(tmp_path, at)
        matchers = [
            {
                "shape_id": "never-seen",
                "match": {"status": 999},
                "expected": _CORRECT_EXPECTED,
                "rest": _CORRECT_REST,
                "origin": "documented",
            }
        ]
        matchers_path = tmp_path / "matchers.json"
        matchers_path.write_text(json.dumps(matchers), encoding="utf-8")
        out_path = tmp_path / "out.json"

        result = gate.run_gate(PLUGIN_DIR, calls_path, refusals_path, matchers_path, out_path)

        assert result["shapes_not_exercised"] == ["never-seen"]
        assert result["per_shape"][0]["not_exercised"] is True
        assert result["matching"]["unmatched"] == 1  # the real record matched nothing

    def test_never_prints_the_raw_key(self, tmp_path):
        at = BASE_AT + 4000.0
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        secret_key = "sk-THIS-MUST-NEVER-APPEAR-IN-OUTPUT"
        row = _high_demand_call_row(at)
        row["key"] = secret_key
        calls_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        refusals_path.write_text(json.dumps(_high_demand_refusal_row(at + 0.05)) + "\n", encoding="utf-8")
        matchers_path = _write_matchers(tmp_path, _CORRECT_EXPECTED, _CORRECT_REST)
        out_path = tmp_path / "out.json"

        gate.run_gate(PLUGIN_DIR, calls_path, refusals_path, matchers_path, out_path)

        assert secret_key not in out_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Field applicability (Change 2): scope/window only for throttle/billing;
# rest's "none" only counts as agreement when a real number was produced.
# ---------------------------------------------------------------------------


class TestFieldApplicabilityUnit:
    def test_scope_and_window_apply_only_to_throttle_and_billing(self):
        for family in ("throttle", "billing"):
            assert gate.field_applies("scope", family) is True
            assert gate.field_applies("window", family) is True
        for family in (
            "server", "timeout", "request_fault", "auth_dead", "auth_refresh",
            "denial", "model_not_ready", "upstream", "unknown",
        ):
            assert gate.field_applies("scope", family) is False
            assert gate.field_applies("window", family) is False

    def test_family_and_action_always_apply(self):
        for family in ("throttle", "server", "timeout", "request_fault", "unknown", ""):
            assert gate.field_applies("family", family) is True
            assert gate.field_applies("action", family) is True


#: A terse real Gemini per-minute throttle (real-01's own shape family):
#: no quotaId, no retry hint, nothing to size scope or a delay by — the
#: plugin's evidence-first classifier declines and the legacy table answers
#: kind="per_minute" -> family="throttle", scope defaults to "model" (R18).
def _gemini_429_call_row(at: float) -> dict:
    return {
        "at": at, "identity": "gemini:gemini-3.8-flash", "key": "key:ccc",
        "attempt": 1, "outcome": "rotate", "kind": "per_minute", "status": 429, "rest_s": None,
    }


def _gemini_429_refusal_row(at: float) -> dict:
    return {
        "at": at, "provider": "gemini", "model": "gemini:gemini-3.8-flash",
        "status": 429, "type": "GeminiAPIError", "code": "",
        "message": "429 RESOURCE_EXHAUSTED: Resource has been exhausted (e.g. check quota).",
        "body": "", "response": "",
    }


class TestFieldApplicabilityEndToEnd:
    def test_scope_and_window_are_not_applicable_for_a_no_counter_family(self, tmp_path):
        # family="server" (real-03's own family) names no counter at all —
        # vocabulary.md §2 — so scope/window must be reported
        # not_applicable, whatever value sits in the answer key's row here
        # (even a deliberately wrong one collects no disagreement, because
        # the family never posed the question).
        at = BASE_AT + 5000.0
        calls_path, refusals_path = _write_evidence(tmp_path, at)
        expected = {"family": "server", "window": "per_day", "scope": "account", "action": "rotate_key"}
        matchers_path = _write_matchers(tmp_path, expected, _CORRECT_REST)
        out_path = tmp_path / "out.json"

        result = gate.run_gate(PLUGIN_DIR, calls_path, refusals_path, matchers_path, out_path)

        row = result["per_shape"][0]
        assert row["not_applicable"].get("scope") == 1
        assert row["not_applicable"].get("window") == 1
        assert row["agree"].get("scope", 0) == 0
        assert row["disagree"].get("scope", 0) == 0
        assert row["agree"].get("window", 0) == 0
        assert row["disagree"].get("window", 0) == 0
        # family/action are unaffected by the gate — still scored normally.
        assert row["agree"].get("family") == 1
        assert row["agree"].get("action") == 1
        # And the top-level summary prints the same counts, never swallowed.
        assert result["not_applicable"]["scope"] >= 1
        assert result["not_applicable"]["window"] >= 1

    def test_a_field_that_does_apply_still_fails_when_wrong(self, tmp_path):
        # family="throttle" DOES drive scope. The real produced scope for
        # this terse refusal (no evidence at all) is the kind-default
        # "model" (R18) — a deliberately wrong expectation of "account" must
        # still be scored, and must still disagree, not vanish into NA.
        at = BASE_AT + 6000.0
        calls_path = tmp_path / "calls.jsonl"
        refusals_path = tmp_path / "refusals.jsonl"
        calls_path.write_text(json.dumps(_gemini_429_call_row(at)) + "\n", encoding="utf-8")
        refusals_path.write_text(
            json.dumps(_gemini_429_refusal_row(at + 0.05)) + "\n", encoding="utf-8"
        )
        expected = {"family": "throttle", "window": "unknown", "scope": "account", "action": "rotate_key"}
        rest = {"kind": "fixed", "seconds": 20, "cap": 3600}
        matchers_path = _write_matchers(tmp_path, expected, rest, name="throttle-matchers.json")
        # ``_write_matchers`` hardcodes a 503/"experiencing high demand"
        # predicate; overwrite it with one that matches this 429 instead.
        matchers = json.loads(matchers_path.read_text(encoding="utf-8"))
        matchers[0]["match"] = {"status": 429, "provider_in": ["gemini"], "message_regex": r"exhausted"}
        matchers_path.write_text(json.dumps(matchers), encoding="utf-8")
        out_path = tmp_path / "out.json"

        result = gate.run_gate(PLUGIN_DIR, calls_path, refusals_path, matchers_path, out_path)

        row = result["per_shape"][0]
        assert row["records"] == 1
        assert row["disagree"].get("scope") == 1
        assert row.get("not_applicable", {}).get("scope", 0) == 0
        assert row["agree"].get("family") == 1
        assert row["agree"].get("action") == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
