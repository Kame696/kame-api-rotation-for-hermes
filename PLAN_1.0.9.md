# KAME 1.0.9 — the definitive plan, and what was built from it

> **Status:** built. Every item below is marked with what actually shipped.
> **Deadline:** 2026-08-24, noon.
> **Replaces** the earlier 1.0.9 plan, which was written by reading code.
> This one was written by measurement: every finding cites a log, a file, or a line.

---

## 0. Evidence base

Everything here was verified against the real host (Hermes 0.20.1, build
2026-08-21T14:45), not against a fixture.

| fact | how it was verified |
|---|---|
| the version actually installed was **1.0.2** | `venv/Scripts/python.exe` reads `plugin.yaml` as `1.0.2`; `dispatch_binding.py` had no `_Spinner`, no jitter, no `inspect.signature`; Hermes' `__pycache__/*.cpython-311.pyc` compiled from sources dated 16-17 Aug |
| the 1.0.8 deploy went into a shadow | the `python` on PATH (`pythoncore-3.14-64`, an app container) reads the **same path** and returns `1.0.8`. Its `AppData\Local` is redirected into `LocalCache` |
| 1.0.7 had once been installed correctly | session of 20 Aug 17:00, messages 26/28: dev and installed both `1.0.7`, identical |
| the dispatch points still exist | `agent/chat_completion_helpers.py:1207` and `:3114` |
| `on_session_reset` is a valid plugin hook | `cli.py:9114`, `gateway/slash_commands.py:327`, registry in `hermes_cli/hooks.py:182` |

---

## 1. Findings

### F1 — HTTP 410 rotates forever  🔴 critical

**User's symptom:** "sometimes it just seems to stop for no reason".

**Proof.** `logs/errors.log.1`, 46 occurrences, session `20260820_174033_090702`:

```
2026-08-21 10:02:50  kame: nvidia:z-ai/glm-5.2 other [410] — resting 20s, taking the next key
...
2026-08-21 10:18:53  kame: nvidia:z-ai/glm-5.2 other [410] ×1 more in the last 25s
```

Sixteen minutes of rotation, one key, no end.

**Cause.** `core/carousel.py:197` — `_TERMINAL_STATUS = frozenset({400, 404, 422})`.
`410 Gone` was not in it. It fell through to the final branch of `classify()` as
`OTHER_S, "other"`, rested 20 s, `select()` returned `EXHAUSTED`,
`_wait_for_recovery` slept 20 s, tried the same key, failed identically. A
closed loop.

Missing alongside 410: **405, 413, 415, 451, 501**.

**The storm filter hides it.** After three failures it collapses into `×N more
in the last Ns`, so the log *looks* calm while the turn burns.

### F1b — the host breaker that rotation cannot clear  🔴 critical (latent)

Found late, and the more dangerous of the two.

`chat_completion_helpers.py:733` — `_check_stale_giveup`:

```python
_giveup = env_int("HERMES_STREAM_STALE_GIVEUP", 5)
_streak = _stale_streak(agent)
if _giveup > 0 and _streak >= _giveup:
    raise RuntimeError(
        "Provider has been unresponsive (no response received) for "
        f"{_streak} consecutive stale attempts — aborting this call to "
        "avoid an indefinite stall. Switch models or start a new "
        "session, then retry."
    )
```

Its docstring: *"Raise immediately when the consecutive-stale streak is past the
give-up threshold — **no network attempt**, no stale-timeout wait."*

And Hermes' own comment (`:656`) says where it came from:

> *"A session wedged against an unresponsive provider hits the stale detector on
> every call and loops forever (observed: 494 consecutive failures over 3+ days)"*

**Why it matters to KAME.** The `_consecutive_stale_streams` counter lives on the
**agent**, not on the key. It resets only on a completed call or on a **provider
swap** (`switch_model` / `try_activate_fallback` / `restore_primary_runtime`).
KAME swaps the **key** and never the provider, so none of those fire.

The consequence, once the streak reaches 5:

1. KAME calls, `_check_stale_giveup` raises `RuntimeError` **before touching the network**
2. KAME classifies it: no HTTP status, falls into `other`, rests the key, takes the next
3. next key: raises identically, instantly, with no network
4. the whole pool is burned in milliseconds, `_wait_for_recovery` sleeps, repeat

