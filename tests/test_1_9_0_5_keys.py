"""1.9.0.5 (component D): no silent leftovers, bad keys named.

D1 ``/kame-keys remove-all`` and the panel's "Remove API Rotation's keys" action take back what the plugin put in the
pool and in ``.env``. D2 judges keys on ``add`` / ``import`` (shape for every provider that has one, one read
to the provider for Gemini) and never touches the network at boot.

Real Hermes pool and ``auth.json`` in the suite's redirected HERMES_HOME; the HTTP call is injected.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("agent.credential_pool")

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_keys_1905_under_test"


def _load():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return tuple(importlib.import_module(f"{PACKAGE}.{name}")
                 for name in ("envsync", "settings", "commands", "envfile", "control", "core.keys", "core.carousel"))


envsync, settings, commands, envfile, control, keys_mod, carousel = _load()

VAR = "GEMINI_API_KEY"


def gk(n: int) -> str:
    """A key shaped like Google's: AIza + 35 characters = 39."""
    return f"AIzaSyTEST{n:02d}".ljust(39, "x")


K = [gk(i) for i in range(1, 8)]


@pytest.fixture(autouse=True)
def _clean_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "KAME_RESOLVER_DISABLED", "KAME_ROTATION_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    settings.forget()
    # no test here may reach the network: an unanswered probe is "no verdict" (the key is added)
    monkeypatch.setattr(commands, "_http_status", lambda url, timeout=8.0, headers=None: None)
    yield
    settings.forget()


