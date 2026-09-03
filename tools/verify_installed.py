"""Confirm the installed plugin actually loads and binds, without an API call.

``hermes plugins doctor`` proves the manifest parses and ``register()`` runs
against a stand-in context. It does not prove that, inside a real Hermes
process with the real plugin manager and the real config, the bindings
attached to the real ``CredentialPool``.

This does. It drives Hermes' own discovery over the live plugin directory and
then asks the credential pool module whether its two methods are the ones
KAME installed. No provider is contacted, no credential is used, no quota is
spent, and nothing is written to the pool.

Run it from anywhere:

    python tools/verify_installed.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_HOME / "hermes-agent"

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")
        failures.append(label)


def main() -> int:
    if not AGENT.is_dir():
        print(f"Hermes not found at {AGENT}")
        return 2
    sys.path.insert(0, str(AGENT))

    from hermes_cli import plugins as plugin_system

    print("[1] discovery finds the installed plugin")
    plugin_system.discover_plugins(force=True)
    manager = plugin_system.get_plugin_manager()
    loaded = {key: value for key, value in manager._plugins.items() if "kame" in key}
    check("one kame plugin discovered", len(loaded), 1)
    if not loaded:
        return 1
    key, entry = next(iter(loaded.items()))
    print(f"        {key} {entry.manifest.version}")
    check("enabled", bool(entry.enabled), True)

    print("\n[2] every hook is registered with the live manager")
    for hook in (
        "transform_api_error_classification",
        # 1.0.9. A reset forgets the storm filter and the status line, which
        # describe a conversation that has just stopped existing; cooldowns are
        # deliberately left alone.
        "on_session_reset",
        "pre_api_request",
        "post_api_request",
    ):
        check(hook, plugin_system.has_hook(hook), True)

    print("\n[3] the credential pool carries KAME's wrappers")
    from agent import credential_pool as cp

    for method in (
        "_mark_exhausted",
        "_available_entries",
        "_select_unlocked",
        # Since v0.1.8. `_persist` is the guard that keeps a derived key off
        # disk, and splitting is switched on by it — an unwrapped `_persist`
        # means a comma-separated credential is still one malformed key.
        "_persist",
        "__init__",
        # Since 1.6.0.0. `_available_entries` and `_select_unlocked` have
        # always excluded the row that holds several keys; `current()` never
        # went through either, and it is what the pointer restored from
        # auth.json resolves through — so an unwrapped `current` means the
        # comma-joined list can still be sent to the provider as one key.
        "current",
    ):
        wrapped = getattr(getattr(cp.CredentialPool, method), "__kame_wrapped__", False)
        check(f"CredentialPool.{method}", wrapped, True)

    print("\n[4] the auxiliary relays carry them too")
    from agent import auxiliary_client as aux

    for relay in (
        "_relay_sync_completion",
        "_relay_sync_stream",
        "_relay_async_completion",
    ):
        wrapped = getattr(getattr(aux, relay), "__kame_wrapped__", False)
        check(f"auxiliary_client.{relay}", wrapped, True)

    print("\n[4b] every model call goes through KAME's carousel")
    # The one binding that changes what a *healthy* call does. Both names are
    # checked because ``run_agent`` picks between them per call (streaming when
    # there is a consumer, plain otherwise), so wrapping one and missing the
    # other would rotate on some turns and not others — the hardest kind of
    # bug to see from the outside.
    from agent import chat_completion_helpers as cch

    for dispatch in ("interruptible_streaming_api_call", "interruptible_api_call"):
        wrapped = getattr(getattr(cch, dispatch), "__kame_carousel__", False)
        check(f"chat_completion_helpers.{dispatch}", wrapped, True)

    print("\n[5] all three slash commands resolved")
    commands = getattr(manager, "_plugin_commands", None) or {}
    # /kame joined the other two in 1.0.9. Each is registered under its own
    # try/except so a failure there cannot cost the rotation hook, which is
    # exactly the guard that would let one of them vanish quietly.
    for command in ("kame-keys", "kame-quota", "kame"):
        check(command, command in commands, True)

    print("\n[6] the ledger and the journal have a real home under this profile")
    # The manager builds a context per plugin and does not keep a reference to
    # it, so this builds one the same way it does — the real class, the real
    # manifest, the real manager. Skipping the phase because the manager did
    # not hand one over is how a check that proves nothing looks.
    live_context = plugin_system.PluginContext(entry.manifest, manager)
    home = getattr(getattr(live_context, "state", None), "data_dir", None)
    check("the plugin has a state directory", home is not None, True)
    if home is not None:
        print(f"        {home}")
        check("that exists on disk", Path(home).is_dir(), True)

    print("\n[7] both switches are readable from where Hermes keeps switches")
    # v0.3.2. The plugin declares `disabled` and `spread_disabled` in its
    # manifest and reads them through the real context at registration. Same
    # live context as phase 6, and that is what makes these answers the host's
    # rather than a stand-in's: `_plugin_relative_segments` rejects reserved
    # roots and dotted paths outright, so a name the plugin reads is worth
    # nothing until the host has been asked whether it will serve it.
    plugin_settings = None
    module_name = getattr(getattr(entry, "module", None), "__name__", "")
    if module_name:
        import importlib

        try:
            plugin_settings = importlib.import_module(f"{module_name}.settings")
        except Exception as exc:
            print(f"        could not import the settings module: {exc}")
    check("the settings module is importable", plugin_settings is not None, True)
    if plugin_settings is not None:
        names = (plugin_settings.ROTATION_DISABLED, plugin_settings.SPREAD_DISABLED)
        for name in names:
            try:
                live_context.get_config(name, None)
                answered = True
            except Exception as exc:
                print(f"        {name}: {exc}")
                answered = False
            check(f"the host answers for {name!r}", answered, True)
        # Decision 42: the same call with a name the host refuses has to be
        # refused, or this phase would pass on a context that answers anything.
        try:
            live_context.get_config("plugins.entries", None)
            refused = False
        except Exception:
            refused = True
        check("and refuses a name it should refuse", refused, True)
        # And KAME is not switched off in this profile, which is what every
        # phase above has been silently assuming.
        check(
            "and KAME is not switched off here",
            plugin_settings.is_on(plugin_settings.ROTATION_DISABLED),
            False,
        )

    print(chr(10) + "[8] /kame doctor answers from the installed copy")
    # 1.6.0.1. The diagnostic moved into the plugin precisely so it would
    # survive a reinstall, and a diagnostic nobody has ever run against a real
    # install is exactly the thing this project has been bitten by before.
    # This calls it through the installed module and reads what comes back.
    if module_name:
        import importlib

        try:
            menu = importlib.import_module(f"{module_name}.menu")
            report = menu.MenuCommand(None).handle("doctor")
        except Exception as exc:
            print(f"        {type(exc).__name__}: {exc}")
            report = ""
        check("it returned a report", bool(report), True)
        check("it did not report its own failure", "/kame doctor failed" in report, False)
        for heading in (
            "Which KAME is running",
            "What it can see",
            "What each error costs a key",
            "Worth a person's time",
        ):
            check(f"it has a section: {heading}", heading in report, True)
        check("it names the running build", "1.6.0" in report, True)
        # The rule every screen in this plugin follows. `key:` prefixes a
        # fingerprint; a raw key never appears, so neither does any string
        # long enough to be one.
        longest = max((len(word) for word in report.split()), default=0)
        check("nothing in it is long enough to be a key", longest < 40, True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("the installed plugin is live and bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
