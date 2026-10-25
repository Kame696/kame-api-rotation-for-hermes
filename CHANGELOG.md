# Changelog — KAME API Rotation for Hermes

Versions follow `major.minor.patch.build`; the newest is at the top. The
[README version history](README.md#history) also summarizes earlier Hermes
versions and development candidates; this file gives the full notes for the
current 1.8.1.x public releases.

---

## [1.8.2.9] — 2026-10-24 — back to your best model, nothing idle burns your CPU, background jobs stop eating your best model's quota, no reply behind two silent models, speed in plain words, a rotation page you can read

**What drove it.** The catalog review of 1.8.2.8 (a silent `import yaml` on a stock Hermes) and
a live test on the author's keys: with the primary and every fallback model busy, the reply ended
up waiting on the LAST, least capable, model while the best one may have recovered; the old top
speed mode doubled the requests on every reply and used up a 14-key pool in four messages; and
the rotation page could not be read at a glance.

- **Back to your best model.** When the model in use says it is busy (or every key of it rests)
  and nothing after Hermes' position is free, KAME goes back up your fallback list, best first,
  one request at a time, asking a busy model again at most every 20 s. Only models of your own
  list on the same provider and endpoint; never with text on screen; never under "Never".
- **A lighter plugin on every reply.** The snapshot the panel reads used to be built and written
  on the same thread that waits for the model (about 10 ms each, 27 ms with the panel open, several
  per reply). Now the reply thread leaves a note and a background thread writes it: 22 ms of plugin
  time per reply against 73 ms in the healthy-path bench, nothing added to a reply that goes well.
- **Speed in plain words.** Saver · 1x, Balanced · up to 1.1x, Faster · up to 1.3x, each with
  one sentence under the buttons; the numbers follow `hedge_budget_pct`. The stored values
  (`off`, `adaptive`, `max`) are unchanged.
- **Faster replaces Fastest.** A reply that goes well sends one request (Fastest sent two on every
  reply). The first backup goes out at 1.5x the model's usual first word, from a budget three times
  Balanced's, and any rate-limit or quota refusal on that model puts it back to Balanced for 10
  minutes. Replay of the 2026-10-18 failure: the old mode 65 requests, 14 of 14 keys refused; Faster
  55 requests, none refused. A config that said `max` now gets Faster, which spends less.
- **Extra requests this hour**, as a percentage of replies, next to what your setting allows.
- **The rotation page.** Three sentences first (what is happening, is anything wrong, what next),
  one ring per model in the order of your list with one segment per key (shape as well as colour),
  "What KAME did", the three controls you reach for, and two buttons for one model: *Wake this
  model* and *Try this model now*. The old cards are folded under "Details"; the thin bar is the
  only thing dropped (the ring says the same).
- **Reads the fallback list on a stock Hermes** (`hermes_yaml`, then PyYAML; one log line if
  neither exists) - the defect the catalog review found.
- **Nothing idle burns your CPU.** Measured on a running gateway: it loads the plugin once per
  profile (four copies) and each copy re-read and re-parsed the 42 KB `config.yaml` three times
  every five seconds, about 120 ms each with the pure-Python reader the real Hermes ships - roughly
  30% of a core with nothing happening. The file is now parsed once per change, by all readers, and
  a typo left in it is not parsed again until it changes.
- **Background jobs get a budget.** One title on your best model made 167
  attempts over 12 minutes, 60 quarantines among them a one-hour daily one, because side jobs
  inherited the chat's endless wait. Now a title (also profile describer, background review) never
  waits and sends at most 3 requests in 30 s; every other side job (compression, approval, vision,
  web extract...) gets 6 requests and up to 5 minutes, or the time Hermes put in its own request,
  whichever is shorter. A job that cannot start well (model busy, keys short of quota) sends no
  request at all, and no side job sends backup requests. It stops with a timeout-shaped error,
  which Hermes answers by moving the job down YOUR fallback list - KAME never picks the model.
  Your chat keeps its wait, `max_total_wait_seconds: 0` included. `KAME_AUX_UNBOUNDED=1` brings
  1.8.2.8 back.
- **Where your quota went.** The journal says which side job sent each request; the first page shows
  a line under a ring when side jobs are 10% or more of that model's requests ("side jobs: 23% of
  requests since 08:10"), Details has a card with the split and the jobs stopped early, and when side
  jobs take 15% or more of your best model the card says, in words, which job you could move to which
  model of YOUR list and the two lines of Hermes' config to do it. KAME never changes that config.
  `tools/speed.py` prints the same split.
- **No reply behind two silent models.** When the best model is silent and the next model raced in
  is silent too, the reply no longer waits a whole slow bound after the last one joined (37-111 s):
  each raced model keeps it waiting only to its own late point, never longer than before; at the slow
  bound one more model of your list joins instead of everything starting over; and when every model
  that could take the reply is already racing, nothing is cut - the first to speak is shown.
- **The busy best model is asked again, beside the reply.** When the model Hermes asked for is
  inside its busy window and is due a look (every 20 s), the reply still starts on the next model at
  once, exactly as before, and the best model is asked beside it: if it answers first it is shown and
  its window closes. Added wait: none. Cost: at most 3 extra requests a minute per busy model, only
  while replies are flowing.
- **The first page says when Hermes cannot reach your whole fallback list** (`agent.api_max_retries`),
  and that the same number is Hermes' general retry count.
- Checked against Google (12 requests, one key): a Gemini function-call turn is accepted by another
  Gemini 3.6 / 3.7 / 3.8 model when its signature travels with it, and refused by all of them without
  it - so the race, the circle back and the widen are safe on these models.

Not in this version: Agent Zero's port of all of the above; smart tiering of reasoning steps onto
a lighter model (decided against: it would be KAME choosing models); a side job that already
streamed text is still ended by Hermes' own stream limits, not by KAME's budget.

---

## [1.8.2.8] — 2026-10-17 — your next model when the provider is busy, everything automatic, the model of each reply on screen

**The case that drove it.** Four fallback models with 14 Gemini keys each, "Switch to the
next model after" set to 10 s, and the first model answering "the model is overloaded"
(503) for well over a minute: Hermes never moved to the next model. KAME's switch clock
only counted the time every key was resting, and a 503 rests a key one second — so the
clock stayed near zero while the user waited (`decisions/0028`).

- **A busy provider moves to your next model at once.** Two keys in a row that get
  "overloaded" or time out means the model is busy, not the keys: Hermes answers with the
  next model of your fallback list. The busy model is left alone for 30 s (doubling while
  it stays busy, at most 5 minutes; an answer on it ends that at once).
- **One big pool, best model first, every reply.** A fallback lasts one reply: Hermes is
  told the primary is back in a moment, so its own turn-start restore puts your best model
  first on the next reply, and KAME passes it down again — without spending a request —
  while it knows that model is still busy or resting. All of a model's keys are used before
  the next model is; nothing switches after part of an answer is on screen. (Owner's live
  test: the reply moved to the fallback model and the selector stayed there.)
- **Speed first: the next model races a late reply.** A reply still silent at about 3 × its
  model's usual first word (8 s at least; 12 s before anything is learned) — after a backup
  key of the same model has had its chance at 2 × — gets a second request on the next model
  of your list — and if that one is silent too, the model after it joins a few seconds later
  — and whichever starts answering first is shown; the others are cut. No waiting
  for a slow model to fail, no starting over. A model that loses this race sits out only
  30 s, so your best model is tried first again almost at once. Not in Saver mode; Hermes
  races a next model of the same provider (same keys, only the model name changes).
- **A slow model is passed on too.** Live on the owner's keys, gemini-3.8-flash took about
  two minutes to a one-word answer — on every key, with no error — while gemini-3.7-flash
  answered in three seconds. Nothing failed, so nothing switched. Now a reply with no sign
  of work (no word, no tool call, no reasoning) by the model's slow bound falls back to the
  next model of your list, and the slow model is left alone for 2 minutes (doubling while
  it stays slow, at most 10), so the next replies go straight to a model that is fast now
  and only one reply now and then pays to check the slow one again.
  *Automatic* learns the bound from that model's own first words on your computer
  (3 × median, 20–45 s; thinking models 25–60 s; 30 / 45 s before
  anything is learned); *Set time* uses your number. Only when a later model can take it
  (not resting, not itself known slow or busy); never for background work; the last model
  of the list is always waited for.
- **No scary line in the chat for a planned switch.** Hermes' "⚠️ Model fallback … Primary
  retry eligible in ~3488 s; recovery is not guaranteed." and its "✅ Primary model
  restored" stay out of the chat when the switch was KAME's own hand-over
  (`hermes-kame-bridge`); any other switch, and a failed turn's whole trace, is shown as
  Hermes writes it. The reply notice and the Events tab say what happened.
- **The switch counts your wait** — the time with nothing on screen, every failed attempt
  included — and is weighed after every failure, not only when every key rests.
- **Three choices instead of numbers to guess:**
  - *Fallback*: **Automatic** (the busy rule; at once when every key rests; otherwise
    about three times your model's usual first-word time, 6–20 s, learned) / Set time /
    Never.
  - *Wait for the first token*: **Automatic** (learned per model, far past its usual first
    word: 20–120 s, 60–300 s for thinking models; streams only; never for a model on this
    computer) / Set time / Off.
  - *Show which model answered*: **Every reply** / Only another model / Never.
  A number you set before 1.8.2.8 keeps working as "Set time" (0 = never / off). Each
  number is shown on the Settings tab only under "Set time".
- **Backups keep working through an overload.** The speed boost stood down whenever half
  the keys were resting — and keys resting one second for a 503 counted, so it switched
  itself off in exactly the storm where it helps. Only keys resting for quota (per-minute,
  daily, credit) count now.
- **Which model answered.** After each reply on the Desktop, a short notice: the model,
  its first word and its total time, and "answered for gemini-3.8-flash" when a model from
  your fallback list stood in (through `hermes-kame-bridge`'s new notice seam; never on a
  messaging gateway, where it would become a chat message). The Events tab opens with
  *Last replies*. Nothing of it enters the conversation the model reads.
- **Clearer words:** *Speed* is Saver / Balanced / Fastest (stored values `off` /
  `adaptive` / `max` unchanged); "Switch to the next model after" is *Fallback after*,
  under *Fallback*; "Stop waiting and show an error after" is *Give up after*, now an
  advanced setting (a chat never gives up by default); the Events tab says *Fallback*.

**Not done, on purpose:** asking several keys at once when the provider is busy — tried
and removed the same day: an overloaded model is overloaded for every key, and parallel
requests during recovery break the 1.7.0.7 rule pinned in
`tests/test_v1_7_0_7_dispatch_recovery_loop.py`. Racing two models at once — the fallback
chain is Hermes'; KAME hands the call over within two failures instead.

**Evidence:** plugin suite 3285 passing, 0 failing (the clock and continuity gates are load-sensitive and pass alone); host corpus and host prose gates unchanged; host assumptions 28 / 29 (the runtime-contract row needs Hermes' own dependencies in the test interpreter); an adversarial review of the whole change, eleven findings, all fixed with a test each (`decisions/0028`); `live_daily` on the installed copy of four profiles; live on the author's own keys — healthy first model: every reply on it, first word 2–4 s; first model out of quota: handed over in 0.2–0.3 s, reply in 6–7 s, and the next reply after it is back is its own again; first model slow (gemini-3.8-flash 1 m 46 s – 2 m 18 s for one word on every key, the next one 14–47 s): median reply **2.6 s** over 8 messages (7 of 8 at 2.2–2.8 s), was 46–120 s; a 503 "overloaded" storm: handed over in 6.3 s, answered at 8.3 s (1.8.2.7 never moved); no "⚠️ Model fallback" line in any reply.

**Upgrading from 1.8.2.7:** install both `hermes-kame-api-rotation` and
`hermes-kame-bridge` 1.8.2.8 and restart Hermes. If you had set "Switch to the next model
after" or "Wait for the first token" yourself, those numbers keep working; choose
*Automatic* to let KAME size them.

---

## [1.8.2.7] — 2026-10-17 — every provider learned, automatic fallback list, fast when quota runs dry, settings anyone can read

**All 82 providers in the registry are now understood, from their own documentation.**
For each one an independent reviewer who never read this plugin wrote the answer key —
every error the provider can return (774 documented shapes) — and the plugin is
graded against it on the real failure path (`tools/provider_doc_gate.py`, with a
regression floor in the test suite). Agreement on what kind of failure it is: 96 → 120
of 132 in the first 16 providers and 80–131 of 95–149 in each of the other batches
(`research/1.8.2.7/expected/`). What the plugin learned, reading only the response (rule
R01: no decision looks at the provider's name):

- **A 403 that does not say the key is bad is a refused model, plan or policy, not a
  dead key** (401 is the credential, 403 is the permission). Three in a row used to
  retire a healthy key. Plan/role/tier refusals on a 401 or 403 are the same.
- **Money said in words or in a code is billing** whatever the status: "not enough
  balance", "account overdue", "credit limit exceeded", "kudos upfront", "used all
  available credits", "free trial quota exhausted", "token quota reached", a card missing.
- **A "quota" error that names a per-minute or concurrency counter is a throttle**,
  not an empty balance (it used to bench the key for an hour).
- **Capacity and saturation 429s** ("queue full", "service tier capacity exceeded",
  "maximum capacity", "model is being loaded", "high demand") rest the key one second,
  not thirty: the key is fine, the provider is busy.
- **Closed doors go back to Hermes at once** instead of asking every key: a 503 with no
  instance or endpoint, "maintenance", a router with no provider or channel available,
  a worker pool with no worker, a local server with no model loaded or not enough memory
  to load it, a model still loading.
- **Request faults dressed as other things** are raised, not rotated: a prompt that does
  not fit the context ("context size has been exceeded"), a safety or content refusal on a
  400/403, a 5xx carrying an `invalid_request` code, a 416.
- AWS Bedrock error names (throttling, service quota, model not ready, model error,
  invalid token, access denied) are read even when Bedrock files them under 400.
- NVIDIA's 429 (no body, or `{"status":429,"title":"Too Many Requests"}`) was already
  read correctly and is now proven on every run; "Function … not found for account" is a
  refusal for that account, not for the request. NVIDIA publishes nothing that tells
  "exhausted" from a plain rate limit, so none can be told apart.
- What only a provider's name could tell apart is listed, not guessed:
  `04_plugins/kame/providers/UNTEACHABLE.md`.

**Timeouts.** A provider timeout (408, 499, 504, 524 or a client-side one) rests the key
0 s — it says nothing about the key. The first one rotates to the next key at once; the
**second in a row goes back to Hermes** instead of walking the whole pool (the 13/10 Google
overload: 57 minutes, 294 attempts). 408/499/524 used to bench a healthy key for 20 s.
(decisions/0024)

**Your fallback list works by itself.** KAME still never picks a model. If you have set
fallback models in Hermes (`hermes fallback add`), then when every key of your model is
resting for more than "Switch to the next model after" (default 60 s) KAME hands the call
to Hermes at once and Hermes answers with the next model on your list — one instant hop
per spent model, not a wait — and Hermes comes back to your model by itself as soon as its
keys return, so the smartest model answers whenever it can. With no list nothing ever
switches. Short rests (a minute of rate limit) are waited out on your model. Hermes
switches at most `agent.api_max_retries` times in one message (default 3): the page warns
when your list is longer and names the value to set. Proven live on the owner's gateway:
3.8 to 3.5 spent, four hops in 0.2 s and 3-flash-preview answered; keys back, 3.8 again. Never
mid-answer, never when no later model is usable, never for background jobs. "Ignore my
fallback list" (advanced) turns it off. (decisions/0025, 0027)

**Running dry is fast.** When two keys in a row are refused for a long rest (a spent
daily quota), the next attempts ask 4, then 8, then 16 keys at once and the first that
answers wins; keys the provider itself said are still spent are asked all together.
Measured on the owner's chain (Gemini 3.8 -> 3.7 -> 3.6 -> 3.5 -> 3, 14 keys each, only
the last with quota, latencies drawn from 308 real refusals): first turn 58 s -> 27 s,
every next turn one request (0.6 s), the hourly re-check 59 s -> 13 s. Off when backup
requests are off. (decisions/0027)

**The panel no longer goes blank.** On Windows `state.json` refuses to open for a few
milliseconds while Hermes replaces it; the panel's backend answered that moment with
"503 snapshot unreadable" (up to 427 times a day in the owner's log) and a panel opened
then showed an error. It now retries and otherwise serves the last good reading; writers
wait out a reader instead of skipping a snapshot. Together with the `StringSetting`
crash fix, this is the 1.8.2.6 "menu stops rendering" report.

**Settings a new user can read.** Four settings are visible (when to switch model, when to
stop waiting, the speed boost, wait for the first token); the already-tuned ones sit behind
"Show advanced settings"; the eleven off switches stay in a folded Troubleshooting card.
"Your API keys" shows how to add keys (`/kame-keys add gemini K1,K2,...`); "keys that
share one quota" is gone from the page (every key is its own account). One plain sentence
per setting (the long text is the tooltip), one power scale (●●● High / ●●○ Medium /
●○○ Low), the fallback list and each model's keys inside "If your keys run out". Events:
a hand-over is a planned "Switched model", not an error; the Overview counts switches and
keys asked together; "Error passed on" says what happened; background jobs are named in
words ("Naming this chat"); the tab is "Background jobs". The Settings tab no longer fails
at the first text setting; credit is to KAME only, with a link to the repository.

**Packages.** The optional `hermes-kame-bridge` (the two Hermes seams still under review
upstream, #133474 and #133461) ships in every release beside the two plugins, at the same
version.

**Measured, not changed.** The plugin's own cost is 3–8 ms of CPU per call, with 32 callers
at once still ~170 calls/s through it (`tools/perf_probe.py`). Re-using a key does not
raise time-to-first-word (`notes/2026-10-16-ttft-reuse-measurement.md`). Verified live on
the real Gemini and NVIDIA keys (`tools/live_1827_smoke.py`): plain and streamed answers,
the hand-over with zero requests sent, and the fallback model answering.

---

## [1.8.2.6] — 2026-10-13 — the Events tab shows your conversation

From the owner's first day on 1.8.2.5: Hermes' background jobs (titles,
compression, vision, session search) were mixed into the Events tab with the
chat's own rotations, and a long Google overload on a background model buried
everything else. Nothing about rotation changes in this release — only what
the panel shows.

- **Events = the chat; Background = Hermes' side jobs.** The Background tab
  existed since 1.8.2.3 but stayed empty on a real Hermes: nothing ever marked
  a call as auxiliary (the planned hook runs in a separate worker thread, and
  the model-name fallback needs a model Hermes never passes). KAME now reads
  the marker Hermes itself sets for every auxiliary call, which reaches the
  call even when Hermes runs it on its protected thread. A background job on
  the chat's own model still goes to Background — and still shares the chat's
  pool, as before.
- **Each Background row names its job**, and the tab filters by it (click
  "title generation", "compression", …).
- **The lists scroll inside themselves** instead of stretching the page; the
  counters stay on top.
- **"Still waiting" line**: a call running for more than a minute says so in
  one sentence — which job, which model, how long, how many refusals, the
  last reason. KAME keeps waiting exactly as configured; the panel just says
  it is.
- **Clearer words**: a 400 that nothing else explains now reads "the provider
  rejected the request itself — another key would get the same answer"; a
  bare 429 with no quota number reads "usually the provider is busy, not your
  quota".
- The host seam check watches the new marker, so a Hermes update that removes
  it is named on the panel (cosmetic: rows would fall back to the Events tab).

## [1.8.2.5] — 2026-10-12 — backups for the chat: the first word wins

Everything 1.8.2.4 does, and the one thing it could not: the chat itself.
Hermes' chat streams, and until now backups only raced calls that do not
stream (titles, compression, vision). On the owner's own traffic (3,275
answered attempts) almost every answer is a stream, keys are never what the
user waits for (median pool wait 0.00 s), and the time goes to slow first
words — Gemini 3.8 Flash: median 5.5 s, worst tenth 21 s and more.

### Stream backups, raced to the first word
- A chat reply still **silent** after **twice that model's usual time to
  its first sign of work** gets a backup on another healthy key (each key is
  its own project, so the backup is real capacity); still silent after twice
  that, a third. The first key to show work — text, a tool call, or streamed
  reasoning (OpenAI-compatible `reasoning_content`, Anthropic `thinking`,
  Responses reasoning events) — wins; the others are cut at once: their HTTP
  response is closed, so they stop costing time. A reply that is thinking is
  working, not stuck, and never gets a backup.
- **Nothing changes until a backup is needed:** a lone stream's chunks pass
  straight through exactly as before, and a refusal before any backup rotates
  as it always did, with no waiting.
- **Every leg is judged on its own key**: a refused backup rests that key and
  cools backups for a minute; a cut leg's request still counts with the pacer
  and teaches the latency trackers (as a lower bound, so slow keys cannot make
  the trigger fire ever earlier).
- **Learned from the first call.** The trigger is twice the median of the
  last 200 first-sign times per model — seeded at the first race from
  `calls.jsonl`, which has been recording them since 1.6, so a restart does
  not start cold — and keeps learning from every race it decides.
- **Held to a budget** (about 10% extra requests; a new budget starts with
  its burst of 3), off under 429 pressure, never for very large prompts,
  never into a resting key; Max mode starts a backup on every reply at once.
  The user only switches hedging on or off.
- Continuations and tool-call replays are not raced; a winner cut mid-answer
  is continued on another key exactly as before.

### Measured
- Replaying the final policy over the owner's real first-sign times (budget
  10%, against 1.8.2.2 / 1.8.1.8, which have no stream backups):
  gemini-flash-latest mean 15.6 → 8.3 s and worst tenth 45 → 17 s;
  gemini-3.8-flash (global) 14.3 → 9.4 s and 38 → 20 s; gemini-3.7-flash worst
  tenth 20 → 14 s; glm-5.3 (global) worst hundredth 69 → 41 s; +9–15% requests.
  Anthropic models (already ~2 s to first sign) and Kimi K3 (uniformly slow, no
  stragglers) are left almost untouched, at +3–6%.
- Live on the owner's pools, alternating raced and unraced calls: Gemini 3.8
  Flash with forced 2 s / 4 s triggers, worst first word **7.4 s vs 33.0 s**;
  with learned triggers over 30 pairs, mean 7.2 vs 7.1 s and worst **16.5 vs
  27.1 s** (7 backups, 4 spoke first). NVIDIA GLM-5.3 with the final rules:
  one backup in 12 replies (it won), total time unchanged — the earlier
  text-only rule had raced replies that were already thinking, which is how
  the "silent, not slow" rule was found.
- Concurrency stress (12 threads, slow / empty / 429 / 503 / mid-answer cuts):
  0 internal errors, no hung threads, no race thread left behind.
- What it is: a cut of the worst waits. It does not make an ordinary reply
  faster — the provider's usual time to first word is the provider's.

### Settings and panel
- Hedging help texts now describe the chat race; `hedge_max_legs` 3 lets a
  still-silent chat reply try a third key.

---

## [1.8.2.4] — 2026-10-12 — the speed pass, made to do what it said

Everything 1.8.2.3 does, with every one of its ten integrations kept. This
release fixes what an audit of 1.8.2.3 (2026-10-12) found in the seams
between those pieces — places where each piece passed its own test and the
whole did something else — and corrects five things 1.8.2.3's notes and
panel promised that the code did not do.

### The backup request now actually saves time
- **The answer comes back when the first leg answers.** 1.8.2.3 joined every
  leg before returning, so a call whose backup answered in 0.25 s still
  reached the agent after the slow leg's full time (measured on the 1.8.2.3
  transport: 3.14 s). The slow leg now finishes on its own thread.
- **Every leg is judged on its own key.** A backup refused per minute rests
  the backup key and cools backups for that model for a minute (that cooldown
  existed in 1.8.2.3 but nothing called it). A primary that fails after
  losing the race rests the primary. A request that answers after losing is
  counted by the pacer and teaches the latency trackers.
- **The first useful answer wins.** Live on the Gemini pool a thinking model
  answered one leg with nothing while the other was still working, and the
  race handed the empty answer on, costing another attempt. An empty leg now
  waits for the other; it is used only if no leg brings anything better, and
  then the usual empty-answer rule rests the right key.
- **The large-prompt guard sees the prompt.** It measured the number of
  keyword arguments instead of the prompt's length, so a huge compression
  prompt could be sent twice. It now counts the prompt's text.
- **No backup to a key of the same quota group** — two keys of one project
  share one quota, and a backup there spends it twice for nothing.
- **Max mode fires from the start**, as its description always said; in
  1.8.2.3 it still waited for the threshold.
- **Thinking models are recognised by what they are.** Gemini 2.5 and later
  (except `-lite`) and any request that turns thinking on get the slower
  thinking threshold; 1.8.2.3 gave the Gemini pool the 6 s one.

### Latency means what each user of it means
- A **stream** is timed to its first sign of an answer; 1.8.2.3 recorded the
  whole stream, so a key that served a long answer looked slow in the
  tiebreak. A non-stream call is timed whole, as before.

### Pacing
- **Quota groups match the key as the Events tab shows it** (`key:a1b2c3`,
  or the six characters alone). In 1.8.2.3 no typed entry could ever match.
  Deleting an entry now ungroups the keys.
- **A learned per-minute limit can recover.** A refusal caused by other keys
  of the same project used to teach a key a near-zero limit until restart.
  Requests the provider answered inside one window now raise it — never above
  a limit set by hand.

### Settings and panel
- The 13 settings 1.8.2.3 added are declared in `plugin.yaml`, so they can be
  set in Hermes' `config.yaml` too (before, only the panel and environment
  variables reached them).
- The panel's build line reads the version instead of a hand-written
  "1.8.2.3".
- Corrected help texts: hedging (the other request is not cancelled; it only
  applies to non-streaming calls), "most parallel requests" (one backup at
  most), the per-model RPM limit (a ceiling, not a replacement), region and
  quota-group examples (the label format the panel shows), "%" shown as "%%".
- `pacing_interval_seconds` was never read by anything in 1.8.2.3. It stays
  so an older config still loads, and says so on the panel.
- The bare-429 ladder's title names RESOURCE_EXHAUSTED again (a 1.8.1.0
  promise the 1.8.2.3 rewording dropped).

