"""Tests for bulk key parsing. Pure functions, no Hermes, no real credentials.

Every "key" here is fabricated. The shapes mimic real providers because the
parser's job is to survive whatever the user actually pastes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation")
)

from core.keys import (  # noqa: E402
    MIN_KEY_LENGTH,
    build_labels,
    decode_text,
    format_plan_lines,
    count_usable,
    group_status,
    looks_like_api_key,
    parse_keys,
    plan_import,
    redact,
)

# Fabricated, correct-shaped keys. 39 chars, `AIza` prefix, like Google's.
K1 = "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
K2 = "AIzaSyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
K3 = "AIzaSyCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"


class TestRedact:
    def test_shows_head_and_tail_only(self):
        out = redact(K1)
        assert out.startswith("AIzaSy")
        assert out.endswith(K1[-4:])
        assert K1 not in out

    def test_short_secrets_are_fully_masked(self):
        # Showing 6 of 10 characters would give most of a short secret away.
        assert redact("abc123") == "******"

    def test_empty(self):
        assert redact("") == ""
        assert redact(None) == ""  # type: ignore[arg-type]


class TestParseKeys:
    def test_comma_separated_like_agent_zero(self):
        keys, rejected = parse_keys(f"{K1},{K2},{K3}")
        assert keys == [K1, K2, K3]
        assert rejected == []

    @pytest.mark.parametrize("sep", [",", ", ", "\n", "\r\n", ";", " ", "\t", " | "])
    def test_every_plausible_separator(self, sep):
        keys, _ = parse_keys(f"{K1}{sep}{K2}")
        assert keys == [K1, K2]

    def test_mixed_separators_in_one_paste(self):
        keys, _ = parse_keys(f"{K1}, {K2}\n{K3};")
        assert keys == [K1, K2, K3]

    def test_deduplicates_within_input(self):
        keys, _ = parse_keys(f"{K1},{K1},{K2}")
        assert keys == [K1, K2]

    def test_preserves_order(self):
        keys, _ = parse_keys(f"{K3},{K1},{K2}")
        assert keys == [K3, K1, K2]

    def test_unwraps_env_assignment(self):
        # Pasting straight out of a .env file is the obvious thing to try.
        keys, _ = parse_keys(f"GEMINI_API_KEY={K1}\nGEMINI_API_KEY_2={K2}")
        assert keys == [K1, K2]

    def test_unwraps_yaml_assignment_and_quotes(self):
        keys, _ = parse_keys(f'gemini_key: "{K1}"\nother: \'{K2}\'')
        assert keys == [K1, K2]

    def test_rejects_urls_and_comments(self):
        keys, rejected = parse_keys(
            f"# my keys\n{K1}\nhttps://example.com/some/long/path/here\n{K2}"
        )
        assert keys == [K1, K2]
        assert len(rejected) == 1

    def test_empty_input(self):
        assert parse_keys("") == ([], [])
        assert parse_keys(None) == ([], [])  # type: ignore[arg-type]

    def test_junk_only(self):
        keys, rejected = parse_keys("hello there")
        assert keys == []
        # Both words are too short to be keys, so both are dropped as junk
        # rather than silently accepted.
        assert rejected == []


class TestLooksLikeApiKey:
    def test_accepts_realistic_shapes(self):
        for key in (K1, "sk-" + "a" * 40, "sk-or-v1-" + "b" * 32):
            assert looks_like_api_key(key)

    def test_rejects_too_short(self):
        assert not looks_like_api_key("a" * (MIN_KEY_LENGTH - 1))

    def test_rejects_internal_whitespace(self):
        assert not looks_like_api_key("AIzaSy AAAA BBBB CCCC DDDD")

    def test_rejects_smart_quotes_from_mangled_paste(self):
        # A curly quote survives length and separator checks, then fails at the
        # provider hours later with an opaque 401.
        assert not looks_like_api_key("AIzaSy’AAAAAAAAAAAAAAAAAAAAAAAAA")

    def test_rejects_url(self):
        assert not looks_like_api_key("https://generativelanguage.googleapis.com")


class TestPlanImport:
    def test_splits_new_from_existing(self):
        plan = plan_import(f"{K1},{K2},{K3}", existing=[K2])
        assert plan.new == [K1, K3]
        assert plan.duplicates == [K2]

    def test_rerunning_an_import_adds_nothing(self):
        # The property that makes bulk import safe to retry.
        plan = plan_import(f"{K1},{K2}", existing=[K1, K2])
        assert plan.is_empty
        assert plan.new == []

    def test_ignores_blank_existing_entries(self):
        plan = plan_import(K1, existing=["", "   ", None])  # type: ignore[list-item]
        assert plan.new == [K1]

    def test_summary_counts(self):
        plan = plan_import(f"{K1},{K2},short", existing=[K2])
        summary = plan.summary()
        assert "1 new" in summary
        assert "1 already in pool" in summary


class TestFormatting:
    def test_plan_lines_never_leak_a_key(self):
        plan = plan_import(f"{K1},{K2}", existing=[K2])
        text = "\n".join(format_plan_lines(plan))
        assert K1 not in text
        assert K2 not in text
        assert "AIzaSy" in text  # still recognisable

    def test_rejected_tokens_are_redacted_too(self):
        secret = "AIzaSyLEAKED_BUT_MALFORMED_KEY_WITH_SPACE"
        plan = plan_import(f"{secret[:20]} {secret[20:]}", existing=[])
        text = "\n".join(format_plan_lines(plan))
        assert secret not in text

    def test_status_lines(self):
        lines = group_status([
            {"label": "kame #1", "token_preview": "AIzaSy…AAAA",
             "last_status": "exhausted", "request_count": 42},
        ])
        assert "exhausted" in lines[0]
        assert "kame #1" in lines[0]
        assert "42" in lines[0]

    def test_an_unusable_entry_does_not_report_its_stale_status(self):
        lines = group_status([
            {"label": "GOOGLE_API_KEY", "token_preview": "",
             "last_status": "ok", "request_count": 0, "usable": False},
        ])
        assert "[no key]" in lines[0]
        assert "[ok]" not in lines[0]

    def test_the_flag_is_opt_in(self):
        # Absent the key, an entry reads exactly the way it always did — no
        # caller that predates the distinction changes behaviour.
        plain = group_status([{"label": "a", "token_preview": "x", "last_status": None}])
        flagged = group_status([
            {"label": "a", "token_preview": "x", "last_status": None, "usable": True},
        ])
        assert plain == flagged
        assert "[ok]" in plain[0]


class TestCountUsable:
    def test_counts_only_what_the_pool_could_pick(self):
        assert count_usable([{"usable": True}, {"usable": False}, {"usable": True}]) == 2

    def test_an_unflagged_entry_counts(self):
        assert count_usable([{"label": "a"}, {"label": "b"}]) == 2

    def test_empty(self):
        assert count_usable([]) == 0


class TestBuildLabels:
    def test_sequential(self):
        assert build_labels(3) == ["kame #1", "kame #2", "kame #3"]

    def test_skips_taken_labels(self):
        labels = build_labels(2, taken=["kame #1", "kame #2"])
        assert labels == ["kame #3", "kame #4"]

    def test_zero_and_negative(self):
        assert build_labels(0) == []
        assert build_labels(-5) == []


class TestTheMarkWindowsPutsAtTheStartOfFiles:
    """A byte-order mark is not part of anything, and it was costing keys.

    Not a hypothetical: these payloads are what PowerShell writes. A file
    saved with ``Set-Content -Encoding utf8BOM`` — which is what Notepad's
    "UTF-8 with BOM" produces — begins with three bytes that, read as UTF-8,
    become a zero-width character glued to the first key. It survives every
    ``strip()``, it is invisible in an editor, and it makes the key fail the
    printable-ASCII test. The user sees one key rejected out of a file that
    looks perfect.

    ``Set-Content -Encoding unicode``, and Windows PowerShell's plain ``>``
    redirect, are worse: UTF-16, read as UTF-8, is mojibake from the first
    byte and **every** key is lost.
    """

    PAIR = f"{K1}\r\n{K2}\r\n"

    def test_utf8_with_a_mark_keeps_both_keys(self):
        data = "\ufeff".encode("utf-8") + self.PAIR.encode("utf-8")
        assert parse_keys(decode_text(data))[0] == [K1, K2]

    def test_utf16_keeps_both_keys(self):
        for encoding in ("utf-16-le", "utf-16-be"):
            data = self.PAIR.encode(encoding)
            data = (b"\xff\xfe" if encoding.endswith("le") else b"\xfe\xff") + data
            assert parse_keys(decode_text(data))[0] == [K1, K2], encoding

    def test_utf32_is_not_read_as_utf16(self):
        # ``BOM_UTF32_LE`` starts with the two bytes of ``BOM_UTF16_LE``, so
        # the wrong order here decodes silently and wrongly rather than
        # failing — the kind of bug that only shows up as lost keys.
        import codecs

        data = codecs.BOM_UTF32_LE + self.PAIR.encode("utf-32-le")
        assert parse_keys(decode_text(data))[0] == [K1, K2]

    def test_a_plain_utf8_file_is_unchanged(self):
        assert parse_keys(decode_text(self.PAIR.encode("utf-8")))[0] == [K1, K2]

    def test_a_mark_that_arrives_in_a_paste_is_still_stripped(self):
        # Copying out of a marked file carries the character into the
        # clipboard, and then no amount of decoding helps — it arrives as
        # text. ``/kame-keys add`` is that path.
        assert parse_keys(f"\ufeff{K1},{K2}")[0] == [K1, K2]

    def test_something_that_is_not_text_reports_rather_than_raises(self):
        # A slash command that throws inside a chat turn is worse than one
        # that reports what it found. Every byte value, decoded with
        # replacement, has to come back as an ordinary parse result — and
        # note it is *not* empty: a run of printable ASCII inside a binary
        # file does look like a key, and saying so is the honest answer.
        keys, rejected = parse_keys(decode_text(bytes(range(0, 256))))
        assert isinstance(keys, list) and isinstance(rejected, list)
        assert all(len(key) >= MIN_KEY_LENGTH for key in keys)

    def test_a_string_that_never_was_bytes_survives(self):
        assert decode_text(f"{K1},{K2}") == f"{K1},{K2}"


class TestARowThatHoldsSeveralKeys:
    """A provider env var set to ``k1,k2,k3`` is stored by Hermes as one
    credential. Redacted, that row prints as ``AIzaSy…cccc`` — the start of
    the first key and the end of the last — which is indistinguishable from
    an ordinary credential. The report has to say what it is."""

    ROW = {"label": "GOOGLE_API_KEY", "token_preview": "AIzaSy…cccc", "holds": 3}

    def test_it_is_not_shown_as_a_key(self):
        line = group_status([self.ROW])[0]
        assert "[list]" in line
        assert "holds 3 keys" in line

    def test_it_does_not_print_a_preview_that_looks_like_a_credential(self):
        assert "AIzaSy…cccc" not in group_status([self.ROW])[0]

    def test_it_counts_as_none_of_them(self):
        """The keys inside are counted where they appear — as their own rows."""
        assert count_usable([self.ROW]) == 0

    def test_the_keys_inside_are_still_counted(self):
        entries = [self.ROW, {"label": "a"}, {"label": "b"}, {"label": "c"}]
        assert count_usable(entries) == 3

    def test_a_row_holding_one_key_is_an_ordinary_key(self):
        line = group_status([{"label": "K", "token_preview": "ab…cd", "holds": 1}])[0]
        assert "[ok]" in line
        assert "[list]" not in line

    def test_the_flag_is_opt_in(self):
        """Every caller that does not know about the distinction reads the
        way it always did."""
        assert count_usable([{"label": "a"}, {"label": "b"}]) == 2
        assert "[list]" not in group_status([{"label": "a"}])[0]
