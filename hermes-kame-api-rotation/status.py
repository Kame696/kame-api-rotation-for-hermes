"""The ``/kame-quota`` slash command — what is benched, and what was learned.

Separate from ``/kame-keys`` because it is a different job with a different
risk. That command writes credentials; this one only reads, and keeping them
apart means a report cannot grow a code path that touches a key.

The command exists because the interesting half of this plugin is invisible.
A cooldown sized correctly looks exactly like a cooldown sized badly — the
call just works, or it just does not, and the reason lives in a log nobody is
tailing. Being able to ask is what turns "it seems fine" into a claim with
evidence behind it.

Everything Hermes-shaped is imported inside the function that needs it, so
the module imports cleanly with no agent present and the whole thing stays
testable against fakes.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from .core import report
from .core.journal import Journal
from .core.ledger import Ledger

logger = logging.getLogger(__name__)

COMMAND_NAME = "kame-quota"
COMMAND_DESCRIPTION = "Show per-model quota benches and what KAME has learned (KAME)"
COMMAND_ARGS_HINT = "[reset]"

_FOOTER = (
    "The tally is observation. One reading acts, and only in the safe"
    " direction: a deadline measured short twice in a row on the same key and"
    " model makes the next bench longer, never shorter. Benches are real, and"
    " a key marked tested is being tried again on a widening schedule."
)

_HELP = """/kame-quota — per-model quota state

  /kame-quota          what is benched now, and what KAME has recorded
  /kame-quota reset    forget the recorded history and the counts below
                       (live benches and the pool are left alone)
  /kame-quota help     this text

A bench listed here is per *model*: the key is still used for every other
model on the same provider. That is the whole point of the plugin, and it is
not something Hermes' own credential listing can show, because its cooldown
field has no model in it.

"all models" means the provider said this limit covers the whole key, not
just the model that hit it — so the key is held everywhere until it lapses.
Without that wording, the limit is treated as this model's alone.

"tested N×" means a model had no usable key left and KAME handed this one
back anyway, to check whether the deadline it chose was too long. It never
does that while any other key is available for that model.

"tested and it worked" means one of those tries came back clean. The deadline
was wrong, so the key is back in rotation for good — the row stays on this
list only because it is what lets KAME keep overriding Hermes' own cooldown
until that cooldown runs out on its own.

"held longer, this deadline proved short before" means the opposite mistake:
twice in a row this key came back at the deadline and was refused again within
minutes, on this same model and this same kind of limit. So KAME is holding it
past what the provider said — doubling each time it happens again, never more
than 8× and never more than a day, and never for a limit that reads like the
key is simply used up. The provider's own number is left untouched underneath,
which is what keeps every other model free to use the key. And the longer
deadline is still only a guess: it gets tested like any other when a model has
nothing else, and one clean call retires it.

For a limit that resets at a time of day rather than after a wait — a daily
cap that rolls at midnight — "held longer" means half an hour later, then an
hour, then two. Not double: a clock that is a few minutes off is off by
minutes, and the next midnight is already a day away.

"What KAME was asked" counts every failure the classification hook saw since
Hermes started, and how many of them KAME could size. Most lines will say "none
sized, left to the host" and that is the plugin working as designed: it declines
everything it does not genuinely recognise, because the built-in classifier is
good and overriding it with a guess is worse than staying quiet. The line worth
looking at is a rate limit — a 429 — with nothing sized, because reading the
wait out of a 429 is the entire job on that status. A column of those means the
provider changed the shape of its answer and KAME has gone quiet without saying
so. Counts only: provider and status number, never any part of the error text.

A line in that section reading "answered with nothing" is a call that came back
without a word and without a tool call. It only appears when it happens. KAME
normally treats a call that answers as proof the key works and drops the bench
on it for good, but a key that has run out does not always say so — on a free
tier it can return an empty turn instead of a refusal — so an empty answer is
counted and otherwise ignored. Nothing is benched because of it and no waiting
time is invented; the bench that was already standing simply keeps standing,
and is retried on its own schedule. If that line is climbing while everything
looks healthy, the key is the thing to look at, not the plugin.

"How the requests are spread" is the other half of the plugin, the half that
works while nothing is wrong: of the keys that are healthy, which one gets the
next request. Hermes' own answer is the first one on the list every time, so a
per-minute limit is hit by one key while the rest sit idle. KAME hands out the
least recently loaded instead, counts it as busy the moment it is handed out
so two calls at once do not pick the same key, and puts a key whose bench just
lapsed behind the ones that have been resting. The numbers here are exactly
what that ordering read. Counted per key, so two pool rows holding the same
key show as one line, which is what the provider is metering.

