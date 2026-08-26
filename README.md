<div align="center">

# \\\ ~ 🐢⚡ Key-Aware Management Engine ⚡🐢 ~ // (API Rotation Plugin) for Hermes

### KAME API Rotation for Hermes — one API key per call, chosen for you

[![Version](https://img.shields.io/badge/version-1.2.4-blue.svg)](https://github.com/Kame696/kame-api-rotation-for-hermes/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Hermes](https://img.shields.io/badge/Hermes-v0.20.x-purple.svg)](https://github.com/NousResearch/hermes)
[![Python](https://img.shields.io/badge/python-3.9%2B-yellow.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-1439_passing-brightgreen.svg)](#-compatibility--verification)
[![Dependencies](https://img.shields.io/badge/dependencies-none-lightgrey.svg)](#-what-it-never-does)
[![GitHub stars](https://img.shields.io/github/stars/Kame696/kame-api-rotation-for-hermes?style=social)](https://github.com/Kame696/kame-api-rotation-for-hermes/stargazers)
[![Donate Bitcoin](https://img.shields.io/badge/donate-bitcoin-f7931a.svg)](#-support-the-project)

<img src="https://raw.githubusercontent.com/Kame696/kame-api-rotation-for-hermes/main/assets/kame-cover.png" width="420" alt="KAME — Key-Aware Management Engine" />

### *4P1 R0T4T10N — 4FRE3D0M*

**Free and MIT — built and paid for by one person.** If KAME saved a run, a tip keeps it going:
**BTC `36BGYhMEVFgY8PLGMVux93pjGt92KVM6dJ`** — *any amount helps, genuinely.*

**API key rotation, rate-limit recovery and 429 failover for the Hermes agent — Gemini, OpenAI, OpenRouter, Anthropic, or a provider that does not exist yet.**

[Install](#-install--three-lines) · [Keys](#-adding-your-keys) · [What you see](#-what-you-see) · [Settings](#-settings) · [Commands](#-commands) · [FAQ](#-faq) · [Verification](#-compatibility--verification) · [Changelog](CHANGELOG.md) · [Internals](docs/internals.md)

</div>

---

## 🎯 What is KAME?

**KAME is what API rotation should have been.**

Round-robin libraries cycle keys blindly. They keep banging on a key that just hit a 429 because they have no memory. They have no idea which key has capacity left. They retry through dead keys and call it "resilience."

KAME does the **opposite** of every assumption round-robin makes:

<table>
<tr>
<td width="50%" valign="top">

### 🧠 Learns from every 429

Parses the provider's own `retry-delay` out of SDK exception fields, `Retry-After` headers and structured error bodies, and respects it **to the second** on per-minute limits. On a **daily quota** it knows not to trust a misleadingly short delay — it cools that key for a real cooldown instead.

No guessing. No fixed backoff. Per-minute or daily, on any provider, KAME does the right thing.

</td>
<td width="50%" valign="top">

### 🎯 Picks the healthiest key, every call

A 60-second sliding window tracks each key's recent activity. KAME selects the key with the **most remaining capacity**, not just the next one in line.

LRU tie-break ensures even spreading across the pool.

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
<td width="50%" valign="top">

### 🤝 Trusts the connection

**Zero artificial timeout on top of what Hermes already gives a call.** `stream_silence_timeout_seconds` defaults to `0` — off — so a slow but healthy provider, or a local model that legitimately takes minutes to think, is never cut for looking quiet. Hermes' own 120-second read timeout is the only ceiling, unless you choose to lower it yourself.

</td>
<td width="50%" valign="top">

### 🥷 Stays invisible

Every wait for a cooling pool adds `random.uniform(0.1, 1.5)` seconds of **jitter** — the same mechanism the Agent Zero sibling uses, built into both for parity. No two waits land on the same tick, so what a provider sees is an uneven, human-shaped pause between calls, not a bot polling on a fixed clock.

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

## 🆚 KAME vs Plain Round-Robin

| Feature | Plain round-robin | **KAME (for Hermes)** |
|---|---|---|
| Selection logic | "next in line" (blind) | **most idle in the last 60s** (RPM-aware) |
| Behavior on refusal | retries the same key, or the turn ends | **rotates to the next key inside the same call** |
| Concurrent calls | pile on key #1 | **spread across keys** (anti-dogpile marking) |
| Daily-quota / out-of-credit | trusts a misleadingly short retry hint | **real cooldown, on any provider** |
| Invalid / revoked key | retried forever, or ends the run | **quarantined, the rest of the pool unaffected** |
| Cut-off streams | arrives as `[System: response cut off…]` | **continued on another key, as one seamless piece** |
| Several keys pasted in one field | sent as one long invalid credential | **auto-split into the pooled keys they are** (1.2.2) |
| A key edited or deleted in config | stays in the pool, quarantines forever | **mirrored out within minutes** (1.2.2) |
| Failure memory | none | **identity-aware health**, per `provider:model` |
| Live status & monitoring | blind wait / frozen agent | **status chip + `/kame` panel with live ETAs** |

> Round-robin only rotates blind. KAME rotates knowing which key can still answer — and it stops calling a key you already deleted.

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

No config required to start rotating. The only thing KAME needs from you is **more than one key in a provider you already use** — see below.

---

## 🔑 Adding your keys

KAME rotates whatever Hermes' credential pool holds. Three ways to fill it; the first two are Hermes' own.

**1 — Paste several keys into one provider field, glued together with commas.** In the dashboard, or in a `GOOGLE_API_KEY` style variable, one string, no spaces needed, no separate fields:

```
GOOGLE_API_KEY=AIzaSy…aaa,AIzaSy…bbb,AIzaSy…ccc
```

That is the whole trick: `KEY1,KEY2,KEY3,etc` — pasted as one value into the one field the provider already gives you. Hermes stores that as one credential. KAME reads it as the three keys it is — that is one of the things this plugin fixes (see [1.2.2](#-faq) below, where a joined list used to be sent to the provider as a single, very invalid key).

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

**Editing the list later works the way you would expect.** Since 1.2.2 the pool is a mirror of what your config resolves to *now*, not an archive of everything it has ever held: replace a key and the old one stops being tried within a few minutes, and stops being counted. Before that, a key you had already deleted went on failing, quarantining for an hour, and reading on the panel as a broken key — so a healthy pair of two could show as "2/3 healthy." The same pass splits every comma-joined value before anything is sent and counts a key once no matter how many config blocks declare it.

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
KAME API Rotation  v1.2.4            ● 14 of 15 keys ready   live
[ Overview ]  [ Settings ]  [ Events (12) ]

RIGHT NOW      Calling gemini-3.7-flash, with 14 of 15 keys ready.
POOL HEALTH    gemini:gemini-3.7-flash   14/15 ready   next in 41s
               ██████████████████████████████████░░
THIS PROCESS   Calls 214 · Rotations 9 · Recovered 7 · Continued 2
```

*Settings* is described below. *Events* lists the last fifty decisions with the time, the key's fingerprint, the reason and the status code — fingerprints and counts only, never a key and never a line of provider text, so the screen is safe in a screenshot.

---

## 🛡️ The 11 Shields

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
| 11 | **Pool mirror** | A key you deleted still being tried, refused, and counted as broken | *(core)* |

And one master switch, `disabled`, which leaves the plugin installed and doing nothing.

### 🔬 How it works — internals

The engine lives in `core/carousel.py`, framework-free on purpose: no sleeping, no logging, no Hermes object, no clock but the one passed in — so the decision rules are testable without a provider or a network. `dispatch_binding.py` is the only part that touches Hermes: it resolves the credential pool, calls `select()` before every request, calls `mark()` after every result, and owns the sleep when the whole pool is resting.

Per-key health state

Every API key carries this dictionary, scoped under `{provider}:{model}`:

```
{
    "sick_until":     float,  # epoch time when key becomes available again
    "last_used":      float,  # for LRU tie-break + anti-dogpile
    "request_log":    [float],# 60s sliding window of request timestamps
    "last_sick_at":   float,  # for the stream-stitch "fresh recovery" filter
    "consecutive_rl": int,    # consecutive rate-limit fails -> adaptive backoff (resets on success)
}
```

Selection algorithm

```
healthy = [k for k in usable if pool[k]["sick_until"] < now]
if not healthy:
    return min(usable, key=lambda k: pool[k]["sick_until"]), "EXHAUSTED"

chosen = min(healthy, key=lambda k: (
    len(pool[k]["request_log"]),  # primary: most remaining 60s-window capacity
    pool[k]["last_used"],         # secondary: LRU for even spreading
))
# Then, inside the same lock: mark used NOW (anti-dogpile)
#                             append to request_log NOW (anti-thundering-herd)
```

ETA-driven sleep formula

```
eta = next_recovery_seconds(identity, keys)   # None if a key is free right now
if eta is None:
    wait = 1.0                                 # re-check slice, never a spin
else:
    wait = min(eta + 0.5 + random.uniform(0.1, 1.5), 60.0)
sleep in 1s slices, interruptible, chip redrawn every slice
```

Every few minutes, `select()` also drops any pooled key the current candidate list no longer offers — so a key you removed from Settings stops being retried, refused and counted as broken, instead of quarantining forever against a config that no longer mentions it (the 1.2.2 fix — see the [FAQ](#-faq)).

---

## ⚙️ Settings

**KAME works with none of these touched.** Since 1.2.0 they sit on **three labelled shelves** — in the panel and in `/kame get` — because they are not equally interesting.

### 🟢 Optional — off until you turn it on

The only setting here that *adds* a behaviour. Everything else either adjusts a default or takes something away.

| Setting | What it does |
|---|---|
| `stream_silence_timeout_seconds` | **Wait for the first token** this long. KAME waits that long for the first character of an answer, and that long again for every character after it; if nothing arrives, the key is rested and the next one takes over. `0`, the default, means never — Hermes' own 120-second read timeout is the only limit. Any other value is raised to at least 5 seconds, because anything shorter fires while a healthy provider is still connecting. Leave it off for a local endpoint, which can legitimately think for minutes. |

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
>
> **Editing `config.yaml` by hand is the one case that needs a restart**, because KAME reads that file once, when Hermes starts. Since 1.2.2 the panel says so: it re-reads the file off the hot path and names any setting whose entry has changed since boot, so an edit that has not taken effect yet can no longer be mistaken for an edit that was wrong. A setting the environment owns is never named — restarting would not apply that one either.

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

## ❓ FAQ

<details>
<summary><b>My provider says I have 3 keys — I only pasted 2. Bug?</b></summary>

It was, through **1.2.1**. A pool row holding `key1,key2` was handed to the provider whole whenever the split didn't run — so a two-key paste showed up as a third, unrecognizable "key" (the joined string itself), quarantined with a 403 or 401 right beside your two real ones. **1.2.2** splits every raw value before anything is sent, on every path, and dedupes by key text — a credential declared in two config blocks is one pool entry, not two. If you still see a phantom key after upgrading, `/kame-keys reset` clears the old quarantine record.
</details>

<details>
<summary><b>I deleted / replaced a key in Settings and it still shows as broken. Bug?</b></summary>

Also fixed in **1.2.2**. Before it, the pool only ever grew — nothing removed a key Hermes' config no longer declared, so a replaced key kept its last refusal and went on being counted, reading as "2/3 healthy" for a pool that was actually a healthy pair. `Carousel.select()` now mirrors the live candidate list: a row nothing has offered in five minutes is dropped, ledger history and all. Five minutes, not instantly, because two agents can read the same identity off different lists, and dropping a row the moment one of them looks away would erase a cooldown the other one earned.
</details>

<details>
<summary><b>Do I need to restart Hermes after installing KAME?</b></summary>

Yes, once — the first install registers the Python half and copies the Desktop panel into place, and Hermes only loads plugins at boot. After that, most changes are live: `/kame set`, the panel's Settings tab, and every `KAME_*` environment variable take effect on the **next call**, no restart. The one exception is hand-editing `daily_quota_cooldown_seconds` or another setting directly in `config.yaml` — KAME reads that file once, at boot, so a restart is what applies it. Since 1.2.2 the panel says so explicitly, naming any setting whose config entry changed since Hermes started.
</details>

<details>
<summary><b>`stream_silence_timeout_seconds` is off by default. Should I turn it on?</b></summary>

Only if you are seeing a stream go silent and stay silent. At `0` (the default) KAME waits as long as Hermes' own 120-second read timeout allows — nothing artificial on top. Turning it on gives up on a key sooner, which helps against a provider that hangs without ever closing the connection, but hurts a **local or self-hosted endpoint**, which can legitimately think for minutes before the first token. Any value you do set is floored at 5 seconds, because anything shorter fires while a healthy provider is still connecting.
</details>

<details>
<summary><b>A key rested for a full hour on what looked like a short refusal. Bug?</b></summary>

No — that is `daily_quota_cooldown_seconds` (default `3600`) working as designed. On a **daily or account-level** refusal, the provider's own retry hint is ignored on purpose: it reports a number of seconds for a counter that does not actually roll over until midnight, and trusting it would mean re-probing a dead key every few seconds for a day. A **per-minute** throttle is never treated this way — KAME reads that provider's `retry-delay` and rests the key for exactly that long, typically single-digit or low double-digit seconds.
</details>

<details>
<summary><b>I only have one API key. Does KAME still help?</b></summary>

Yes — everything except spreading load applies: quarantine on an invalid key, the real cooldown on a daily refusal, stream stitching on a cut answer, live status while it rests. What it will not do is rotate to a second key, because there isn't one. **1.1.3** made sure a single healthy key is never rested over a transient stream hiccup just because it happened to be the only one; a full daily cooldown on a sole key is still honored in full — KAME considered and rejected capping it, because a cooldown that came from the provider's own words should bind the pool whatever else is or isn't in it.
</details>

<details>
<summary><b>KAME picked the same key twice in a row. Bug?</b></summary>

Likely not. With only 2–3 keys, one that just succeeded and still has fresh capacity in its 60-second window can legitimately be re-picked — the sliding window tracks *load*, not "did this key go last." With more keys (10+), this becomes rare on its own, because there is almost always a less-loaded one to prefer.
</details>

<details>
<summary><b>Can I use KAME with Anthropic / OpenAI / OpenRouter, not just Gemini?</b></summary>

Yes. KAME is provider-agnostic by construction — it decides on evidence in the response (retry timing, rate-limit headers, the shape of the error body), never on who the provider is. A provider released after this README was written is covered by the same rule, with no update needed to this plugin.
</details>

<details>
<summary><b>`400 API key not valid` on one key — is that KAME breaking something?</b></summary>

The opposite. That is the **Quarantine** shield: the provider refused that specific key outright, so KAME stops sending it traffic instead of retrying a dead credential every call. The rest of the pool keeps working. Fix or replace the key in your provider console; nothing further to do on KAME's side.
</details>

<details>
<summary><b>Can I manage keys from the Hermes Android app?</b></summary>

Yes — `/kame-keys` is a chat command, so it works anywhere Hermes chat works, including Android, talking to the same gateway your desktop does. `/kame-keys add <key1,key2,…>` pools them the same way a desktop paste would.
</details>

<details>
<summary><b>During an outage, does my log fill with the same failure over and over?</b></summary>

No — that is what **Storm collapse** exists to stop. During a repeated failure, KAME writes the first one, then nothing further until the storm ends, at which point `/kame events` shows the whole run as one entry rather than dozens of identical ones. Set `storm_collapse_disabled: true` if you want every single failure recorded individually.
</details>

<details>
<summary><b>Does KAME's status chip or panel cost extra API calls?</b></summary>

Zero. The chip, the panel and `/kame events` are all read from local state KAME already tracks — fingerprints, counters, timestamps. Nothing about watching the panel touches the network.
</details>

---

## ✅ Compatibility & verification

Built and tested against **Hermes v0.20.x, Python 3.9+**. **1439 tests** pass offline; a further set of harnesses runs against the *installed* Hermes rather than against fixtures:

```bash
python -m pytest tests/ -q                       # 1439 offline tests
hermes plugins doctor ./hermes-kame-api-rotation --ci
python tools/host_assumptions.py                 # the host facts KAME's decisions rest on
python tools/sandbox_binding.py                  # the binding, against the real host
python tools/host_corpus.py                      # Hermes' own error corpus, with and without KAME
python tools/host_pool_suite.py                  # Hermes' own credential-pool suites, with and without KAME
```

`tools/live_429.py` and `tools/live_multikey.py` drive real refusals off a real socket, through the real SDK, the real classifier and the real pool. See [Internals — Verify](docs/internals.md#verify) for what each one proves and how each was checked against being vacuous.

**Honest limits.** The 1.1.x and 1.2.x series were developed while running against Google's free tier daily, and every fix past 1.1.0 exists because of what that produced — a provider that refuses a continuation in its own words (1.1.2), a stream that stops inside a tool call (1.1.3), and a pool that outlived the config it was supposed to mirror (1.2.2). What is still **not** covered by an automated proof is the provider's own quota running out in normal use: the 429 in the harness is a captured payload replayed off a local socket, not a live counter reaching its limit.

---

## 🪪 Evolution

Every release, newest first. The full entries — what broke, what the log said, what was decided — are in [CHANGELOG.md](CHANGELOG.md).

| Version | Headline | What it gave you |
|---|---|---|
| **1.2.2** | The pool is a mirror, not an archive | The comma-joined list stopped being sent as a key of its own, a key deleted from the config stops being retried, and one save is one movement on screen |
| **1.2.1** | Resilient Gemini streams | SDK-wrapped stream read timeouts are recognized as non-terminal and rotated transparently instead of ending the turn |
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

**Version parity with Agent Zero.** The same MAJOR.MINOR means the same generation of behaviour on both hosts; the patch number moves independently. 1.1.x and the 1.2.1–1.2.2 patch pair exist only here because they fixed stream and pool-mirroring issues that Agent Zero either owns itself or doesn't expose the same way — the two lines rejoin at each shared MAJOR.MINOR.

---

## 🐢 The Agent Zero sibling

KAME started as an [Agent Zero](https://github.com/agent0ai/agent-zero) plugin and was ported here. The two share the decision core and the version line.

**→ [kame-api-rotation-for-agent-zero](https://github.com/Kame696/kame-api-rotation-for-agent-zero)**

Both ports, the parity rule and the table of what each host already does itself:
**[kame-api-rotation](https://github.com/Kame696/kame-api-rotation)** — the family's front door.

---

## 🤝 Contributing

PRs welcome. The engine is intentionally small and modular — `core/carousel.py` owns selection and cooldown, `core/answer.py` owns stream stitching, `settings.py` owns the three shelves. When proposing changes:

1. Keep the **decision core** stable — selection, anti-dogpile, cooldown sizing and the mirror are battle-tested against a real free-tier pool.
2. Add features behind opt-in settings when possible, following `settings.GROUPS` (see `stream_silence_timeout_seconds` as the pattern for something that lives in "Optional").
3. A new offline test in `tests/` plus, where it applies, a harness in `tools/` that runs against the real host — see [Internals — Verify](docs/internals.md#verify) for what "against the real host" means here.

Bugs and feature requests via [GitHub issues](https://github.com/Kame696/kame-api-rotation-for-hermes/issues).

---

## ❤️ Support the project

KAME is free, MIT, and written by one person against real quotas on a real free
tier. There is no company behind it, no telemetry, and nothing to upsell.

If it saved you a run — or an afternoon — a tip keeps it going:

**Bitcoin** — `36BGYhMEVFgY8PLGMVux93pjGt92KVM6dJ`

*Any amount helps, genuinely. Every sat goes into keeping this project alive and
learning.* And if money is not on the table, a ⭐ costs nothing and helps other
people find it.

---

## 📜 Licence

**MIT License** — see [`LICENSE`](LICENSE).

`Copyright (c) 2026 KAME (https://github.com/Kame696)`

You can use, modify, distribute, and even sell KAME with the only requirement being to keep the copyright notice.

---

## 🎀 Credits & Star

Built by [**KAME**](https://github.com/Kame696). Engine refinement guided by real production log analysis against a real free-tier pool. Special thanks to every 429 that taught KAME something new.

If KAME made your agent less frustrating, drop a star ⭐ — it costs you nothing and helps others find this.

[**⭐ Star Kame696/kame-api-rotation-for-hermes on GitHub →**](https://github.com/Kame696/kame-api-rotation-for-hermes/stargazers)

---

<div align="center">

🐢⚡ **KAME v1.2.4** — *because round-robin was never enough*

**Bitcoin** — `36BGYhMEVFgY8PLGMVux93pjGt92KVM6dJ`

*4P1 R0T4T10N — 4FRE3D0M*

</div>
