"""Build the distributable zip of the plugin, named after its own manifest.

    python tools/package.py

Unlike ``deploy.py`` this one does not care which interpreter runs it — it only
reads and writes inside this repository, never inside the Hermes install.

Three things it does that a hand-rolled ``zip -r`` did not, each one a mistake
that was actually made while building the 1.0.0 archive by hand:

* **The version comes from the manifest, never from an argument.** An archive
  named for a version its ``plugin.yaml`` does not carry is worse than no
  archive: it installs, it reports the wrong version, and every later question
  about which build is running has a wrong answer available.
* **``__pycache__`` never ships.** A stale ``.pyc`` compiled against a different
  source file is the one payload that can make an install behave like a version
  nobody wrote.
* **The paths inside are rooted at the plugin directory**, so unzipping into
  ``$HERMES_HOME/plugins`` produces exactly the directory Hermes expects, with
  no nesting to fix by hand.

It ends by listing what it wrote, because an archive built from the wrong
working tree looks identical from the outside.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hermes-kame-api-rotation"
DIST = ROOT / "dist"

#: Nothing generated, nothing editor-local. Anything not matched here ships, so
#: a new source file is included by existing rather than by being listed.
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".swp"}


def manifest_version(manifest: Path) -> str:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"no version in {manifest}")


def shipped_files() -> list[Path]:
    out = []
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        out.append(path)
    return out


def main() -> int:
    if not SOURCE.is_dir():
        print(f"source not found: {SOURCE}")
        return 2

    version = manifest_version(SOURCE / "plugin.yaml")
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{SOURCE.name}-{version}.zip"

    files = shipped_files()
    # Deflate rather than store: the plugin is text, and the archive is small
    # enough that the difference is a courtesy rather than a saving.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(SOURCE.parent).as_posix())

    total = sum(path.stat().st_size for path in files)
    print(f"version : {version}  (from plugin.yaml)")
    print(f"wrote   : {target}")
    print(f"          {len(files)} file(s), {total:,} bytes -> "
          f"{target.stat().st_size:,} bytes")

    # The one check worth making on the way out: the archive must contain the
    # manifest at the path Hermes looks for it, or the zip is unusable in a way
    # that only shows up on somebody else's machine.
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    wanted = f"{SOURCE.name}/plugin.yaml"
    if wanted not in names:
        print(f"FAIL: {wanted} is not in the archive")
        return 1
    print(f"          root: {wanted} present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
