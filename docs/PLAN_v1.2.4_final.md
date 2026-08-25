# KAME Hermes v1.2.4 — Complete Implementation Plan
# Saved: 2026-08-25 | Author: ENI (Antigravity)
# Status: READY TO EXECUTE

## Summary of 6 Fixes

| # | Fix | File(s) | Lines |
|---|---|---|---|
| 1 | Delete 3 dead functions (pacific midnight, offset, looks_like_google) | `core/quota.py` | 782-866 |
| 2 | Remove dead exports | `core/__init__.py` | 38,41,96,99 |
| 3 | Reduce MAX_ABSOLUTE_HORIZON 7d→1d | `core/quota.py` | 39 |
| 4 | Reduce DEFAULT_ACCOUNT_BENCH 24h→1h | `core/quota.py` | 70 |
| 5 | Add per-window escalation caps (per-minute: 300s) | `core/escalate.py` | 59-62, 164 |
| 6 | Fix multi-profile race in state.py | `state.py` | 121-124 |
| 7 | Thread-safe _SilenceTimeout in dispatch_binding.py | `dispatch_binding.py` | 1137-1152 |
| 8 | Clean stale pacific comments (5 files) | multiple | see below |
| 9 | Bump CHANGELOG | `CHANGELOG.md` | top |

---

## CHANGE 1 — core/quota.py : delete dead Google/pacific block

### Lines to DELETE: 782-866
```
(lines 782-866 inclusive — the section starting with
"# ── Google's daily reset ──" through the end of seconds_until_pacific_midnight)
```

### EXACT BEFORE (lines 782-866):
```python
# ── Google's daily reset ──────────────────────────────────────────────────

_GOOGLE_EVIDENCE = (
    "generativelanguage.googleapis.com",
    "google.rpc",
    "googleapis",
    "aiplatform",
)

_GOOGLE_PROVIDERS = frozenset({
    "gemini", "google", "google-gemini", "google-ai-studio", "vertex",
})


def looks_like_google(provider: str = "", message: str = "", body: Any = None) -> bool:
    """Is this Google, by name or by the fingerprints in its own response?

    Used for exactly one decision — whether a daily window resets at
    midnight US/Pacific — and never as a gate on whether to act at all.
    Checking the body as well as the name means a proxy or an aggregator
    that forwards a Google error still gets the right reset time.
    """
    if str(provider or "").strip().lower() in _GOOGLE_PROVIDERS:
        return True
    hay = str(message or "").lower()
    if isinstance(body, (dict, list, tuple)):
        pairs: List[Tuple[str, Any]] = []
        try:
            _walk(body, pairs)
        except Exception:
            pairs = []
        hay += " ".join(str(v) for _, v in pairs if isinstance(v, str)).lower()
    return any(marker in hay for marker in _GOOGLE_EVIDENCE)


def _pacific_offset_hours(moment: datetime) -> int:
    """US/Pacific UTC offset, without depending on tzdata being installed.
    ...
    """
    ...

def seconds_until_pacific_midnight(now: Optional[datetime] = None) -> float:
    """Seconds until the next midnight in US/Pacific.
    ...
    """
    ...
```

### EXACT AFTER (replacing lines 782-866 with nothing — pure deletion):
```python
# (gap removed — section deleted in v1.2.4, the three functions that lived
#  here were never called after v1.2.4 removed their call site)
```

The section marker line 782 (`# ── Google's daily reset`) through
line 866 (end of seconds_until_pacific_midnight) is fully removed.
Line 869 (`# ── the decision ──`) becomes the next section.

---

## CHANGE 2 — core/quota.py : MAX_ABSOLUTE_HORIZON 7d → 1d

### Line 39 BEFORE:
```python
MAX_ABSOLUTE_HORIZON_SECONDS = 7 * 24 * 60 * 60
```
### Line 39 AFTER:
```python
MAX_ABSOLUTE_HORIZON_SECONDS = 24 * 60 * 60  # 1 day — matches Agent Zero _KAME_HARD_DELAY_CAP_S
```

---

## CHANGE 3 — core/quota.py : DEFAULT_ACCOUNT_BENCH 24h → 1h

### Lines 67-70 BEFORE:
```python
# Account-level exhaustion (out of credits) is not a throttle and will not
# clear on its own, but a human may top up, so it is re-probed daily rather
# than treated as permanently dead.
DEFAULT_ACCOUNT_BENCH_SECONDS = 24 * 60 * 60.0
```
### Lines 67-70 AFTER:
```python
# Account-level exhaustion (out of credits) is not a throttle and will not
# clear on its own, but a human may top up, so it is re-probed hourly rather
# than treated as permanently dead. Matches Agent Zero's _KAME_DAILY_COOLDOWN_S = 3600.
DEFAULT_ACCOUNT_BENCH_SECONDS = 60 * 60.0
```