An infinite loop **with zero network traffic**. Worse than the 410: a 410 at
least leaves a trace in the provider's error log. This leaves nothing.

**Honest about its status.** It was never proven in the logs. The observed streak
reached 1, three times, always reset:

```
2026-08-18 11:05:28  Interrupted provider wait counted as stale after 40s
                     with no output; consecutive stale attempts=1.
2026-08-19 20:03:02  ... after 49s ...; consecutive stale attempts=1.
2026-08-19 22:22:10  ... after 42s ...; consecutive stale attempts=1.
```

A latent hazard, not a proven cause of what was seen. But cheap to defend
against and expensive to discover in production — and Hermes itself documents
the three-day wedged session that motivated the breaker.

*(Side note: the 19 Aug 20:03:02 mark sits 25 seconds before the 20:03:27
ordinal refusal in F2. Probably related; nothing stronger is claimed.)*

### F2 — rewind / edit / resend is refused  🔴 critical — **and it is not KAME's bug**

**User's symptom:** *"restore to checkpoint, returning to a specific message,
still does not work"*.

**Proof.** `logs/agent.log.1` and `errors.log.1`, 8 occurrences between 18 and 20 Aug:

```
2026-08-19 20:03:27  tui_gateway.server: prompt.submit: REFUSED truncation due to
  ordinal mismatch for session 50567630 (ordinal=17,
  truncate_before_row_id_ordinal=44, truncate_before_row_id=2175,
  prefix_user_count=0). Stale truncate_before_user_ordinal detected.
```

The client says 17, the server says 44. And the drift **grows across a session**:

| when | client | server | drift |
|---|---|---|---|
| 18/08 02:15 | 5 | 4 | −1 |
| 19/08 20:03 | 17 | 44 | +27 |
| 19/08 21:12 | 20 | 52 | +32 |
| 20/08 00:08 | 15 | 60 | +45 |

The screenshot `composer_2026-08-19_23-03-44-371_657faf.png` is from 20:03 —
the same minute as the 19 Aug refusal.

**Cause.** `agent/conversation_loop.py:3495`. When a stream is cut mid-answer,
Hermes builds a `PARTIAL_STREAM_STUB` (`chat_completion_helpers.py:3090`) and the
continuation machine **appends two rows to the history**:

```python
interim_msg["_length_continuation_fragment"] = True   # the partial assistant row
continue_msg = {"role": "user",
                "content": _LENGTH_CONTINUATION_NETWORK_STUB,
                "_length_continuation_nudge": True}   # the synthetic user row
messages.append(continue_msg)
agent._session_messages = messages
```

The synthetic row is `role: "user"`. The server counts it in the user ordinal;
the client never displays it. Up to 4 continuations per turn
(`length_continue_retries < 4`). `_reconcile_client_ordinal`
(`tui_gateway/methods_prompt.py:173`) compares the two numbers and, on a
mismatch, **refuses with 4030** rather than truncating in the wrong place —
correct behaviour, wrong cause.

**Engineering verdict.** It is a Hermes bug. A plugin cannot fix the host's
ordinal arithmetic without rewriting the history, which would be worse than the
disease. What KAME does is **amplify** it: every partial stream the carousel
hands back to the host (the `progress.any` rule, which is correct) becomes one
more pair of rows.

**What went into 1.0.9:** not the fix — the *visibility*. See F2b.

### F2b — why the stream is cut mid-answer

`_build_partial_stream_stub` is used *"when the SSE stream ends without a
finish_reason after delivering content"*. Two possible origins:

1. the provider dropped the connection;
2. Hermes' own stalled-stream detector aborted it —
   `_derive_stream_stale_timeout` (`chat_completion_helpers.py:747`), base
   `HERMES_STREAM_STALE_TIMEOUT` = **180 s**, escalating to 240 s / 300 s by
   context size, with a floor for reasoning models.

The screenshot `composer_2026-08-21_00-12-23-705_c6cc78.png` shows the real
effect: a table cut in the middle of a cell, then `[System: The previous response
was cut off...]`, and the continuation came back as broken markdown — the table
became loose text. It is not only "it was cut": the continuation destroys the
rendering.

If the cause is (2), the remedy is raising `HERMES_STREAM_STALE_TIMEOUT` — the
host's setting, not the plugin's. `/kame` shows the effective value and says so.

