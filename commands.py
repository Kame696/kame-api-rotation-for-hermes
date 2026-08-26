"""The ``/kame-keys`` slash command — bulk key intake, from anywhere.

Registered through ``ctx.register_command()``, so it resolves in CLI sessions
*and* gateway sessions. That second half is the point: the gateway is what the
Android app talks to, so pasting a batch of keys from a phone works through
the same code path as typing it into the terminal.

Every Hermes import in this module is deliberately made inside the function
that needs it. The plugin must import cleanly with no agent present — that is
what lets the test suite exercise the whole thing against fakes, and what
keeps a Hermes refactor from turning an import error into a failed plugin
load.

Writes go through ``pool.add_entry()``, the same call the dashboard's
``POST /api/credentials/pool`` uses. No hand-editing of ``auth.json``, and
the file is copied to a timestamped backup before the first write of a run.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core.keys import (
    ImportPlan,
    build_labels,
    decode_text,
    format_plan_lines,
    count_usable,
    group_status,
    plan_import,
    redact,
)
from .core.multikey import split_value

logger = logging.getLogger(__name__)

COMMAND_NAME = "kame-keys"
COMMAND_DESCRIPTION = "Bulk-add and inspect pooled API keys (KAME)"
COMMAND_ARGS_HINT = "add <k1,k2,k3> | import <file> | status | reset"

DEFAULT_PROVIDER = "gemini"

# A leading provider id: letters plus separators, never a digit. See
# _split_provider for why the absence of digits is the discriminator.
_PROVIDER_NAME = re.compile(r"[A-Za-z][A-Za-z_-]*")

# Keep a few generations so a bad import is recoverable, but not so many that
# the Hermes home fills with copies of a file containing every key.
MAX_BACKUPS = 5

_HELP = """/kame-keys — bulk API key management

  /kame-keys                      show pooled keys and their health
  /kame-keys add K1,K2,K3         add keys (comma, space, or newline separated)
  /kame-keys add openrouter K1,K2 add keys to a specific provider
  /kame-keys import <file>        add every key found in a file
  /kame-keys reset [provider]     clear exhaustion status, re-enable all keys
  /kame-keys help                 this text

Default provider is `gemini`. Keys are matched against the pool before
writing, so re-running an import adds nothing. `auth.json` is backed up
before the first write.

