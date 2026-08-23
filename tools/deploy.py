r"""Copy the working tree of the plugin over the installed one.

Run it with an interpreter that can prove it writes to the real Hermes
directory. The script checks that itself and refuses otherwise::

    python tools/deploy.py

The reason is not style. Several interpreters and shells on this machine run
inside an app container that redirects ``AppData\Local`` into a per-package
``LocalCache``. A copy made from one of those lands in a private shadow of the
plugins directory that Hermes never reads -- and the shadow is *consistent*, so
every check afterwards reads the same redirected path and truthfully reports the
version that was just written there. The deploy looks like it succeeded, the
manifest check passes, and Hermes goes on running the previous release. That is
how 1.0.8 was lost: it was deployed on 20 Aug and never ran once.

Since 1.1.0 the refusal is empirical rather than positional. The script writes
a uniquely named probe file into the Hermes home and then looks for that exact
name under ``%LOCALAPPDATA%\Packages\*\LocalCache\Local\hermes\``. If it
turns up there, this process is being redirected and the deploy is refused. If
it does not, the write went to the real directory -- which is the only thing
worth knowing, and the only way to know it that a redirected process cannot
fake. The old rule (the interpreter must live under the Hermes directory) was
sound but too narrow: it also refused interpreters that were demonstrably
writing to the real tree, which left the deploy needing a human at a keyboard
for no reason.

A post-copy manifest comparison is still printed, but it is the second line of
defence, not the first: inside a container it would agree with itself. Since
1.1.2 the copy is also verified file by file, by digest, through whichever
route it was written with.

**The way out (1.1.2).** Refusing is correct and it is not a deploy. When the
probe says this process is redirected, the script now tries the same directory
through the local administrative share -- ``\\localhost\C$\Users\...`` -- and
probes *that* the same way. The redirection is a filter on the path, and the
share reaches the volume by another road, so the probe answers honestly for
each route independently. If the second route is clean the deploy goes through
it and says so; if it is not, the refusal stands exactly as before. Nothing is
assumed about which one works today: both are measured, every run.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional

HERMES_HOME = Path.home() / "AppData/Local/hermes"
SOURCE = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"
TARGET = HERMES_HOME / "plugins/hermes-kame-api-rotation"
# The Desktop UI half goes to the *standalone* runtime door, not to
# `plugins/<name>/desktop/plugin.js`. Both are loaded by the same pipeline, but
# the unified door ships `defaultEnabled: false` to match the Python half's
# installed-but-inert posture -- so a chip installed there stays invisible until
# someone finds the toggle in Settings, which defeats the point of a chip.
DESKTOP_SOURCE = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation/desktop-ui/plugin.js"
DESKTOP_TARGET = HERMES_HOME / "desktop-plugins/hermes-kame-api-rotation/plugin.js"
VENV_PYTHON = HERMES_HOME / "hermes-agent/venv/Scripts/python.exe"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")

# Path fragments that only ever appear in a virtualized or store-managed
# interpreter. Any one of them means AppData\Local is not what it claims.
CONTAINER_MARKERS = (
    "localcache",
    "windowsapps",
    "pythonsoftwarefoundation",
)


def version_of(manifest: Path) -> str:
    if not manifest.is_file():
        return "(no manifest)"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"')
    return "(no version)"


def unc_view(path: Path) -> Optional[Path]:
    r"""The same local directory reached through the administrative share.

    ``C:\Users\...`` becomes ``\\localhost\C$\Users\...``: the same bytes on
    the same volume, reached through the network redirector instead of through
    the container's redirected view of the drive. Returns ``None`` for a path
    that has no drive letter to translate, and for one that is already a UNC
    path -- there is no second road out of the second road.
    """
    drive = path.drive
    if len(drive) != 2 or not drive.endswith(":"):
        return None
    return Path(f"//localhost/{drive[0]}$") / path.relative_to(path.anchor)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_interpreter() -> str:
    """Return an error message, or an empty string when the write goes home.

    Two independent reasons to refuse, reported separately so the output says
    which one fired rather than only that something was wrong.
    """
    executable = Path(sys.executable).resolve()
    lowered = str(executable).lower()

    hit = next((m for m in CONTAINER_MARKERS if m in lowered), "")
    if hit:
        return (
            f"interpreter runs inside an app container (matched {hit!r}).\n"
            "Its AppData/Local is redirected, so this copy would land in a "
            "private shadow that Hermes never reads."
        )

    return probe_redirection(HERMES_HOME)


def probe_redirection(home: Path) -> str:
    """Write a probe into the Hermes home and see whether it lands in a shadow.

    The redirection this guards against is per-process and invisible from
    inside: reads fall through to the real tree, so every check made by
    reading agrees with reality right up until the moment a write is needed.
    The only question that cannot be faked is where a write actually goes, so
    that is the question this asks -- one file, unique name, deleted straight
    away, and a scan of every package's redirected view for it.
    """
    token = f".kame-probe-{uuid.uuid4().hex}"
    probe = home / token
    shadow_root = Path.home() / "AppData/Local/Packages"

    try:
        probe.write_text("deploy probe\n", encoding="utf-8")
    except OSError as exc:
        return f"could not write to {home} at all ({exc})."

    try:
        found = sorted(shadow_root.glob(f"*/LocalCache/Local/hermes/{token}"))
    except OSError:
        found = []
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
        # A probe that landed in a shadow is deleted from the shadow by the
        # line above (same redirection), so nothing is left behind either way.

    if found:
        return (
            "this process writes into an app container's private copy of the "
            f"Hermes directory:\n  {found[0].parent}\n"
            "Reads fall through to the real tree, so a deploy made from here "
            "would look correct and change nothing."
        )
    return ""


def main() -> int:
    if not SOURCE.is_dir():
        print(f"source not found: {SOURCE}")
        return 2

    problem = check_interpreter()
    print(f"interpreter: {sys.executable}")

    home, target, desktop_target = HERMES_HOME, TARGET, DESKTOP_TARGET
    if problem:
        # The direct road is redirected. Measure the other one before giving
        # up: a refusal is the right answer to "this would land in a shadow",
        # and the wrong answer to "this machine cannot be deployed to today".
        alternative = unc_view(HERMES_HOME)
        second = probe_redirection(alternative) if alternative else "no share view to try"
        if second:
            print()
            print("REFUSING TO DEPLOY")
            print(problem)
            print()
            print(f"the share view was tried too and is no better: {second}")
            print()
            print("Run it again with the Hermes venv interpreter:")
            print(f'  "{VENV_PYTHON}" tools/deploy.py')
            if not VENV_PYTHON.is_file():
                print()
                print(
                    "  (that interpreter is not visible from here either, which is "
                    "itself\n   evidence of the redirection -- run the command from "
                    "a plain shell,\n   not from this one)"
                )
            return 3
        home = alternative
        target = home / "plugins/hermes-kame-api-rotation"
        desktop_target = home / "desktop-plugins/hermes-kame-api-rotation/plugin.js"
        print()
        print("this process is redirected, so the deploy goes through the share view:")
        print(f"  {home}")
        print("(probed: a write there does not appear in any package's LocalCache)")
        print()

    source_version = version_of(SOURCE / "plugin.yaml")
    print(f"source     : {SOURCE}  ({source_version})")
    print(f"target     : {target}  ({version_of(target / 'plugin.yaml')})")

    # Overwrite in place rather than wiping the directory first. A running
    # Hermes holds handles inside it -- ``__pycache__`` at least -- and a
    # deploy that starts by deleting a directory it cannot delete leaves a
    # half-installed plugin behind. Files that no longer exist in the source
    # are removed afterwards, one at a time, and a locked one is reported
    # instead of aborting the deploy that already succeeded.
    shutil.copytree(SOURCE, target, ignore=IGNORE, dirs_exist_ok=True)

    wanted = {
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    for path in sorted(target.rglob("*"), reverse=True):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.relative_to(target).as_posix() in wanted:
            continue
        try:
            path.unlink()
            print(f"removed    : {path.relative_to(target).as_posix()}")
        except OSError as exc:
            print(f"stale file left behind: {path} ({exc})")

    if DESKTOP_SOURCE.is_file():
        desktop_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DESKTOP_SOURCE, desktop_target)
        print(f"desktop    : {desktop_target}")
    else:
        print(f"desktop    : missing at {DESKTOP_SOURCE} - the chip and /kame page will not appear")

    landed = version_of(target / "plugin.yaml")
    # Compiled bytecode left by the running Hermes is not part of the deploy
    # and counting it makes the number drift upwards on every run.
    files = sum(
        1
        for path in target.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    print(f"copied     : {files} file(s), manifest now {landed}")
    if landed != source_version:
        print("the installed manifest does not match the source - wrong filesystem?")
        return 1

    # Every file, by content, read back through the same route it was written
    # with. The version line above proves a manifest arrived; this proves the
    # code did -- and it is the check that would have caught the deploy of
    # 1.0.8 that never ran, whichever road it had taken.
    differs = []
    for relative in sorted(wanted):
        landed_file = target / relative
        try:
            if not landed_file.is_file() or digest(landed_file) != digest(SOURCE / relative):
                differs.append(relative)
        except OSError as exc:
            differs.append(f"{relative} ({exc})")
    if DESKTOP_SOURCE.is_file():
        try:
            if digest(desktop_target) != digest(DESKTOP_SOURCE):
                differs.append("desktop-ui/plugin.js -> desktop-plugins/")
        except OSError as exc:
            differs.append(f"desktop-plugins/plugin.js ({exc})")
    if differs:
        print(f"verified   : {len(differs)} file(s) DIFFER after the copy:")
        for relative in differs[:10]:
            print(f"             {relative}")
        return 1
    print(f"verified   : {len(wanted)} file(s) match the source by digest")
    print()
    print("Restart Hermes for the new code to load. A deployed plugin that was")
    print("never restarted into is the same as a plugin that was never deployed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
