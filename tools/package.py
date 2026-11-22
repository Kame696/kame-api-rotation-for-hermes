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
#: 1.9.0.5: ONE package. The provider profiles moved into the rotation plugin (``provider_profiles.py``);
#: ``hermes-kame-provider/`` stays in the repository, untouched, but is no longer built or shipped.
SOURCES = (ROOT / "hermes-kame-api-rotation",)
#: 1.8.2.7: the optional bridge ships beside them in every release (not in the
#: catalog entry); built here so its ZIP always carries the same version.
OPTIONAL = (ROOT / "hermes-kame-bridge",)
SOURCE = SOURCES[0]  # the name earlier releases (and their tests) read
DIST = ROOT / "dist"

#: Nothing generated, nothing editor-local. Anything not matched here ships, so
#: a new source file is included by existing rather than by being listed.
#:
#: ``graphify-out`` is here for the same reason as the caches: it is generated
#: from the source beside it, it is 3.6 MB of it, and a knowledge graph of the
#: plugin is a thing the plugin's author reads, not a thing the plugin needs to
#: run. It appeared in the tree after 1.2.6 and would otherwise have shipped by
#: the "anything not matched here ships" rule above — which is the rule working,
#: and this is the answer it wanted.
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "graphify-out"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".swp"}


def manifest_version(manifest: Path) -> str:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"no version in {manifest}")


def shipped_files(source: Path = SOURCE) -> list[Path]:
    out = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        out.append(path)
    return out


def build(source: Path) -> int:
    if not source.is_dir():
        print(f"source not found: {source}")
        return 2

    version = manifest_version(source / "plugin.yaml")
    DIST.mkdir(exist_ok=True)
    target = DIST / f"{source.name}-{version}.zip"

    files = shipped_files(source)
    # Deflate rather than store: the plugin is text, and the archive is small
    # enough that the difference is a courtesy rather than a saving.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(source.parent).as_posix())

    total = sum(path.stat().st_size for path in files)
    print(f"version : {version}  (from {source.name}/plugin.yaml)")
    print(f"wrote   : {target}")
    print(f"          {len(files)} file(s), {total:,} bytes -> "
          f"{target.stat().st_size:,} bytes")

    # The one check worth making on the way out: the archive must contain the
    # manifest at the path Hermes looks for it, or the zip is unusable in a way
    # that only shows up on somebody else's machine.
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    wanted = f"{source.name}/plugin.yaml"
    if wanted not in names:
        print(f"FAIL: {wanted} is not in the archive")
        return 1
    print(f"          root: {wanted} present")
    return 0


def build_all_in_one(sources: tuple) -> int:
    """``KAME-for-Hermes-<version>-all-in-one.zip``: every shipped plugin directory in one archive (1.9.0.5:
    the rotation plugin and, when present, the optional bridge; never ``hermes-kame-provider``)."""
    version = manifest_version(SOURCE / "plugin.yaml")
    DIST.mkdir(exist_ok=True)
    target = DIST / f"KAME-for-Hermes-{version}-all-in-one.zip"
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            for path in shipped_files(source):
                archive.write(path, path.relative_to(source.parent).as_posix())
                count += 1
    with zipfile.ZipFile(target) as archive:
        roots = sorted({name.split("/", 1)[0] for name in archive.namelist()})
    if "hermes-kame-provider" in roots:
        print("FAIL: hermes-kame-provider must not be in the all-in-one")
        return 1
    print(f"wrote   : {target}  ({count} file(s), {target.stat().st_size:,} bytes; roots: {', '.join(roots)})")
    return 0


def main() -> int:
    everything = SOURCES + tuple(o for o in OPTIONAL if (o / "plugin.yaml").exists())
    versions = {manifest_version(source / "plugin.yaml") for source in everything}
    if len(versions) != 1:
        print(f"FAIL: the plugins carry different versions: {sorted(versions)}")
        return 1
    for source in everything:
        code = build(source)
        if code:
            return code
    return build_all_in_one(everything)


if __name__ == "__main__":
    raise SystemExit(main())
