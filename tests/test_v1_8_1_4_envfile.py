"""1.8.1.4 -- the one file full of secrets is never left half-written.

``envfile.write``/``forget`` rewrite Hermes' ``.env`` on every ``/kame set``
and every panel save. They used ``Path.write_text``: truncate, then write. A
write that failed in between (a full disk, a killed process) left the file --
every provider key the user has -- empty or cut. They now write a temporary
file beside it, fsync it and rename it over, keeping the file's mode, its line
endings and a symlink that points at it.
"""
from __future__ import annotations

import errno
import importlib
import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1814_envfile_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
envfile = importlib.import_module(f"{PACKAGE}.envfile")

SECRETS = (
    b"# provider keys\r\n"
    b"OPENAI_API_KEY=sk-live-do-not-lose-me\r\n"
    b"GOOGLE_API_KEY=AIzaSy-also-precious\r\n"
    b"KAME_DAILY_COOLDOWN=120\r\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    target.write_bytes(SECRETS)
    monkeypatch.setattr(envfile, "path", lambda: target)
    return target


def _leftovers(directory: Path):
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


class TestAFailedWriteLosesNothing:
    def test_a_full_disk_mid_write_leaves_every_key_in_place(self, env, monkeypatch):
        def full_disk(_fd):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(envfile.os, "fsync", full_disk)
        ok, detail = envfile.write("KAME_DAILY_COOLDOWN", "300")
        assert ok is False
        assert "could not write" in detail
        assert env.read_bytes() == SECRETS
        assert _leftovers(env.parent) == []

    def test_forget_fails_the_same_safe_way(self, env, monkeypatch):
        def full_disk(_fd):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(envfile.os, "fsync", full_disk)
        ok, _ = envfile.forget("KAME_DAILY_COOLDOWN")
        assert ok is False
        assert env.read_bytes() == SECRETS
        assert _leftovers(env.parent) == []


class TestTheRewriteIsFaithful:
    def test_crlf_lines_stay_crlf_and_the_others_survive_byte_for_byte(self, env):
        ok, _ = envfile.write("KAME_DAILY_COOLDOWN", "300")
        assert ok is True
        assert env.read_bytes() == SECRETS.replace(
            b"KAME_DAILY_COOLDOWN=120", b"KAME_DAILY_COOLDOWN=300"
        )

    def test_lf_lines_stay_lf(self, env):
        env.write_bytes(SECRETS.replace(b"\r\n", b"\n"))
        envfile.forget("KAME_DAILY_COOLDOWN")
        assert env.read_bytes() == (
            b"# provider keys\nOPENAI_API_KEY=sk-live-do-not-lose-me\nGOOGLE_API_KEY=AIzaSy-also-precious\n"
        )

    def test_a_refused_rename_falls_back_to_the_old_in_place_write(self, env, monkeypatch):
        # Windows refuses a rename over a file another process holds open.
        def busy(_src, _dst):
            raise PermissionError(errno.EACCES, "in use")

        monkeypatch.setattr(envfile.os, "replace", busy)
        ok, _ = envfile.write("KAME_DAILY_COOLDOWN", "300")
        assert ok is True
        assert b"KAME_DAILY_COOLDOWN=300\r\n" in env.read_bytes()
        assert _leftovers(env.parent) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits and symlinks")
class TestTheFileKeepsItsShape:
    def test_an_owner_only_file_stays_owner_only(self, env):
        env.chmod(0o600)
        envfile.write("KAME_DAILY_COOLDOWN", "300")
        assert stat.S_IMODE(env.stat().st_mode) == 0o600

    def test_a_group_readable_file_keeps_its_mode(self, env):
        # Hermes keeps 0640 on managed installs on purpose; not ours to change.
        env.chmod(0o640)
        envfile.write("KAME_DAILY_COOLDOWN", "300")
        assert stat.S_IMODE(env.stat().st_mode) == 0o640

    def test_a_new_file_starts_owner_only(self, tmp_path, monkeypatch):
        target = tmp_path / ".env"
        monkeypatch.setattr(envfile, "path", lambda: target)
        ok, _ = envfile.write("KAME_DAILY_COOLDOWN", "300")
        assert ok is True
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_a_symlinked_env_is_written_through_and_stays_a_link(self, tmp_path, monkeypatch):
        real = tmp_path / "real.env"
        real.write_bytes(SECRETS)
        link = tmp_path / ".env"
        link.symlink_to(real)
        monkeypatch.setattr(envfile, "path", lambda: link)
        envfile.write("KAME_DAILY_COOLDOWN", "300")
        assert link.is_symlink()
        assert b"KAME_DAILY_COOLDOWN=300" in real.read_bytes()


commands = importlib.import_module(f"{PACKAGE}.commands")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
class TestTheAuthStoreBackupIsBornPrivate:
    """``/kame-keys add|import`` copies auth.json aside first. The copy used to
    be opened at the umask's 0644 by ``shutil.copy2`` and only then given
    auth.json's 0600."""

    def test_the_backup_is_owner_only_from_the_moment_it_exists(self, tmp_path, monkeypatch):
        auth = tmp_path / "auth.json"
        auth.write_text('{"credential_pool": {"openai": [{"access_token": "sk-x"}]}}')
        auth.chmod(0o600)
        monkeypatch.setattr(commands, "_auth_store_path", lambda: auth)
        # Without the mode copy, what the file was born with is what remains.
        monkeypatch.setattr(commands.shutil, "copystat", lambda *a, **k: None)
        old = os.umask(0o022)
        try:
            name = commands._backup_auth_store()
        finally:
            os.umask(old)
        backup = tmp_path / name
        assert backup.read_bytes() == auth.read_bytes()
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