Pasting keys into a chat puts them in the session transcript. `import`
reads from a file instead and is the safer habit when that matters."""


# ── Hermes bridges ────────────────────────────────────────────────────────
# Thin wrappers, one per thing we need from the host. Isolating them keeps
# the command logic testable by monkeypatching four small functions instead
# of standing up a fake Hermes package.


def _load_pool(provider: str):
    from agent.credential_pool import load_pool

    return load_pool(provider)


def _make_entry(provider: str, key: str, label: str):
    import uuid

    from agent.credential_pool import (
        AUTH_TYPE_API_KEY,
        PooledCredential,
        SOURCE_MANUAL,
    )

    return PooledCredential(
        provider=provider,
        id=uuid.uuid4().hex[:6],
        label=label,
        auth_type=AUTH_TYPE_API_KEY,
        priority=0,
        source=SOURCE_MANUAL,
        access_token=key,
    )


def _pooled_providers() -> List[str]:
    from hermes_cli.auth import read_credential_pool

    return sorted(read_credential_pool().keys())


def _known_providers() -> frozenset:
    """Every provider id this Hermes recognises, plus any already pooled.

    Falls back to the ids KAME cares about if the registry cannot be read.
    A short fallback only ever *narrows* what counts as a provider name, so
    the failure mode is "you must name the provider Hermes' own way", never
    "your key was stored as a provider".
    """
    names = set()
    try:
        from providers import list_providers

        for profile in list_providers():
            name = getattr(profile, "name", None) or getattr(profile, "id", None)
            if name:
                names.add(str(name).strip().lower())
    except Exception:
        logger.debug("kame: provider registry unavailable", exc_info=True)

    try:
        names.update(p.strip().lower() for p in _pooled_providers() if p)
    except Exception:
        logger.debug("kame: pooled provider list unavailable", exc_info=True)

    if not names:
        names = {"gemini", "openrouter", "anthropic", "openai-codex", "xai", "zai"}
    return frozenset(names)


def _auth_store_path() -> Optional[Path]:
    from hermes_cli.auth import get_hermes_home

    return Path(get_hermes_home()) / "auth.json"


def _unsuppress(provider: str) -> None:
    """Re-adding a key is an explicit re-engagement signal.

    Mirrors what ``hermes auth add`` and the dashboard POST both do: a source
    suppressed by an earlier removal must stop being suppressed, or the key
    the user just added silently fails to seed.
    """
    from agent.credential_pool import CUSTOM_POOL_PREFIX

    if provider.startswith(CUSTOM_POOL_PREFIX):
        return
    from hermes_cli.auth import _load_auth_store, unsuppress_credential_source

    suppressed = _load_auth_store().get("suppressed_sources", {})
    for source in list(suppressed.get(provider, []) or []):
        unsuppress_credential_source(provider, source)


# ── backup ────────────────────────────────────────────────────────────────


def _backup_auth_store() -> Optional[str]:
    """Copy auth.json aside. Returns the backup name, or None if there was
    nothing to copy. Never raises — a failed backup is reported, not fatal."""
    try:
        path = _auth_store_path()
        if path is None or not path.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"auth.json.kame-{stamp}.bak")
        shutil.copy2(path, target)
        _prune_backups(path)
        return target.name
    except Exception:
        logger.debug("kame: auth.json backup failed", exc_info=True)
        return None


def _prune_backups(path: Path) -> None:
    try:
        backups = sorted(path.parent.glob("auth.json.kame-*.bak"))
        for stale in backups[:-MAX_BACKUPS]:
            stale.unlink(missing_ok=True)
    except Exception:
        logger.debug("kame: backup prune failed", exc_info=True)


# ── argument parsing ──────────────────────────────────────────────────────


def _split_provider(rest: str) -> Tuple[str, str]:
    """Pull an optional leading provider name off the front of the payload.

    ``add gemini K1,K2`` and ``add K1,K2`` must both work, with no flag to
    say which form is in use.

    Membership in the real provider registry is the test — not length, not
    shape. Every shape-based rule fails in one direction or the other:
    ``alibaba-coding-plan`` is 19 characters of letters and hyphens and
    passes ``looks_like_api_key``, so a length rule files a provider name
    away as a credential; and a rule loose enough to catch it would swallow
    the first key of a space-separated paste. A name either is a provider
    Hermes knows about or it is not, and getting this wrong writes a
    non-credential into the pool, so the ambiguity is not worth carrying.
    """
    text = (rest or "").strip()
    if not text:
        return DEFAULT_PROVIDER, ""

    head, _, tail = text.partition(" ")
    head = head.strip().strip(",").lower()
    if head and _PROVIDER_NAME.fullmatch(head) and head in _known_providers():
        return head, tail.strip()
    return DEFAULT_PROVIDER, text


# ── subcommands ───────────────────────────────────────────────────────────


def _runtime_key(entry) -> str:
    """The key the host would actually send, by the host's own rule.

    ``access_token`` is the stored field; ``runtime_api_key`` is the property
    the pool runs on, and the two are not the same thing. A nous entry keys on
    ``agent_key`` and only while its invoke JWT is still valid, and a borrowed
    credential persists as a metadata-only row that is hydrated on load — so
    reading the stored field alone reports a live key as blank and an expired
    one as present. Falls back to ``access_token`` only if the property is
    missing entirely, which no Hermes this plugin supports does.
    """
    try:
        key = getattr(entry, "runtime_api_key", None)
        if key is not None:
            return str(key or "").strip()
    except Exception:
        # The property is computed — a nous entry consults the auth module to
        # judge its JWT. A raising entry is an unusable one, not a crash.
        logger.debug("kame: runtime key unavailable for a pool entry", exc_info=True)
        return ""
    return str(getattr(entry, "access_token", "") or "").strip()


def _is_api_key_entry(entry) -> bool:
    """Whether the host's skip rule applies to this entry at all."""
    try:
        from agent.credential_pool import AUTH_TYPE_API_KEY
    except Exception:
        # Without the host's own constant there is no way to tell an OAuth
        # entry from an API-key one, and guessing would mislabel one of them.
        # Say no: the report loses a warning, it does not gain a false one.
        logger.debug("kame: AUTH_TYPE_API_KEY unavailable", exc_info=True)
        return False
    return str(getattr(entry, "auth_type", "") or "") == AUTH_TYPE_API_KEY


