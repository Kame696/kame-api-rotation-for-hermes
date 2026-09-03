r"""Every copy of this plugin on the machine, and whether they are the same build.

A version number is a claim written by whoever last edited the manifest. A
fingerprint is a digest of the modules that actually loaded, so it is the only
thing that can tell a deploy that landed from a deploy that was written
somewhere else and verified there.

Two failures this exists for, both measured on this machine:

* **1.0.8 was lost** to an interpreter running inside an app container that
  redirects ``AppData\Local`` into a per-package ``LocalCache``. The copy
  landed in a private shadow, and every check afterwards read the same shadow
  and truthfully reported the version just written there. Route selection is
  delegated to ``deploy.py`` rather than repeated here, for that reason.
* **Profiles drift silently.** Hermes gives each profile its own ``plugins/``
  directory and a profile runs whatever is in *its* directory. The base home
  upgrades, the default profile's panel shows the new version, and a profile
  made earlier goes on running the old one — with every check made from the
  default profile agreeing with itself.

Reads only. Nothing is written, no provider is contacted, no credential is
touched.

    python tools/fingerprints.py
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy  # noqa: E402  — and the point is to reuse its route selection


def copies(home: Path) -> List[Tuple[str, Path]]:
    found: List[Tuple[str, Path]] = [
        ("source", deploy.SOURCE),
        ("installed", home / "plugins" / deploy.SOURCE.name),
    ]
    profiles = home / "profiles"
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            candidate = entry / "plugins" / deploy.SOURCE.name
            if candidate.is_dir():
                found.append((f"profile {entry.name}", candidate))
    return [(label, path) for label, path in found if path.is_dir()]


def read(label: str, root: Path, index: int) -> Tuple[str, str, str]:
    """``(version, fingerprint, note)`` for one copy, loaded on its own."""
    name = f"kame_fingerprint_{index}"
    try:
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        integrity = importlib.import_module(f"{name}.integrity")
        result = integrity.verify()
    except Exception as exc:
        return "?", "?", f"could not load: {type(exc).__name__}: {exc}"
    version = deploy.version_of(root / "plugin.yaml")
    note = ""
    if not result.get("complete"):
        missing = result.get("missing_required") or []
        note = f"INCOMPLETE — {len(missing)} required module(s) missing"
    return version, str(result.get("fingerprint") or "?"), note


def main() -> int:
    home = deploy.HERMES_HOME
    if deploy.probe_redirection(home):
        alternative = deploy.unc_view(home)
        if not alternative or deploy.probe_redirection(alternative):
            print("REFUSING: every route from this process is redirected;")
            print("anything read here would be a private shadow of the real install.")
            return 2
        home = alternative
        print(f"this process is redirected, so the read goes through: {home}\n")

    found = copies(home)
    if not found:
        print("no copy of the plugin found")
        return 2

    rows = []
    for index, (label, root) in enumerate(found):
        version, mark, note = read(label, root, index)
        rows.append((label, version, mark, note))
        line = f"  {label:<16} {version:<10} {mark}"
        if note:
            line += f"  ← {note}"
        print(line)
        print(f"                   {root}")

    marks = {mark for _label, _version, mark, _note in rows}
    print()
    if any(note for *_rest, note in rows):
        print("At least one copy is incomplete. An incomplete copy registers,")
        print("reports itself active, and does less than it says.")
        return 1
    if len(marks) == 1:
        print("Every copy is the same build.")
        print("Hermes loads plugins at boot, so the copy that is *running* is")
        print("whichever one was there at the last restart. The panel header")
        print("and /kame both print the fingerprint of the running one.")
        return 0
    print("THESE ARE NOT THE SAME BUILD.")
    print("Run tools/deploy.py and tools/deploy_profiles.py, then restart Hermes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
