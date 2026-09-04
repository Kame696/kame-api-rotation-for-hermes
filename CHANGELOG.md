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
| **1.6.0.3** | It was arguing with the provider | Google spent 46 minutes telling this plugin exactly how long to wait — 340 times, never once above **59.8 seconds** — and KAME held keys for **five minutes** on ten of them, then sat still for 468 seconds across 33 waits because it had benched its own pool that far out. The cause was one branch: on a throttle it *multiplied* the provider's stated number by two per repeat, while the two ladders beside it for daily quotas and 5xx have always treated their number as a floor. A key refused twice in a row is not evidence the provider lied; on a rolling window it is the ordinary case, and the provider restates a fresh number every time. **A stated number is now obeyed exactly.** When nothing states one — 232 of those 400 refusals arrived as the terse `Resource has been exhausted`, with no number at all — the invented ladder still climbs, but never past the longest wait that provider has actually asked for. And the field that decides everything is readable again: both of Google's free-tier quotas report the *identical* metric name and differ only in `quotaId`, which Hermes' adapter parses and throws away — so a **daily** cap arriving with Google's misleading `retryDelay: 1s` used to be re-probed every twenty seconds for the rest of the day. KAME now reads the untouched response body the adapter kept beside it, sees `...PerDay...`, and rests the hour |
| **1.6.0.2** | Saying nothing was costing an hour | A throttle the provider named but did not size left this plugin silent, and silence is not neutral: Hermes' own fallback for an unsized 429 is **one hour per key**, so the commonest refusal there is — nineteen of them in one three-minute run — took a credential out for an hour on evidence that named no duration at all. It is now twenty seconds. Two more places where weaker evidence was overruling stronger: the advice **Hermes itself** appends to a Google error was being read as though Google had written it, turning a stated seven-second wait into an hour at account scope; and the SDK's exception class was consulted *before* the provider's own sentence, so a key Google called invalid could never be retired and a stated five-minute wait was thrown away. No behaviour was added — three signals were put back in the order of how much they are worth. Plus four things found by using it: with three Hermes processes sharing a home the panel was renaming which one it described on every heartbeat, so the Events tab flipped between 150 rows and none — it now follows the process actually routing your calls, and says whether KAME is alive at all; a dropped stream no longer takes half a two-key pool out for thirty seconds; a neighbouring profile's row says whether its KAME is doing any work; and "Wait for the first token" finally names a number to try |
| **1.6.0.1** | Two Hermes, one file — and a refused key stopped coming back for ever | The Desktop and the gateway both write the plugin's status file and each overwrote the whole of the other's, so the panel flickered between two readings a release apart and the Settings form rebuilt itself under the cursor; the file now holds a section per process, and the panel names the others. A credential the provider refused rests twenty seconds instead of an hour and is offered last, and one the provider names dead leaves rotation altogether, because over-benching a healthy key costs an hour while under-benching a dead one costs a request that fails in milliseconds. Events finally records the rotations themselves, not only the failures. Settings is half the height, with a Refresh that re-reads the `.env` for real. And the diagnostic that reads a real run is `/kame doctor` inside the plugin instead of a script in the repo |
| **1.6.0.0** | The plugin stopped being right in private | Gemini's refusals are read from where Hermes actually files them, so a 21-second throttle is no longer benched as a spent account for a whole day; the cooldown KAME works out finally reaches the pool instead of being dropped one hop short; the row holding several keys can no longer be sent as a key; a cut answer keeps going while any key is still adding words; a dropped tool call is asked of another key before Hermes tells the model its call was too big; and the panel says, in one line per provider, what KAME can actually see |
| **1.5.0** | The rest of the evidence, and the gate that was never run | The class of the exception is read at last, so a failure with no status and no body is sized instead of guessed; two payloads 1.4.0 wrongly claimed are given back to the host; the Settings panel can no longer freeze |
| **1.4.0** | The evidence was on the exception all along | The installed plugin had no engine and nothing said so; cooldowns are sized from the provider's own fields instead of its prose; the Events tab says where every wait came from |
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

## [1.6.0.3] — 2026-09-04

**In short.** The provider kept saying how long to wait, and this plugin kept
doubling it. Plus the one field that separates a per-minute throttle from a
day-long one, which the host parses and discards, read back off the response
it was discarded from.

### What the log said

1.6.0.2 loaded at 21:03:54 on 2026-09-03 and ran until 21:49. In that window
Gemini refused **340 times**, and every refusal that carried a number carried
a freshly computed one:

| what Google asked for | |
|---|---|
| shortest | 1.5s |
| median | 41.1s |
| **longest** | **59.8s** |

KAME rested keys for **5m 0s** on ten of them, and for 1m 4s, 1m 7s, 1m 10s
and 1m 34s on others — every one of those longer than the provider had *ever*
asked. With the pool benched that far out the agent then sat in the wait loop
for **468.7 seconds across 33 waits**, on 103 calls. That is the stall, and it
was self-inflicted.

### One branch, out of step with the two beside it

`Carousel._escalate` has three ladders. Two of them read like this:

```python
grown = BASE * (2 ** (strikes - 1))
return min(max(delay, grown), cap)      # daily, and 5xx
```

The number the provider stated is a **floor**; the ladder only lifts a refusal
nobody sized. The third one read like this:

```python
grown = max(delay, 1.0) * (2 ** (strikes - 1))
return min(grown, RL_BACKOFF_CAP_S)     # throttles
```

It *multiplied* the provider's own number — and it is the branch that runs on
the commonest failure there is. 8 × 53.8s is 430s, capped to the 300s ceiling:
the 5m benches, exactly.

Repeating a throttle is not evidence that the provider's number was wrong. On
a rolling window it is the ordinary case — the key is asked again while its
window is still full, and the provider answers again, correctly, with a new
number. Widening on that reads a **restatement as a refutation**.

Measured refutation already has an owner and keeps it: the journal counts
deadlines that were waited out in full and refused anyway, and
`escalate.stretch` widens only after two of them. That mechanism is untouched.
An earlier draft of this release capped its unnamed-window ceiling at five
minutes too, and `test_a_widened_bench_never_outlasts_a_day` — which has been
in the suite since escalation shipped — refused it. The test was right: a
12-hour deadline refuted four times has earned a day-long ceiling. The cap
came back out.

**A throttle the provider sized now rests for exactly what was stated**, every
repeat, with the one-second floor still under it so a sub-second reply cannot
spin. A number *above* the ceiling is obeyed rather than clamped down to it,
because clamping re-probes into a window the provider just said is spent.

### What may be invented, and how far

232 of that run's 400 refusals were the terse form, with no number anywhere:

    Gemini HTTP 429 (RESOURCE_EXHAUSTED): Resource has been exhausted
    (e.g. check quota).

The ladder exists for exactly this, and still climbs 1s, 2s, 4s … But the
terse message is not a different condition — it is the same condition worded
shorter, and the other 168 refusals had already said, 168 times, that this
window closes in under a minute. A plugin holding a key for five minutes while
sitting on that evidence is not being careful.

**The invented ladder is now bounded by the longest wait the provider itself
has asked for**, per `provider:model`. Nothing rests for that number; it is a
ceiling on a guess. Where the provider has never stated one, the old constant
still applies.

### The field the host parses and drops

Google's two free-tier quotas report the **identical** metric —
`generativelanguage.googleapis.com/generate_content_free_tier_requests` — and
are told apart only by `quotaId`:

| quotaId | what it means |
|---|---|
| `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` | seconds |
| `GenerateRequestsPerDayPerProjectPerModel-FreeTier` | the rest of the day |

