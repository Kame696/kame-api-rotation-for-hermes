r"""1.9.0.5 (F1): every Hermes symbol the package imports, checked by import against a Hermes checkout.

    ~/dev/hermes-venv/Scripts/python.exe tools/host_symbols.py --host ~/dev/hermes-agent

The list is built from the source, not written by hand: every ``from <root>... import name`` and
``import <root>...`` in ``hermes-kame-api-rotation/**/*.py`` (inside functions and ``try`` blocks too) whose
root is one of Hermes' top-level packages. Each is then imported with the checkout on ``sys.path`` and a
throw-away ``HERMES_HOME``. One line per symbol: PASS or FAIL (or OPTIONAL-MISSING). Exit 1 on any FAIL.

Run it with the interpreter of Hermes' own venv (it has Hermes' dependencies). Reads only; no network, no key.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "hermes-kame-api-rotation"
HERMES_ROOTS = ("agent", "hermes_cli", "gateway", "tools", "providers", "hermes_constants", "run_agent")

#: Symbols KAME guards with a fallback for an older (or newer) Hermes: the import sits inside ``try`` /
#: ``except ImportError`` and the code carries on without it. A miss is reported as OPTIONAL-MISSING and does
#: not fail the run. Keep this list short, and say why for every entry.
OPTIONAL: Dict[str, str] = {
    # Module-level seams that only exist on a Hermes that ships the matching upstream change; KAME probes
    # for them with try/except and takes its own path otherwise.
    "agent.secret_scope": "per-profile secret scope, newer Hermes only; guarded by try/except in control.py and envsync.py",
    "agent.secret_scope.build_profile_secret_scope": "same seam as agent.secret_scope",
    "agent.secret_scope.reset_secret_scope": "same seam as agent.secret_scope",
    "agent.secret_scope.set_secret_scope": "same seam as agent.secret_scope",
    "hermes_cli.env_loader.hydrate_profile_secret_sources": "profile secret sources, newer Hermes only; guarded",
    "hermes_cli.plugin_session.session_key": "session key helper, newer Hermes only; try/except in chat_line.py",
    "hermes_cli.anon_auth.parse_welcome_refusal": "Nous anon-auth parser; optional, KAME reads the refusal itself",
    "agent.tts_provider.TTSProvider": "register_tts_provider seam; speech is optional (older Hermes lacks it)",
    "agent.transcription_provider.TranscriptionProvider": "register_transcription_provider seam; optional",
    "agent.process_bootstrap.build_keepalive_http_client": "only used by the pre-1.9.0.5 native Gemini path; guarded",
    "agent.ssl_verify.resolve_httpx_verify": "only used by the pre-1.9.0.5 native Gemini path; guarded",
    "agent.gemini_native_adapter.GeminiNativeClient": "native Gemini path of older Hermes; guarded",
    "agent.gemini_native_adapter.is_native_gemini_base_url": "native Gemini path of older Hermes; guarded",
}


def collect(package: Path = PACKAGE) -> Dict[str, List[str]]:
    """``{dotted symbol: [files that import it]}`` for every Hermes import in ``package``."""
    found: Dict[str, Set[str]] = {}

    def add(sym: str, path: Path) -> None:
        found.setdefault(sym, set()).add(path.relative_to(package).as_posix())

    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            raise SystemExit(f"cannot parse {path}: {exc}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in HERMES_ROOTS:
                        add(alias.name, path)
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module or node.module.split(".")[0] not in HERMES_ROOTS:
                    continue
                for alias in node.names:
                    add(node.module if alias.name == "*" else f"{node.module}.{alias.name}", path)
    return {k: sorted(v) for k, v in sorted(found.items())}


def check(symbol: str) -> Tuple[bool, str]:
    """Import ``symbol``: a module, or ``module.attribute`` (attribute or submodule)."""
    parts = symbol.split(".")
    for cut in range(len(parts), 0, -1):
        module_name = ".".join(parts[:cut])
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
                continue                      # that prefix is not a module: try a shorter one
            return False, f"ModuleNotFoundError: {exc}"
        except Exception as exc:  # noqa: BLE001 - report, never raise
            return False, f"{type(exc).__name__}: {str(exc)[:160]}"
        obj = module
        for attr in parts[cut:]:
            if not hasattr(obj, attr):
                try:
                    obj = importlib.import_module(f"{module_name}.{attr}")
                    module_name += f".{attr}"
                    continue
                except Exception:  # noqa: BLE001
                    return False, f"{module_name} has no attribute {attr!r}"
            obj = getattr(obj, attr)
        return True, ""
    return False, "no importable module"


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", required=True, help="path of a Hermes checkout")
    ap.add_argument("--package", default=str(PACKAGE), help="plugin directory to scan")
    args = ap.parse_args(argv)
    host = Path(args.host).resolve()
    if not (host / "providers").is_dir():
        print(f"not a Hermes checkout: {host}")
        return 2
    home = tempfile.mkdtemp(prefix="kame-host-symbols-")
    os.environ["HERMES_HOME"] = home
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.path.insert(0, str(host))
    os.chdir(host)
    symbols = collect(Path(args.package))
    fails = optional = 0
    for symbol, files in symbols.items():
        ok, why = check(symbol)
        if ok:
            print(f"PASS     {symbol}")
        elif symbol in OPTIONAL:
            optional += 1
            print(f"OPTIONAL-MISSING {symbol}  ({OPTIONAL[symbol]}; {why})")
        else:
            fails += 1
            print(f"FAIL     {symbol}  <- {', '.join(files[:3])}  ({why})")
    print(f"host {host}: {len(symbols)} symbol(s), {len(symbols) - fails - optional} pass, "
          f"{optional} optional-missing, {fails} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