def _entry_summaries(pool) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for entry in pool.entries():
        token = _runtime_key(entry)
        # The host skips any API-key entry with no runtime key: `if
        # entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
        # continue`, before status or cooldown is even looked at. Such an
        # entry is not a key that is having a bad day — it can never serve a
        # request, and counting it as one is how a pool of three keys and no
        # working credential reads as healthy.
        #
        # Only API-key entries: an OAuth entry legitimately carries no api
        # key, and calling those unusable would invent a second wrong answer
        # to replace the first.
        usable = bool(token) or not _is_api_key_entry(entry)
        # A value holding several keys is reported as the container it is.
        # Recomputed from the value rather than read off a flag, so the report
        # is right whether or not the binding that splits them is installed —
        # and a Hermes where splitting is off still says why one row shows a
        # key count instead of a key.
        holds = len(split_value(token)[0]) if _is_api_key_entry(entry) else 0
        summaries.append({
            "label": getattr(entry, "label", None) or "?",
            "token_preview": redact(token) if token else "",
            "last_status": getattr(entry, "last_status", None),
            "request_count": getattr(entry, "request_count", 0),
            "usable": usable,
            "holds": holds,
        })
    return summaries


def _cmd_status(rest: str) -> str:
    providers = [p for p in [rest.strip().lower()] if p] or _pooled_providers()
    if not providers:
        return "No pooled credentials yet. Add some with `/kame-keys add <key1,key2>`."

    lines: List[str] = []
    for provider in providers:
        try:
            pool = _load_pool(provider)
        except Exception as exc:
            lines.append(f"{provider}: could not load pool ({exc})")
            continue
        summaries = _entry_summaries(pool)
        if not summaries:
            continue
        usable = count_usable(summaries)
        # A container is not a credential in either half of the fraction. It
        # is listed, so the reader can see where the keys came from, but "6 of
        # 7 usable" for six good keys inside one row would be a complaint
        # about nothing.
        countable = len([s for s in summaries if not int(s.get("holds") or 0) > 1])
        if usable == countable:
            lines.append(f"{provider} — {countable} key(s)")
        else:
            # The count is the first thing read and the only thing some
            # readers read. A total that includes entries the pool cannot
            # pick answers "do I have keys?" with a yes that is not true.
            lines.append(
                f"{provider} — {usable} of {countable} key(s) usable"
            )
        lines.extend(group_status(summaries))

    return "\n".join(lines) if lines else "No pooled credentials yet."


def _existing_tokens(pool) -> List[str]:
    return [
        str(getattr(entry, "access_token", "") or "")
        for entry in pool.entries()
    ]


def _existing_labels(pool) -> List[str]:
    return [str(getattr(entry, "label", "") or "") for entry in pool.entries()]


