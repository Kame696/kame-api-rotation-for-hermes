"""Which key to use *next*, when several are healthy.

Everything else in this plugin answers "is this key usable?". Nothing answers
"of the usable ones, which?" — and the host's default answer is
``STRATEGY_FILL_FIRST``: hand out ``available[0]`` every time. One key absorbs
every request until the provider refuses it, then the next key absorbs every
request until the provider refuses that one. With fifteen keys and a
per-minute limit, that is fifteen consecutive walls instead of fifteen keys'
worth of throughput.

The Agent Zero engine this rotation was ported from has answered that
question since v1.0.0, and it is the one large piece that was left behind:

    best_key = min(healthy, key=lambda k: (
        len(pool[k]["request_log"]),   # fewest requests in the last 60s
        pool[k]["last_used"],          # tie-break: least recently used
    ))
    pool[best_key]["last_used"] = now
    pool[best_key]["request_log"].append(now)   # count it before the call

This is that, with the same window and the same tie-break. The counting at
selection time is not bookkeeping — it is the anti-dogpile: a key is counted
as busy the moment it is handed out, so a second thread selecting a
millisecond later sees it as loaded and picks a different one. Counting after
the call instead would let every concurrent caller pick the same key.

Two things this deliberately does not do. It never *excludes* a key: the
order changes, the set does not, so no request can fail because of anything
decided here. And it holds no key material — only the credential ids the pool
itself uses, which are hashes or short ids and appear in logs already.

Memory-only and bounded. A sixty-second window has no meaning across a
restart, so nothing is persisted; and both the number of buckets and the
number of marks per key are capped, because a long-running agent walking many
models must not turn this into a leak.
"""

from __future__ import annotations

import hashlib
import threading
from bisect import bisect_left, insort
from typing import Dict, List, Optional, Sequence, Set, Tuple

# The window Google, OpenAI and Anthropic all meter requests-per-minute over.
# Same number the Agent Zero engine uses, for the same reason.
WINDOW_SECONDS = 60.0

# A bucket is one (provider, model) pair, which is how per-minute quota is
# metered and how the Agent Zero engine keys its own health state. An agent
# that walks a lot of models would otherwise accumulate one bucket per model
# for the life of the process.
MAX_BUCKETS = 64

# Per key, per bucket. At the ceiling the oldest marks are dropped, which
# understates the load of a key being hammered far beyond any real limit —
# and a key with 240 requests in the last minute is not one this is going to
# recommend anyway.
MAX_MARKS = 240

# How long after a bench lapses a credential still counts as "just released".
# Ninety seconds because the shortest real refusals this plugin sizes are
# per-minute throttles: a key handed back at the end of one is inside the next
# window for about that long, and it is the window this plugin's own journal
# keeps catching a re-refusal in.
JUST_RELEASED_SECONDS = 90.0


def bucket_for(provider: object, model: object) -> str:
    """The name of the counter a request is metered against.

    Falls back to the provider alone when no model has been announced, rather
    than to nothing: two keys on the same provider still spread better against
    a shared count than against no count at all.
    """
    provider_name = str(provider or "").strip().lower()
    model_name = str(model or "").strip().lower()
    return f"{provider_name}:{model_name}" if model_name else provider_name


def mark_id(credential_id: object, secret: object) -> str:
    """The name a credential is counted under: its key, not its row.

    A per-minute limit is metered by the provider against the *key*. The
    pool's id names a *row*, and the two come apart in both directions:

    * Two rows can hold one key. Hermes seeds a provider from the environment
      and from ``auth.json`` and ends up with siblings carrying an identical
      ``runtime_api_key`` — its own rotation code has to detect exactly that
      (``credential_pool.py``, "carry the identical runtime_api_key"). Counted
      by row they look like two idle keys, and spreading across them dogpiles
      one provider counter at twice the intended rate.
    * One row can hold two keys over time. Replace a spent key in place and,
      counted by row, the new key inherits every request the old one spent.

    So the counter is named by the key. SHA-256, truncated, one-way — the same
    construction the ledger uses to name a split key (decision 37). Nothing
    here can be turned back into a key, which is what lets this value sit in a
    dictionary that may end up in a dump.

    Falls back to the bare id when there is no key to read — an OAuth entry,
    whose access token the host rotates, and which would otherwise look like a
    brand-new credential after every refresh.
    """
    material = str(secret or "")
    if not material:
        return str(credential_id or "")
    return f"#{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


