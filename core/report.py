"""Turn the ledger and the journal into something a person can read.

Two audiences, one page. *What is benched right now* is operational — the
answer to "why is my key not being used" — and *what KAME has learned* is
evidence, the material a later version will tune its guesses against.

Kept pure and framework-free like the rest of ``core``: documents in, lines
of text out. The command layer supplies the labels and the clock. That makes
every wording decision here testable without a Hermes install, and it means
the same report can be printed from an Agent Zero build later.

**Never renders key material.** Credentials are identified by the label the
pool already shows in its own listings, or by a short prefix of the internal
id when there is no label. Neither is secret; the token never reaches this
module at all.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .journal import Journal, WindowStat, summarize
from .ledger import Ledger


def humanize(seconds: Optional[float]) -> str:
    """A duration a person reads at a glance, not a precise one."""
    if seconds is None:
        return "?"
    total = int(max(0, round(seconds)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, remainder = divmod(total, 60)
        # Seconds stop mattering once the wait is long enough to walk away
        # from, and dropping them keeps a column of these readable.
        return f"{minutes}m {remainder}s" if remainder and total < 600 else f"{minutes}m"
    if total < 86400:
        hours, remainder = divmod(total, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, remainder = divmod(total, 86400)
    hours = remainder // 3600
    return f"{days}d {hours}h" if hours else f"{days}d"


def _name(credential_id: str, labels: Optional[Dict[str, str]]) -> str:
    if labels:
        label = labels.get(credential_id)
        if label:
            return str(label)
    return (credential_id or "?")[:8]


def render_benches(
    ledger: Ledger,
    *,
    now: float,
    labels: Optional[Dict[str, str]] = None,
) -> List[str]:
    """The live per-model benches, grouped by what they apply to."""
    live = [bench for bench in ledger.benches() if bench.is_live(now)]
    if not live:
        return ["  nothing is benched per-model right now"]

    grouped: Dict[str, List] = {}
    for bench in live:
        grouped.setdefault(f"{bench.provider or '?'} · {bench.model}", []).append(bench)

    lines: List[str] = []
    for heading in sorted(grouped):
        lines.append(f"  {heading}")
        for bench in sorted(grouped[heading], key=lambda b: b.until):
            if bench.is_refuted:
                # Still on the books, holding nothing. The row survives only to
                # prove the host's matching cooldown is KAME's to unwind, and
                # saying "free in 8h" about a key that is in rotation right now
                # would be the most misleading line in the report.
                lines.append(
                    f"    {_name(bench.credential_id, labels):<20}"
                    f" back in rotation · tested and it worked"
                )
                continue
            row = (
                f"    {_name(bench.credential_id, labels):<20}"
                f" free in {humanize(bench.until - now)}"
            )
            if bench.is_extended:
                # The one number in the report KAME chose rather than read, so
                # it is the one that most needs saying out loud.
                row += " · held longer, this deadline proved short before"
            if bench.covers_every_model:
                # Changes how the line should be read: this one is not filed
                # against a model, it is filed against the key.
                row += " · all models"
            if bench.probes:
                # Worth showing because it answers the question a bench alone
                # raises: "is my agent just sitting there?" A probe count says
                # the deadline is being tested, not merely obeyed.
                row += f" · tested {bench.probes}×"
            lines.append(row)
    return lines


def _stat_lines(stat: WindowStat) -> List[str]:
    lines = [f"  {stat.provider or '?'} / {stat.model} / {stat.window}"]

    counted = f"    {stat.blocks} block(s)"
    if stat.kame_sized:
        counted += f", {stat.kame_sized} sized by KAME"
    else:
        counted += ", none sized by KAME"
    if stat.kame_dropped:
        # Said plainly because it is the one line here that is about the
        # plugin rather than about the provider: KAME read these, named a
        # deadline for them, and the pool kept a different one.
        counted += f", {stat.kame_dropped} KAME sized but the pool did not keep"
    if stat.shortest_predicted is not None and stat.longest_predicted is not None:
        if stat.shortest_predicted == stat.longest_predicted:
            counted += f", held {humanize(stat.shortest_predicted)}"
        else:
            counted += (
                f", held {humanize(stat.shortest_predicted)}"
                f"–{humanize(stat.longest_predicted)}"
            )
    lines.append(counted)

    if stat.recoveries:
        observed = f"    came back after {humanize(stat.fastest_recovery)} at the fastest"
        if stat.early_recoveries:
            observed += f", {stat.early_recoveries} before KAME expected"
        lines.append(observed)

    # The two findings worth a sentence of their own, and they are still
    # stated as observations. The tally here is per provider/model/window over
    # a fortnight; what actually widens a bench is a stricter reading of the
    # same records — a run of them back to back on one key — so this line
    # describes the evidence and deliberately does not promise the action.
    if stat.looks_short:
        lines.append(
            f"    {stat.under_predictions} repeat(s) landed right after the deadline"
            " — the real window looks longer than this"
        )
    elif stat.under_predictions:
        lines.append(
            f"    {stat.under_predictions} repeat(s) landed right after the deadline"
            " — not enough to call it yet"
        )
    if stat.looks_long:
        lines.append("    keys here recover earlier than they are held")
    if stat.looks_ignored:
        lines.append(
            "    every deadline KAME read here was dropped before it reached the"
            " pool — the cooldowns you are seeing are the host's, not KAME's"
        )
    return lines


def render_spread(
    snapshot: Optional[Dict[str, Dict[str, int]]],
    *,
    totals: Optional[Dict[str, Dict[str, int]]] = None,
    names: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Who has been picked in the last minute, per provider and model.

    The rest of this report is about refusals — things that already went
    wrong. This section is the only one that shows the plugin working while
    nothing is wrong, which is the state it is in almost all the time: the
    request counts that selection ordered on, exactly as it saw them.

    The counter is named by key rather than by pool row (see
    ``dispersion.mark_id``), so the hash is what arrives here and the labels
    are carried separately. A key with no remembered label is shown by a short
    piece of its hash, which is one-way and names nothing outside this process
    — the same rule as everywhere else in this module: no key material.

    Two numbers per key, because the window selection decides on is sixty
    seconds and that is a short thing to be looking at: what it took in the
    last minute, and what it has taken since Hermes started. Somebody checking
    whether their keys rotate at all should not have to catch the pool in the
    act, and a key showing nothing since start is the case worth seeing.

    Rows are ordered busiest first, because the question this answers is
    "is one key taking everything?" and the answer belongs on the first line.
    """
    window = snapshot or {}
    running = totals or {}
    lines: List[str] = []
    for bucket in sorted(set(window) | set(running)):
        counts = window.get(bucket, {})
        overall = running.get(bucket, {})
        keys = set(counts) | set(overall)
        if not keys:
            continue
        # A provider name can itself contain a colon (Hermes writes custom
        # endpoints as ``custom:<name>``) and so can a model tag (``llama3:8b``),
        # so no split is right for both. First colon wins: it keeps the model
        # tag intact, which is the half a reader is identifying the row by.
        provider, _, model = bucket.partition(":")
        heading = f"{provider or '?'} · {model}" if model else (provider or "?")
        lines.append(f"  {heading}")
        ordered = sorted(
            keys,
            key=lambda entry_id: (-counts.get(entry_id, 0), -overall.get(entry_id, 0), entry_id),
        )
        for entry_id in ordered:
            recent = counts.get(entry_id, 0)
            total = overall.get(entry_id, 0)
            plural = "" if recent == 1 else "s"
            head = f"{recent} request{plural}" if recent else "idle"
            lines.append(
                f"    {_name(entry_id, names):<20} {head:<12} {total} since Hermes started"
            )
    return lines or ["  nothing has been handed out yet"]


