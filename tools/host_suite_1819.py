"""1.8.1.9 gate: Hermes' own suites, clean and with KAME live in every home.

``host_pool_suite.py`` asked whether KAME's *pool binding* changed an answer
in Hermes' credential-pool suites. 1.8.1.9 has no pool binding: it touches
nothing of Hermes' and instead hands Hermes a client through the provider
profiles (``hermes-kame-provider``). So the question becomes: with every
bundled API-key chat profile answering ``create_client`` with KAME's client,
in every test home, does any of Hermes' own tests — the credential pool, the
provider-client seam, the auxiliary client, the Gemini adapter, the error
hook — come out differently?

Run twice, compare. The interposer counts the KAME clients it actually built;
a "with KAME" run that built none measured nothing and fails the gate.

    python tools/host_suite_1819.py [--hermes PATH]

Nothing is installed into the user's Hermes; every run gets a throwaway
``HERMES_HOME`` and no credentials from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PROVIDER_DIR = ROOT / "hermes-kame-provider"

SUITES = (
    "tests/agent/test_credential_pool.py",
    "tests/agent/test_credential_pool_deferred_refresh.py",
    "tests/agent/test_credential_pool_key_rotation.py",
    "tests/agent/test_credential_pool_lease_refresh_reselect.py",
    "tests/agent/test_credential_pool_provider_boundary.py",
    "tests/agent/test_credential_pool_routing.py",
    "tests/agent/test_credential_pool_sole_cooldown.py",
    "tests/agent/test_credential_pool_unmatched_rotation_bound.py",
    "tests/agent/test_restore_primary_pool_reselect.py",
    "tests/agent/test_provider_client_seam.py",
    "tests/agent/test_auxiliary_provider_supplied_client.py",
    "tests/agent/test_gemini_native_adapter.py",
    "tests/agent/test_gemini_alias_native_route.py",
    "tests/agent/test_stream_serving_provider.py",
    "tests/agent/test_first_chunk_at_hook.py",
    "tests/agent/test_auxiliary_hooks.py",
    "tests/plugins/test_transform_api_error_classification_hook.py",
    "tests/hermes_cli/test_gemini_provider.py",
    "tests/hermes_cli/test_external_process_provider_seam.py",
    "tests/providers",
    # Every place Hermes builds a client for a bundled provider: these are the
    # suites where KAME's client is actually handed out.
    "tests/agent/test_auxiliary_client.py",
    "tests/agent/test_streaming.py",
    "tests/agent/test_create_openai_client_reuse.py",
    "tests/agent/test_shared_http_transport.py",
    "tests/agent/test_error_classifier.py",
)

#: Host tests that replace the client class Hermes would construct with a mock
#: and then assert on the mock. KAME's client is a subclass of the real class,
#: built from the real one, so the mock is never called and these "diverge" by
#: construction. What they check is pinned on KAME's own client instead.
EXPECTED_DIVERGENCE = {
    "tests/agent/test_auxiliary_client.py::TestNvidiaBillingHeaders::test_resolve_provider_client_cloud_adds_billing_origin_header":
        "mocks agent.auxiliary_client.OpenAI; header pinned by test_facade_1819::TestHermesHeadersReachKamesClient",
    "tests/agent/test_auxiliary_client.py::TestNvidiaBillingHeaders::test_resolve_provider_client_local_nim_skips_billing_origin_header":
        "mocks agent.auxiliary_client.OpenAI; pinned by test_facade_1819::TestHermesHeadersReachKamesClient",
    "tests/hermes_cli/test_gemini_provider.py::TestGeminiAgentInit::test_gemini_resolve_provider_client_uses_native_client":
        "mocks GeminiNativeClient; KAME's client is a GeminiNativeClient subclass (test_facade_1819::TestTheShapeHermesExpects)",
}

INTERPOSER = r'''
import importlib.util, json, os, sys
from pathlib import Path

_BUILT = {"kame_clients": 0, "declined": 0}


def pytest_configure(config):
    plugin_dir = Path(os.environ["KAME_SUITE_PLUGIN_DIR"])
    provider_dir = Path(os.environ["KAME_SUITE_PROVIDER_DIR"])
    spec = importlib.util.spec_from_file_location(
        "kame_suite_plugin", plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)])
    kame = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = kame
    spec.loader.exec_module(kame)
    facade = importlib.import_module("kame_suite_plugin.facade")
    importlib.import_module("kame_suite_plugin.transport").configure(facade.TRANSPORT)
    real = facade.make_client

    def counted(provider, client_kwargs):
        client = real(provider, client_kwargs)
        _BUILT["kame_clients" if client is not None else "declined"] += 1
        return client

    facade.make_client = counted  # the gate's own copy of KAME, not Hermes
    facade.publish(kame)

    class EveryHome(dict):
        def get(self, key, default=None):
            return kame

        def __bool__(self):
            return True

    sys.modules[facade.REGISTRY_KEY].homes = EveryHome()
    from providers import list_providers
    list_providers()
    pspec = importlib.util.spec_from_file_location("kame_suite_provider", provider_dir / "__init__.py")
    provider = importlib.util.module_from_spec(pspec)
    sys.modules[pspec.name] = provider
    pspec.loader.exec_module(provider)
    _BUILT["profiles"] = provider._REGISTERED
    _BUILT["per_test"] = 0


import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    # After the test's fixtures ran (they may rebuild the provider registry),
    # before its body: a home plugin is re-discovered the same way in real use.
    provider = sys.modules.get("kame_suite_provider")
    if provider is not None:
        try:
            if provider._register_all():
                _BUILT["per_test"] += 1
            from providers import get_provider_profile
            seen = get_provider_profile("gemini")
            if type(seen).__module__ == "kame_suite_provider":
                _BUILT["kame_profile_visible"] = _BUILT.get("kame_profile_visible", 0) + 1
        except Exception as exc:
            _BUILT.setdefault("errors", []).append(repr(exc)[:200])


def pytest_unconfigure(config):
    out = os.environ.get("KAME_SUITE_REPORT")
    if out:
        Path(out).write_text(json.dumps(_BUILT), encoding="utf-8")
'''


def run(hermes: Path, suites, kame: bool, work: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not any(w in k.upper() for w in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
           and not k.startswith("HERMES_")}
    home = Path(tempfile.mkdtemp(prefix="kame-suite-home-", dir=work))
    env.update(HERMES_HOME=str(home), KAME_RECORDER_DISABLED="1", KAME_CALL_TIMINGS_DISABLED="1",
               PYTHONIOENCODING="utf-8")
    paths = [str(hermes)]
    args = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
            "-p", "no:randomly", "-o", "addopts=", "--timeout", "120", "-rfE"]
    report = work / ("kame.json" if kame else "clean.json")
    if kame:
        (work / "kame_suite_interposer.py").write_text(INTERPOSER, encoding="utf-8")
        paths.insert(0, str(work))
        env.update(KAME_SUITE_PLUGIN_DIR=str(PLUGIN_DIR), KAME_SUITE_PROVIDER_DIR=str(PROVIDER_DIR),
                   KAME_SUITE_REPORT=str(report))
        args += ["-p", "kame_suite_interposer"]
    env["PYTHONPATH"] = os.pathsep.join(paths + [env.get("PYTHONPATH", "")])
    proc = subprocess.run(args + list(suites), cwd=str(hermes), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=3600)
    out = proc.stdout + proc.stderr
    failed = set(re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M))
    summary = next((l for l in reversed(out.splitlines()) if re.search(r"\d+ (passed|failed)", l)), "")
    built = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    return {"failed": failed, "summary": summary.strip("= "), "built": built, "code": proc.returncode}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hermes", type=Path, default=Path(os.environ.get(
        "KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes/hermes-agent")))
    args = parser.parse_args()
    hermes = args.hermes.resolve()
    suites = [s for s in SUITES if (hermes / s).exists()]
    missing = [s for s in SUITES if s not in suites]
    with tempfile.TemporaryDirectory(prefix="kame-suite-") as tmp:
        work = Path(tmp)
        clean = run(hermes, suites, False, work)
        kame = run(hermes, suites, True, work)
    print(f"suites: {len(suites)} present" + (f", absent here: {missing}" if missing else ""))
    print(f"clean:     {clean['summary']}")
    print(f"with KAME: {kame['summary']}")
    print(f"           KAME profiles registered: {kame['built'].get('profiles')}, "
          f"KAME clients built: {kame['built'].get('kame_clients')}, declined: {kame['built'].get('declined')}")
    diverged_all = sorted(kame["failed"] - clean["failed"])
    for name in diverged_all:
        if name in EXPECTED_DIVERGENCE:
            print(f"  expected  {name}")
            print(f"            ({EXPECTED_DIVERGENCE[name]})")
    diverged = [name for name in diverged_all if name not in EXPECTED_DIVERGENCE]
    healed = sorted(clean["failed"] - kame["failed"])
    for name in diverged:
        print(f"  DIVERGED  {name}")
    for name in healed:
        print(f"  (passes only with KAME)  {name}")
    measured = bool(kame["built"].get("profiles")) and kame["built"].get("kame_clients", 0) > 0
    if not measured:
        print("GATE FAIL: the KAME run built no KAME client, so it measured nothing")
        return 1
    if diverged:
        print(f"GATE FAIL: {len(diverged)} host test(s) answer differently with KAME")
        return 1
    print("GATE PASS: Hermes' own suites answer the same with KAME's client in every home")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