Hermes' `gemini_http_error` parses the payload, keeps four fields of it
(`status`, `reason`, `metadata`, `message` — the `google.rpc.ErrorInfo` slice)
and drops the whole `QuotaFailure` block. So `quotaId` never reached KAME, and
every throttle in the owner's log classified as `rate_limit [unknown]`.

That matters because of something Agent Zero learned in production and Google
documents in its own forum: a **daily** exhaustion can arrive with
`retryDelay: "1s"`. Payload from that thread, verbatim in shape —
`quotaValue: "250"`, daily quota gone, retry in one second. KAME's rule for
distrusting a short number on a long window has existed since 1.4.0; it had no
window to apply it to.

The adapter keeps the whole `httpx.Response` on the exception beside the four
fields it saved. **KAME now reads it.** Same payload, same host shape:

| | window | rest | why |
|---|---|---|---|
| 1.6.0.2 | `unknown` | none stated → the 20s floor, all day | "a per-credential counter is spent" |
| **1.6.0.3** | **`per_day`** | **3600s** | *ignoring misleading 1s hint* |

Attribute reads only, every one guarded. `.text` on a streaming response that
was never read raises immediately and does no I/O, so this cannot block a call
or consume a body somebody else needs — when it raises, the verdict is exactly
what 1.6.0.2 produced. A per-minute payload still rests for the stated 41.3s.

### Gates

| gate | result |
|---|---|
| `pytest tests` | **1696 passed** (21 new) |
| `tools/host_corpus.py` | 130/130 — *KAME changed nothing* |
| `tools/host_prose.py` | both host footers stripped, and silent alone |
| `tools/host_assumptions.py` | every host fact still holds |
| `node tests/ui_reconcile.mjs` | all checks passed |
| mutation | 5 new rules broken one at a time; **all 5 turn the suite red** |

---

## [1.6.0.2] — 2026-09-03

**In short.** Three places where weaker evidence was overruling stronger, and
the most expensive of them was **saying nothing at all**.

### Silence is not neutrality

On 2026-09-03 the owner's Hermes made 64 calls and rotated 133 times, and
asked why the panel was showing credentials held for minutes when the log said
KAME was resting them for one and five seconds.

Both readings were true. There are two clocks and only one of them was ever
checked:

| | who reads it | what an unsized throttle means |
|---|---|---|
| the rest between attempts, inside a turn | `dispatch_binding` | `_escalate`'s one-second floor |
| the bench on the credential, across turns | `agent/credential_pool` | `_exhausted_ttl` — **one hour** |

Nineteen of that run's refusals were this exact string:

    Gemini HTTP 429 (RESOURCE_EXHAUSTED): Resource has been exhausted
    (e.g. check quota).

No stated delay, no quota id, no metric. `classify` recognises it correctly —
a spent per-credential counter, rotate — and deliberately leaves `reset_at`
unset, with a comment saying so: *"bench it for nothing"*. There is no "bench
it for nothing" in this host. `_exhausted_until` reads the deadline KAME
supplied and, finding none, applies `EXHAUSTED_TTL_429_SECONDS = 60 * 60`
(`agent/credential_pool.py:125`).

So the branch written to cost a key nothing cost it the most this host can
charge, on the commonest refusal there is.

**A throttle KAME names but cannot size now benches for twenty seconds.** Not
in the verdict — putting a number there would rebuild the escalating ladder
1.5.0 removed for NVIDIA, whose burst limits clear in seconds — but at the one
seam that owns the second clock, `PoolBinding._carry_deadline`. The whole unit
suite passes unchanged, which is the evidence that the classifier's contract
was not disturbed.

Twenty seconds for the same reason a refused key rests twenty: re-probing
costs one failed request, over-benching costs the use of a healthy key, and
the two are not close. It is an opening, not a ceiling — `core/escalate.py`
doubles a bench that proves short, so a window that really is long is found by
measurement in a couple of refusals instead of guessed at an hour on the first.

### The host's own voice, read as the provider's

`agent/gemini_native_adapter.py` builds a Google error message and then
appends its own advice to it — free-tier guidance at `:907`, legacy-key
guidance at `:913` — before any hook sees it. It arrives welded to Google's
sentence with nothing to tell them apart.

Two of `_BILLING_PATTERNS` match that advice, and the second is the
instructive one:

    "...so the free tier is exhausted in a handful of messages..."
    "...regenerate the key in a billing-enabled project..."

The first pattern was written for Alibaba Model Studio, where "the free tier
of the model has been exhausted" is a real and decisive account fact. Hermes
says the same words about the *user's* situation. No pattern can separate
them, because the difference is not in the words — it is in who wrote them.

Billing is the most expensive verdict this plugin has: an hour, at **account**
scope, with the re-probe and the escalation both disarmed by the reason
itself. Measured, on the owner's own payload:

| message | verdict before | after |
|---|---|---|
| Google's 429, `Please retry in 6.89161299s` | 6.9s | 6.9s |
| the same, with Hermes' footer appended | **billing, 1 hour, account** | 6.9s |
| the footer *alone*, on an HTTP 500 | **billing, 1 hour, account** | declined |

The last row is the point: the footer needed no help from the provider's half
of the string. It was appended 340 times in one week of the owner's logs.

The host's advice is now removed before a single pattern reads a word.
`tools/host_prose.py` checks the anchors against the installed Hermes and
**discovers** any new constant the adapter welds onto a message, so a reworded
footer is a failing gate instead of a silent return of this bug.

### The SDK's class name is the weakest thing in the payload

`read_exception_class` sat as a third equal beside the field lookup and the
status lookup, which gave it the power to end the classification for the four
families KAME does not act on — before a single pattern read the provider's
own sentence:

| payload | before | after |
|---|---|---|
| `400 "API key not valid. Please pass a valid API key."` + `BadRequestError` | declined | `auth_permanent` |
| `402 "Usage limit reached, try again in 5 minutes"` + `APIStatusError` | declined | 5 minutes |

The first is Google's only way of saying a key is revoked, so a genuinely dead
credential could never be retired. The second is the payload 1.5.0 shipped
`look_up_status`'s billing override for — silently dead ever since, because
the class name got there first.

The class is still read, twice, in the order its evidence deserves: it may
*add* a family KAME acts on (a bare `RateLimitError` with an empty body is
still a throttle), and its four "stay out of it" families are consulted last,
where nothing else spoke. A `BadRequestError` wrapping "Invalid JSON payload"
is still left alone. The difference was always in the payload, never in the
class.

### The rule underneath all three

Stated once, and now enforced:

    1. a machine-readable field the provider filled in
    2. a number the provider stated
    3. the provider's own sentence
    4. the HTTP status on its own
    5. the SDK exception class name
    6. text the HOST appended  <- not evidence at all

### What did not change, and why

A first attempt also claimed any bare 429 as a twenty-second throttle. The
host's own corpus failed three cases — Anthropic's `usage_limit_reached`,
a bare "Monthly quota reached.", and the long-context tier notice — all of
which Hermes reads better than a status code can. It was removed. A status
code alone is never evidence that the host got something wrong.

The per-minute escalation cap stays at 300s. `stretch()` did not fire once in
the run that produced this release — zero "measured short" lines in
`agent.log` — so there is no measurement to move it with, and a cap changed
without one is churn.

### Four things the owner found by using it

Reported after a live afternoon on this build, and all four are the same shape:
KAME was right and had no way to say so.

**The Events tab could not tell "quiet" from "stopped".** It records failures
and rotations, so an afternoon where nothing failed draws exactly what a plugin
that stopped running draws — an empty list. The list was read as frozen,
cleared, and Hermes restarted twice; the log for that window shows KAME working
throughout. The counts alone do not settle it either: *53 calls* reads the same
a second later and an hour later. The tab now ends with one line saying whether
KAME is installed, how many calls it has routed, **and when the last one was**,
and it warns when the reading itself is stale — a Hermes that died mid-turn
leaves a snapshot that still looks live. `/kame events` says the same thing.