class Dispersion:
    """Request marks per credential, per bucket, over a sliding window.

    Every method is safe to call from several threads: selection happens
    inside the pool's own lock on some paths and outside it on others, and a
    structure that is only *usually* consistent would show up as a rare
    wrong-key choice that nobody could reproduce.
    """

    def __init__(
        self,
        *,
        window_seconds: float = WINDOW_SECONDS,
        max_buckets: int = MAX_BUCKETS,
        max_marks: int = MAX_MARKS,
    ) -> None:
        self._window = float(window_seconds)
        self._max_buckets = int(max_buckets)
        self._max_marks = int(max_marks)
        self._lock = threading.Lock()
        # bucket -> credential id -> sorted list of timestamps
        self._marks: Dict[str, Dict[str, List[float]]] = {}
        # bucket -> insertion order, so the oldest bucket is the one dropped
        self._order: List[str] = []
        # bucket -> credential id -> requests since this process started.
        # Nothing decides on these; they exist because the window that does
        # decide is sixty seconds long, and somebody checking whether their
        # keys are being rotated at all should not have to look inside one.
        # One integer per key per bucket, and the buckets are already capped.
        self._totals: Dict[str, Dict[str, int]] = {}

    # -- writing ----------------------------------------------------------

    def note(self, bucket: str, credential_id: str, now: float) -> None:
        """Count one request against a credential.

        Called when a key is *handed out*, not when its call comes back. That
        ordering is the whole anti-dogpile: two threads selecting at the same
        moment must not both see the same key as idle.
        """
        if not bucket or not credential_id:
            return
        with self._lock:
            marks = self._bucket(bucket)
            series = marks.setdefault(str(credential_id), [])
            insort(series, float(now))
            self._prune(series, now)
            if len(series) > self._max_marks:
                del series[: len(series) - self._max_marks]
            running = self._totals.setdefault(bucket, {})
            running[str(credential_id)] = running.get(str(credential_id), 0) + 1

    # -- reading ----------------------------------------------------------

    def load(self, bucket: str, credential_id: str, now: float) -> Tuple[int, float]:
        """``(requests in the window, last used)`` for one credential.

        ``last used`` is ``0.0`` for a key that has not been used inside the
        window, which sorts it first — an unused key is exactly what should be
        picked next.
        """
        with self._lock:
            series = self._marks.get(bucket, {}).get(str(credential_id))
            if not series:
                return 0, 0.0
            self._prune(series, now)
            return len(series), (series[-1] if series else 0.0)

    def order(
        self,
        bucket: str,
        ids: Sequence[str],
        now: float,
        *,
        just_released: Optional[Set[str]] = None,
    ) -> List[str]:
        """The given ids, rested first and least-loaded within that.

        ``just_released`` are the ones whose bench lapsed moments ago. They go
        behind every fully rested key and keep their load order among
        themselves — a preference, never an exclusion, so a pool where every
        key was just released degrades to plain load ordering rather than to
        an empty answer.

        The reason is this plugin's own measurements: a deadline read too
        short shows up as a key refused again within minutes of being handed
        back, which is the pattern ``escalate.py`` widens benches for. A key
        that has been resting is the safer of two otherwise equal choices, and
        equal is what they usually are.

        Ties beyond (rested, count, last used) keep the order they came in, so
        a pool this has never seen comes back exactly as the host arranged it.
        """
        recent = just_released or frozenset()
        with self._lock:
            marks = self._marks.get(bucket, {})
            ranked = []
            for position, raw_id in enumerate(ids):
                entry_id = str(raw_id)
                series = marks.get(entry_id)
                if series:
                    self._prune(series, now)
                count = len(series) if series else 0
                last_used = series[-1] if series else 0.0
                rested = 1 if entry_id in recent else 0
                ranked.append(((rested, count, last_used, position), entry_id))
        ranked.sort()
        return [entry_id for _rank, entry_id in ranked]

    def snapshot(self, now: float) -> Dict[str, Dict[str, int]]:
        """``{bucket: {credential: requests in the window}}``, for the report.

        A copy, taken under the lock, with the window applied — so what is
        rendered is what selection would decide on right now, and the caller
        cannot hold a reference to live state while a selection mutates it.
        """
        with self._lock:
            out: Dict[str, Dict[str, int]] = {}
            for bucket, marks in self._marks.items():
                counted = {}
                for entry_id, series in marks.items():
                    self._prune(series, now)
                    if series:
                        counted[entry_id] = len(series)
                if counted:
                    out[bucket] = counted
            return out

    def introduce(self, bucket: str, ids: Sequence[str]) -> None:
        """Register credentials that exist, whether or not they are ever used.

        Without this the running totals only ever hold keys that have been
        picked, and the row the whole section exists to show — the key that
        has taken *nothing* while another takes everything — is the one row
        that never appears. Called with the candidates of a selection, which
        is the only place in this plugin that sees the whole healthy set.

        Creates counters at zero and never touches one that already exists.
        """
        if not bucket or not ids:
            return
        with self._lock:
            self._bucket(bucket)
            running = self._totals.setdefault(bucket, {})
            for raw_id in ids:
                entry_id = str(raw_id)
                if entry_id:
                    running.setdefault(entry_id, 0)

    def totals(self) -> Dict[str, Dict[str, int]]:
        """``{bucket: {credential: requests since this process started}}``.

        The companion to ``snapshot``: that one answers "is the load spread
        right now", this one answers "has this key ever been used". Both are
        needed, because a minute is a short time to be looking at and a key
        that has taken nothing all day is the interesting case.

        Decides nothing. Selection reads the window and only the window.
        """
        with self._lock:
            return {bucket: dict(counts) for bucket, counts in self._totals.items() if counts}

    # -- internals --------------------------------------------------------

    def _bucket(self, bucket: str) -> Dict[str, List[float]]:
        """The marks for one bucket, evicting the oldest bucket at the cap.

        Caller holds the lock.
        """
        existing = self._marks.get(bucket)
        if existing is not None:
            return existing
        while len(self._order) >= self._max_buckets:
            oldest = self._order.pop(0)
            self._marks.pop(oldest, None)
            # The running totals follow the window state out. Keeping them
            # would make the one structure with no expiry the one with no
            # bound either.
            self._totals.pop(oldest, None)
        created: Dict[str, List[float]] = {}
        self._marks[bucket] = created
        self._order.append(bucket)
        return created

    def _prune(self, series: List[float], now: float) -> None:
        """Drop marks older than the window. Caller holds the lock.

        The series is kept sorted, so this is a binary search and a slice
        rather than a scan — this runs on the path that hands out every
        credential.
        """
        cutoff = float(now) - self._window
        cut = bisect_left(series, cutoff)
        if cut:
            del series[:cut]
