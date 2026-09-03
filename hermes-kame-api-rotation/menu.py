"""The ``/kame`` slash command — the readout that works everywhere.

Hermes reads ``config_schema`` out of a plugin manifest and validates it, but
nothing in the shipped web UI renders it back: the Plugins page offers an
on/off toggle and nothing else. So every switch this plugin has was, before
1.0.9, real, documented, and unreachable without hand-editing a YAML file the
user had no reason to know existed. A knob nobody can find is a knob nobody
has.

Three verbs, and the split is deliberate:

``/kame``           what the pool is doing right now
``/kame get``       every setting, its value, and where that value came from
``/kame set k v``   change one, in this process and across restarts

**Plain text, and why.** 1.0.10 wrote this panel in markdown and it rendered as
markdown *source* — literal ``##`` and literal table pipes. Desktop does not
run a plugin command's output through a markdown renderer: the reply is a
system message, matched by ``SLASH_STATUS_RE`` and painted by
``LinkifiedText ... pretty={false}`` inside a ``whitespace-pre-wrap`` block
(``apps/desktop/src/components/assistant-ui/thread/system-message.tsx``). What
that row renders is text, exactly as written, so this module writes text: named
sections and one ``label: value`` per line. Not columns — the face is
proportional, so padding would produce a table that does not line up.

The real panel is not here. Since 1.1.0 the plugin ships
``desktop/plugin.js``, a Desktop UI plugin that contributes a status-bar chip
and a full KAME page; this command stays because the CLI has no such surface,
and because a readout that only exists inside a GUI is a readout you cannot
have when the GUI is what you are debugging.

``set`` writes ``KAME_*`` lines into Hermes' own ``.env`` because that is the
file Hermes already reads for durable environment, and because KAME's
precedence puts the environment above config — so a single write is both the
immediate change and the permanent one, with no restart in between. The write
is surgical: every line that is not the variable being set is copied through
byte for byte, and the file is never read into a log or into a return value.
It holds credentials, and this module only ever matches lines that begin with
``KAME_``.

``/kame-keys`` and ``/kame-quota`` stay where they are. This is the panel;
those two are the jobs big enough to have earned their own command, and one
of them writes credentials — keeping it out of here means a settings panel
can never grow a code path that touches a key.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import envfile, settings
from .core import __version__
from .core.carousel import format_duration
from .core.events import EVENTS

logger = logging.getLogger(__name__)

COMMAND_NAME = "kame"
COMMAND_DESCRIPTION = "KAME rotation status and settings (KAME)"
COMMAND_ARGS_HINT = "[get | set <key> <value> | doctor]"

#: Nominal label width. Kept as a parameter callers can pass so the intent of
#: a wide label survives in the source, but ``_row`` no longer pads: see its
#: docstring for why columns are the wrong shape on the surface this lands on.
_LABEL = 22

#: Host settings worth showing beside KAME's own, because a question about
#: KAME's behaviour is very often really a question about one of these four.
#: Defaults are Hermes' own, read from the source noted in each comment; they
#: are shown only when the variable is unset, and labelled as the host's.
_HOST_VARS = (
    ("HERMES_STREAM_RETRIES", "2", "reconnects inside one attempt (not a second go at a spent key)"),
    ("HERMES_STREAM_READ_TIMEOUT", "120", "silence allowed inside one attempt"),
    ("HERMES_STREAM_STALE_TIMEOUT", "180", "before Hermes calls a stream stale"),
    ("HERMES_STREAM_STALE_GIVEUP", "5", "stale streams before Hermes refuses the call itself"),
)

_HELP = """/kame — rotation status and settings

  /kame                  what the pool is doing right now
  /kame get              every setting, its value, and where it came from
  /kame set <key> <v>    change one setting, now and after a restart
  /kame reset <key>      put one setting back to its default
  /kame events           the last rotations, quarantines and cut streams
  /kame doctor           is it rotating correctly? build, pools, rests, trouble
  /kame help             this text

`set` writes to the .env file Hermes reads on start, and the environment
outranks the plugin config, so the change is live immediately *and* survives
a restart without anyone editing YAML. Only lines beginning with KAME_ are
ever touched, and the file is never printed back.

Switches take true/false, on/off, or 1/0. Numbers are in seconds.

