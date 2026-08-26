"""Per-model quota memory — the fact the host's pool has no field for.

Hermes benches a credential per *provider*. Several providers meter per key
*per model*: a key spent on the main model still has its whole allowance on a
smaller auxiliary one. ``mark_exhausted_and_rotate()`` takes no model
argument, and ``PooledCredential`` has no model column, so that distinction
has nowhere to live upstream. It lives here.

The ledger is the authority on *why* a key is benched. The pool stays the
authority on *whether* it is — this module never touches it. The binding
layer reads both and reconciles.

Framework-agnostic on purpose: plain data in, plain data out, and no clock of
its own beyond the ``now`` each caller supplies. That keeps every rule here
testable without a Hermes install, and lets the same rules back an Agent Zero
build later.

**Only ever unwinds its own writes.** Every bench carries the ``reset_at``
KAME claimed for it. If the host's stored cooldown no longer matches that
fingerprint, somebody else wrote it — another process, a host-side
classification, a manual edit — and the entry is not ours to touch. This is
the invariant that keeps a per-model optimisation from becoming a way to
resurrect a key that a different subsystem deliberately benched.

**A bench is a prediction, and predictions lose to observations.** Every
deadline in here was reasoned out of an error message; none of them was
measured. When a key that this ledger says is spent goes out anyway — the
escape hatch in ``probe.py`` sends one when a model has nothing else — and
the call comes back clean, the deadline is simply wrong, and the row says so
from then on. It is marked, not deleted: the number in it is the fingerprint,
and a released key needs that proof on every selection for as long as the
host holds a cooldown to it. Pulling in the same direction, a bench never
shortens while it stands; a smaller later truth does not undo a larger
earlier one. Only a success does that.

**Two deadlines, and the difference is the whole of v0.1.0.** ``reset_at`` is
the number the *host* stored, and it is never adjusted, because it is the
fingerprint that proves this bench is KAME's to unwind. ``extended_to`` is
how long KAME itself is holding the key, which is longer when the host's
deadline has already been measured too short (``escalate.py``) or when a
shorter refusal landed on top of a longer one. Every question about
*withholding* reads ``until``; every question about *ownership* reads
``reset_at``. Conflating them would buy a longer bench at the price of the
per-model release this plugin exists for — the host's cooldown would stop
matching anything in here, and the key would be locked out of every other
model for exactly as long as KAME held it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Bumped when the persisted shape changes in a way older code cannot read.
# Hermes' compatibility contract asks that persisted plugin state stay
# readable or ship a migration; an unknown version is treated as unreadable
# and discarded rather than guessed at, because a misread ledger un-benches
# the wrong key.
SCHEMA_VERSION = 1

# A ceiling, not a target. ``ctx.state`` allows 10 MiB per plugin and a bench
# is well under 200 bytes, so this is nowhere near the limit — it exists so a
# pathological loop (a provider echoing a fresh model name per call) cannot
# grow the file without bound. Expired benches are evicted first, so the cap
# only ever bites on genuinely live data.
MAX_BENCHES = 512

# Mirrored from ``core.quota.QuotaScope`` rather than imported, so this module
# keeps its promise of depending on nothing. The two must agree; the values are
# plain strings precisely so a mismatch shows up as "unknown" — the inert
# reading — instead of an exception in the middle of a bench.
SCOPE_PER_MODEL = "per_model"
SCOPE_ACCOUNT = "account"
SCOPE_UNKNOWN = "unknown"

# Model identifiers arrive in several dresses for the same quota bucket:
# ``models/gemini-3.6-flash`` from Google's native API, ``gemini/gemini-3.6-flash``
# from litellm's provider-prefixed routing, and the bare name from config.
# Left unnormalised, one key spent on the main model would look unspent under
# whichever spelling the next call happened to use — the exact bug this module
# exists to prevent, reintroduced through the back door.
_MODEL_PREFIX = re.compile(r"^(?:models/|[a-z0-9_.-]+/)+", re.IGNORECASE)

# Deliberately *not* stripped: version and variant suffixes. ``-flash`` and
# ``-flash-lite`` are separate quotas at Google, as are ``-exp`` builds. Any
# rule that collapsed them would bench a healthy allowance. When two names do
# share a bucket, that is a per-provider fact to be learned from observation,
# not guessed at from string shape.


def normalize_model(model: Any) -> str:
    """Reduce a model identifier to the name its quota is metered under.

    Strips routing prefixes and case. Returns ``""`` for anything unusable,
    which callers treat as "no model known" rather than as a model named
    empty-string.
    """
    text = str(model or "").strip()
    if not text:
        return ""
    return _MODEL_PREFIX.sub("", text).strip().lower()


def _coerce_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN fails every comparison, so a NaN reset_at would make a bench neither
    # live nor expired — it would sit in the ledger forever and match nothing.
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


@dataclass(frozen=True)
class Bench:
    """One credential, spent on one model, until one moment.

    ``reset_at`` is absolute epoch seconds, matching what the pool stores in
    ``last_error_reset_at`` — the two are compared directly, so they must not
    drift into different units.
    """

    credential_id: str
    provider: str
    model: str
    reset_at: float
    recorded_at: float
    reason: str = "rate_limit"
    # How many times this claim has been tested, and when last. Kept on the
    # bench rather than in a table of its own because the two die together:
    # when the deadline lapses the attempt history is meaningless, and one
    # structure that expires as a unit cannot leak the way two would.
    probes: int = 0
    last_probe_at: float = 0.0
    # How far the refusal reaches. ``account`` means every model on this key
    # is spent until ``reset_at``, so releasing it for a different model
    # walks into the same wall. Anything else is treated as this model's
    # alone, which is what every version before v0.0.8 assumed universally.
    scope: str = SCOPE_UNKNOWN
    # When a call on this pair came back clean while the bench was still
    # standing — in other words, the moment this deadline was proved wrong.
    # The row is kept rather than deleted because the deadline in it is the
    # fingerprint that proves the host's cooldown is KAME's to unwind; delete
    # it and the key stays locked out by a bench nobody can claim.
    refuted_at: float = 0.0
    # How long KAME is holding this key on its own account, past the deadline
    # the host stored. Zero — the ordinary case — means the two agree. It is a
    # separate field rather than a bigger ``reset_at`` because ``reset_at`` has
    # a second job: it is the fingerprint, and it has to keep matching the
    # number the host is holding or this bench stops being provably ours.
    extended_to: float = 0.0

    @property
    def key(self) -> Tuple[str, str]:
        return (self.credential_id, self.model)

    @property
    def until(self) -> float:
        """When KAME stops withholding this key. The deadline that decides."""
        return max(self.reset_at, self.extended_to)

    @property
    def is_extended(self) -> bool:
        """KAME is holding this key past what the host was told."""
        return self.extended_to > self.reset_at

    @property
    def covers_every_model(self) -> bool:
        return self.scope == SCOPE_ACCOUNT and not self.is_refuted

    @property
    def is_refuted(self) -> bool:
        """This claim was tested and the key worked anyway."""
        return self.refuted_at > 0.0

    def is_live(self, now: float) -> bool:
        return self.until > now

    def holds(self, now: float) -> bool:
        """Whether this bench should still keep the key out of rotation.

        A refuted bench is live — its deadline has not passed and the host is
        very likely still holding the key to it — but it no longer withholds
        anything, because observation beats prediction and the observation was
        a successful call.
        """
        return self.is_live(now) and not self.is_refuted

    def to_dict(self) -> Dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "provider": self.provider,
            "model": self.model,
            "reset_at": self.reset_at,
            "recorded_at": self.recorded_at,
            "reason": self.reason,
            "probes": self.probes,
            "last_probe_at": self.last_probe_at,
            "scope": self.scope,
            "refuted_at": self.refuted_at,
            "extended_to": self.extended_to,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["Bench"]:
        """Rebuild one bench, or return None if the row is unusable.

        Persisted state outlives the code that wrote it and can be edited by
        hand. A row missing its identity or its deadline cannot be acted on
        safely, so it is dropped rather than defaulted into something that
        looks actionable.
        """
        if not isinstance(payload, dict):
            return None
        credential_id = str(payload.get("credential_id") or "").strip()
        model = normalize_model(payload.get("model"))
        reset_at = _coerce_float(payload.get("reset_at"))
        if not credential_id or not model or reset_at is None:
            return None
        recorded_at = _coerce_float(payload.get("recorded_at"))
        # Probe bookkeeping is absent from rows written before v0.0.7. Missing
        # means "never tested", which is the correct reading of a row that
        # predates testing — no migration, and no version bump that would have
        # thrown away a live ledger on upgrade.
        probes = _coerce_float(payload.get("probes"))
        last_probe_at = _coerce_float(payload.get("last_probe_at"))
        # Absent in rows written before v0.0.8, and absent is the safe
        # reading: "unknown" restores the behaviour those rows were written
        # under, so an upgrade cannot silently start holding keys back on
        # evidence that was never recorded.
        scope = str(payload.get("scope") or SCOPE_UNKNOWN).strip().lower()
        # Absent in rows written before v0.0.9. Absent means "never tested and
        # never disproved", which is exactly what those rows meant.
        refuted_at = _coerce_float(payload.get("refuted_at"))
        # Absent in rows written before v0.1.0, where the host's deadline and
        # KAME's were the same number by construction. Zero restores exactly
        # that reading, so an upgrade cannot invent a longer hold.
        extended_to = _coerce_float(payload.get("extended_to"))
        return cls(
            credential_id=credential_id,
            provider=str(payload.get("provider") or "").strip().lower(),
            model=model,
            reset_at=reset_at,
            recorded_at=recorded_at if recorded_at is not None else 0.0,
            reason=str(payload.get("reason") or "rate_limit"),
            probes=max(0, int(probes)) if probes is not None else 0,
            last_probe_at=last_probe_at if last_probe_at is not None else 0.0,
            scope=scope if scope in (SCOPE_PER_MODEL, SCOPE_ACCOUNT) else SCOPE_UNKNOWN,
            refuted_at=max(0.0, refuted_at) if refuted_at is not None else 0.0,
            extended_to=max(0.0, extended_to) if extended_to is not None else 0.0,
        )


@dataclass(frozen=True)
class Refutation:
    """What one successful call disproved. Empty is the ordinary answer.

    Successes are the normal state of the world and almost never contradict
    anything; an empty refutation is the signal to the binding layer that
    nothing changed and the ledger does not need writing back.
    """

    refuted: Optional[Bench] = None
    narrowed: List[Bench] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.refuted is not None or bool(self.narrowed)


class Ledger:
    """A bounded set of live benches, keyed by credential and model.

    One bench per ``(credential_id, model)`` pair: a second failure on the
    same pair replaces the first rather than accumulating, because the newer
    deadline is the provider's current answer and the older one is stale by
    definition.
    """

    __slots__ = ("_benches",)

    def __init__(self, benches: Optional[Iterable[Bench]] = None) -> None:
        self._benches: Dict[Tuple[str, str], Bench] = {}
        for bench in benches or ():
            self._benches[bench.key] = bench

    # -- reading ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._benches)

    def benches(self) -> List[Bench]:
        return list(self._benches.values())

    def find(self, credential_id: str, model: Any) -> Optional[Bench]:
        return self._benches.get((str(credential_id or ""), normalize_model(model)))

    def benched_until(self, credential_id: str, model: Any, now: float) -> Optional[float]:
        """When this credential is next usable *for this model*, if benched.

        A refuted bench answers ``None``: the pair was tested while the bench
        stood and the call came back clean, so the deadline is known wrong and
        holding the key to it would be holding it against evidence.
        """
        bench = self.find(credential_id, model)
        if bench is None or not bench.holds(now):
            return None
        return bench.until

    def is_spent_for(self, credential_id: str, model: Any, now: float) -> bool:
        return self.benched_until(credential_id, model, now) is not None

    def live_benches_for(self, credential_id: str, now: float) -> List[Bench]:
        """Every model this credential is currently spent on."""
        return [
            bench
            for bench in self._benches.values()
            if bench.credential_id == credential_id and bench.is_live(now)
        ]

    def shared_bench_for(self, credential_id: str, now: float) -> Optional[Bench]:
        """The live bench that covers every model on this credential, if any.

        When two of them are live, the later deadline is the answer: both are
        true, and the key is unusable until the last one lapses.
        """
        shared = [
            bench
            for bench in self.live_benches_for(credential_id, now)
            if bench.covers_every_model
        ]
        if not shared:
            return None
        return max(shared, key=lambda bench: bench.until)

    def spent_until(self, credential_id: str, model: Any, now: float) -> Optional[float]:
        """When this credential is next usable for this model, all reasons in.

        ``benched_until`` answers "is this pair spent"; this answers the
        question the pool actually asks, which includes an account-wide bench
        earned on some *other* model. The later of the two wins — a key held
        back by two live facts comes back when the last one lapses.
        """
        candidates = [
            value
            for value in (
                self.benched_until(credential_id, model, now),
                getattr(self.shared_bench_for(credential_id, now), "until", None),
            )
            if value is not None
        ]
        return max(candidates) if candidates else None

    def latest_reset_for(self, credential_id: str, now: float) -> Optional[float]:
        """The furthest live deadline across all models for one credential.

        This is what the host's single provider-scoped cooldown field has to
        hold when the plugin cannot express per-model truth — the safe
        summary, since releasing a key early re-hammers a limit that is still
        spent.
        """
        live = [b for b in self.live_benches_for(credential_id, now) if b.holds(now)]
        return max((bench.until for bench in live), default=None)

    # -- writing ---------------------------------------------------------

    def record(
        self,
        *,
        credential_id: str,
        provider: str,
        model: Any,
        reset_at: float,
        now: float,
        reason: str = "rate_limit",
        scope: str = SCOPE_UNKNOWN,
        extend_to: Optional[float] = None,
    ) -> Optional[Bench]:
        """Remember that this credential is spent on this model until then.

        ``reset_at`` is the deadline the *host* stored and is written down
        exactly as given — it is the fingerprint. ``extend_to`` is how much
        longer KAME wants to hold the key on evidence of its own, and it is
        kept in its own field so the fingerprint survives. Both a caller's
        extension and a longer bench already standing on this pair are honoured;
        the later of them wins.

        Returns the stored bench, or ``None`` when the inputs cannot identify
        one — an unknown credential or an unknown model makes the record
        unusable later, and a bench nobody can match is worse than no bench:
        it consumes the cap and can never be unwound.
        """
        credential_id = str(credential_id or "").strip()
        model_name = normalize_model(model)
        deadline = _coerce_float(reset_at)
        if not credential_id or not model_name or deadline is None:
            return None
        if deadline <= now:
            # Already elapsed. Recording it would create a row that is dead on
            # arrival and reconciles to a no-op.
            return None
        # A refusal that arrives while the pair is still benched is the same
        # episode continuing — very often the answer to a probe. The attempt
        # history has to survive it, or every failed probe would reset the
        # backoff to five minutes and the widening schedule would never widen.
        #
        # A *refuted* bench is not carried, though it is still live: the pair
        # worked in between, so this refusal opens a new episode. Carrying the
        # old deadline would re-apply a number that was already disproved, and
        # carrying the probe count would start the new episode's backoff at
        # whatever the last one had climbed to.
        previous = self._benches.get((credential_id, model_name))
        carried = previous if previous is not None and previous.holds(now) else None

        # Never shorten a standing bench. A key already held to midnight for a
        # spent daily quota does not become free in sixty seconds because a
        # later call came back with a per-minute complaint; the daily counter is
        # still spent and the shorter answer is simply a smaller truth. Taking
        # the longer of the two costs a key sitting out an allowance it may
        # still have, while taking the shorter one walks the pool straight back
        # into the wall it just hit.
        #
        # The longer deadline is carried in ``extended_to`` rather than written
        # over ``reset_at``, because the host is holding the *new* number and
        # that is the one an ownership check has to match. Before v0.1.0 this
        # overwrote the fingerprint, which cost the per-model release for as
        # long as the host's own cooldown ran.
        extended = _coerce_float(extend_to) or 0.0
        if carried is not None:
            extended = max(extended, carried.until)
        if extended <= deadline:
            extended = 0.0
        # Evidence does not evaporate. A provider that named the scope once
        # and stayed quiet the next time has not changed its mind, so a fresh
        # "unknown" never overwrites something the provider actually said.
        scope = str(scope or SCOPE_UNKNOWN).strip().lower()
        if scope not in (SCOPE_PER_MODEL, SCOPE_ACCOUNT):
            scope = carried.scope if carried is not None else SCOPE_UNKNOWN
        bench = Bench(
            credential_id=credential_id,
            provider=str(provider or "").strip().lower(),
            model=model_name,
            reset_at=deadline,
            recorded_at=now,
            reason=str(reason or "rate_limit"),
            probes=carried.probes if carried is not None else 0,
            last_probe_at=carried.last_probe_at if carried is not None else 0.0,
            scope=scope,
            extended_to=extended,
        )
        self._benches[bench.key] = bench
        self.prune(now)
        return bench

    def note_probe(self, credential_id: str, model: Any, now: float) -> Optional[Bench]:
        """Record that this bench was tested at ``now``.

        Written *before* the credential is handed over, never after: the call
        may not come back, and a probe nobody counted is a probe that repeats
        immediately. Returns the updated bench, or ``None`` if there was none
        to update.
        """
        key = (str(credential_id or "").strip(), normalize_model(model))
        bench = self._benches.get(key)
        if bench is None:
            return None
        updated = replace(bench, probes=bench.probes + 1, last_probe_at=now)
        self._benches[key] = updated
        return updated

    def note_success(self, credential_id: str, model: Any, now: float) -> "Refutation":
        """A call on this pair came back clean. Write down what that disproves.

        Two different claims can be standing when a key answers, and they are
        disproved to different depths:

        *The pair's own bench.* "This credential is spent on this model until
        ``reset_at``" — and the credential just served this model. The claim is
        wrong, whole. It is marked refuted rather than deleted, because the
        deadline stored in it is the fingerprint that proves the host's
        matching cooldown is KAME's to unwind. Delete the row and the key is
        stranded behind a bench no longer claimed by anybody.

        *An account-wide bench earned on some other model.* "Every model on
        this key is spent" — and one of them just answered. What is disproved
        is the reach, not the deadline: the model that actually hit the limit
        was never retried, so its own bench stands, narrowed to itself.

        Nothing is invented in the other direction. A success says nothing
        about a pair that was never in question, and this writes nothing for
        one.
        """
        credential_id = str(credential_id or "").strip()
        model_name = normalize_model(model)
        moment = _coerce_float(now)
        if not credential_id or not model_name or moment is None:
            return Refutation()

        refuted: Optional[Bench] = None
        bench = self._benches.get((credential_id, model_name))
        if bench is not None and bench.holds(moment):
            refuted = replace(bench, refuted_at=moment)
            self._benches[refuted.key] = refuted

        narrowed: List[Bench] = []
        for other in list(self._benches.values()):
            if other.credential_id != credential_id or other.model == model_name:
                continue
            if not other.holds(moment) or other.scope != SCOPE_ACCOUNT:
                continue
            demoted = replace(other, scope=SCOPE_PER_MODEL)
            self._benches[demoted.key] = demoted
            narrowed.append(demoted)

        return Refutation(refuted=refuted, narrowed=narrowed)

    def forget(self, credential_id: str, model: Any) -> bool:
        return self._benches.pop((str(credential_id or ""), normalize_model(model)), None) is not None

    def forget_credential(self, credential_id: str) -> int:
        """Drop every bench for one credential — it was removed or revoked."""
        credential_id = str(credential_id or "")
        doomed = [key for key in self._benches if key[0] == credential_id]
        for key in doomed:
            del self._benches[key]
        return len(doomed)

    def prune(self, now: float) -> int:
        """Drop expired benches, then oldest-first down to the cap."""
        expired = [key for key, bench in self._benches.items() if not bench.is_live(now)]
        for key in expired:
            del self._benches[key]
        dropped = len(expired)
        if len(self._benches) > MAX_BENCHES:
            # Evict by deadline: the benches expiring soonest are the ones
            # whose loss costs least, since they were about to lapse anyway.
            # Refuted rows go first regardless of deadline — they withhold
            # nothing, so all they can still cost is a release KAME would have
            # made anyway once the host's own cooldown lapsed.
            ordered = sorted(
                self._benches.values(), key=lambda b: (not b.is_refuted, b.until)
            )
            for bench in ordered[: len(self._benches) - MAX_BENCHES]:
                del self._benches[bench.key]
                dropped += 1
        return dropped

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "benches": [bench.to_dict() for bench in self._benches.values()],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Ledger":
        """Rebuild from persisted state, tolerating anything.

        A ledger that cannot be read yields an empty one, which degrades the
        plugin to its v0.0.3 behaviour — correct cooldowns, no per-model
        rescue. That is the right failure: losing an optimisation, never
        acting on a half-understood record.
        """
        if not isinstance(payload, dict):
            return cls()
        if payload.get("version") != SCHEMA_VERSION:
            return cls()
        rows = payload.get("benches")
        if not isinstance(rows, list):
            return cls()
        benches = [bench for bench in (Bench.from_dict(row) for row in rows) if bench is not None]
        return cls(benches)