### F3 — silence during the wait  🟠

**User's symptom:** *"it is horrible to watch the seconds run and nothing
happening"*. Screenshot `composer_2026-08-20_03-11-18-465_c279dc.png`: the agent
sitting at "Exploring 6 files / Searched files 1.5s", nothing progressing.

**Cause, three layers stacked:**

- `_Vigil.VIGIL_FIRST_S = 90.0`, then `VIGIL_REPEAT_S = 600.0`. Between the 90 s
  mark and the ten-minute mark: nothing.
- `_Spinner._THROTTLE_S = 10.0`, fixed, which makes a countdown impossible.
- Class state with no lock, shared between the main lane, the auxiliary lane and
  subagents. One swallows another's update.

*(This plan originally claimed a fourth cause — that the diff gate "loses" an
update held back by the throttle. Re-reading it, `_last_text` is only written on
an actual emit, which is correct. The claim was wrong and is struck.)*

### F4 — the settings do not exist for the user  🟠

`config_schema` is read and validated by the backend (`hermes_cli/plugins.py:766`),
but **no file in `web_dist` references `config_schema`**. The Plugins page only
turns a plugin on and off. KAME's switches existed only in `config.yaml` or in an
environment variable — invisible in practice.

### F5 — the manifest is still v1  🟡

`hermes_cli/plugins.py:659` defines `SUPPORTED_MANIFEST_VERSION = 2` and accepts
`manifest_version`, `api_version`, `license`, `homepage`, `tags`, `capabilities`,
`emits`, `listens`, `depends`. KAME's `plugin.yaml` declared none of them. A
requirement for publishing.

### F6 — a deploy can go into the shadow without saying so  🔴 operational

`tools/deploy.py` documented the problem in its docstring and only **printed**
the interpreter. It did not stop. That is how 1.0.8 disappeared.

---

## 2. Scope of 1.0.9 — and what shipped

### #0 — `deploy.py` aborts on the wrong interpreter ✅

Before any copy, `sys.executable` must live under the Hermes home, and must not
match any app-container marker (`localcache`, `windowsapps`,
`pythonsoftwarefoundation`). It exits 3 with an explanation otherwise. Proven by
running it under the exact interpreter that lost 1.0.8. It prints a restart
reminder on success.

*First item, because without it nothing else is verifiable.*

### #1 — 410 and the evidence rule ✅

**Narrow.** `_TERMINAL_STATUS = {400, 404, 405, 410, 413, 415, 422, 451, 501}`.

**Host breaker (F1b).** The `RuntimeError` from `_check_stale_giveup` became its
own category, `host_breaker`, checked **first** in both `classify()` and
`is_terminal()` — ahead of the auth reading of the same text — and terminal on
the spot. Detected on the stable phrase `"consecutive stale attempts"`, with
`"provider has been unresponsive"` as a second anchor. Two defences, not one:
the streak is also **cleared on every rotation**, mirroring what Hermes itself
does on a provider swap, so the breaker should never be reached at all. The
message passes Hermes' own wording through rather than dressing it up.

**General — and not a ceiling.** A0's eternal carousel is untouched
(`decisions/0002-eternal-carousel-no-timeout.md`). What was added is a rule of
proof, in the same spirit as the rest of the plugin:

> When **every** key in the pool has been tried at least once in this `run()`,
> all returned the same `(kind, status)`, and not one of them answered at all —
> that is proof the request is the problem, not the keys. Promote to terminal.

With fifteen keys the trigger is fifteen identical failures. With one key, one.
A quota wait can never trigger it: `server`, `timeout`, `per_minute`, `daily`,
`insufficient_quota` and `auth` are excluded by name, because those are things a
key causes on its own.

### #2 — live status ✅

Channel: `agent._emit_wait_notice` → `_touch_activity` + `thinking_callback`
(`run_agent.py:1071`). Documented as *"never raises"*; it becomes `thinking.delta`
on the desktop, spinner text in the CLI, and an activity description in the
gateway heartbeat. **Creates no message, moves no ordinal** — so it cannot make
F2 worse.

The shipped sentence, matching Agent Zero's word for word:

