"""Tests for /kame-keys, against a fake credential pool.

No Hermes is imported and no auth.json is touched. The command module reaches
the host through four small bridge functions; those are what get replaced
here, which is the reason they exist as separate functions at all.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"

_PKG = "kame_commands_under_test"


def _load_commands():
    spec = importlib.util.spec_from_file_location(
        _PKG,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{_PKG}.commands")


cmd = _load_commands()

# Fabricated but realistically shaped: 39 chars, `AIza` prefix, mixed case
# and digits like a real Google key. The digits matter — a fixture without
# them would not exercise the same code paths as a live key.
K1 = "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7"
K2 = "AIzaSyB2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
K3 = "AIzaSyC3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9"


_UNSET = object()


class FakeEntry:
    def __init__(self, provider, key, label, status=None, count=0,
                 auth_type="api_key", runtime_key=_UNSET):
        self.provider = provider
        self.access_token = key
        self.label = label
        self.last_status = status
        self.request_count = count
        self.id = label
        self.auth_type = auth_type
        # The pool runs on this property, not on the stored token, and the
        # two disagree on real entries: a nous credential keys on its invoke
        # JWT and reports "" once that expires, while a borrowed row is a
        # metadata-only stub until it is hydrated on load. Defaulting it to
        # the stored token keeps every existing test meaning what it meant.
        self._runtime_key = key if runtime_key is _UNSET else runtime_key

    @property
    def runtime_api_key(self):
        return self._runtime_key


class FakePool:
    """Enough of CredentialPool to exercise the command paths."""

    def __init__(self, provider, keys=()):
        self.provider = provider
        self._entries = [
            FakeEntry(provider, key, f"seed #{i}") for i, key in enumerate(keys, 1)
        ]
        self.reset_calls = 0

    def entries(self):
        return list(self._entries)

    def add_entry(self, entry):
        self._entries.append(entry)
        return entry

    def reset_statuses(self):
        self.reset_calls += 1
        for entry in self._entries:
            entry.last_status = None
        return len(self._entries)


@pytest.fixture
def pool(monkeypatch):
    """Wire the command module to a single fake pool, with no side effects."""
    fake = FakePool("gemini")
    pools = {"gemini": fake}

    monkeypatch.setattr(cmd, "_load_pool", lambda p: pools.setdefault(p, FakePool(p)))
    monkeypatch.setattr(
        cmd, "_make_entry",
        lambda provider, key, label: FakeEntry(provider, key, label),
    )
    monkeypatch.setattr(cmd, "_pooled_providers", lambda: sorted(pools))
    monkeypatch.setattr(cmd, "_unsuppress", lambda provider: None)
    monkeypatch.setattr(cmd, "_backup_auth_store", lambda: "auth.json.kame-TEST.bak")
    # Real ids from Hermes' own provider registry, including the long
    # hyphenated one that defeats every shape-based split.
    monkeypatch.setattr(
        cmd, "_known_providers",
        lambda: frozenset({"gemini", "openrouter", "anthropic", "alibaba-coding-plan"}),
    )
    # `agent.credential_pool` is not importable here, so the real bridge
    # would answer "not an API-key entry" for everything and the usability
    # check would never fire. Stand in for the host's constant; the bridge
    # itself is exercised against the real Hermes in tools/sandbox_binding.py.
    monkeypatch.setattr(
        cmd, "_is_api_key_entry",
        lambda entry: str(getattr(entry, "auth_type", "")) == "api_key",
    )
    fake._all = pools
    return fake


class TestAdd:
    def test_comma_separated_bulk_add(self, pool):
        out = cmd.handle(f"add {K1},{K2},{K3}")
        assert "Added 3 key(s) to gemini" in out
        assert len(pool.entries()) == 3

    def test_newline_separated_bulk_add(self, pool):
        out = cmd.handle(f"add {K1}\n{K2}")
        assert "Added 2 key(s)" in out

    def test_never_echoes_a_key(self, pool):
        out = cmd.handle(f"add {K1},{K2}")
        assert K1 not in out
        assert K2 not in out
        assert "AIzaSy" in out  # redacted previews are still shown

    def test_second_run_adds_nothing(self, pool):
        cmd.handle(f"add {K1},{K2}")
        out = cmd.handle(f"add {K1},{K2}")
        assert "Nothing to add" in out
        assert len(pool.entries()) == 2

    def test_partial_overlap_adds_only_the_new_one(self, pool):
        cmd.handle(f"add {K1}")
        out = cmd.handle(f"add {K1},{K2}")
        assert "Added 1 key(s)" in out
        assert "1 already in pool" in out
        assert len(pool.entries()) == 2

    def test_explicit_provider(self, pool):
        out = cmd.handle(f"add openrouter {K1}")
        assert "to openrouter" in out
        assert len(pool.entries()) == 0  # gemini pool untouched
        assert len(pool._all["openrouter"].entries()) == 1

    def test_labels_do_not_collide_with_existing(self, pool):
        cmd.handle(f"add {K1}")
        cmd.handle(f"add {K2}")
        labels = [e.label for e in pool.entries()]
        assert len(set(labels)) == len(labels)

    def test_backup_is_reported(self, pool):
        out = cmd.handle(f"add {K1}")
        assert "auth.json.kame-TEST.bak" in out

    def test_no_backup_when_nothing_to_write(self, pool, monkeypatch):
        called = []
        monkeypatch.setattr(
            cmd, "_backup_auth_store", lambda: called.append(1) or "x.bak"
        )
        cmd.handle("add short")
        assert called == []

    def test_empty_payload(self, pool):
        assert "Usage" in cmd.handle("add")

    def test_add_survives_one_failing_entry(self, pool, monkeypatch):
        def flaky(provider, key, label):
            if key == K2:
                raise RuntimeError("disk full")
            return FakeEntry(provider, key, label)

        monkeypatch.setattr(cmd, "_make_entry", flaky)
        out = cmd.handle(f"add {K1},{K2},{K3}")
        # The good keys still land; the bad one is named only in redacted form.
        assert "Added 2 key(s)" in out
        assert "disk full" in out
        assert K2 not in out


class TestStatus:
    def test_empty_pool(self, pool):
        assert "No pooled credentials" in cmd.handle("")

    def test_lists_redacted(self, pool):
        cmd.handle(f"add {K1},{K2}")
        out = cmd.handle("")
        assert "gemini — 2 key(s)" in out
        assert K1 not in out
        assert "AIzaSy" in out

    def test_shows_exhaustion(self, pool):
        cmd.handle(f"add {K1}")
        pool.entries()[0].last_status = "exhausted"
        assert "exhausted" in cmd.handle("status")

    def test_bad_pool_does_not_crash_status(self, pool, monkeypatch):
        def boom(provider):
            raise RuntimeError("corrupt store")

        monkeypatch.setattr(cmd, "_load_pool", boom)
        monkeypatch.setattr(cmd, "_pooled_providers", lambda: ["gemini"])
        assert "could not load pool" in cmd.handle("status")


class TestAnEntryThePoolWillNeverPick:
    """A row with no runtime key is not a key having a bad day.

    The pool skips it before it looks at status or cooldown, so its stored
    status — usually ``ok``, because nothing ever happened to it — describes
    a request it will never make. This is the state an env source that
    resolves to nothing leaves behind, which is one commented-out line away
    from being anybody's live pool.
    """

    def test_it_is_not_counted_as_a_key(self, pool):
        cmd.handle(f"add {K1},{K2}")
        pool.add_entry(FakeEntry("gemini", "", "GOOGLE_API_KEY", runtime_key=""))
        out = cmd.handle("status")
        assert "gemini — 2 of 3 key(s) usable" in out

    def test_it_is_not_reported_as_ok(self, pool):
        pool.add_entry(FakeEntry("gemini", "", "GOOGLE_API_KEY", runtime_key=""))
        line = [ln for ln in cmd.handle("status").splitlines() if "GOOGLE_API_KEY" in ln][0]
        assert "[no key]" in line
        assert "[ok]" not in line

    def test_a_healthy_pool_still_reads_the_way_it_did(self, pool):
        cmd.handle(f"add {K1},{K2}")
        out = cmd.handle("status")
        assert "gemini — 2 key(s)" in out
        assert "no key" not in out

    def test_the_runtime_key_is_what_counts_not_the_stored_one(self, pool):
        # A nous entry keys on its invoke JWT: the stored token can be
        # present and the runtime key empty once that JWT expires. Reading
        # the stored field would call this one healthy.
        pool.add_entry(FakeEntry("gemini", K3, "stale-jwt", runtime_key=""))
        out = cmd.handle("status")
        assert "0 of 1 key(s) usable" in out

    def test_and_the_other_direction_too(self, pool):
        # The mirror case: nothing stored, a live runtime key. Reading the
        # stored field would show a working credential as blank and, once
        # the count was added, as missing.
        pool.add_entry(FakeEntry("gemini", "", "borrowed", runtime_key=K1))
        out = cmd.handle("status")
        assert "gemini — 1 key(s)" in out
        assert "AIzaSy" in out
        assert K1 not in out

    def test_an_oauth_entry_without_an_api_key_is_left_alone(self, pool):
        # An OAuth credential legitimately carries no API key. Calling it
        # unusable would replace one wrong answer with another.
        pool.add_entry(
            FakeEntry("gemini", "", "oauth-login", auth_type="oauth", runtime_key="")
        )
        out = cmd.handle("status")
        assert "gemini — 1 key(s)" in out
        assert "no key" not in out

    def test_a_raising_runtime_key_is_unusable_not_a_crash(self, pool):
        # ``runtime_api_key`` is computed — the nous branch calls into the
        # host's auth module to judge a JWT. An entry that throws there is
        # one the pool cannot use, and a slash command must not raise.
        class Exploding(FakeEntry):
            @property
            def runtime_api_key(self):
                raise RuntimeError("auth module unavailable")

        pool.add_entry(Exploding("gemini", K1, "explodes"))
        out = cmd.handle("status")
        assert "0 of 1 key(s) usable" in out


class TestImport:
    def test_reads_a_file(self, pool, tmp_path):
        path = tmp_path / "keys.txt"
        path.write_text(f"GEMINI_API_KEY={K1}\nGEMINI_API_KEY_2={K2}\n", encoding="utf-8")
        out = cmd.handle(f"import {path}")
        assert "Added 2 key(s)" in out

    def test_missing_file(self, pool, tmp_path):
        out = cmd.handle(f"import {tmp_path / 'nope.txt'}")
        assert "File not found" in out

    def test_quoted_path_with_spaces(self, pool, tmp_path):
        path = tmp_path / "my keys.txt"
        path.write_text(K1, encoding="utf-8")
        assert "Added 1 key(s)" in cmd.handle(f'import "{path}"')

    def test_no_path(self, pool):
        assert "Usage" in cmd.handle("import")


class TestReset:
    def test_clears_statuses(self, pool):
        cmd.handle(f"add {K1},{K2}")
        out = cmd.handle("reset")
        assert "cleared exhaustion on 2" in out
        assert pool.reset_calls == 1

    def test_nothing_to_reset(self, pool, monkeypatch):
        monkeypatch.setattr(cmd, "_pooled_providers", lambda: [])
        assert "No pooled credentials to reset" in cmd.handle("reset")


class TestDispatch:
    def test_help(self, pool):
        assert "/kame-keys" in cmd.handle("help")

    def test_unknown_subcommand_shows_help(self, pool):
        out = cmd.handle("frobnicate")
        assert "Unknown subcommand" in out
        assert "/kame-keys" in out

    def test_never_raises(self, pool, monkeypatch):
        def boom(_rest):
            raise RuntimeError("kaboom")

        monkeypatch.setitem(cmd._SUBCOMMANDS, "add", boom)
        out = cmd.handle(f"add {K1}")
        assert "failed" in out
        # The exception is reported, but raw_args held live keys.
        assert K1 not in out

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_blank_input_is_status(self, pool, text):
        assert "No pooled credentials" in cmd.handle(text)


class TestRegistration:
    def test_registers_the_slash_command(self, pool):
        class Ctx:
            def __init__(self):
                self.commands = {}

            def register_command(self, name, handler, description="", args_hint=""):
                self.commands[name] = (handler, description, args_hint)

        ctx = Ctx()
        cmd.register_command(ctx)
        assert "kame-keys" in ctx.commands
        handler, description, args_hint = ctx.commands["kame-keys"]
        assert handler is cmd.handle
        assert description and args_hint

    def test_command_name_has_no_leading_slash(self):
        # register_command strips one, but a name that needs stripping is a
        # sign the caller is guessing at the contract.
        assert not cmd.COMMAND_NAME.startswith("/")


class TestProviderSplit:
    @pytest.mark.parametrize("text,expected", [
        (f"{K1}", "gemini"),
        (f"openrouter {K1}", "openrouter"),
        (f"OpenRouter {K1}", "openrouter"),
        ("", "gemini"),
    ])
    def test_leading_provider_detection(self, pool, text, expected):
        provider, _ = cmd._split_provider(text)
        assert provider == expected

    def test_a_key_is_never_mistaken_for_a_provider(self, pool):
        provider, payload = cmd._split_provider(f"{K1},{K2}")
        assert provider == "gemini"
        assert K1 in payload

    def test_space_separated_keys_keep_the_first_one(self, pool):
        # `add K1 K2` must not read K1 as a provider name.
        provider, payload = cmd._split_provider(f"{K1} {K2}")
        assert provider == "gemini"
        assert K1 in payload and K2 in payload

    def test_long_hyphenated_provider_is_not_eaten_as_a_key(self, pool):
        # `alibaba-coding-plan` is 19 chars of letters and hyphens: it passes
        # looks_like_api_key, so any length- or shape-based split would store
        # the provider name itself as a credential.
        provider, payload = cmd._split_provider(f"alibaba-coding-plan {K1}")
        assert provider == "alibaba-coding-plan"
        assert payload == K1

        out = cmd.handle(f"add alibaba-coding-plan {K1}")
        assert "to alibaba-coding-plan" in out
        stored = [e.access_token for e in pool._all["alibaba-coding-plan"].entries()]
        assert stored == [K1]

    def test_unknown_word_is_not_treated_as_a_provider(self, pool):
        # A typo'd provider must not silently create a new pool; it falls
        # through to the default, where the word is then rejected as junk.
        provider, payload = cmd._split_provider(f"gemeni {K1}")
        assert provider == "gemini"
        assert "gemeni" in payload


class TestReportingARowThatHoldsSeveralKeys:
    """What ``/kame-keys status`` says about a comma-separated env var."""

    KEYS = [f"AIzaSyStatusKey{n}" + "y" * 23 for n in range(3)]

    def test_the_count_is_of_keys_not_of_rows(self, pool):
        pool.add_entry(FakeEntry("gemini", ",".join(self.KEYS), "GOOGLE_API_KEY"))
        for n, key in enumerate(self.KEYS, start=1):
            pool.add_entry(FakeEntry("gemini", key, f"GOOGLE_API_KEY ({n}/3)"))
        out = cmd.handle("status")
        assert "gemini — 3 key(s)" in out

    def test_the_container_is_named_as_one(self, pool):
        pool.add_entry(FakeEntry("gemini", ",".join(self.KEYS), "GOOGLE_API_KEY"))
        out = cmd.handle("status")
        assert "[list] GOOGLE_API_KEY" in out
        assert "holds 3 keys" in out

    def test_it_is_not_reported_as_a_working_credential(self, pool):
        """Before this, one malformed row read as ``1 key(s)`` and ``[ok]``."""
        pool.add_entry(FakeEntry("gemini", ",".join(self.KEYS), "GOOGLE_API_KEY"))
        out = cmd.handle("status")
        assert "[ok] GOOGLE_API_KEY" not in out
        assert "1 key(s)" not in out

    def test_no_key_appears_in_the_report(self, pool):
        pool.add_entry(FakeEntry("gemini", ",".join(self.KEYS), "GOOGLE_API_KEY"))
        out = cmd.handle("status")
        assert not any(key in out for key in self.KEYS)

    def test_an_ordinary_pool_still_reads_the_way_it_did(self, pool):
        pool.add_entry(FakeEntry("gemini", self.KEYS[0], "kame #1"))
        pool.add_entry(FakeEntry("gemini", self.KEYS[1], "kame #2"))
        out = cmd.handle("status")
        assert "gemini — 2 key(s)" in out
        assert "[list]" not in out