**The Events tab really did stop showing things — and the cause was not the
tab.** The first read of this was wrong and is worth writing down as such: the
list had been cleared by hand, so an explanation existed, and the heartbeat
above was added on the assumption that the screen was merely quiet. It was not.
The panel picks which process's section to render with `ownSection`, and that
function asked for "the one whose role is `desktop`, else the freshest".
`state.role()` reads `sys.argv`, and the Desktop on this machine starts its
backends in a way that matches neither `serve` nor `--profile` — so **three
live processes** all reported the generic `hermes`, the first half of the rule
never matched, and the second named a different one on every heartbeat. Read
straight off the owner's file:

    pid=13496  role='hermes'  events=150
    pid=16048  role='hermes'  events=0
    pid=6780   role='hermes'  events=3

One second the tab showed 150 rows, the next none, the next three. No restart
could clear it and no new session could either, because nothing was stuck.

Three rules replace it. Only sections still being written are considered — a
process that has exited leaves its section on disk until some Hermes starts
again, and the old rule would happily have picked one. The section already on
screen stays there while it lives, so a neighbour saving cannot move the page.
And when a fresh choice is needed it goes to the process that most recently
**routed a call**, not the one that most recently wrote: `last_call_at` moves
only on real traffic, so it follows a model or profile switch and cannot fire
on a timer. A `gateway` never wins — it serves the phone.

Reverting `ownSection` to the old rule fails four of the five new checks in
`tests/ui_reconcile.mjs`; removing the liveness filter fails the fifth.

**A dropped stream took half a two-key pool out for thirty seconds.** 1.1.3
exempted a pool of *one* from the drop rest, reasoning that a rest whose only
job is to send the next request elsewhere buys nothing when there is nowhere
else. That reasoning does not stop at one. Measured on the owner's NVIDIA pool,
which has two keys:

    kame: nvidia:moonshotai/kimi-k3 key:b65dd9 cut the answer after 568
          character(s) — resting it 30s and continuing on another key

The answer was fine — it continued on the other key and arrived whole. The half
minute afterwards was not: one refusal on the survivor and the carousel has
nothing to pick. Below three healthy keys the drop rest is now **five seconds**,
still long enough to move the next selection, which is the entire stated purpose
of it. Three or more keeps the full thirty.

**A neighbouring profile's row said nothing about whether its KAME was
working.** "5 of 5 ready" is a statement about keys and reads identically on a
profile whose plugin never registered. Each row now also carries that process's
call count and when it last routed one, or says the plugin is not installed
there.

**"Wait for the first token" gave advice with no number in it.** It said what
the setting does and that zero is right for almost everyone, and stopped — so
anyone who *did* have a provider that accepts a request and then hangs had to
guess between the 5s floor and Hermes' own 120s. It now recommends **60**: clear
of the slowest honest first token, and half of what Hermes would otherwise spend
before giving up.

### What was investigated and left alone

**Rotating on a 503 was already right, and already happening.** The owner's
point — a 503 is not the key's fault, so keep trying — is what the code does,
and the log is the proof:

    17:32:54  503 key:2c604b — resting 10s
    17:33:24  503 key:aa51d2 — resting 5s
    17:33:54  503 key:da0c7b — resting 5s
    17:33:56  answered on attempt 4

`classify` declining a 5xx does not cost the credential anything either: the
host benches only on `billing`, `rate_limit` and `auth`
(`agent_runtime_helpers.py:1235`), so no 503 has ever produced a quarantine.

**A malformed table could not be attributed to this plugin.** The exported
transcript carries the whole 4,680-character answer; only the separator row is
wrong — ten cells where every other row has nine. No cut or stitch was recorded
for that session.

### Gates

| gate | result |
|---|---|
| `pytest tests` | **1672 passed**, 0 failed |
| `tools/host_corpus.py` | **130 passed** — "KAME changed nothing in the host's own corpus" |
| `tools/decisions.py --check` | 9 decisions moved, every one of them named above |
| `tools/host_prose.py` | both footers found, stripped, and declined on their own |
| `node tests/ui_reconcile.mjs` | **10 checks** — five of them new, covering which process the screen describes |
| mutation | all **5** Python rules and both panel rules turn their suites red when deleted |

---

## [1.6.0.1] — 2026-09-02

**In short.** 1.6.0.0 was correct about one Hermes. This machine runs two.

### Two processes, one file

A Hermes home is served by more than one process and usually is: the Desktop
runs `hermes_cli.main --profile <name> serve`, the gateway that the phone app
talks to runs `hermes_cli.main gateway run`. Both load this plugin. Both use
the same credential pool. Neither knew the other existed.

The status file the panel reads was written as though one process owned it, so
each wrote all of it and erased the other. Measured on the owner's install:
**40 reads over 20 seconds returned 26 documents from one process and 14 from
the other**, one of them a whole release behind. The panel re-reads once a
second and re-renders whenever the bytes change, so it re-rendered *every
second* — which is exactly the fault 1.2.3's byte comparison was written to
prevent, defeated by there being two writers. On the Settings tab that is a
form rebuilding itself under the cursor.

The document now holds **one section per process**, each carrying that
process's own view. The panel reads the section belonging to the Hermes it is
part of, compares *that* rather than the file, and lists the others by role,
version and build. Sections whose process stops writing age out.

There is no compatibility copy at the top level. A panel older than this
schema refuses the document by its number and says so in a sentence, which is
a better failure than half-rendering somebody else's process.

The neighbours are not a curiosity. They share the credential pool: a key the
gateway is resting is a key the Desktop cannot use either, and a gateway still
running last month's build is why a fix that is definitely installed is
definitely not working on half the traffic.

### Refusing a *model* is not refusing the key

Found by reading the two classifiers side by side, not by any failing test.

`core/classify.py` is the evidence-first classifier Hermes calls; `core/carousel.py`
holds the table-driven fallback. Both had an opinion about a 403 that says *this
key may not use this model* — a suspended project, an API never switched on, a
model outside the tier the key pays for — and the two opinions disagreed:

| | bench | may retire the key |
|---|---|---|
| `carousel.DENIED_REST_S` | an hour | no |
| `classify.DENIAL_BENCH_SECONDS` | twenty seconds | — |

So the same refusal cost the same key two different amounts depending on which
classifier answered first. The test named for catching exactly this,
`test_the_evidence_first_classifier_agrees_with_the_table`, was asserting one
side against the other's *constant* and passing while they differed.

The worse half was not the number. `Verdict.reason` is coerced to a
`FailoverReason` member on the host side and the whole classification is dropped
if it will not coerce, so a denial has to travel as `auth` on the wire — and
`auth` is in `RETIRING_KINDS`. A denial therefore arrived at the dispatch loop
indistinguishable from a bare 401, and **three refusals from one model the key
was never entitled to retired a credential that worked everywhere else**.

Measured, through the real dispatch translation, six denials on one key:

    before   kind=auth     retired=True    <- a working key, gone
    after    kind=denied   retired=False

Fixed in four places:

* One constant, `quota.DEFAULT_DENIAL_BENCH_SECONDS`, read by both classifiers.
  Twenty seconds, and the hour is not lost — `denied` is on the doubling ladder,
  so a permission that really is permanent reaches an hour by itself while a plan
  somebody upgrades comes back in the seconds it actually took.
* `Verdict.kind` carries KAME's own finer name beside the word Hermes accepts.
  It never leaves the plugin.
* `denied` joins `_NEVER_PROMOTED`, which *preserves* behaviour rather than
  changing it: it used to be covered by the `auth` entry.
