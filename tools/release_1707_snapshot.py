"""Read-only delivery snapshot; on-disk matching is not running acceptance."""
import hashlib
import json
import re
import runpy
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "hermes-kame-api-rotation"
_PACKAGE = runpy.run_path(str(ROOT / "tools/package.py"))


def files(root):
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file() and not (_PACKAGE["SKIP_DIRS"] & set(p.parts))
        and p.suffix not in _PACKAGE["SKIP_SUFFIXES"]}


def version(root):
    p = root / "plugin.yaml"
    if not p.is_file():
        return None
    m = re.search(r"(?m)^version:\s*[\"']?([0-9.]+)", p.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _check_archive(archive, expected):
    packaged = {"exists": archive.is_file(), "matches_source": False}
    if archive.is_file():
        try:
            z = zipfile.ZipFile(archive)
        except (zipfile.BadZipFile, OSError):
            packaged["crc_valid"] = False
            return packaged
        with z:
            members = [i.filename for i in z.infolist() if not i.is_dir()]
            prefix = NAME + "/" if all(n.startswith(NAME + "/") for n in members) else ""
            actual = {n.removeprefix(prefix): hashlib.sha256(z.read(n)).hexdigest()
                for n in members}
            packaged.update(crc_valid=z.testzip() is None,
                duplicate_members=len(members) != len(set(members)),
                matches_source=actual == expected and len(members) == len(set(members)) and z.testzip() is None,
                sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    return packaged


def check_archive(archive, expected):
    try:
        return _check_archive(archive, expected)
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
        return {"exists": archive.is_file(), "matches_source": False, "crc_valid": False}


def snapshot(home):
    source = ROOT / NAME
    expected = files(source)
    archive = ROOT / "dist" / (NAME + "-1.7.0.7.zip")
    packaged = check_archive(archive, expected)
    profiles = []
    for name, base in [("default", home), ("k", home/"profiles/k"), ("lo1", home/"profiles/lo1")]:
        plugin = base / "plugins" / NAME
        profiles.append({"profile": name, "path": str(plugin), "version": version(plugin),
            "matches_source": files(plugin) == expected if plugin.is_dir() else False,
            "running_build_verified": False})
    return {"scope": "on-disk shipped-file snapshot; not full release or runtime acceptance",
        "source_version": version(source), "archive": packaged, "profiles": profiles,
        "ready_for_owner_testing": False,
        "pending": "Provider acceptance, full release audit and actual running-profile handshakes remain required"}


if __name__ == "__main__":
    result = snapshot(Path.home()/"AppData/Local/hermes")
    target = ROOT/"research/1.7.0.7/delivery-snapshot.json"
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