---

## CHANGE 4 — core/__init__.py : remove dead exports

### Lines 38, 41 BEFORE (inside the quota import block):
```python
    looks_like_google,
    ...
    seconds_until_pacific_midnight,
```
### AFTER: those two lines simply deleted.

### Lines 96, 99 BEFORE (inside __all__):
```python
    "looks_like_google",
    ...
    "seconds_until_pacific_midnight",
```
### AFTER: those two lines simply deleted.

---

## CHANGE 5 — core/escalate.py : per-window escalation caps

### Lines 59-62 BEFORE:
```python
# A ceiling on the result, whatever the arithmetic says. A day is the longest
# bench anything else in this plugin produces (the account-level default), and
# a learned number should not be able to out-hold a read one.
MAX_HOLD_SECONDS = 24 * 60 * 60.0
```
### Lines 59-67 AFTER:
```python
# Per-window escalation ceilings. Per-minute and per-hour caps match Agent
# Zero's backoff caps (_KAME_RL_BACKOFF_CAP_S = 300s) so a stuck RPM key
# cannot escalate to a day-long bench. Daily/weekly/monthly keep the 24h
# ceiling because those windows genuinely take hours to recover.
MAX_HOLD_SECONDS = 24 * 60 * 60.0          # daily / weekly / monthly
MAX_PER_MINUTE_HOLD_SECONDS = 300.0         # matches AZ _KAME_RL_BACKOFF_CAP_S
MAX_PER_HOUR_HOLD_SECONDS   = 3600.0        # one full hour max on per-hour escalation

_WINDOW_ESCALATION_CAPS: Dict[str, float] = {}  # populated after QuotaWindow import
```

Then after the import of QuotaWindow, add (after line 46):
```python
from .quota import SOURCE_ANCHOR, QuotaWindow

# Populated here so QuotaWindow is in scope.
_WINDOW_ESCALATION_CAPS = {
    QuotaWindow.PER_MINUTE: MAX_PER_MINUTE_HOLD_SECONDS,
    QuotaWindow.PER_HOUR:   MAX_PER_HOUR_HOLD_SECONDS,
}
```

And add `Dict` to the typing import (line 43):
```python
from typing import Dict, Optional
```

### Line 164 (inside stretch()) BEFORE:
```python
    widened = min(seconds * factor_for(strikes), MAX_HOLD_SECONDS)
```
### AFTER:
```python
    cap = _WINDOW_ESCALATION_CAPS.get(str(window or "").strip().lower(), MAX_HOLD_SECONDS)
    widened = min(seconds * factor_for(strikes), cap)
```

### Lines 69-73 (stale pacific comment) BEFORE:
```python
# A deadline that is a calendar instant rather than a stopwatch reading. Only
# ``quota.py``'s Pacific-midnight branch produces one today, and it says so in
# the decision instead of leaving it to be guessed from the window — a
# non-Google daily cap carries the same window name and is a one-hour
# re-probe, which is a stopwatch and scales correctly.
```
### AFTER:
```python
# A deadline that is a calendar instant rather than a stopwatch reading.
# A stopwatch deadline scales multiplicatively; a calendar anchor (a provider
# that says "your counter rolls at HH:MM") is moved additively instead,
# because multiplying a calendar instant produces nonsense — 2× midnight is
# not a meaningful time.
```

---

## CHANGE 6 — state.py : fix multi-profile race

### Lines 121-124 BEFORE:
```python
    # `<base>/profiles/<name>` -> `<base>`. Checked by shape, not by name, so
    # a profile that happens to be called "profiles" cannot confuse it.
    if home.parent.name == "profiles":
        home = home.parent.parent
```
### AFTER (delete those 4 lines entirely):
```python
    # Each profile gets its own plugin-data directory. Do not collapse to the
    # base home — that caused state.json races between concurrent profiles
    # (fixed v1.2.4).
```

---

## CHANGE 7 — dispatch_binding.py : thread-safe _SilenceTimeout

Need to add `import threading` check (it may already be imported — verify first).

### Find `__slots__` line ~1124 BEFORE:
```python
    __slots__ = ("_seconds", "_previous", "_applied")
```
### AFTER:
```python
    __slots__ = ("_seconds", "_previous", "_applied", "_lock_acquired")
```