* The log line and the Events row stopped calling it an invalid credential. That
  gate was a second reading of the error text rather than the verdict already
  decided one screen above, so a key that simply is not entitled to one model was
  announced as "not a valid credential — replace it in Settings", which is false
  twice over. It now says the key may not use this model and is untouched
  everywhere else, under its own event kind.

New gates: a denial run through the real `_on_failure` six times must not retire
the key; the doctor's hand-written rest table must agree with the code; and the
panel's event vocabulary is now derived from `events._KINDS` instead of a list
of five names typed beside it — that list was passing for every kind it had not
been told about, and `denied_model` was one.

### Two texts still said five minutes

The Settings help under **Daily quota cooldown** and a comment in
`Carousel.select` both described the refusal bench as five minutes, left over
from before that number was measured down to twenty seconds. The Settings one is
read by a person deciding what to type.

### A key the provider refuses leaves rotation

The owner asked the question this release should have started from: *chave
inválida não deveria ser retirada do pool?* — and then drew the distinction
that makes the answer workable: *recusar para o modelo não significa que a API
não funciona.*

Both halves are right, and the classifier **already knew the difference**.
`classify` reaches `auth_permanent` only when the provider used the words —
"API key not valid", "invalid api key", "key is no longer valid" — and a bare
401 lands on the ambiguous `auth` instead. That is the 1.4.0 fix, which took
`"unauthorized"` out of the invalid-key vocabulary after twenty-one healthy
keys were quarantined for an hour each on expired tokens a second from
refreshing.

`dispatch_binding` then flattened both into `"auth"` on the next line. The
strongest evidence this plugin can gather was worked out and thrown away.

Three kinds now, and each is handled as what it is:

| Evidence | What it means | What happens |
|---|---|---|
| `revoked` — the provider used the words | This is not a key | **Out of rotation on the first one** |
| `auth` — a bare 401, no explanation | Could be a token mid-refresh, a proxy, an incident | 20s, offered last, out after **3 in a row** |
| `denied` — "this key may not use *this model*" | The **pairing** is refused, not the key | 1h, per-model, **never** retired |

That last row is the owner's own distinction and it is the expensive one to
get wrong: a key refused for one model may be the healthiest credential in the
account on every other, and retiring it would throw away a working key over a
permission that was never about the key. The hour is also the one refusal
where a long wait is honest rather than a guess — nothing about an
authorisation moves on its own — and it costs nothing, because the carousel's
health is per `provider:model`.

**Retiring is not deleting.** KAME does not write credentials, and that has
not changed. A retired key keeps its row, keeps its place in your config, and
comes back the instant a call on it succeeds, its value changes, or the pool
is cleared. Three consecutive refusals with *no successful call in between* —
any success, or any other kind of failure, resets the count to zero.

**Retiring outranks being ready, and that is what makes it worth having.** The
demotion added earlier in this release handles the easy case, where a working
key is sitting there unused. The case it gets wrong is the one that actually
happens: the working key is resting twenty seconds off a throttle, the refused
key's own rest has lapsed, so the refused key is the only thing "ready" and
the call goes to a credential we have already been told is dead. That spends a
request and hands back an error where waiting twenty seconds would have handed
back an answer. A retired key is now removed from consideration even when that
leaves nothing ready. There is a test that fails without it.

**The escape hatch is the whole safety argument.** The rule applies only while
some key is *not* retired. If every key in a pool has been refused, every key
is offered again — the request goes out and the provider's own error comes
back, exactly as it would with no plugin installed. Retiring can never take a
pool to zero, so the worst case of a wrong verdict is no worse than not having
the rule at all.

The panel and `/kame doctor` now say two different sentences, because they ask
the reader for two different things: *"1 key has left rotation … nothing was
deleted, paste the replacement over it and it comes back by itself"*, and
*"1 key was just refused and is still being tried — one refusal is not proof."*
The old banner said "until then every turn spends an attempt discovering the
same thing", which stopped being true.

### A refusal is not a clock

The owner watched four keys sit out an hour each and said the useful thing:
*não tem problema tentar novamente, então 1 hora é muito grande.* The instinct
was right, and it generalises past the case that prompted it.

KAME's cooldowns divide in two, and until now both got the same hour:

* **Clocks** — a per-minute throttle, a daily cap, an account allowance. The
  provider is metering time, and only time helps.
* **Refusals** — a 401, a revoked key, a 403 saying this key may not have this
  model. The provider is describing the credential. Waiting fixes nothing.

The reasoning for giving a refusal the daily hour was that since waiting
cannot repair a refused key, the length hardly matters. It matters, because
the two ways of being wrong are not the same size:

| Wrong in this direction | Costs |
|---|---|
| Too long — the provider had an incident, or the 401 was transient | a **healthy key, for an hour** |
| Too short — the key really is dead | **one request**, refused in milliseconds, never metered |

A refusal now rests **twenty seconds**, and it got there in two steps worth
recording. The first cut was to five minutes — a number invented here. Twenty
is the number the escalation ladder in `carousel._escalate` applies to this
kind anyway, and measuring the two side by side showed the invented one was
doing no work:

```
base  20s ->  20  40  80 160 320 640 1280 2560 3600 ...
base 300s -> 300 300 300 300 320 640 1280 2560 3600 ...
```

The ladder's own floor governs either way and both reach the hourly re-probe
at the same point. All the larger base bought was flattening the first four
strikes at five minutes — precisely the window in which a re-check is most
likely to find a transient refusal already cleared. The owner asked whether
five minutes was "muita coisa ou pouco"; the honest answer was that it was a
guess sitting on top of a rule that already had an answer.

Without the demotion the shorter bench would have been *worse* than the hour
it replaced: a key that answered 401 comes back with an empty request window
and the oldest `last_used` in the pool, which is precisely the profile the
least-loaded/least-recently-used rule reaches for. The one key known not to
work would have been the first one tried, every five minutes. There is a test
that fails when the demotion is removed.

**The rest of the table was audited against Agent Zero's and is unchanged:**

| Refusal | KAME for Hermes | Agent Zero |
|---|---|---|
| timeout | 3s, no ladder | 3s |
| 5xx | 5s, doubling to 90s | 5s, doubling to 90s |
| rate limit with a stated delay | the provider's number | the provider's number |
| rate limit with none | 20s, doubling to 5m | 20s, doubling to 5m |
| daily / out of credit | 1h | 1h |
| a dropped stream | 30s | — |
| bare 401, no explanation | **20s, offered last, out after 3** | 1h |
| provider named the key dead | **out of rotation at once** | 1h |
| this key may not use this model | 1h, per-model, never retired | 1h |

Nothing rests an hour on a first refusal any more except a quota that is
genuinely spent, and that is now a test rather than a claim.

### Events shows the rotation, not only the failure

`switch`, `recovery` and `wait` have been in the event vocabulary since 1.1.1
and **not one of them was ever written**. The tab recorded failures only, so a
rotation engine doing its job produced a screen of red with no record of
anything working.

All three are recorded now: which key took over, whether it answered and after
how long, and every wait with the pool state that caused it. The buffer went
from 50 rows to 150, because one incident is roughly three rows where it used
to be one.

The tab was rebuilt around that split. Three tallies — everything, KAME
rotating, providers refusing — that are also the filter; a coloured rail per
row saying which half a row belongs to; the classifier's vocabulary
(`per_minute`, `auth_permanent`, `insufficient_quota`) rendered in words a
reader who did not write this can read; relative times, with the wall clock on
the hover; and a legend naming all nine kinds, because the tab used to be nine
words with no glossary anywhere on screen.

### Settings, and a Refresh that means something

Thirteen settings, each with a title, a paragraph, two monospace names and a
chip, made a page that had to be scrolled past to reach its own buttons. Rows
are now half the height, with the help clamped and the whole of it on hover; a
row whose value is not the default is marked in the margin and counted in its
group's title; and the toolbar moved to the top.

