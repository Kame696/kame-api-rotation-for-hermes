"""What actually happened, so the next guess can be better than this one.

The ledger answers "is this key spent on this model right now". It is a
working set: a bench lives until its deadline and is then thrown away. That
makes it useless for the question the plugin has to answer eventually —
*were we right?*

This module keeps the receipts. Every real block is written down with the
prediction that was made at the time, and every recovery that can be observed
is paired back to the block it followed. Two errors then become countable:

**Predicted too short.** The key was handed back at the deadline KAME chose,
the very next attempt failed the same way, and the provider's real window was
longer than the evidence suggested. This is the expensive one — it burns a
request and re-benches the key, over and over, which is exactly the hourly
loop the plugin exists to stop.

**Predicted too long.** The key worked again well before KAME said it would.
Nothing fails, so nothing complains; the cost is silent, in keys sitting on
the bench with allowance left on them.

Neither is visible from a single failure. Both are obvious across twenty, and
twenty is what a journal is for.

**Nothing here ever releases a key.** For five versions this module only
watched, on the rule that a guess tuned against data collected after the
guess existed is not evidence. From v0.1.0 exactly one reading acts —
``short_streak``, the count of consecutive refusals that landed the instant a
deadline lapsed — and it acts in the safe direction only: it can lengthen a
bench, never end one. A release still comes from the ledger, or from a
successful call, and never from a statistic.

**And a bench only counts if it was actually served.** v0.1.0 read the
sequence off the blocks alone, which quietly assumed the key spent the whole
stretch waiting. It often does not: a probe answers and the key is released
early, or it is handed back on time and works for a minute before hitting the
limit again. Either way the refusal that follows is a fresh limit, not a
measurement, so from v0.1.1 a recorded success inside the gap breaks the run
— see ``_worked_in_between``.

**Facts only, and no error text ever.** The classification hook's contract
warns that the message and body it hands over may carry an unredacted
provider dump, which on an auth failure can include key material. What is
stored here is a timestamp, an identifier, a status code, and KAME's own
verdict about the window — every field either produced by this plugin or
already a public property of the request.

Framework-agnostic, like the rest of ``core``: plain data in, plain data out,
no clock of its own. The same records back an Agent Zero build later, which
is the point — the Hermes install is where the observations are cheap to
collect, not where they stop being useful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ledger import _coerce_float, normalize_model

# Bumped when the persisted shape changes in a way older code cannot read.
# An unknown version is discarded rather than guessed at; losing history costs
# accuracy in a future tuning pass, while misreading it would teach the plugin
# something false and durable.
SCHEMA_VERSION = 1

# Ceilings, not targets. A block is ~200 bytes of JSON, so the whole document
# stays under a hundred kilobytes against a 10 MiB allowance. The caps exist
# so a pathological provider — one echoing a fresh model name per call, or a
# credential failing in a tight loop — cannot grow the file without bound.
MAX_BLOCKS = 300
MAX_RECOVERIES = 300

# How far back a record is worth keeping. Quota policy changes: a provider's
# free tier from a month ago is not evidence about its free tier today, and a
# stale sample that outvotes a fresh one is worse than no sample.
MAX_AGE_SECONDS = 14 * 86400.0

# A repeat block this soon after the previous prediction's deadline means the
# key was handed back, used once, and rejected — the prediction was short.
# Wide enough to survive a key that is not retried the instant it frees up,
# narrow enough that an unrelated failure hours later is not blamed on it.
UNDER_PREDICTION_GRACE_SECONDS = 180.0

# Who chose the cooldown that was stored on the credential.
SIZED_BY_KAME = "kame"
SIZED_BY_HOST = "host"
# Three things can happen to a deadline, and for a long time this field had
# two values for them. KAME can decline to size the bench; KAME can size it
# and the pool can store its number; and KAME can size it and the number can
# fail to survive into the entry — a host that clamps it, an entry replaced
# between the verdict and the write, a Hermes build whose cooldown field
# moved. The third used to be recorded as ``host``, exactly like the first.
#
# That is the one reading that has to stay separable, because it is the
# signature of an inert plugin: every refusal classified, every deadline
# computed, and nothing of it reaching the pool. Folded into ``host``, a
# plugin whose numbers are all being dropped produces a journal that looks
# like a quiet, healthy install — the same failure shape as v0.1.3, where
# three versions of careful sizing sat behind a misclassification and never
# ran. Measurement still counts only ``kame``: a bench KAME did not govern
# is not evidence about KAME's sizing either way.
SIZED_BY_DROPPED = "dropped"

_SIZED_BY_VALUES = frozenset({SIZED_BY_KAME, SIZED_BY_HOST, SIZED_BY_DROPPED})


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def _sized_by(value: Any) -> str:
    """Coerce to one of the three, defaulting to the claim that says least.

    A row written by a newer version, or a row corrupted, must not be read as
    "KAME sized this" — that is the value the measurement acts on.
    """
    cleaned = _clean(value)
    return cleaned if cleaned in _SIZED_BY_VALUES else SIZED_BY_HOST


@dataclass(frozen=True)
class Block:
    """One credential, refused on one model, at one moment.

    ``reset_at`` is the deadline the key was **actually held to**, not the one
    KAME proposed. Normally that is what the pool stored; when KAME is holding
    the key past the host's own cooldown (see ``escalate.py``) it is KAME's
    longer number, because that is the one that governed. Recording the
    outcome rather than the intent is what makes the record checkable later —
    and it is what lets a widened deadline that is *still* too short be
    measured as short, instead of resetting the evidence to zero.
    """

    at: float
    provider: str
    model: str
    credential_id: str
    status_code: Optional[int] = None
    window: str = "unknown"
    source: str = ""
    reset_at: Optional[float] = None
    sized_by: str = SIZED_BY_HOST
    reason: str = "rate_limit"

    @property
    def key(self) -> Tuple[str, str]:
        return (self.credential_id, self.model)

    @property
    def predicted_seconds(self) -> Optional[float]:
        """How long the key was going to be held, per this block's decision."""
        if self.reset_at is None:
            return None
        return max(0.0, self.reset_at - self.at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "provider": self.provider,
            "model": self.model,
            "credential_id": self.credential_id,
            "status_code": self.status_code,
            "window": self.window,
            "source": self.source,
            "reset_at": self.reset_at,
            "sized_by": self.sized_by,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["Block"]:
        if not isinstance(payload, dict):
            return None
        at = _coerce_float(payload.get("at"))
        model = normalize_model(payload.get("model"))
        credential_id = str(payload.get("credential_id") or "").strip()
        if at is None or not model or not credential_id:
            # Without all three the row cannot be grouped, paired, or aged out.
            return None
        status_code = payload.get("status_code")
        return cls(
            at=at,
            provider=_clean(payload.get("provider")),
            model=model,
            credential_id=credential_id,
            status_code=int(status_code) if isinstance(status_code, int) else None,
            window=_clean(payload.get("window")) or "unknown",
            source=_clean(payload.get("source")),
            reset_at=_coerce_float(payload.get("reset_at")),
            sized_by=_sized_by(payload.get("sized_by")),
            reason=str(payload.get("reason") or "rate_limit"),
        )


@dataclass(frozen=True)
class Recovery:
    """The first success seen on a pair after it was refused.

    One per ``(credential, model)``, overwritten by the next block/recovery
    cycle. Keeping only the freshest is deliberate: the question this answers
    is "how long does this pair really take to come back", and last week's
    answer is not evidence about today's quota policy.
    """

    credential_id: str
    provider: str
    model: str
    blocked_at: float
    recovered_at: float
    predicted_reset_at: Optional[float] = None

    @property
    def key(self) -> Tuple[str, str]:
        return (self.credential_id, self.model)

    @property
    def observed_seconds(self) -> float:
        return max(0.0, self.recovered_at - self.blocked_at)

    @property
    def was_early(self) -> bool:
        """The key worked before the deadline KAME set for it."""
        return (
            self.predicted_reset_at is not None
            and self.recovered_at < self.predicted_reset_at
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "provider": self.provider,
            "model": self.model,
            "blocked_at": self.blocked_at,
            "recovered_at": self.recovered_at,
            "predicted_reset_at": self.predicted_reset_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["Recovery"]:
        if not isinstance(payload, dict):
            return None
        credential_id = str(payload.get("credential_id") or "").strip()
        model = normalize_model(payload.get("model"))
        blocked_at = _coerce_float(payload.get("blocked_at"))
        recovered_at = _coerce_float(payload.get("recovered_at"))
        if not credential_id or not model or blocked_at is None or recovered_at is None:
            return None
        return cls(
            credential_id=credential_id,
            provider=_clean(payload.get("provider")),
            model=model,
            blocked_at=blocked_at,
            recovered_at=recovered_at,
            predicted_reset_at=_coerce_float(payload.get("predicted_reset_at")),
        )


class Journal:
    """Blocks in the order they happened, and the latest recovery per pair."""

    __slots__ = ("_blocks", "_recoveries")

    def __init__(
        self,
        blocks: Optional[Iterable[Block]] = None,
        recoveries: Optional[Iterable[Recovery]] = None,
    ) -> None:
        self._blocks: List[Block] = sorted(blocks or (), key=lambda b: b.at)
        self._recoveries: Dict[Tuple[str, str], Recovery] = {}
        for recovery in recoveries or ():
            self._recoveries[recovery.key] = recovery

    # -- reading ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._blocks)

    def blocks(self) -> List[Block]:
        """Oldest first."""
        return list(self._blocks)

    def recoveries(self) -> List[Recovery]:
        return list(self._recoveries.values())

    def last_block(self, credential_id: str, model: Any) -> Optional[Block]:
        wanted = (str(credential_id or "").strip(), normalize_model(model))
        for block in reversed(self._blocks):
            if block.key == wanted:
                return block
        return None

    def recovery_for(self, credential_id: str, model: Any) -> Optional[Recovery]:
        return self._recoveries.get(
            (str(credential_id or "").strip(), normalize_model(model))
        )

    # -- writing ---------------------------------------------------------

    def record_block(
        self,
        *,
        at: float,
        provider: str,
        model: Any,
        credential_id: str,
        status_code: Optional[int] = None,
        window: str = "unknown",
        source: str = "",
        reset_at: Optional[float] = None,
        sized_by: str = SIZED_BY_HOST,
        reason: str = "rate_limit",
    ) -> Optional[Block]:
        """File one refusal. Returns the stored row, or ``None`` if unusable.

        A row without a credential or a model can never be grouped or paired,
        so it is dropped rather than stored as a fact about nothing.
        """
        moment = _coerce_float(at)
        model_name = normalize_model(model)
        credential = str(credential_id or "").strip()
        if moment is None or not model_name or not credential:
            return None
        block = Block(
            at=moment,
            provider=_clean(provider),
            model=model_name,
            credential_id=credential,
            status_code=status_code if isinstance(status_code, int) else None,
            window=_clean(window) or "unknown",
            source=_clean(source),
            reset_at=_coerce_float(reset_at),
            sized_by=_sized_by(sized_by),
            reason=str(reason or "rate_limit"),
        )
        self._blocks.append(block)
        # A block that arrives out of order — a clock adjustment, a record
        # replayed from another process — would break the "oldest first"
        # promise every reader here relies on.
        if len(self._blocks) > 1 and block.at < self._blocks[-2].at:
            self._blocks.sort(key=lambda b: b.at)
        self.prune(moment)
        return block

    def record_success(
        self,
        *,
        at: float,
        provider: str,
        model: Any,
        credential_id: str,
    ) -> Optional[Recovery]:
        """Pair a success back to the block it followed, if it teaches anything.

        Returns ``None`` — meaning "do not bother persisting" — for the
        overwhelmingly common case of a call that succeeded on a pair with no
        outstanding question. Successes are the normal state of the world; if
        every one of them were a write, the journal would cost a locked file
        update per API call to record that nothing happened.
        """
        moment = _coerce_float(at)
        if moment is None:
            return None
        block = self.last_block(credential_id, model)
        if block is None or block.at > moment:
            return None
        if moment - block.at > MAX_AGE_SECONDS:
            # The block is older than anything this journal keeps; pairing it
            # would produce a "recovery" measured against a forgotten world.
            return None
        known = self._recoveries.get(block.key)
        if known is not None and known.blocked_at >= block.at:
            # Already answered for this block. The first success is the
            # measurement; later ones only confirm it.
            return None
        recovery = Recovery(
            credential_id=block.credential_id,
            provider=_clean(provider) or block.provider,
            model=block.model,
            blocked_at=block.at,
            recovered_at=moment,
            predicted_reset_at=block.reset_at,
        )
        self._recoveries[recovery.key] = recovery
        self.prune(moment)
        return recovery

    def forget_credential(self, credential_id: str) -> int:
        """Drop everything about one credential — it was removed or revoked."""
        credential = str(credential_id or "").strip()
        before = len(self._blocks) + len(self._recoveries)
        self._blocks = [b for b in self._blocks if b.credential_id != credential]
        for key in [k for k in self._recoveries if k[0] == credential]:
            del self._recoveries[key]
        return before - (len(self._blocks) + len(self._recoveries))

    def prune(self, now: float) -> int:
        """Drop what is too old, then the oldest, down to the caps."""
        moment = _coerce_float(now)
        dropped = 0
        if moment is not None:
            horizon = moment - MAX_AGE_SECONDS
            kept = [b for b in self._blocks if b.at >= horizon]
            dropped += len(self._blocks) - len(kept)
            self._blocks = kept
            stale = [k for k, r in self._recoveries.items() if r.recovered_at < horizon]
            for key in stale:
                del self._recoveries[key]
            dropped += len(stale)
        if len(self._blocks) > MAX_BLOCKS:
            excess = len(self._blocks) - MAX_BLOCKS
            self._blocks = self._blocks[excess:]
            dropped += excess
        if len(self._recoveries) > MAX_RECOVERIES:
            ordered = sorted(self._recoveries.values(), key=lambda r: r.recovered_at)
            for recovery in ordered[: len(self._recoveries) - MAX_RECOVERIES]:
                del self._recoveries[recovery.key]
                dropped += 1
        return dropped

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "blocks": [block.to_dict() for block in self._blocks],
            "recoveries": [recovery.to_dict() for recovery in self._recoveries.values()],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Journal":
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
            return cls()
        rows = payload.get("blocks")
        pairs = payload.get("recoveries")
        blocks = [
            block
            for block in (Block.from_dict(row) for row in (rows if isinstance(rows, list) else []))
            if block is not None
        ]
        recoveries = [
            recovery
            for recovery in (
                Recovery.from_dict(row) for row in (pairs if isinstance(pairs, list) else [])
            )
            if recovery is not None
        ]
        return cls(blocks, recoveries)


# ── derived statistics ────────────────────────────────────────────────────
# Kept out of the stored shape on purpose. Facts persist; interpretation is
# recomputed, so a better rule can be applied to records that were collected
# before anyone thought of it.


def _landed_right_after(previous: Optional[Block], at: float) -> bool:
    """Did a refusal at ``at`` arrive the moment ``previous``'s deadline lapsed?

    That sequence — held until X, handed back, refused again within minutes —
    is the one thing the journal can prove about a deadline without knowing
    anything about the provider: it was too short.

    Necessary, and on its own not sufficient: see ``_worked_in_between``.
    """
    if previous is None or previous.reset_at is None:
        return False
    elapsed = at - previous.reset_at
    return 0.0 <= elapsed <= UNDER_PREDICTION_GRACE_SECONDS


def _worked_in_between(
    journal: "Journal",
    credential_id: str,
    model: str,
    *,
    after: float,
    before: float,
) -> bool:
    """Did this exact pair answer a call between one refusal and the next?

    If it did, the stretch was not a bench at all and the refusal that follows
    proves nothing about the deadline. Two ways that happens, both ordinary:
    a probe answered and v0.0.9 released the key early, or the key was handed
    back on time, worked for a minute, and hit the limit again inside the
    grace window. Reading either as "the deadline was short" would widen a
    bench on a coincidence — and on a two-key pool both are common.

    The recovery this reads is written from the best-effort selection mirror,
    so it can be missed. That asymmetry is fine and is the reason this check
    is allowed to depend on it: a missed recovery leaves the previous
    version's behaviour, while a spurious one only declines to hold a key
    longer. Evidence is required to withhold; nothing is required to stop.

    One recovery per pair is enough, even though a run is walked backwards
    over several. A newer success overwrites an older one, but a success in
    the *newest* gap breaks the walk on its first link, so the walk never
    reaches a gap whose evidence has been overwritten.
    """
    recovery = journal.recovery_for(credential_id, model)
    if recovery is None:
        return False
    return after < recovery.recovered_at < before


def short_streak(
    journal: Journal,
    *,
    credential_id: str,
    model: Any,
    window: str,
    at: float,
) -> int:
    """Consecutive proven-short deadlines, counting a refusal happening now.

    The subject is the refusal at ``at`` — the one being classified this very
    moment, before it has been written down. It counts as the newest link in
    the chain, so the answer is "including this one, how many times in a row
    has this deadline been measured too short".

    Charged narrowly, by design. The run is per credential and per model,
    because a key's limits are the key's own — a free key and a paid one on
    the same model do not share a window — and every link must carry the same
    ``window``, because a per-minute throttle proving short is not evidence
    about a daily cap. Anything that breaks the run resets it to zero, which
    is what makes the widening it feeds self-clearing: no timer, no decay, just
    one ordinary refusal landing at an ordinary time.
    """
    moment = _coerce_float(at)
    if moment is None:
        return 0
    wanted = _clean(window) or "unknown"
    pair = (str(credential_id or "").strip(), normalize_model(model))
    if not pair[0] or not pair[1]:
        return 0

    history = [block for block in journal.blocks() if block.key == pair]
    if not history:
        return 0

    # Walk the run backwards from the refusal in flight. Each link needs the
    # deadline before it to have been the same kind of claim, or the wrong
    # window would inherit the blame for another window's mistake.
    streak = 0
    subject_at = moment
    for previous in reversed(history):
        if previous.at > subject_at:
            # Out of order — a clock adjustment, a replayed record. Not a chain.
            break
        if _clean(previous.window) != wanted:
            break
        if not _landed_right_after(previous, subject_at):
            break
        if _worked_in_between(
            journal, pair[0], pair[1], after=previous.at, before=subject_at
        ):
            # The key answered during the stretch it was supposed to be
            # serving. Nothing was measured, so nothing is charged.
            break
        streak += 1
        subject_at = previous.at
    return streak


@dataclass(frozen=True)
class WindowStat:
    """What the journal knows about one provider, model and quota window."""

    provider: str
    model: str
    window: str
    blocks: int = 0
    kame_sized: int = 0
    # Benches KAME sized whose number did not reach the pool. Counted, never
    # measured: see the note on SIZED_BY_DROPPED.
    kame_dropped: int = 0
    shortest_predicted: Optional[float] = None
    longest_predicted: Optional[float] = None
    under_predictions: int = 0
    recoveries: int = 0
    fastest_recovery: Optional[float] = None
    early_recoveries: int = 0
    last_seen: float = 0.0

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.provider, self.model, self.window)

    @property
    def looks_short(self) -> bool:
        """The evidence says this window is being under-predicted.

        Two independent repeats, not one: a single key retried at the wrong
        moment is a coincidence, and a rule that fires on coincidences would
        stretch cooldowns on noise.
        """
        return self.under_predictions >= 2

    @property
    def looks_long(self) -> bool:
        """Keys here come back sooner than KAME holds them."""
        return self.early_recoveries >= 2

    @property
    def looks_ignored(self) -> bool:
        """KAME sized these benches and the pool kept none of its numbers.

        Not a statement about the sizing — a statement that the sizing is not
        reaching anything. One dropped deadline is a race or a clamp; every
        deadline dropped, with none stored, is a plugin running for nothing.
        """
        return self.kame_dropped >= 2 and self.kame_sized == 0


def summarize(journal: Journal, *, now: float) -> List[WindowStat]:
    """Group the raw records into one row per provider/model/window.

    Under-predictions are counted per credential, in time order, and charged
    to the *later* block — the one that proves the earlier deadline was too
    early. Grouping by window only afterwards keeps a per-minute throttle from
    inheriting the blame for a daily cap on the same model.
    """
    horizon = now - MAX_AGE_SECONDS
    fresh = [block for block in journal.blocks() if block.at >= horizon]

    # Which blocks are repeats that landed right after a previous deadline.
    # Tracked by position rather than by value: two failures of the same key
    # on the same model can be identical in every field except the timestamp,
    # and a set of blocks would silently merge them.
    short_calls: set = set()
    by_pair: Dict[Tuple[str, str], List[int]] = {}
    for index, block in enumerate(fresh):
        by_pair.setdefault(block.key, []).append(index)
    for series in by_pair.values():
        previous: Optional[Block] = None
        for index in series:
            block = fresh[index]
            if _landed_right_after(previous, block.at) and not _worked_in_between(
                journal, block.credential_id, block.model,
                after=previous.at, before=block.at,  # type: ignore[union-attr]
            ):
                short_calls.add(index)
            previous = block

    recoveries_by_group: Dict[Tuple[str, str], List[Recovery]] = {}
    for recovery in journal.recoveries():
        if recovery.recovered_at < horizon:
            continue
        recoveries_by_group.setdefault((recovery.provider, recovery.model), []).append(recovery)

    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for index, block in enumerate(fresh):
        bucket = grouped.setdefault(
            (block.provider, block.model, block.window),
            {
                "blocks": 0,
                "kame_sized": 0,
                "kame_dropped": 0,
                "predicted": [],
                "under": 0,
                "last_seen": 0.0,
            },
        )
        bucket["blocks"] += 1
        if block.sized_by == SIZED_BY_KAME:
            bucket["kame_sized"] += 1
        elif block.sized_by == SIZED_BY_DROPPED:
            bucket["kame_dropped"] += 1
        predicted = block.predicted_seconds
        if predicted is not None:
            bucket["predicted"].append(predicted)
        if index in short_calls:
            bucket["under"] += 1
        bucket["last_seen"] = max(bucket["last_seen"], block.at)

    stats: List[WindowStat] = []
    for (provider, model, window), bucket in grouped.items():
        # A recovery is measured per pair and knows nothing about which window
        # caused it, so it is attributed to every window seen for that model.
        # Over-holding is a property of the model's benches as a whole, which
        # is the granularity the report speaks at.
        paired = recoveries_by_group.get((provider, model), [])
        durations = [recovery.observed_seconds for recovery in paired]
        predicted: List[float] = bucket["predicted"]
        stats.append(
            WindowStat(
                provider=provider,
                model=model,
                window=window,
                blocks=bucket["blocks"],
                kame_sized=bucket["kame_sized"],
                kame_dropped=bucket["kame_dropped"],
                shortest_predicted=min(predicted) if predicted else None,
                longest_predicted=max(predicted) if predicted else None,
                under_predictions=bucket["under"],
                recoveries=len(paired),
                fastest_recovery=min(durations) if durations else None,
                early_recoveries=sum(1 for recovery in paired if recovery.was_early),
                last_seen=bucket["last_seen"],
            )
        )
    stats.sort(key=lambda stat: (-stat.last_seen, stat.provider, stat.model))
    return stats
