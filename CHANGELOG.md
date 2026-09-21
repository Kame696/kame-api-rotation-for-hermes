# Changelog — KAME API Rotation for Hermes

What changed in each release, in a few lines. Versions follow
`major.minor.patch.build`; the newest is at the top.

---

## [1.8.1.0] — 2026-09-21 — the Gemini `RESOURCE_EXHAUSTED` ladder

- **New:** Gemini's bare `429 RESOURCE_EXHAUSTED` — no `retryDelay`, no
  `quotaId`, no `Retry-After` — rests the refused key **1s, then 2, 4, 8, 16,
  32, 64s** on each repeat instead of a flat 30s. It resets the moment that key
  answers. A number the provider states is always obeyed and never multiplied,
  and every other error keeps its own rest. Switch: `KAME_UNSIZED_BACKOFF`.
- **Measured in real use:** fewer calls per minute than the flat rest (24
  against 29) during the same kind of congestion, with no fewer answers.
- **Fixed — works on Hermes 0.21.3.** Hermes 0.21.3 gave its credential pool
  per-model cooldowns and passes `model=` into the methods KAME wraps; KAME's
  wrappers refused the new argument, so every key selection raised `TypeError`.
  They now forward whatever the host passes, and a host "look" at the pool
  (`count=False`) is no longer counted as load on a key. Measured with Hermes'
  own pool suite: 58 changed behaviours before, 0 after — on 0.21.1 and 0.21.3.
- **Fixed:** a refusal Hermes has its own dedicated contract for (0.21.3's
  anonymous welcome tier) is left to Hermes, so its message and alternates
  reach you. Decided by Hermes' own parser of the payload, never by name.
- **Fixed:** the Settings panel reported "off" as the default of the three
  switches that ship on, so *Reset* briefly showed the wrong position.
- **Fixed:** the *Optional* shelf said "off until you turn it on" while three of
  its four settings ship on.
- **Changed:** two invisible byte-order-mark characters in the source are now
  written as escapes. Same value at runtime; the Hermes catalog's security
  scan now reports **safe** instead of *caution*.

## [1.8.0.2] — 2026-09-20 — thirty seconds for a throttle with no number

- A per-minute refusal that names no wait rests **30s** (was 20s). Measured:
  a refused key retried within 30s answered **0 of 73** times; over the whole
  recorded corpus 30s avoids 388 of 413 avoidable refused calls.
- Supersedes 1.8.0.1, whose 45s came from a miscounted curve.

## [1.8.0.1] — 2026-09-20

- First measurement of the bare Gemini 429 on real traffic; rest raised from
  20s. Superseded by 1.8.0.2.

## [1.8.0.0] — 2026-09-19 — no key held longer than an hour

- **Ceiling on every hold** (`max_hold_seconds`, default 1h), whatever set it:
  the provider's stated wait, the daily cooldown or KAME's own escalation.
- **Account-wide limits bench the account**, not one model: a subscription
  limit (e.g. Codex `usage_limit_reached`) rests that credential on every
  model of the provider.
- **Tokens-per-minute is its own window**, separate from requests-per-minute.
- **A timeout rotates without benching** — the key was fine, the provider was
  slow.
- **A gateway 401 rests 20s, not 5 minutes** — keys refused that way were
  measured answering again within seconds.
- **Several Hermes profiles share one health file**, so a key benched in one is
  benched in all, and a hold survives a restart. Only key hashes are written.
- **The refusal recorder keeps a short allowlist of response headers** (retry
  and rate-limit headers), with anything key- or address-shaped dropped.
- Checked against an answer key written without reading the code: error
  family 100%, window 99.56%, scope 99.83%, rest 98.80%.

## [1.7.0.6] — 2026-09-09

- One account's daily limit no longer rests another account's key.
- A fresh provider deadline is not multiplied by old failure history.
- Subscription reset fields are read.

## [1.7.0.5] — 2026-09-07

- The Events tab shows where every rest came from, including KAME's own
  default and a number read from the error sentence.

## [1.7.0.4] — 2026-09-07

- **A 5xx never escalates** and rests 1s flat: an outage is the provider's
  problem, not the key's.

## [1.7.0.3] — 2026-09-07

- A retry hint in **milliseconds was read as minutes** (`683ms` became 11.4
  hours on a healthy key). One duration parser now reads every unit.

## [1.7.0.2] — 2026-09-07

- A **daily-quota label alone buys a 5-minute re-probe**, not an hour: keys
  refused "for the day" were measured answering again 6 to 36 minutes later.
  The hour applies once the whole pool has gone 20 minutes without an answer.

## [1.7.0.1] — 2026-09-06

- Fourteen keys had been sharing one internal name, which disabled the only
  rule allowed to widen a rest on evidence. Each key is its own identity again.

## [1.7.0.0] — 2026-09-05

- Rebuilt against written rules and measured against 13,561 real refusals
  before a line changed.

## [1.6.0.4] — 2026-09-04

- A model that *thinks* is no longer mistaken for one that has started
  answering, which had switched rotation off for thinking models.

## [1.6.0.3] — 2026-09-04

- The provider's stated wait is obeyed instead of doubled.
- Gemini's `quotaId` — the one field that tells a per-minute limit from a
  per-day one — is read back after Hermes' adapter discards it.

## [1.6.0.2] — 2026-09-03

- Weaker evidence no longer overrules stronger; an unsized 429 no longer
  benches a key for an hour.

## [1.6.0.1] — 2026-09-02

- Several Hermes processes on one home (Desktop and gateway) are shown and
  counted separately.

## [1.6.0.0] — 2026-09-02

- Three ceilings that could end a turn were removed; the rest of the release
  made existing behaviour true in every path.

## [1.5.0] — 2026-08-29

- Transport failures are classified by exception type.
- A 402 that says "try again in 5 minutes" is a wait, not a dead balance.

## [1.4.0] — 2026-08-29

- Gemini failures carried an empty message, so nothing could be read from
  them; the error text is read from the exception itself now.

## [1.2.9] — 2026-08-26

- The classifier uses the real provider of each call instead of assuming
  Gemini; `Retry-After` and structured error bodies reach it.

## [1.2.6] — 2026-08-26

- Hermes' own guidance footer ("requests/day") no longer makes a per-minute
  limit look like a daily quota.

## [1.2.5] — 2026-08-25

- Consecutive timeouts shorten the silence timeout for the remaining keys.

## [1.2.2] — 2026-08-24

- A pool row holding several comma-separated keys is split before anything is
  sent; one key declared twice counts once; a key removed from the config
  leaves the pool.

## [1.2.0] — 2026-08-23

- Settings grouped into three labelled shelves.

## [1.1.3] — 2026-08-23

- A key is not rested when it is the only one that is well.

## [1.1.2] — 2026-08-23

- Gemini refuses a prefilled continuation ("Requests ending with a model turn
  are not supported"); that no longer breaks the turn.
- The manifest declares a version Hermes' installer accepts.

## [1.1.1] — 2026-08-22

- A settings editor, an Events screen and a first-run state in the panel.

## [1.1.0] — 2026-08-21

- The Desktop panel, and a fix for "Response truncated due to output length
  limit" on Gemini parallel tool calls.

## [1.0.9] — 2026-08-21

- The live status line and the `/kame` command.

## [1.0.0] — the carousel

- A key chosen for every call (fewest requests in the last 60s, least recently
  used on a tie); a failure rests that key and rotates without the error
  reaching the conversation.

## [0.0.3] — no provider allowlist

- Acts on evidence — retry attributes, headers, structured bodies — never on
  a provider's name.