**Refresh** is new, and it is a real action rather than a redraw: it re-reads
Hermes' own `.env` into this process and republishes, so a `KAME_` line edited
by hand, or a change made from another window, takes effect without a restart.
A deleted line is treated as a reset, which is what deleting a line means.
Only `KAME_` names this build knows are touched — a variable belonging to
another plugin, or to a future release, is left exactly where it is. It
deliberately does *not* re-read `config.yaml`: KAME reads that once at
start-up, and pretending otherwise would make the "restart pending" notice on
the same page a lie.

Two help texts were rewritten. **Wait for the first token** now says outright
that zero is the right answer unless a provider hangs, and why a number like
twenty seconds abandons a reasoning model mid-thought. **Daily quota cooldown**
now says what it does *not* govern, which is the refused credential above.

### `/kame doctor`

The tool that reads a real run and says whether it looks right was a script in
this repository. That is the one place a diagnostic is guaranteed not to be
when it is wanted: a fresh install, another machine, a reinstall, an assistant
that has never seen this code.

It is a command now. `/kame doctor` answers, from inside the process it is
describing: which build is actually running and whether the other Hermes
processes agree; what each pool holds and what is ready right now; the whole
kind-to-rest table beside how often each kind has actually happened; how many
decisions were read off the payload rather than guessed; and a list of the
things a person has to do something about — refused credentials by
fingerprint, keys in use that the config does not hold, a neighbour on an
older build.

The rest table in it is written by hand on purpose. Derived from the code it
would agree with the code by construction and prove nothing; written down, it
is a second statement of intent, and a test holds the two together.

### Verified

* **1631 unit tests**, including a new `test_v1_6_0_1.py` with 72 checks: the
  refusal bench, the demotion (proven to fail without it), the kind-to-rest
  table against Agent Zero's, the per-process snapshot, the ready-versus-
  healthy count, the empty-provider card, the unpooled-key surface, the
  environment re-read, and the doctor.
* The panel harness renders the real `plugin.js`, including a check that a
  neighbour writing its own section does not re-render this panel.

## [1.6.0.0] — 2026-09-02

**In short.** Two subsystems that were each correct and never met, one
provider whose evidence nothing read, and three ceilings that ended a turn
the plugin exists to keep alive. Nothing here is a new feature; every item is
a thing 1.5.0 already believed it was doing.

The release began as a list of bugs and turned into a contract, stated by the
owner and now written into the code as named tests: *the agent is
uninterrupted; it never tries the same API twice in a row; it rotates to the
one most likely to be healthy; it never stops except when a single healthy
credential is left. The agent should not stop because of errors.*

### The measurement it all came from

The prediction journal on the owner's own install, 13.9 days, **295 recorded
blocks**:

| What the journal said | Count |
|---|---|
| `sized_by: kame` — a cooldown KAME chose that reached the pool | **0** |
| `sized_by: dropped` — KAME sized it, the number never arrived | 185 |
| `sized_by: host` — KAME named no deadline | 110 |
| Entries whose `last_error_reset_at` was set | **0 of 295** |
| Gemini refusals decided by prose rather than by a field | 184 of 225 |
| Recoveries carrying the prediction they tested | **0 of 30** |

A plugin whose entire purpose is to size a wait had never once delivered one.

### The deadline never reached the pool

KAME's hook returns `error_context: {"reset_at": ...}`. It survives
`hermes_cli/plugins.py:6151` and lands on `ClassifiedError.error_context`.
Then `agent/conversation_loop.py:4280` **rebuilds the context from the raw
exception** and passes that on instead — `ClassifiedError.error_context` is
read by nobody. The host's own extractor looks for `error.body` (Hermes'
Gemini adapter files nothing there), then a `Retry-After` header (Google
sends none), then three prose regexes. On this traffic it produced a deadline
zero times.

The deadline is now put back at the last seam KAME owns — the pool's own
`_mark_exhausted` — and only when the host derived none of its own, so a
number the provider actually stated always wins. Proven against the real
`CredentialPool`: the entry gets the deadline, `_exhausted_until` returns it,
and the journal can finally say who sized the bench.

### Gemini's refusals were read from the wrong place

Hermes serves Google through its own adapter, which reads the response body
once and files the parsed error on the exception:
`details={"status": "RESOURCE_EXHAUSTED", "reason": "RATE_LIMIT_EXCEEDED",
"metadata": {"quota_limit": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}}`
— with `code` set to `gemini_rate_limited`, a name Hermes invents. The body is
then spent, so `error_body` arrived empty and every reader below it found
nothing.

The cost was not silence. Google's free-tier 429 says, word for word,
OpenAI's out-of-credits sentence. `_AMBIGUOUS_BILLING_PATTERNS` exists for
exactly that collision and is settled by evidence from the body — which was
empty. So **184 of 295 refusals** were read as `billing` at `account` scope: a
key benched for a day, every model down with it, the probe disarmed by the
reason and the escalation disarmed by the window. A twenty-one second
throttle, treated as a spent account.

`exc.details` is now read alongside the body, the quota identifier is found
in a parsed payload as well as raw JSON, and a catalogued throttle settles
the ambiguous sentence outright. The same nine payloads that produced one
right answer now produce nine.

### The row holding several keys could still be sent as a key

`_available_entries` and `_select_unlocked` have excluded the parent of a
split since the feature shipped. `current()` never went through either, and
it is what the restored `_current_id` resolves through — and only the parent
is ever written to `auth.json`, because a derived row must not reach disk. So
after a restart the live credential could be the comma-joined list, sent
whole and refused as invalid. Reproduced against the real pool; visible in
the journal as 31 of NVIDIA's 56 blocks sitting on the bare parent row.

### Three ceilings that ended a turn

* **A cut answer** stopped after three continuations. It now continues while
  any key is still adding words, and stops when the pool starts repeating
  itself with nothing new — unanimity, not a count, the same shape rotation
  has always used. `stream_resume_limit` stays as the guard rail it always
  claimed to be, defaulted to the top of its range.
* **A dropped tool call** went straight back to Hermes, which tells the model
  that its previous tool call was too large and must not be retried — often
  false, and it teaches the model to avoid a working tool. When nothing has
  reached the screen, the same call is now asked of another key first.
* **The auxiliary lane** fires no classification hook, so its refusals were
  the only ones the plugin never read, and it benches after its own call has
  unwound — so a titling call's cooldown landed on the model that answers the
  user. It now reads its own refusals and says which model earned the bench.

### Asked for, and added

* **Stay on this model, always** — a new switch. Hermes answers a spent
  credential by rotating and then quietly switching model and provider; turn
  this on and it does not. Off by default, because falling back is the host's
  own behaviour. The auxiliary lane is routed by Hermes and this cannot reach
  it, which the help text says rather than leaving it to be discovered.
* **The event inspector is readable.** Its card was painted with
  `--ui-bg-primary`, which is a 90%-transparent wash meant for tinting a
  surface that already exists — so the payload was read over the events list
  behind it. It now uses the opaque token Hermes' own popovers use.
* **"What KAME can see"** — one line per provider: rows stored, keys after
  splitting, how many are resting, and where they came from. Counts and
  origins only, never a key. It is the answer to "is it even seeing my
  keys?", which turned out to be the question behind every report.
* **Temporary files are swept.** The snapshot write is atomic and cleans up
  after every exception, but `os.replace` is the last statement and a process
  killed before it leaves the file behind. Six of them, 40 KB, one per
  mid-write restart.
* **Five detection markers** carried back from the Agent Zero plugin after a
  release-by-release audit of all 28 of its versions. Three were already
  covered here; `tokens per day`, `tokens per min` and `quota left` were not —
  the shape a refusal takes when the provider states the counter instead of
  naming the category.

