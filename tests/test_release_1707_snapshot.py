"""The delivery snapshot detects stale code without claiming running acceptance."""
import runpy
from pathlib import Path

M = runpy.run_path(str(Path(__file__).resolve().parents[1]/"tools/release_1707_snapshot.py"))

def test_code_comparison_detects_changed_bytes(tmp_path):
    (tmp_path/"core.py").write_text("value = 1")
    before=M["files"](tmp_path)
    (tmp_path/"core.py").write_text("value = 2")
    assert M["files"](tmp_path) != before

def test_missing_profiles_never_claim_installed_or_running(tmp_path):
    result=M["snapshot"](tmp_path)
    assert len(result["profiles"]) == 3
    assert not result["ready_for_owner_testing"]
    assert all(not p["matches_source"] and not p["running_build_verified"] for p in result["profiles"])


def test_valid_changed_and_duplicate_archives(tmp_path):
    import zipfile, hashlib, warnings
    zpath = tmp_path / "candidate.zip"
    expected = {"core.py":hashlib.sha256(b"ok").hexdigest()}
    with zipfile.ZipFile(zpath,"w") as z:
        z.writestr("core.py",b"ok")
    assert M["check_archive"](zpath,expected)["matches_source"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore",UserWarning)
        with zipfile.ZipFile(zpath,"a") as z:
            z.writestr("core.py",b"ok")
    result=M["check_archive"](zpath,expected)
    assert result["duplicate_members"] and not result["matches_source"]
    with zipfile.ZipFile(zpath,"w") as z:
        z.writestr("core.py",b"changed")
    assert not M["check_archive"](zpath,expected)["matches_source"]


def test_invalid_zip_is_a_failed_check_not_a_crash(tmp_path):
    p=tmp_path/"bad.zip";p.write_bytes(b"not a zip")
    result=M["check_archive"](p,{})
    assert not result["crc_valid"] and not result["matches_source"]


def test_damaged_member_crc_fails_cleanly(tmp_path):
    import zipfile,hashlib
    p=tmp_path/"damaged.zip"
    payload=b"unique-payload-for-crc-check"
    with zipfile.ZipFile(p,"w",compression=zipfile.ZIP_STORED) as z:
        z.writestr("core.py",payload)
    data=p.read_bytes();assert data.count(payload)==1
    p.write_bytes(data.replace(payload,b"X"+payload[1:],1))
    result=M["check_archive"](p,{"core.py":hashlib.sha256(payload).hexdigest()})
    assert not result["crc_valid"] and not result["matches_source"]


def test_noncode_asset_changes_are_detected(tmp_path):
    (tmp_path/"style.css").write_text("color:red")
    before=M["files"](tmp_path)
    assert "style.css" in before
    (tmp_path/"style.css").write_text("color:blue")
    assert M["files"](tmp_path) != before


def test_archive_missing_asset_fails_comparison(tmp_path):
    import zipfile,hashlib
    p=tmp_path/"assets.zip"
    expected={"core.py":hashlib.sha256(b"ok").hexdigest(),
              "style.css":hashlib.sha256(b"red").hexdigest()}
    with zipfile.ZipFile(p,"w") as z:
        z.writestr("core.py",b"ok")
    assert not M["check_archive"](p,expected)["matches_source"]
    with zipfile.ZipFile(p,"a") as z:
        z.writestr("style.css",b"red")
    assert M["check_archive"](p,expected)["matches_source"]