### Tests
- New: the speed features driven through the real transport path
  (`tests/test_1_8_2_4_speed_paths.py`), and a registry-wide check that every
  setting has a real reader (`tests/test_1_8_2_4_every_setting_is_read.py`).
  Both fail on 1.8.2.3 and pass here.
- The full suite runs green again: 1.8.2.3 was checked with its own 166 tests
  only, and 16 older tests had gone red — settings lists that predated the
  text kind and the Speed shelf, two event-privacy checks that had stopped
  seeing any call site, a title, and a pacer state that leaked between tests.

### Known, unchanged from 1.8.2.3
- Backup requests for **streaming** calls stay off until a loopback harness
  proves the race can be cancelled cleanly. The chat streams, so the chat
  itself is not made faster by backups; auxiliary calls (title, compression,
  vision) are.
- Spinner and Messages-wire rotation still depend on upstream #133474 /
  #133461 or the optional `hermes-kame-bridge`.

---

## [1.8.2.3] — 2026-10-11 — the speed pass: pacing that learns, backups that fire, dead settings resurrected

Everything 1.8.2.2 does, plus ten integrations designed and built in this tree,
finished, wired to real runtime consumers and test-gated (165 own tests green, plus a live 5/5 smoke against a real 14-key
pool run by the developer before the merge).