### The bench that was filed under the wrong model

Found by running the live harnesses, which had never been run: a sole key
benched for a daily cap was offered again the *instant* its bench was
written. The bench was real. It had been filed under the auxiliary model.

The note that says which model an auxiliary bench belongs to exists because
the host benches after the relay call has unwound, and it deliberately
outlives that call. What it must not outlive is the **next** refusal: a
titling call that fails seconds before the conversation's own 429 made KAME
file the conversation's bench under the small model, and per-model release —
working exactly as designed — then handed the key straight back to the model
that had just spent it.

`agent/auxiliary_client.py` never calls `classify_api_error`, so reaching the
classification hook is proof the refusal belongs to the main lane. The note
is now cleared there, before the verdict is even reached, so a refusal KAME
declines to size clears it too.

### What the provider said, and what kept going wrong

Two things the journal could not tell anyone, both carried back from the
Agent Zero plugin.

**The verdict had nothing to argue with it.** Every row held the window KAME
concluded and no record of what the provider itself named — so a confident
right verdict and a confident wrong one were the same row, and the
misclassification that produced this release was invisible in the very file
that recorded it 184 times. Rows now carry the window the provider's own
counter stated, read on its own, beside the one KAME acted on. Where they
disagree, the report says how often.

The counter's *text* is deliberately not carried anywhere. It is
provider-authored, and the report has one invariant worth more than the
detail: no error text reaches it. What travels is a word from KAME's own
vocabulary.

**A daily cap, a per-minute throttle and a rejected credential are three
problems.** The fortnight tally grouped by window, which answers "is KAME
reading these" and cannot answer what an owner asks first — *what keeps
happening, and is waiting even the right answer to it*. A new section counts
refusals by kind per provider, says how many of each KAME sized, and marks
the kinds no timer fixes.

Rendered against the owner's own 295-block journal, which promptly found a
bug in it: ordering rows by count alone printed `gemini` twice under two
separate headings.

### Sent, versus blamed

Every count in this plugin names the credential the **pool** was pointing at.
That is not the same claim as *the key the request carried*, and this release
fixed two bugs that lived in the gap between them: a parent row holding
several keys handed out as though it were one, and a bench filed under the
model that was not spending. Both were found by accident.

The carousel now fingerprints the key at the moment it puts it on the agent,
and the bench that may follow is asked whether it is blaming that same key.
Fingerprints only — a truncated SHA-256, never the key and never a prefix of
it — and derived by one shared function on both sides, because comparing two
slightly different readings of the same credential would manufacture
disagreements out of nothing.

Counted, never acted on. Writing the cooldown is the host's job, and a plugin
that started overruling it on the strength of a fingerprint mismatch would be
spending somebody else's quota on a guess. The number surfaces in the panel
and in the snapshot; zero is the only healthy value.

### And the screens the owner actually meets

Rendered cold, from the installed build, with an empty state — the one state
nothing else exercises, because every test sets something up before it looks.
Three fixes came out of it:

* **`/kame` prints the build fingerprint.** The panel has shown it since
  1.4.0; the command had not, which left the one check worth making after an
  upgrade available only to whoever opens the sidebar.
* **`tools/deploy.py` now says that profiles exist** and that it does not
  write them. That drift is silent and self-consistent by construction: the
  default profile's panel shows the new version and agrees with every check
  made from it. Caught by writing the tool below and running it once.
* **`tools/fingerprints.py`** lists every copy on the machine — source, base
  home, each profile — with the digest of the modules that actually loaded,
  and says plainly whether they are one build. It found a real drift on its
  first run.

**`tools/live_setting.py`** joins the live harnesses and asks the host about
every declared setting, not just the newest. Hermes serves a plugin only the
names under `plugins.entries.<id>.settings` and refuses the rest outright, so
a setting can be declared, read, and shown on a shelf while being completely
decorative — and the owner finds out at the moment they type the command. 63
checks: the host answers for all thirteen, refuses a name it should refuse,
each has a shelf and help text, and a switch and a number survive set, read
and reset. It also proves a value outside a setting's range changes nothing
and says why, rather than being clamped in silence.

### Verified

* 1558 unit tests.
* The host's own error corpus, 87 cases, run clean and run again with KAME
  behind the real hook dispatch: identical both ways.
* Every host fact the plugin reasons about, re-checked against the installed
  Hermes.
* The full sandbox suite against the real `CredentialPool`,
  `PooledCredential`, `GeminiAPIError` and `extract_api_error_context`,
  including the six sections written for this release — one of which now
  follows a provider's own counter from the exception, through the real hook
  and the real pool, into the journal row.
* **All six live harnesses, five of them run for the first time.** Real sockets, the real
  OpenAI SDK, the real `classify_api_error`, the real pool. They found the
  bench-attribution bug above and three stale expectations of their own:
  `live_429` still asserted the US/Pacific midnight calculation that 1.2.4
  removed on purpose, and `live_429` and `live_multikey` both checked that the
  `pre_api_request` hook was registered and then never fired it — so every
  per-model claim below that point had been measuring a plugin that had been
  told nothing about which model was spending.

## [1.5.0] — 2026-08-29

The release that read the rest of what the host was already handing over, and
ran the gate that would have caught 1.4.0.

**In short**

- 🐛 *Fixed* — The classifier hook discarded `error_type` and `error_code`
  under a comment explaining that it discarded them. `error_type` is literally
  `type(error).__name__` — the only evidence a transport failure carries, since
  it has no status and no body at any point.
- 🐛 *Fixed* — A **402 that says "try again in 5 minutes"** was read as a dead
  balance: key retired, not retryable, never re-probed. An empty balance does
  not tell you when to come back.
- 🐛 *Fixed* — **"Monthly quota reached."** was called a rate limit and
  re-probed hourly against a wall that stands for weeks. It is an allowance
  that is gone, and only another key helps.
- 🐛 *Fixed* — The Settings panel could freeze: five of `readSnapshot`'s seven
  exits never settled a save in flight, so `Saving…` stayed for ever with every
  control disabled. The schema-mismatch exit made an upgrade window the trigger.
- ➕ *Added* — A curated exception-class table, so a bare `RateLimitError`
  with no body is sized instead of declined, and a transport error rests three
  seconds instead of twenty.
- ➕ *Added* — Two host tripwires: the two functions the carousel patches, and
  the two hook arguments this release started reading.
- ♻️ *Changed* — The Events tab opens a failure's payload in an inspector, and
  only for a failure. A row that succeeded has nothing to check.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### The evidence that was already on the call

`agent/error_classifier.py:680` computes `error_type` as `type(error).__name__`
and `:1808` derives `error_code` through an extractor that walks the
exception's `__cause__`/`__context__` chain five levels deep, parses JSON
nested inside `error.message`, and knows the `error_code`/`errorCode`
spellings this plugin's own path list does not. Both were passed to the hook.
Both went into `**_ignored`.

The class name matters most where everything else is absent. A transport
failure — `APIConnectionError`, `ReadTimeout`, `ConnectError` — has no status
and no body, ever, and used to fall to the twenty-second rest for the
unrecognised. It now rests three seconds and rotates, which is the whole of the
right answer.

**The table is curated rather than folded into `_TABLE`, and that is the point.**
`_n` strips separators, so four class names normalise onto existing rows:
`RateLimitError`, `OverloadedError` and `NotFoundError` land on rows that mean
the same thing — and `AuthenticationError` lands on `authentication_error`,
which reads as *retire this credential, permanently*. But that class is raised
for **every** 401, including an expired OAuth token one second from refreshing.
Feeding class names into the general lookup would have recreated the defect
1.4.0 removed when it took `"unauthorized"` out of the permanent-auth patterns,
after 21 hour-long quarantines of healthy keys. `AuthenticationError`,
`PermissionDeniedError` and `ProviderStreamError` are listed as deliberate
omissions with the reason attached, and a test asserts their absence.