```
KAME API Rotation: 12/15 healthy
KAME API Rotation: 0/15 healthy — waiting — next key in 1m 23s (around 14:32:07)
```

`_Spinner` rewrite:

- a class-level `threading.Lock`, with the emit **outside** the lock;
- the throttle became a per-call cadence (`cadence_for`): 30 s while the wait is
  long, 5 s under a minute, 1 s in the last ten seconds — about 120 frames an
  hour rather than 3600;
- the wall clock is dropped below 60 s, where it says nothing;
- an unchanged line is redrawn after 30 s regardless, because the spinner is
  shared and the host overwrites it with its own activity;
- switchable off: `KAME_LIVE_STATUS_DISABLED=1`.

`_Vigil` stays as the lifecycle channel for very long waits, which is where it
is right.

### #3 — `/kame`, the panel ✅

A slash command, the same mechanism `/kame-keys` and `/kame-quota` already prove.

```
/kame              the panel: rotation now, health per provider:model with ETA,
                   counters (calls / rotations / recovered / surfaced / waited),
                   answers cut mid-stream, plugin version and load path, and the
                   four host variables KAME depends on
/kame get          every switch, its effective value, and where it came from
                   (environment > config > default)
/kame set <k> <v>  writes a KAME_* line into Hermes' own .env — live immediately
                   and permanent at once, since the environment outranks config
```

The `.env` write is surgical: only `KAME_*` lines are ever matched, a duplicate
assignment of the same variable is dropped (dotenv takes the last one), every
other line is copied through byte for byte, and nothing read is logged or
returned. `/kame` also reports the count of mid-stream cuts and names
`HERMES_STREAM_STALE_TIMEOUT` — the F2/F2b diagnostic, in the user's hand.

`/kame-keys` and `/kame-quota` stay as they are.

### #4 — patience for a silent stream ✅ (renamed)

Shipped as `silent_stream_patience_seconds`, not `first_token_patience`. The
implementation turned out to be Hermes' own `HERMES_STREAM_READ_TIMEOUT` rather
than a KAME watchdog aborting a call it does not own — which is what makes it
safe, and is the opposite of the `_StreamWatchdog` this project removed in 1.0.8.
Because it is a read timeout it also covers a silent stretch *mid*-stream, safe
by a different route: with `progress.any` set, the carousel hands the failure
back to Hermes instead of rotating.

**Off by default** (0). A floor of 5 s applies above zero, with a warning.

### #5 — `on_session_reset` ✅

Declared in `hooks` and `provides_hooks`. The handler clears `_Spinner`, and
only for the session named in the payload (`hermes_cli/hooks.py:182` always
names one; an unlabelled reset clears the lot, because a stuck line everywhere
is worse than one redraw everywhere).

Two things it deliberately does **not** clear. Carousel cooldowns: a spent quota
is still spent after a `/reset`. And `StormFilter` — the plan originally had a
`reset()` for it, and the cross-session review took it back out. The filter
describes a *provider outage*, not a chat, and it guards the log, which is one
file per process that a reset does not clear either; clearing it on one
session's reset restarted the collapse for every other session still living
through the same outage.

### #6 — manifest v2 ✅

```yaml
manifest_version: 2
api_version: "0.20"
license: MIT
homepage: https://github.com/Kame696/kame-api-rotation
tags: [api, rotation, quota, rate-limit, resilience, multi-key]
```

### #7 — bind-by-signature, for real ✅

The old `if len(params) < 2` never fired. It now requires the second parameter to
still be named `api_kwargs` — the dict the wrapper rewrites — and steps aside
with one log line if it is not. `agent` is deliberately **not** pinned: it is a
generic word whose rename would not mean the contract had moved.

### #8 — speed: the host's own retries ❌ REVERTED in 1.0.10

Built here, shipped in 1.0.9, and wrong. The reasoning was: `HERMES_STREAM_RETRIES`
defaults to 2, so a dead key costs three round trips before the carousel may move —
Agent Zero's own 1.0.4 bug, measured there at 30-40 s per failure.