Related: /kame-keys adds pooled keys in bulk, /kame-quota shows what is
benched per model and what KAME has learned about each deadline."""


def _wrap(text: str, width: int) -> List[str]:
    """``text`` broken onto lines of at most ``width`` characters.

    ``textwrap`` would do this, and is not imported for one sentence in a reply
    that is otherwise built from string joins — the whole rule here is "break
    on a space, never inside a word", and a word longer than the width goes on
    its own line rather than being cut.
    """
    lines: List[str] = []
    current = ""
    for word in str(text or "").split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _row(label: str, value: str, width: int = _LABEL) -> str:
    """One ``label: value`` line, indented two spaces.

    Deliberately *not* space-padded into columns. Desktop renders this reply in
    a proportional face inside a `whitespace-pre-wrap` block
    (``LinkifiedText ... pretty={false}``), so padding produces a ragged
    almost-table rather than a table — worse to read than plain pairs, and it
    only lines up in the CLI. The panel that earns columns is the page, where
    a real layout is available; here the shape that survives both surfaces is
    one fact per line.

    ``width`` is accepted so callers can express intent, and ignored.
    """
    return f"  {label}: {value}"


#: Writing to a file full of credentials lives in one module, ``envfile``, and
#: this command and the settings panel both go through it. Two copies of that
#: function drift, and the half that drifts is the half nobody reads — so the
#: copy that used to be here was moved out in 1.1.1 rather than duplicated.
_write_env = envfile.write


class MenuCommand:
    """The panel. Reads live state; writes only KAME's own settings."""

    def __init__(self, binding: Any = None) -> None:
        self._binding = binding

    # -- what is happening now --------------------------------------------

    def _rotation_lines(self) -> List[str]:
        """Counters, or the reason there are none.

        Read off the live binding rather than off a store, because the
        interesting question here is about *this* process: an install that
        refused and an install that has simply had nothing to do look
        identical from the outside, and only ``reason`` tells them apart.
        """
        binding = self._binding
        if binding is None:
            return [
                "Rotation: NOT INSTALLED",
                "",
                "  Every call carries the key Hermes resolved, and a failure follows",
                "  Hermes' own retry rules. The log line beginning",
                "  'kame: every call keeps the host's key' says why.",
            ]

        # Every number below is for this Hermes process, not for this chat.
        # One process serves every open conversation, the auxiliary lane and
        # every subagent through one binding, and the counters are the
        # binding's. Saying so is the difference between "KAME rotated twice
        # for me" and the truth, which is that it rotated twice for somebody.
        lines = ["In this Hermes process, across every conversation it is serving:", ""]
        lines.append(_row("calls", str(getattr(binding, "calls", 0))))
        lines.append(_row("rotations", str(getattr(binding, "rotations", 0))))
        lines.append(_row("recovered", str(getattr(binding, "recovered", 0))))
        lines.append(_row("surfaced", str(getattr(binding, "surfaced", 0))))
        waits = getattr(binding, "waits", 0)
        if waits:
            lines.append(
                _row(
                    "waited",
                    "{dur} across {n} pause(s), rather than failing the turn".format(
                        dur=format_duration(getattr(binding, "waited_s", 0.0)), n=waits
                    ),
                )
            )
        drops = getattr(binding, "stitched", 0)
        if drops:
            lines.append(
                _row(
                    "continued",
                    f"{drops} answer(s) finished on another key after the stream "
                    "was cut, delivered as one reply",
                )
            )
        in_tool_call = getattr(binding, "tool_call_cuts", 0)
        if in_tool_call:
            # Separated from the line below since 1.1.3. These cannot be
            # continued by anything — the arguments of the call were still
            # being written — so a reader who sees this number climbing is
            # looking at a key that cannot hold a long stream, not at a
            # feature that could have saved the turn.
            lines.append(
                _row(
                    "cut in a tool call",
                    f"{in_tool_call} stream(s) stopped while a tool call was "
                    "being written; a half-written call cannot be continued",
                )
            )
        cuts = getattr(binding, "mid_stream_cuts", 0)
        if cuts:
            # The F2 diagnostic. Worth its paragraph: this is the number that
            # explains "rewind stopped working", and there is nowhere else in
            # Hermes it can be read.
            lines.append(_row("cut mid-stream", f"{cuts} answer(s), handed back to Hermes"))
            lines.append("")
            lines.append(
                "  Each one makes Hermes continue the answer from a synthetic row"
            )
            lines.append(
                "  ('[System: The previous response was cut off...]') that moves the"
            )
            lines.append(
                "  server's ordinal without moving the client's, which is what makes"
            )
            lines.append(
                "  rewind and edit refuse later in a session. Raising"
            )
            lines.append(
                "  HERMES_STREAM_STALE_TIMEOUT is the lever; the ordinal arithmetic"
            )
            lines.append("  itself is the host's, not KAME's.")
            lines.append("")
            lines.append(
                "  Since 1.1.1 this only happens when the answer could not be"
            )
            lines.append(
                "  continued: the cut was inside a tool call, stream_resume_limit"
            )
            lines.append(
                "  was spent, or stream_stitch_disabled is on."
            )
        return lines

    def _pool_lines(self) -> List[str]:
        """Health per ``provider:model``, with the ETA when nothing is ready."""
        try:
            from .core.carousel import ENGINE

            pools: Dict[str, Dict[str, Any]] = ENGINE.snapshot()
        except Exception:
            logger.debug("kame: could not read the pool", exc_info=True)
            return ["Pool: could not be read — see the log."]
        if not pools:
            return [
                "Pool:",
                "",
                "  Nothing recorded yet — no call has failed this session, which is",
                "  the state you want it in.",
            ]
        lines = ["Pool health, per provider and model:", ""]
        for identity in sorted(pools):
            row = pools[identity]
            soonest = row.get("soonest")
            parts = [f"{row['healthy']}/{row['keys']} healthy"]
            resting = row.get("resting", 0)
            if resting:
                parts.append(f"{resting} resting")
            if soonest is not None:
                parts.append(f"next back in {format_duration(soonest)}")
            if row.get("kinds"):
                parts.append(", ".join(row["kinds"]))
            lines.append(_row(identity, "   ".join(parts), max(_LABEL, 28)))
        return lines

    def _repair_lines(self) -> List[str]:
        """Whether the Gemini parallel-tool-call repair is in place.

        Shown unconditionally, including when it is off, because "why am I
        still seeing 'Response truncated due to output length limit'" needs an
        answer that is not silence.
        """
        try:
            from . import gemini_slots

            report = gemini_slots.report()
        except Exception:
            logger.debug("kame: could not read the Gemini repair state", exc_info=True)
            return []
        lines = ["Gemini parallel tool-call repair:", ""]
        if report.get("applied"):
            lines.append(_row("status", "in place"))
            lines.append(_row("calls separated", str(report.get("repaired", 0))))
        else:
            lines.append(_row("status", "not applied"))
            lines.append(_row("because", str(report.get("reason") or "unknown")))
        lines.append("")
        lines.append(
            "  Two parallel calls to one tool arrive under the same slot key and"
        )
        lines.append(
            "  their arguments are concatenated into one unparseable string, which"
        )
        lines.append(
            "  Hermes reports as 'Response truncated due to output length limit'."
        )
        lines.append("  Host bug; KAME repairs the stream on the way past.")
        return lines

    def _host_lines(self) -> List[str]:
        """The four host variables KAME reasons about, and who set them.

        KAME reads these and changes none of them. v1.0.9 did change one, and
        the note under the table is why that was a mistake worth naming rather
        than quietly reverting.
        """
        lines = ["Host settings KAME reads:", ""]
        for name, default, what in _HOST_VARS:
            value = os.environ.get(name)
            shown = value if value is not None else f"{default} (host default)"
            lines.append(_row(name, f"{shown}  ({what})", 30))
        if settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0) > 0:
            lines.append("")
            lines.append(
                "  KAME lowers HERMES_STREAM_READ_TIMEOUT around one attempt and puts"
            )
            lines.append(
                "  it straight back, because stream_silence_timeout_seconds is set."
            )
            lines.append(
                "  It never does this if you set the variable yourself, and never for"
            )
            lines.append("  a local endpoint. Set that setting to 0 to stop it entirely.")
        else:
            lines.append("")
            lines.append("  KAME sets none of them.")
        return lines

    def _desktop_lines(self) -> List[str]:
        """Where the real panel is, and whether it can be fed."""
        try:
            from . import state

            path = state.state_path()
            reason = state.disabled_reason()
        except Exception:
            return []
        lines = ["The live panel:", ""]
        lines.append("  Sidebar > KAME API Rotation for the page, and the status bar")
        lines.append("  chip for pool health at a glance — both are on at all times,")
        lines.append("  not only mid-turn.")
        try:
            from . import desktop_ui

            installed = desktop_ui.report()
            if not installed.get("installed"):
                lines.append("")
                lines.append(_row("panel", f"NOT installed: {installed.get('reason') or 'unknown'}"))
        except Exception:
            logger.debug("kame: could not read the desktop install state", exc_info=True)
        if reason:
            lines.append("")
            lines.append(_row("snapshot", f"NOT being written: {reason}"))
        elif path is not None:
            lines.append("")
            lines.append(_row("snapshot", str(path)))
        return lines

    @staticmethod
    def _fingerprint() -> str:
        """A digest of the modules that loaded, or a word saying why not.

        Never raises. This is a line in a status screen, and a status screen
        that cannot be printed is worse than one missing a line.
        """
        try:
            from . import integrity

            result = integrity.verify()
            mark = str(result.get("fingerprint") or "")
            if not mark:
                return "unknown"
            if not result.get("complete"):
                missing = result.get("missing_required") or []
                return f"{mark} — INCOMPLETE, {len(missing)} module(s) missing"
            return mark
        except Exception:  # pragma: no cover — a hash over files already read
            return "unknown"

    def show(self) -> str:
        """The panel, as text.

        Desktop paints a plugin command's reply with ``pretty={false}`` inside
        a ``whitespace-pre-wrap`` block, so what is written here is what is
        read: padded columns, no markdown, nothing that needs a renderer.
        """
        reason = getattr(self._binding, "reason", None) if self._binding else None
        head = f"KAME API Rotation {__version__}"
        out: List[str] = [f"{head} — {reason}" if reason else head, ""]
        out.append(f"  loaded from {Path(__file__).resolve().parent}")
        # The build, not the version number. A version is what the manifest
        # claims; the fingerprint is a digest of the code that actually
        # loaded, so it is the only thing that can tell a deploy that landed
        # from a deploy that was written somewhere else and verified there —
        # which is how 1.0.8 was lost. The panel has shown it since 1.4.0;
        # this command had not, which left the one check worth making after
        # an upgrade available only to whoever opens the sidebar.
        out.append(f"  build {self._fingerprint()}")
        out.append("")
        out.extend(self._desktop_lines())
        out.append("")
        out.extend(self._rotation_lines())
        out.append("")
        out.extend(self._pool_lines())
        out.append("")
        out.extend(self._repair_lines())
        out.append("")
        out.extend(self._host_lines())
        out.append("")
        out.append("More:")
        out.append("")
        out.append(_row("/kame get", "every setting and where it came from"))
        out.append(_row("/kame set <key> <v>", "change one, live and permanent"))
        out.append(_row("/kame reset <key>", "put one setting back to its default"))
        out.append(_row("/kame events", "the last rotations, quarantines and cut streams"))
        out.append(_row("/kame-quota", "the quota ledger, per key and model"))
        out.append(_row("/kame-keys", "add pooled keys in bulk"))
        return "\n".join(out)

    # -- settings ---------------------------------------------------------

    @staticmethod
    def _value_of(key: str) -> str:
        """One setting's value as a person reads it, with where it came from."""
        if key in settings.ALL_NUMBERS:
            value = settings.number(key, settings.ALL_NUMBERS[key])
            # The unit comes from the setting, not from the formatter. A
            # resume budget printed as "3s" is a number nobody can act on.
            unit = settings.UNITS.get(key, "")
            shown = f"{value:g} {unit}".strip() if value else "0 (off)"
        else:
            shown = str(settings.is_on(key)).lower()
        return f"{shown}  ({settings.provenance(key)})"

    def get(self) -> str:
        rows = [
            f"KAME settings — {__version__}",
            "",
            "The value in force, and where it came from. KAME works with none",
            "of these touched.",
        ]
        # Grouped the same way, and for the same reason, as the panel: twelve
        # names in one column read as twelve equal knobs, and they are not —
        # one is an optional extra, two are tuning, and the rest turn parts of
        # the plugin off. The shelves come from `settings`, so this listing and
        # the panel cannot disagree about which is which.
        shown: set = set()
        for group in settings.groups():
            keys = [key for key in group["keys"] if settings.known(key)]
            if not keys:
                continue
            rows.append("")
            rows.append(f"  {group['title']}")
            for line in _wrap(str(group["note"]), 66):
                rows.append(f"  {line}")
            rows.append("")
            for key in keys:
                shown.add(key)
                rows.append(_row(key, self._value_of(key), 34))
        leftover = [
            key
            for key in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS)
            if key not in shown
        ]
        if leftover:
            rows.append("")
            rows.append("  Other")
            rows.append("")
            for key in leftover:
                rows.append(_row(key, self._value_of(key), 34))
        rows.append("")
        rows.append("Precedence is environment, then config, then default. /kame set")
        rows.append("writes the environment one, so it takes effect on the next call and")
        rows.append("stays set after a restart. These are settings for the process, not")
        rows.append("for this chat: a change here applies to every conversation Hermes")
        rows.append("is serving, which is what a pool of keys shared between them needs.")
        return "\n".join(rows)

    def set(self, key: str, raw: str) -> str:
        # A name this setting used to carry is answered under its current one,
        # the same way the config file and the environment answer to both.
        key = settings.canonical(key)
        raw = raw.strip()
        if not settings.known(key):
            listing = "\n".join(
                f"  {name}" for name in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS)
            )
            return f"Unknown setting {key}. KAME understands:\n{listing}"
        variable = settings.env_name(key)
        if not variable:
            return f"{key} has no environment variable, so it cannot be set from here."

        # One validator, shared with the panel since 1.1.1. Two of them is how
        # a command and a page start disagreeing about what 2.5 means for a
        # setting that counts whole things.
        value, error = settings.parse(key, raw)
        if value is None:
            return f"{error}."

        # The process environment first, so the change is live even if the
        # file write fails — a setting that took effect but was not persisted
        # is a much better outcome than one that did neither.
        os.environ[variable] = value
        ok, detail = _write_env(variable, value)
        head = (
            f"{key} = {settings.effective(key)!r}, in force from the next call, "
            "in every conversation this Hermes is serving"
        )
        if ok:
            return f"{head}\n{detail}\nNo restart needed."
        return (
            f"{head}\nNot made permanent: {detail}\n"
            f"To keep it across restarts, add {variable}={value} to Hermes' .env yourself."
        )

    def reset(self, key: str) -> str:
        """Put one setting back to its default, everywhere it was written.

        Not "set it to the default value": that would leave the setting reading
        as coming from the environment, and a later change to what the default
        *is* would never reach anyone who had reset. The line is removed
        instead, including the line under the name this setting used to have.
        """
        key = settings.canonical(key)
        if not settings.known(key):
            return f"Unknown setting {key}. /kame get lists them."
        removed: List[str] = []
        for variable in (settings.env_name(key), settings._LEGACY_ENV_FOR.get(key, "")):
            if not variable:
                continue
            os.environ.pop(variable, None)
            ok, detail = envfile.forget(variable)
            if not ok:
                return f"{key} was reset for this session only: {detail}"
            removed.append(detail)
        return (
            f"{key} = {settings.effective(key)!r}, back to its default "
            f"({settings.provenance(key)}).\n" + "\n".join(removed)
        )

    def events(self, limit: int = 20) -> str:
        """The recent decisions, newest first. Fingerprints only — never a key."""
        rows = EVENTS.recent(limit)
        if not rows:
            return (
                "KAME events\n\n"
                "  Nothing recorded yet. Rotations, quarantines, cut streams and\n"
                "  continuations appear here as they happen."
            )
        import time as _time

        out = [f"KAME events — the last {len(rows)}, newest first", ""]
        for row in rows:
            when = _time.strftime("%H:%M:%S", _time.localtime(row.get("at", 0)))
            parts = [row.get("kind", "?")]
            if row.get("identity"):
                parts.append(str(row["identity"]))
            if row.get("key"):
                parts.append(str(row["key"]))
            if row.get("reason"):
                parts.append(str(row["reason"]))
            if row.get("code"):
                parts.append(f"[{row['code']}]")
            if row.get("seconds"):
                parts.append(f"rested {format_duration(row['seconds'])}")
            out.append(f"  {when}  " + "  ".join(parts))
        return "\n".join(out)

    def doctor(self) -> str:
        """The diagnostic, from inside the process it is diagnosing.

        This used to be a script in the repository, which meant the one moment
        it was needed — a fresh install, somebody else's machine, an assistant
        that has never seen this codebase — was the one moment nobody had it.
        The reading lives with the code now.

        Everything it needs is already in memory: the snapshot this process
        publishes for the panel, and the journal it has been keeping since
        1.0.0. Nothing is read off disk and nothing is written.
        """
        import sys as _sys
        import time as _time

        from .core import doctor as doctor_mod

        try:
            from . import state

            snapshot = state.snapshot(self._binding)
            # `snapshot()` builds this process's own section and nothing else;
            # the panel works the neighbours out from the document it read.
            # The doctor has no document, so it asks for them — and they are
            # the most useful thing on the panel's Overview, because a gateway
            # on last month's build is why a fix that is definitely installed
            # is definitely not working on half the traffic.
            snapshot["neighbours"] = state.neighbours()
            # The fingerprint is normally the one `register()` computed, and
            # recomputing it on a heartbeat would be a lot of disk for an
            # answer that cannot change without a restart. This is not a
            # heartbeat: it is somebody typing `doctor` because they want to
            # know which build this is, so when the registered value is
            # missing — the plugin imported outside a running Hermes, which is
            # exactly the reinstall case this command exists for — it is
            # computed here rather than printed as a question mark.
            build = snapshot.setdefault("build", {})
            if not build.get("fingerprint"):
                from . import integrity

                report = integrity.verify()
                build["fingerprint"] = report.get("fingerprint") or ""
                build["complete"] = bool(report.get("complete", True))
                build["missing"] = list(report.get("missing_required") or [])
        except Exception:
            logger.debug("kame: doctor could not read the snapshot", exc_info=True)
            return (
                "KAME doctor\n\n"
                "  This process could not read its own state. That is itself\n"
                "  the finding: see the Hermes log for the exception."
            )

        kinds = []
        try:
            from .core import journal as journal_mod

            # The pool binding owns the journal store; the menu is handed the
            # *dispatch* binding, which is a different object. Reached through
            # the package rather than through a parameter so the doctor still
            # works in a session where only one of the two installed.
            package = _sys.modules.get(__name__.rsplit(".", 1)[0])
            pool = getattr(package, "_binding", None)
            store = getattr(pool, "_journal", None)
            if store is not None:
                kinds = journal_mod.count_kinds(store.load(), now=_time.time())
        except Exception:
            # A doctor that refuses to speak because one of its inputs is
            # missing is worse than one that says less. The build, the pools
            # and the trouble list do not need the journal.
            logger.debug("kame: doctor could not read the journal", exc_info=True)

        return doctor_mod.render(snapshot, kinds)

    # -- entry point ------------------------------------------------------

    def handle(self, raw_args: str = "") -> str:
        """Always returns text, never raises.

        A slash command that throws inside a chat turn is far worse than one
        that reports a problem.
        """
        try:
            parts = (raw_args or "").strip().split()
            if not parts:
                return self.show()
            verb = parts[0].lower()
            if verb in {"help", "-h", "--help", "?"}:
                return _HELP
            if verb in {"get", "settings", "config"}:
                return self.get()
            if verb == "set":
                if len(parts) < 3:
                    return "Usage: /kame set <key> <value>   (/kame get lists the keys)"
                return self.set(parts[1], " ".join(parts[2:]))
            if verb == "reset":
                if len(parts) < 2:
                    return "Usage: /kame reset <key>   (/kame get lists the keys)"
                return self.reset(parts[1])
            if verb in {"events", "log", "history"}:
                return self.events()
            if verb in {"doctor", "check", "diagnose"}:
                return self.doctor()
            return f"Unknown subcommand {verb}.\n\n{_HELP}"
        except Exception as exc:
            logger.debug("kame: /%s failed", COMMAND_NAME, exc_info=True)
            return f"/{COMMAND_NAME} failed: {type(exc).__name__}: {exc}"


def register_command(ctx, *, binding: Any = None) -> MenuCommand:
    command = MenuCommand(binding)
    ctx.register_command(
        COMMAND_NAME,
        command.handle,
        description=COMMAND_DESCRIPTION,
        args_hint=COMMAND_ARGS_HINT,
    )
    return command
