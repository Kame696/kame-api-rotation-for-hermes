# 🐢⚡ Changelog — KAME API Rotation for Hermes

All notable changes to the Hermes port of KAME are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/); versions
follow [Semantic Versioning](https://semver.org/).

Each release opens with **In short** — the whole release in a handful of lines —
and folds the reasoning, the logs it came from and the test results underneath.

---

## 📌 At a glance

| Version | Headline | What changed for you |
|---|---|---|
| **1.2.9** | True provider limits and inspectable payloads | The classifier uses the exact provider name instead of a hardcoded 'gemini', fixing NVIDIA limits; the UI now lets you click on any event to inspect the raw error payload. |
| **1.2.8** | Clean rotation UI | The spinner says 'rotating...' instead of spamming which key is being tested. |
| **1.2.7** | Resilient 429 extraction | Fixes a bug where empty exception messages caused KAME to bench keys for 0 seconds on 429s. |
| **1.2.6** | Header-based waiting | Wired `dispatch_binding.py` into the modern `core.classify` engine. |
| **1.2.5** | Speed update - faster rotation during outages | Adaptive storm timeout slashes per-key wait from 20s to 5s after two consecutive timeouts; provider circuit breaker skips to recovery wait after 3; concurrent agents no longer block each other |
| **1.2.4** | Full parity with Agent Zero on daily caps | Removed the US/Pacific midnight calculation that locked Google Gemini keys for 12-18h on burst errors. Daily quotas floor at 1 hour max (3600s), and per-minute RPM throttles keep standard short recovery |
| **1.2.3** | The panel stopped rebuilding itself under the cursor | 1.2.2's own fix for the flicker was the cause of the next one — a keyless list remounted the settings form mid-save; fixed structurally, with a test that renders the real panel and checks it |
| **1.2.2** | The pool is a mirror, not an archive | The comma-joined list stopped being sent as a key of its own, a key deleted from the config stops being retried, and one save is one movement on screen |
| **1.2.1** | Resilient Gemini Streams | Captures SDK-wrapped stream read timeouts as non-terminal timeouts and rotates smoothly without ending turns |
| **1.2.0** | Settings you can read | Three labelled shelves, so the one optional extra can no longer be mistaken for something rotation needs |
| **1.1.3** | The rest that bought nothing | A key is no longer benched when it is the only well one — two defects found by reading the loop, not by anything failing |
| **1.1.2** | The provider that refuses | Gemini will not be handed its own turn back; the continuation adapts instead of ending the turn it exists to save |
| **1.1.1** | Answers arrive whole | Stream stitching, plus a panel that can actually be used: every switch a switch, every number a field |
| **1.1.0** | A real panel | `/kame` stops being markdown painted raw and becomes a Desktop panel; Gemini's merged tool calls repaired |
| **1.0.10** | The status line that shows up | The host keeps only text in its own shape — matched, so the line appears instead of blanking Hermes' own |
| **1.0.9** | Causes, not symptoms | "It stops", "it freezes", "rewind broke" traced to four host facts — including a 410 the carousel was rotating against |
| **1.0.8** | Trust the connection | The stream watchdog deleted (it corrupted rewind/edit/resend); throttled live status put in its place |
| **1.0.2** | Stable baseline | Storm-log collapse, quota-period ordering, 1103 tests green |
| **1.0.1** | Wait without ceiling | The 10-minute cap removed — a wait ends when the provider recovers, not when a timer says so |
| **1.0.0** | The carousel | A failed call moves to the next key instead of ending the turn |
| **0.0.3** | No provider allowlist | Every decision moved onto evidence in the response — never on who the provider is |

**Version parity with Agent Zero.** The same MAJOR.MINOR means the same
generation of behaviour on both hosts; the patch number moves independently.
The 1.1.x series exists only here, because it fixed stream handling that Agent
Zero owns itself — the two lines rejoin at 1.2.0.

## [1.3.0] - 2026-08-26
### Added
- **Absolute Shield**: KAME now intercepts unrecoverable API errors (like 404 Not Found, 400 Context Exceeded, etc.) and aborts the request immediately. Instead of Hermes blindly retrying 3 times and crashing the chat with a red error, KAME returns a clean, synthetic system message explaining the failure instantly.
- **UI Popup**: Clicking 'inspect payload' in the KAME Desktop UI now opens a direct alert/popup window with the full error details, fixing the issue where inline expansion wasn\'t visible.

## [1.2.9] - 2026-08-26

- **Fixed**: Removed hardcoded `provider="gemini"` in `dispatch_binding.py`. The classifier now uses the actual provider from the identity string, restoring correct logic for NVIDIA and other non-Google providers.
- **Fixed**: Exception body and headers are now passed correctly to the classifier, allowing `Retry-After` headers and structured error JSON to be read.
- **Added**: Desktop UI now supports clicking on any event in the Events tab to expand and inspect the raw error payload (`raw_error`), eliminating the "black box" feeling during outages.

## [1.2.8] - 2026-08-26

- **Changed**: Simplified the UI rotation status. It now displays "rotating..." instead of flickering "on key X", providing a cleaner visual experience.

## [1.2.7] - 2026-08-26

- **Fixed**: Exception message extraction now gracefully falls back to `str(exc)` if the exception lacks a `.message` attribute. This prevents a critical bug where empty error strings caused `extract_retry_delay_seconds` to return `None`, leading to a 0-second cooldown and rapid rotation exhaustion during 429s.

## [1.2.6] - 2026-08-26

- **Fixed**: Gemini rate limits were occasionally misclassified as daily quotas (triggering a 1-hour wait) because the natively injected Hermes guidance footer contained the text "requests/day".
- **Fixed**: Wired `dispatch_binding.py` into the modern `core.classify` engine. This restores accurate header-based waiting (e.g. for NVIDIA provider throttling) which was accidentally omitted in the 1.2.4 parity port.

## [1.2.5] — 2026-08-25

The speed update. Every version until now rotated keys at the speed of the
provider's failure — a hanging provider that takes 20 seconds to time out burned
20 seconds per key, sequentially, across the whole pool. With twelve keys that is
four minutes of dead air before the wait state even starts.

**In short**

- ⚡ *Adaptive Storm Timeout* — After two consecutive timeouts, the silence
  timeout for remaining keys is slashed to 25% (floored at 5s). A pool that
  would have burned 240s now burns ~50s.
- 🔌 *Provider Circuit Breaker* — After three consecutive timeouts from different
  keys, KAME concludes the provider is down and skips straight to the recovery
  wait state instead of burning through the rest of the pool. The user sees a
  countdown instead of silence.
- 🧵 *Non-Blocking Concurrency* — The `_SILENCE_TIMEOUT_LOCK` that serialised
  every concurrent API call (two agents blocked each other for the full timeout)
  now holds only for the microsecond of the `os.environ` write, not for the
  duration of the call itself.

## [1.2.4] — 2026-08-25

Restores full logic parity with Agent Zero v1.2.0. The initial v1.2.4 only removed the call site for the Pacific midnight bug; this update finishes the job, cleaning the dead code and addressing five other critical discrepancies found in a side-by-side audit of the Hermes port.

**In short**

- 🗑️ *Dead Code Gone* — Removed `seconds_until_pacific_midnight`, `_pacific_offset_hours`, and `looks_like_google` entirely.
- 📉 *Caps Aligned* — Per-minute escalation now caps at 5 minutes (`300.0s`), matching Agent Zero, instead of escalating indefinitely to 24 hours. The absolute horizon was also lowered from 7 days to 24 hours.
- ⏱️ *Account Bench Aligned* — Out-of-credit account benches now re-probe hourly (`3600.0s`) matching Agent Zero, down from 24 hours.
- 🔒 *Multi-Profile Safety* — `state.json` is now kept strictly per-profile. A bug that stripped profile paths and collapsed all profiles to the same file (causing races) is fixed.
- 🧵 *Thread-Safe Env* — Added a lock around the process-wide `HERMES_STREAM_READ_TIMEOUT` `os.environ` modifications to prevent concurrent agent executions from stomping each other's timeouts.

## [1.2.3] — 2026-08-24

Three symptoms reported together: the status chip looked like it was
restarting, the Settings tab looked like it was refreshing on its own, and a
number typed into **Wait for the first token** went back to 0 on Save. All
three traced to one file, `desktop-ui/plugin.js` — the backend was innocent,
and a test now says so directly rather than by absence of complaint.

**In short**

- 🐛 *Fixed* — Pressing Save no longer loses what was just typed. The "Saving…"
  paragraph that 1.2.2 added to hold the pending value on screen made the
  settings list grow by one row the instant it appeared; a **keyless** list
  reconciles by position, so React unmounted and rebuilt every setting field
  at that moment, taking the in-flight value down with it. Every list in the
  panel is keyed now, so nothing above a field can ever shift it into another
  field's old slot.
- 🐛 *Fixed* — The Settings and Events tabs no longer re-render once a second.
  The panel handed every subscriber a new snapshot object every second
  regardless of whether the file's bytes had changed, and the whole page
  re-rendered on the same clock a countdown needed on one tab only. Reads are
  now compared to the last bytes before anything is rebuilt, and only the two
  small pieces that show a countdown subscribe to the clock.
- 🐛 *Fixed* — A second `register()` call — Hermes reloading the plugin without
  disposing the old one first — could start a second one-second reader
  alongside the first. One reader can run at a time now.
- ✅ *Added* — `tests/ui_reconcile.mjs` renders the real panel with React and
  the SDK stubbed, opens every tab, and fails if any list of two or more
  children is keyless. `tests/test_v1_2_3.py` runs it from `pytest tests/`
  and separately proves the save/reset/describe path never touched the value
  — the regression this release fixes was never in that path.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Fixed

- **The remount that ate a keystroke.** `h()`, the panel's local JSX wrapper,
  drops falsy children before React ever sees the list — so a child that
  appears conditionally changes the list's length, not just its content. The
  settings cards lived in one such list. The moment Save was pressed, 1.2.2's
  own pending-value paragraph appeared above them and pushed every card down
  one slot, into the position the *previous* render had filled with an
  element of a different type. React's answer to a type change at a given
  position is to tear down that subtree and build a new one — so every
  `NumberSetting` and `FlagSetting` was destroyed and rebuilt at exactly the
  moment it was holding a value the backend had not published yet, and the
  rebuilt field read the snapshot instead, which still said 0. Read as "I
  typed 30 and it saved 0". Every variadic child list in the file — the chip,
  every card, every field, every row on every tab — now carries a stable
  `key`, so a sibling appearing or disappearing can never again relocate an
  unrelated component and destroy its state.

- **The second-long re-render.** `readSnapshot()` parsed the file and called
  `$snapshot.set()` every second whether or not the file had changed — and the
  backend only rewrites it unconditionally once every twenty seconds, so most
  of those seconds were spent re-rendering identical data. Bytes are now kept
  from the last read and compared before anything downstream is touched;
  pending-request timeouts still resolve on an unchanged read, only the
  snapshot update is skipped.

- **The clock the whole page listened to.** `KamePage` read the one-second
  `$now` atom at its top level so the Overview tab's countdown would tick,
  which meant Settings and Events re-rendered on the same clock for no reason
  of their own. The header's live/stale line and the pool countdown moved
  into two small components (`HeaderStatus`, and the existing `RightNow` /
  `PoolRow`) that read the clock themselves; `KamePage` no longer reads it at
  all.

- **A second reader.** `startReading()` had no guard against being called
  twice, so a plugin reload that skipped disposing the previous instance —
  which Hermes' own reload path can do — left two `setInterval` readers
  racing each other against the same file. A module-level guard now refuses
  to start a second one while the first is still running.

### Added

- **`tests/ui_reconcile.mjs`.** Loads `plugin.js` for real with `jsx`/`jsxs`,
  the hooks, and `window.hermesDesktop` stubbed to a realistic fixture,
  resolves function components recursively into a tree, opens all three tabs,
  and asserts every child list of two or more is fully keyed. Caught three
  remaining keyless lists on its first run — in the pool row's tally block, in
  `Field()`, and in the header — before the fix was complete.

- **`tests/test_v1_2_3.py`.** Runs the Node check from `pytest`, and carries
  four backend tests that drive `control._apply` exactly as the panel's Save
  button does — set, read back through `describe_all` twenty times in a row,
  reset, and a below-floor value refused with a sentence — all to state
  plainly that the value was never lost on that side of the file.

### Verified

`node tests/ui_reconcile.mjs` — 4/4 structural checks pass, all three tabs.
`pytest tests/test_v1_2_3.py` — 8/8 pass (one skips without `node` on PATH).

</details>

## [1.2.2] — 2026-08-24

Found by reading one real pool. A NVIDIA provider with **two** comma-separated
keys reported **three**, and the third — `key:d48bbb` in `state.json` — was the
comma-joined list itself, being offered to the provider as one long credential
and refused with a 403. The two real keys were beside it, both quarantined 401.
The pool had learned three broken things about a healthy pair of keys.

**In short**

- 🐛 *Fixed* — A pool row holding several comma-separated keys is split into
  the keys it holds before anything is sent. The joined list is never a
  candidate.
- 🐛 *Fixed* — One physical key declared in two config blocks (`providers:` and
  `custom_providers:`) counts once.
- 🐛 *Fixed* — A key edited out of the config leaves the pool instead of being
  retried, refused and counted as broken for ever.
- 🐛 *Fixed* — Saving a setting from the panel no longer makes the value flick
  back to the old one and forward again. One save is one movement.
- ✨ *Added* — The panel says when `config.yaml` has been edited since Hermes
  started, and that a restart is what applies it.
- ✏️ *Changed* — The silent-stream timeout is titled **Wait for the first
  token**. The setting name is untouched.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Fixed

- **The comma-joined list is no longer a credential.** `candidates()` split the
  agent's own `api_key` when the host's pool handed back nothing at all, and
  only then. A Hermes pool holding one row whose value is `k1,k2` is not
  nothing, so the split never ran and the row went out whole. Every raw value
  is now split, and a value that yields more than one part is replaced by its
  parts — on every path, from `providers:`, from `custom_providers:`, and from
  a key the agent carries itself. Nothing downstream of `candidates()` can see
  a comma.

- **One key, one row.** The same split pass dedupes by key text, so a
  credential written into two config blocks is one entry in the pool with one
  health record, rather than two rows that quarantine separately and make a
  pair of keys read as three.

- **The pool retires what the config no longer declares.** `Carousel` only ever
  added keys; the single thing that removed anything was `forget()`, which
  drops a whole identity. A key replaced in Settings therefore stayed in the
  pool, kept its last refusal, and went on being counted — a panel reading
  "2/3 healthy" for a pool of two. `select()` now mirrors the candidate list:
  a row nothing has offered for five minutes is dropped, with its ledger
  history left where escalation keeps it. Five minutes rather than
  immediately, because one identity can be reached by two agents with
  different lists, and dropping a row the moment one of them looks away would
  erase a cooldown the other one earned. An empty candidate list mirrors
  nothing at all — a host failing to load a pool for one call is not evidence
  that every key was deleted.

- **One save is one movement.** The panel's number field handed itself back to
  the snapshot the instant Save was pressed, and the snapshot had not been
  written yet — so the value fell back to the old number for a tick or two and
  then jumped to the new one. Read as a setting changing itself twice. A
  control now holds what was written until the backend reports that value, or
  until it is clear the write did not land. Switches do the same.

### Added

- **"config.yaml has been edited since Hermes started."** KAME reads the config
  once, at registration, because `ctx.get_config` re-parses the file on every
  access and these settings sit on the classification and selection paths.
  That is still the right trade, and it left a person with no way to tell an
  edit that was wrong from an edit that was merely not in force yet. The file
  is now re-read off the hot path — on the snapshot the panel already takes,
  throttled to once every fifteen seconds — and any setting whose entry has
  changed is named on the Settings screen, with what to do about it. A setting
  the environment owns is never named: restarting would not apply that edit
  either.

### Changed

- **"Wait for the first token."** The title only; `stream_silence_timeout_seconds`,
  `KAME_STREAM_SILENCE_TIMEOUT` and the deprecated `silent_stream_patience_seconds`
  all still answer to their own names. "Give up on a silent key after" named
  the mechanism; this names the decision.

### Tests

- **A test that could fail for something KAME never did.** Two tests in
  `test_v1_1_3.py` proved "no wait happened" by recording
  `dispatch_binding.time.sleep` — and that attribute *is* the stdlib module, so
  the recorder was installed process-wide. `register()` starts a state
  heartbeat, a daemon looping on `time.sleep`, which never stops; a full suite
  ends with two of them alive, because two test files load the plugin under
  different package names. A heartbeat tick landing inside the recorder's
  window put a number in a list that was asserted empty, which is why the
  failure appeared in one full run and not in the next one over identical code,
  and never at all when the file ran alone. Both tests now use
  `DispatchBinding(sleep=...)`, the injection point the binding has exposed
  since 1.0.1. No shipped code changed: in Hermes, `register()` runs once,
  against one module instance, with one heartbeat.

### Considered and rejected

- **A sixty-second cap on a sole credential's cooldown.** The host has one
  (`EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS`), and mirroring it would undo 1.1.3.
  A cooldown that came from the provider's own words — a daily quota, an auth
  refusal, a `Retry-After` — binds whatever else is in the pool, and retrying
  it once a minute for an afternoon spends the quota it is waiting for. The
  cooldowns a pool of one genuinely makes meaningless are the ones that exist
  only to move the next call elsewhere, and
  `dispatch_binding._rest_unless_it_is_the_only_one` has dropped exactly those
  — and dropped them to nothing, not to sixty seconds — since 1.1.3.

- **Refusing to record an outcome for a key no `select()` ever offered.** It
  reads like a safety rule and protects nothing: `select()` only ever chooses
  from the candidates it was handed, so a row nothing declares is unreachable
  whatever the pool holds, and `_mirror` retires it. What the rule did do was
  silence 1.1.3's `_rest_unless_it_is_the_only_one`, which marks a key the
  moment a stream drops.

</details>

---

## [1.2.1] — 2026-08-24

SDK-wrapped streaming read timeouts (e.g. `Gemini streaming request failed: The read operation timed out`) are now classified as non-terminal timeouts, rotating to the next key or continuing the stream instead of ending the turn.

**In short**

- 🐛 *Fixed* — Gemini streaming read operation timeouts are recognized as non-terminal timeouts and rotated transparently.

---

## [1.2.0] — 2026-08-23

The release where the two ports carry the same number again. Nothing in the
rotation loop changed; what changed is what a person sees the first time they
open the settings screen.

**In short**

- ✏️ *Changed* — The settings are on three labelled shelves instead of in one list.
- ✏️ *Changed* — A setting the table has never heard of is shown under "Other", never dropped.
- ✏️ *Changed* — The silent-key timeout explains itself to the person reading it.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Changed
- **The settings are on three labelled shelves instead of in one list.** Twelve
  names in one column read as twelve equal knobs, and they are not equal. One
  of them adds a behaviour and is off until somebody turns it on; two are
  numbers whose defaults are already right; the other nine take something away
  and exist so that a person who suspects KAME of breaking their agent can
  prove it with one switch. The screen now says which is which, in a sentence
  per shelf:
  - **Optional** — the silent-key timeout, alone, because it is the only
    setting here that a normal install is *expected* to leave off.
  - **Tuning** — the daily cooldown and the resume budget. Safe to change,
    rarely worth changing.
  - **Turn parts of KAME off** — every `*_disabled` switch, named for what it
    gives back to Hermes and marked as a diagnostic rather than a preference.

  The shelves live in `settings.GROUPS` and reach the panel through the
  snapshot, so the grouping and the settings it describes cannot drift apart,
  and `/kame get` prints the same three headings with the same sentences.

- **A setting the table has never heard of is shown under "Other", never
  dropped.** The failure mode of every hand-kept order list is a knob added
  later that quietly stops appearing. It has to land in the wrong place rather
  than nowhere.

- **The silent-key timeout explains itself to the person reading it**, not to
  the person who wrote it: what the wait actually covers (the first character
  and every character after it), what zero means (Hermes' own 120 seconds and
  nothing else), and why a value under five seconds is raised.

- Snapshot schema 3: `setting_groups` is new, and the counters carry
  `tool_call_cuts` from 1.1.3. The panel and `state.py` move together, as
  always — the panel refuses a document it does not understand.

</details>

## [1.1.3] — 2026-08-23

Two defects with the same shape, both found by reading the loop rather than by
anything failing: KAME doing something reasonable-looking on a path nobody had
measured.

**In short**

- 🔧 *Fixed* — A key is no longer rested when it is the only one that is well.
- 🔧 *Fixed* — A stream that stops inside a tool call is no longer recorded as an answer.
- ✏️ *Changed* — The settings panel leads with the numbers.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Fixed
- **A key is no longer rested when it is the only one that is well.** This
  plugin's cooldowns come in two kinds, and until now they were treated alike.
  Some are the provider's own words — a `Retry-After`, a daily quota, an auth
  refusal — and those bind whatever else is in the pool, because sending again
  immediately would only collect the same refusal. Others exist for exactly one
  reason: to make the *next* selection pick a different key. The thirty seconds
  a key serves after cutting a stream is one of those.

  A rest of the second kind is meaningless when there is no different key to
  pick. The carousel found nothing usable, waited the full thirty seconds, and
  then continued the answer on the same key it could have used immediately — so
  a single-key install paid half a minute for a rotation that never happened.
  It is now applied only while another key is healthy. The failure is still
  recorded either way: `failures`, `last_sick_at` and `kind` are written on both
  paths, so a key that keeps dropping streams keeps looking like one, and only
  the sentence is dropped.

- **A stream that stops inside a tool call is no longer recorded as an
  answer.** Half a JSON payload is not something a second model call can be
  asked to finish, so KAME refuses to continue one — that part was right and is
  unchanged. What was wrong is what happened next: the stub went back through
  the *success* path, so the key that did it was marked healthy, its failure
  count never moved, nothing appeared on the Events screen, and the next
  selection treated it as the freshest key in the pool. A key that could not
  hold a long stream was invisible for exactly as long as it kept failing that
  way.

  It now records a `stream_drop` naming the tool that was being called, counts
  it as `tool_call_cuts` — separately from `mid_stream_cuts`, because these end
  the turn whatever KAME does — and rests the key under the rule above. The
  stub itself still goes back untouched, and text delivered by an earlier key
  still comes back with it.

### Changed
- **The settings panel leads with the numbers.** Every switch KAME has is named
  `*_disabled`: they are escape hatches, read once when something has gone wrong
  and then left alone for months. The numbers are the part that gets tuned, and
  nine switches stood between the top of the panel and all three of them. The
  silence timeout leads, because it is the one number whose wrong value is felt
  on every call in both directions. A setting the order list has never heard of
  is shown after the ones it knows, rather than not at all.

</details>

## [1.1.2] — 2026-08-23

The first release of 1.1.1 into real use found the one provider that will not
be handed its own turn back, and the failure was the worst kind: the feature
that exists so the user never sees a broken answer was ending the turn instead.

**In short**

- 🔧 *Fixed* — Gemini refuses a prefilled continuation, and that no longer breaks the turn.
- 🔧 *Fixed* — A continuation that cannot be made to work never costs the answer.
- 🔧 *Fixed* — And neither does any other way out of the carousel.
- 🔧 *Fixed* — The manifest declares a version Hermes' installer will actually accept.
- 🛠️ *Tooling* — `tools/deploy.py` has a second road, and verifies what it wrote.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Fixed
- **Gemini refuses a prefilled continuation, and that no longer breaks the
  turn.** 1.1.1 continued a cut answer by appending the text so far as a
  trailing assistant message — the mechanism every OpenAI-compatible endpoint
  and the Anthropic Messages API understand. Gemini's native API answers it
  with `HTTP 400 (INVALID_ARGUMENT): Requests ending with a model turn are not
  supported`, and a 400 is a refusal of the *request*, so the carousel
  surfaced it and the answer the user had already started reading became a
  failed turn.

  The continuation now has two shapes: the prefill, and the same partial answer
  followed by a short user turn asking the model to carry on. The second is
  used only after a provider has refused the first, in its own words — never
  from the provider's name. The plugin took a provider allowlist back out in
  0.0.3 for the same reason: identity is not evidence.

  The refusal is remembered for the life of the process, so it costs one
  request once rather than one per cut answer.
- **A continuation that cannot be made to work never costs the answer.**
  Whatever a resumed attempt runs into — the refusal above, an unknown
  parameter, anything terminal — the text the user has already read is
  returned as the answer so far and Hermes carries on from it. Nothing KAME
  does to recover an answer may ever end worse than not trying.
- **And neither does any other way out of the carousel.** The rule above was
  written for the exit that had just failed; reading the rest of the loop
  found four more that predate it. The user pressing stop, every key going to
  rest with no recovery to wait for, the pool agreeing the request is at
  fault, and there being no key to pick at all each either raised the last
  error or handed the call back to Hermes. Once text is on screen both are
  wrong: raising throws away an answer the user is reading, and letting Hermes
  make the call itself prints that answer a second time, because its request
  carries no record of what was already delivered. All five exits now return
  the answer so far. With nothing on screen, every one of them behaves exactly
  as it did.
- **The manifest declares a version Hermes' installer will actually accept.**
  `manifest_version` went to 2 in 1.0.9 for the marketplace metadata, and
  Hermes' two halves disagree about that number: the loader
  (`hermes_cli/plugins.py`) understands 2, while the installer behind
  `hermes plugins install` and the Desktop plugin dashboard
  (`hermes_cli/plugins_cmd.py`) understands 1 and *raises* on anything higher.
  Installing from a repository — the one thing a published plugin has to do —
  failed with `requires manifest_version 2, but this installer only supports
  up to 1`; a file copy never met the gate, which is why nothing showed it
  until now. The declaration is 1 again, and nothing is lost by it: licence,
  homepage, tags, `api_version` and `config_schema` are read at either
  version. `tools/host_assumptions.py` now watches the installer's own
  constant and fails when it catches up, which is when the number may rise.

### Tooling
- **`tools/deploy.py` has a second road, and verifies what it wrote.** The
  probe that refuses to deploy from inside an app container was right and left
  the machine undeployable for as long as the redirection lasted — on this one
  it covers every shell and interpreter at once, so there was no fallback. The
  redirection is a filter on a *path*, so the same directory reached through
  the local administrative share (`\\localhost\C$\...`) is the real one; the
  script now probes that route the same way and uses it only when a write
  there provably does not appear in any package's `LocalCache`. If both roads
  are redirected the refusal stands, unchanged. The copy is then verified file
  by file by digest, through whichever route it was written with — the check
  that would have caught the 1.0.8 deploy that never ran.

</details>

## [1.1.1] — 2026-08-22

1.1.0 could show things. This release can be *used*: every switch is a switch,
every number is a field, the panel says why something happened, and the one
thing the user still saw go wrong — an answer stopping in the middle of a
sentence — stops happening.

**In short**

- ➕ *Added* — The stream seam.
- ➕ *Added* — A settings editor in the panel.
- ➕ *Added* — The path back.
- ➕ *Added* — An Events screen.
- ➕ *Added* — A first-run state.
- ✏️ *Changed* — The status-bar chip names the pool, not the model.
- ✏️ *Changed* — The sidebar row is `KAME API Rotation`.
- ✏️ *Changed* — `silent_stream_patience_seconds` is now `stream_silence_timeout_seconds`.
- ✏️ *Changed* — An invalid key says so.
- 🔧 *Fixed* — `api_version` is an integer.
- 🔧 *Fixed* — Uninstalling removes the Desktop half.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Added
- **The stream seam.** When a provider closes the stream mid-answer, Hermes
  does not raise: it **returns** a stub (`partial-stream-stub`,
  `finish_reason=length`), and the conversation loop papers over it by
  appending `[System: The previous response was cut off...]` and asking the
  model to carry on — visible, and the cause of rewind and edit refusing later
  in the same session, because that synthetic row moves the server's message
  ordinal without moving the client's. KAME now continues the answer itself,
  on another key: the text already delivered is sent back as a trailing
  assistant message, and whatever the model repeats of it is trimmed off the
  stream before it reaches the screen. What Hermes gets back is one continuous
  response, so it has nothing to explain.

  The trimming is the whole difficulty and it is a separate, pure module
  (`core/stitch.py`) so it can be tested without a provider. Three shapes are
  handled: a continuation repeating the last few words, a model that **starts
  the entire answer again** from the first word, and one that picks up
  mid-word and repeats nothing. Matching ignores whitespace and case — a model
  resuming rarely reproduces the original spacing — and maps every conclusion
  back to the original string, so the text kept keeps its own spacing. Nothing
  is ever dropped that has not been matched character for character against
  what the user already saw.

  Bounded and reversible: `stream_resume_limit` (3 by default, 0–10) caps the
  resumes in one turn, `stream_stitch_disabled` turns the whole thing off, and
  a drop that happened while a **tool call's arguments** were being written is
  never continued — half-written JSON is not something a second model call can
  be asked to finish, and Hermes tags that case (`_dropped_tool_names`) which
  is exactly the discriminator KAME reads. `stitched`, `resumes` and
  `stream_drops` are counted separately from `mid_stream_cuts`, which keeps
  its old meaning: a cut the user actually saw.
- **A settings editor in the panel.** Every flag is a switch and every number
  is a field, with its range, its unit, its one-sentence explanation, and
  where the value came from (default / environment / config) shown beside it.
  Per-setting **Reset**, **Reset to defaults**, **Clear pool** — with the
  tooltip that says what it does *not* do: *only clears rotation and
  quarantine; it does not delete any key* — and a confirmation on the two
  switches that stop KAME doing its job.

  A durable change is written to Hermes' own `.env`, and **only `KAME_*`
  lines**: the file is rewritten with every other byte intact, a duplicate
  assignment of the same variable is collapsed rather than left to shadow the
  new one, and nothing from that file is ever logged. The process environment
  is set first, so a change that cannot be persisted is still in force for
  this session and says so.
- **The path back.** The panel writes `control.json` next to the snapshot and
  the Python half applies it on a one-second heartbeat, then reports the
  outcome in the next snapshot. The action list is closed (`set`, `reset`,
  `reset_all`, `clear_pool`, `clear_events`), the setting name has to be one
  KAME knows, and the value goes through the same validator `/kame set` uses.
  **No action reads, writes or names a key.**
- **An Events screen.** The last fifty decisions — rotation, quarantine,
  invalid key, 503 storm, cut answer, continuation — with the time, the key's
  fingerprint, a short reason and the status code. Also `/kame events` in the
  chat. Not a log copy: no provider error text is kept, because a provider can
  quote the request back inside one, and the request can be the user's prompt.
- **A first-run state.** A fresh install shows what to do (paste several keys
  into one provider field, separated by commas; or `/kame-keys`) instead of a
  wall of zeroes.

### Changed
- **The status-bar chip names the pool, not the model.** `gemini:gemini-3.7-flash 14/15`
  rather than `KAME 14/15`: two pools of two different keys were rendering as
  one number. The pool being called leads, other pools follow by how recently
  they were used, the third and beyond collapse into `+N`, a pool nothing has
  touched in ten minutes is left off, and the ETA appears only when one
  exists. A long model name truncates with the whole picture in the tooltip,
  and the chip is width-capped so it can never push the status bar around.
- **The sidebar row is `KAME API Rotation`.** "KAME" alone said nothing to
  anyone who had not installed it on purpose.
- **`silent_stream_patience_seconds` is now `stream_silence_timeout_seconds`**
  (`KAME_STREAM_SILENCE_TIMEOUT`). It read like a virtue rather than a
  timeout. The old name and the old variable still work, in the config file
  and in the environment, and at `/kame set` and in the panel, for ever — a
  setting somebody wrote a year ago must not become a silent no-op because a
  maintainer preferred a different word.
- **An invalid key says so.** A credential the provider refuses as invalid is
  reported as *replace this key* — in the log, in the events, and as a red dot
  on the chip — instead of an hourly quarantine that comes round again for
  ever.

### Fixed
- **`api_version` is an integer.** The manifest declared `"0.20"`, which
  Hermes logged as `api_version '0.20' is not an integer; ignoring` on every
  boot. It is the plugin API generation, not the plugin's version, and every
  bundled plugin declares `1`.
- **Uninstalling removes the Desktop half.** `install()` copies the UI file
  into `desktop-plugins/`, a directory Hermes never associates with this
  plugin, so removing the plugin left a sidebar row, a chip and a page reading
  a snapshot nothing was writing. It is now taken back out through
  `ctx.on_unload`, and only ever its own file and its own empty directory.

</details>

## [1.1.0] — 2026-08-21

1.0.10 put the status line where the renderer would keep it, and stopped there.
Everything else it added was written for a surface that does not exist: `/kame`
was markdown, and a plugin command's reply is painted with `pretty={false}`, so
the headings and the table pipes arrived as themselves. This release stops
guessing at a panel and ships one.

**In short**

- ➕ *Added* — A real panel, in the app's own language.
- ➕ *Added* — The snapshot behind it.
- ➕ *Added* — The install is the install.
- 🔧 *Fixed* — "Response truncated due to output length limit" on turns nowhere near a length limit.
- 🔧 *Fixed* — `/kame` is plain text.
- 🔧 *Fixed* — The version reads as newer than the one before it.
- ✏️ *Changed* — `tools/deploy.py` proves it is writing to the real Hermes rather than assuming it from where the interpreter lives.
- ➕ *Added* — 4 new host tripwires, 22 in all.
- ➕ *Added* — 49 new tests.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Added
- **A real panel, in the app's own language.** The package now carries a
  Desktop UI plugin (`desktop-ui/plugin.js`), loaded by the renderer's runtime
  door and built out of the app's own SDK — `StatusDot`, `Tip`, the `--ui-*`
  tokens, the same `Codicon` sidebar rows core uses:
  - a **status-bar chip**, on screen at all times and not only mid-turn, that
    shows pool health (`KAME 14/15`) and, whenever nothing is usable, the
    countdown to the first key coming back;
  - a **`/kame` page** with a row in the sidebar and an entry in the command
    palette: what KAME is doing this second, one bar per `provider:model`
    pool, this process's counters, the Gemini repair's state, and every
    setting with where its value came from.

  The countdown is recomputed against the snapshot's own age on every tick, so
  it moves every second rather than jumping every twenty. A reading older than
  75 seconds is presented as stale, with its age, rather than as truth — the
  one failure a poller cannot otherwise see is a backend that died mid-turn
  leaving a snapshot that still looks live.
- **The snapshot behind it** (`state.py`). A small JSON document under
  `plugin-data/hermes-kame-api-rotation/`, written atomically, carrying
  fingerprints and counts and **no key material of any kind**. A file rather
  than `plugin_api.py`, because that door needs `dashboard/plugin.json`
  discovery, a `plugins.enabled` entry and a restart, and this is a status
  readout — the exact thing a file is good at. Published on every rotation,
  every wait, every recovery, and on a 20-second heartbeat so an idle pool
  still proves it is alive.
- **The install is the install.** The UI file ships inside the package at
  `desktop-ui/plugin.js` — a path neither runtime door scans — and the Python
  half copies it to `desktop-plugins/hermes-kame-api-rotation/plugin.js` when
  it registers. That door loads default-on; the unified one
  (`plugins/<name>/desktop/plugin.js`) caps `defaultEnabled` to false to match
  the Python half's installed-but-inert posture, and a status chip nobody can
  see until they find a toggle is not a status chip. One copy, no toggle, and
  an upgrade refreshes it because the write happens whenever the bytes differ.

### Fixed
- **"Response truncated due to output length limit" on turns nowhere near a
  length limit.** Not a token cap, and not KAME's rotation: Gemini's stream
  translator keys a tool-call slot on part index, name and thought signature,
  and two parallel calls to the same tool arrive as the same part index, under
  the same name, with no signature. They share one slot, so
  `{"query":"A"}` and `{"query":"B"}` are concatenated into a string that
  parses as neither; `message_sanitization` cannot repair it and substitutes
  `{}`; Hermes reads the empty call as truncated, retries four times, and
  reports a length limit. KAME now repairs the stream on the way past
  (`gemini_slots.py`), with the narrowest rule that can describe the
  corruption — a delta is moved to its own index and its own id **only** when
  the text already accumulated and the text arriving are each, on their own, a
  complete JSON object, which a genuine fragment can never be.

  It patches nothing until it has proved all of it at runtime: the setting is
  on, the adapter imports, the function is there and unpatched, its signature
  is what KAME read, its source still contains the merge's markers, and a
  self-check reproduces the merge on a synthetic two-call stream and shows the
  repair separating it. Any one of those failing leaves the host untouched and
  says why in `/kame`. `KAME_GEMINI_TOOL_CALL_FIX_DISABLED=1` turns it off.
- **`/kame` is plain text.** Named sections and one `label: value` per line —
  not markdown, and not padded columns either: the row renders in a
  proportional face, so padding produces a table that does not line up. The
  panel that earns columns is the page.
- **The version reads as newer than the one before it.** 1.0.10 was a correct
  version and a bad string: to the person reading it in a panel it looked
  older than 1.0.9. This is 1.1.0.

### Changed
- **`tools/deploy.py` proves it is writing to the real Hermes rather than
  assuming it from where the interpreter lives.** It writes a uniquely named
  probe into the Hermes home and looks for that name under every app
  container's `LocalCache`; if it turns up there, the deploy is refused. The
  old rule (the interpreter must live under the Hermes directory) was sound
  but too narrow — it refused interpreters that were demonstrably writing to
  the real tree. The deploy also installs the Desktop half.

### Added — checks
- **4 new host tripwires, 22 in all.** The slash reply is still rendered with
  `pretty={false}` (the day it is not, markdown becomes available again); the
  standalone desktop-plugin door is still default-on while the unified one
  still caps it; the SDK still exports every name the chip imports, and the
  chip still imports nothing the loader would refuse; and Gemini's adapter
  still merges parallel tool calls — if the host fixes its own bug, the patch
  should be removed, and that is where it will show up.
- **49 new tests**, one class per promise: the panel is text, the snapshot
  carries no key material and is not rewritten when nothing changed, the
  repair fires on the corruption and never on a genuine fragment, the Desktop
  half's contract is checkable without a renderer, and the version says one
  thing everywhere it is written down.

### Test results
- 1252/1252 tests passing.
- 22/22 host assumptions holding against the installed Hermes.
- The Gemini repair's self-check run against the installed adapter: the merge
  reproduced, the repair separated the two calls, patch applied.

</details>

## [1.0.10] — 2026-08-21

Everything v1.0.9 added was invisible, and one thing it changed made the
symptom it was chasing worse. This release is that, corrected, after reading
the parts of the host v1.0.9 had only reasoned about.

**In short**

- 🔧 *Fixed* — The status line never appeared, and was blanking the host's.
- 🔧 *Fixed* — `HERMES_STREAM_RETRIES` is no longer touched — and this was making the mid-stream cuts worse.
- ✏️ *Changed* — `/kame` is markdown.
- ➕ *Added* — Two host tripwires, eighteen in all.
- ➕ *Added* — 6 new tests.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Fixed
- **The status line never appeared, and was blanking the host's.** Desktop
  does not render every `thinking.delta` it receives. Each one goes through
  `providerWaitText` (`apps/desktop/src/store/provider-wait.ts`), which keeps
  the text only if it opens with ⏳/⚠/↻ followed by `waiting on`, `no output`,
  `no response` or `model returned`, and passes **the empty string** on for
  anything else — clearing the row rather than leaving it alone. So
  `KAME API Rotation: 15/15 healthy`, sent every ten seconds, was not merely
  unseen: it wiped the core's own explanation each time it went out. KAME now
  speaks the row's language, in the same shape the core uses
  (`agent/chat_completion_helpers.py:1631`):
  - `⏳ waiting on gemini-2.5-pro — KAME 15/15 keys healthy`
  - `↻ waiting on gemini-2.5-pro — KAME 12/15 keys healthy, on key 3`
  - `⏳ waiting on a key to come back — KAME 0/15 keys healthy, next key in 1m 23s (around 14:32:07)`
  - `↻ model returned — KAME 15/15 keys healthy, back after 4m12s`

  It lands in the row that already carries the elapsed-seconds timer
  (`ResponseLoadingIndicator`, and `TurnActivityIndicator` for the quiet gaps
  mid-turn), which is where it was asked for.
- **`HERMES_STREAM_RETRIES` is no longer touched — and this was making the
  mid-stream cuts worse.** v1.0.9 read the name and assumed Agent Zero's bug:
  retries against a key already known to be spent. Hermes' variable is a
  different thing wearing the same word. It is consulted once
  (`agent/chat_completion_helpers.py:4693`) and acted on only in the
  *transient network* branch — timeout, dropped connection, SSE parse error,
  empty stream — where the repair is a fresh socket to the same endpoint. A
  429 or a 401 arrives as an `APIStatusError`, never enters that branch, and
  already reached KAME on the first try. So setting it to 0 bought nothing on
  the failures the carousel exists for, and spent the one recovery that was
  free and invisible: a blip mid-answer ended the stream, and Hermes continued
  it with the synthetic row the user can read —
  `[System: The previous response was cut off by a network error mid-stream…]`.
  `host_tuning.py` is deleted, `host_retry_suppression_disabled` with it, and
  a tripwire now fails if any assignment to a `HERMES_STREAM_*` variable comes
  back.

### Changed
- **`/kame` is markdown.** A plugin slash command returns a string and nothing
  else (`hermes_cli/plugins.py:2106`) — there is no panel API to reach for, so
  the panel is a well-set page: headings, a counters table, a pool table with
  the ETA per `provider:model`, and a host-variables table that now says KAME
  sets none of them. Desktop renders it; the CLI shows the plain text it
  already was.

### Added
- **Two host tripwires, eighteen in all.** One reads the installed Desktop
  source, rebuilds `providerWaitText`'s gate from it, and runs every line KAME
  can produce through it — including a check that the gate still rejects what
  v1.0.9 sent, so a copy that drifted into accepting everything would be
  caught. The other reads the shipped plugin and fails if it ever writes one
  of the host's stream variables again.
- **6 new tests** — the gate, the four lines that must pass it, the model
  label, and the deleted tuning staying deleted.

### Test results
- 1203/1203 tests passing.
- 18/18 host assumptions holding against the installed Hermes.

</details>

## [1.0.9] — 2026-08-21

The release that went looking for the causes rather than the symptoms. Three
separate complaints — "it stops for no reason", "it freezes", "rewind stopped
working" — turned out to be four distinct host-level facts, and every fix
below is named after the one it answers.

**In short**

- 🔧 *Fixed* — The 410 loop.
- 🔧 *Fixed* — Rotating into Hermes' own circuit breaker.
- 🔧 *Fixed* — The silent gap between 90 seconds and ten minutes.
- ➕ *Added* — The live status line, always on.
- ➕ *Added* — A cadence instead of a pulse.
- ➕ *Added* — `/kame` — the panel that did not exist.
- ➕ *Added* — A mid-stream cut counter, and what it explains.
- ➕ *Added* — `on_session_reset`.
- ➕ *Added* — Three new host tripwires.
- ➕ *Added* — 84 new tests.
- ✏️ *Changed* — Speed: `HERMES_STREAM_RETRIES` is claimed and set to 0.
- ✏️ *Changed* — Unanimity is evidence about the request.
- ✏️ *Changed* — Bind by signature now pins the parameter that matters.
- ✏️ *Changed* — `_Spinner` is locked, and keyed per conversation.
- ✏️ *Changed* — `/kame` says whose numbers it is showing.
- ✏️ *Changed* — `deploy.py` refuses to copy from the wrong interpreter.
- ✏️ *Changed* — `first_token_patience_seconds` is now `silent_stream_patience_seconds`,.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Fixed
- **The 410 loop.** `_TERMINAL_STATUS` did not include HTTP 410, so an
  endpoint that no longer exists read as a bad moment rather than a bad
  request, and the carousel rotated the entire pool against it and started
  over. Observed 46 times in one sixteen-minute window against
  `nvidia:z-ai/glm-5.2`. 410 is now terminal, alongside the eight statuses
  that were already there.
- **Rotating into Hermes' own circuit breaker.** `_check_stale_giveup` raises
  once `_consecutive_stale_streams` reaches `HERMES_STREAM_STALE_GIVEUP` (5),
  and it raises *before* a request goes out. That counter lives on the agent,
  so it is per session and not per key: once tripped, KAME would spend fifteen
  keys in milliseconds without a packet leaving the machine, and spend them
  again on the next turn. Hermes' own comment cites the case that motivated
  it — 494 consecutive failures over three days. Fixed twice over: the streak
  is cleared on every rotation, which is exactly what the host itself does on
  a provider swap; and if the breaker fires anyway its message classifies as
  `host_breaker`, ahead of the auth reading of the same text, and is terminal.
- **The silent gap between 90 seconds and ten minutes.** The Vigil said
  something at `VIGIL_FIRST_S` (90 s) and then not again until
  `VIGIL_REPEAT_S` (600 s). Nothing was frozen; nothing was being said. This
  is the "freeze" in every screenshot of a stuck spinner.

### Added
- **The live status line, always on.** `KAME API Rotation: 12/15 healthy`,
  and while the pool is resting, `— waiting — next key in 1m 23s (around
  14:32:07)`. Same sentence Agent Zero's spinner uses, deliberately: two ports
  of one plugin that describe themselves differently are two plugins to the
  person reading the screen. It rides `thinking.delta`, the transient channel
  that creates no message and moves no ordinal, so it cannot break rewind,
  edit, or resend.
- **A cadence instead of a pulse.** The wait itself has always been
  ETA-driven and still is; only the *drawing* is paced, and now by how fast
  the number is changing: 30 s while the wait is long, 5 s under a minute,
  1 s in the final ten seconds. About 120 frames in an hour where a
  one-second tick would have been 3600. The diff gate means a settled line
  costs nothing at all — with one deliberate exception: the spinner is shared,
  and once Hermes writes its own activity into it ("Exploring 6 files") KAME's
  line is off the screen while the gate still believes it is showing. So an
  unchanged line is allowed through again after 30 s. Two frames a minute at
  the very worst, and "is it still rotating?" stops depending on whether the
  host happened to overwrite the answer.
- **`/kame` — the panel that did not exist.** Hermes parses `config_schema`
  and never renders it, so every switch this plugin has was real, documented
  and unreachable without hand-editing YAML. `/kame` shows the live rotation,
  pool health per provider and model with the ETA, the counters, and the four
  host variables KAME depends on. `/kame get` lists every setting with its
  value *and its provenance* — environment, config, or default — because "off
  because a file says so" and "off because nothing mentions it" are the same
  word and two different problems. `/kame set <key> <value>` writes a `KAME_*`
  line into Hermes' own `.env`, which makes it live immediately and permanent
  at once. The write is surgical: only `KAME_*` lines are ever matched, every
  other line is copied through byte for byte, and nothing read is logged.
- **A mid-stream cut counter, and what it explains.** Continuing a cut-off
  answer makes Hermes append a synthetic user row tagged
  `_length_continuation_nudge`; the server counts it, the client never renders
  it, and `_reconcile_client_ordinal` then refuses the rewind or edit whose
  ordinal no longer matches — error 4030, with the drift growing across a
  session (5 vs 4, then 17 vs 44, then 20 vs 52, then 15 vs 60 in the logs).
  That arithmetic belongs to the host and KAME will not rewrite a history it
  does not own. What it can do is be the only thing in the process that sees
  the cause happen, so it counts them and `/kame` says so, and names the lever
  (`HERMES_STREAM_STALE_TIMEOUT`).
- **`on_session_reset`.** A reset clears the conversation, not the calendar.
  The status line describes a chat that no longer exists, so it is forgotten —
  and only for the session named in the payload, never for the others this
  process is serving. Cooldowns are deliberately kept: a key benched until
  midnight is still benched at 00:01, and clearing them would walk the whole
  pool back into the wall it just learned about, one request at a time. The
  storm filter is kept for the same reason and one more — it describes a
  provider outage rather than a chat, and it guards the log, which is one file
  per process that a reset does not clear either.
- **Three new host tripwires** in `tools/host_assumptions.py`, one per
  load-bearing fact this release rests on: the give-up counter is per session
  and not per key, the host retries the same key before KAME sees a failure,
  and a cut answer still appends a row the client never sees. Sixteen host
  facts now checked, each sabotaged first to prove the check is not vacuous.
- **84 new tests** (`test_v1_0_9.py`), one class per refusal, including one
  class for the thing a plugin in a multi-conversation process gets wrong.

### Changed
- **Speed: `HERMES_STREAM_RETRIES` is claimed and set to 0.** The host's
  default is two retries on the *same* key before the exception reaches the
  wrapper, so a dead key cost three round trips before the carousel was
  allowed to move. Agent Zero measured this class of delay at 30-40 s per
  failure and fixed it in its own 1.0.4; Hermes has the identical knob and had
  no equivalent fix. New module `host_tuning.py` claims it — and only where
  the variable is unset, because a value somebody chose outranks one KAME
  assumed — and restores exactly what was there on unload.
- **Unanimity is evidence about the request.** When every key in the pool
  answers with the identical `(kind, status)` and not one of them answered at
  all, the failure is promoted to terminal instead of being rotated through
  again. Never for the kinds a key can cause on its own — `server`,
  `timeout`, `per_minute`, `daily`, `insufficient_quota`, `auth` — because
  fifteen keys out of quota is a bad afternoon, not a bad request, and
  promoting that would throw away the wait this plugin exists for.
- **Bind by signature now pins the parameter that matters.** 1.0.8 checked
  only the argument count. It now requires the second parameter to still be
  named `api_kwargs` — the dict the wrapper rewrites — and steps aside with
  one line if it is not. `agent` is deliberately not pinned: it is a generic
  word whose rename would not mean the contract had moved.
- **`_Spinner` is locked, and keyed per conversation.** One Hermes process
  serves every open chat, the auxiliary lane and every subagent through one
  binding, so a status line kept in a single pair of class attributes is a
  status line every conversation shares. Two consequences, both real: without
  a lock, two lanes interleave a read and a write and one update is silently
  lost; and with one shared pair, a conversation rotating and a conversation
  sitting idle take turns overwriting each other's throttle, and each one
  suppresses the other's line as a repeat of a sentence it never showed. The
  state is now an `OrderedDict` keyed by `agent.session_id`, with agents that
  carry none told apart by identity, and the oldest 64 conversations kept —
  an evicted one simply looks new and redraws. The emit happens outside the
  lock, because holding one across the host's own callback is how one slow
  lane stops every other one.
- **`/kame` says whose numbers it is showing.** The counters are the binding's
  and the binding is one object for the whole process, so the panel now labels
  them "in this Hermes process, across every conversation it is serving", the
  way `/kame-quota` already did. `/kame set` says the same about settings,
  which are process-wide on purpose: a pool shared between conversations needs
  rules shared between them too.
- **`deploy.py` refuses to copy from the wrong interpreter.** 1.0.8 was
  deployed on 20 August and never ran: the interpreter used was a Store /
  PythonManager build whose `AppData/Local` is redirected into a per-package
  `LocalCache`, so the copy landed in a private shadow Hermes never reads.
  The tool now exits rather than copy unless `sys.executable` lives under the
  Hermes home, and prints the restart reminder on success.
- **`first_token_patience_seconds` is now `silent_stream_patience_seconds`,**
  because the implementation turned out to be Hermes' own
  `HERMES_STREAM_READ_TIMEOUT` rather than a KAME watchdog aborting a call it
  does not own — which also makes it cover a silent stretch *mid*-stream, safe
  by a different route since `progress.any` hands such a failure back to
  Hermes instead of rotating. Still off by default; a floor of 5 s applies
  above zero.

### Settings
Three new, all off by default: `live_status_disabled`,
`host_retry_suppression_disabled`, `silent_stream_patience_seconds`.

### Test results
- 1200/1200 tests passing (1116 existing + 84 new).
- 16/16 host assumptions holding against the installed Hermes.

</details>

## [1.0.8] — 2026-08-20

**In short**

- ➖ *Removed* — `_StreamWatchdog`.
- ➖ *Removed* — `_emit_wait_notice` in the rotation loop.
- ➖ *Removed* — `_emit_wait_notice` in `_Vigil.maybe_speak`.
- ➖ *Removed* — `VIGIL_FIRST_S = 5.0`.
- ➖ *Removed* — `CHUNK_STALE_TIMEOUT`.
- ➕ *Added* — `_Spinner`.
- ➕ *Added* — Jitter.
- ➕ *Added* — Bind by signature.
- ➕ *Added* — 13 new tests.
- • *Decided* — First-Token Timeout.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### Removed
- **`_StreamWatchdog`** — the thread daemon that killed the active HTTP client
  after 60s without a token. It violated ADR 0002 ("Trust the Connection"),
  could corrupt the HTTP client state used by Hermes for rewind/edit/resend,
  and reintroduced the same death-loop pattern removed from Agent Zero in
  v0.4.7–v0.5.5.
- **`_emit_wait_notice` in the rotation loop** — the per-rotation call to
  `thinking_callback` that flooded the spinner channel on every key attempt.
- **`_emit_wait_notice` in `_Vigil.maybe_speak`** — redundant with `_emit_status`
  (the lifecycle channel), which the Vigil already uses.
- **`VIGIL_FIRST_S = 5.0`** reverted to `90.0` — 5 seconds was too aggressive
  and spammed the spinner on every brief wait.
- **`CHUNK_STALE_TIMEOUT`** setting, env var, and config_schema entry —
  orphaned by the StreamWatchdog removal.

### Added
- **`_Spinner`** — throttled (10s) live status through `thinking.delta`, the
  same transient channel the Hermes "30s / 60s / 90s" wait notices use. It
  does not create messages and does not affect message ordinals, so it cannot
  break rewind, edit, or resend. Shows rotation state when something changes:
  `KAME: rotating (attempt 3) — 12/15 healthy`,
  `KAME: 3/15 resting — ETA 2m`,
  `KAME: back up — 15/15 healthy`. Never reveals key fingerprints.
- **Jitter** — `random.uniform(0.1, 1.5)` seconds on every recovery wait
  (Agent Zero parity). Anti-bot-detection, anti-multi-client-sync-collision.
  Injectable; default is 0.0 in tests for determinism, active in `install()`.
- **Bind by signature** (Agent Zero v1.0.9 parity) — `inspect.signature()`
  checks that dispatch functions accept at least 2 positional arguments before
  wrapping. If a future Hermes changes the shape, KAME prints one line and
  steps aside — the agent keeps running.
- **13 new tests** (`test_v1_0_8.py`) covering _Spinner throttle/diff/no-flood/
  no-key-leak, jitter injection, and bind-by-signature step-aside.

### Kept (from 1.0.3–1.0.7, all good additions by the previous developer)
- `classify.py`: 8 new provider patterns (Alibaba, Z.AI, HuggingFace, Kimi,
  concurrency limits).
- `classify.py`: `structured_error_tokens()` — resolves type-vs-prose
  contradictions (Kimi 429 with "overloaded" message but `rate_limit_error` type).
- `classify.py`: HTTP 200 guard (prevents MiniMax false-positive billing).
- `classify.py`: `PER_WEEK` checked before `PER_MONTH` (priority bug fix).
- `quota.py`: `extract_reset_moment_from_text()` — reads "resets at
  2026-08-21" timestamps from Z.AI codes 1308/1310/1316-1321.

### Decided not to implement
- **First-Token Timeout** — ADR 0002 is clear: any artificial timeout on
  reasoning models eventually becomes the problem. Hermes already provides
  `HERMES_API_TIMEOUT` (1800s) and per-provider `request_timeout_seconds`.
  Reimplementing this in KAME would reintroduce the bug the StreamWatchdog
  caused.

### Test results
- 1116/1116 tests passing (1103 existing + 13 new).

</details>

## [1.0.2] — stable baseline

- Storm log collapse: 3 full failures, then periodic counts.
- Bug fix: `PER_WEEK` / `PER_MONTH` ordering.
- Bug fix: MiniMax HTTP 200 not false-positive billing.
- 1103 tests passing.

## [1.0.1] — wait without ceiling

- Removed the 10-minute wait ceiling (ADR 0002 parity).
- Vigil: narrated long waits at 90s, then every 10 minutes.
- Spin guard: yield slice when selector and clock disagree.
- Storm collapse of log lines.

## [1.0.0] — the carousel

- `DispatchBinding` wraps both dispatch functions.
- Pre-call: picks healthiest key (RPM 60s + LRU).
- Failure: rests key and rotates, error never reaches the conversation loop.
- 400 / `INVALID_ARGUMENT`: quarantines key, does not abort the turn.
- Partial stream is never replayed (progress flag).

## [0.2.4–0.2.6] — end-to-end testing harness

- 429 off real socket → per-model bench.
- `field_binding` for multi-key split in Settings.
- Live testing through real SDK.

## [0.1.0] — backoff per key and per kind

- Backoff escalates independently per key and per error type.

## [0.0.4] — per-model health isolation

- `pool_binding` isolates health per `provider:model`.

## [0.0.3] — no provider allowlist

- Acts on evidence (retry attributes, headers, structured bodies), not identity.

## [0.0.1] — initial port

- Gemini-only, post-failure hook.
