"""The one place this plugin is allowed to write to a file full of secrets.

Hermes keeps durable environment in a ``.env`` next to its config, and that
file holds API keys — the user's, for every provider they have configured. A
plugin that writes to it is one bad line away from destroying credentials that
exist nowhere else, so the rules are narrow and they are enforced here rather
than trusted to each caller:

* **only ``KAME_*``.** A name outside this plugin's own namespace is refused,
  not sanitised. There is no legitimate reason for a key-rotation plugin to
  set ``OPENAI_API_KEY``, and the day some future code path tries, this
  refuses instead of succeeding.
* **every other line survives byte for byte.** Comments, blank lines, ordering
  and every credential are copied through untouched. Only the exact variable
  being set is rewritten.
* **nothing read is ever returned or logged.** Not on success, not in an
  error, not at debug level. The return value names the file and says whether
  the write happened; it never quotes a line.

Extracted from ``menu.py`` in 1.1.1, when the settings panel became the second
caller. Two copies of a function that edits a credential store is one copy too
many: they drift, and the half that drifts is the half nobody reads.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The only prefix this module will write. Not configurable, on purpose.
NAMESPACE = "KAME_"


def path() -> Optional[Path]:
    """Hermes' own ``.env``, asked of Hermes rather than guessed at.

    Imported inside the function so this module stays importable with no
    Hermes in the process, which is how the test suite exercises it.
    """
    try:
        from hermes_cli.config import get_env_path

        found = get_env_path()
        return Path(found) if found else None
    except Exception:
        logger.debug("kame: this Hermes exposes no .env path", exc_info=True)
        return None


def _read_lines(target: Path) -> Tuple[List[str], str]:
    """The file's lines and the line ending it already uses.

    Read as bytes so the ending is observed rather than guessed: Hermes writes
    ``.env`` in text mode, which is CRLF on Windows and LF elsewhere, and a
    rewrite that changed every ending would not be "every other line survives
    byte for byte". A file that does not exist yet gets the platform's ending,
    which is what a text-mode write would have given it.
    """
    raw = target.read_bytes() if target.is_file() else b""
    if b"\r\n" in raw:
        newline = "\r\n"
    elif b"\n" in raw:
        newline = "\n"
    else:
        newline = os.linesep
    return raw.decode("utf-8").splitlines(), newline


def _replace_contents(target: Path, text: str) -> None:
    """Put ``text`` in ``target`` without ever leaving it half-written.

    1.8.1.4. This file holds every provider key the user has, and it used to be
    rewritten with ``write_text``: truncate, then write. A write that failed in
    between -- a full disk, a killed process -- left ``.env`` empty or cut, on
    every ``/kame set`` and every panel save. Now the bytes go to a temporary
    file beside it, are flushed to disk, and replace it in one rename, the same
    way Hermes' own ``_write_env_lines`` does. The file keeps its mode (0600
    stays 0600; a new file starts at 0600, not the umask's 0644), and a
    symlinked ``.env`` is written through, so the link survives.

    Where the rename itself is refused -- Windows, while another process holds
    the file open -- the old in-place write is the fallback, so this is never
    worse than before and usually much better.
    """
    real = Path(os.path.realpath(target)) if target.is_symlink() else target
    try:
        mode = stat.S_IMODE(real.stat().st_mode)
    except OSError:
        mode = 0o600
    data = text.encode("utf-8")
    handle, temporary = tempfile.mkstemp(
        dir=str(real.parent), prefix=f".{real.name}.kame-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        try:
            os.replace(temporary, real)
        except OSError:
            real.write_bytes(data)
            os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write(name: str, value: str) -> Tuple[bool, str]:
    """Set one ``KAME_*`` line. Returns ``(ok, detail)`` and never raises.

    The whole file is rewritten because there is no line-level API for it,
    which makes "everything else is left exactly as it was" a property worth
    stating rather than assuming.
    """
    if not name.startswith(NAMESPACE):
        # Belt and braces. Callers only ever pass a name that came out of
        # ``settings.env_name``, but this function edits a file full of
        # secrets, so it refuses anything outside the plugin's own namespace
        # rather than trusting its callers to stay correct forever.
        return False, f"refusing to write {name}: only KAME_* variables belong to this plugin"
    target = path()
    if target is None:
        return False, "this Hermes exposes no .env path, so the change applies to this session only"
    try:
        lines, newline = _read_lines(target)
    except Exception as exc:
        return False, f"could not read {target}: {type(exc).__name__}"

    replaced = False
    out: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(f"{name}=") and not stripped.startswith("#"):
            if not replaced:
                out.append(f"{name}={value}")
                replaced = True
            # A second assignment of the same variable is dropped rather than
            # kept: dotenv takes the last one, so leaving a stale duplicate
            # below the line just written would silently undo the write.
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append("# KAME API Rotation")
        out.append(f"{name}={value}")

    try:
        _replace_contents(target, newline.join(out) + newline)
    except Exception as exc:
        return False, f"could not write {target}: {type(exc).__name__}"
    return True, f"{'updated' if replaced else 'added'} in {target}"


def read_kame() -> Dict[str, str]:
    """Every ``KAME_*`` assignment in Hermes' ``.env``, as it stands right now.

    Only this plugin's namespace, and only the last assignment of each name —
    the same rule dotenv applies, so what this returns is what a restarted
    Hermes would actually have in its environment.

    Never raises, and returns nothing at all when there is no file to read:
    the callers use this to *offer* a refresh, and a refresh that cannot see
    the file is a refresh that changes nothing rather than an error.
    """
    target = path()
    if target is None or not target.is_file():
        return {}
    found: Dict[str, str] = {}
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.debug("kame: could not read the .env", exc_info=True)
        return {}
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(NAMESPACE) or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        if not name.startswith(NAMESPACE):
            continue
        value = value.strip()
        # dotenv strips one matched pair of quotes, so a value written by
        # another tool reads the same here as it would after a restart.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[name] = value
    return found


def forget(name: str) -> Tuple[bool, str]:
    """Remove one ``KAME_*`` line, so the setting falls back to its default.

    The other half of "Reset to defaults". Setting the variable to the default
    value would look identical on screen and be a different thing: the setting
    would still read as coming from the environment, and a later change to
    what the default *is* would not reach a user who had reset.
    """
    if not name.startswith(NAMESPACE):
        return False, f"refusing to touch {name}: only KAME_* variables belong to this plugin"
    target = path()
    if target is None:
        return False, "this Hermes exposes no .env path, so the change applies to this session only"
    if not target.is_file():
        return True, "nothing to remove"
    try:
        lines, newline = _read_lines(target)
    except Exception as exc:
        return False, f"could not read {target}: {type(exc).__name__}"
    kept = [
        line
        for line in lines
        if not (line.lstrip().startswith(f"{name}=") and not line.lstrip().startswith("#"))
    ]
    if len(kept) == len(lines):
        return True, "nothing to remove"
    try:
        _replace_contents(target, (newline.join(kept) + newline) if kept else "")
    except Exception as exc:
        return False, f"could not write {target}: {type(exc).__name__}"
    return True, f"removed from {target}"
