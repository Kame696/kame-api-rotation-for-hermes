"""Proving the plugin on disk is a whole plugin, before it claims to be one.

This module exists because of ten days that were spent debugging code that was
never running.

The install in the user's Hermes said ``version: 1.3.3``. Its
``dispatch_binding.py`` was 107 KB of 1.3.3 and opened with ``from .core import
multikey, stitch``. **There was no ``core/`` directory.** The copy that
produced it had not recursed: the package's ``.pyc`` files were sitting in the
package root instead of in ``__pycache__/``, which is the signature of a
non-recursive file copy, and the whole subpackage had been dropped on the way.

The import raised, the guards caught it, and the snapshot published:

    {"version": "1.3.3", "installed": true, "reason": "active",
     "first_run": true, "pools": [], "events": [],
     "counters": {"calls": 0, "rotations": 0, ...}}

**Registered, "active", and completely inert.** Every counter zero, every pool
empty, for nine days, while a person read `1.3.3` on the panel and reasonably
concluded that 1.3.3 was what they were testing.

Three separate things went wrong and this module answers all three:

1. **The version came from a manifest.** ``plugin.yaml`` is a text file that a
   partial copy updates as readily as a complete one, so it says what the last
   writer intended, not what is there. :func:`fingerprint` is computed from the
   bytes actually on disk, so it cannot describe files that are missing.
2. **A missing module was reported as health.** ``installed: true`` /
   ``reason: "active"`` were true statements about *registration* and worthless
   statements about *function*. :func:`verify` names what is absent.
3. **Nothing compared what was deployed with what was loaded.** A deploy that
   writes to one directory while Hermes loads another cannot be caught by
   checking the write. It can be caught by asking the running plugin what it
   is, which is what the fingerprint is for.

Nothing here raises. A plugin that crashes while checking whether it is
complete has done more damage than the incompleteness it was checking for.
"""

from __future__ import annotations

import hashlib
import os
from typing import Dict, List, Tuple

#: The modules without which rotation is not rotation. Not every file — a
#: missing `menu.py` costs a slash command and is worth reporting rather than
#: refusing over. These are the ones whose absence means the carousel cannot
#: turn, and every one of them is reached by an import at register time, so a
#: gap here is a gap that would have surfaced as a traceback nobody saw.
REQUIRED_MODULES: Tuple[str, ...] = (
    "dispatch_binding.py",
    "pool_binding.py",
    "settings.py",
    "state.py",
    "host_text.py",
    "core/__init__.py",
    "core/carousel.py",
    "core/catalog.py",
    "core/classify.py",
    "core/evidence.py",
    "core/events.py",
    "core/quota.py",
    "core/redact.py",
    "core/storm.py",
    "core/stitch.py",
    "core/multikey.py",
)

#: Reported but never fatal: the plugin works without them, less well.
OPTIONAL_MODULES: Tuple[str, ...] = (
    "commands.py",
    "menu.py",
    "control.py",
    "envfile.py",
    "gemini_slots.py",
    "desktop-ui/plugin.js",
)

#: Directories the fingerprint walks past. Generated caches, and the two trees
#: that prove the plugin rather than being it — the published repository keeps
#: ``tests/`` and ``tools/`` beside a flattened copy of the plugin, and the
#: archive contains neither. Kept in step with ``tools/package.py``'s
#: ``SKIP_DIRS`` so that what is hashed is what ships; a test asserts they agree.
_NOT_SHIPPED = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", "graphify-out", "tests", "tools"}
)


def _root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except Exception:
        return b""


def missing_modules(root: str = "") -> Tuple[List[str], List[str]]:
    """``(required_missing, optional_missing)`` — by looking, not by importing.

    Deliberately a filesystem check rather than an import attempt. An import
    tells you whether *this* interpreter can load the module right now, which
    is a different and later question, and it runs arbitrary module-level code
    to find out. Existence is the question being asked here, and a stat answers
    it without side effects.
    """
    base = root or _root()
    required = [
        name for name in REQUIRED_MODULES
        if not os.path.isfile(os.path.join(base, *name.split("/")))
    ]
    optional = [
        name for name in OPTIONAL_MODULES
        if not os.path.isfile(os.path.join(base, *name.split("/")))
    ]
    return required, optional


def fingerprint(root: str = "") -> str:
    """Twelve hex characters naming the source tree that is actually here.

    Computed over every ``.py`` and ``.js`` file the package contains — paths
    included, so a file that vanished changes the answer as surely as a file
    that was edited. ``__pycache__`` is skipped: compiled bytecode is a
    derivative, and on the broken install it was stale relative to its own
    sources, which would have made the fingerprint describe neither.

    ``_NOT_SHIPPED`` is skipped for a different reason, and it is the whole
    point of the number. The published repository keeps the plugin flattened at
    its root with ``tests/`` and ``tools/`` beside it; the zip contains neither.
    Without this, a checkout and the archive built from that exact checkout
    fingerprint differently — and a handshake that disagrees with itself when
    nothing is wrong teaches people to ignore it. The set matches the packager's
    ``SKIP_DIRS`` plus the two directories that hold the proofs rather than the
    plugin, so what is hashed is what ships.

    Short on purpose. It goes on a status line and into a log, where it is read
    by a human comparing two numbers, and twelve characters is far past the
    collision risk that matters for "did the thing I built reach the thing that
    is running".
    """
    base = root or _root()
    digest = hashlib.sha256()
    try:
        for directory, subdirs, files in os.walk(base):
            subdirs[:] = sorted(d for d in subdirs if d not in _NOT_SHIPPED)
            for name in sorted(files):
                if not name.endswith((".py", ".js")):
                    continue
                full = os.path.join(directory, name)
                relative = os.path.relpath(full, base).replace(os.sep, "/")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(_read(full))
                digest.update(b"\0")
    except Exception:
        return "unknown"
    return digest.hexdigest()[:12]


def verify(root: str = "") -> Dict[str, object]:
    """Everything a human or a deploy needs to know about this install.

    ``complete`` is the only field that gates behaviour. The rest is for
    saying *what* is wrong, because "KAME did not start" and "KAME started
    without its engine" look identical from a chat window and have completely
    different fixes.
    """
    base = root or _root()
    required, optional = missing_modules(base)
    return {
        "complete": not required,
        "missing_required": required,
        "missing_optional": optional,
        "fingerprint": fingerprint(base),
        "root": base,
    }


def describe(report: Dict[str, object]) -> str:
    """One sentence, for the log and the status line.

    Written to be actionable in the exact situation that produced it: somebody
    copied the plugin and lost a directory, and the only signal they had was a
    version number that was right about intent and wrong about fact.
    """
    if report.get("complete"):
        missing_optional = report.get("missing_optional") or []
        if missing_optional:
            return (
                f"build {report.get('fingerprint')} — complete, without "
                f"{', '.join(str(m) for m in missing_optional)}"
            )
        return f"build {report.get('fingerprint')} — complete"
    missing = report.get("missing_required") or []
    return (
        "this install is incomplete and KAME will not rotate with it: "
        + ", ".join(str(m) for m in missing)
        + f" missing from {report.get('root')}. Re-deploy the whole package "
        "directory — a copy that does not recurse drops core/ and leaves a "
        "plugin that registers, reports itself active, and does nothing."
    )