### Two payloads 1.4.0 claimed and did not own

`tools/host_assumptions.py` and `tools/host_corpus.py` both existed before
1.4.0 shipped. The corpus one — which runs Hermes' own ~14-provider error
suite twice, with and without KAME behind the real hook dispatch — was not run.
Against the pristine 1.4.0 tree it fails on two cases:

| Payload | Host | KAME 1.4.0 |
|---|---|---|
| 402, body *"Usage limit reached, try again in 5 minutes"* | `rate_limit` | `billing` |
| 429, *"Monthly quota reached."* | `billing` | `rate_limit` |

Both are fixed, and the fix for the second one is conditional in a way the
corpus itself taught: *"Weekly usage limit reached. Resets in 6hr 29min."* is
the **same window** as the monthly wall and the opposite verdict, because the
provider said when it comes back. `decision.source == "window"` is exactly
that distinction — KAME's own default, applied because nothing was supplied.

1.5.0 leaves the host's corpus at 130/130, unchanged, both runs.

### The panel that appeared and would not move

`readSnapshot()` has seven exits; only two called `settle()`, which is the only
thing that clears a pending request once `CONTROL_TIMEOUT_MS` has passed. The
five silent ones — no file bridge, no plugin root, read threw, parse failed,
**schema mismatch** — left `$pending` set for ever, and every input, switch and
button is disabled while it is. The schema-mismatch exit is the realistic
trigger: `SCHEMA` moves with any release that changes the document, so the
window between new files on disk and a restarted backend was the freeze.

### Not done, and why

- **No plugin-side fix for rewind after a model switch.** Root-caused to the
  host: it inserts a `display_kind: "model_switch"` marker row, ordinal
  counting skips rows carrying that tag, and Hermes' own comment
  (`tui_gateway/server.py:6213`) spells out what happens when a code path drops
  it — *"every later rewind resolves one turn too early and `replace_messages`
  hard-deletes the difference"*. KAME was verified clean: `identity()` is
  recomputed per call, nothing is cached across a switch, and the plugin never
  touches host message history. Adding a second actor to a corrupted ordinal
  address space is the 1.0.8 stream-watchdog mistake repeated.
- **The dispatch patch stays.** All 33 hooks in `VALID_HOOKS` were checked:
  `pre_llm_call` injects context only, `on_stream_*` are documented observers
  that cannot transform the stream, and `ctx.llm` is for a plugin's own
  out-of-band calls. Nothing can swap a credential and re-drive the request. A
  tripwire now names the two patched functions so a rename upstream fails
  loudly instead of silently stopping rotation.
- **No `retry-after-ms` handling.** It was a hypothesis; no provider researched
  was found sending a millisecond retry header.
- **No build-time throttle on the snapshot.** Tried, and reverted: it cannot
  know a document changed without building it, so it broke *"a changed document
  is written immediately"* — a guarantee with a test defending it. The saving
  was microseconds.

### Verification

1502 tests · `tests/ui_reconcile.mjs` 4/4 · `tools/host_assumptions.py` 38/38 ·
**`tools/host_corpus.py` 130/130 both runs — KAME changed nothing in the host's
own corpus.**

</details>

## [1.4.0] — 2026-08-29

The release that went looking for why nine days of fixes had not changed
anything, and found that the plugin being fixed was not the plugin that was
running.

**In short**

- 🔎 *Found* — The installed KAME had no `core/` package. It registered,
  published `installed: true, reason: "active"`, and rotated nothing.
- 🐛 *Fixed* — `getattr(exc, "message", "")` returned the empty string on every
  Gemini failure there has ever been, so the sizing cascade had nothing to read.
- 🐛 *Fixed* — Google's per-minute sentence is, word for word, OpenAI's
  out-of-credits sentence. The table read both as a daily cap.
- 🐛 *Fixed* — A throttle nothing could size benched its key for **zero
  seconds**, because `Verdict.reason` said `rate_limit` and the escalation
  ladder spoke only `per_minute`.
- ➕ *Added* — `core/evidence.py`, `core/catalog.py`, `core/redact.py`,
  `host_text.py`, `integrity.py`.
- ➕ *Added* — The Events tab says where every cooldown came from, and expands
  to the provider's own payload, redacted before it was stored.

<details>
<summary><b>Everything in this release, in detail</b></summary>

### What the telemetry said

The plugin had been writing a journal the whole time. Nine days of one real
pool, 2026-08-17 to 2026-08-26 — 276 recorded blocks, 30 recoveries:

| Measure | Value |
|---|---|
| `reset_at` set | **0 / 276 (0 %)** |
| `sized_by: "dropped"` | **184 / 276 (67 %)** |
| `source: header` | **0 — never fired** |
| Recovery, median | **3.8 h** |
| Recovery, longest | **192 h (8 days)** |

And in the host log, per symptom:

| Line | Count |
|---|---|
| `daily [429] — resting 1h 0m` | 79 |
| `auth [400] — resting 1h 0m` | 21 |
| `rate_limit [429] — resting 0s` | 20 |
| unanimity promoting a pool-wide 429 to terminal | 14 |
| `per_minute [429] — resting 20s/40s` (NVIDIA) | 250 |
| `panel requested clear_pool` | 15 |

That last row is a person opening the panel to get working again.

### The install was never the plugin

Four copies of KAME existed in the Hermes tree. The one Hermes scans said
`1.0.2` and carried an engine dated 08-17 **with the Pacific-midnight bug
live**. A second, at `hermes-agent/hermes_plugins/hermes_kame_api_rotation/`,
declared `1.3.3` — and had no `core/` directory at all, while its
`dispatch_binding.py` opened with `from .core import multikey, stitch`.

That directory is not one Hermes reads. `hermes_plugins` is a synthetic
namespace module created at runtime with an empty `__path__`; the loader imports
a plugin with `spec_from_file_location(..., submodule_search_locations=[plugin_dir])`
against the real directory. `git status` inside the Hermes checkout reports it
untracked. Somebody read the module name in a log line and deployed to a folder
of that name.

The copy that made it had not recursed: the package's `.pyc` files were sitting
in the package root instead of `__pycache__/`, and the whole subpackage had been
dropped on the way. Every zip in `dist/` and `releases/` was checked and all 29
contain a complete `core/` — the packager was never the problem.

**`integrity.py`** answers it. At registration the plugin checks that every
module it needs is on disk, computes a twelve-character fingerprint over the
bytes actually there, and publishes both. A version string is written by
whoever last edited the manifest and survives a partial copy; a fingerprint
computed from the files cannot describe files that are missing. The panel shows
it beside the version, and an incomplete install gets the loudest thing on the
page instead of a green tick.

### The attribute that was never there

```python
message = str(getattr(exc, "message", "") or "")
```

The host's `GeminiAPIError` passes its text to `Exception.__init__` and defines
no `message` attribute. That read returned the empty string on every Gemini
failure. An empty message meant the footer strip below it had nothing to strip,
the classifier had no prose to match, and the four-source cascade in `quota` had
nothing to size from — which is the whole of the 0 %, the 67 %, and the header
source that never fired once.

Everything it wanted was on the exception:

```python
self.code = code            # "gemini_rate_limited" / "gemini_unauthorized" / ...
self.status_code = status_code
self.response = response    # the httpx.Response, body and headers included
self.retry_after = retry_after
self.details = details      # google.rpc.ErrorInfo -> {reason, metadata}
```

