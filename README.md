<p align="center">
  <img src="assets/kame-cover.png" width="300" alt="KAME — Key-Aware Management Engine" />
</p>

# KAME API Rotation for Hermes

**One API key per call, chosen for you. A failed call moves to the next key instead of ending your turn. A spent quota is waited out instead of given up on.**

If you own several API keys — a handful of free-tier Google keys, two OpenRouter keys, a spare OpenAI key — Hermes will use the first one until the provider stops answering, and then your turn ends. KAME uses all of them: it picks the healthiest key before every request, and when a call fails it rests that key for exactly as long as the provider's own answer justifies and sends the next one, without the error reaching your chat.

[Install](#install) · [Screenshots](#screenshots) · [Adding your keys](#adding-your-keys) · [Settings](#settings) · [Commands](#commands) · [Licence](#licence) · [Changelog](CHANGELOG.md) · [Internals](docs/internals.md)

---

## What you get

| Without KAME | With KAME |
|---|---|
| The same key is sent on every call until it refuses | The least-used key of the last 60 seconds goes out, so fifteen keys spread fifteen ways |
| A provider 503, three times in a row, ends the turn | That key rests 5 seconds, the next one takes over, and the answer arrives |
| A rate-limited key is benched for a flat hour | It is benched for what the provider actually said — 21 seconds for a per-minute throttle, until midnight for a daily cap |
| A key exhausted on your main model is unusable for the small one too | The bench remembers which model spent the quota |
| `400 API key not valid` ends the run | That one key is quarantined and another answers |
| An answer cut off mid-sentence arrives with `[System: The previous response was cut off…]` | It is continued on another key and arrives as one piece |
| Nothing on screen while a quota lasts | A chip on the status bar and a `/kame` panel say which keys are resting and when the next one is back |

Everything above happens with **no provider allowlist**. KAME acts on the evidence in a response — retry timing in SDK exceptions, rate-limit headers, structured error bodies — and stays out of the way when it cannot read one, leaving Hermes' own classifier in charge. A provider that does not exist yet is covered by the same rule.

---

## Install

```bash
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation
```

Then restart Hermes. That is the whole install: the Python half registers, and it copies its own Desktop half to `$HERMES_HOME/desktop-plugins/hermes-kame-api-rotation/plugin.js`, which the renderer loads by default — no toggle, no second step.

From a local copy of this repository instead:

```bash
cp -r hermes-kame-api-rotation "$HERMES_HOME/plugins/"
```

On Windows `$HERMES_HOME` is `%LOCALAPPDATA%\hermes`.

**Requires** Hermes (Python 3.9+) and **no third-party packages at all**. The panel and the status chip need a Hermes Desktop shell; the rotation itself is CLI-safe and needs nothing.

**Uninstall** is deleting that directory — the Desktop half removes itself when the plugin unloads. To switch it off without removing it, set `KAME_ROTATION_DISABLED=1` or use the first switch in the panel's Settings tab.

---

## Adding your keys

KAME rotates whatever Hermes' credential pool holds. Three ways to fill it; the first two are Hermes' own.

**1. Paste several keys into one provider field.** In the dashboard, or in a `GOOGLE_API_KEY` style variable, separate them with commas:

```
GOOGLE_API_KEY=AIzaSy…aaa,AIzaSy…bbb,AIzaSy…ccc
```

Hermes stores that as one credential. KAME reads it as the three keys it is — that is one of the things this plugin fixes.

**2. One at a time, the built-in way.**

```bash
hermes auth add gemini --type api-key
```

**3. `/kame-keys` — bulk, from any chat**, including the Android app.

```
/kame-keys add AIza…,AIza…,AIza…
/kame-keys add openrouter sk-or-…,sk-or-…
/kame-keys import ~/keys.txt
/kame-keys                       show pooled keys and their health
/kame-keys reset                 clear exhaustion, re-enable every key
```

Commas, spaces, newlines, semicolons and pipes all separate keys, so a paste from a `.env`, a spreadsheet column or Agent Zero's comma list imports as it is. `KEY=value` lines are unwrapped. Keys already pooled are skipped, so re-running an import adds nothing.

**A key is never echoed** — every key in every message is printed as `AIzaSy…q7R8`, including on the error path. Writes go through the same `pool.add_entry()` the dashboard uses, and `auth.json` is backed up before the first write of a run.

Pasting keys into a chat does put them in that session's transcript. `import <file>` avoids that.

One key works too. There is nothing to rotate to, and every other part of the plugin still applies.

---

## Screenshots

> The two views below are **drawn, not photographed** — this repository ships no capture of a running Desktop shell. They are accurate to the layout and the wording, not to the pixels.

**The status-bar chip**, on screen at all times — the pool being called first, the rest collapsed:

```
●  KAME  gemini:gemini-3.7-flash 14/15   openai:gpt-5-mini 3/3  +2
```

A red dot appears beside a pool holding a key the provider refused as invalid. A countdown (`4m 12s`) appears beside a pool with nothing usable, and only then.

**The panel** at `/kame`, from the sidebar row *KAME API Rotation*:

```
KAME API Rotation  v1.2.0            ● 14 of 15 keys ready   live
[ Overview ]  [ Settings ]  [ Events (12) ]

RIGHT NOW      Calling gemini-3.7-flash, with 14 of 15 keys ready.
POOL HEALTH    gemini:gemini-3.7-flash   14/15 ready   next in 41s
               ██████████████████████████████████░░
THIS PROCESS   Calls 214 · Rotations 9 · Recovered 7 · Continued 2
```

*Settings* is described below. *Events* lists the last fifty decisions with the time, the key's fingerprint, the reason and the status code — fingerprints and counts only, never a key and never a line of provider text, so the screen is safe in a screenshot.

---

## Settings

**KAME works with none of these touched.** They are on three labelled shelves, in the panel and in `/kame get`, because they are not equally interesting:

### Optional — off until you turn it on

| Setting | What it does |
|---|---|
| `stream_silence_timeout_seconds` | **Give up on a silent key after** this long. KAME waits that long for the first character of an answer, and that long again for every character after it; if nothing arrives, the key is rested and the next one takes over. `0`, the default, means never — Hermes' own 120-second read timeout is the only limit. Any other value is raised to at least 5 seconds, because anything shorter fires while a healthy provider is still connecting. Leave it off for a local endpoint, which can legitimately think for minutes. |

This is the only setting here that *adds* a behaviour. Everything else either adjusts a default or takes something away.

### Tuning — already right for the providers this was built against

| Setting | Default | What it does |
|---|---|---|
| `daily_quota_cooldown_seconds` | `3600` | How long a key rests after a daily or account-level refusal — the one case where the provider's own retry hint is ignored on purpose, because it reports seconds for a counter that does not roll until midnight. |
| `stream_resume_limit` | `3` | How many times one request may continue an answer the provider cut off, before what arrived is handed back as it is. |

### Turn parts of KAME off — escape hatches, not preferences

Nine switches, each named for what it gives back to Hermes: `disabled`, `spread_disabled`, `carousel_disabled`, `field_probe_disabled`, `resolver_disabled`, `storm_collapse_disabled`, `live_status_disabled`, `gemini_tool_call_fix_disabled`, `stream_stitch_disabled`. They exist so that somebody who suspects KAME of breaking their agent can prove it with one switch. Leave them alone unless something is wrong.

**Where a value lives.** Every setting is also an environment variable (`KAME_STREAM_RESUME_LIMIT`, `KAME_ROTATION_DISABLED`, …) and **the environment wins**, because `KAME_ROTATION_DISABLED` is the emergency switch and an emergency switch a config file can override is not one. Otherwise Hermes' own `plugins.entries.hermes-kame-api-rotation.settings` in `config.yaml` is read, and then the built-in default. `/kame get` and the panel both print which of the three a value came from.

Changing a setting from the panel or with `/kame set` writes only `KAME_*` lines to Hermes' own `.env`, so it takes effect on the next call and survives a restart. Nothing else in that file is touched.

---

## Commands

| Command | What it answers |
|---|---|
| `/kame` | Everything at a glance: pool health, this process's counters, what is resting and until when |
| `/kame get` | Every setting, its value, and where that value came from |
| `/kame set <key> <value>` | Change one, live and permanently |
| `/kame reset <key>` | Put one setting back to its default |
| `/kame events` | The last rotations, quarantines and cut streams |
| `/kame-quota` | The quota ledger, per key and per model |
| `/kame-keys` | Add pooled keys in bulk (see above) |

---

## What it never does

- **It never reads, writes, prints or deletes an API key.** Keys are handed to the provider by Hermes, as always. Every screen and every file KAME writes carries fingerprints and counts.
- **It declares no capabilities.** Hermes gates seven things behind an explicit user grant — replacing built-in tools, redirecting host-owned LLM calls, acting on chat platforms. KAME needs none of them, so there is no consent prompt to answer and nothing to grant.
- **It sends nothing anywhere.** No telemetry, no network call of its own, no third-party package.
- **It never overrules Hermes on a failure it cannot read.** A response with no retry timing in it is left to the host's own classifier, unchanged.

---

## Is it working?

Three ways to tell, in order of effort:

1. The **chip** on the status bar shows `14/15` and counts down when a key is resting.
2. `/kame-quota` shows how many requests each healthy key took in the last sixty seconds. Fifteen keys with roughly equal counts is the whole feature, visible.
3. `/kame events` shows the decisions themselves — which key was rested, for how long, and what the provider said the status was.

If KAME is doing nothing, the counters say so plainly rather than looking like an install that went quiet.

---

## Compatibility and status

Built and tested against Hermes v0.20.x, Python 3.9+. **1416 tests** pass offline; a further set of harnesses runs against the *installed* Hermes rather than against fixtures:

```bash
python -m pytest tests/ -q                       # 1416 offline tests
hermes plugins doctor ./hermes-kame-api-rotation --ci
python tools/host_assumptions.py                 # the host facts KAME's decisions rest on
python tools/sandbox_binding.py                  # the binding, against the real host
python tools/host_corpus.py                      # Hermes' own error corpus, with and without KAME
python tools/host_pool_suite.py                  # Hermes' own credential-pool suites, with and without KAME
```

`tools/live_429.py` and `tools/live_multikey.py` drive real refusals off a real socket, through the real SDK, the real classifier and the real pool. See [Internals — Verify](docs/internals.md#verify) for what each one proves and how each was checked against being vacuous.

**Honest limits.** The 1.1.x series was developed while running against Google's free tier daily, and two releases exist because of what that produced — a provider that refuses a continuation in its own words (1.1.2), and a stream that stops inside a tool call (1.1.3). What is still not covered by an automated proof is the provider's own quota running out in normal use: the 429 in the harness is a captured payload replayed off a local socket, not a live counter reaching its limit.

---

## The Agent Zero sibling

KAME started as an [Agent Zero](https://github.com/agent0ai/agent-zero) plugin and was ported here. The two share the decision core and the version line: **the same MAJOR.MINOR means the same generation of behaviour on both hosts**, and the patch number moves independently. See [kame-api-rotation-for-agent-zero](https://github.com/Kame696/kame-api-rotation-for-agent-zero).

---

## Licence

MIT. See [LICENSE](LICENSE).
