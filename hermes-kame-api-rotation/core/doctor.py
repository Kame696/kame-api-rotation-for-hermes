"""Is this rotating the way it should be? — answered from inside the plugin.

Everything here was, until 1.6.0.1, a script in the repository's ``tools/``
directory: ``inspect_run.py``, which read the logs, the journal and the
snapshot off disk and said whether a run looked right. It worked, and it was
in the one place that does not survive contact with reality — a reinstall, a
new machine, a different assistant, or the owner simply not having the repo
open. A diagnostic that lives beside the code it diagnoses is a diagnostic
nobody can run when they need it.

So it lives in the plugin now, reachable as ``/kame doctor`` from any chat,
and the script stays as the offline half — the one that can compare several
profiles at once and read a log file this process never wrote.

Framework-free like the rest of ``core``: it is handed plain dictionaries and
returns plain lines. That is what makes "the doctor says the pool is fine" a
thing a test can assert without a Hermes, a network or a provider.

Four questions, in the order a person asks them:

1. **Is the build that is running the build I installed?** The version string
   is written by hand and has been wrong; the fingerprint is computed from the
   files that actually loaded and has not.
2. **Can it see my keys?** A pool with nothing in it and a pool nobody is
   using look identical from the chat.
3. **Is each kind of refusal getting the right rest?** The number beside each
   kind is the thing the owner asked to be able to check, in as many words:
   *whether it is rotating correctly at the correct time for each error*.
4. **Is anything here a person has to fix?** A refused credential, a key that
   is not in the config, a provider whose own counter disagrees with the one
   KAME acted on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

#: The rests this build applies, by kind, with what each one is for. Written
#: down here rather than derived, because the point of showing it is to let a
#: reader check the code against the intent — a table generated from the code
#: agrees with the code by construction and proves nothing.
#:
#: Kept in step with ``carousel`` by a test that reads both.
EXPECTED_RESTS = (
    ("timeout", 3.0, "the provider went quiet; the key is fine"),
    ("server", 5.0, "a 5xx is the provider's problem, not the key's"),
    ("per_minute", None, "the provider's own retry hint, or 20s when it gives none"),
    ("rate_limit", None, "same as per_minute — the modern classifier's name for it"),
    ("other", 20.0, "nothing recognised the refusal"),
    ("auth", 20.0, "a bare 401: offered last, and out after 3 in a row"),
    ("revoked", 20.0, "the provider named the key dead: out of rotation at once"),
    ("denied", 20.0, "this key may not use THIS model; it still works elsewhere"),
    ("daily", 3600.0, "the day's allowance is spent, so only time helps"),
    ("insufficient_quota", 3600.0, "out of credit — a person has to top it up"),
)


def _plural(count: int, one: str, many: Optional[str] = None) -> str:
    return one if count == 1 else (many or f"{one}s")


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60):02d}m"


def _rule(title: str) -> List[str]:
    return ["", title, "-" * len(title)]


def build_lines(snapshot: Dict[str, Any]) -> List[str]:
    """Which KAME is actually running here."""
    build = snapshot.get("build") or {}
    version = str(snapshot.get("version") or "?")
    fingerprint = str(build.get("fingerprint") or "?")
    complete = bool(build.get("complete", True))
    missing = list(build.get("missing") or [])

    out = [f"  build       {version}  {fingerprint}"]
    if not complete:
        out.append(
            f"  INCOMPLETE  {len(missing)} required {_plural(len(missing), 'file')} "
            f"missing: {', '.join(missing[:4])}"
        )
        out.append("              this install is not the one that was packaged")
    role = str(snapshot.get("role") or "")
    profile = str(snapshot.get("profile") or "")
    where = role or "hermes"
    if profile:
        where = f"{where}, profile {profile}"
    out.append(f"  running as  {where}  (process {snapshot.get('pid', '?')})")

    neighbours = list(snapshot.get("neighbours") or [])
    if neighbours:
        out.append(
            f"  sharing     {len(neighbours)} other Hermes "
            f"{_plural(len(neighbours), 'process', 'processes')} using the same keys"
        )
        for other in neighbours:
            other_print = str((other.get("build") or {}).get("fingerprint") or "?")
            mark = "  <- different build" if other_print != fingerprint else ""
            out.append(
                f"                {other.get('role') or 'hermes'} "
                f"{other.get('profile') or ''} "
                f"v{other.get('version') or '?'} {other_print}{mark}"
            )
    return out


def pool_lines(snapshot: Dict[str, Any]) -> List[str]:
    """What the pools hold, and what is wrong with them."""
    pools = list(snapshot.get("pools") or [])
    if not pools:
        return [
            "  no pool has been built yet — nothing has called a provider through",
            "  this process since it started. That is not a fault; it is silence.",
        ]

    out = []
    for pool in pools:
        healthy = int(pool.get("healthy", 0) or 0)
        invalid = int(pool.get("invalid", 0) or 0)
        keys = int(pool.get("keys", 0) or 0)
        ready = max(0, healthy - invalid)
        soonest = pool.get("soonest_s")
        out.append(f"  {pool.get('identity', '?')}")
        # Three independent facts, not a partition. "Refused" and "resting"
        # can be the same key — a refused credential is benched too — so
        # adding them up would report more keys than the pool holds, which is
        # how the first draft of this line came to say "0 of 3 ready, 2
        # refused, 2 resting" about a pool of three.
        detail = f"      {keys} {_plural(keys, 'key')}, {ready} ready now"
        if invalid:
            detail += f", {invalid} refused as {_plural(invalid, 'a credential', 'credentials')}"
        if soonest is not None:
            detail += f", next one back in {_duration(float(soonest))}"
        out.append(detail)
        outside = list(pool.get("outside_pool") or [])
        if outside:
            out.append(
                f"      {len(outside)} {_plural(len(outside), 'key')} not in the credential "
                f"pool — Hermes resolved {'it' if len(outside) == 1 else 'them'} "
                f"from this model's own settings: {', '.join(outside)}"
            )
    return out


def rest_lines(kinds: Sequence[Any]) -> List[str]:
    """The kind → rest table, beside what the journal actually recorded.

    ``kinds`` is a sequence of ``journal.KindStat``. It is read through
    ``getattr`` rather than unpacked, so this module stays usable with any
    object carrying the same four names — which is what lets the test hand it
    a stub instead of building a journal.
    """
    seen: Dict[str, int] = {}
    for stat in kinds or ():
        name = str(getattr(stat, "kind", "") or "")
        seen[name] = seen.get(name, 0) + int(getattr(stat, "blocks", 0) or 0)

    out = [
        "  Each kind of refusal, the rest this build applies, and how often it",
        "  has actually happened in the last fortnight.",
        "",
    ]
    for kind, rest, why in EXPECTED_RESTS:
        count = seen.get(kind, 0)
        out.append(
            f"    {kind:<20} {_duration(rest):>8}   "
            f"{str(count) + ' seen' if count else '—':>10}   {why}"
        )
    out.append("")
    out.append("    A rest escalates when the same key says the same thing again:")
    out.append("    5s, 10s, 20s ... to 90s for a server error, and the provider's")
    out.append("    own number doubling to 5m for a rate limit.")
    out.append("")
    out.append("    A refusal is not answered with a clock at all. The provider")
    out.append("    sends no time with a 401 because there is no time to send —")
    out.append("    it is saying the key is wrong, not that you should wait. So a")
    out.append("    refused key is offered LAST, and leaves rotation entirely")
    out.append("    after three in a row, or after one if the provider used the")
    out.append("    words. It is never deleted: KAME does not write credentials.")
    return out


def evidence_lines(kinds: Sequence[Any]) -> List[str]:
    """How many decisions were read off the payload, and how many were guesses."""
    total = 0
    sized = 0
    stated = 0
    contradicted = 0
    for stat in kinds or ():
        blocks = int(getattr(stat, "blocks", 0) or 0)
        total += blocks
        sized += int(getattr(stat, "kame_sized", 0) or 0)
        stated += int(getattr(stat, "stated", 0) or 0)
        contradicted += int(getattr(stat, "contradicted", 0) or 0)

    if not total:
        return ["  nothing has been refused yet, so there is nothing to grade"]

    out = [
        f"  {total} {_plural(total, 'refusal')} recorded in the last fortnight",
        f"  {sized} of them sized by KAME rather than by the host",
        f"  {stated} carried a counter the provider named",
    ]
    if contradicted:
        out.append(
            f"  {contradicted} where the provider named one counter and KAME acted"
        )
        out.append(
            "     on another. That is the misclassification signal — worth a look,"
        )
        out.append("     not proof, since a provider can name a counter loosely.")
    elif stated:
        out.append("  none of them disagreed with the counter the provider named")
    return out


def trouble_lines(snapshot: Dict[str, Any], kinds: Sequence[Any]) -> List[str]:
    """Only the things a person has to do something about."""
    trouble: List[str] = []

    totals = snapshot.get("totals") or {}
    rejected = int(totals.get("rejected", 0) or 0)
    retired = int(totals.get("retired", 0) or 0)
    if retired:
        named: List[str] = []
        for pool in snapshot.get("pools") or []:
            named.extend(str(k) for k in (pool.get("retired_keys") or []))
        trouble.append(
            f"  {retired} {_plural(retired, 'key')} out of rotation: the provider "
            f"refused {'it' if retired == 1 else 'them'} as "
            f"{_plural(retired, 'a credential', 'credentials')}"
            f"{'. ' + ', '.join(named) if named else '.'}"
        )
        trouble.append(
            "     KAME has stopped offering "
            + ("it" if retired == 1 else "them")
            + ", so nothing is being spent on "
        )
        trouble.append(
            "     "
            + ("it" if retired == 1 else "them")
            + " any more. Nothing was deleted — paste the replacement over"
        )
        trouble.append("     it in Settings and it comes back by itself.")
    if rejected > retired:
        trouble.append(
            f"  {rejected - retired} {_plural(rejected - retired, 'key')} just refused "
            f"and still being tried. One refusal is not proof — an expired"
        )
        trouble.append(
            "     token a second from refreshing sends the same thing — so it takes"
        )
        trouble.append("     three in a row, with nothing working in between.")

    for pool in snapshot.get("pools") or []:
        outside = list(pool.get("outside_pool") or [])
        if outside:
            trouble.append(
                f"  {pool.get('identity')}: {len(outside)} "
                f"{_plural(len(outside), 'key')} in use that the credential pool "
                f"does not hold."
            )
            trouble.append(
                "     Usually right — Hermes can resolve a key from a model's own"
            )
            trouble.append(
                "     settings — but it is also what a key pasted in the wrong place"
            )
            trouble.append("     looks like.")

    for stat in kinds or ():
        if getattr(stat, "needs_a_person", False) and int(getattr(stat, "blocks", 0) or 0):
            trouble.append(
                f"  {getattr(stat, 'provider', '?')}: "
                f"{getattr(stat, 'blocks')} × {getattr(stat, 'kind')} — no timer fixes this one."
            )

    build = snapshot.get("build") or {}
    if not build.get("complete", True):
        trouble.append("  This install is missing files it was packaged with.")

    mine = str(build.get("fingerprint") or "")
    for other in snapshot.get("neighbours") or []:
        theirs = str((other.get("build") or {}).get("fingerprint") or "")
        if mine and theirs and theirs != mine:
            trouble.append(
                f"  Another Hermes ({other.get('role') or 'hermes'}"
                f"{' ' + str(other.get('profile')) if other.get('profile') else ''}) "
                f"is on build {theirs}, not {mine}."
            )
            trouble.append(
                "     It loads the plugin at start-up, so restarting it is what makes"
            )
            trouble.append("     the two agree.")
            break

    if not trouble:
        return ["  Nothing here needs a person."]
    return trouble


def render(snapshot: Dict[str, Any], kinds: Sequence[Any] = ()) -> str:
    """The whole report, as one block of text for a chat turn."""
    out: List[str] = ["KAME doctor — what this process is actually doing"]

    out += _rule("Which KAME is running")
    out += build_lines(snapshot)

    out += _rule("What it can see")
    out += pool_lines(snapshot)

    out += _rule("What each error costs a key")
    out += rest_lines(kinds)

    out += _rule("How well-evidenced the decisions were")
    out += evidence_lines(kinds)

    out += _rule("Worth a person's time")
    out += trouble_lines(snapshot, kinds)

    return "\n".join(out)


__all__ = [
    "EXPECTED_RESTS",
    "build_lines",
    "evidence_lines",
    "pool_lines",
    "render",
    "rest_lines",
    "trouble_lines",
]