def _apply_plan(provider: str, plan: ImportPlan, pool) -> str:
    if plan.is_empty:
        head = f"Nothing to add to {provider} ({plan.summary()})."
        detail = format_plan_lines(plan)
        return "\n".join([head, *detail]) if detail else head

    backup = _backup_auth_store()
    labels = build_labels(len(plan.new), taken=_existing_labels(pool))

    added = 0
    failures: List[str] = []
    for key, label in zip(plan.new, labels):
        try:
            pool.add_entry(_make_entry(provider, key, label))
            added += 1
        except Exception as exc:
            # Report which key failed by its redacted form only.
            failures.append(f"  x {redact(key)}: {exc}")

    if added:
        try:
            _unsuppress(provider)
        except Exception:
            logger.debug("kame: unsuppress after import failed", exc_info=True)

    lines = [f"Added {added} key(s) to {provider} ({plan.summary()})."]
    if backup:
        lines.append(f"Backup: {backup}")
    lines.extend(format_plan_lines(plan))
    lines.extend(failures)
    lines.append(f"Pool now holds {len(pool.entries())} key(s).")
    return "\n".join(lines)


def _cmd_add(rest: str) -> str:
    provider, payload = _split_provider(rest)
    if not payload.strip():
        return "Nothing to add. Usage: `/kame-keys add K1,K2,K3`"

    try:
        pool = _load_pool(provider)
    except Exception as exc:
        return f"Could not load the {provider} pool: {exc}"

    plan = plan_import(payload, _existing_tokens(pool))
    return _apply_plan(provider, plan, pool)


def _cmd_import(rest: str) -> str:
    provider, payload = _split_provider(rest)
    raw_path = payload.strip().strip("\"'")
    if not raw_path:
        return "Which file? Usage: `/kame-keys import <path>`"

    path = Path(raw_path).expanduser()
    try:
        # Bytes, not text: the file's own byte-order mark decides how it is
        # decoded. Reading it as UTF-8 regardless cost the first key of a
        # file Notepad saved and every key of one PowerShell redirected.
        text = decode_text(path.read_bytes())
    except FileNotFoundError:
        return f"File not found: {path}"
    except Exception as exc:
        return f"Could not read {path}: {exc}"

    try:
        pool = _load_pool(provider)
    except Exception as exc:
        return f"Could not load the {provider} pool: {exc}"

    plan = plan_import(text, _existing_tokens(pool))
    return _apply_plan(provider, plan, pool)


def _cmd_reset(rest: str) -> str:
    providers = [p for p in [rest.strip().lower()] if p] or _pooled_providers()
    if not providers:
        return "No pooled credentials to reset."

    lines: List[str] = []
    for provider in providers:
        try:
            pool = _load_pool(provider)
            count = pool.reset_statuses()
            lines.append(f"{provider}: cleared exhaustion on {count} credential(s)")
        except Exception as exc:
            lines.append(f"{provider}: reset failed ({exc})")
    return "\n".join(lines)


_SUBCOMMANDS = {
    "add": _cmd_add,
    "import": _cmd_import,
    "status": _cmd_status,
    "list": _cmd_status,
    "reset": _cmd_reset,
}


def handle(raw_args: str) -> str:
    """Slash-command entry point. Always returns text, never raises.

    A slash command that throws inside a chat turn is far worse than one that
    reports a problem, so every path here is wrapped.
    """
    try:
        text = (raw_args or "").strip()
        if not text:
            return _cmd_status("")

        verb, _, rest = text.partition(" ")
        verb = verb.strip().lower()

        if verb in {"help", "-h", "--help", "?"}:
            return _HELP

        handler = _SUBCOMMANDS.get(verb)
        if handler is None:
            return f"Unknown subcommand `{verb}`.\n\n{_HELP}"
        return handler(rest.strip())
    except Exception as exc:
        # Deliberately reports the exception type and message but never the
        # arguments — raw_args holds live keys on the `add` path.
        logger.debug("kame: /%s failed", COMMAND_NAME, exc_info=True)
        return f"/{COMMAND_NAME} failed: {type(exc).__name__}: {exc}"


def register_command(ctx) -> None:
    ctx.register_command(
        COMMAND_NAME,
        handle,
        description=COMMAND_DESCRIPTION,
        args_hint=COMMAND_ARGS_HINT,
    )
