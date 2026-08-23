"""The key a real Hermes turn would carry, resolved by the real Hermes.

``live_multikey.py`` proves the pool reads fifteen keys out of one variable.
``live_field.py`` proves the field they are typed into accepts them. Both were
already true when the owner of fifteen keys opened Hermes and read *API
inválida* on the first message — because neither of them is on the path that
decides what a request carries.

That path is ``hermes_cli.runtime_provider.resolve_runtime_provider``, and
what it hands back for a variable holding fifteen keys is a single "key" five
hundred characters long. This harness watches exactly that, against the
installed plugin and the host's own resolver:

* with the plugin off, one variable holding fifteen keys resolves to one
  comma-joined "key" that no provider will accept — and the pool the host
  attaches beside it is carrying that same list, so there is nothing to
  rotate *to* either;
* with it on, the same variable resolves to one key, the pool holds the other
  fourteen, and benching the one in flight moves the turn to another;
* a variable holding one key is answered identically either way;
* the two paths that never consult a pool at all — an explicitly supplied
  key, which is the shape the fallback chain uses, and a host whose pool
  wrappers declined to install — are repaired too, which is the whole reason
  this binding is separate from the pool one;
* and ``KAME_RESOLVER_DISABLED=1`` gives the host's own answer back.

Nothing here talks to a provider. Resolution is offline — it reads config,
``.env`` and ``auth.json`` — so the keys are obvious fakes and ``HERMES_HOME``
is a throwaway directory. The real profile is neither read nor written.

    "$HERMES_HOME/hermes-agent/venv/Scripts/python.exe" tools/live_resolver.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_ROOT / "hermes-agent"
INSTALLED_PLUGIN = HERMES_ROOT / "plugins" / "hermes-kame-api-rotation"

PROVIDER = "gemini"
VAR = "GOOGLE_API_KEY"
MODEL = "gemini-3.7-flash"

# Fifteen, the shape the user actually pastes. Long enough to clear the
# split's minimum-length floor, so a key dropped here would be dropped for the
# right reason rather than for being a short fake.
KEYS = [f"AIzaSyFAKE-live-resolver-key-{n:04d}" for n in range(1, 16)]
BLOB = ",".join(KEYS)

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got}")
        print(f"        want {want}")
        failures.append(label)


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def _describe(runtime: dict) -> str:
    """What a runtime carries, without carrying a key into the output."""
    key = str(runtime.get("api_key") or "")
    pool = runtime.get("credential_pool")
    return (
        f"api_key: {len(key)} chars, {key.count(',')} commas | "
        f"credential_pool: {'yes' if pool is not None else 'no'}"
    )


def main() -> int:
    if not AGENT.is_dir():
        print(f"Hermes not found at {AGENT}")
        return 2
    if not INSTALLED_PLUGIN.is_dir():
        print(f"the installed plugin is not at {INSTALLED_PLUGIN}")
        return 2

    home = Path(tempfile.mkdtemp(prefix="kame-resolver-"))
    shutil.copytree(
        INSTALLED_PLUGIN,
        home / "plugins" / "hermes-kame-api-rotation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (home / "config.yaml").write_text(
        "model:\n"
        f"  provider: {PROVIDER}\n"
        f"  default: {MODEL}\n"
        "plugins:\n"
        "  enabled:\n"
        "    - hermes-kame-api-rotation\n",
        encoding="utf-8",
    )
    # The variable, exactly as the user has it: in the process environment,
    # holding the whole list. This is the state the live gateway was measured
    # in before this release existed.
    (home / ".env").write_text(f"{VAR}={BLOB}\n", encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)
    os.environ[VAR] = BLOB
    os.environ.pop("GEMINI_API_KEY", None)
    sys.path.insert(0, str(AGENT))
    print(f"throwaway home: {home}")
    print(f"one variable, {len(KEYS)} keys, {len(BLOB)} characters")

    try:
        from hermes_cli import runtime_provider

        host_resolve = runtime_provider.resolve_runtime_provider

        print("\n[1] stock Hermes sends the whole list as one key")
        stock = host_resolve(requested=PROVIDER)
        print(f"        {_describe(stock)}")
        check("the list is the key", stock["api_key"], BLOB)
        # A pool *is* attached here, which is worse than none: it holds one
        # credential and that credential's key is the same list. Rotation has
        # somewhere to go and nowhere to go to.
        stock_pool = stock.get("credential_pool")
        check_true("a pool is attached", stock_pool is not None)
        check("holding one credential", len(stock_pool.entries()), 1)
        check(
            "whose key is the list as well",
            [e for e in stock_pool.entries() if str(e.runtime_api_key) == BLOB],
            list(stock_pool.entries()),
        )

        print("\n[2] the installed plugin loads and wraps the resolver")
        from hermes_cli import plugins as plugin_system

        plugin_system.discover_plugins(force=True)
        manager = plugin_system.get_plugin_manager()
        loaded = [key for key in manager._plugins if "kame" in key]
        check("one kame plugin", len(loaded), 1)
        if not loaded:
            return 1
        plugin_module = manager._plugins[loaded[0]].module
        binding = getattr(plugin_module, "_resolver_binding", None)
        check_true("the resolver binding is live", binding is not None and binding.installed)
        if binding is None or not binding.installed:
            print(f"        reason: {getattr(binding, 'reason', 'not installed at all')}")
            return 1
        check_true(
            "on the function every caller imports",
            getattr(runtime_provider.resolve_runtime_provider, "__kame_wrapped__", False),
        )

        print("\n[3] the same variable now resolves to one key")
        runtime = runtime_provider.resolve_runtime_provider(requested=PROVIDER)
        print(f"        {_describe(runtime)}")
        first = str(runtime["api_key"])
        check("one key, not a list", "," in first, False)
        check_true("and it is one of the fifteen", first in KEYS)
        check("everything else is the host's answer", runtime["base_url"], stock["base_url"])
        check("including the provider", runtime["provider"], stock["provider"])
        # Which layer did it, said out loud. With everything installed the
        # pool binding has already split the credential by the time the host
        # reads one out of it, so the resolver finds a single key and returns
        # it untouched. The repairs are counted, and here there are none —
        # phases 7 and 8 are where this binding is the only thing standing
        # between the user and the comma-joined list.
        check("the pool binding got there first", binding.repaired, 0)

        print("\n[4] and the pool comes with it")
        pool = runtime.get("credential_pool")
        check_true("a pool is attached", pool is not None)
        if pool is None:
            return 1
        offered, _pending = pool._available_entries()
        check("holding the fifteen keys", len(offered), len(KEYS))
        check(
            "none of them the list",
            [e for e in offered if "," in str(e.runtime_api_key)],
            [],
        )
        check_true(
            "and it knows which entry this key is",
            bool(pool.entry_id_for_api_key(first)),
        )

        print("\n[5] a 429 on that key moves the turn to another")
        # On the pool the agent is actually holding, which is what rotation
        # runs against: the entry is marked exhausted through the host's own
        # method and the pool must hand back a different key.
        entry = next(e for e in pool.entries() if str(e.runtime_api_key) == first)
        rotated = pool.mark_exhausted_and_rotate(status_code=429, credential_id=entry.id)
        second = str(getattr(rotated, "runtime_api_key", "") or "")
        check("a different key", second != first, True)
        check_true("still one of the fifteen", second in KEYS)
        check(
            "and the spent one is no longer offered",
            [e for e in pool._available_entries()[0] if str(e.runtime_api_key) == first],
            [],
        )

        print("\n[6] one key in the variable is untouched")
        before = binding.repaired
        os.environ[VAR] = KEYS[0]
        (home / ".env").write_text(f"{VAR}={KEYS[0]}\n", encoding="utf-8")
        single = runtime_provider.resolve_runtime_provider(requested=PROVIDER)
        check("the key is the key", single["api_key"], KEYS[0])
        check("and the repair did not run", binding.repaired, before)
        os.environ[VAR] = BLOB
        (home / ".env").write_text(f"{VAR}={BLOB}\n", encoding="utf-8")

        print("\n[7] a key supplied directly, which no pool is consulted for")
        # The shape ``fallback_providers`` uses: the key comes from config
        # rather than from the variable, and the host's pool branch is skipped
        # entirely. The pool binding cannot reach this one — only this binding
        # can — which is why the two are separate.
        binding.uninstall()
        check(
            "stock sends the list",
            host_resolve(requested=PROVIDER, explicit_api_key=BLOB)["api_key"],
            BLOB,
        )
        binding.install(runtime_provider)
        direct = runtime_provider.resolve_runtime_provider(
            requested=PROVIDER, explicit_api_key=BLOB
        )["api_key"]
        check("with the plugin, one key", "," in str(direct), False)
        check_true("and it is one of the fifteen", direct in KEYS)
        check("and this time the repair is what did it", binding.repaired, 1)

        print("\n[8] and on a host where the pool wrappers declined")
        # The supported degraded mode: a Hermes release moves what the pool
        # binding patches, so nothing splits. The list must still not go out.
        pool_binding = getattr(plugin_module, "_binding", None)
        check_true("the pool binding is there to remove", pool_binding is not None)
        if pool_binding is not None:
            pool_binding.uninstall()
            try:
                binding.uninstall()
                check(
                    "with neither, the list is the key",
                    runtime_provider.resolve_runtime_provider(requested=PROVIDER)["api_key"],
                    BLOB,
                )
                binding.install(runtime_provider)
                alone = str(
                    runtime_provider.resolve_runtime_provider(requested=PROVIDER)["api_key"]
                )
                check("with this one alone, still one key", "," in alone, False)
                check_true("and it is one of the fifteen", alone in KEYS)
            finally:
                from agent import credential_pool as _cp

                pool_binding.install(_cp)

        print("\n[9] the switch gives the host's own resolution back")
        binding.uninstall()
        os.environ["KAME_RESOLVER_DISABLED"] = "1"
        try:
            from importlib import import_module

            module = import_module(f"{plugin_module.__name__}.resolver_binding")
            settings = import_module(f"{plugin_module.__name__}.settings")
            check("the switch reads as on", settings.is_on(settings.RESOLVER_DISABLED), True)
            check("so nothing installs", module.install(runtime_provider), None)
        finally:
            os.environ.pop("KAME_RESOLVER_DISABLED", None)
            binding.install(runtime_provider)
        check_true(
            "and the wrapper is back",
            getattr(runtime_provider.resolve_runtime_provider, "__kame_wrapped__", False),
        )

        print()
        if failures:
            print(f"{len(failures)} FAILED: {', '.join(failures)}")
            return 1
        print("the turn carries one key:")
        print("        stock Hermes resolved a fifteen-key variable to one")
        print("        five-hundred-character 'key', beside a pool holding that")
        print("        same list. With the plugin the turn carries one of the")
        print("        fifteen, the pool holds the other fourteen, a 429 moves")
        print("        to the next — and the two paths no pool is consulted on")
        print("        are repaired by this binding alone.")
        return 0
    finally:
        os.environ.pop(VAR, None)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