Hermes' variable is a different thing wearing the same word. It is consulted once
(`agent/chat_completion_helpers.py:4693`) and acted on **only in the transient
network branch** — timeout, dropped connection, SSE parse error, empty stream —
where the repair is a fresh socket to the same endpoint. A 429 or a 401 arrives as
an `APIStatusError`, never enters that branch, and already reached KAME on the
first try. Zeroing it therefore bought nothing on the failures the carousel exists
for, and spent the one recovery that was free and invisible: a blip mid-answer
ended the stream, and Hermes continued it with the row the user can read,
`[System: The previous response was cut off by a network error mid-stream…]` —
which is the complaint that sent us back to the source.

`host_tuning.py` and `host_retry_suppression_disabled` are deleted in 1.0.10, and
`tools/host_assumptions.py` fails if an assignment to a `HERMES_STREAM_*` variable
ever comes back. The lesson is the one the rest of this plan is built on and this
item skipped: read the host's use of a name before trusting the name.

---

## 3. Out of scope — and why

| item | reason |
|---|---|
| fixing the rewind ordinal (F2) | host code. A plugin rewriting history would be worse than the disease. 1.0.9 **measures and shows**; the fix is an issue for Hermes |
| rotating on a partial stream | reintroduces the answer printed twice. It was item #1 of the old plan; it stays refused |
| moving `_remove_shims` out of the `finally` | would leak the shims on the exception path. Item #2 of the old plan |
| caching `getattr` in the spinner | absorbed by the `_Spinner` rewrite |
| pruning the `_pools` dict | each entry is tiny. Deferred |
| a shared error dictionary A0 ↔ Hermes | good idea, 1.1.0 scope. See §5 |

---

## 4. Execution order — as run

1. **#0** deploy assert ✅
2. **#1** 410 + host breaker + evidence rule ✅
3. **#8** host retry suppression ✅
4. **#2** live status ✅
5. **#5** `on_session_reset` ✅
6. **#7** bind by signature ✅
7. **#4** silent-stream patience ✅
8. **#6** manifest v2 ✅
9. **#3** `/kame` ✅
10. tests for each item, full suite ✅ — 1200 passing
11. host tripwires extended to 16 facts ✅
12. CHANGELOG, README, `.zip` ✅
13. deploy + restart + live test — **pending the user's word**

---

## 5. Agent Zero parity and shared numbering

A0 is at 1.0.9. Hermes reaches 1.0.9 with this delivery. From there:

- **1.0.9** — one per framework, the same number by useful coincidence.
- **1.1.0** — a joint release. What is worth porting from Hermes to A0: the new
  terminal statuses, the evidence rule, and the error dictionary.
- The error dictionary becomes a **shared, machine-readable file**
  (`kame-errors.yaml`): patterns, quota windows, terminal statuses, backoff
  ladders. Both plugins read the same file. An AI opening the folder understands
  how to act without reading Python. `PARITY.md` stays the ledger of
  what-exists-where.

What does **not** converge, correctly: the binding. A0 reimplements the request;
Hermes wraps the host's dispatch. Different frameworks.

---

## 6. Verification

- [x] deploy refuses the wrong interpreter, proven by running it under the PATH python
- [x] 410 leaves the carousel instead of rotating (unit-proven; the live
      reproduction against `nvidia:z-ai/glm-5.2` is part of the live test)
- [x] a unanimous pool promotes to terminal; quota never promotes
- [x] the give-up `RuntimeError` leaves at once as `host_breaker`, without burning the pool
- [x] the stale streak is cleared on every rotation, so the breaker is not reached
- [x] the countdown is drawn at a cadence that follows the ETA, ~120 frames/hour
- [x] `/kame` opens, `/kame set` writes, `/kame get` shows the value's provenance
- [x] the `.env` write leaves every non-`KAME_` line byte-for-byte unchanged
- [x] silent-stream patience is off by default and floors at 5 s above zero
- [x] `on_session_reset` clears the spinner for the session named in the
      payload, and does **not** clear cooldowns or the storm filter
- [x] nothing the plugin keeps per class is per *conversation* by accident:
      `_Spinner` keyed by `agent.session_id` with 64-entry LRU, the carousel
      and the settings shared on purpose, `/kame` labelling its counters as
      the process's
- [x] full suite green — 1200 tests
- [x] 16/16 host assumptions holding against the installed Hermes
- [ ] `hermes plugins doctor` clean with the v2 manifest — needs the install
- [ ] 24 h of real use with no `other [4xx]` rotating

A green test is not "done". Done is surviving real use.
