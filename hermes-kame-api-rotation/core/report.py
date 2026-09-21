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

from typing import Dict, List, Optional, Tuple

from .journal import Journal, KindStat, WindowStat, count_kinds, summarize
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


#: Statuses that mean a human has to do something, not that a timer has to
#: run out. A throttle is the pool's business; a credential the provider
#: keeps calling invalid is the owner's, and no amount of rotation fixes it.
_NEEDS_A_PERSON = (401, 403)

#: How many refusals of the same kind, on one key, before it is worth a line.
#: Two is noise on any pool; this is the shape of a key that is simply wrong.
_REPEAT_FLOOR = 3


def render_repeat_offenders(
    book: Journal, *, now: float, labels: Optional[Dict[str, str]] = None
) -> List[str]:
    """Keys the provider keeps refusing for a reason waiting cannot fix.

    Carried back from the Agent Zero plugin, which counted failures per kind
    per key and could therefore say *which* key was the problem. This port
    recorded the same facts in the journal and rendered none of them, so a
    single bad credential in a pool of fifteen was invisible: the pool
    rotated past it every time, correctly, for ever.

    The owner's own journal is the case. Twenty 401s on NVIDIA across
    13.9 days, eleven of them on one key — and nothing anywhere said so. The
    pool kept working, which is exactly why nobody could see it.

    Deliberately a *report* and not a rule. KAME does not retire a key on a
    401: 1.4.0 removed that after twenty-one healthy keys were quarantined
    for an hour each on transient auth failures. Counting them and naming the
    key leaves the decision with the person who can actually replace it.

    Counts and labels only, like every other section here.
    """
    try:
        blocks = book.blocks()
    except Exception:  # pragma: no cover - a journal that cannot be read
        return []
    tally: Dict[Tuple[str, int], int] = {}
    for block in blocks:
        status = getattr(block, "status_code", None)
        credential = str(getattr(block, "credential_id", "") or "")
        if status in _NEEDS_A_PERSON and credential:
            key = (credential, int(status))
            tally[key] = tally.get(key, 0) + 1
    rows = [
        (credential, status, count)
        for (credential, status), count in tally.items()
        if count >= _REPEAT_FLOOR
    ]
    if not rows:
        return []
    rows.sort(key=lambda row: (-row[2], row[0]))
    lines = ["", "Keys a wait will not fix"]
    for credential, status, count in rows:
        what = "rejected the credential" if status == 401 else "refused this key"
        lines.append(
            f"  {_name(credential, labels):<20} {status}  x{count}"
            f" · the provider {what}"
        )
    lines.append(
        "  KAME keeps rotating past these. Replacing one is the only thing "
        "that clears it."
    )
    return lines


def render_learning(stats: List[WindowStat]) -> List[str]:
    if not stats:
        return ["  nothing recorded yet — no real refusal has passed through KAME"]
    lines: List[str] = []
    for stat in stats:
        lines.extend(_stat_lines(stat))
    return lines



def render_kinds(stats: List[KindStat]) -> List[str]:
    """What kept going wrong, by kind, over the same fortnight.

    The tally below this one answers "is KAME reading these" and groups by the
    window it concluded. This answers the question an owner asks first — *what
    keeps happening* — and it is a different question, because a daily cap, a
    per-minute throttle and a rejected credential are three problems with
    three answers and only one of them is a timer.

    Two columns beyond the count, and both are about trust rather than volume:

    * how many of that kind KAME actually sized, so a kind it reads well and
      a kind it never reads are not one number;
    * how often the provider's own counter named a **different** window than
      the one KAME acted on. That is the misclassification signal, and until
      1.6.0.0 nothing recorded the provider's half at all — the journal held a
      confident verdict with nothing able to contradict it.

    A disagreement is only reported where there was something to disagree
    with. Providers that name no counter render no column, because "0
    contradictions" out of nothing said is not reassurance, it is silence.

    Counts and KAME's own vocabulary. No provider text reaches this module.
    """
    if not stats:
        return []
    lines: List[str] = ["", "What kept going wrong (last 14 days)"]
    provider = None
    for stat in stats:
        if stat.provider != provider:
            provider = stat.provider
            lines.append(f"  {provider or '?'}")
        detail = f"{stat.kame_sized} sized by KAME" if stat.kame_sized else "none sized"
        line = f"    {stat.kind:<26} ×{stat.blocks:<4} {detail}"
        if stat.contradicted:
            line += (
                f"  ← the provider named another window on "
                f"{stat.contradicted} of {stat.stated}"
            )
        elif stat.needs_a_person:
            # The one row where the count is not the message. Rotation is
            # working perfectly and will go on working perfectly for ever;
            # somebody has to replace a credential or pay a bill.
            line += "  ← waiting does not fix this one"
        lines.append(line)
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
    # Before the window tally, because it is the shorter and blunter reading
    # of the same fortnight: what kept happening, and whether waiting is even
    # the right answer to it. The tally below then says how well KAME is
    # reading each window.
    lines.extend(render_kinds(count_kinds(book, now=now)))
    lines.extend(["", "What KAME has seen (last 14 days)"])
    lines.extend(render_learning(summarize(book, now=now)))
    # Last, and usually absent. It is the only section that asks the reader
    # to do something, so it belongs after the picture that justifies it.
    lines.extend(render_repeat_offenders(book, now=now, labels=labels))
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)
