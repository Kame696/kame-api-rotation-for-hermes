# Changelog — KAME API Rotation for Hermes

Versions follow `major.minor.patch.build`; the newest is at the top. The
[README version history](README.md#history) also summarizes earlier Hermes
versions and development candidates; this file gives the full notes for the
current 1.8.1.x public releases.

---

## [1.8.1.9] — 2026-10-05 — the carousel through a provider profile, no runtime overrides

The plugin catalog's rule 9 (a listed plugin must not replace, wrap or rebind
Hermes core) closed the 1.8.1.8 catalog update, NousResearch/hermes-agent#131918.
1.8.1.9 removes every rebind and keeps the carousel: it now runs inside the
model client Hermes asks a **provider profile** for — the documented extension
point for bringing your own transport (`ProviderProfile.create_client`).
`hermes plugins validate` passes both packages with no warnings, "no core
override" included.

### Two packages
- `hermes-kame-api-rotation` (general plugin): the carousel, the refusal
  classifier, `/kame`, `/kame-keys`, `/kame-quota` and the Desktop panel.
- `hermes-kame-provider` (`kind: model-provider`): for every bundled API-key
  provider it registers a profile that inherits everything from Hermes' own
  and adds `create_client` and `create_messages_client`, which ask the general
  plugin for a rotating client. Without the general plugin, or with KAME
  switched off, it answers `None` and Hermes builds its own client.

### How the carousel reaches Hermes now
- The client is a subclass of the one Hermes would have built
  (`GeminiNativeClient` for Google's native endpoint, `openai.OpenAI`
  otherwise), so every `isinstance` and attribute Hermes reads still works.
  Only `chat.completions.create` is KAME's.
- Every decision is 1.8.1.8's, moved unchanged from the wrapped dispatch
  function into `transport.py`: key selection, refusal classification and
  sizing, unbounded waits on a fully resting pool, empty-answer budget,
  continuation of a cut answer (stitching), tool-call replay, unanimity rules.
- Waits stream keep-alive chunks every 15 s, so Hermes' stale-stream watchdog
  never mistakes a quota wait for a hung provider.
- Comma-joined keys (`GOOGLE_API_KEY=k1,k2,…`) are split by the client itself;
  custom endpoints read their `custom:<name>` pool, as 1.8.1.8 did.
- Gemini's quota window (`QuotaFailure.quotaId`, per-minute vs per-day)
  survives the streaming error path through an httpx response hook on KAME's
  own connection — no wrapper around Hermes' error factory any more.
- TLS settings of custom providers (`ssl_ca_cert`, `ssl_verify`) are honoured.

### Measured against 1.8.1.8 (real Hermes turns, local fake providers)
Six scenarios (all healthy; per-minute 429 on 5 of 15 keys; per-day 429 on
one model only; 503 overload; every key limited; stream cut mid-answer) on the
native Gemini wire and the OpenAI wire: same turns answered, same requests per
key. Steady turns on the OpenAI wire take 0.2 s instead of 0.6 s. On the
OpenAI wire a cut answer is no longer repeated ("Hello there," three times in
1.8.1.8, once now). Tables: [VALIDATION.md](VALIDATION.md).

### Fixed while porting
- A continuation that dropped after sending a few words lost them between keys.
- A drop that arrives as an error now obeys the 1.6.0.0 stop rule (stop once
  every key has continued the answer and added nothing).
- A tool call cut on every key goes back to Hermes exactly as the last key
  sent it, never a mix of several keys' fragments.
- An empty answer is not journalled as proof that a key is back.

