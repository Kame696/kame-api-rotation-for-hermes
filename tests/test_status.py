"""``/kame-quota`` — the only way to see what the plugin is doing.

The interesting half of this plugin is invisible: a cooldown sized right and
a cooldown sized wrong both look like a call that worked, or did not. These
tests hold the command to the two things that make it worth having — it tells
the truth about state, and it cannot throw inside a chat turn.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_binding import PACKAGE, FakeState  # noqa: E402

status = importlib.import_module(f"{PACKAGE}.status")
store_module = importlib.import_module(f"{PACKAGE}.store")
journal_module = importlib.import_module(f"{PACKAGE}.core.journal")

LedgerStore = store_module.LedgerStore
JournalStore = store_module.JournalStore

NOW = 1_000_000.0
HOUR = 3600.0
MAIN = "gemini-3.6-flash"


class FakeBinding:
    """Only the two attributes the command reads off a live binding."""

    def __init__(self, state) -> None:
        self._store = LedgerStore(state, ttl_seconds=0.0)
        self._journal = JournalStore(state, ttl_seconds=0.0)


class FakeContext:
    def __init__(self) -> None:
        self.commands = {}

    def register_command(self, name, handler, description="", args_hint="") -> None:
        self.commands[name] = handler


def _command():
    state = FakeState()
    binding = FakeBinding(state)
    return status.QuotaCommand(binding, clock=lambda: NOW), binding, state


class TestSayingWhatIsTrue:
    def test_an_untethered_plugin_says_which_half_is_running(self):
        # Cooldown sizing without the pool binding is a supported mode, not a
        # failure. The report has to name it rather than print an empty page
        # that reads like nothing is wrong.
        text = status.QuotaCommand(None).handle("")
        assert "not active" in text
        assert "still doing its first job" in text

    def test_a_fresh_install_reports_an_empty_slate(self):
        command, _binding, _state = _command()
        text = command.handle("")
        assert "nothing is benched" in text
        assert "no real refusal has passed through" in text

    def test_a_live_bench_is_shown(self):
        command, binding, _state = _command()
        book = binding._store.load()
        book.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + HOUR, now=NOW,
        )
        binding._store.save(book, now=NOW)

        text = command.handle("")
        assert MAIN in text
        assert "1h" in text

    def test_a_bench_being_tested_says_so(self):
        # A bench alone raises the question "is my agent just sitting there?"
        # The count is the answer: the deadline is being checked, not obeyed.
        command, binding, _state = _command()
        book = binding._store.load()
        book.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 12 * HOUR, now=NOW - HOUR,
        )
        book.note_probe("k0", MAIN, NOW - 600)
        binding._store.save(book, now=NOW)
        assert "tested 1×" in command.handle("")

    def test_a_bench_that_covers_the_whole_key_says_which(self):
        # Without this the line reads as "one model is out" when in fact the
        # key is out everywhere — the report would understate the situation.
        command, binding, _state = _command()
        book = binding._store.load()
        book.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + HOUR, now=NOW, scope="account",
        )
        binding._store.save(book, now=NOW)
        assert "all models" in command.handle("")

    def test_an_untested_bench_says_nothing_extra(self):
        command, binding, _state = _command()
        book = binding._store.load()
        book.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + HOUR, now=NOW,
        )
        binding._store.save(book, now=NOW)
        bench_line = next(line for line in command.handle("").splitlines() if "free in" in line)
        assert "tested" not in bench_line

    def test_the_report_separates_what_watches_from_what_acts(self):
        # The footer used to claim the whole page changed nothing. That stopped
        # being true when benches started being tested, and a report that
        # overstates its own harmlessness is worse than no footer. v0.1.0 took
        # a second bite out of it: one reading now lengthens a bench, so the
        # footer has to say which one and in which direction.
        text = command_text = _command()[0].handle("").lower()
        assert "the tally is observation" in text
        assert "one reading acts" in text
        assert "longer, never shorter" in text
        assert "benches are real" in command_text


class TestResetting:
    def test_reset_clears_the_history_and_leaves_the_benches(self):
        command, binding, _state = _command()
        led = binding._store.load()
        led.record(credential_id="k0", provider="gemini", model=MAIN,
                   reset_at=NOW + HOUR, now=NOW)
        binding._store.save(led, now=NOW)
        book = binding._journal.load()
        book.record_block(at=NOW, provider="gemini", model=MAIN, credential_id="k0")
        binding._journal.save(book, now=NOW)

        assert "cleared" in command.handle("reset")
        assert binding._journal.load(force=True).blocks() == []
        assert len(binding._store.load(force=True)) == 1

    def test_reset_on_an_untethered_plugin_says_so(self):
        assert "Nothing else was recorded" in status.QuotaCommand(None).handle("reset")

    def test_reset_starts_the_counts_over_too(self):
        # The most useful thing to do with that count is zero it and watch it
        # fill: "is KAME reading this provider now" is a question about the
        # next few refusals, not about every one since the process started.
        runtime = importlib.import_module(f"{PACKAGE}.runtime")
        runtime.note_classification("gemini", 429, sized=False)
        command, _binding, _state = _command()
        command.handle("reset")
        assert runtime.classifications() == []
        assert "nothing has failed yet" in command.handle("")


class TestNeverThrowingInAChatTurn:
    def test_help_is_available(self):
        assert "/kame-quota" in status.QuotaCommand(None).handle("help")

    def test_an_unknown_subcommand_explains_itself(self):
        text = status.QuotaCommand(None).handle("frobnicate")
        assert "Unknown subcommand" in text
        assert "/kame-quota" in text

    def test_a_store_that_explodes_is_reported_not_raised(self):
        class Exploding:
            _store = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
            _journal = None

        text = status.QuotaCommand(Exploding()).handle("")
        assert "failed" in text
        assert "RuntimeError" in text

    def test_unreadable_state_still_renders(self):
        command, _binding, state = _command()
        state.fail_read = True
        assert "nothing is benched" in command.handle("")


class TestRegistration:
    def test_it_registers_under_its_own_name(self):
        ctx = FakeContext()
        status.register_command(ctx)
        assert list(ctx.commands) == ["kame-quota"]

    def test_the_registered_handler_is_the_one_that_never_raises(self):
        ctx = FakeContext()
        status.register_command(ctx)
        assert "/kame-quota" in ctx.commands["kame-quota"]("help")


class TestShowingTheSpread:
    """The command has to reach the live dispersion, and survive its absence.

    The state is memory-only on the binding, so unlike the ledger and the
    journal there is no file to fall back to: either the binding installed and
    has it, or the section has nothing true to say.
    """

    class FakeDispersion:
        def __init__(self, snapshot=None, boom=False, totals=None) -> None:
            self._snapshot = snapshot or {}
            self._totals = totals if totals is not None else self._snapshot
            self._boom = boom
            self.asked_at = None

        def snapshot(self, now):
            if self._boom:
                raise RuntimeError("no")
            self.asked_at = now
            return self._snapshot

        def totals(self):
            if self._boom:
                raise RuntimeError("no")
            return self._totals

    def test_what_selection_saw_is_what_the_report_prints(self):
        command, binding, _state = _command()
        binding._dispersion = self.FakeDispersion({f"gemini:{MAIN}": {"#aa": 4}})
        binding._names = {"#aa": "GOOGLE_API_KEY[2]"}
        text = command.handle("")
        assert "GOOGLE_API_KEY[2]" in text
        assert "4 requests" in text
        assert binding._dispersion.asked_at == NOW

    def test_a_binding_without_the_ordering_says_the_minute_was_quiet(self):
        # Not an error: the pool binding can install without it, and an older
        # installed build has no dispersion at all.
        command, _binding, _state = _command()
        assert "nothing has been handed out yet" in command.handle("")

    def test_a_dispersion_that_throws_does_not_take_the_report_with_it(self):
        command, binding, _state = _command()
        binding._dispersion = self.FakeDispersion(boom=True)
        text = command.handle("")
        assert "nothing has been handed out yet" in text
        assert "What KAME has seen" in text

    def test_an_entry_with_no_readable_key_is_named_from_the_pool_listing(self):
        # mark_id falls back to the bare credential id for an OAuth entry, and
        # that id is the one the pool listing already labels — so the two name
        # sources have to be merged, not chosen between.
        command, binding, _state = _command()
        binding._dispersion = self.FakeDispersion({"gemini:x": {"row-7": 1}})
        binding._names = {}
        original = status._labels
        status._labels = lambda: {"row-7": "OAuth (personal)"}
        try:
            assert "OAuth (personal)" in command.handle("")
        finally:
            status._labels = original

    def test_the_help_explains_the_section(self):
        text = status.QuotaCommand(None).handle("help")
        assert "How the requests are spread" in text
        assert "since Hermes started" in text
        assert "KAME_SPREAD_DISABLED=1" in text


class TestShowingWhatItWasAsked:
    """The counts come off module state, so they survive a missing binding.

    That is the point of where they live: the install whose pool binding
    refused is the one whose state is hardest to see, and the classification
    half is still running in it.
    """

    def _runtime(self):
        return importlib.import_module(f"{PACKAGE}.runtime")

    def setup_method(self):
        self._runtime().forget_classifications()

    def teardown_method(self):
        self._runtime().forget_classifications()

    def test_the_report_shows_what_the_hook_counted(self):
        self._runtime().note_classification("gemini", 429, sized=True)
        self._runtime().note_classification("gemini", 429, sized=False)
        command, _binding, _state = _command()
        text = command.handle("")
        assert "gemini" in text
        assert "×2" in text and "1 sized" in text

    def test_a_plugin_with_no_binding_still_reports_them(self):
        # Without this the mode that most needs a report gets three lines of
        # apology and no state at all.
        self._runtime().note_classification("gemini", 429, sized=False)
        text = status.QuotaCommand(None).handle("")
        assert "not active" in text
        assert "a wait KAME could not read" in text

    def test_and_says_nothing_has_failed_when_nothing_has(self):
        assert "nothing has failed yet" in status.QuotaCommand(None).handle("")

    def test_the_help_explains_the_section(self):
        text = status.QuotaCommand(None).handle("help")
        assert "What KAME was asked" in text
        assert "never any part of the error text" in text


class TestShowingTheAnswersThatCarriedNothing:
    """A bench that is not being released has to be explainable.

    An empty answer stops the release path, and from the host's side nothing
    is wrong: every call succeeded. Without a line here the report would show
    a bench standing with no failure behind it and nothing saying why.
    """

    def _runtime(self):
        return importlib.import_module(f"{PACKAGE}.runtime")

    def setup_method(self):
        self._runtime().forget_empty_answers()

    def teardown_method(self):
        self._runtime().forget_empty_answers()

    def test_the_report_shows_them(self):
        self._runtime().note_empty_answer("gemini")
        self._runtime().note_empty_answer("gemini")
        command, _binding, _state = _command()
        text = command.handle("")
        assert "answered with nothing" in text
        assert "×2" in text

    def test_a_plugin_with_no_binding_still_reports_them(self):
        self._runtime().note_empty_answer("gemini")
        text = status.QuotaCommand(None).handle("")
        assert "answered with nothing" in text

    def test_a_healthy_process_shows_no_such_line(self):
        command, _binding, _state = _command()
        assert "answered with nothing" not in command.handle("")

    def test_reset_starts_them_over_too(self):
        self._runtime().note_empty_answer("gemini")
        command, _binding, _state = _command()
        command.handle("reset")
        assert self._runtime().empty_answers() == []