Two numbers per key: what it took in the last minute, which is the window the
ordering actually decides on, and what it has taken since Hermes started. The
second one is there so you do not have to catch the pool inside a sixty-second
window to see whether your keys are being rotated at all — a key sitting at
"idle · 0 since Hermes started" beside one with hundreds is the picture worth
seeing. Neither number survives a restart, and only the first one decides
anything. Setting KAME_SPREAD_DISABLED=1 turns the ordering off and gives the
host its own back.

A deadline only counts as "short" if the key never answered while it was
supposed to be waiting. If it did — because it was tested and worked, or
because it was handed back on time and simply ran out again — that is a fresh
limit, not a bad guess, and nothing is held longer for it."""


def _labels() -> Dict[str, str]:
    """Credential id to the name the pool already displays. Never a token.

    Best-effort: a report with short ids in it is worse to read and just as
    true, so a pool that cannot be loaded costs nothing important.
    """
    labels: Dict[str, str] = {}
    try:
        from agent.credential_pool import load_pool
        from hermes_cli.auth import read_credential_pool

        for provider in read_credential_pool().keys():
            try:
                for entry in load_pool(provider).entries():
                    identifier = str(getattr(entry, "id", "") or "")
                    label = getattr(entry, "label", None)
                    if identifier and label:
                        labels[identifier] = str(label)
            except Exception:
                logger.debug("kame: could not label the %s pool", provider, exc_info=True)
    except Exception:
        logger.debug("kame: no pools to label", exc_info=True)
    return labels


class QuotaCommand:
    """Holds the two stores the report reads.

    Constructed with the live binding when there is one so the command and
    the hot path share a cache — reading through the same store is what keeps
    a report from showing a stale bench that the pool has already acted on.
    """

    def __init__(self, binding: Any = None, *, clock=time.time) -> None:
        self._binding = binding
        self._clock = clock

    # -- what the report reads -------------------------------------------

    def _ledger(self) -> Ledger:
        store = getattr(self._binding, "_store", None)
        if store is None:
            return Ledger()
        return store.load(force=True)

    def _journal(self) -> Journal:
        store = getattr(self._binding, "_journal", None)
        if store is None:
            return Journal()
        return store.load(force=True)

    def _spread(self):
        """What selection has handed out in the last minute, if it is running.

        Memory-only state on the live binding, so there is nothing to load and
        nothing to fail — but an older binding, or one whose install was
        refused, simply has no dispersion, and the section then says so rather
        than pretending the pool has been idle.
        """
        dispersion = getattr(self._binding, "_dispersion", None)
        if dispersion is None:
            return None, None
        try:
            return dispersion.snapshot(self._clock()), dispersion.totals()
        except Exception:
            logger.debug("kame: could not read the spread", exc_info=True)
            return None, None

    def _names(self, labels: Dict[str, str]) -> Dict[str, str]:
        """Labels for the spread section, which counts by key, not by row.

        Two sources because there are two kinds of name in that snapshot: a
        key hash, which only the binding can put a label to, and a bare
        credential id for an entry with no readable key, which the pool
        listing already names. Neither carries key material.
        """
        names = dict(labels)
        remembered = getattr(self._binding, "_names", None)
        if isinstance(remembered, dict):
            names.update({str(k): str(v) for k, v in remembered.items()})
        return names

    # -- subcommands ------------------------------------------------------

    def _asked(self) -> List:
        """The classification counts, which do not need the binding at all.

        This is the half of the plugin that runs even when the pool binding
        refused to install — and that is precisely the install whose state is
        hardest to see, so the counts have to survive the early return below.
        """
        try:
            from . import runtime

            return runtime.classifications()
        except Exception:
            logger.debug("kame: could not read the classification counts", exc_info=True)
            return []

    def _quiet(self) -> List:
        """The count of calls that answered with nothing, same rules as above.

        Read from the same place and for the same reason: it is a fact about
        what passed through the hooks, not about the pool, so an install whose
        binding refused still has one to show.
        """
        try:
            from . import runtime

            return runtime.empty_answers()
        except Exception:
            logger.debug("kame: could not read the empty-answer counts", exc_info=True)
            return []

    def _carousel(self) -> List[str]:
        """What the per-call rotation has actually been doing, or why it is not.

        This is the section a user reads to answer "is it rotating?", which
        every previous release could only answer indirectly — by showing
        benches, which appear only when something already failed. Counts and
        health, never a credential: a status command that prints a key is a
        status command nobody can screenshot.
        """
        try:
            from . import _dispatch_binding as binding
            from .core.carousel import ENGINE, format_duration
        except Exception:  # pragma: no cover — the package is already imported
            return []

        if binding is None:
            return [
                "Per-call rotation is OFF — every request carries the key Hermes",
                "resolved, and a failure follows Hermes' own retry and rotation",
                "rules. Check the log for the reason it did not install.",
                "",
            ]

        lines = [
            "Rotation (this Hermes process)",
            f"  calls {binding.calls} · rotations {binding.rotations} · "
            f"recovered {binding.recovered} · surfaced {binding.surfaced}",
        ]
        snapshot = ENGINE.snapshot()
        if not snapshot:
            lines.append("  no call has gone out yet")
        for identity in sorted(snapshot):
            row = snapshot[identity]
            detail = f"  {identity}: {row['healthy']}/{row['keys']} keys ready"
            if row["resting"]:
                detail += f", {row['resting']} resting"
            if row["kinds"]:
                detail += f" ({', '.join(row['kinds'])})"
            detail += f" · {row['successes']} ok / {row['failures']} refused"
            lines.append(detail)
        # Since 1.0.1 a call waits as long as a key needs, so the honest
        # question is no longer "what is the ceiling" but "how much of this
        # session was spent waiting" — the number that explains a slow day
        # without anyone having to guess at it.
        if binding.waits:
            lines.append(
                f"  waited {format_duration(binding.waited_s)} across "
                f"{binding.waits} pause(s) for a key to come back"
            )
        lines.append(
            "  a call rotates for as long as any key still has a chance; "
            "press stop to cancel one"
        )
        lines.append("")
        return lines

    def show(self) -> str:
        head = self._carousel()
        if self._binding is None:
            asked = self._asked()
            lines = [
                "Per-model quota memory is not active — KAME is sizing cooldowns",
                "but the credential pool binding did not install. Check the log",
                "for the reason; the plugin is still doing its first job.",
                "",
                "What KAME was asked (since Hermes started)",
            ]
            # The one thing that can still be said truthfully in this mode,
            # and the thing worth saying: whether the half that *is* running
            # is answering anything.
            lines.extend(report.render_asked(asked))
            lines.extend(report.render_quiet(self._quiet()))
            return "\n".join(head + lines)
        labels = _labels()
        spread, spread_totals = self._spread()
        body = report.render(
            self._ledger(),
            self._journal(),
            now=self._clock(),
            labels=labels,
            spread=spread,
            spread_totals=spread_totals,
            names=self._names(labels),
            asked=self._asked(),
            quiet=self._quiet(),
            footer=_FOOTER,
        )
        return "\n".join(head) + body if head else body

    def _forget_counts(self) -> None:
        """Start the classification count over.

        Part of ``reset`` because the most useful thing to do with that count
        is to zero it and watch it fill: "is KAME reading this provider *now*"
        is a question about the next few refusals, not about every one since
        the process started. Benches and the pool are untouched either way.
        """
        try:
            from . import runtime

            runtime.forget_classifications()
            runtime.forget_empty_answers()
        except Exception:
            logger.debug("kame: could not clear the classification counts", exc_info=True)

    def reset(self) -> str:
        self._forget_counts()
        store = getattr(self._binding, "_journal", None)
        if store is None:
            # Still worth saying what did happen: without a binding the counts
            # are the only state there was, and they are gone.
            return "Counts cleared. Nothing else was recorded to reset."
        return (
            "Recorded history and counts cleared. Live benches were left alone."
            if store.clear()
            else "Could not clear the recorded history — see the log."
        )

    # -- entry point ------------------------------------------------------

    def handle(self, raw_args: str = "") -> str:
        """Always returns text, never raises.

        A slash command that throws inside a chat turn is far worse than one
        that reports a problem.
        """
        try:
            verb = (raw_args or "").strip().split(" ", 1)[0].strip().lower()
            if verb in {"help", "-h", "--help", "?"}:
                return _HELP
            if verb in {"reset", "clear", "forget"}:
                return self.reset()
            if not verb:
                return self.show()
            return f"Unknown subcommand `{verb}`.\n\n{_HELP}"
        except Exception as exc:
            logger.debug("kame: /%s failed", COMMAND_NAME, exc_info=True)
            return f"/{COMMAND_NAME} failed: {type(exc).__name__}: {exc}"


def register_command(ctx, *, binding: Any = None) -> QuotaCommand:
    command = QuotaCommand(binding)
    ctx.register_command(
        COMMAND_NAME,
        command.handle,
        description=COMMAND_DESCRIPTION,
        args_hint=COMMAND_ARGS_HINT,
    )
    return command