### Every wire, not only chat completions
- **Anthropic Messages** (`anthropic`, `minimax`, `/anthropic` URLs,
  `api.kimi.com/coding`): KAME's Messages client, asked through
  `ProviderProfile.create_messages_client` (the Messages-wire twin of
  `create_client`, NousResearch/hermes-agent#133461). The carousel's decisions
  are the same; `wires.py` translates the stream. A cut answer is continued on
  another key (1.8.1.8 retried it from scratch: 3.5 s instead of 18.4 s in the
  cut-stream scenario). One HTTP pool for all keys: ~19 ms per turn over
  Hermes' own client.
- **Responses** (`xai`, `api.openai.com`, `openai-codex`, any provider
  configured with `api_mode: codex_responses`): KAME's client answers
  `responses.create`, and Hermes' own Responses adapter serves the auxiliary
  lane over it. As in 1.8.1.8, a cut Responses answer is not stitched.

### The status line
- `⏳ waiting on gemini-2.5-pro — KAME 13/15 keys healthy`, the countdown
  while every key rests and the "KAME: … resting" notices are back on every
  surface (CLI spinner, TUI, Desktop, messaging gateway) through
  `agent.status_output.notify_turn_status` (NousResearch/hermes-agent#133474),
  with 1.8.1.8's wording and throttle. On a Hermes without that function the
  line is drawn by the Desktop panel above the message box (`composer.top`), as
  in the first 1.8.1.9 build.

### Settings
- Back from the first 1.8.1.9 build: `spread_disabled` (each call takes the
  first healthy key in pool order, Hermes' own fill_first), `quota_id_disabled`,
  `live_status_disabled`, `resolver_disabled`.
- Not back, because Hermes itself now does what they guarded, since the
  oldest Hermes this release supports (v2026.9.21):
  `gemini_tool_call_fix_disabled` (Hermes keeps parallel Gemini tool calls
  apart; 1.8.1.8's own self-check stands down on it) and
  `field_probe_disabled` (the Desktop saves a pasted key without probing it,
  so a paste of several keys is accepted as typed).
- Hooks: `transform_api_error_classification`, `post_api_request`,
  `on_session_reset` (clears the status-line throttle, never a cooldown).

### Keys in one variable
- `GOOGLE_API_KEY=k1,k2,…` becomes one pool row per key on every start
  (`envsync.py`), and follows the variable: a key added or removed there is
  added or removed in the pool, a variable back to one key is given back to
  Hermes. Only rows KAME made (`source: manual:kame-env:<VAR>`) are ever
  removed. Through Hermes' public pool API; `auth.json` is backed up before
  the first write. `resolver_disabled` turns it off.
- `/kame-keys split [provider]` remains for doing it once by hand.

## [1.8.1.8] — 2026-10-02 — serial reliability

### Fixed
- Profile-local callable ownership and stoppable context-captured heartbeat:
  one profile's unload/reload no longer steals another profile's bindings.
- Exact stream boundaries, productive continuation beyond the previous default
  ten-cut ceiling, and honest no-progress termination. Explicit zero/off and
  operator-selected 1–10 limits remain supported.
- An incomplete, unexecuted tool request replays the original request without
  duplicating its displayed preamble or inventing tool arguments. Terminal
  diagnostics, cancellation and warning preferences are preserved.
- Desktop formatting accepts absent live fields and automatic continuation.

### Improved
- Writer-owned synchronous snapshot cache avoids redundant disk reads while
  preserving atomic writes and detecting foreign-file/home changes. Controlled
  local cost fell about 28 ms (63–65%) per tested call; not total answer speed.
- Independent current-host contract, lifecycle, TCP and mutation checks. The
  full development suite passed 3,157 tests (six skips, four expected failures).
  Public-checkout reproduction and exclusions are reported in VALIDATION.md.

### Preserved and disclosed
- Rotation/classification engine, provider/model choice, reasoning, history,
  signatures, shared quota health and auxiliary/key-intake behavior remain.
- Optional provider-request racing is not shipped. No automatic quality downgrade,
  telemetry, self-updater or credential egress is introduced.
- Guarded internal host bindings remain. The earlier review in #117966/#118402
  is pin-specific; the catalog SHA update requests renewed review under rule 9.
- Representative provider-quality/p95 gains and universal uptime are not claimed.

The 1.8.1.7 development candidate was not released separately; its reproducible
clock-test and verification improvements are included here. Earlier public
releases and their artifacts remain unchanged.

## [1.8.1.7] — 2026-10-01 — reproducible clocks, measured speed, honest admission

**Local owner-test candidate; not published.** The full 1.8.1.6 rotation,
stream continuation, model-scoped cooldown, auxiliary, key-intake and Desktop
behavior is retained. No new delay, client rebuild, parallel request feature
or cross-provider strategy is introduced.

- Promote the existing Windows clock-test repair: backdate activity instead of
  requiring a 10 ms sleep to advance a coarse monotonic clock. New regressions
  exercise repeated instants, delivery versus reasoning, and several clock resolutions.
- Add paired 1/2/14-key healthy-dispatch overhead measurement against the
  immutable official 1.8.1.6 ZIP, exact archive/source verification, sequential
  real-key probes and isolated full-Hermes acceptance tooling.
- Run the pinned upstream no-core-override admission lint without suppressing
  findings. Current rule 9 requires renewed human review of the retained guarded
  bindings; the earlier #117966 ruling allowed them at its reviewed pin, not
  automatically at a new SHA. Standard single-use middleware does not replace
  these retry-capable integrations.
- Preserve all historical version entries and the published 1.8.1.6 artifact.
  Isolated test tools retain their explicit dependency path, suppress lazy
  host installations and use logical fixture epochs only for restart/release
  visibility. Real storm, recovery and lock measurements remain unchanged;
  CI collects `tests/` explicitly rather than archived/research trees.
  Scope, actual test counts, live outcomes, hashes and publication dependencies
  are recorded in `research/1.8.1.7/REPORT.md`.

## [1.8.1.6] — 2026-09-24 — one hour means one hour

**One release, four steps.** v1.8.1.6 is the only GitHub release for
1.8.1.3–1.8.1.6; the entries below it are the steps inside it.

**In a quick list:**

- **No key waits longer than your ceiling** (`max_hold_seconds`, one hour by
  default) — not even when the provider asks for a day.
- **Your `.env` can't be left half-written** if saving it fails; before, a
  failed write could lose keys.
- **A prompt blocked by a content filter is handed back once**, not resent on
  every key.
- **A too-long request no longer loops over every key** because its error
  text happened to contain "429" (as in "142935 tokens").
- **A computer clock that jumps back** (sleep, time sync) can't keep a key out
  past the ceiling.
- **The health file the three profiles share heals itself** when damaged,
  instead of silently switching sharing off.
- **A strange number** — a giant `retryDelay`, a `nan` setting — no longer
  crashes the turn or `/kame set`.
- **Key backups are private** (owner-only) from the first byte.
- **Checked against Hermes 0.21.4 and 0.21.5.**

**In one line:** no key sits out longer than `max_hold_seconds`, whoever set
the hold — the provider included.

- **The provider's own long wait is held to the ceiling.** A `Retry-After` of a
  day, "please try again in 6h12m", Codex's `resets_in_seconds`: Hermes stored
  that deadline and KAME obeyed it, so a key could sit out 24 hours under a
  one-hour ceiling — measured on the real Hermes 0.21.5 pool in four of six
  ways a hold can be set. All six now bring the key back at the ceiling. If the
  provider still refuses then, the key is simply held again: a genuinely long
  outage costs one refused request per hour, which is what the ceiling always
  promised.
- **Lowering the ceiling applies at once**, to holds already running, and to
  Hermes' own fixed one-hour cooldown when you set the ceiling below it.
- **Holds from before the upgrade** are released once they have lasted a
  ceiling.
- Unchanged: a key the provider rejected for good, and a Codex model your plan
  does not include, stay out until you act — those are not waits.

**Verified:** 3,047 offline tests on Windows (3,050 on Linux); host witnesses
green on Hermes 0.21.3, 0.21.4 and 0.21.5, with a new check of the ceiling
against the real pool (`tools/sandbox_binding.py` [18c]). Installed in the
author's running Hermes 0.21.3: 16 of 16 real agent turns answered (Gemini
3.8, NVIDIA Kimi K3), and a replay of 2,634 recorded refusals changed no
decision against 1.8.1.2.

## [1.8.1.5] — 2026-09-24 — the ceiling holds, whatever the clock does

*Shipped inside the v1.8.1.6 release; not released on its own.*

**In one line:** `max_hold_seconds` now bounds every rest for real, and four
inputs that could break KAME's bookkeeping are read safely instead.

**The ceiling**

- **A clock that steps back no longer stretches a rest.** Rests are wall-clock
  deadlines; after the computer's clock stepped back two hours (NTP, a resume,
  a hand-set clock) a 30-second rest had become two hours, on every key resting
  at that moment. A rest is now trimmed to the ceiling from the moment it is
  next looked at.
- **Another profile's longer hold is released at this profile's ceiling.** With
  pool-health sharing on, a sibling profile allowed 9h holds a key for 9h here
  too, even with a 1h ceiling: the ceiling was re-applied from *now* on every
  read, so it moved with the clock and released nothing, while the ETA always
  said "one hour". It is now fixed at the first read of that hold.

**Read safely**

- **A number too large to read** (`retryDelay`, `X-RateLimit-Reset` or
  `retry_after` of hundreds of digits, from a provider or a proxy) is ignored
  instead of ending the turn with an `OverflowError`; the same for a damaged row
  in KAME's own stored benches.
- **A damaged shared pool-health file heals** on the next write. Five kinds of
  damage (bytes that are not UTF-8, a row with a non-numeric time) used to
  switch sharing off for good, silently, and make *Clear pool* fail.
- **`/kame set <setting> nan`** is refused with a sentence instead of a
  traceback, and `KAME_…=nan` in the environment means "not set" instead of the
  lowest allowed value. A panel request that fails for any reason is answered
  as failed instead of looking like a stopped backend.

**Verified:** 3,032 offline tests (Linux); host witnesses green on Hermes
0.21.3, 0.21.4 and 0.21.5; the cross-port replay still agrees with the Agent
Zero port on 860 of 877 refusal shapes; the failure-path fuzz with huge
numbers added raises nothing (26,164 + 23,416 runs). Not yet run live on a
gateway with real keys.

## [1.8.1.4] — 2026-09-24 — safer with your keys, wiser about errors

*Shipped inside the v1.8.1.6 release; not released on its own.*

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

## [1.8.1.3] — 2026-09-24 — checked against Hermes 0.21.4 and 0.21.5

*Shipped inside the v1.8.1.6 release; not released on its own.*

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
