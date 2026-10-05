"""A key variable that holds several keys, kept as one pool row per key.

Until 1.8.1.8, ``GOOGLE_API_KEY=k1,k2,k3`` worked everywhere because KAME
wrapped Hermes' resolver and pool loader and split the value in memory
(``resolver_binding``, ``pool_binding``). The catalog forbids those wraps, and
without them Hermes reads such a value as one key: every path that does not
go through KAME's client sends the whole list as a single credential, which
every provider refuses. 1.8.1.9 first asked the user to run
``/kame-keys split`` by hand; this does the same thing on its own, at every
start, and keeps doing it as the variable changes.

What it writes, and only through Hermes' public pool API:

* one ``manual`` row per key the variable holds, tagged with the source
  ``manual:kame-env:<VAR>`` so a later start knows which rows it owns;
* a suppression of the ``env:<VAR>`` source, the same marker
  ``hermes auth remove`` leaves, so Hermes does not also seed the whole list
  as one key.

And how it follows the variable:

* a key added to the variable is added to the pool at the next start;
* a key removed from the variable is removed from the pool — only rows this
  module made, never a row the user added by hand;
* a variable that goes back to one key (or away) gets its rows removed and
  its source unsuppressed, so Hermes seeds it exactly as it would have.

Nothing else about the pool changes: statuses, cooldowns and priorities of
existing rows are left alone. The variable itself is never written. With
``resolver_disabled`` (or ``KAME_RESOLVER_DISABLED=1``) the module does
nothing, and ``/kame-keys split`` remains for doing it once by hand.

Never raises: a start that cannot sync is a start with the pool as it was.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from . import settings
from .core.multikey import split_value

logger = logging.getLogger(__name__)

SOURCE_PREFIX = "manual:kame-env:"


def _source_for(var: str) -> str:
    return f"{SOURCE_PREFIX}{var}"


def _env_value(var: str) -> str:
    try:
        from agent.credential_pool import get_env_prefer_dotenv

        return str(get_env_prefer_dotenv(var) or "")
    except Exception:
        import os

        return os.environ.get(var, "")


def _variables() -> List[Tuple[str, str, str]]:
    """``(provider, VAR, base_url)`` for every API-key variable Hermes declares."""
    out: List[Tuple[str, str, str]] = []
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        return out
    seen = set()
    for provider_id, config in PROVIDER_REGISTRY.items():
        if getattr(config, "auth_type", "api_key") != "api_key":
            continue
        # The registry also lists aliases ("google", "nim", ...) once provider
        # profiles are registered; a pool belongs to the canonical id only.
        canonical = str(getattr(config, "id", "") or provider_id)
        if canonical != str(provider_id):
            try:
                from hermes_cli.providers import normalize_provider

                canonical = normalize_provider(canonical) or canonical
            except Exception:
                pass
        if canonical != str(provider_id) or canonical in seen:
            continue
        seen.add(canonical)
        base_url = ""
        env_url_var = getattr(config, "base_url_env_var", "") or ""
        if env_url_var:
            base_url = _env_value(env_url_var).rstrip("/")
        base_url = base_url or str(getattr(config, "inference_base_url", "") or "")
        for var in getattr(config, "api_key_env_vars", ()) or ():
            out.append((str(provider_id), str(var), base_url))
    return out


def _lock() -> Any:
    try:
        from hermes_cli.auth import _auth_store_lock

        return _auth_store_lock()
    except Exception:
        return contextlib.nullcontext()


def _token(entry: Any) -> str:
    return str(getattr(entry, "access_token", "") or "")


def _remove(pool: Any, entry: Any) -> bool:
    index, _found, _error = pool.resolve_target(getattr(entry, "id", ""))
    if index is None:
        return False
    pool.remove_index(index)
    return True


def _sync_one(provider: str, var: str, base_url: str, report: List[str], backup: Any) -> None:
    from agent.credential_pool import load_pool
    from hermes_cli.auth import (
        is_source_suppressed,
        suppress_credential_source,
        unsuppress_credential_source,
    )

    keys = split_value(_env_value(var))[0]
    source = _source_for(var)
    env_source = f"env:{var}"
    several = len(keys) > 1

    pool = load_pool(provider)
    ours = [e for e in pool.entries() if str(getattr(e, "source", "")) == source]
    if not several and not ours:
        return

    if not several:
        backup()
        for entry in ours:
            _remove(pool, entry)
        if is_source_suppressed(provider, env_source):
            unsuppress_credential_source(provider, env_source)
        report.append(f"{provider}: {var} holds one key again; {len(ours)} KAME row(s) removed")
        return

    from . import commands

    wanted = set(keys)
    stale = [e for e in ours if _token(e) not in wanted]
    listed = [e for e in pool.entries()
              if str(getattr(e, "source", "")) == env_source and len(split_value(_token(e))[0]) > 1]
    tokens = [_token(e) for e in ours]
    missing = [k for k in keys if k not in {_token(e) for e in pool.entries()}]
    if (not stale and not listed and not missing and len(tokens) == len(set(tokens))
            and is_source_suppressed(provider, env_source)):
        return
    backup()
    removed = 0
    seen: Dict[str, Any] = {}
    for entry in ours:
        token = _token(entry)
        # A key the variable no longer holds, or a duplicate left by a second
        # process syncing at the same moment.
        if token not in wanted or token in seen:
            removed += _remove(pool, entry)
        else:
            seen[token] = entry

    # The comma-list row Hermes seeded before the suppression existed.
    for entry in list(pool.entries()):
        if str(getattr(entry, "source", "")) == env_source and len(split_value(_token(entry))[0]) > 1:
            removed += _remove(pool, entry)

    existing = {_token(e) for e in pool.entries()}
    new = [k for k in keys if k not in existing]
    added = 0
    if new:
        labels = commands.build_labels(len(new), taken=[str(getattr(e, "label", "") or "") for e in pool.entries()])
        from dataclasses import replace

        for key, label in zip(new, labels):
            entry = commands._make_entry(provider, key, f"{var} {label}")
            entry = replace(entry, source=source, base_url=base_url or None)
            pool.add_entry(entry)
            added += 1

    if not is_source_suppressed(provider, env_source):
        suppress_credential_source(provider, env_source)
    if added or removed:
        report.append(f"{provider}: {var} holds {len(keys)} keys; {added} row(s) added, {removed} removed")


def sync() -> List[str]:
    """Bring the pool in line with every multi-key variable. Returns what changed."""
    report: List[str] = []
    if settings.is_on(settings.RESOLVER_DISABLED) or settings.is_on(settings.ROTATION_DISABLED):
        return report
    variables = _variables()
    pending = [(p, v, u) for p, v, u in variables if len(split_value(_env_value(v))[0]) > 1]
    try:
        from hermes_cli.auth import read_credential_pool

        pooled = read_credential_pool()
    except Exception:
        pooled = {}
    # Variables that were lists at an earlier start and may need their rows taken back.
    for provider, var, url in variables:
        rows = pooled.get(provider) or []
        if (provider, var, url) not in pending and any(
            isinstance(r, dict) and r.get("source") == _source_for(var) for r in rows
        ):
            pending.append((provider, var, url))
    if not pending:
        return report
    try:
        with _lock():
            done: List[bool] = []

            def backup() -> None:
                # auth.json is copied aside once, before the first write of this start.
                if not done:
                    from . import commands

                    commands._backup_auth_store()
                    done.append(True)

            for provider, var, url in pending:
                try:
                    _sync_one(provider, var, url, report, backup)
                except Exception as exc:
                    logger.warning("kame: could not sync %s from %s: %s", provider, var, type(exc).__name__)
                    logger.debug("kame: sync failure", exc_info=True)
    except Exception:
        logger.debug("kame: key-variable sync skipped", exc_info=True)
    for line in report:
        logger.info("kame: %s", line)
    return report
