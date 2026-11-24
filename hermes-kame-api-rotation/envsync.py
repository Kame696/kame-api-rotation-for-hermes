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
        report.append(f"{provider}: {var} holds one key again; {len(ours)} API Rotation row(s) removed")
        return

    from . import commands
    from .core.carousel import fingerprint
    from .core.keys import shape_problem

    # 1.9.0.5 (D2): a key the provider's shape rule refuses is still the user's variable, so it is kept in the
    # pool; it is named (fingerprint only) in the log at every start and in /kame-keys.
    misshapen = [fingerprint(k) for k in keys if shape_problem(provider, k)]
    if misshapen:
        logger.warning("kame: %s: %s holds %d key(s) that do not look like a %s key: %s", provider, var,
                       len(misshapen), provider, ", ".join(misshapen))

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


@contextlib.contextmanager
def _profile_scope() -> Any:
    """Run inside this profile's secret scope when none is bound.

    Hermes reads keys through ``agent.secret_scope.get_secret``, which raises
    ``UnscopedSecretError`` on a multiplexed process with no scope installed —
    the case at plugin start and on a helper thread. Binding the profile's own
    scope (``build_profile_secret_scope`` of the active home) is the documented
    fix; on a Hermes without the module this does nothing.
    """
    token = None
    try:
        from agent import secret_scope

        if secret_scope.current_secret_scope() is None:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
            token = secret_scope.set_secret_scope(
                secret_scope.build_profile_secret_scope(home), profile_home=str(home))
    except Exception:
        logger.debug("kame: no secret scope bound for the key sync", exc_info=True)
    try:
        yield
    finally:
        if token is not None:
            from agent import secret_scope

            secret_scope.reset_secret_scope(token)


def sync() -> List[str]:
    """Bring the pool in line with every multi-key variable. Returns what changed."""
    if settings.is_on(settings.RESOLVER_DISABLED) or settings.is_on(settings.ROTATION_DISABLED):
        return []
    with _profile_scope():
        return _sync_all()


def _sync_all() -> List[str]:
    report: List[str] = []
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


def remove_all(provider: Optional[str] = None) -> List[Dict[str, Any]]:
    """1.9.0.5 (D1): take back everything :func:`sync` put in place, so the plugin can be uninstalled.

    For every key variable Hermes declares (or only ``provider``'s): drop the pool rows this module made
    (source ``manual:kame-env:<VAR>``) and lift the suppression of ``env:<VAR>``. When the variable in ``.env``
    still holds several keys, the reply names the step that leaves Hermes one key (edit the line, or
    ``hermes config set <VAR> <one key>``): 1.9.0.6 (Hermes catalog review of #159044) never edits a provider's
    key line or copies ``.env``, which 1.9.0.5 did. Rows the user added by hand are never touched.

    Returns one dict per variable that had something to take back: ``provider``, ``var``, ``removed`` (count),
    ``fingerprints`` (of the removed rows), ``unsuppressed``, ``env`` (``envfile.keys_in_line`` outcome),
    ``held``, ``auth_backup``. Key material is never in it. Runs even
    with the resolver switched off (it is the user's explicit request); never raises.
    """
    try:
        with _profile_scope():
            return _remove_all(provider)
    except Exception:
        logger.warning("kame: remove-all failed", exc_info=True)
        return []


def _remove_all(provider: Optional[str]) -> List[Dict[str, Any]]:
    from agent.credential_pool import load_pool
    from hermes_cli.auth import is_source_suppressed, unsuppress_credential_source

    from . import envfile
    from .core.carousel import fingerprint

    wanted = str(provider or "").strip().lower()
    results: List[Dict[str, Any]] = []
    saved: List[Optional[str]] = []
    with _lock():
        for name, var, _url in _variables():
            if wanted and name.lower() != wanted:
                continue
            source = _source_for(var)
            env_source = f"env:{var}"
            try:
                pool = load_pool(name)
                ours = [e for e in pool.entries() if str(getattr(e, "source", "")) == source]
                suppressed = bool(is_source_suppressed(name, env_source))
                if not ours and not suppressed:
                    continue
                if not saved:
                    from . import commands

                    saved.append(commands._backup_auth_store())
                row: Dict[str, Any] = {
                    "provider": name, "var": var, "removed": 0, "fingerprints": [], "unsuppressed": False,
                    "env": "absent", "held": 0,
                    "auth_backup": saved[0],
                }
                for entry in ours:
                    row["fingerprints"].append(fingerprint(_token(entry)))
                    row["removed"] += int(_remove(pool, entry))
                if suppressed:
                    unsuppress_credential_source(name, env_source)
                    row["unsuppressed"] = True
                several = len(split_value(_env_value(var))[0])
                outcome, held = envfile.keys_in_line(var)
                row.update(env=outcome, held=held or several)
                results.append(row)
            except Exception as exc:
                logger.warning("kame: could not take back %s: %s", var, type(exc).__name__)
                logger.debug("kame: remove-all failure", exc_info=True)
    for row in results:
        logger.info("kame: remove-all %s/%s: %d row(s) removed, .env %s", row["provider"], row["var"],
                    row["removed"], row["env"])
    return results


def describe_removal(results: List[Dict[str, Any]]) -> str:
    """The reply for :func:`remove_all`: counts and fingerprints only."""
    if not results:
        return ("Nothing to remove: API Rotation has no key rows of its own in your pool "
                "(keys you added with /kame-keys add or in Hermes stay where they are).")
    lines: List[str] = []
    for row in results:
        prints = ", ".join(row["fingerprints"])
        lines.append(f"{row['provider']}: {row['var']}: {row['removed']} API Rotation row(s) removed"
                     + (f" ({prints})" if prints else "") + (", source un-suppressed" if row["unsuppressed"] else ""))
        if row["env"] == "several":
            lines.append(f"  .env: {row['var']} still holds {row['held']} keys, and without API Rotation Hermes reads "
                         f"that list as ONE key. Keep one: edit that line in Hermes' .env, or run "
                         f"`hermes config set {row['var']} <one of your keys>`. API Rotation does not edit it.")
        elif row["env"] == "absent" and row["held"] > 1:
            lines.append(f"  {row['var']} holds {row['held']} keys outside Hermes' .env (your system environment); "
                         "API Rotation does not edit that. Keep one key there, or Hermes reads the list as one key.")
        elif row["env"] == "error" and row["held"] > 1:
            lines.append(f"  {row['var']} holds {row['held']} keys; keep one of them in Hermes' .env, or Hermes reads "
                         "the list as one key.")
    lines.append("Keys you added by hand or in Hermes were not touched. API Rotation can now be uninstalled.")
    return "\n".join(lines)
