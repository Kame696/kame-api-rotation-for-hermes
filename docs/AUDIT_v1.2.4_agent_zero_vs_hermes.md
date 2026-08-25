# 🐢⚡ KAME Full Audit — Agent Zero vs Hermes

> **Date:** 2026-08-25
> **Audited by:** ENI (Antigravity) — 5 parallel research agents
> **Scope:** Agent Zero v1.2.0 (`kame_engine.py`) vs Hermes v1.2.4 (`hermes-kame-api-rotation/`)

## Quick Verdicts

| Question | Answer |
|---|---|
| Agent Zero v1.2.0 contaminated by Hermes? | **NO** — zero Hermes code, zero Pacific midnight. Clean. |
| Pacific Midnight still active in Hermes? | **DEAD CODE** — function defined but never called anywhere. |
| `looks_like_google()` still active in Hermes? | **DEAD CODE** — defined, exported, never called. |

## Discrepancies Found (6 Must-Fix)

1. Dead code (pacific midnight, looks_like_google) still in quota.py
2. Escalation cap 86400s (24h) can bench per-minute keys for a day (AZ caps at 300s)
3. No 5xx handling — by design, Hermes host handles this (classify.py:507-531)
4. Multi-profile race on state.json (strips profile path to base home)
5. os.environ bleed in dispatch_binding.py (process-wide timeout env)
6. Account bench 24h vs AZ's 1h

## Resolution

All 6 issues addressed in v1.2.4 proper. See CHANGELOG.md entry for v1.2.4.
