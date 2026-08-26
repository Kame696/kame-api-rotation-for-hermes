"""Teach the credential pool the one thing it has no field for: the model.

Hermes benches a credential per provider. Google, among others, meters free
tier quota per key *per model* — the error body says so in its own words
(``GenerateRequestsPerMinutePerProjectPerModel``). A key spent on the main
model still has its whole allowance on a smaller auxiliary one, and the pool
has nowhere to record that, so it locks the key out of everything.

Left alone, that turns KAME's accuracy into a liability. Before KAME, a daily
cap benched a key for the default hour. With KAME correctly reading "resets
at midnight", the same key is benched for the real duration — and because
the bench is provider-wide, a model that spent nothing loses the key for the
whole day instead of an hour. Being right about *when* is worse than being
wrong about it unless the plugin is also right about *what for*.

Two wrappers close that, and no more than two:

``_mark_exhausted``
    The single point where a bench is written. Runs after the host, reads
    what the host actually stored, and files the same deadline in the ledger
    against the model that was in flight. Recording the host's own number is
    what makes the bench provably KAME's later — no marker field required in
    a structure the plugin does not own.

``_available_entries``
    The single point where "which keys can I use" is answered. Runs after the
    host and adjusts the answer for the model in flight: hand back a key the
    host benched on a different model, withhold one the ledger knows is spent
    on this one — and, when that leaves nothing at all, offer one back anyway
    to test a deadline KAME chose itself. See ``core/probe.py`` for why a
    total lockout is the one situation where trusting the prediction is the
    expensive option.

A third is wrapped when it is there and skipped when it is not:

``_select_unlocked``
    Purely so the journal can name the credential behind a *successful* call.
    The hook that reports success carries a provider and a model and no key,
    and this is where the pool decides which key that will be. It changes no
    behaviour and its absence costs only the recovery half of the statistics,
    so it is optional where the other two are not.

The first two are wrapped on the class, not on an instance, because the pool object
is not shared. The main conversation holds a long-lived ``agent._credential_pool``
while the auxiliary path calls ``load_pool()`` and gets a fresh object every
time. A class-level wrapper reaches both, including pools constructed before
the plugin loaded.

**Nothing is written to the pool and nothing is written to disk.** The
wrappers observe and filter. Every host invariant — cooldown clearing,
DEAD pruning, OAuth refresh, persistence — runs exactly as it would with the
plugin absent, because the original method runs first, in full, unmodified.

**It refuses to install unless it recognises what it is patching.** The
checks below are not defensive noise; they are the promise that a Hermes
upgrade which moves any of this degrades the plugin to its previous
behaviour instead of corrupting credential state. Hermes' compatibility
contract covers documented surfaces, and these are internals — so the
plugin verifies them itself, every start, and declines when they have moved.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import os
import threading
import time
from typing import Any, Callable, List, Optional, Tuple

from . import runtime
from .core import escalate
from .core import journal as journal_module
from .core import dispersion, multikey
from .core import probe, reconcile
from .core.reconcile import EntryView

logger = logging.getLogger(__name__)

# Stamped on our wrappers so a second install is a no-op rather than a stack
# of wrappers each calling the last.
_MARK = "__kame_wrapped__"

# Escape hatch for the one feature that changes which healthy key goes out.
_SPREAD_DISABLED_ENV = "KAME_SPREAD_DISABLED"

# Ceiling on the mark-id-to-label map the report reads.
_MAX_NAMES = 512

# Attributes the wrappers read off a pooled credential. A release decision
# rests on all of them; if any is gone, the shape of the entry is not the one
# these rules were written against.
_REQUIRED_ENTRY_FIELDS = (
    "id",
    "last_status",
    "last_error_reset_at",
    "auth_type",
    "runtime_api_key",
)

_REQUIRED_MODULE_NAMES = (
    "CredentialPool",
    "PooledCredential",
    "STATUS_EXHAUSTED",
    "STATUS_DEAD",
    "AUTH_TYPE_API_KEY",
)


class Incompatible(Exception):
    """The installed Hermes does not present the surfaces these wrappers need."""


def _check_signature(function: Any, required: Tuple[str, ...]) -> None:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError) as exc:  # builtins, C functions
        raise Incompatible(f"cannot inspect {getattr(function, '__name__', function)}") from exc
    missing = [name for name in required if name not in parameters]
    if missing:
        raise Incompatible(
            f"{getattr(function, '__name__', function)} is missing {', '.join(missing)}"
        )


def inspect_module(module: Any) -> None:
    """Raise ``Incompatible`` unless every surface the wrappers use is present.

    Deliberately strict about *shape* and silent about *behaviour*: it checks
    that the names exist and the parameters are spelled the way the wrappers
    call them. A version of Hermes that keeps the shape and changes the
    meaning is not detectable from here, which is why the wrappers themselves
    only ever add to or subtract from an answer the host computed.
    """
    for name in _REQUIRED_MODULE_NAMES:
        if not hasattr(module, name):
            raise Incompatible(f"credential_pool has no {name}")

    pool_class = module.CredentialPool
    for method in ("_mark_exhausted", "_available_entries", "entries"):
        if not callable(getattr(pool_class, method, None)):
            raise Incompatible(f"CredentialPool has no {method}()")

    _check_signature(
        pool_class._mark_exhausted,
        ("entry", "status_code", "error_context", "persist", "failure_reason"),
    )
    _check_signature(pool_class._available_entries, ("clear_expired", "refresh"))

    credential = module.PooledCredential
    entry_fields = getattr(credential, "__dataclass_fields__", None)
    if not entry_fields:
        raise Incompatible("PooledCredential is not a dataclass")
    # Some of these are stored columns and some are computed — ``runtime_api_key``
    # resolves an OAuth access token behind a property. Both count as present;
    # what matters is that reading the name off an entry yields the value the
    # release rules were written against.
    missing = [
        name
        for name in _REQUIRED_ENTRY_FIELDS
        if name not in entry_fields and not hasattr(credential, name)
    ]
    if missing:
        raise Incompatible(f"PooledCredential is missing {', '.join(missing)}")


class PoolBinding:
    """Installs, owns, and can fully remove the wrappers."""

    def __init__(
        self,
        store: Any,
        *,
        journal: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        # Optional on purpose: the ledger is what makes the plugin correct,
        # the journal is what will make a later version smarter. A plugin
        # constructed without one behaves exactly as v0.0.5 did.
        self._journal = journal
        self._clock = clock
        self._module: Any = None
        self._originals: dict = {}
        self.installed = False
        self.reason = "not installed"
        self.watching_selection = False
        # Splitting a multi-key credential is only safe while the parts can be
        # kept off disk, so it is switched on by the persist guard and by
        # nothing else. See ``_guard_persist``.
        self.splitting_multikey = False
        # Which of several healthy keys to hand out next. Memory-only and
        # never persisted: a sixty-second window means nothing across a
        # restart. See ``core/dispersion.py``.
        self._dispersion = dispersion.Dispersion()
        self.spreading_load = False
        # Set only while the host is choosing a credential. Everything else
        # that asks "which keys are usable" — a status report, a count, the
        # host's own emptiness check — gets the host's own order back
        # untouched, because none of those is a decision about which key to
        # spend and re-ordering them would change answers for no gain.
        self._selecting = threading.local()
        # Mark id -> the name a person would recognise. The load counter is
        # keyed by a hash of the key, which is right for counting and useless
        # in a report, so the label is remembered separately as keys are
        # handed out. Labels only: this map is one more place a key must not
        # be, and it is bounded because a long run walks many credentials.
        self._names: Dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------

    def install(self, module: Any) -> bool:
        """Wrap the two methods. Returns whether the plugin gained per-model memory.

        Never raises: a refusal is a supported outcome, reported through
        ``reason`` and a single log line, and leaves the host untouched.
        """
        if self.installed:
            return True
        try:
            inspect_module(module)
        except Incompatible as exc:
            self.reason = str(exc)
            logger.warning(
                "kame: per-model memory disabled — %s. Cooldown sizing still active.",
                self.reason,
            )
            return False

        pool_class = module.CredentialPool
        if getattr(pool_class._mark_exhausted, _MARK, False) or getattr(
            pool_class._available_entries, _MARK, False
        ):
            # Another instance of this plugin — or a reload that did not tear
            # down — already owns these. Stacking would double-count.
            self.reason = "already wrapped by another KAME instance"
            logger.debug("kame: %s", self.reason)
            return False

        self._module = module
        self._originals = {
            "_mark_exhausted": pool_class._mark_exhausted,
            "_available_entries": pool_class._available_entries,
        }
        pool_class._mark_exhausted = self._build_mark_exhausted(
            self._originals["_mark_exhausted"]
        )
        pool_class._available_entries = self._build_available_entries(
            self._originals["_available_entries"]
        )
        self._watch_selection(pool_class)
        self._guard_persist(pool_class)
        self._expand_on_construction(pool_class)
        self.installed = True
        self.reason = "active"
        logger.info("kame: per-model quota memory active")
        return True

    def _watch_selection(self, pool_class: Any) -> None:
        """Wrap selection if it is recognisable, and shrug if it is not.

        Deliberately outside ``inspect_module``: refusing the whole binding
        because an observation point moved would trade a correctness feature
        for a statistics feature, which is the wrong way round.
        """
        original = getattr(pool_class, "_select_unlocked", None)
        if not callable(original) or getattr(original, _MARK, False):
            return
        try:
            _check_signature(original, ("refresh",))
        except Incompatible as exc:
            logger.debug("kame: not watching selection — %s", exc)
            return
        self._originals["_select_unlocked"] = original
        pool_class._select_unlocked = self._build_select_unlocked(original)
        self.watching_selection = True
        # Spreading load needs both halves: the count written when a key is
        # handed out, and the order read when the next one is asked for. With
        # only the second half the order would never change, so the feature
        # switches on here and nowhere else.
        self.spreading_load = not _spread_disabled()

    def _guard_persist(self, pool_class: Any) -> None:
        """Wrap ``_persist`` so a derived key can never be written, and only
        then allow multi-key credentials to be split at all.

        The split parts exist in memory for the length of a load. Writing them
        would create a second copy of a key list whose only correct source is
        the ``.env`` line — one that no longer updates when that line is
        edited, and that would be re-split on the next load into parts of
        parts. So the guard is not a precaution around the feature; it is the
        thing that makes the feature safe to have.

        The host would in fact strip the secret itself: an ``env:`` source is
        borrowed, and ``sanitize_borrowed_credential_payload`` removes secret
        fields at the disk boundary, leaving a fingerprint. Two reasons that
        is not enough to skip this. A *manual* source is persistable, so a
        pasted multi-key entry would be written with its keys intact. And a
        stripped row is still a row: a pool would accumulate metadata for
        credentials that only ever existed as a view of another one.

        Failing to wrap is not a failure to install. It turns splitting off
        and leaves every other wrapper working, which is exactly the
        behaviour of the version before this one.
        """
        original = getattr(pool_class, "_persist", None)
        if not callable(original):
            logger.debug("kame: no _persist to guard — multi-key splitting off")
            return
        if getattr(original, _MARK, False):
            return
        try:
            _check_signature(original, ("removed_ids",))
        except Incompatible as exc:
            logger.debug("kame: not guarding persistence — %s", exc)
            return

        def _kame_persist(pool, *, removed_ids=None):
            with pool._lock:
                try:
                    everything = list(pool._entries)
                    stored = [
                        entry
                        for entry in everything
                        if not multikey.is_child_source(getattr(entry, "source", ""))
                    ]
                except Exception:
                    # Unable to tell derived rows from stored ones. Write what
                    # the host would have written; the pool's own contents are
                    # not this wrapper's to gamble with.
                    logger.debug(
                        "kame: persist guard could not read entries", exc_info=True
                    )
                    return original(pool, removed_ids=removed_ids)
                if len(stored) == len(everything):
                    return original(pool, removed_ids=removed_ids)
                # Hidden for the duration of the write and put back exactly as
                # it was. ``write_credential_pool`` merges back any row that is
                # on disk and missing from what it is given, so leaving these
                # out cannot delete anything: a row that was never written has
                # nothing to merge, and every stored row is still in the list.
                pool._entries = stored
                try:
                    return original(pool, removed_ids=removed_ids)
                finally:
                    pool._entries = everything

        setattr(_kame_persist, _MARK, True)
        self._originals["_persist"] = original
        pool_class._persist = _kame_persist
        self.splitting_multikey = True

    def _expand_on_construction(self, pool_class: Any) -> None:
        """Split at construction, so ``entries()`` is right before any request.

        Selection alone would be enough for rotation to work. It would not be
        enough for anything that *reads* the pool — a status report, the
        host's own credential listing, the emptiness check a caller makes
        before deciding it has no keys at all. Those call ``entries()``, and a
        pool that only tells the truth once someone asks it to pick would
        answer all of them with the one malformed row.
        """
        if not self.splitting_multikey:
            return
        original = getattr(pool_class, "__init__", None)
        if not callable(original) or getattr(original, _MARK, False):
            return
        try:
            _check_signature(original, ("provider", "entries"))
        except Incompatible as exc:
            logger.debug("kame: not splitting at construction — %s", exc)
            return
        binding = self

        def _kame_init(pool, *args, **kwargs):
            # Passed through rather than named: the signature check above
            # already established that the two parameters this depends on are
            # spelled the way they always were, and a constructor that gains a
            # third is not a reason to stop building pools.
            original(pool, *args, **kwargs)
            binding._expand(pool)

        setattr(_kame_init, _MARK, True)
        self._originals["__init__"] = original
        pool_class.__init__ = _kame_init

    # -- multi-key credentials -------------------------------------------

    def _expand(self, pool) -> None:
        """Make a credential that holds several keys present as several keys.

        Idempotent, and re-derived rather than remembered: a part whose key is
        no longer in the value disappears, a newly added key appears, and a
        part that already exists is kept as the same object so an in-memory
        cooldown survives the recomputation.
        """
        if not self.splitting_multikey or self._module is None:
            return
        try:
            api_key_type = self._module.AUTH_TYPE_API_KEY
        except Exception:
            return
        try:
            with pool._lock:
                entries = list(pool._entries)
                by_id = {entry.id: entry for entry in entries}
                rebuilt: List[Any] = []
                changed = False
                for parent in entries:
                    if multikey.is_child_source(getattr(parent, "source", "")):
                        # Re-emitted below by whichever parent still wants it.
                        # A part with no parent left is a part of nothing.
                        continue
                    rebuilt.append(parent)
                    keys = self._splittable_keys(parent, api_key_type)
                    if not keys:
                        continue
                    plans = multikey.plan_children(
                        parent_id=parent.id,
                        parent_source=parent.source,
                        parent_label=parent.label,
                        keys=keys,
                    )
                    for plan in plans:
                        existing = by_id.get(plan["id"])
                        if existing is not None and multikey.is_child_source(
                            getattr(existing, "source", "")
                        ):
                            rebuilt.append(existing)
                            continue
                        rebuilt.append(self._build_child(parent, plan))
                        changed = True
                if changed or len(rebuilt) != len(entries):
                    pool._entries = rebuilt
        except Exception:
            # A pool that did not gain its parts is the pool the previous
            # version had. A pool left half-rebuilt is not something any
            # version had, so nothing is assigned until the list is complete.
            logger.debug("kame: multi-key expansion skipped", exc_info=True)

    def _splittable_keys(self, entry: Any, api_key_type: Any) -> List[str]:
        """The keys inside one entry, or nothing if it is not that kind of entry.

        Reads ``runtime_api_key`` rather than ``access_token``, which also
        settles ``nous`` without naming it: that provider's runtime credential
        is an invoke JWT read from ``agent_key``, and the property returns
        either that JWT — which has no separator in it — or the empty string
        when it has expired. Neither splits. An earlier version excluded the
        provider explicitly and no test could be made to fail by removing the
        exclusion, because there is no value it can hold that would split.
        """
        if str(getattr(entry, "auth_type", "")) != str(api_key_type):
            return []
        try:
            names = {f.name for f in dataclasses.fields(entry)}
        except TypeError:
            return []
        if "source" not in names:
            # The marker that keeps a part off disk lives in ``source``. An
            # entry with nowhere to put it is an entry that must not be split:
            # the parts would be indistinguishable from stored credentials at
            # exactly the moment that distinction protects the key.
            return []
        try:
            raw = entry.runtime_api_key
        except Exception:
            return []
        keys, rejected = multikey.split_value(raw)
        if keys and rejected:
            # A count, never the text. The fragments are the parts of a
            # credential value that did not look like a key at all, and the
            # only safe thing to say about them is how many there were.
            logger.debug(
                "kame: %s holds %d key(s) and %d unusable fragment(s)",
                getattr(entry, "label", "?"), len(keys), rejected,
            )
        return keys

    def _build_child(self, parent: Any, plan: dict) -> Any:
        """One part, as a credential in its own right.

        Everything the parent knows about *reaching* the provider is inherited
        — base URL, priority, auth type — because the parts differ in exactly
        one respect. Everything the parent knows about how requests *went* is
        dropped: a status, a cooldown or a request count belonged to the
        parent's whole malformed value and says nothing about any single key
        inside it.
        """
        names = {f.name for f in dataclasses.fields(parent)}
        overrides: dict = {"id": plan["id"], "source": plan["source"], "label": plan["label"]}
        # Whichever field this entry shape actually keeps the key in. The
        # host's own class stores ``access_token`` and computes
        # ``runtime_api_key`` from it, but the part must end up carrying one
        # key by whatever route this entry carries one at all — setting only a
        # field that happens not to exist here would hand back a part whose
        # key is still the whole list.
        for name in ("access_token", "runtime_api_key"):
            if name in names:
                overrides[name] = plan["access_token"]
        # History belonged to the parent's whole malformed value and says
        # nothing about any single key inside it.
        for name in (
            "request_count",
            "last_status",
            "last_status_at",
            "last_error_code",
            "last_error_reason",
            "last_error_message",
            "last_error_reset_at",
        ):
            if name in names:
                overrides[name] = 0 if name == "request_count" else None
        if "extra" in names:
            extra = dict(getattr(parent, "extra", None) or {})
            # The parent's fingerprint is of the whole list. Carrying it onto
            # a part would label the part with a digest of something it is not.
            extra.pop("secret_fingerprint", None)
            overrides["extra"] = extra
        return dataclasses.replace(parent, **overrides)

    def _supersedes(self, entry: Any) -> bool:
        """Whether this entry has been replaced by its own parts.

        The parent of a split is not a credential any provider will accept —
        its key is a comma-joined list — so it must not be selected. It stays
        in the pool because it is the row that exists on disk and the one the
        env var maps to; only its turn in the rotation is taken away.
        """
        if not self.splitting_multikey or self._module is None:
            return False
        try:
            return bool(self._splittable_keys(entry, self._module.AUTH_TYPE_API_KEY))
        except Exception:
            return False

    def uninstall(self) -> None:
        """Put the original methods back. Safe to call when not installed."""
        if not self.installed or self._module is None:
            return
        pool_class = self._module.CredentialPool
        for name, original in self._originals.items():
            if getattr(getattr(pool_class, name, None), _MARK, False):
                setattr(pool_class, name, original)
        self._originals = {}
        self._module = None
        self.installed = False
        self.watching_selection = False
        self.splitting_multikey = False
        self.spreading_load = False
        self.reason = "uninstalled"

    # -- wrapper: recording ----------------------------------------------

    def _build_mark_exhausted(self, original: Callable) -> Callable:
        binding = self

        def _kame_mark_exhausted(
            pool,
            entry,
            status_code=None,
            error_context=None,
            *,
            persist: bool = True,
            failure_reason: Optional[str] = None,
        ):
            updated = original(
                pool,
                entry,
                status_code,
                error_context,
                persist=persist,
                failure_reason=failure_reason,
            )
            try:
                binding._remember(pool, updated, status_code=status_code)
            except Exception:
                # The bench itself is already written and correct. Failing to
                # note which model earned it costs the optimisation, never the
                # host's own recovery.
                logger.debug("kame: could not record per-model bench", exc_info=True)
            return updated

        setattr(_kame_mark_exhausted, _MARK, True)
        return _kame_mark_exhausted

    def _remember(self, pool: Any, updated: Any, *, status_code: Any = None) -> None:
        module = self._module
        if module is None or updated is None:
            return
        if getattr(updated, "last_status", None) != module.STATUS_EXHAUSTED:
            # DEAD is a permanent credential fault, not a quota fact — it is
            # true for every model and must never be released for one.
            return
        provider = getattr(pool, "provider", "")
        model = runtime.model_for(provider)
        if not model:
            return

        now = self._clock()
        reset_at = getattr(updated, "last_error_reset_at", None)
        reason = str(getattr(updated, "last_error_reason", "") or "rate_limit")

        # Claimed once, here, and handed to both halves. The verdict is
        # single-use by design — it must not be raced for between the ledger
        # and the journal, and the ledger needs it now that a bench carries
        # the scope the classifier read off the provider's own words.
        judgement = runtime.take_judgement(provider, model, now=now)

        # A refusal from the key under test *is* the answer to the probe, and a
        # negative one. Claiming it here stops a later success — from a
        # different key, after the pool rotated on — from being read as this
        # key having recovered.
        pending = runtime.take_probe(provider, now=now)
        if pending is not None and pending.credential_id != getattr(updated, "id", ""):
            # Some other key failed while the probe is still out. Put it back:
            # the question it asked has not been answered yet.
            runtime.note_probe_issued(
                provider, pending.credential_id, pending.model, now=pending.at
            )

        window = getattr(judgement, "window", "unknown") if judgement is not None else "unknown"

        # Read before anything is written: the streak is the run of refusals
        # ending with *this* one, and this one is not in the journal yet.
        strikes = self._short_streak(
            credential_id=getattr(updated, "id", ""),
            model=model,
            window=window,
            now=now,
        )
        held_to = reset_at
        if reset_at is not None:
            stretched = escalate.stretch(
                reset_at=reset_at,
                now=now,
                strikes=strikes,
                window=window,
                reason=reason,
                source=getattr(judgement, "source", "") if judgement is not None else "",
            )
            if stretched is not None:
                held_to = stretched
                logger.info(
                    "kame: %s's %s deadline on %s has been measured short %d× —"
                    " holding it %.0fs instead of %.0fs",
                    _label(updated),
                    window,
                    model,
                    strikes,
                    max(0.0, stretched - now),
                    max(0.0, float(reset_at) - now),
                )

        # The ledger first: it is what the next selection reads, and it must
        # not be at the mercy of a bookkeeping bug in the half that only
        # watches.
        if reset_at is not None:
            self._record_bench(
                pool, updated, provider, model, reset_at, reason, now,
                scope=getattr(judgement, "scope", "unknown") if judgement is not None else "unknown",
                extend_to=held_to if held_to != reset_at else None,
            )
        # A deadline of None means the host fell back to its default TTL. KAME
        # did not size that bench, so KAME must not claim it in the ledger —
        # the fingerprint that proves ownership later is precisely this number,
        # and a bench without one can never be matched. It is still a real
        # refusal and still worth writing down.

        try:
            self._record_block(
                updated=updated,
                provider=provider,
                model=model,
                held_to=held_to,
                host_reset_at=reset_at,
                status_code=status_code,
                reason=reason,
                now=now,
                judgement=judgement,
            )
        except Exception:
            logger.debug("kame: could not journal the block", exc_info=True)

    def _short_streak(self, *, credential_id: str, model: str, window: str, now: float) -> int:
        """How many times in a row this exact deadline has been measured short.

        Zero without a journal, which is the only honest answer when there are
        no measurements — and it is the answer that changes nothing.
        """
        if self._journal is None or not credential_id:
            return 0
        try:
            return journal_module.short_streak(
                self._journal.load(),
                credential_id=credential_id,
                model=model,
                window=window,
                at=now,
            )
        except Exception:
            logger.debug("kame: could not count short deadlines", exc_info=True)
            return 0

    def _record_bench(
        self,
        pool: Any,
        updated: Any,
        provider: str,
        model: str,
        reset_at: float,
        reason: str,
        now: float,
        scope: str = "unknown",
        extend_to: Optional[float] = None,
    ) -> None:
        ledger = self._store.load()
        recorded = ledger.record(
            credential_id=getattr(updated, "id", ""),
            provider=provider,
            model=model,
            reset_at=reset_at,
            now=now,
            reason=reason,
            scope=scope,
            extend_to=extend_to,
        )
        if recorded is None:
            return
        self._store.save(ledger, now=now)
        logger.debug(
            "kame: %s spent on %s for %.0fs%s",
            _label(updated),
            model,
            max(0.0, recorded.until - now),
            " (every model on this key)" if recorded.covers_every_model else "",
        )

    def _record_block(
        self,
        *,
        updated: Any,
        provider: str,
        model: str,
        held_to: Optional[float],
        host_reset_at: Optional[float],
        status_code: Any,
        reason: str,
        now: float,
        judgement: Any = None,
    ) -> None:
        """Write the refusal down, with whatever KAME was thinking at the time.

        Two deadlines go in and they do different jobs. ``held_to`` is how long
        the key was really kept out — KAME's own number when it is holding the
        key longer than the host was told — and it is what the row stores,
        because the next refusal is measured against it. ``host_reset_at`` is
        what the pool actually stored, and it decides only *who sized this
        bench*.

        Three outcomes, and they are three values. No verdict, or a verdict
        that named no deadline, is ``host``. A deadline that reached the entry
        is ``kame``. A deadline KAME computed that did **not** reach the entry
        is ``dropped`` — it did not govern, so it is not measured, but it is
        not the same fact as KAME having stayed out of it, and reading it as
        one hides a plugin that is classifying everything and changing
        nothing.
        """
        if self._journal is None:
            return
        window = judgement.window if judgement is not None else "unknown"
        source = judgement.source if judgement is not None else ""
        sized_by = journal_module.SIZED_BY_HOST
        if judgement is not None and judgement.reset_at is not None:
            # KAME did size this one. Whether it governed anything is a
            # separate question, and the answer is in the entry.
            sized_by = journal_module.SIZED_BY_DROPPED
            if host_reset_at is not None and (
                abs(float(host_reset_at) - float(judgement.reset_at))
                <= reconcile.FINGERPRINT_TOLERANCE_SECONDS
            ):
                sized_by = journal_module.SIZED_BY_KAME

        book = self._journal.load()
        row = book.record_block(
            at=now,
            provider=provider,
            model=model,
            credential_id=getattr(updated, "id", ""),
            status_code=status_code if isinstance(status_code, int) else None,
            window=window,
            source=source,
            reset_at=held_to,
            sized_by=sized_by,
            reason=reason,
        )
        if row is None:
            return
        self._journal.save(book, now=now)

    def note_success(self, provider: str, model: str) -> None:
        """Record that a call came back clean, and act on it if it settles a bet.

        Two different things happen here, from two different sources, and the
        difference is the point:

        *The ledger.* Only a call that answers an outstanding **probe** touches
        it. That is a release decision, and it is allowed exactly because the
        escape hatch chose the key by name and handed back a list with one
        entry in it. Nothing is inferred; the key that worked is the key KAME
        put back on the table.

        *The journal.* Statistics, from the selection mirror, which is
        best-effort and says so. An occasional observation landing against the
        wrong key of the right provider is tolerable in a sample and is not
        allowed to move a bench.
        """
        now = self._clock()
        try:
            self._settle_probe(provider, model, now)
        except Exception:
            # The bench stands and the key waits out a deadline it has already
            # disproved — bad, but the failure mode of the version before this
            # one, not a new one.
            logger.debug("kame: could not settle the probe", exc_info=True)

        if self._journal is None:
            return
        credential_id = runtime.selected_for(provider)
        if not credential_id:
            return
        book = self._journal.load()
        recovery = book.record_success(
            at=now,
            provider=provider,
            model=model,
            credential_id=credential_id,
        )
        if recovery is None:
            return
        self._journal.save(book, now=now)
        logger.debug(
            "kame: %s came back on %s after %.0fs (predicted %s)",
            credential_id[:8],
            recovery.model,
            recovery.observed_seconds,
            "early" if recovery.was_early else "on time or late",
        )

    def _settle_probe(self, provider: str, model: str, now: float) -> None:
        """Mark what this success disproved, if it answered a probe.

        Returns quietly in the overwhelmingly common case: no probe is
        outstanding, because nothing was locked out, because the pool had a
        usable key like it does almost always.
        """
        pending = runtime.take_probe(provider, now=now)
        if pending is None:
            return
        ledger = self._store.load()
        refutation = ledger.note_success(pending.credential_id, model, now)
        if not refutation:
            return
        self._store.save(ledger, now=now)
        if refutation.refuted is not None:
            logger.info(
                "kame: %s works on %s after all — releasing it %.0fs early",
                pending.credential_id[:8],
                model,
                max(0.0, refutation.refuted.reset_at - now),
            )
        for narrowed in refutation.narrowed:
            logger.info(
                "kame: %s answered on %s, so %s's limit is not key-wide",
                pending.credential_id[:8],
                model,
                narrowed.model,
            )

    # -- wrapper: watching selection --------------------------------------

    def _build_select_unlocked(self, original: Callable) -> Callable:
        binding = self

        def _kame_select_unlocked(pool, *, refresh: bool = True):
            binding._selecting.active = True
            try:
                result = original(pool, refresh=refresh)
            finally:
                binding._selecting.active = False
            try:
                entry = result[0] if isinstance(result, tuple) and result else None
                if entry is not None:
                    provider = getattr(pool, "provider", "")
                    entry_id = getattr(entry, "id", "")
                    runtime.note_selection(provider, entry_id)
                    # Counted as busy the moment it is handed out, not when
                    # its call returns. That ordering is the anti-dogpile: a
                    # second thread selecting a millisecond later sees this
                    # key as loaded and picks a different one.
                    mark = _mark_id(entry)
                    binding._dispersion.note(
                        dispersion.bucket_for(provider, runtime.model_for(provider)),
                        mark,
                        binding._clock(),
                    )
                    binding._remember_name(mark, entry)
            except Exception:
                # This runs inside the pool's lock on the path that hands out
                # every credential. It must be incapable of costing a
                # selection, so it observes and swallows.
                logger.debug("kame: could not note the selected credential", exc_info=True)
            return result

        setattr(_kame_select_unlocked, _MARK, True)
        return _kame_select_unlocked

    # -- wrapper: filtering ----------------------------------------------

    def _build_available_entries(self, original: Callable) -> Callable:
        binding = self

        def _kame_available_entries(pool, *, clear_expired: bool = False, refresh: bool = False):
            # Before the host decides what is available, make sure everything
            # that *is* a credential is present as one. Done here as well as at
            # construction because the main conversation holds a pool that may
            # have been built before this plugin loaded, and that pool would
            # otherwise never gain its parts.
            binding._expand(pool)
            available, pending = original(pool, clear_expired=clear_expired, refresh=refresh)
            try:
                available = [
                    entry for entry in available if not binding._supersedes(entry)
                ]
            except Exception:
                logger.debug("kame: multi-key filter skipped", exc_info=True)
            try:
                adjusted = binding._adjust(pool, available, refresh=refresh)
            except Exception:
                logger.debug("kame: per-model adjustment skipped", exc_info=True)
                return available, pending
            try:
                return binding._spread(pool, adjusted), pending
            except Exception:
                logger.debug("kame: load spreading skipped", exc_info=True)
                return adjusted, pending

        setattr(_kame_available_entries, _MARK, True)
        return _kame_available_entries

    # -- which of the healthy ones ---------------------------------------

    def _spread(self, pool: Any, available: List[Any]) -> List[Any]:
        """Order the usable keys least-loaded first, or leave them alone.

        The host's default is ``fill_first``: hand out ``available[0]`` every
        time, so one key absorbs every request until the provider refuses it.
        That is the right default for a pool of one paid key and the wrong one
        for the pool this plugin exists for, where a per-minute limit is per
        key and fifteen idle keys sit behind the one being hammered.

        Only the default is re-ordered. ``round_robin``, ``random`` and
        ``least_used`` are typed into a config file by a person who decided how
        their keys should be picked; ``fill_first`` is what the host returns
        when nobody decided anything. Overriding an unstated default is a
        correction — overriding a stated choice is ignoring the user.

        Never removes an entry. The order changes, the set does not, so no
        request can fail because of anything decided here.
        """
        if not self.spreading_load or len(available) < 2:
            return available
        if not getattr(self._selecting, "active", False):
            return available
        module = self._module
        if module is None:
            return available
        default_strategy = getattr(module, "STRATEGY_FILL_FIRST", None)
        if default_strategy is None:
            # A build that names its strategies differently is a build whose
            # selection this cannot reason about. Leave the order alone.
            return available
        if str(getattr(pool, "_strategy", "")) != str(default_strategy):
            return available
        provider = getattr(pool, "provider", "")
        bucket = dispersion.bucket_for(provider, runtime.model_for(provider))
        now = self._clock()
        marks = [_mark_id(entry) for entry in available]
        # Every healthy candidate gets a counter, used or not. This is the
        # only place in the plugin that sees the whole healthy set at once,
        # and without it the report can only ever list keys that were picked
        # — which hides the exact row it exists to show: the key that has
        # taken nothing while another takes everything.
        self._dispersion.introduce(bucket, marks)
        for mark, entry in zip(marks, available):
            self._remember_name(mark, entry)
        ranked = self._dispersion.order(
            bucket,
            marks,
            now,
            just_released=self._just_released(available, now),
        )
        position = {entry_id: index for index, entry_id in enumerate(ranked)}
        return sorted(
            available, key=lambda entry: position.get(_mark_id(entry), len(position))
        )

    def _remember_name(self, mark: str, entry: Any) -> None:
        """Keep a readable name for a counted credential, for the report only.

        Bounded by dropping the whole map at the ceiling rather than by
        evicting one entry: this is a convenience for rendering, and the only
        cost of losing it is a report that falls back to the short id.
        """
        if len(self._names) >= _MAX_NAMES:
            self._names.clear()
        self._names[mark] = _label(entry)

    def _just_released(self, available: List[Any], now: float) -> set:
        """The mark ids of keys whose bench lapsed moments ago.

        Read off the entry rather than off the ledger, deliberately. The pool
        keeps ``last_error_reset_at`` until something succeeds on the
        credential, so a deadline that has just passed is still legible there —
        including the ones the *host* wrote, which the ledger knows nothing
        about. And an entry that is still benched has a deadline in the future,
        so it never lands in this set: it is either withheld already or it is
        the escape hatch's probe, which is offered precisely because there was
        nothing rested to prefer.

        The Agent Zero engine spends this preference only on compression calls,
        where one failure costs a very large call. Here it is spent on every
        selection, because it costs nothing when the keys are equal and the
        evidence that a just-released key refuses again is this plugin's own:
        it is the pattern ``escalate.py`` exists to widen benches for.
        """
        recent = set()
        for entry in available:
            reset_at = getattr(entry, "last_error_reset_at", None)
            if not reset_at:
                continue
            try:
                lapsed = now - float(reset_at)
            except (TypeError, ValueError):
                continue
            if 0.0 <= lapsed <= dispersion.JUST_RELEASED_SECONDS:
                recent.add(_mark_id(entry))
        return recent

    def _adjust(self, pool: Any, available: List[Any], *, refresh: bool) -> List[Any]:
        """Re-answer "which keys are usable" for the model actually in flight."""
        model = runtime.model_for(getattr(pool, "provider", ""))
        if not model:
            # No announcement, or one from a different provider. The host's
            # answer is the only defensible one.
            return available

        ledger = self._store.load()
        if not len(ledger):
            return available

        snapshot = pool.entries()
        now = self._clock()
        actions = reconcile.plan(
            [self._view(entry) for entry in snapshot],
            ledger,
            model=model,
            now=now,
        )
        merged = (
            self._apply(pool, snapshot, available, actions, model=model, refresh=refresh)
            if actions
            else list(available)
        )
        if merged:
            return merged
        # Nothing is usable for this model. Every deadline standing between the
        # user and their agent right now is a guess KAME made, and there is no
        # other key for the pool to route to — so one of them gets tested. That
        # includes a key the *host* would have handed over and KAME withheld:
        # a hold is a prediction like any other, and no prediction of this
        # plugin's gets to lock anybody out.
        rescued = self._rescue(
            pool, snapshot, ledger,
            model=model, now=now, refresh=refresh,
            held_by_us={a.credential_id for a in actions if a.kind == reconcile.HOLD},
        )
        return [rescued] if rescued is not None else merged

    def _apply(
        self,
        pool: Any,
        snapshot: List[Any],
        available: List[Any],
        actions: List[Any],
        *,
        model: str,
        refresh: bool,
    ) -> List[Any]:
        held = {a.credential_id for a in actions if a.kind == reconcile.HOLD}
        released = {a.credential_id for a in actions if a.kind == reconcile.RELEASE}

        kept = {getattr(entry, "id", ""): entry for entry in available}
        for credential_id in held:
            kept.pop(credential_id, None)

        merged: List[Any] = []
        for entry in snapshot:
            entry_id = getattr(entry, "id", "")
            if entry_id in kept:
                merged.append(kept.pop(entry_id))
            elif entry_id in released and self._is_usable(pool, entry, refresh=refresh):
                merged.append(entry)
        # Anything the host returned that is not in the snapshot is still the
        # host's answer and is kept: subtracting a key KAME never reasoned
        # about would be a regression, not a correction.
        merged.extend(kept.values())

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "kame: %s on %s — %d usable (%s)",
                getattr(pool, "provider", "?"),
                model,
                len(merged),
                "; ".join(f"{a.kind} {a.credential_id[:8]}: {a.why}" for a in actions),
            )
        return merged

    # -- the escape hatch -------------------------------------------------

    def _rescue(
        self,
        pool: Any,
        snapshot: List[Any],
        ledger: Any,
        *,
        model: str,
        now: float,
        refresh: bool,
        held_by_us: Optional[set] = None,
    ) -> Optional[Any]:
        """Offer one benched key back, to test a deadline KAME chose itself.

        Reached only when the model in flight has nothing else at all. The
        host's own sole-credential shortening exists for this situation and is
        switched off by the very thing this plugin does well — a
        provider-supplied ``reset_at`` overrides it — so the escape hatch has
        to be put back here, scoped per model, which is the dimension the host
        cannot see.

        Three gates before anything is offered, in this order: the bench is
        one KAME wrote (the host's stored deadline still matches the ledger's
        fingerprint), the credential passes every check the host itself
        applies, and the probe policy says this is the moment. Failing any of
        them returns ``None`` and the lockout stands — which is the correct
        answer when the keys really are spent.
        """
        held_by_us = held_by_us or set()
        candidates = []
        for entry in snapshot:
            view = self._view(entry)
            if view.is_dead:
                continue
            ours = view.credential_id in held_by_us
            if not view.is_benched and not ours:
                continue
            bench = self._blocking_bench(ledger, view, model=model, now=now, ours=ours)
            if bench is None:
                continue
            if not self._is_usable(pool, entry, refresh=refresh):
                continue
            candidates.append((bench, entry))

        if not candidates:
            return None

        choice = probe.choose([bench for bench, _entry in candidates], now=now)
        if choice is None:
            return None

        entry = next(
            entry
            for bench, entry in candidates
            if bench.credential_id == choice.credential_id
            and bench.model == choice.model
        )
        if choice.fresh:
            # Written before the key is handed over. If the call never comes
            # back — a crash, a hang, a process kill — the attempt still
            # counted, because an uncounted probe is one that repeats on the
            # very next query.
            try:
                # Counted against the bench that is actually doing the
                # blocking, which for an account-wide limit is one filed under
                # a different model. Counting it under the model in flight
                # would leave the real bench forever untested.
                ledger.note_probe(choice.credential_id, choice.model, now)
                self._store.save(ledger, now=now)
            except Exception:
                # Better to skip the probe than to issue one that cannot be
                # counted: an unrecorded probe has no backoff at all.
                logger.debug("kame: could not record the probe", exc_info=True)
                return None
            logger.info(
                "kame: nothing usable for %s — testing %s (%.0fs still on the clock)",
                model,
                _label(entry),
                max(0.0, choice.bench.until - now),
            )
        # Announce which key is being tested, so a clean answer can be credited
        # to it without guessing. This is the whole reason a probe is allowed
        # to settle a release decision while the ordinary selection mirror is
        # not: the list handed back has exactly one entry in it, and it is this
        # one.
        runtime.note_probe_issued(
            getattr(pool, "provider", ""),
            choice.credential_id,
            model,
            now=now,
        )
        return entry

    def _blocking_bench(
        self, ledger: Any, view: EntryView, *, model: str, now: float, ours: bool = False
    ) -> Optional[Any]:
        """The KAME-written bench that is keeping this key from this model.

        Usually the one filed against the model in flight. It can also be an
        account-wide bench earned on a different model, and that case has to
        be reachable or v0.0.8 would hand the lockout straight back: a
        provider that says "this key is done everywhere" would bench the whole
        pool with no way to ever test the claim.

        Ownership is proved by the deadline: the number the host is holding
        has to be one this ledger recorded. A bench KAME did not write is
        never tested. ``ours`` is the case where the host is holding nothing
        at all and the only thing withholding the key is this plugin's own
        ledger — there the hold is ours by construction, there is no host
        deadline to match, and the binding constraint is simply the latest
        live bench that reaches this model.
        """
        relevant = [
            bench
            for bench in ledger.live_benches_for(view.credential_id, now)
            if bench.holds(now) and (bench.model == model or bench.covers_every_model)
        ]
        if not relevant:
            return None
        if ours:
            return max(relevant, key=lambda bench: bench.until)
        host_reset = view.reset_at
        if host_reset is None:
            # A default-TTL bench. KAME never writes one, so it is not ours.
            return None
        for bench in relevant:
            if abs(float(host_reset) - bench.reset_at) <= reconcile.FINGERPRINT_TOLERANCE_SECONDS:
                return bench
        return None

    def _view(self, entry: Any) -> EntryView:
        module = self._module
        status = getattr(entry, "last_status", None)
        if module is not None:
            if status == module.STATUS_DEAD:
                status = reconcile.STATUS_DEAD
            elif status == module.STATUS_EXHAUSTED:
                status = reconcile.STATUS_EXHAUSTED
        return EntryView(
            credential_id=getattr(entry, "id", ""),
            status=status,
            reset_at=getattr(entry, "last_error_reset_at", None),
        )

    def _is_usable(self, pool: Any, entry: Any, *, refresh: bool) -> bool:
        """Mirror the host's own gates before handing a benched key back.

        The host applies these before it appends an entry to the available
        list. Releasing a key past them would put an empty API key or a
        credential with an expired token into rotation — a failure the host
        was specifically avoiding.
        """
        module = self._module
        if module is None:
            return False
        if getattr(entry, "auth_type", None) == module.AUTH_TYPE_API_KEY and not getattr(
            entry, "runtime_api_key", None
        ):
            return False
        if refresh:
            needs_refresh = getattr(pool, "_entry_needs_refresh", None)
            if needs_refresh is None:
                # The host would have decided this and cannot. Withholding one
                # key costs a rotation; handing over an unrefreshed token
                # costs a failed call and a wrongly benched credential.
                return False
            try:
                if needs_refresh(entry):
                    return False
            except Exception:
                return False
        return True


def _spread_disabled() -> bool:
    """Whether the load spreading half of the plugin is switched off.

    Separate from ``KAME_ROTATION_DISABLED`` because it answers a different
    question. This is the one piece that changes *which* healthy key goes out
    rather than whether a benched one may: it is the only part able to make a
    working install pick a different key than stock Hermes would, so it gets
    its own switch — for somebody who wants stock ``fill_first`` back without
    losing cooldown sizing, and for the harness that has to prove which
    differences are this feature and which are a regression.

    Settable in the environment or, since v0.3.2, as ``spread_disabled``
    under this plugin's own config entry — see ``settings``.
    """
    from . import settings

    return settings.is_on(settings.SPREAD_DISABLED)


def _mark_id(entry: Any) -> str:
    """What the load counter calls this credential.

    Reads the key the host would actually send, not the stored column, so a
    split part is counted as itself rather than as the comma-joined row it
    was derived from. Reading it must never cost a selection: an entry that
    raises on the attribute is counted by its id alone, which is what an
    OAuth entry gets anyway.
    """
    try:
        secret = getattr(entry, "runtime_api_key", None)
    except Exception:
        secret = None
    return dispersion.mark_id(getattr(entry, "id", ""), secret)


def _label(entry: Any) -> str:
    """A stable, non-secret name for one credential, for logs only."""
    label = getattr(entry, "label", None)
    if label:
        return str(label)
    entry_id = str(getattr(entry, "id", "") or "?")
    return entry_id[:8]
