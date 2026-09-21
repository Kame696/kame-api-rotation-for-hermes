# Changelog — KAME API Rotation for Hermes

Versions follow `major.minor.patch.build`; the newest is at the top.

---

## [1.8.1.0] — 2026-09-21 — first public release

**Picks the key**

- The healthiest key for every call, not only after a failure: fewest requests
  in the last 60 seconds wins, least recently used breaks the tie.
- Several keys in one provider field, comma-separated. Health is kept per
  `provider:model`, so a key spent on one model still serves the others.
- KAME never switches you to another model.

**Reads every refusal from its own evidence** — the payload, never the provider's name

- Built from **13,561 real refusals** (311 distinct messages) recorded from
  Gemini, NVIDIA, OpenRouter, Anthropic and OpenAI-compatible endpoints, and
  graded during development against an independent answer key of **68 error
  shapes from 12 providers and gateways** — Google Gemini, OpenAI, OpenAI
  Codex, Anthropic, NVIDIA, OpenRouter, Groq, DeepSeek, AIHubMix, TokenRouter,
  ZenMux and GLM — sorted into **11 kinds of error**.

- A stated wait (`retryDelay`, `Retry-After`, a reset header) is obeyed to the
  second and never multiplied.
- Gemini's bare `429 RESOURCE_EXHAUSTED` — no number at all — rests the key
  **1s, then 2, 4, 8, 16, 32, 64s** on each repeat, back to 1s the moment it
  answers. Switch: `KAME_UNSIZED_BACKOFF`.
- Any other throttle with no number rests **20–30s**. Measured: a refused key
  retried within 30s answered 0 of 73 times.
- A daily quota re-probes in **5 minutes** while other keys still answer, and
  rests **1 hour** once the whole pool has gone 20 minutes without an answer.
- An account-wide limit benches that credential on every model of the
  provider; tokens-per-minute is its own window.
- A **5xx** rests 1s and never escalates. A **timeout** rotates without resting
  the key.
- An invalid or revoked key leaves the rotation and comes back by itself when
  replaced; a 403 for one model skips only that model.
- **No key is held longer than one hour** (`max_hold_seconds`), whatever set
  the hold.

**Keeps the turn alive**

- When every key is resting, it waits for the first one back and says so,
  instead of failing the turn.
- An answer cut mid-stream is continued on another key and joined into one
  reply.
- Several Hermes profiles share one health file; only key hashes are written.

**What you see**

- A status-bar chip, a Desktop panel (overview, events, settings) and the
  `/kame` and `/kame-keys` commands. A key is never printed in full.
- The Desktop panel ships inside the package at `desktop/plugin.js` and is
  turned on in Desktop **Settings → Plugins**; KAME writes nothing outside its
  own install folder to put it there.
- **Clear pool** starts every key from zero: it also releases the holds in the
  shared health file, KAME's ledger on disk and Hermes' own "exhausted" mark on
  each pooled key.
- `/kame-keys add|import` write to `~/.hermes/auth.json`, after saving a
  plaintext backup `auth.json.kame-<timestamp>.bak` beside it (last 5 kept).

**Verified**

- Works on Hermes **0.21.1 and 0.21.3**. 2,834 offline tests;
  `hermes plugins validate` passes with the security scan **safe**; Hermes'
  own pool and error-classification suites are unchanged by KAME; 12/12
  runtime contracts against the real turn loop.
- Build fingerprint `0101a50d447a` (the same on every copy, whatever its line endings), shown in `/kame` and the panel header.