### Speed
- **Adaptive hedging (automatic).** When one call is slower than the learned
  p90 time-to-first-answer of *this* pool:model, a backup leg is sent to a
  second healthy key and the first answer wins. Budgeted (~10% of calls),
  capped (2-3 legs), never fires on prompts above a size ceiling, never fires
  while the pool is under 429 pressure, and reasoning models get their own
  slower threshold so long thinking never burns quota on backups. The Events
  tab shows every leg and the winner.
- **Pacing that learns.** A sliding-window admission check per key, seeded
  from the provider's known per-minute limits and *learned* from real 429s.
  Zero added delay while a key is inside its window; the minimum wait when
  every key is inside one. Replaces the burst-unsafe GCRA of the prototype
  with the shape providers actually enforce. Per-model RPM overrides stay.
- **Latency tiebreak, working this time.** The prototype wrote samples under
  one key id and read them under another (the bug that made the whole feature
  dead code). The tracker is now fingerprint-idempotent — the bug class cannot
  exist — and it only breaks ties between *equally free* keys, so fairness of
  load stays first.

### The tab, the walls, the hints
- **Secondary Events tab.** Title, vision, compression and every other
  auxiliary call now lands on its own tab with a task column, instead of
  burying the chat's rotations under background noise. The Main tab is
  chat-only.
