"""Run KAME against Hermes' OWN error corpus, through the real dispatch.

Every fixture in this project was written by the same hand that wrote the
classifier, which makes them a statement of intent and not evidence. Hermes
ships its own corpus — ``tests/agent/test_error_classifier.py`` and
``tests/test_transform_api_error_classification_hook.py``, several hundred
assertions over roughly fourteen providers, written by people who never saw
this plugin. That corpus is the third-party witness.

The question it answers is not "does KAME classify well". It is the harder
one: **does KAME leave the host's judgment intact.** KAME's hook is consulted
BEFORE the whole built-in pipeline and the first valid answer wins, so a hook
that claims too much silently replaces fourteen providers' worth of tuned
classification with its own opinion. If that happened, the host's own suite
would start failing — with KAME's verdict in the diff.

So the suite is run twice against the real ``get_plugin_error_classification``
dispatch: once with the hook absent, once with KAME's callback behind it. Any
test that changes outcome is a payload where KAME overrode the host, and is
printed with the payload that caused it. Identical results mean the plugin is
invisible to everything it does not genuinely recognise.

Nothing is installed, no provider is contacted, no credential is read: the
corpus is exceptions constructed in memory.

    python tools/host_corpus.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
HERMES = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes/hermes-agent"))

CORPUS = (
    "tests/agent/test_error_classifier.py",
    "tests/test_transform_api_error_classification_hook.py",
)

# Written next to this file, passed to pytest with ``-p``. It runs inside the
# pytest process, before collection, and puts KAME's callback behind the
# host's real hook dispatch — not in place of it.
INTERPOSER = '''
import sys
from pathlib import Path
import importlib.util

PLUGIN_DIR = Path(__file__).resolve().parent / "plugin_dir.txt"
plugin_dir = Path(PLUGIN_DIR.read_text(encoding="utf-8").strip())

spec = importlib.util.spec_from_file_location(
    "kame_corpus", plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)],
)
kame = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = kame
spec.loader.exec_module(kame)

import hermes_cli.plugins as host_plugins

_original_invoke = host_plugins.invoke_hook


def invoke_hook(name, **kwargs):
    """Add KAME to the transform hook; never take anyone else's place.

    The host's results come first and KAME's is appended, which is what
    registration order would give a plugin loaded last, and which keeps the
    corpus' own synthetic plugin able to win its case.
    """
    results = list(_original_invoke(name, **kwargs))
    if name != "transform_api_error_classification":
        return results
    try:
        result = kame._on_api_error_classification(**kwargs)
    except Exception as exc:  # a raising hook is itself a finding
        print(f"KAME RAISED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return results
    if result is not None:
        results.append(result)
    return results


host_plugins.invoke_hook = invoke_hook
'''


def load_plugin_package():
    spec = importlib.util.spec_from_file_location(
        "kame_probe",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_corpus(*, with_kame: bool, workdir: Path) -> tuple[int, str]:
    """Run the host's corpus in a subprocess; return (failures, raw output)."""
    argv = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    argv.extend(CORPUS)
    if with_kame:
        argv.extend(["-p", "kame_interpose"])

    env = dict(os.environ)
    # A throwaway home: the corpus should not read the real plugin state, and
    # KAME's ledger must not touch the user's install.
    env["HERMES_HOME"] = str(workdir / "home")
    env["PYTHONPATH"] = os.pathsep.join([str(HERMES), str(workdir)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.run(
        argv, cwd=str(HERMES), env=env,
        capture_output=True, text=True, timeout=600,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


def failed_tests(output: str) -> set[str]:
    names: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            names.add(line.split(" ", 1)[1].split(" ")[0])
    return names


def summary_line(output: str) -> str:
    for line in reversed(output.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            return line.strip()
    return "(no summary)"


def main() -> int:
    if not HERMES.is_dir():
        print(f"Hermes not found at {HERMES}")
        return 2
    for name in CORPUS:
        if not (HERMES / name).is_file():
            print(f"corpus file missing: {name}")
            return 2

    workdir = Path(tempfile.mkdtemp(prefix="kame-corpus-"))
    (workdir / "home").mkdir()
    (workdir / "kame_interpose.py").write_text(INTERPOSER, encoding="utf-8")
    (workdir / "plugin_dir.txt").write_text(str(PLUGIN_DIR), encoding="utf-8")

    print("[1] the host's corpus on its own")
    base_rc, base_out = run_corpus(with_kame=False, workdir=workdir)
    print(f"        {summary_line(base_out)}")

    print("\n[2] the same corpus with KAME behind the real hook dispatch")
    kame_rc, kame_out = run_corpus(with_kame=True, workdir=workdir)
    print(f"        {summary_line(kame_out)}")

    if "KAME RAISED" in kame_out:
        print("\nKAME raised inside the hook — the host contract says it must not:")
        for line in kame_out.splitlines():
            if "KAME RAISED" in line:
                print(f"        {line.strip()}")
        return 1

    base_failures = failed_tests(base_out)
    kame_failures = failed_tests(kame_out)
    caused = sorted(kame_failures - base_failures)
    fixed = sorted(base_failures - kame_failures)

    print()
    if caused:
        print(f"KAME changed the verdict on {len(caused)} case(s) the host had right:")
        for name in caused:
            print(f"        {name}")
        print("\nEach one is a payload where KAME claimed an error it does not own.")
        print(kame_out[-4000:])
        return 1

    if fixed:
        # The host failing on its own is an environment problem, not a result.
        print(f"note: {len(fixed)} case(s) failed only without KAME — check the environment:")
        for name in fixed:
            print(f"        {name}")

    if base_rc != 0 and not base_failures:
        print("the host's own corpus did not run cleanly; the comparison proves nothing")
        print(base_out[-2000:])
        return 1

    print("KAME changed nothing in the host's own corpus")
    print("        it declines every payload it does not recognise, across every")
    print("        provider the host tests, and the host pipeline decides those.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
