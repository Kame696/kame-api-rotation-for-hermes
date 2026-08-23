"""Run Hermes' OWN credential-pool suites with KAME's binding attached.

``host_corpus.py`` asks whether KAME disturbs the host's *classification*.
This asks the same question about the part that actually reaches into the
host: the binding wraps five ``CredentialPool`` methods — ``__init__``,
``_available_entries``, ``_select_unlocked``, ``_mark_exhausted`` and
``_persist`` — which is every path selection and persistence take.

The sandbox proves those wrappers do what KAME wants against a pool KAME
built. It cannot prove they leave alone the sixteen behaviours the host
tests for itself: deferred refresh, lease reselection, OAuth write-through,
quarantine locking, provider boundaries, sole-credential cooldown, rotation
bounds. Those suites were written by people who never saw this plugin, and
they fail loudly if a wrapper changed an answer.

Same shape as the corpus harness: run them clean, run them with the binding
installed, compare. Nothing is installed into the user's Hermes and
``HERMES_HOME`` is redirected to a throwaway directory.

    python tools/host_pool_suite.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
HERMES = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes/hermes-agent"))

# The host tests that assert its own ``fill_first`` default, which is the
# default this plugin exists to replace. Both build a two-key pool, select
# once, and require the *same* key to come back on the next select — stated as
# setup rather than as the property under test, but it is still what they
# assert, and a plugin that spreads load across healthy keys must fail them.
#
# Named here instead of tolerated in silence: a divergence the harness cannot
# name is a regression it has not noticed yet. Each of these must (a) fail with
# the plugin as shipped and (b) pass with KAME_SPREAD_DISABLED=1 — if it fails
# both ways it is not this feature and the harness says so; if it passes both
# ways the feature has stopped working and the harness says that too.
EXPECTED_DIVERGENCE = {
    "tests/agent/test_credential_pool_routing.py::TestApiKeyHintRealPool::test_without_hint_current_entry_is_marked",
    "tests/agent/test_credential_pool_routing.py::TestFailureAttribution::test_auth_refresh_targets_failing_key_not_pointer",
}

SUITES = (
    "tests/agent/test_credential_pool.py",
    "tests/agent/test_credential_pool_deferred_refresh.py",
    "tests/agent/test_credential_pool_key_rotation.py",
    "tests/agent/test_credential_pool_lease_refresh_reselect.py",
    "tests/agent/test_credential_pool_no_entries_log_throttle.py",
    "tests/agent/test_credential_pool_oat_authtype.py",
    "tests/agent/test_credential_pool_oauth_writethrough.py",
    "tests/agent/test_credential_pool_provider_boundary.py",
    "tests/agent/test_credential_pool_quarantine_locking.py",
    "tests/agent/test_credential_pool_routing.py",
    "tests/agent/test_credential_pool_sole_cooldown.py",
    "tests/agent/test_credential_pool_unmatched_rotation_bound.py",
    "tests/agent/test_restore_primary_pool_reselect.py",
    "tests/agent/test_auxiliary_anthropic_pool_fallback_regression.py",
)

# Installed once, before collection, on the real module — the same way the
# plugin's ``register()`` installs it inside a live Hermes.
INTERPOSER = '''
import sys
from pathlib import Path
import importlib.util

plugin_dir = Path((Path(__file__).resolve().parent / "plugin_dir.txt").read_text(encoding="utf-8").strip())

spec = importlib.util.spec_from_file_location(
    "kame_pool_probe", plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)],
)
kame = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = kame
spec.loader.exec_module(kame)

pool_binding = importlib.import_module("kame_pool_probe.pool_binding")
store_module = importlib.import_module("kame_pool_probe.store")

from agent import credential_pool as cp


class _MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


_binding = pool_binding.PoolBinding(
    store_module.LedgerStore(_MemoryState(), ttl_seconds=0.0)
)
if not _binding.install(cp):
    print("KAME FAILED TO INSTALL", file=sys.stderr)

import os

if os.environ.get("KAME_POOL_SABOTAGE") == "1":
    # Negative control. A comparison that always says "no difference" is
    # worthless unless a real difference would show up, so this hides one
    # credential from selection - exactly the kind of damage a careless
    # wrapper does - and the harness checks that the host notices.
    _wrapped = cp.CredentialPool._available_entries

    def _sabotaged(pool, *, clear_expired=False, refresh=False):
        entries = _wrapped(pool, clear_expired=clear_expired, refresh=refresh)
        return list(entries)[:-1] if len(entries) > 1 else entries

    cp.CredentialPool._available_entries = _sabotaged
'''


def run(
    *,
    with_kame: bool,
    workdir: Path,
    sabotage: bool = False,
    spread: bool = True,
) -> tuple[int, str]:
    argv = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    argv.extend(SUITES)
    if with_kame:
        argv.extend(["-p", "kame_pool_interpose"])

    env = dict(os.environ)
    env["HERMES_HOME"] = str(workdir / "home")
    env.pop("KAME_POOL_SABOTAGE", None)
    env.pop("KAME_SPREAD_DISABLED", None)
    if sabotage:
        env["KAME_POOL_SABOTAGE"] = "1"
    if not spread:
        env["KAME_SPREAD_DISABLED"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(HERMES), str(workdir)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.run(
        argv, cwd=str(HERMES), env=env, capture_output=True, text=True, timeout=900
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def failed_tests(output: str) -> set[str]:
    names = set()
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
    missing = [name for name in SUITES if not (HERMES / name).is_file()]
    if missing:
        print("suite files missing:")
        for name in missing:
            print(f"        {name}")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="kame-pool-"))
    (workdir / "home").mkdir()
    (workdir / "kame_pool_interpose.py").write_text(INTERPOSER, encoding="utf-8")
    (workdir / "plugin_dir.txt").write_text(str(PLUGIN_DIR), encoding="utf-8")

    print(f"[1] the host's own pool suites ({len(SUITES)} files) on their own")
    base_rc, base_out = run(with_kame=False, workdir=workdir)
    print(f"        {summary_line(base_out)}")

    print("\n[2] the same suites with KAME's binding on the real CredentialPool")
    kame_rc, kame_out = run(with_kame=True, workdir=workdir)
    print(f"        {summary_line(kame_out)}")

    if "KAME FAILED TO INSTALL" in kame_out:
        print("\nthe binding did not install — the comparison proves nothing")
        return 1

    base_failures = failed_tests(base_out)
    caused = sorted(failed_tests(kame_out) - base_failures)
    print()
    if base_failures:
        # Said out loud rather than subtracted in silence: a reader who sees
        # "changed nothing" must not take it to mean "everything passed".
        # These fail identically with and without the plugin, and the
        # comparison is only about the difference.
        print(f"{len(base_failures)} test(s) already fail without KAME, and still do with it:")
        for name in sorted(base_failures):
            print(f"        {name}")
        print("        (this environment, not the plugin - the comparison is the difference)")
        print()
    unexplained = [name for name in caused if name not in EXPECTED_DIVERGENCE]
    if unexplained:
        print(f"KAME changed {len(unexplained)} of the host's own pool behaviours:")
        for name in unexplained:
            print(f"        {name}")
        print(kame_out[-5000:])
        return 1

    if EXPECTED_DIVERGENCE:
        print("[2b] the same suites again with load spreading switched off")
        _, flat_rc_out = run(with_kame=True, workdir=workdir, spread=False)
        print(f"        {summary_line(flat_rc_out)}")
        flat_caused = failed_tests(flat_rc_out) - base_failures
        print()

        stale = sorted(EXPECTED_DIVERGENCE - set(caused))
        if stale:
            # Not a passing condition. These exist to fail; if they stopped,
            # the plugin has stopped re-ordering anything and nobody noticed.
            print(f"{len(stale)} expected divergence(s) did not happen:")
            for name in stale:
                print(f"        {name}")
            print("        load spreading is no longer changing the host's order.")
            return 1

        still = sorted(set(caused) & flat_caused)
        if still:
            print(f"{len(still)} divergence(s) survive with spreading switched off:")
            for name in still:
                print(f"        {name}")
            print("        those are not this feature. Something else changed.")
            print(flat_rc_out[-5000:])
            return 1

        print(f"{len(caused)} of the host's tests answer differently, all of them")
        print("        assertions about its fill_first default, and all of them")
        print("        pass again with KAME_SPREAD_DISABLED=1:")
        for name in sorted(caused):
            print(f"        {name}")
        print()

    if base_rc != 0 and not failed_tests(base_out):
        print("the host's own suites did not run cleanly; the comparison proves nothing")
        print(base_out[-2000:])
        return 1

    # A silent harness and a harmless plugin print the same thing, so break
    # the binding on purpose and make sure the difference shows up.
    print("[3] the same suites with one credential deliberately hidden")
    _, bad_out = run(with_kame=True, workdir=workdir, sabotage=True)
    print(f"        {summary_line(bad_out)}")
    caught = sorted(failed_tests(bad_out) - base_failures - EXPECTED_DIVERGENCE)
    print()
    if not caught:
        print("a broken binding passed these suites - they cannot prove the")
        print("        working one is harmless either. Fix the harness first.")
        return 1
    print(f"broken on purpose, the host caught it in {len(caught)} test(s):")
    for name in caught[:3]:
        print(f"        {name}")
    if len(caught) > 3:
        print(f"        ... and {len(caught) - 3} more")
    print()

    print("apart from the named fill_first assertions, KAME changed nothing")
    print("        the host tests about its own pool: persistence, refresh,")
    print("        quarantine, routing, provider boundaries, sole-credential")
    print("        cooldown and rotation bounds answer exactly as they do")
    print("        without the plugin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