- **Host seam check with signature binding.** The boot check now *binds* the
  plugin's real call shape against each host symbol's signature and names the
  drift on the panel when a Hermes update changes something (it caught its
  first real drift on its first run). Registration never fails because of it.
- **Retry hints in three families.** Temporary → "retry, next key free in
  Xs". Daily-quota wall → "do not try now" (the old hint said "retry" on a
  spent daily quota and the agent looped forever — that is gone). Permanent →
  "do not retry". The exception type is preserved, no more rebuilt errors.

### The rest
- **Region in the pool identity.** Vertex path/subdomain, Bedrock and Azure
  patterns parsed from the endpoint URL; per-key overrides from the panel
  apply without a restart. A 429 in one region does not bench another.
- **Settings rewritten for humans.** Every new setting carries title, plain
  description, provenance (env/config/default) and a recommendation line; a
  25-assertion meta-test fails the suite if any setting is ever shipped
  invisible, ungrouped or unconsumed — the "dead setting" class of bug is
  now a build failure, not a discovery.
- **`reset_all` clears string settings too**, and the panel shows the live
  build line in its header so what you run is never a guess.

### Known, documented
- Streaming hedge race cancellation needs a dedicated loopback harness before
  it goes on; the decision layer is complete and tested, and the transport
  degrades to the single-key path there.
