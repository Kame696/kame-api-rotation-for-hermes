r"""Deploy the plugin into every Hermes *profile*, not only the base home.

``deploy.py`` writes two places: ``plugins/<name>/`` and
``desktop-plugins/<name>/plugin.js`` under the Hermes home. That is the whole
install on a machine with one profile, and it is half of it on a machine with
several — Hermes gives each profile its own ``plugins/`` directory, and a
profile keeps running whatever copy is in *its* directory.

The failure mode this exists for is silent by construction. The base home is
upgraded, the panel in the default profile shows the new version, and a
profile created earlier goes on running the old one. Every check made from
the default profile agrees with itself. Measured on this machine before this
script existed: three copies of the plugin, all at the same fingerprint by
luck rather than by process, and two desktop halves that had already drifted
apart from each other.

Route selection is delegated to ``deploy.py`` rather than repeated, because
getting it wrong is how 1.0.8 was lost: several interpreters here run inside
an app container that redirects ``AppData\Local`` into a per-package
``LocalCache``, and a copy made from one of those lands in a private shadow
that Hermes never reads — consistently, so every check afterwards reads the
same shadow and truthfully reports the version just written there.

    python tools/deploy_profiles.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy  # noqa: E402  — same directory, and the point is to reuse its route


def profile_targets(home: Path) -> List[Tuple[str, Path, Path]]:
    """``(name, plugin_dir, desktop_js)`` for every profile that has this plugin.

    Only profiles that *already* carry a copy are written to. Creating one
    where the user never installed it would enable the plugin in a profile by
    surprise, which is a decision that belongs to them and not to a deploy
    script.
    """
    found: List[Tuple[str, Path, Path]] = []
    profiles = home / "profiles"
    if not profiles.is_dir():
        return found
    for entry in sorted(profiles.iterdir()):
        if not entry.is_dir():
            continue
        plugin_dir = entry / "plugins" / deploy.SOURCE.name
        desktop_js = entry / "desktop-plugins" / deploy.SOURCE.name / "plugin.js"
        if plugin_dir.is_dir() or desktop_js.is_file():
            found.append((entry.name, plugin_dir, desktop_js))
    return found


def copy_tree(source: Path, target: Path) -> int:
    """Overwrite in place, never wipe first.

    Same reasoning as ``deploy.py``: a running Hermes holds handles inside
    this directory — ``__pycache__`` at least — and a deploy that begins by
    deleting a directory it cannot delete leaves a half-installed plugin
    behind, which is worse than the stale one it replaced.
    """
    shutil.copytree(source, target, ignore=deploy.IGNORE, dirs_exist_ok=True)

    # A file the source no longer has is a file the previous release left
    # behind, and it is still importable. Removed one at a time so a locked
    # one is reported rather than aborting a deploy that already worked.
    # Compiled bytecode is not part of the deploy and is left alone.
    wanted = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    for path in sorted(target.rglob("*"), reverse=True):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.relative_to(target).as_posix() in wanted:
            continue
        try:
            path.unlink()
            print(f"  removed : {path.relative_to(target).as_posix()}")
        except OSError as exc:
            print(f"  stale   : {path.relative_to(target).as_posix()} ({exc})")
    return len(wanted)


def verify(source: Path, target: Path) -> List[str]:
    """Every source file, compared by digest through the route it was written.

    A count is not a verification: a copy that silently landed somewhere else
    would report the same count from the same place it wrote to.
    """
    mismatched: List[str] = []
    for path in source.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source)
        mirror = target / relative
        if not mirror.is_file() or deploy.digest(mirror) != deploy.digest(path):
            mismatched.append(str(relative))
    return mismatched


def main() -> int:
    # ``probe_redirection`` returns the empty string when the route is clean
    # and a description of the shadow when it is not. Same convention as
    # ``deploy.py``, and worth stating because reading it the other way round
    # is a deploy that refuses on every healthy machine.
    home = deploy.HERMES_HOME
    problem = deploy.probe_redirection(home)
    if problem:
        alternative = deploy.unc_view(home)
        second = (
            deploy.probe_redirection(alternative)
            if alternative
            else "no share view to try"
        )
        if second:
            print("REFUSING: every route from this process is redirected.")
            print(f"  direct: {problem}")
            print(f"  share : {second}")
            return 1
        home = alternative
        print(f"this process is redirected, so the deploy goes through: {home}")

    targets = profile_targets(home)
    if not targets:
        print("no profile carries this plugin — nothing to do")
        return 0

    version = deploy.version_of(deploy.SOURCE / "plugin.yaml")
    print(f"source  : {deploy.SOURCE}  ({version})")
    failures = 0
    for name, plugin_dir, desktop_js in targets:
        print(f"\nprofile : {name}")
        if plugin_dir.is_dir() or (plugin_dir.parent.is_dir() and plugin_dir.exists()):
            before = deploy.version_of(plugin_dir / "plugin.yaml")
            count = copy_tree(deploy.SOURCE, plugin_dir)
            mismatched = verify(deploy.SOURCE, plugin_dir)
            print(f"  plugin  : {before} -> {version}, {count} file(s)")
            if mismatched:
                failures += 1
                print(f"  MISMATCH: {len(mismatched)} file(s), first {mismatched[:3]}")
            else:
                print("  verified: every file matches the source by digest")
        if desktop_js.is_file() or desktop_js.parent.is_dir():
            desktop_js.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(deploy.DESKTOP_SOURCE, desktop_js)
            same = deploy.digest(desktop_js) == deploy.digest(deploy.DESKTOP_SOURCE)
            print(f"  panel   : {'verified' if same else 'MISMATCH'}")
            if not same:
                failures += 1

    if failures:
        print(f"\n{failures} target(s) did not verify")
        return 1
    print("\nEvery profile now runs the same build as the base home.")
    print("Restart Hermes for it to load — a deployed plugin that was never")
    print("restarted into is the same as a plugin that was never deployed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
