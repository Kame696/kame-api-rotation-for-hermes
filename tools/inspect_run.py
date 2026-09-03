r"""Read what KAME actually did on this machine, and say whether it looks right.

Everything else in ``tools/`` proves the plugin behaves correctly against a
harness. This reads the opposite direction: the logs, the journal and the live
snapshot that a *real* Hermes wrote while somebody used it.

It answers the questions worth asking after an upgrade, in this order:

1. **Which build is actually running**, per profile, and did it register
   complete? A version is a claim; the fingerprint is the code that loaded.
2. **Is it rotating, and on what?** Every rest is logged with the reason and
   the wait, so they can be counted by reason instead of read one by one.
3. **Is it classifying, and how?** Since 1.4.0 every verdict is logged with the
   window and where the evidence came from. Counted **before and after** the
   current build registered, because that comparison is the only honest way to
   tell an upgrade that improved classification from one that merely ran.
4. **Is anything wrong?** Quarantines, providers that went down, answers that
   could not be continued, and the 1.6.0.0 blame check are pulled out and
   shown on their own.

Read-only, and deliberately so: it opens log files, the journal and the
snapshot, and writes nothing anywhere. Safe to run while Hermes is serving.

    python tools/inspect_run.py                # every profile, since its restart
    python tools/inspect_run.py --all          # the whole log, not just since boot
    python tools/inspect_run.py --profile k    # one profile
    python tools/inspect_run.py --errors 20    # show the last N warning lines verbatim
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))
PLUGIN_NAME = "hermes-kame-api-rotation"

#: How much of each log to read. These files reach tens of megabytes; the tail
#: is where a run that just happened lives, and reading all of it to answer a
#: question about the last hour would be slow for no gain.
TAIL_BYTES = 8_000_000

TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

#: ``build <hash> - complete`` at registration. Only the hash is matched here.
#: The separator is an em dash written through whatever console encoding the
#: log was opened with, and a wildcard wide enough to cross it also eats the
#: first letter of the word after it — which is how this first reported every
#: healthy build as ``omplete``. The word is read separately, by looking for
#: it, so no encoding can shift it.
REGISTRATION = re.compile(PLUGIN_NAME + r": build ([0-9a-f]{12})")


def completeness(line: str) -> str:
    """``complete``, ``incomplete``, or what the line says instead.

    ``incomplete`` is checked first because it contains the other word.
    """
    lowered = line.lower()
    if "incomplete" in lowered:
        return "incomplete"
    if "complete" in lowered:
        return "complete"
    return "state unstated"

#: The verdict line, since 1.4.0: reason, window, and which piece of evidence
#: decided it. What these three say is the whole point of this release.
VERDICT = re.compile(PLUGIN_NAME + r": (\S+)/(\S+) -> (\S+) \[(\S+) via ([^\]]*)\]")

#: One rest. ``reason`` here is KAME's own word, never the provider's text.
RESTED = re.compile(
    r"kame: (\S+?):(\S+?) (key:[0-9a-f]+) (\S+) \[?(\d+)?\]?.{1,4} resting ([^,]+), taking the next"
)

#: The 1.6.0.0 instrument. Never expected to fire.
BLAMED = re.compile(r"kame: benching (\S+) \((key:[0-9a-f]+)\) but the request carried")

TROUBLE: Tuple[Tuple[str, Any], ...] = (
    ("a key was quarantined as invalid", re.compile(r"is not a valid credential")),
    ("the provider looked down", re.compile(r"provider appears down")),
    ("every key refused the same request", re.compile(r"every key answered")),
    ("an answer could not be continued", re.compile(r"cannot be continued")),
    ("a continuation was refused", re.compile(r"continuation was refused")),
    ("KAME blamed a key that never went out", BLAMED),
)

GOOD: Tuple[Tuple[str, Any], ...] = (
    ("answers continued on another key", re.compile(r"continuing it on another key")),
    ("answers delivered as one response", re.compile(r"delivered as one response")),
    ("merged Gemini tool calls repaired", re.compile(r"split \d+ merged Gemini tool call")),
)


def moment(line: str) -> Optional[float]:
    found = TIMESTAMP.match(line)
    if not found:
        return None
    try:
        return datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def when(stamp: Optional[float]) -> str:
    if not stamp:
        return "-"
    return time.strftime("%d %b %H:%M", time.localtime(stamp))


def read_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    try:
        return path.read_bytes()[-TAIL_BYTES:].decode("utf-8", "replace").splitlines()
    except OSError as exc:
        print(f"  (could not read {path.name}: {exc})")
        return []


def roots() -> List[Tuple[str, Path]]:
    found = [("base", HERMES_HOME)]
    profiles = HERMES_HOME / "profiles"
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            if entry.is_dir():
                found.append((entry.name, entry))
    return found


def snapshot_of(root: Path) -> Dict[str, Any]:
    """The live panel state, or an empty dict.

    Rewritten constantly by the running process, so a read can land mid-write.
    One retry, because a torn read here is noise rather than a finding - it was
    mistaken for one once, and reported as missing counters on a build that had
    them.
    """
    path = root / "plugin-data" / PLUGIN_NAME / "state.json"
    for _attempt in range(2):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.2)
    return {}


def journal_of(root: Path) -> List[Dict[str, Any]]:
    """Every refusal the journal kept, from whichever data directory holds it.

    The plugin's data directory name depends on how the host built its context,
    and both spellings exist on this machine, so the journal is found by its
    payload rather than by a path that has already changed once.
    """
    rows: List[Dict[str, Any]] = []
    data = root / "plugin-data"
    if not data.is_dir():
        return rows
    for path in sorted(data.glob("*" + PLUGIN_NAME + "*/state.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        book = payload.get("quota_journal_v1")
        if isinstance(book, dict):
            rows.extend(row for row in (book.get("blocks") or []) if isinstance(row, dict))
    return rows


def registrations(lines: List[str]) -> List[Tuple[float, str, str]]:
    out: List[Tuple[float, str, str]] = []
    for line in lines:
        found = REGISTRATION.search(line)
        if found:
            out.append((moment(line) or 0.0, found.group(1), completeness(line)))
    return out


def table(title: str, rows: List[Tuple[str, int]], width: int = 34) -> None:
    if not rows:
        return
    print(f"  {title}")
    for label, count in rows:
        print(f"    {label:<{width}} {count}")


def report(label: str, root: Path, since_boot: bool, errors: int) -> List[str]:
    """One profile. Returns the lines worth repeating in the final verdict."""
    notes: List[str] = []
    print("\n" + "=" * 72)
    print(label)
    print("=" * 72)

    agent = read_lines(root / "logs" / "agent.log")
    errs = read_lines(root / "logs" / "errors.log")
    if not agent and not errs:
        print("  no logs here - this profile has not run")
        return notes

    boots = registrations(agent)
    boot_at = 0.0
    running = ""
    if not boots:
        print("  KAME never registered in the tail of this log.")
        notes.append(f"{label}: no KAME registration found")
    else:
        boot_at, running, state = boots[-1]
        print(f"  running build : {running}  ({state})")
        print(f"  registered    : {when(boot_at)}   [{len(boots)} start(s) in the tail]")
        if state != "complete":
            notes.append(f"{label}: the build registered as {state!r}, not complete")
        earlier = {mark for _at, mark, _s in boots if mark != running}
        if earlier:
            print(f"  previously    : {', '.join(sorted(earlier))}")

    snap = snapshot_of(root)
    if snap:
        panel = (snap.get("build") or {}).get("fingerprint")
        counters = snap.get("counters") or {}
        totals = snap.get("totals") or {}
        age = time.time() - float(snap.get("updated_at") or 0)
        print(f"  panel says    : v{snap.get('version')} {panel}  "
              f"(written {int(age)}s ago, pid {snap.get('pid')})")
        if running and panel and panel != running:
            notes.append(
                f"{label}: the panel reports {panel} but the log registered "
                f"{running} - two builds are involved"
            )
        print(f"  keys          : {totals.get('healthy', '?')} healthy of "
              f"{totals.get('keys', '?')}, {totals.get('resting', '?')} resting")
        live = [(name, value) for name, value in sorted(counters.items()) if value]
        table("this process so far", live or [("nothing yet", 0)])
        if counters.get("blamed_another_key"):
            notes.append(
                f"{label}: blamed_another_key is "
                f"{counters['blamed_another_key']} - it must be 0"
            )

    horizon = boot_at if (since_boot and boot_at) else 0.0
    window = "since this build started" if horizon else "in the whole log tail"

    def within(line: str) -> bool:
        if not horizon:
            return True
        at = moment(line)
        return at is None or at >= horizon

    rests: Counter = Counter()
    waits: Counter = Counter()
    for line in agent + errs:
        if not within(line):
            continue
        found = RESTED.search(line)
        if found:
            rests[found.group(4)] += 1
            waits[(found.group(4), found.group(6))] += 1
    print()
    if rests:
        table(f"rotations {window}, by what the key answered", rests.most_common())
        table(
            "  and how long each rest was",
            [(f"{reason} for {length}", n) for (reason, length), n in waits.most_common(8)],
            width=32,
        )
    else:
        print(f"  no rotation {window} - nothing has failed yet")

    before: Counter = Counter()
    after: Counter = Counter()
    for line in agent:
        found = VERDICT.search(line)
        if not found:
            continue
        at = moment(line) or 0.0
        key = (found.group(3), found.group(4), found.group(5))
        (after if (boot_at and at >= boot_at) else before)[key] += 1
    print()
    if after or before:
        print("  what KAME concluded, and from which evidence")
        for title, counted in (("on this build", after), ("on earlier builds", before)):
            if not counted:
                continue
            print(f"    {title}:")
            for (reason, quota, source), n in counted.most_common(8):
                print(f"      {n:>4}  {reason:<12} {quota:<11} via {source}")
        stale = sum(n for (reason, quota, _s), n in before.items()
                    if reason == "billing" and quota == "account")
        fresh = sum(n for (reason, quota, _s), n in after.items()
                    if reason == "billing" and quota == "account")
        if stale and not after:
            notes.append(
                f"{label}: {stale} refusal(s) were read as a spent account on an "
                "earlier build - the reading 1.6.0.0 fixes. Nothing has failed on "
                "this build yet, so there is nothing to compare against"
            )
        elif stale and fresh:
            notes.append(
                f"{label}: still reading {fresh} refusal(s) as a spent account "
                f"(was {stale} before this build) - worth looking at"
            )
    else:
        print("  no verdict logged - KAME has not been asked to classify anything")

    rows = journal_of(root)
    recent = [r for r in rows if not horizon or float(r.get("at") or 0) >= horizon]
    print()
    print(f"  journal       : {len(rows)} refusal(s) kept, {len(recent)} {window}")
    if recent:
        sized = Counter(str(r.get("sized_by") or "?") for r in recent)
        table("  who sized the wait", sized.most_common(), width=32)
        stated = [r for r in recent
                  if str(r.get("stated_window") or "unknown") not in ("", "unknown")]
        disagreed = [r for r in stated
                     if str(r.get("stated_window")) != str(r.get("window"))]
        if stated:
            print(f"    the provider named its own counter on {len(stated)} of "
                  f"{len(recent)}; it disagreed with KAME on {len(disagreed)}")
            if disagreed:
                notes.append(
                    f"{label}: the provider named a different window than KAME "
                    f"acted on, {len(disagreed)} time(s) - a misclassification, in "
                    "KAME's own record"
                )
        if sized.get("dropped") and not sized.get("kame"):
            notes.append(
                f"{label}: KAME sized {sized['dropped']} wait(s) and none of them "
                "reached the pool - the defect 1.6.0.0 exists to fix"
            )

    print()
    for title, pattern in GOOD:
        hits = [l for l in agent + errs if pattern.search(l) and within(l)]
        if hits:
            print(f"  {len(hits):>4}  {title}")

    shown = 0
    for title, pattern in TROUBLE:
        hits = [l for l in agent + errs if pattern.search(l) and within(l)]
        if not hits:
            continue
        if not shown:
            print("\n  worth a look:")
        shown += 1
        print(f"    {len(hits):>4}  {title}")
        if pattern is BLAMED:
            notes.append(f"{label}: {len(hits)} blame mismatch(es) in the log")
        if errors:
            for line in hits[-errors:]:
                print(f"          {line.strip()[:150]}")

    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all", action="store_true",
        help="read the whole log tail, not only since the current build started",
    )
    parser.add_argument("--profile", default="", help="one profile by name, or 'base'")
    parser.add_argument(
        "--errors", type=int, default=0,
        help="print this many of each troublesome line verbatim",
    )
    args = parser.parse_args()

    if not HERMES_HOME.is_dir():
        print(f"Hermes not found at {HERMES_HOME}")
        return 2

    wanted = roots()
    if args.profile:
        wanted = [(name, path) for name, path in wanted if name == args.profile]
        if not wanted:
            print(f"no profile named {args.profile!r}")
            return 2

    print(f"Hermes home: {HERMES_HOME}")
    print("read-only - nothing here writes anything")

    notes: List[str] = []
    for name, path in wanted:
        notes.extend(report(name, path, since_boot=not args.all, errors=args.errors))

    print("\n" + "=" * 72)
    print("what this says")
    print("=" * 72)
    if not notes:
        print("  Nothing stands out. Every profile that ran registered a complete")
        print("  build, no bench was blamed on a key that never went out, and no")
        print("  refusal was read as a spent account on this build.")
        return 0
    for note in notes:
        print(f"  - {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