def render_asked(rows: Optional[List]) -> List[str]:
    """What the classifier was asked about since Hermes started, and answered.

    Declining is the common path, so this section is written not to read as a
    fault list: a 401 or a 404 with nothing sized is the plugin doing its job.
    The one row that gets a sentence of its own is a rate limit KAME could not
    size — sizing a wait is the entire job on that status, and a column of
    them with nothing sized is what an inert plugin looks like from outside.

    Counts only. The payloads behind these numbers may carry an unredacted
    provider dump, and none of it reaches this module.
    """
    if not rows:
        return ["  nothing has failed yet since Hermes started"]
    lines: List[str] = []
    for row in rows:
        status = row.status_code if row.status_code is not None else "?"
        answered = (
            f"{row.sized} sized" if row.sized else "none sized, left to the host"
        )
        line = f"  {str(row.provider or '?'):<20} {status}  ×{row.total} · {answered}"
        if row.worth_pointing_at:
            # The inert-plugin signature, and the only line here that is a
            # claim about KAME rather than about the provider.
            line += "  ← a wait KAME could not read"
        lines.append(line)
    return lines


def render_quiet(rows: Optional[List]) -> List[str]:
    """Calls that came back with nothing in them, per provider.

    Rendered in the same section as the classifications and only when there
    are any, because on a healthy install there never are: these lines exist
    to explain a bench that is *not* being released. A key answering with
    empty turns looks identical to a working key from the host's side, and
    without this the report would show a bench standing while every call
    succeeded, with nothing anywhere saying why.

    Counts only, like everything else in this section.
    """
    if not rows:
        return []
    lines: List[str] = []
    for row in rows:
        lines.append(
            f"  {str(row.provider or '?'):<20} answered with nothing"
            f"  ×{row.total} · not counted as proof the key works"
        )
    return lines


def render_learning(stats: List[WindowStat]) -> List[str]:
    if not stats:
        return ["  nothing recorded yet — no real refusal has passed through KAME"]
    lines: List[str] = []
    for stat in stats:
        lines.extend(_stat_lines(stat))
    return lines


def render(
    ledger: Ledger,
    book: Journal,
    *,
    now: float,
    labels: Optional[Dict[str, str]] = None,
    spread: Optional[Dict[str, Dict[str, int]]] = None,
    spread_totals: Optional[Dict[str, Dict[str, int]]] = None,
    names: Optional[Dict[str, str]] = None,
    asked: Optional[List] = None,
    quiet: Optional[List] = None,
    footer: str = "",
) -> str:
    lines = ["KAME — per-model quota", "", "Benched right now"]
    lines.extend(render_benches(ledger, now=now, labels=labels))
    # Between the two older sections on purpose: benched keys are what is
    # unavailable, this is what is being used instead, and the learning tally
    # is the long-run evidence behind both.
    lines.extend(["", "How the requests are spread"])
    lines.extend(render_spread(spread, totals=spread_totals, names=names))
    # Before the fortnight tally because it answers a shorter question that
    # has to be asked first: was KAME even consulted, and could it answer?
    # A learning section that is empty because nothing failed and one that is
    # empty because every failure was declined are different problems.
    lines.extend(["", "What KAME was asked (since Hermes started)"])
    lines.extend(render_asked(asked))
    # Appended to the same section rather than given one of its own: it is the
    # same question — what reached KAME and what it could make of it — and on
    # a healthy install it renders nothing at all.
    lines.extend(render_quiet(quiet))
    lines.extend(["", "What KAME has seen (last 14 days)"])
    lines.extend(render_learning(summarize(book, now=now)))
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)
