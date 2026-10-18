"""Replay source-linked cases without host imports, credentials or requests.

Unlike regression tests, this report may contain unmet expectations. A mismatch
is a finding to investigate, not permission to rewrite the expected result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "hermes-kame-api-rotation"))
from core import classify


def replay(cases):
    results = []
    for case in cases:
        if not case.get("source_url", "").startswith("https://"):
            raise ValueError("Every case needs a source URL")
        args = dict(case["input"])
        args.setdefault("now_epoch", 1800000000)
        if case.get("request_url"):
            error = Exception(args.get("error_message", ""))
            error.request = SimpleNamespace(url=case["request_url"])
            args["error"] = error
        verdict = classify(**args)
        observed = {name: getattr(verdict, name, None) for name in (
            "reason", "quota_window", "quota_scope", "source", "reset_at",
            "retryable", "should_rotate_credential")}
        observed["defers_to_host"] = verdict is None
        differences = {key: {"expected": expected, "observed": observed.get(key)}
                       for key, expected in case.get("expected", {}).items()
                       if observed.get(key) != expected}
        results.append({"id": case["id"], "surface": case["surface"],
            "source_url": case["source_url"], "evidence_kind": case["evidence_kind"],
            "observed": observed, "differences": differences,
            "assessment": "needs-review" if differences else "matches-declared-expectations"})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    raw = args.cases.read_bytes()
    results = replay(json.loads(raw))
    report = {"cases_sha256": hashlib.sha256(raw).hexdigest(),
              "cases": len(results), "needs_review": sum(bool(r["differences"]) for r in results),
              "scope": "core classifier only; not live or host dispatch validation",
              "results": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k != "results"}))
    return 1 if report["needs_review"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
