"""The journal and the report, with no Hermes and no plugin adapter involved.

These are the rules that decide whether a prediction was wrong, and the words
a person reads when they ask. Both have to hold on their own — the whole
point of keeping them in ``core`` is that a later Agent Zero build inherits
them without inheriting a Hermes binding.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
sys.path.insert(0, str(ROOT))

from tests.test_binding import PACKAGE  # noqa: E402  (loads the package once)

journal_module = importlib.import_module(f"{PACKAGE}.core.journal")
report = importlib.import_module(f"{PACKAGE}.core.report")
tally = importlib.import_module(f"{PACKAGE}.core.tally")
ledger_module = importlib.import_module(f"{PACKAGE}.core.ledger")

Journal = journal_module.Journal
Block = journal_module.Block
Recovery = journal_module.Recovery
summarize = journal_module.summarize
SIZED_BY_KAME = journal_module.SIZED_BY_KAME
SIZED_BY_HOST = journal_module.SIZED_BY_HOST
SIZED_BY_DROPPED = journal_module.SIZED_BY_DROPPED
Ledger = ledger_module.Ledger

NOW = 1_000_000.0
MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0
MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"


def block(book, *, at, model=MAIN, credential="k0", reset_after=HOUR, **kwargs):
    return book.record_block(
        at=at,
        provider="gemini",
        model=model,
        credential_id=credential,
        reset_at=None if reset_after is None else at + reset_after,
        **kwargs,
    )


class TestRecordingARefusal:
    def test_a_block_round_trips_through_storage(self):
        book = Journal()
        block(book, at=NOW, window="per_day", source="body", sized_by=SIZED_BY_KAME)
        restored = Journal.from_dict(book.to_dict())
        row = restored.blocks()[0]
        assert (row.credential_id, row.model, row.window) == ("k0", MAIN, "per_day")
        assert row.sized_by == SIZED_BY_KAME
        assert row.predicted_seconds == HOUR

    def test_the_model_spelling_is_normalised_on_the_way_in(self):
        book = Journal()
        block(book, at=NOW, model=f"models/{MAIN}")
        assert book.blocks()[0].model == MAIN
        assert book.last_block("k0", f"gemini/{MAIN}") is not None

    def test_a_row_without_an_identity_is_refused(self):
        book = Journal()
        assert block(book, at=NOW, credential="") is None
        assert block(book, at=NOW, model="") is None
        assert book.blocks() == []

    def test_out_of_order_arrivals_are_re_sorted(self):
        # A clock adjustment, or a record replayed from another process.
        # Every reader here assumes oldest-first.
        book = Journal()
        block(book, at=NOW)
        block(book, at=NOW - MINUTE)
        assert [row.at for row in book.blocks()] == [NOW - MINUTE, NOW]

    def test_a_garbage_document_yields_an_empty_journal(self):
        for payload in (None, [], {"version": 99}, {"version": 1, "blocks": "no"}):
            assert Journal.from_dict(payload).blocks() == []

    def test_one_unreadable_row_does_not_discard_the_rest(self):
        book = Journal()
        block(book, at=NOW)
        payload = book.to_dict()
        payload["blocks"].append({"credential_id": "", "at": "nonsense"})
        assert len(Journal.from_dict(payload).blocks()) == 1


class TestForgettingOnSchedule:
    def test_records_past_the_horizon_are_dropped(self):
        book = Journal()
        block(book, at=NOW - journal_module.MAX_AGE_SECONDS - HOUR)
        block(book, at=NOW)
        book.prune(NOW)
        assert [row.at for row in book.blocks()] == [NOW]

    def test_the_cap_bites_oldest_first(self):
        book = Journal()
        for index in range(journal_module.MAX_BLOCKS + 20):
            block(book, at=NOW + index, credential=f"k{index}")
        rows = book.blocks()
        assert len(rows) == journal_module.MAX_BLOCKS
        assert rows[0].at == NOW + 20

    def test_a_removed_credential_takes_its_history_with_it(self):
        book = Journal()
        block(book, at=NOW, credential="k0")
        block(book, at=NOW, credential="k1")
        book.record_success(at=NOW + MINUTE, provider="gemini", model=MAIN, credential_id="k0")
        assert book.forget_credential("k0") == 2
        assert [row.credential_id for row in book.blocks()] == ["k1"]
        assert book.recoveries() == []


class TestPairingASuccessBack:
    def test_a_success_after_a_block_measures_the_gap(self):
        book = Journal()
        block(book, at=NOW, reset_after=MINUTE)
        recovery = book.record_success(
            at=NOW + 90.0, provider="gemini", model=MAIN, credential_id="k0"
        )
        assert recovery.observed_seconds == 90.0
        assert recovery.was_early is False

    def test_a_success_before_the_deadline_is_early(self):
        book = Journal()
        block(book, at=NOW, reset_after=DAY)
        recovery = book.record_success(
            at=NOW + HOUR, provider="gemini", model=MAIN, credential_id="k0"
        )
        assert recovery.was_early is True

    def test_a_success_with_nothing_to_answer_is_ignored(self):
        book = Journal()
        assert book.record_success(
            at=NOW, provider="gemini", model=MAIN, credential_id="k0"
        ) is None

    def test_a_second_success_adds_nothing(self):
        book = Journal()
        block(book, at=NOW, reset_after=MINUTE)
        book.record_success(at=NOW + 90.0, provider="gemini", model=MAIN, credential_id="k0")
        assert book.record_success(
            at=NOW + 120.0, provider="gemini", model=MAIN, credential_id="k0"
        ) is None

    def test_a_new_block_reopens_the_question(self):
        book = Journal()
        block(book, at=NOW, reset_after=MINUTE)
        book.record_success(at=NOW + 90.0, provider="gemini", model=MAIN, credential_id="k0")
        block(book, at=NOW + 600.0, reset_after=MINUTE)
        recovery = book.record_success(
            at=NOW + 700.0, provider="gemini", model=MAIN, credential_id="k0"
        )
        assert recovery is not None
        assert recovery.blocked_at == NOW + 600.0

    def test_a_success_before_the_block_is_not_a_recovery(self):
        # Clock skew, or a record that arrived out of order. A negative
        # duration would poison every statistic downstream.
        book = Journal()
        block(book, at=NOW)
        assert book.record_success(
            at=NOW - MINUTE, provider="gemini", model=MAIN, credential_id="k0"
        ) is None

    def test_a_success_long_after_a_forgotten_world_is_ignored(self):
        book = Journal()
        book._blocks.append(
            Block(
                at=NOW,
                provider="gemini",
                model=MAIN,
                credential_id="k0",
                reset_at=NOW + HOUR,
            )
        )
        assert book.record_success(
            at=NOW + journal_module.MAX_AGE_SECONDS + HOUR,
            provider="gemini",
            model=MAIN,
            credential_id="k0",
        ) is None


class TestCountingTheMistakes:
    def test_a_repeat_right_after_the_deadline_is_an_under_prediction(self):
        book = Journal()
        block(book, at=NOW, reset_after=MINUTE, window="per_minute")
        block(book, at=NOW + MINUTE + 5.0, reset_after=MINUTE, window="per_minute")
        stat = summarize(book, now=NOW + HOUR)[0]
        assert stat.under_predictions == 1
        assert stat.blocks == 2

    def test_a_repeat_long_after_the_deadline_is_not(self):
        book = Journal()
        block(book, at=NOW, reset_after=MINUTE, window="per_minute")
        block(book, at=NOW + HOUR, reset_after=MINUTE, window="per_minute")
        assert summarize(book, now=NOW + 2 * HOUR)[0].under_predictions == 0

    def test_one_repeat_is_not_enough_to_call_it(self):
        book = Journal()
        block(book, at=NOW, reset_after=MINUTE, window="per_minute")
        block(book, at=NOW + MINUTE + 5.0, reset_after=MINUTE, window="per_minute")
        assert summarize(book, now=NOW + HOUR)[0].looks_short is False

    def test_two_repeats_are(self):
        book = Journal()
        at = NOW
        for _ in range(3):
            block(book, at=at, reset_after=MINUTE, window="per_minute")
            at += MINUTE + 5.0
        stat = summarize(book, now=at + HOUR)[0]
        assert stat.under_predictions == 2
        assert stat.looks_short is True

    def test_a_repeat_on_a_different_key_is_not_charged_to_the_first(self):
        book = Journal()
        block(book, at=NOW, credential="k0", reset_after=MINUTE, window="per_minute")
        block(book, at=NOW + MINUTE + 5.0, credential="k1", reset_after=MINUTE,
              window="per_minute")
        assert summarize(book, now=NOW + HOUR)[0].under_predictions == 0

    def test_windows_are_counted_apart(self):
        book = Journal()
        block(book, at=NOW, reset_after=MINUTE, window="per_minute")
        block(book, at=NOW + HOUR, reset_after=DAY, window="per_day")
        stats = {stat.window: stat for stat in summarize(book, now=NOW + 2 * HOUR)}
        assert set(stats) == {"per_minute", "per_day"}
        assert stats["per_day"].longest_predicted == DAY

    def test_models_are_counted_apart(self):
        book = Journal()
        block(book, at=NOW, model=MAIN)
        block(book, at=NOW, model=AUX)
        assert len(summarize(book, now=NOW + HOUR)) == 2

    def test_early_recoveries_are_counted(self):
        book = Journal()
        for index, credential in enumerate(("k0", "k1")):
            block(book, at=NOW + index, credential=credential, reset_after=DAY,
                  window="per_day")
            book.record_success(
                at=NOW + HOUR, provider="gemini", model=MAIN, credential_id=credential
            )
        stat = summarize(book, now=NOW + 2 * HOUR)[0]
        assert stat.early_recoveries == 2
        assert stat.looks_long is True
        assert stat.fastest_recovery == pytest.approx(HOUR, abs=2.0)

    def test_stale_records_do_not_vote(self):
        book = Journal()
        book._blocks.append(
            Block(at=NOW - 2 * journal_module.MAX_AGE_SECONDS, provider="gemini",
                  model=MAIN, credential_id="k0", reset_at=NOW)
        )
        assert summarize(book, now=NOW) == []

    def test_who_sized_the_bench_is_counted(self):
        book = Journal()
        block(book, at=NOW, sized_by=SIZED_BY_KAME)
        block(book, at=NOW + HOUR, sized_by=SIZED_BY_HOST)
        stat = summarize(book, now=NOW + 2 * HOUR)[0]
        assert (stat.blocks, stat.kame_sized) == (2, 1)

    def test_a_deadline_that_was_dropped_is_counted_apart_from_both(self):
        # It is not KAME's bench — it never governed — and it is not the host
        # having been left to it either. Counting it as the second hides the
        # difference between a plugin staying out of the way and a plugin
        # whose every number is being discarded.
        book = Journal()
        block(book, at=NOW, sized_by=SIZED_BY_KAME)
        block(book, at=NOW + HOUR, sized_by=SIZED_BY_DROPPED)
        block(book, at=NOW + 2 * HOUR, sized_by=SIZED_BY_HOST)
        stat = summarize(book, now=NOW + 3 * HOUR)[0]
        assert (stat.blocks, stat.kame_sized, stat.kame_dropped) == (3, 1, 1)

    def test_every_deadline_dropped_and_none_kept_is_called_out(self):
        book = Journal()
        for index in range(2):
            block(book, at=NOW + index * HOUR, sized_by=SIZED_BY_DROPPED)
        assert summarize(book, now=NOW + 2 * HOUR)[0].looks_ignored is True

    def test_but_not_while_any_of_them_is_getting_through(self):
        # One dropped deadline is a clamp or a race, and the plugin is
        # plainly working. The alarm is for none getting through at all.
        book = Journal()
        block(book, at=NOW, sized_by=SIZED_BY_DROPPED)
        block(book, at=NOW + HOUR, sized_by=SIZED_BY_DROPPED)
        block(book, at=NOW + 2 * HOUR, sized_by=SIZED_BY_KAME)
        assert summarize(book, now=NOW + 3 * HOUR)[0].looks_ignored is False

    def test_a_word_this_version_does_not_know_is_not_read_as_ours(self):
        # A row from a newer build, or a corrupted one. The measurement acts
        # on `kame`, so an unknown value must fall to the claim that says
        # least — never to the one that says KAME governed this bench.
        restored = Block.from_dict(
            {"at": NOW, "model": MAIN, "credential_id": "k0", "sized_by": "whatever"}
        )
        assert restored.sized_by == SIZED_BY_HOST


class TestTheReport:
    def test_durations_read_like_a_person_wrote_them(self):
        assert report.humanize(42) == "42s"
        assert report.humanize(90) == "1m 30s"
        assert report.humanize(3 * HOUR + 20 * MINUTE) == "3h 20m"
        assert report.humanize(DAY + 2 * HOUR) == "1d 2h"
        assert report.humanize(None) == "?"

    def test_an_empty_state_says_so_instead_of_showing_nothing(self):
        text = report.render(Ledger(), Journal(), now=NOW)
        assert "nothing is benched" in text
        assert "no real refusal has passed through" in text

    def test_a_bench_is_shown_by_label_and_time_left(self):
        pool_ledger = Ledger()
        pool_ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 18 * MINUTE, now=NOW,
        )
        text = report.render(
            pool_ledger, Journal(), now=NOW, labels={"k0": "GOOGLE_API_KEY"}
        )
        assert "GOOGLE_API_KEY" in text
        assert "18m" in text
        assert MAIN in text

    def test_a_key_with_no_label_is_shown_by_a_short_id_not_a_token(self):
        pool_ledger = Ledger()
        pool_ledger.record(
            credential_id="379e9e0011", provider="gemini", model=MAIN,
            reset_at=NOW + HOUR, now=NOW,
        )
        text = report.render(pool_ledger, Journal(), now=NOW)
        assert "379e9e00" in text
        assert "379e9e0011" not in text

    def test_an_under_predicted_window_is_called_out(self):
        book = Journal()
        at = NOW
        for _ in range(3):
            block(book, at=at, reset_after=MINUTE, window="per_minute")
            at += MINUTE + 5.0
        text = report.render(Ledger(), book, now=at)
        assert "looks longer" in text

    def test_a_plugin_whose_numbers_are_all_being_dropped_says_so(self):
        # The one line here that is about KAME rather than about the
        # provider. Without it the report of an inert plugin is indis-
        # tinguishable from the report of a quiet one.
        book = Journal()
        for index in range(2):
            block(book, at=NOW + index * HOUR, sized_by=SIZED_BY_DROPPED)
        text = report.render(Ledger(), book, now=NOW + 2 * HOUR)
        assert "did not keep" in text
        assert "the cooldowns you are seeing are the host's" in text

    def test_and_a_working_plugin_does_not(self):
        book = Journal()
        for index in range(2):
            block(book, at=NOW + index * HOUR, sized_by=SIZED_BY_KAME)
        text = report.render(Ledger(), book, now=NOW + 2 * HOUR)
        assert "did not keep" not in text
        assert "2 sized by KAME" in text

    def test_the_report_says_it_only_watches(self):
        text = report.render(Ledger(), Journal(), now=NOW, footer="observations only")
        assert "observations only" in text


class TestTheSpreadSection:
    """The only part of the report that shows the plugin working.

    Everything above it is about refusals. This is what the user asked to be
    able to see: rotation happening, on keys nothing is wrong with.
    """

    def test_a_quiet_minute_says_so_rather_than_showing_a_blank(self):
        text = report.render(Ledger(), Journal(), now=NOW)
        assert "How the requests are spread" in text
        assert "nothing has been handed out yet" in text

    def test_each_key_is_shown_with_its_share(self):
        text = report.render(
            Ledger(), Journal(), now=NOW,
            spread={f"gemini:{MAIN}": {"#aa": 3, "#bb": 1}},
            names={"#aa": "GOOGLE_API_KEY[1]", "#bb": "GOOGLE_API_KEY[2]"},
        )
        assert "gemini · " + MAIN in text
        assert "GOOGLE_API_KEY[1]    3 requests" in text
        assert "GOOGLE_API_KEY[2]    1 request" in text

    def test_the_busiest_key_is_the_first_line(self):
        # The question this section answers is "is one key taking everything?"
        # and the answer has to be readable without counting rows.
        lines = report.render_spread({f"gemini:{MAIN}": {"#quiet": 1, "#busy": 9}})
        assert "#busy" in lines[1]
        assert "#quiet" in lines[2]

    def test_an_unlabelled_key_is_shown_by_a_short_hash_not_a_token(self):
        mark = "#0123456789abcdef"
        lines = report.render_spread({f"gemini:{MAIN}": {mark: 2}})
        assert "#0123456" in lines[1]
        assert mark not in lines[1]

    def test_a_provider_with_no_model_still_gets_a_heading(self):
        # bucket_for falls back to the bare provider when no model has been
        # announced, and a heading reading "openai · " would look like a bug.
        lines = report.render_spread({"openai": {"#aa": 1}})
        assert lines[0].strip() == "openai"

    def test_a_bucket_that_emptied_out_does_not_print_a_bare_heading(self):
        assert report.render_spread({"gemini:x": {}}) == [
            "  nothing has been handed out yet"
        ]

    def test_the_section_holds_no_key_material(self):
        text = report.render(
            Ledger(), Journal(), now=NOW,
            spread={f"gemini:{MAIN}": {"#aa": 1}},
            names={"#aa": "GOOGLE_API_KEY[1]"},
        )
        assert "sk-" not in text


class TestTheAskedSection:
    """The count that tells a working install from an inert one.

    It has to say the normal thing normally — most failures are declined on
    purpose — and point at exactly one case: a rate limit whose wait KAME
    could not read, which is the whole job on that status.
    """

    def _seen(self, provider, status, total, sized):
        return tally.Seen(provider=provider, status_code=status, total=total, sized=sized)

    def test_a_quiet_process_says_so_rather_than_showing_a_blank(self):
        text = report.render(Ledger(), Journal(), now=NOW)
        assert "What KAME was asked (since Hermes started)" in text
        assert "nothing has failed yet since Hermes started" in text

    def test_it_shows_how_much_of_what_it_saw_it_could_read(self):
        lines = report.render_asked([self._seen("gemini", 429, 7, 5)])
        assert "gemini" in lines[0]
        assert "429" in lines[0]
        assert "×7" in lines[0]
        assert "5 sized" in lines[0]

    def test_declining_everything_on_an_auth_failure_reads_as_normal(self):
        lines = report.render_asked([self._seen("anthropic", 401, 12, 0)])
        assert "left to the host" in lines[0]
        assert "could not read" not in lines[0]

    def test_a_rate_limit_it_never_read_is_pointed_at(self):
        lines = report.render_asked([self._seen("gemini", 429, 12, 0)])
        assert "a wait KAME could not read" in lines[0]

    def test_and_one_reading_takes_the_flag_away(self):
        lines = report.render_asked([self._seen("gemini", 429, 12, 1)])
        assert "could not read" not in lines[0]

    def test_a_failure_with_no_status_still_prints_a_row(self):
        lines = report.render_asked([self._seen("gemini", None, 3, 0)])
        assert "?" in lines[0] and "×3" in lines[0]


class TestTheSpreadSectionOverTime:
    """Two numbers per key: the minute that decides, and the run since start.

    The window is what selection reads. It is also sixty seconds long, which
    is a poor thing to ask somebody to catch the pool inside of.
    """

    def test_a_key_that_is_quiet_now_still_shows_what_it_has_taken(self):
        lines = report.render_spread(
            {}, totals={f"gemini:{MAIN}": {"#aa": 41}}, names={"#aa": "KEY[1]"}
        )
        assert "idle" in lines[1]
        assert "41 since Hermes started" in lines[1]

    def test_both_numbers_are_shown_when_the_key_is_busy(self):
        lines = report.render_spread(
            {f"gemini:{MAIN}": {"#aa": 3}}, totals={f"gemini:{MAIN}": {"#aa": 41}}
        )
        assert "3 requests" in lines[1] and "41 since Hermes started" in lines[1]

    def test_a_key_that_has_never_been_picked_is_visible_beside_one_that_has(self):
        # The case the whole section exists for: fifteen keys, one of them
        # taking everything. Ordering by the minute alone would hide it.
        lines = report.render_spread(
            {f"gemini:{MAIN}": {"#busy": 2}},
            totals={f"gemini:{MAIN}": {"#busy": 900, "#never": 0}},
        )
        assert len(lines) == 3
        assert "#busy" in lines[1] and "#never" in lines[2]

    def test_nothing_at_all_says_so(self):
        assert report.render_spread({}, totals={}) == ["  nothing has been handed out yet"]

    def test_a_model_tag_with_a_colon_in_it_survives_the_heading(self):
        # Ollama names models `llama3:8b`, and the bucket joins provider and
        # model with a colon. Splitting on the last one would cut the tag.
        lines = report.render_spread({"ollama:llama3:8b": {"#aa": 1}})
        assert lines[0].strip() == "ollama · llama3:8b"


class TestTheEmptyAnswerLines:
    """Calls that came back with nothing in them.

    They belong in the "asked" section because they answer the same question
    — what reached KAME and what it could make of it — and they must be
    absent entirely on a healthy install, because a line saying "0" about a
    thing that never happens is noise in a report read during an incident.
    """

    def _seen(self, provider, total):
        return tally.Seen(provider=provider, status_code=None, total=total, sized=0)

    def test_a_healthy_install_shows_no_line_at_all(self):
        assert report.render_quiet([]) == []
        assert report.render_quiet(None) == []

    def test_it_says_what_was_seen_and_what_was_not_concluded(self):
        lines = report.render_quiet([self._seen("gemini", 4)])
        assert "gemini" in lines[0]
        assert "×4" in lines[0]
        assert "not counted as proof the key works" in lines[0]

    def test_it_rides_in_the_asked_section(self):
        text = report.render(
            Ledger(), Journal(), now=NOW, quiet=[self._seen("gemini", 2)]
        )
        asked = text.index("What KAME was asked (since Hermes started)")
        learned = text.index("What KAME has seen (last 14 days)")
        assert asked < text.index("answered with nothing") < learned

    def test_it_never_claims_a_failure_happened(self):
        # Nothing refused anything here. A line implying a provider said no
        # would put a number in front of a reader that no provider ever sent.
        line = report.render_quiet([self._seen("gemini", 2)])[0]
        assert "429" not in line
        assert "sized" not in line
