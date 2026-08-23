<div align="center">

# \\\ ~ 🐢⚡ Key-Aware Management Engine ⚡🐢 ~ // (API Rotation Plugin) for Hermes

### KAME API Rotation for Hermes — one API key per call, chosen for you

[![Version](https://img.shields.io/badge/version-1.2.0-blue.svg)](https://github.com/Kame696/kame-api-rotation-for-hermes/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Hermes](https://img.shields.io/badge/Hermes-v0.20.x-purple.svg)](https://github.com/NousResearch/hermes)
[![Python](https://img.shields.io/badge/python-3.9%2B-yellow.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-1416_passing-brightgreen.svg)](#-compatibility--verification)
[![Dependencies](https://img.shields.io/badge/dependencies-none-lightgrey.svg)](#-what-it-never-does)
[![GitHub stars](https://img.shields.io/github/stars/Kame696/kame-api-rotation-for-hermes?style=social)](https://github.com/Kame696/kame-api-rotation-for-hermes/stargazers)

<img src="https://raw.githubusercontent.com/Kame696/kame-api-rotation-for-hermes/main/assets/kame-cover.png" width="420" alt="KAME — Key-Aware Management Engine" />

### *4P1 R0T4T10N — 4FRE3D0M*

**API key rotation, rate-limit recovery and 429 failover for the Hermes agent — Gemini, OpenAI, OpenRouter, Anthropic, or a provider that does not exist yet.**

[Install](#-install--three-lines) · [Keys](#-adding-your-keys) · [What you see](#-what-you-see) · [Settings](#-settings) · [Commands](#-commands) · [Verification](#-compatibility--verification) · [Changelog](CHANGELOG.md) · [Internals](docs/internals.md)

</div>

---

## 🎯 What is KAME?

**A failed call should not end your turn.**

Hermes sends the key it resolved. If that key is rate-limited, out of quota, or the provider is having a bad ten minutes, the failure comes back to you and the turn is over — even when you own fourteen other keys that would have answered instantly.

KAME sits between the two and does the obvious thing nobody had done: **it picks a key per call, and when a call fails it tries the next one instead of giving up.**

<table>
<tr>
<td width="50%" valign="top">

### 🧠 Learns from every 429

Reads the provider's own retry timing — out of SDK exception fields, `Retry-After` headers and structured error bodies — and rests that key for exactly that long. A per-minute throttle costs 21 seconds. A daily cap costs an hour, because the number the provider prints for a daily counter is a lie it tells every time.

</td>
<td width="50%" valign="top">

### 🎯 Picks the healthiest key, every call

A 60-second sliding window tracks how hard each key has been worked. The least-loaded one goes out next, so fifteen keys spread fifteen ways instead of one key carrying everything until it breaks.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🎠 Rotates instead of failing

A refusal moves to the next key inside the same request. Your chat never sees the error, the turn never ends, and the answer arrives from whichever key was willing to give it.

</td>
<td width="50%" valign="top">

### 🧵 Continues answers that get cut

A stream that stops mid-sentence is picked up on another key and handed back as **one piece** — not as a visible break with `[System: The previous response was cut off…]` stapled over it.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📊 Remembers per model, not per key

A key exhausted on your main model is still fine for the small one. The ledger is keyed by `provider:model`, so a quota spent in one place does not bench a key everywhere.

</td>
<td width="50%" valign="top">

### 👁️ Says what it is doing

A chip on the status bar and a `/kame` panel show which keys are resting and when the next one is back — **fingerprints and counts only, never a key**. A long wait stops looking like a hung agent.

</td>
</tr>
<tr>
<td colspan="2" valign="top">

### 🚫 No provider allowlist — anywhere

KAME decides on **evidence in the response**, never on who the provider is: retry timing in the exception, rate-limit headers, the shape of the error body. When it cannot read one, it says nothing and leaves Hermes' own classifier in charge. A provider released next year is covered by the same rule, with no update to this plugin.

</td>
</tr>
</table>

> **You give it the keys you already own. It gives you an agent that does not stop at the first refusal.**

---

## 🆚 With and without KAME

| Without KAME | With KAME |
|---|---|
| The same key is sent on every call until it refuses | The least-used key of the last 60 seconds goes out, so fifteen keys spread fifteen ways |
| A provider 503, three times in a row, ends the turn | That key rests 5 seconds, the next one takes over, and the answer arrives |
| A rate-limited key is benched for a flat hour | It is benched for what the provider actually said — 21 seconds for a per-minute throttle, until midnight for a daily cap |
| A key exhausted on your main model is unusable for the small one too | The bench remembers which model spent the quota |
| `400 API key not valid` ends the run | That one key is quarantined and another answers |
| An answer cut off mid-stream arrives with `[System: The previous response was cut off…]` | It is continued on another key and arrives as one piece |
| Several keys pasted into one field are sent as one long invalid key | They are read as the several keys they are |
| Nothing on screen while a quota lasts | A chip on the status bar and a `/kame` panel say which keys are resting and when the next one is back |

---

## ⚡ Install — three lines

```bash
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation
```

Then **restart Hermes**. That is the whole install: the Python half registers, and it copies its own Desktop half to `$HERMES_HOME/desktop-plugins/hermes-kame-api-rotation/plugin.js`, which the renderer loads by default — no toggle, no second step.

From a local copy of this repository instead:

```bash
cp -r hermes-kame-api-rotation "$HERMES_HOME/plugins/"
```

On Windows `$HERMES_HOME` is `%LOCALAPPDATA%\hermes`.

| | |
|---|---|
| **Requires** | Hermes (Python 3.9+) and **no third-party packages at all** |
| **Needs a Desktop shell for** | the panel and the status chip. Rotation itself is CLI-safe and needs nothing |
| **Uninstall** | delete the directory — the Desktop half removes itself when the plugin unloads |
| **Switch off without removing** | `KAME_ROTATION_DISABLED=1`, or the first switch in the panel's Settings tab |

---

## 🔑 Adding your keys

KAME rotates whatever Hermes' credential pool holds. Three ways to fill it; the first two are Hermes' own.

**1 — Paste several keys into one provider field.** In the dashboard, or in a `GOOGLE_API_KEY` style variable, separate them with commas:

```
GOOGLE_API_KEY=AIzaSy…aaa,AIzaSy…bbb,AIzaSy…ccc
```

Hermes stores that as one credential. KAME reads it as the three keys it is — that is one of the things this plugin fixes.

**2 — One at a time, the built-in way.**

```bash
hermes auth add gemini --type api-key
```

**3 — `/kame-keys`, bulk, from any chat** — including the Android app.

```
/kame-keys add AIza…,AIza…,AIza…
/kame-keys add openrouter sk-or-…,sk-or-…
/kame-keys import ~/keys.txt
/kame-keys                       show pooled keys and their health
/kame-keys reset                 clear exhaustion, re-enable every key
```

Commas, spaces, newlines, semicolons and pipes all separate keys, so a paste from a `.env`, a spreadsheet column or Agent Zero's comma list imports as it is. `KEY=value` lines are unwrapped. Keys already pooled are skipped, so re-running an import adds nothing.

> 🔒 **A key is never echoed** — every key in every message is printed as `AIzaSy…q7R8`, including on the error path. Writes go through the same `pool.add_entry()` the dashboard uses, and `auth.json` is backed up before the first write of a run.
>
> Pasting keys into a chat does put them in that session's transcript. `import <file>` avoids that.

One key works too. There is nothing to rotate to, and every other part of the plugin still applies.

---

## 🖥️ What you see

> The two views below are **layout diagrams, not photographs** — this repository ships no capture of a running Desktop shell. They are accurate to the layout and the wording, not to the pixels.

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

## 🛡️ The shields

Every protection KAME adds is one named subsystem with one switch that gives the job back to Hermes. Nothing here is a preference; each switch exists so somebody who suspects KAME of breaking their agent can prove it in one move.

| # | Shield | What it stops | Switch |
|---|---|---|---|
| 1 | **Spread** | One key carrying every call until it breaks | `spread_disabled` |
| 2 | **Carousel** | A single refusal ending the whole turn | `carousel_disabled` |
| 3 | **Resolver** | Several comma-separated keys being sent as one long invalid key | `resolver_disabled` |
| 4 | **Field probe** | The Settings key field refusing a legitimate multi-key paste | `field_probe_disabled` |
| 5 | **Cooldown sizing** | A 21-second throttle costing an hour — or a daily cap costing 21 seconds | `daily_quota_cooldown_seconds` |
| 6 | **Quarantine** | `400 API key not valid` on one key ending the run | *(core)* |
| 7 | **Stream stitching** | An answer cut mid-sentence arriving visibly broken | `stream_stitch_disabled` |
| 8 | **Gemini tool-call repair** | Two parallel tool calls merged into one unparseable argument string | `gemini_tool_call_fix_disabled` |
| 9 | **Storm collapse** | A ten-minute outage writing the same failure a thousand times | `storm_collapse_disabled` |
| 10 | **Live status** | A long quota wait looking like a frozen agent | `live_status_disabled` |

And one master switch, `disabled`, which leaves the plugin installed and doing nothing.

---

## ⚙️ Settings

**KAME works with none of these touched.** Since 1.2.0 they sit on **three labelled shelves** — in the panel and in `/kame get` — because they are not equally interesting.

### 🟢 Optional — off until you turn it on

The only setting here that *adds* a behaviour. Everything else either adjusts a default or takes something away.

| Setting | What it does |
|---|---|
| `stream_silence_timeout_seconds` | **Give up on a silent key after** this long. KAME waits that long for the first character of an answer, and that long again for every character after it; if nothing arrives, the key is rested and the next one takes over. `0`, the default, means never — Hermes' own 120-second read timeout is the only limit. Any other value is raised to at least 5 seconds, because anything shorter fires while a healthy provider is still connecting. Leave it off for a local endpoint, which can legitimately think for minutes. |

### 🔵 Tuning — already right for the providers this was built against

| Setting | Default | What it does |
|---|---|---|
| `daily_quota_cooldown_seconds` | `3600` | How long a key rests after a daily or account-level refusal — the one case where the provider's own retry hint is ignored on purpose, because it reports seconds for a counter that does not roll until midnight. |
| `stream_resume_limit` | `3` | How many times one request may continue an answer the provider cut off, before what arrived is handed back as it is. |

### 🔴 Turn parts of KAME off — escape hatches, not preferences

Nine switches, each named for what it gives back to Hermes: `disabled`, `spread_disabled`, `carousel_disabled`, `field_probe_disabled`, `resolver_disabled`, `storm_collapse_disabled`, `live_status_disabled`, `gemini_tool_call_fix_disabled`, `stream_stitch_disabled`. Leave them alone unless something is wrong.

> **Where a value lives.** Every setting is also an environment variable (`KAME_STREAM_RESUME_LIMIT`, `KAME_ROTATION_DISABLED`, …) and **the environment wins**, because `KAME_ROTATION_DISABLED` is the emergency switch and an emergency switch a config file can override is not one. Otherwise Hermes' own `plugins.entries.hermes-kame-api-rotation.settings` in `config.yaml` is read, and then the built-in default. `/kame get` and the panel both print which of the three a value came from.
>
> Changing a setting from the panel or with `/kame set` writes only `KAME_*` lines to Hermes' own `.env`, so it takes effect on the next call and survives a restart. Nothing else in that file is touched.

---

## 💬 Commands

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

## 🚫 What it never does

- **It never reads, writes, prints or deletes an API key.** Keys are handed to the provider by Hermes, as always. Every screen and every file KAME writes carries fingerprints and counts.
- **It declares no capabilities.** Hermes gates seven things behind an explicit user grant — replacing built-in tools, redirecting host-owned LLM calls, acting on chat platforms. KAME needs none of them, so there is no consent prompt to answer and nothing to grant.
- **It sends nothing anywhere.** No telemetry, no network call of its own, no third-party package.
- **It never overrules Hermes on a failure it cannot read.** A response with no retry timing in it is left to the host's own classifier, unchanged.

---

## ❓ Is it working?

Three ways to tell, in order of effort:

1. The **chip** on the status bar shows `14/15` and counts down when a key is resting.
2. `/kame-quota` shows how many requests each healthy key took in the last sixty seconds. **Fifteen keys with roughly equal counts is the whole feature, visible.**
3. `/kame events` shows the decisions themselves — which key was rested, for how long, and what the provider said the status was.

If KAME is doing nothing, the counters say so plainly rather than looking like an install that went quiet.

---

## ✅ Compatibility & verification

Built and tested against **Hermes v0.20.x, Python 3.9+**. **1416 tests** pass offline; a further set of harnesses runs against the *installed* Hermes rather than against fixtures:

```bash
python -m pytest tests/ -q                       # 1416 offline tests
hermes plugins doctor ./hermes-kame-api-rotation --ci
python tools/host_assumptions.py                 # the host facts KAME's decisions rest on
python tools/sandbox_binding.py                  # the binding, against the real host
python tools/host_corpus.py                      # Hermes' own error corpus, with and without KAME
python tools/host_pool_suite.py                  # Hermes' own credential-pool suites, with and without KAME
```

`tools/live_429.py` and `tools/live_multikey.py` drive real refusals off a real socket, through the real SDK, the real classifier and the real pool. See [Internals — Verify](docs/internals.md#verify) for what each one proves and how each was checked against being vacuous.

**Honest limits.** The 1.1.x series was developed while running against Google's free tier daily, and two releases exist because of what that produced — a provider that refuses a continuation in its own words (1.1.2), and a stream that stops inside a tool call (1.1.3). What is still **not** covered by an automated proof is the provider's own quota running out in normal use: the 429 in the harness is a captured payload replayed off a local socket, not a live counter reaching its limit.

---

## 🪪 Evolution

Every release, newest first. The full entries — what broke, what the log said, what was decided — are in [CHANGELOG.md](CHANGELOG.md).

| Version | Headline | What it gave you |
|---|---|---|
| **1.2.0** | Settings you can read | Three labelled shelves, so the one optional extra can no longer be mistaken for something rotation needs |
| **1.1.3** | The rest that bought nothing | A key is no longer benched when it is the only well one — two defects found by reading the loop, not by anything failing |
| **1.1.2** | The provider that refuses | Gemini will not be handed its own turn back; the continuation now adapts instead of ending the turn it exists to save |
| **1.1.1** | Answers arrive whole | Stream stitching, plus a panel that can actually be used: every switch a switch, every number a field |
| **1.1.0** | A real panel | `/kame` stops being markdown painted raw and becomes a Desktop panel; Gemini's merged tool calls repaired |
| **1.0.10** | The status line that shows up | The host keeps only text in its own shape — matched, so the line appears instead of blanking Hermes' own |
| **1.0.9** | Causes, not symptoms | "It stops", "it freezes", "rewind broke" traced to four host facts — including a 410 the carousel was rotating against |
| **1.0.8** | Trust the connection | The stream watchdog deleted (it corrupted rewind/edit/resend); throttled live status put in its place |
| **1.0.2** | Stable baseline | Storm-log collapse, quota-period ordering, and 1103 tests green |
| **1.0.1** | Wait without ceiling | The 10-minute cap removed — a wait ends when the provider recovers, not when a timer says so |
| **1.0.0** | The carousel | A failed call moves to the next key instead of ending the turn |
| **0.0.3** | No provider allowlist | Every decision moved onto evidence in the response — never on who the provider is |

**Version parity with Agent Zero.** The same MAJOR.MINOR means the same generation of behaviour on both hosts; the patch number moves independently. 1.1.x exists only here because it fixed stream handling that Agent Zero owns itself — the two lines rejoin at 1.2.0.

---

## 🐢 The Agent Zero sibling

KAME started as an [Agent Zero](https://github.com/agent0ai/agent-zero) plugin and was ported here. The two share the decision core and the version line.

**→ [kame-api-rotation-for-agent-zero](https://github.com/Kame696/kame-api-rotation-for-agent-zero)**

---

## 📜 Licence

MIT. See [LICENSE](LICENSE).

---

<div align="center">

**If this kept your agent alive through a quota, ⭐ the repo.**

*Built by [Kame696](https://github.com/Kame696).*

</div>
