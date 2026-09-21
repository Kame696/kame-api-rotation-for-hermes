"""Execute Gemini host contracts with MockTransport, never real requests.

Run in a fresh subprocess. The host home is redirected before imports; no
installed plugin, credentials, production logs or configuration are changed.
Each contract has a deliberate broken variant to check that it detects drift.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def mapping(adapter):
    contents, _ = adapter._build_gemini_contents([
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "partial answer"},
        {"role": "user", "content": "continue"},
    ])
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][0]["text"] == "partial answer"


def refusal(adapter, httpx):
    words = "Requests ending with a model turn are not supported"
    response = httpx.Response(400, json={"error": {
        "status": "INVALID_ARGUMENT", "message": words}})
    error = adapter.gemini_http_error(response)
    assert words in str(error)
    assert error.status_code == 400


def guidance(adapter, httpx, evidence, host_text):
    words = "Quota exceeded for generate_content_free_tier_requests. Please retry in 7s."
    error = adapter.gemini_http_error(httpx.Response(429, json={"error": {
        "status": "RESOURCE_EXHAUSTED", "message": words}}))
    block = adapter._FREE_TIER_GUIDANCE
    assert block.strip() in str(error), "host guidance was not appended"
    assert block in host_text.guidance_blocks(), "plugin did not discover current host guidance"
    clean = evidence.harvest(error, message=str(error), guidance_blocks=host_text.guidance_blocks())
    assert words in clean.message
    assert block.strip() not in clean.message, "host advice leaked into provider evidence"


def factory(adapter, httpx, qid, evidence, classifier):
    """Drive actual client entrypoints, including an unread streaming body."""
    original = adapter.gemini_http_error
    assert qid.install(), "quota-id binding did not install"
    try:
        for stream, window, label in [(False, "per_day", "PerDay"),
                                       (True, "per_day", "PerDay"),
                                       (True, "per_minute", "PerMinute")]:
            body = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded",
                "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequests" + label + "PerProjectPerModel-FreeTier"}]},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}}
            class UnreadBody(httpx.SyncByteStream):
                def __iter__(self):
                    yield json.dumps(body).encode("utf-8")
            calls = []
            def respond(request):
                calls.append(str(request.url))
                return httpx.Response(429, headers={"content-type": "application/json"}, stream=UnreadBody())
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = adapter.GeminiNativeClient(api_key="synthetic-not-a-key", http_client=transport)
                try:
                    result = client.chat.completions.create(model="gemini-test", stream=stream,
                        messages=[{"role": "user", "content": "test"}])
                    if stream:
                        list(result)
                except adapter.GeminiAPIError as error:
                    assert getattr(error, "body", None) == body, "factory binding lost structured body"
                    ev = evidence.harvest(error, message=str(error))
                    assert classifier.stated_window(error_body=ev.body, error=error) == window
                    if stream:
                        try:
                            error.response.text
                        except httpx.ResponseNotRead:
                            pass
                        else:
                            raise AssertionError("stream fixture was not actually unread")
                else:
                    raise AssertionError("expected API error did not reach caller")
            assert len(calls) == 1, "contract unexpectedly retried"
            assert ("streamGenerateContent" in calls[0]) == stream
    finally:
        qid.uninstall()
    assert adapter.gemini_http_error is original, "factory not restored"


def evaluate(name, operation):
    try:
        operation()
    except Exception as exc:
        return {"name": name, "passed": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"name": name, "passed": True, "detail": "executed contract"}


def run(host):
    sys.path.insert(0, str(host))
    adapter = importlib.import_module("agent.gemini_native_adapter")
    import httpx
    plugin = ROOT / "hermes-kame-api-rotation"
    spec = importlib.util.spec_from_file_location("kame_host_contract_707", plugin / "__init__.py",
                                                submodule_search_locations=[str(plugin)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    get = lambda name: importlib.import_module(spec.name + "." + name)
    qid, evidence, classifier, host_text = (get(n) for n in
        ("quota_id_binding", "core.evidence", "core.classify", "host_text"))
    checks = {
        "assistant_mapping": lambda: mapping(adapter),
        "refusal_fields": lambda: refusal(adapter, httpx),
        "guidance_separation": lambda: guidance(adapter, httpx, evidence, host_text),
        "factory_paths": lambda: factory(adapter, httpx, qid, evidence, classifier),
    }
    results = {name: evaluate(name, callback) for name, callback in checks.items()}
    broken = {}
    with patch.object(adapter, "_build_gemini_contents", return_value=([], None)):
        broken["assistant_mapping"] = evaluate("assistant_mapping", checks["assistant_mapping"])
    with patch.object(adapter, "gemini_http_error", return_value=RuntimeError("lost provider message")):
        broken["refusal_fields"] = evaluate("refusal_fields", checks["refusal_fields"])
    with patch.object(host_text, "guidance_blocks", return_value=[]):
        broken["guidance_separation"] = evaluate("guidance_separation", checks["guidance_separation"])
    with patch.object(qid, "install", return_value=True):
        broken["factory_paths"] = evaluate("factory_paths", checks["factory_paths"])
    return {"host": str(host), "network": "MockTransport only", "checks": results,
            "mutation_detected": {name: not row["passed"] for name, row in broken.items()},
            "passed": all(r["passed"] for r in results.values()) and
                      all(not r["passed"] for r in broken.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, default=Path(os.environ.get("KAME_HERMES_ROOT",
        str(Path.home() / "AppData/Local/hermes/hermes-agent"))))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kame-host-contract-") as home:
        os.environ["HERMES_HOME"] = home
        os.environ["KAME_RECORDER_DISABLED"] = "1"
        os.environ["KAME_CALL_TIMINGS_DISABLED"] = "1"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        result = run(args.host)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