def _env(tmp_path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_bytes(text.encode("utf-8"))
    return path


def _rows(provider="gemini"):
    from agent.credential_pool import load_pool

    return [(e.source, e.access_token) for e in load_pool(provider).entries()]


def _ours():
    return sorted(t for s, t in _rows() if s == envsync.SOURCE_PREFIX + VAR)


def _suppressed():
    from hermes_cli.auth import is_source_suppressed

    return is_source_suppressed("gemini", f"env:{VAR}")


# --- D1 ---------------------------------------------------------------------------------------------------


def test_remove_all_takes_back_rows_suppression_and_the_key_list_in_env(tmp_path):
    original = f"# my keys\nOTHER=keep-me\n{VAR}={','.join(K[:3])}\nZED=also-kept\n"
    _env(tmp_path, original)
    envsync.sync()
    assert _ours() == sorted(K[:3]) and _suppressed()
    commands._cmd_add("gemini " + K[5])  # a key the user added by hand: must survive

    text = commands.handle("remove-all")

    assert _ours() == []
    assert not _suppressed()
    assert ("manual", K[5]) in _rows()
    kept = (tmp_path / ".env").read_bytes().decode("utf-8")
    assert kept == f"# my keys\nOTHER=keep-me\n{VAR}={K[0]}\nZED=also-kept\n"
    backups = sorted(tmp_path.glob(".env.kame-*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes().decode("utf-8") == original
    # counts and fingerprints, never a key
    assert "3 API Rotation row(s) removed" in text and "held 3 keys" in text and "Backup:" in text
    assert "uninstalled" in text
    assert all(k not in text and k[:12] not in text for k in K)
    assert carousel.fingerprint(K[0]) in text


def test_remove_all_after_it_hermes_reads_one_valid_key(tmp_path):
    _env(tmp_path, f"{VAR}={','.join(K[:3])}\n")
    envsync.sync()
    commands.handle("remove-all")
    # the next start has nothing left to take back, and Hermes seeds the first key by itself
    assert envsync.sync() == []
    assert (f"env:{VAR}", K[0]) in _rows()
    assert all("," not in token for _s, token in _rows())


def test_remove_all_with_one_key_in_env_leaves_the_file_alone(tmp_path):
    from dataclasses import replace

    from agent.credential_pool import load_pool

    _env(tmp_path, f"{VAR}={K[0]}\n")
    load_pool("gemini").add_entry(
        replace(commands._make_entry("gemini", K[1], "ours"), source=envsync.SOURCE_PREFIX + VAR))
    before = (tmp_path / ".env").read_bytes()
    text = commands.handle("remove-all")
    assert "1 API Rotation row(s) removed" in text
    assert (tmp_path / ".env").read_bytes() == before
    assert not list(tmp_path.glob(".env.kame-*.bak"))


def test_remove_all_with_nothing_of_ours_says_so_and_changes_nothing(tmp_path):
    _env(tmp_path, f"{VAR}={K[0]}\n")
    before = (tmp_path / ".env").read_bytes()
    text = commands.handle("remove-all")
    assert text.startswith("Nothing to remove")
    assert (tmp_path / ".env").read_bytes() == before


def test_remove_all_for_another_provider_touches_nothing(tmp_path):
    _env(tmp_path, f"{VAR}={','.join(K[:3])}\n")
    envsync.sync()
    text = commands.handle("remove-all openrouter")
    assert text.startswith("Nothing to remove")
    assert _ours() == sorted(K[:3])


def test_remove_all_works_with_the_resolver_switched_off(monkeypatch, tmp_path):
    _env(tmp_path, f"{VAR}={','.join(K[:2])}\n")
    envsync.sync()
    monkeypatch.setenv("KAME_RESOLVER_DISABLED", "1")
    settings.forget()
    commands.handle("remove-all")
    assert _ours() == []


def test_a_list_outside_env_is_named_not_edited(monkeypatch, tmp_path):
    # the variable lives in the system environment only: KAME does not write there
    monkeypatch.setenv(VAR, ",".join(K[:3]))
    envsync.sync()
    text = commands.handle("remove-all")
    assert "outside Hermes' .env" in text
    assert os.environ[VAR] == ",".join(K[:3])
    assert not list(tmp_path.glob(".env.kame-*.bak"))


def test_the_panel_action_is_the_same_removal(tmp_path):
    assert "undo_setup" in control.ACTIONS
    _env(tmp_path, f"{VAR}={','.join(K[:3])}\n")
    envsync.sync()
    ok, detail = control._apply("undo_setup", "", None)
    assert ok and "3 API Rotation row(s) removed" in detail
    assert _ours() == []
    ok, detail = control._apply("undo_setup", "", None)
    assert ok and detail.startswith("Nothing to remove")


def test_env_backups_are_pruned_to_the_last_few(tmp_path):
    _env(tmp_path, f"{VAR}={','.join(K[:2])}\n")
    for index in range(envfile.MAX_BACKUPS + 3):
        (tmp_path / f".env.kame-20200101-00000{index}.bak").write_text("old", encoding="utf-8")
    name = envfile.backup()
    left = sorted(p.name for p in tmp_path.glob(".env.kame-*.bak"))
    assert len(left) == envfile.MAX_BACKUPS and name in left
    assert (tmp_path / name).read_bytes() == (tmp_path / ".env").read_bytes()


def test_a_backup_never_overwrites_an_earlier_one(tmp_path, monkeypatch):
    _env(tmp_path, f"{VAR}={','.join(K[:2])}\n")
    monkeypatch.setattr(envfile.time, "strftime", lambda fmt: "20261122-120000")

    class _Now:
        microsecond = 7

    monkeypatch.setattr(envfile.datetime, "datetime", type("D", (), {"now": staticmethod(lambda: _Now)}))
    first = envfile.backup()
    (tmp_path / ".env").write_text("changed\n", encoding="utf-8")
    second = envfile.backup()
    third = envfile.backup()
    assert len({first, second, third}) == 3
    assert (tmp_path / first).read_bytes().decode() == f"{VAR}={','.join(K[:2])}\n"
    assert "000007" in first


def test_one_remove_all_over_two_variables_takes_one_backup_with_both_original_lines(tmp_path):
    original = f"GOOGLE_API_KEY={','.join(K[:3])}\n{VAR}={','.join(K[3:6])}\nOTHER=x\n"
    _env(tmp_path, original)
    envsync.sync()
    envsync.remove_all("gemini")
    backups = sorted(tmp_path.glob(".env.kame-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes().decode("utf-8") == original
    text = (tmp_path / ".env").read_bytes().decode("utf-8")
    assert text == f"GOOGLE_API_KEY={K[0]}\n{VAR}={K[3]}\nOTHER=x\n"


def test_keep_first_key_refuses_a_name_that_is_not_a_variable(tmp_path):
    _env(tmp_path, f"{VAR}={','.join(K[:2])}\n")
    assert envfile.keep_first_key("A B")[0] == "error"
    assert envfile.keep_first_key("")[0] == "error"


# --- D2 ---------------------------------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, answers=None, default=200):
        self.answers = answers or {}
        self.default = default
        self.urls = []
        self.headers = []

    def __call__(self, url, timeout=8.0, headers=None):
        # the key travels in a header; ``urls`` keeps the key beside the URL so tests can tell who was asked
        self.headers.append(dict(headers or {}))
        carried = (headers or {}).get("x-goog-api-key", "")
        self.urls.append(f"{url}#{carried}")
        for key, status in self.answers.items():
            if key in carried or key in url:
                return status
        return self.default


@pytest.fixture()
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(commands, "_http_status", fake)
    return fake


def test_shape_rule_is_gemini_only_and_non_empty_for_everyone_else():
    assert keys_mod.shape_problem("gemini", K[0]) is None
    assert keys_mod.shape_problem("gemini", "AIza" + "x" * 10) == "does not look like a gemini key"
    assert keys_mod.shape_problem("gemini", "sk-" + "x" * 36) == "does not look like a gemini key"
    assert keys_mod.shape_problem("openrouter", "anything-goes") is None
    assert keys_mod.shape_problem("openrouter", "") is not None


def test_gemini_accepts_the_new_AQ_keys_and_still_the_AIza_ones():
    # fix 1: Google now issues AQ. keys (40+ characters); AIza stays exactly 39
    assert keys_mod.shape_problem("gemini", "AQ." + "x" * 37) is None
    assert keys_mod.shape_problem("gemini", "AQ." + "x" * 60) is None
    assert keys_mod.shape_problem("gemini", "AQ." + "x" * 36) == "does not look like a gemini key"
    assert keys_mod.shape_problem("gemini", "AIza" + "x" * 36) == "does not look like a gemini key"
    assert keys_mod.shape_problem("gemini", "AIza" + "x" * 35) is None


def test_add_probes_each_new_gemini_key_once_through_models_and_adds_the_good_ones(http):
    text = commands.handle(f"add {K[0]},{K[1]},{K[1]},")  # a duplicate and a trailing comma
    assert [u.split("?")[0] for u in http.urls] == [
        "https://generativelanguage.googleapis.com/v1beta/models"] * 2
    assert all("pageSize=1" in u for u in http.urls)
    # fix 10: the key is in the x-goog-api-key header, never in the URL
    assert [h["x-goog-api-key"] for h in http.headers] == K[:2]
    assert all("key=" not in u.split("#")[0] and not any(k in u.split("#")[0] for k in K) for u in http.urls)
    assert "Added 2 key(s)" in text
    assert sorted(t for _s, t in _rows()) == sorted(K[:2])


def test_a_key_the_provider_refuses_is_named_and_not_added(http):
    http.answers = {K[1]: 400, K[2]: 403, K[3]: 401}
    text = commands.handle(f"add {K[0]},{K[1]},{K[2]},{K[3]}")
    assert "Added 1 key(s)" in text
    assert text.count("rejected by the provider") == 3
    assert "HTTP 400" in text and "HTTP 403" in text and "HTTP 401" in text
    assert [t for _s, t in _rows()] == [K[0]]
    assert all(k not in text for k in K)


def test_a_key_with_the_wrong_shape_is_a_warning_only_and_the_provider_decides(http):
    odd = "AIzaTooShortButLongEnoughToPass1"
    text = commands.handle(f"add {K[0]},{odd}")
    assert "does not look like a gemini key" in text
    assert len(http.urls) == 2  # fix 1: the odd one is asked too
    assert sorted(t for _s, t in _rows()) == sorted([K[0], odd])  # accepted: the probe said 200
    http.urls.clear()
    http.answers = {"AQ.refused": 400}
    text = commands.handle("add AQ.refused" + "y" * 30)
    assert "rejected by the provider" in text and "AQ.refused" not in text


def test_an_AQ_key_is_added_without_a_warning(http):
    key = "AQ." + "z" * 45
    text = commands.handle(f"add {key}")
    assert "Added 1 key(s)" in text and "does not look like" not in text


def test_no_answer_from_the_provider_is_no_verdict_the_key_is_added_and_said_unchecked(http):
    http.answers = {K[0]: 429, K[1]: 503}
    http.default = None
    text = commands.handle(f"add {K[0]},{K[1]},{K[2]}")
    assert "Added 3 key(s)" in text
    assert text.count("could not be asked just now") == 3


def test_import_probes_too(http, tmp_path):
    path = tmp_path / "keys.txt"
    path.write_text("\n".join([K[0], K[1]]), encoding="utf-8")
    http.answers = {K[1]: 400}
    text = commands.handle(f"import {path}")
    assert len(http.urls) == 2
    assert "Added 1 key(s)" in text and "rejected by the provider" in text


def test_a_key_already_in_the_pool_is_not_asked_again(http):
    commands.handle(f"add {K[0]}")
    http.urls.clear()
    commands.handle(f"add {K[0]},{K[1]}")
    assert len(http.urls) == 1 and K[1] in http.urls[0]


def test_an_unknown_provider_is_checked_by_shape_only_and_never_probed(http):
    text = commands.handle("add openrouter sk-or-v1-" + "a" * 30)
    assert http.urls == []
    assert "Added 1 key(s)" in text


def test_nothing_is_asked_at_boot(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("the network must not be touched at boot")

    monkeypatch.setattr(commands, "_http_status", boom)
    _env(tmp_path, f"{VAR}={','.join(K[:3])}\n")
    assert envsync.sync()
    envsync.sync()


def test_boot_names_a_misshapen_key_in_the_log_by_fingerprint(caplog, tmp_path):
    bad = "AIzaThisOneIsTooLongToBeAGoogleKeyForSure0000"
    _env(tmp_path, f"{VAR}={K[0]},{bad}\n")
    with caplog.at_level("WARNING"):
        envsync.sync()
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "do not look like a gemini key" in line and carousel.fingerprint(bad) in line
    assert bad not in line and K[0] not in line


def test_the_key_list_names_a_stored_key_that_does_not_look_right(http):
    from agent.credential_pool import load_pool

    pool = load_pool("gemini")
    pool.add_entry(commands._make_entry("gemini", K[0], "good one"))
    pool.add_entry(commands._make_entry("gemini", "AIzaWrongLengthButLongEnoughToParse", "odd one"))
    text = commands.handle("status gemini")
    assert "odd one: does not look like a gemini key" in text
    assert "good one: does not look" not in text