- The two upstream seams (`notify_turn_status` #133474,
  `create_messages_client` #133461) stay optional as in 1.8.2.2 — the
  `hermes-kame-bridge` companion provides both meanwhile.

---

- **Fixed within the day: a mid-stream failure path crashed the agent.** A
  mechanical edit had left ``bool(seen, call=call)`` on the streaming error
  path — Python's ``bool()`` takes no keyword arguments, so the first provider
  error *during* a stream on a real pool raised ``TypeError`` instead of
  rotating. Now ``bool(seen)`` with the call handed to ``_on_failure`` where
  it belongs, and a new AST gate (``test_1_8_2_3_no_bad_bool.py``) fails the
  suite if any ``bool()`` call ever grows kwargs again. Found by the owner on
  his live 14-key pool within an hour of the merge.

## [1.8.2.2] — 2026-10-07 — what the owner's own test found against 1.8.1.8

- **The Settings form is back on the panel's Settings tab.** 1.8.2.0 had moved
  it to Settings ▸ Plugins and left a link, which took away the 1.1.1
  editable panel. The form is now in both places: the same component, sending
  the same requests.
- **A setting saved from the panel is now in force in every Hermes process on
  the profile.** The first process to poll applies the request: the Desktop
  backend, the messaging gateway, or a second window. That process also writes
  `.env`, but before 1.8.2.2 only its own environment changed. In the owner's
  test the gateway took a `stream_silence_timeout_seconds` of 20 s, and the
  Desktop process serving the chat never had it. Every process now rereads
  `.env` within a second of it changing.
- **The panel no longer refreshes by itself.** Over `ctx.rest` a read can take
  longer than the one-second tick. Overlapping reads then resolved out of
  order, and the page flipped between an older and a newer snapshot. A single
  failed read also blanked the page and rebuilt it, which remounted the
  settings form. Now one read runs at a time, and the last good reading stays
  on screen for up to 15 s of failed reads. `tests/ui_reconcile.mjs` has two
  new checks for this; both fail on 1.8.2.1.

## [1.8.2.1] — 2026-10-07 — the cron bound the catalog review asked for

- **`max_total_wait_seconds`, unset, now depends on where the call runs.**
  - **A cron run** (detected with `gateway.session_context.get_session_env("HERMES_CRON_SESSION")`) gets 300 s. That is below Hermes' own `HERMES_CRON_TIMEOUT` of 600 s. Past it, the provider's original refusal goes to Hermes, and the Events tab records `gave_up`.
  - **A chat** keeps the unbounded wait (ADR 0002).
  - **Any value you set wins everywhere**, from the environment or from config. `0` means never stop waiting, in chats and cron alike.
- **The README the catalog renders states plainly** that `hermes-kame-bridge` patches Hermes core at runtime and is not supported by Hermes.

## [1.8.2.0] — 2026-10-06 — the catalog review, every call on the Events tab, keys read on the new Hermes

Everything 1.8.1.9 does, plus what the catalog review of
NousResearch/hermes-agent#133362 asked for and what the owner's own test on the
newest Hermes found.

### The catalog review
- **The panel uses only `@hermes/plugin-sdk`.** It reads the snapshot and sends
  its requests through `ctx.rest`, the SDK's door to a plugin's own backend.
  The backend is a new `dashboard/plugin_api.py` with two routes, `/state` and
  `/control`. Nothing calls `window.hermesDesktop.*` any more. `ctx.rest` is
  profile-scoped, so the panel now shows the profile you are looking at, where
  1.8.1.9 read the base home while each profile wrote its own.
- **Settings moved to Settings ▸ Plugins ▸ KAME API Rotation.** The form, its
  switches, numbers, validation and buttons are unchanged. The panel's
  Settings tab now holds a link to that page.
- **`max_total_wait_seconds`** (`KAME_MAX_TOTAL_WAIT`) is new and **off by
  default**, so ADR 0002 is unchanged. A call waits for as long as every key is
  resting, each key's rest is capped by `max_hold_seconds`, and the call retries
  the moment one key returns. Set a number for cron or an unattended gateway:
  once one call has waited that long, the provider's original refusal goes to
  Hermes and Hermes' own failure path runs. The Events tab records this as
  `gave_up`.
- **Refusal records are smaller and private.** `message`, `body` and `response`
  are capped at 600 characters, the same as `core.redact`. The fields the
  corpus measures (`quotaId`, `quotaMetric`, `retryDelay`, `reason`, …) are
  pulled out of the full text first, into `fields`, so the cap loses no
  evidence. `refusals.jsonl`, `calls.jsonl` and `settings-changes.jsonl` are
  created owner-only (0600).

### Events show everything
- Every call now leaves a `sent` row: the key, and how many keys were ready.
  Every first-try answer leaves an `answered` row: how long the first word took
  and how long the whole call took. A slow answer on a healthy pool used to
  leave no trace on the panel. The buffer holds 400 rows (it held 150).

### The newest Hermes
- **Multi-key variables sync again.** Hermes now reads keys through a
  profile-scoped secret store and refuses an unscoped read
  (`UnscopedSecretError`). The `.env` sync binds the profile's own scope
  (`agent.secret_scope.build_profile_secret_scope`), and the background sync
  thread inherits the caller's context.
- The status line looks for `notify_turn_status` again on every use instead of
  once at start, so the seam is found even when it appears after KAME loads.

### Optional: `hermes-kame-bridge` (not in the catalog)
This separate plugin adds the two Hermes seams KAME uses that are not merged
yet, and only where Hermes lacks them:
- **`notify_turn_status` (#133474):** KAME's live line appears on the spinner
  in the CLI, the TUI and the Desktop.
- **`ProviderProfile.create_messages_client` (#133461):** per-call key
  rotation on the Anthropic Messages wire (Anthropic, MiniMax, Kimi coding).
  Without it, that wire used Hermes' own client, and measured exactly like
  Hermes without KAME. 1.8.1.8 had rotated there by patching Hermes.

A seam Hermes already ships is left alone, so the bridge is harmless after
updating Hermes. It patches Hermes code, which is why it is not in the catalog.

### Tests
- The UI harness now walks the Settings and Events tabs it opens. It used to
  render them only after switching back to Overview. The keyless rows it then
  found in the settings form are keyed.

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
1.8.1.8, once now). Tables: `research/1.8.1.9/harness/`.

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
