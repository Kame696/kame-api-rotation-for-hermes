# Changelog — KAME API Rotation for Hermes

Versions follow `major.minor.patch.build`; the newest is at the top. The
[README version history](README.md#history) also summarizes earlier Hermes
versions and development candidates; this file gives the full notes for the
current 1.8.1.x public releases.

---

## [1.8.1.4] — safer with your keys, wiser about errors

**In one line:** the agent still never stops on a quota; KAME now never loses
a key on a failed write, never mistakes a token count for a rate limit, and
never mistakes a rate limit for the end of the turn.

**Your keys**

- **The `.env` cannot be left half-written.** `/kame set` and every panel save
  rewrite Hermes' `.env` — the file with all your provider keys. A write that
  failed midway (a full disk, a killed process) used to truncate it: measured,
  46 of 61 key lines gone. It is now written beside, flushed and swapped in one
  step, keeping the file's permissions, its symlink and its line endings.
- **Key backups are owner-only from the first byte** (they used to exist for a
  moment with default permissions).
- **A key in a URL** (`?key=`, `api_key=`, `token=`) is redacted from the
  refusal file whatever it looks like.

**Reading errors**

- **A context-too-long error is handed back**, not rotated. A token count like
  `142935` contains the digits 429 and was read as a rate limit — every key was
  rested while the oversized request was sent again. `429` now counts only as a
  number on its own, and a certain request-fault field
  (`context_length_exceeded`, `model_not_found`) ends the turn first.
- **A rate limit never ends the turn**, whatever words it uses ("Request blocked
  by rate limiting rule" used to), and a malformed request that merely mentions
  "quota" or "429" is handed back instead of going round every key.
- **A flagged prompt is not resent on every key**: a `content_policy_violation`
  code, or a prompt "blocked by the safety filter", is handed back even on a 403.
  A key denial that happens to say "blocked by" still rotates.
- **A scalar `error.details`** (`1`, `true`) no longer crashes the failure
  handler into a failed turn.
- **Gemini's host footer comes off whole** even when only its opening is known,
  so a 21-second per-minute throttle is no longer read as billing for an hour.
- **Gemini's bare `RESOURCE_EXHAUSTED`** takes the 1-2-4…64s ladder in the
  SDK's `429 RESOURCE_EXHAUSTED:` rendering too.

**Hermes 0.21.4+**

- **Per-model cooldowns are watched.** Hermes now benches an Anthropic 429 per
  model on its own path; KAME carries its reading there (an unsized 429 rests
  30s, not the host's hour) and the cooldown appears in `/kame events`.
- **The journal records the deadline that governed** — a host-set 24h hold is
  no longer written down as one hour.

**Verified:** 2,970 offline tests (Linux); host witnesses green on Hermes
0.21.3, 0.21.4 and 0.21.5; a cross-port replay of the 877 refusal shapes this
suite exercises gives the Agent Zero port's decision on 860 (the rest are
documented port differences); a fuzz of 26,164 odd payloads through both ports'
failure paths raises nothing. Not yet run live on a gateway with real keys.

## [1.8.1.3] — checked against Hermes 0.21.4 and 0.21.5

**In one line:** nothing the agent does changes; KAME was re-checked against the
two Hermes releases that shipped after 1.8.1.2, and the checks themselves were
fixed where they cried wolf.

- **Hermes 0.21.4 and 0.21.5 verified offline.** Installed from their release
  tags and run through every host witness in `tools/`: host facts 40/40,
  runtime contracts 12/12 (each mutation caught), Hermes' own error corpus and
  credential-pool suites unchanged by KAME apart from the two load-spreading
  assertions it changes on purpose, Gemini contracts and prose stripping green.
  0.21.4+ adds `model=` to pool selection (forwarded untouched) and a per-model
  cooldown path of its own for Anthropic 429s and Codex entitlement refusals
  (see the report for what that path does not tell KAME).
- **Four witnesses no longer mistake a reformatted Hermes for a broken one.**
  Desktop's wait-row regex now spans three lines; the installer's manifest cap
  has two real shapes (private on 0.21.3, shared on 0.21.4+); one pool suite
  file was folded into another in 0.21.5 (eight new pool suites now run too);
  the API server checks agents into a memory-session pool in 0.21.5.
- **Gate tests read this run's evidence.** Since 1.8.1.2 the clock and
  continuity gates write to a sandbox, but their tests still read
  `research/1.8.0.0/` — passing on stale files where that folder exists and
  failing on every fresh clone. The double-burn scenario now counts only the
  burns sharing can prevent (turns the carousel called healthy): sharing off 34
  in every repeat, sharing on median 0.
- **CI.** `tests.yml` (3 OSes × Python 3.11–3.13) and a weekly `host-compat.yml`
  that installs the newest Hermes tag and runs the witnesses.
- The drive-letter deploy test runs on Windows only.

## [1.8.1.2] — 2026-09-24 — smarter reading, same non-stop agent

**In one line:** the agent still never stops on a quota; KAME now reads the
wait time in more places and wastes fewer calls.

- **Better detection.** When a provider says how long to wait — in a header,
  in the error body, or in plain words like "try again in 7s" — KAME obeys it.
  1.8.1.1 missed the plain-words and body cases and waited a fixed 30s instead.
- **Fewer wasted calls.** A ChatGPT/Codex plan that says "back in 3.5 hours"
  is no longer re-tried every 30 seconds; the key rests up to the one-hour cap.
- **Better judgment.** If *every* key says "your plan does not include this"
  or "your country needs billing", the turn shows that message instead of
  waiting forever — waiting can never fix those. Quota and rate limits still
  wait and come back on their own, exactly as before.
- **Same brain as Agent Zero.** Both ports now give the same answer on all
  1,984 real errors recorded by the author (checked by a new tool).
- **Tested for real:** hours of live agent turns on 14 Gemini and 2 NVIDIA
  keys through the running Hermes gateway, plus every existing test suite.

## [1.8.1.1] — 2026-09-22 — reliability patch

- **Clear pool reports the truth.** Shared health, the ledger, the receipt
  journal and Hermes' own exhausted marks must all clear; a persistence or host
  failure is surfaced instead of returning a successful reset.
- **Recorder redaction is stricter.** Structured and textual credentials,
  Authorization values, passwords and refresh tokens are removed before
  evidence is stored, while quota identifiers remain available to classification.
- **The optional stream-silence timeout is per call.** It now uses a ContextVar
  read through Hermes' in-call timeout reader instead of temporarily mutating a
  process-global environment variable, so concurrent calls cannot race or
  restore one another's values. An explicit Hermes timeout still wins.
- **Current Hermes compatibility was re-checked.** The installer now shares the
  loader's manifest-version cap; KAME stays on manifest v1 because it needs no
  v2-only syntax and this preserves older installer compatibility.
- **Dispatch preserves evidence.** HTTP 498 is server capacity; plan entitlement,
  Gemini billing preconditions and Anthropic spend limits remain account failures
  even on HTTP 400. Gateway model/channel denials do not retire credentials.
- **The unsized delay dial is honored.** A generic classifier default is no longer
  passed off as a provider deadline. Named windows, explicit retry hints and the
  bare-Gemini 1..64s ladder retain their own rules.
- Relayed moderation remains terminal; other upstream relay failures retain
  Hermes host ownership instead of penalizing the aggregator credential.
- Desktop's host-marked unified-package mirror is no longer reported as an
  obsolete duplicate. Deployment preserves that host-managed panel.
- Final validation: 2,883 Hermes tests passed, installed-host gates passed,
  and short CLI turns answered with Gemini 3.6/3.7/3.8 and NVIDIA Kimi K3.
  Agent Zero installation and long owner conversations remain separate.

---

## [1.8.1.0] — 2026-09-21 — error-reader release baseline

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
