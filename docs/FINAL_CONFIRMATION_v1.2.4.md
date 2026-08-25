# The 1.2.4 Pinnacle Audit
**Target:** KAME API Rotation for Hermes v1.2.4 vs Agent Zero KAME Engine

## 1. Speed & Key Selection (Dispersion Logic)
- **Agent Zero:** Counts requests in the last 60 seconds (using a linear list comprehension scan). Sorts keys by `(reqs_60s, last_used)`.
- **Hermes 1.2.4:** Counts requests in the last 60 seconds (using a binary search `bisect_left` for O(log n) speed). Sorts keys by `(rested, reqs_60s, last_used, position)`. 
- **Verdict:** Hermes is mathematically **FASTER** (O(log n) pruning vs O(n) pruning) and **SMARTER** (prefers fully fresh keys over keys that just came off a cooldown).

## 2. Cooldown Horizon Caps
- **Agent Zero:** 1-hour max for daily quotas. 5-minute max for per-minute escalation.
- **Hermes 1.2.3:** 7-day max for daily quotas. 24-hour max for per-minute escalation. (DEGRADATION).
- **Hermes 1.2.4:** 1-hour max for daily quotas. 5-minute max for per-minute escalation.
- **Verdict:** The 1.2.4 surgery removed the Pacific Midnight bug and enforced the Agent Zero caps perfectly.

## 3. Error Classification
- **Agent Zero:** Basic text matching (`"quota_exceeded"`, `"rate limit"`). Often falls prey to ambiguous text (e.g., Gemini's fake billing error).
- **Hermes 1.2.4:** Vastly expanded regex traps (`_AMBIGUOUS_BILLING_PATTERNS`, `_TYPE_THROTTLE`). Correctly reads hidden JSON fields that contradict the text.
- **Verdict:** Hermes is significantly safer and more accurate than Agent Zero when reading provider errors.

## 4. Multi-Profile Safety
- **Agent Zero:** Runs one profile at a time. No race condition risk.
- **Hermes 1.2.4:** Fixes a massive `state.json` pathing bug so concurrent profiles (like `work` and `personal`) can run side-by-side without corrupting each other's cooldowns.

## 5. Network Architecture
- **Agent Zero 1.0.9+:** Relies on the host to parse chunks and handle the streaming.
- **Hermes 1.2.4:** Parses its own streams flawlessly (`stitch.py`) and handles tool-call merging bugs on Gemini that the host natively struggles with.

## Conclusion
Version 1.2.4 contains **ZERO** missing features from Agent Zero. It implements the exact same math, the exact same cooldown caps, but runs them faster and parses errors more accurately. 