**`core/evidence.py`** reads all of it, guarded field by field — a property that
raises costs one field, not the harvest, which is the shape that cost 1.3.1 a
hotfix. It also recovers `google.rpc.RetryInfo.retryDelay` from the raw body,
which the host discards: `exc.retry_after` is populated only from a
`Retry-After` header, and Gemini does not send one.

### The host's own handwriting, read as evidence

Hermes appends `_FREE_TIER_GUIDANCE` to every free-tier 429, and that paragraph
contains *"a few hundred requests/day"*. The day markers in `quota` match
`/day`. A sixty-second throttle therefore arrived carrying, in the host's
handwriting, the phrase that means "spent for the day" — and was benched for an
hour, on key after key, fourteen keys deep.

**`host_text.py`** imports the blocks from the host rather than copying them, so
a reword upstream is followed automatically; the literals are a floor, and the
state is reported (`host:2` or `fallback`) rather than assumed. 1.2.6 fixed the
same bug by splitting on one hardcoded prefix and mutating the exception's
`args` on the way past. This leaves the host's exception exactly as it arrived.

### One sentence, two meanings

> `You exceeded your current quota, please check your plan and billing details.`

Google sends it for a 21-second throttle. OpenAI sends it when the balance is
empty. Identical. `carousel.DAILY_INDICATORS` listed **both**
`"exceeded your current quota"` and `"billing"`, so every Gemini throttle read
as a daily cap: 1,088 occurrences of that sentence in nine days.

`classify.py` already knew — its ambiguous-billing pattern matches the sentence
and refuses to read it as billing unless the payload also fails to name a wait,
with a comment saying the sentence had already cost one version. The lesson
lived in the module that declines most of the time and had never been carried to
the module that decides. Now it is in both, and what replaces the phrases is not
another phrase.

### The catalogue

**`core/catalog.py`** is a table of what providers call things, keyed on
machine-readable fields — `error.type`, `error.code`, Google's `status`,
`ErrorInfo.reason`, RFC 7807's `title`. No prose. Prose is the only evidence two
providers can share while meaning opposite things; the fields never collide.

Its ranking rule is the interesting part, and three real payloads forced it:

```
Google    status "INVALID_ARGUMENT"      + ErrorInfo.reason "API_KEY_INVALID"
OpenAI    type   "invalid_request_error" + code "invalid_api_key"
DeepSeek  code   "invalid_request_error" + type "authentication_error"
```

DeepSeek inverts OpenAI. So specificity is a property of the **value**, not of
the field it sits in: `invalid_request_error` and `INVALID_ARGUMENT` are marked
uncertain — buckets, not facts — and any certain row beats them wherever it sat.
A bare bucket with nothing beside it still reads as a malformed request.

Also now read, none of which was before:

- **NVIDIA's `title`.** Its entire 429 body is
  `{"status": 429, "title": "Too Many Requests"}` — no `Retry-After`, no
  `X-RateLimit-*`, sometimes no body at all. `title` was the one field it fills
  in and the one field nothing looked at. The status is recovered from the body
  too: 15 of 38 recorded NVIDIA blocks had no status code on the exception.
- **402, 498, 529.** Anthropic and DeepSeek use 402 for an empty balance; Groq
  returns 498 when the flex tier is at capacity; Anthropic 529 is overload.
  None is in a standard status set, so all three fell into the generic
  twenty-second bucket.
- **Google's 400 `FAILED_PRECONDITION`** — "the free tier is unavailable in your
  country, enable billing". A 400 that is an account problem, and read as
  terminal it ended a turn no rotation could have saved.
- **OpenRouter's normalised `error_type`**, including the length errors it turns
  into *successful* completions with `finish_reason: length`.
- **Alibaba's `Throttling.*` family**, which never says "rate limit", and
  `AllocationQuota.FreeTierOnly`, which is billing on a 403 and must beat the
  prefix rule.

The reasoning, with sources, is in `knowledge_base/provider-errors.md`.

### Resting zero seconds

`Verdict.reason` says `rate_limit`. `Carousel._escalate` handled `daily`,
`insufficient_quota`, `denied`, `auth`, `per_minute` and `server` — and fell
through to a flat rest for anything else. A throttle the payload could not size
arrives with a delay of zero, so the key was benched for **nothing** and the
pool burned through every credential it had in a few hundred milliseconds before
declaring itself exhausted.

Two halves of one plugin disagreeing about the name of the commonest failure
there is. 1.2.7 blamed an empty error string and added a fallback for it; the
fallback was correct and the bench stayed at zero, because the string was never
what routed the kind.

`rate_limit` now escalates as the throttle it is, with a one-second floor on the
first strike — not an invented cooldown, the smallest rest that cannot spin.

### The dictionary, reviewed

Three entries removed from `core/carousel.py`, each with the release that had
already learned it elsewhere:

- **`"exceeded your current quota"`, `"billing"`** out of `DAILY_INDICATORS`.
- **`"unauthorized"`** out of `INVALID_KEY_INDICATORS`. It is the HTTP reason
  phrase for 401, so it arrives on every bare 401 a proxy or an expired token
  produces, and reading it as "this key is not a key" retires a healthy
  credential over a refresh that was about to succeed. `classify.py` had removed
  it for that reason, citing the host's own corpus. 21 hour-long quarantines.
- **`"is disabled"`** narrowed to `"is disabled for this project"` in
  `PERMANENT_DENIAL_INDICATORS`. The bare stem reaches into "streaming is
  disabled", "caching is disabled for this model" — configuration facts, each of
  which benched a healthy key for an hour.

Added: `"key is invalid"` and `"key is no longer valid"`, which is how Anthropic
and DeepSeek phrase it — the words the other way round from every pattern that
was already there.

### Seeing why, without leaking how

1.1.1 built the Events screen and kept **no** provider text, because a provider
can quote the request back inside an error and the request can be the user's
prompt. 1.2.9 put the raw payload back under a click, silently, without meeting
the rule it was reversing — and shipped it as a browser alert beside a mangled
template literal that left three undefined identifiers in `EventRow`, so the tab
threw on the first event carrying a status code and took the panel with it.

Both halves were right about something. **`core/redact.py`** scrubs on the way
*into* the store, so the secret is not in the file, not in a screenshot, and not
in a support bundle — where redacting at display time would have left it in all
three. Credentials go by vendor prefix, by named field, and by shape; the shape
rule requires a digit, because the first draft of it ate
`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`, which is the single most
informative string a Gemini 429 carries.

The row expands inline, and it carries one new column: **where the cooldown came
from** — `field`, `header`, `retryDelay`, `reset time`, or `guess`. The most
useful number in nine days of telemetry was the proportion that were guesses,
and nobody could see it.

### Snapshot schema 5

`build` (complete, fingerprint, missing modules, guidance source) on the
document; `detail` and `sized_by` on each event. The panel and `state.py` move
together, as always.

### Test results

- 1,487 tests passing (1,445 existing + 42 new in `tests/test_v1_4_0.py`).
- `node tests/ui_reconcile.mjs` — 4/4 structural checks, all three tabs.
- The schema-agreement test now asserts *agreement* rather than the literal 4 it
  had been pinned to since 1.2.2 — a test that has to be edited to allow a
  correct change teaches people to edit tests.

### Not done, on purpose

- **No rotation ceiling.** ADR 0002, unchanged.
- **The Absolute Shield from 1.3.0 is not here.** It swallowed genuine bad
  requests that had nothing on screen — breaking the contract 1.1.2 wrote and
  tested by name — and injected KAME's own text into the model's message
  history, which every release since 1.0.8 has routed around on purpose. If some
  terminal kinds deserve a synthetic answer, the list has to be explicit and
  tested, and the object returned has to be the host's own response type.
- **1.3.3's unconditional replay is not here.** It reversed 1.0.0's "a partial
  stream is never replayed" without a bound.

</details>

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
