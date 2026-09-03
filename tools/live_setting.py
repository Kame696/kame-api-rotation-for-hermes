"""Every setting the plugin reads, asked of the host that has to serve it.

A setting in this plugin has to survive four separate places agreeing about
it: the manifest declares it, Hermes' own config layer agrees to answer for
the name, ``/kame set`` writes it somewhere the next read will find, and the
panel groups it. Three of those can be right while the fourth is decorative,
and the failure is invisible until the owner types the command — which is the
worst moment to discover that a switch does nothing.

Hermes serves a plugin only the names under ``plugins.entries.<id>.settings``
and refuses everything else outright, so a name the plugin reads is worth
nothing until the host has been asked whether it will serve it. That question
is asked here for **every** declared setting rather than for the newest one,
because the way this breaks is a setting added later and wired to three of the
four places.

No provider is contacted and no credential is used. The real plugin manager
and the real config layer run against a throwaway home holding a copy of the
*installed* plugin, so the artifact under test is the deployed one and nothing
is written to the real profile.

    python tools/live_setting.py
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_ROOT / "hermes-agent"
INSTALLED_PLUGIN = HERMES_ROOT / "plugins" / "hermes-kame-api-rotation"

failures: list = []


def check(label, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")
        failures.append(label)


def check_true(label, value) -> None:
    check(label, bool(value), True)


def main() -> int:
    if not AGENT.is_dir():
        print(f"Hermes not found at {AGENT}")
        return 2
    if not INSTALLED_PLUGIN.is_dir():
        print(f"the installed plugin is not at {INSTALLED_PLUGIN}")
        return 2

    home = Path(tempfile.mkdtemp(prefix="kame-setting-"))
    shutil.copytree(
        INSTALLED_PLUGIN,
        home / "plugins" / "hermes-kame-api-rotation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # Plugins are opt-in and the allow-list lives in the home's own
    # config.yaml. Nothing else is taken from the real profile.
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-kame-api-rotation\n", encoding="utf-8"
    )
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(AGENT))
    print(f"throwaway home: {home}")

    from hermes_cli import plugins as plugin_system

    plugin_system.discover_plugins(force=True)
    manager = plugin_system.get_plugin_manager()
    loaded = [key for key in manager._plugins if "kame" in key]
    print("\n[1] the installed plugin loads under the real manager")
    check("one kame plugin", len(loaded), 1)
    if not loaded:
        return 1
    entry = manager._plugins[loaded[0]]
    plugin = entry.module
    print(f"        {entry.manifest.version}")
    settings = importlib.import_module(plugin.__name__ + ".settings")

    commands = getattr(manager, "_plugin_commands", None) or {}
    handler = commands.get("kame")
    handler = (
        handler.get("handler")
        if isinstance(handler, dict)
        else getattr(handler, "handler", handler)
    )
    check_true("the /kame command resolved", callable(handler))
    if not callable(handler):
        return 1

    names = list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS)
    print(f"\n[2] the host answers for every one of the {len(names)} declared settings")
    # The real context, built the way the manager builds it. A stand-in would
    # answer for anything, which is the one thing this phase must not do.
    context = plugin_system.PluginContext(entry.manifest, manager)
    for name in names:
        try:
            context.get_config(name, None)
            answered = True
        except Exception as exc:
            print(f"        {name}: {exc}")
            answered = False
        check(name, answered, True)

    print("\n[3] and refuses a name it should refuse")
    # Without this the phase above would pass on a context that answers
    # anything, which would prove nothing at all.
    try:
        context.get_config("plugins.entries", None)
        refused = False
    except Exception:
        refused = True
    check("a reserved root is still refused", refused, True)

    print("\n[4] every setting is on exactly one shelf, and explains itself")
    shelves = set()
    for name in names:
        group = settings.group_of(name)
        shelves.add(group)
        check_true(f"{name} has a shelf ({group})", bool(group))
        check_true(f"{name} has help text", len(settings.explain(name).strip()) > 20)
    print(f"        shelves in use: {', '.join(sorted(shelves))}")

    print("\n[5] a switch survives set, read, and reset")
    # Driven through the command the owner would actually type, not through
    # the settings module: writing the value is the half that has to reach a
    # file the next read finds.
    switch = settings.NO_MODEL_FALLBACK
    settings.forget()
    check(f"{switch} starts off", settings.is_on(switch), False)
    handler(f"set {switch} true")
    settings.forget()
    check("on after /kame set", settings.is_on(switch), True)
    handler(f"reset {switch}")
    settings.forget()
    check("off again after /kame reset", settings.is_on(switch), False)

    print("\n[6] so does a number")
    knob = settings.STREAM_RESUME_LIMIT
    default = settings.ALL_NUMBERS[knob]
    # Downwards, because since 1.6.0.0 this setting's default *is* its
    # ceiling: it bounds a rule that normally stops for a better reason,
    # so there is nothing above it left to ask for.
    wanted = default - 2
    settings.forget()
    check(f"{knob} starts at its default", settings.number(knob, default), default)
    handler(f"set {knob} {wanted}")
    settings.forget()
    check("changed after /kame set", settings.number(knob, default), wanted)
    handler(f"reset {knob}")
    settings.forget()
    check("back to the default", settings.number(knob, default), default)

    print("\n[6b] and a value outside the range changes nothing")
    # A setter that accepted anything and clamped in silence would leave
    # the owner with a number on screen that is not the number in force.
    refusal = str(handler(f"set {knob} {default + 2}"))
    settings.forget()
    check("still the default", settings.number(knob, default), default)
    check_true("and it says why", "outside" in refusal or "accepts" in refusal)

    print("\n[7] and all of them are listed where the owner would look")
    listing = str(handler("get"))
    for name in names:
        check(f"{name} in /kame get", name in listing, True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures[:5])}")
        return 1
    print("every declared setting is real: the host serves it, the command")
    print("writes it, the plugin reads it back, and the panel has a shelf for it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
