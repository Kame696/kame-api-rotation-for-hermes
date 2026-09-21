"""Offline real-adapter error propagation and request-count contract."""
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch


def denied(*args, **kwargs):
    raise AssertionError("Network forbidden in transport contract")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.dont_write_bytecode = True
    rows = []
    with tempfile.TemporaryDirectory(prefix="kame-gemini-contract-") as home:
        with patch.dict(os.environ, {"HERMES_HOME": home, "KAME_RECORDER_DISABLED": "1"}), \
             patch.object(socket.socket, "connect", denied), \
             patch.object(socket, "getaddrinfo", denied):
            sys.path.insert(0, str(args.host))
            import httpx
            from agent.gemini_native_adapter import GeminiNativeClient, GeminiAPIError
            sys.path.insert(0, str(root / "hermes-kame-api-rotation"))
            from core import classify
            package_name = "kame_transport_contract"
            source_root = root / "hermes-kame-api-rotation"
            spec = importlib.util.spec_from_file_location(package_name, source_root / "__init__.py",
                submodule_search_locations=[str(source_root)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[package_name] = package
            spec.loader.exec_module(package)
            dispatch = importlib.import_module(package_name + ".dispatch_binding")
            for stream in (False, True):
                for status, label, details, expected in (
                    (429, "RESOURCE_EXHAUSTED", [], "unknown"),
                    (429, "RESOURCE_EXHAUSTED", [{
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]
                    }], "per_minute"),
                    (503, "UNAVAILABLE", [], None),
                ):
                    calls = []
                    def respond(request):
                        calls.append(request.url.path)
                        return httpx.Response(status, headers={"retry-after": "2.5"},
                            json={"error": {"code": status, "status": label,
                                "message": label, "details": details}})
                    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                        client = GeminiNativeClient(api_key="synthetic", http_client=http)
                        try:
                            value = client.chat.completions.create(model="contract-model",
                                messages=[{"role": "user", "content": "synthetic"}], stream=stream)
                            if stream:
                                list(value)
                            raise AssertionError("Error response unexpectedly succeeded")
                        except GeminiAPIError as exc:
                            assert exc.status_code == status
                            assert exc.retry_after == 2.5
                            assert len(calls) == 1, calls
                            verdict = classify(provider="gemini", status_code=status,
                                               error=exc, now_epoch=1800000000)
                            if expected is not None:
                                assert verdict.quota_window == expected, verdict
                                assert verdict.reset_at == 1800000002.5, verdict
                            binding = dispatch.DispatchBinding()
                            with patch("time.time", return_value=1800000000):
                                decision = binding._on_failure("gemini:contract-model", "synthetic",
                                    exc, "contract", 1, False)  # No response content delivered.
                                assert decision[0] == "rotate", decision
                                assert decision[2] == status, decision
                                cooldown = binding.engine.next_recovery_seconds(
                                    "gemini:contract-model", ["synthetic"])
                                assert cooldown == 2.5, (decision, cooldown)
                            rows.append({"stream": stream, "status": status,
                                "quota_window_expected": expected, "calls": len(calls),
                                "status_preserved": True, "retry_hint_preserved": True,
                                "dispatch_action": decision[0], "cooldown": cooldown})
    source = args.host / "agent/gemini_native_adapter.py"
    result = {"passed": True, "network": "denied; HTTPX MockTransport",
              "host_source": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "cases": rows, "limitations": "No outer agent retry loop or live server exercised"}
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
