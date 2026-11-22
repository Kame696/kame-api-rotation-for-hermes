r"""1.9.0.5 (F2): a fresh install of the one package, checked against a real Hermes checkout. No network.

    python tools/fresh_install_1905.py --host ~/dev/hermes-agent [--ref v0.23.3|origin/main]

Builds a throw-away ``HERMES_HOME`` (the package copied into ``plugins/``, enabled in ``config.yaml``, gemini as
the model, a fake two-key ``GOOGLE_API_KEY`` in ``.env``), then, in a subprocess with the checkout on the path
and the interpreter of Hermes' venv, asserts that:

* the plugin loaded and the ``gemini`` profile is KAME's (``Kame...``, registered at runtime);
* ``create_client(api_key=<fake>, base_url=<profile's>)`` returns a ``KameProfileClient``;
* the bundled profiles that define their own ``create_client`` are exactly the expected set (a new one is a
  FAIL: it would need the same treatment as gemini).

``--ref`` checks that ref out in the checkout first (detached) and puts the original ref back afterwards.
Run it with ``~/dev/hermes-venv/Scripts/python.exe``. Exit 0 = PASS, 1 = FAIL, 2 = cannot run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "hermes-kame-api-rotation"
PLUGIN_DIR = ROOT / PLUGIN

#: Bundled profiles that supply their own client (so KAME must wrap it, see ``facade.make_profile_client``).
#: A new name in the sweep means a provider that may bypass the rotation: review it, then add it here.
EXPECTED_OWN_CLIENT = {"gemini", "solstice"}

#: Two well-shaped (``AIza`` + 35, 39 characters) but invalid keys, never sent anywhere.
FAKE_KEYS = ("AIza" + "FakeKeyOne".ljust(35, "0"), "AIza" + "FakeKeyTwo".ljust(35, "0"))
assert all(len(k) == 39 for k in FAKE_KEYS)

CHILD = r'''
import json, os, sys
out = {}
from hermes_cli import plugins as hp
hp.discover_plugins(force=True)
import providers
from providers.base import ProviderProfile
mgr = hp.get_plugin_manager()
loaded = getattr(mgr, "_plugins", {})
rec = loaded.get("hermes-kame-api-rotation")
out["plugin_loaded"] = bool(rec) and getattr(rec, "error", None) is None and getattr(rec, "enabled", True)
out["plugin_error"] = str(getattr(rec, "error", "") or "")[:200] if rec else "not discovered"
prof = providers.get_provider_profile("gemini")
out["gemini_class"] = type(prof).__name__
out["gemini_source"] = providers.provider_source("gemini")
fake = sys.argv[1]
cli = prof.create_client(api_key=fake, base_url=prof.base_url)
out["client_class"] = type(cli).__name__
out["client_has_chat_completions"] = hasattr(getattr(cli, "chat", None), "completions")
sweep = []
for p in providers.list_providers():
    for cls in type(p).__mro__:
        if cls is ProviderProfile:
            break
        if cls.__name__.startswith("Kame"):
            continue
        if "create_client" in cls.__dict__:
            sweep.append(p.name)
            break
out["sweep"] = sorted(set(sweep))
print(json.dumps(out))
'''


def _git(host: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(host), *args], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def run(host: Path, python: str) -> int:
    home = Path(tempfile.mkdtemp(prefix="kame-fresh-install-"))
    try:
        (home / "plugins").mkdir()
        shutil.copytree(PLUGIN_DIR, home / "plugins" / PLUGIN,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (home / "config.yaml").write_text(
            f"plugins:\n  enabled:\n    - {PLUGIN}\nmodel:\n  provider: gemini\n  default: gemini-flash-lite-latest\n",
            encoding="utf-8")
        (home / ".env").write_text("GOOGLE_API_KEY=" + ",".join(FAKE_KEYS) + "\n", encoding="utf-8")
        env = dict(os.environ)
        env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(host), "PYTHONIOENCODING": "utf-8",
                    "KAME_RECORDER_DISABLED": "1", "KAME_CALL_TIMINGS_DISABLED": "1"})
        env.pop("HERMES_PLUGIN_HOST_PROCESS", None)
        env.pop("GOOGLE_API_KEY", None)
        done = subprocess.run([python, "-c", CHILD, FAKE_KEYS[0]], cwd=str(host), env=env,
                              capture_output=True, text=True, timeout=300)
        lines = [ln for ln in done.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            print(f"FAIL: no result (rc={done.returncode})\n{done.stdout[-800:]}\n{done.stderr[-1200:]}")
            return 1
        out = json.loads(lines[-1])
    finally:
        shutil.rmtree(home, ignore_errors=True)
    checks = [
        ("plugin loaded", out["plugin_loaded"], out["plugin_error"]),
        ("gemini profile is KAME's", out["gemini_class"].startswith("Kame") and out["gemini_source"] == "runtime",
         f"{out['gemini_class']} / {out['gemini_source']}"),
        ("create_client returns a KameProfileClient", out["client_class"] == "KameProfileClient",
         out["client_class"]),
    ]
    sweep = set(out["sweep"])
    checks.append(("sweep matches the expected set", sweep == EXPECTED_OWN_CLIENT,
                   f"found {sorted(sweep)}, expected {sorted(EXPECTED_OWN_CLIENT)}"
                   + (f"; NEW: {sorted(sweep - EXPECTED_OWN_CLIENT)}" if sweep - EXPECTED_OWN_CLIENT else "")
                   + (f"; GONE: {sorted(EXPECTED_OWN_CLIENT - sweep)}" if EXPECTED_OWN_CLIENT - sweep else "")))
    ok = True
    for name, passed, detail in checks:
        ok &= bool(passed)
        print(f"{'PASS' if passed else 'FAIL'}  {name}  ({detail})")
    print(f"sweep of bundled profiles with their own create_client: {sorted(sweep)}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", required=True, help="path of a Hermes checkout")
    ap.add_argument("--ref", default="", help="check this ref out first (restored afterwards)")
    ap.add_argument("--python", default=sys.executable, help="interpreter of Hermes' venv (default: this one)")
    args = ap.parse_args(argv)
    host = Path(args.host).resolve()
    if not (host / "providers").is_dir():
        print(f"not a Hermes checkout: {host}")
        return 2
    original = ""
    try:
        if args.ref:
            if _git(host, "status", "--porcelain", "--untracked-files=no"):
                print("refusing to switch ref: the checkout has local changes")
                return 2
            head = subprocess.run(["git", "-C", str(host), "symbolic-ref", "-q", "--short", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            original = head or _git(host, "rev-parse", "HEAD")
            _git(host, "checkout", "-q", "--detach", args.ref)
            print(f"host {host} at {args.ref} ({_git(host, 'rev-parse', '--short', 'HEAD')})")
        return run(host, args.python)
    finally:
        if original:
            try:
                _git(host, "checkout", "-q", original)
            except RuntimeError as exc:
                print(f"WARNING: could not restore {original}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