### `__enter__` method BEFORE (lines 1137-1143):
```python
    def __enter__(self) -> "_SilenceTimeout":
        if self._seconds <= 0:
            return self
        self._previous = os.environ.get(self.VARIABLE)
        os.environ[self.VARIABLE] = f"{self._seconds:g}"
        self._applied = True
        return self
```
### AFTER:
```python
    def __enter__(self) -> "_SilenceTimeout":
        if self._seconds <= 0:
            return self
        _SILENCE_TIMEOUT_LOCK.acquire()
        self._lock_acquired = True
        self._previous = os.environ.get(self.VARIABLE)
        os.environ[self.VARIABLE] = f"{self._seconds:g}"
        self._applied = True
        return self
```

### `__exit__` method BEFORE (lines 1145-1152):
```python
    def __exit__(self, *_exc: Any) -> None:
        if not self._applied:
            return
        if self._previous is None:
            os.environ.pop(self.VARIABLE, None)
        else:
            os.environ[self.VARIABLE] = self._previous
        self._applied = False
```
### AFTER:
```python
    def __exit__(self, *_exc: Any) -> None:
        try:
            if not self._applied:
                return
            if self._previous is None:
                os.environ.pop(self.VARIABLE, None)
            else:
                os.environ[self.VARIABLE] = self._previous
            self._applied = False
        finally:
            if self._lock_acquired:
                self._lock_acquired = False
                _SILENCE_TIMEOUT_LOCK.release()
```

Also need to add near the top of the class (before class definition):
```python
_SILENCE_TIMEOUT_LOCK = threading.Lock()
```

And add `_lock_acquired` init in `__init__`:
```python
        self._lock_acquired = False
```

---

## CHANGE 8 — stale comment cleanups (no logic changes)

### carousel.py lines 43-44:
BEFORE: `resets at midnight Pacific. Believing it produces a key that returns to`
AFTER:  `returns a short retryDelay on a daily-quota 429. Believing it produces a key that returns to`

### carousel.py lines 80-82:
BEFORE: `#: Agent Zero default, chosen so a key that regains quota at midnight Pacific\n#: is retried hourly rather than every twenty seconds.`
AFTER:  `#: Agent Zero default, chosen so a daily-quota key is retried hourly\n#: rather than hammered every twenty seconds.`

### probe.py line 10:
BEFORE: `read as "midnight Pacific" locks the user out for a day, and if that reading`
AFTER:  `misread as a day-long bench locks the user out, and if that reading`

### escalate.py lines 135-141 (anchor comment inside stretch docstring):
BEFORE: `An *anchor* — midnight US/Pacific, the\n    instant a daily counter is believed to roll — is a moment, and a moment\n    that proves early is moved, by minutes, not multiplied. Scaling an anchor\n    is not merely clumsy: on the daily cap it is a no-op, because the next\n    anchor is already a day away and the ceiling eats the whole multiplier.\n    The deadline in this plugin that most needs correcting was the one\n    escalation could not touch.`
AFTER:  `An *anchor* — a calendar instant the provider says its\n    counter rolls at — is a moment, and a moment that proves early is moved,\n    by minutes, not multiplied. Scaling an anchor is not merely clumsy: on\n    the daily cap it is a no-op because the next anchor is already a day away\n    and the ceiling eats the whole multiplier.`

### dispatch_binding.py line 710:
BEFORE: `recover until midnight Pacific, and ten hours of a spinner is`
AFTER:  `recover until the daily quota rolls over, and hours of a spinner is`

---

## CHANGE 9 — CHANGELOG.md

Add v1.2.4 entry at the top replacing the placeholder with the real content.

---

## Execution Order (if interrupted, resume from next ✗)

- [ ] 1. `core/quota.py` — delete dead block lines 782-866
- [ ] 2. `core/quota.py` — MAX_ABSOLUTE_HORIZON line 39
- [ ] 3. `core/quota.py` — DEFAULT_ACCOUNT_BENCH line 70
- [ ] 4. `core/__init__.py` — remove 2 imports + 2 __all__ entries
- [ ] 5. `core/escalate.py` — add window caps constants + Dict import
- [ ] 6. `core/escalate.py` — modify stretch() line 164
- [ ] 7. `core/escalate.py` — clean anchor comment lines 69-73
- [ ] 8. `state.py` — delete profile stripping lines 121-124
- [ ] 9. `dispatch_binding.py` — add lock + thread-safe __enter__/__exit__
- [ ] 10. Comment cleanups (carousel.py, probe.py, escalate.py docstring, dispatch_binding.py)
- [ ] 11. CHANGELOG.md — update v1.2.4 entry
- [ ] 12. `pytest tests/` — run full test suite
- [ ] 13. Build ZIP to dist/
