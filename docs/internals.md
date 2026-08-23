# Internals

How KAME works underneath, and why each decision was made the way it was. Nothing
here is needed to use the plugin — start with the [README](../README.md). This is
the file to read before changing the code, or when you want the evidence behind a
claim the README makes in one line.

---

## No provider allowlist

v0.0.1 and v0.0.2 only acted on Gemini. That was a regression, not a scope decision: an allowlist is a promise to do nothing for whichever provider is not on it, **including every provider that does not exist yet**. Agent Zero's KAME never had one.

v0.0.3 acts on **evidence, not identity**. The rule is: speak when the response carries retry timing the host does not read, decline otherwise. The cascade, strongest first:

1. **Exception attributes** — `retry_after` (litellm, OpenAI, Anthropic SDKs), `retry_delay` as a protobuf `Duration` (Google).
2. **HTTP headers** — `Retry-After` (RFC 7231: seconds *or* an HTTP-date), then any rate-limit reset header. Header *names* vary per provider; their shape does not, so they are matched by shape (`x-ratelimit-reset-requests`, `anthropic-ratelimit-tokens-reset`, `x-ratelimit-reset`, …). When several are present the **longest** wins: releasing a key early re-hammers a limit that is still spent, which is the failure that cascades.
3. **Structured body** — any key naming a retry or reset, found by walking the body rather than by encoding each provider's layout. Depth-bounded, because the body is arbitrary remote JSON.
4. **Free text** — a duration after a retry keyword. Compound forms included: `6m11.52s`, `2h 30m`, `4hr 5min`, `1500ms`.

Values are bounded before they leave the module. A provider that says "wait 99999999 seconds" is either broken or being parsed wrong, and neither is a reason to lose a credential for three years.

Independently of the delay, the **quota window** is read — per-minute, hour, day, week, month, or account-level — from markers checked widest-first. That is the part that makes a daily cap behave differently from a throttle, and it is why `PerDay` in a body beats a `37s` hint in the same body.

The single provider-specific rule left is midnight US/Pacific for a daily window, which is a Google fact. It is gated on `looks_like_google()`, which checks the provider name **or** the fingerprints in the response body — so a proxy or aggregator forwarding a Google error still gets the right reset time, and everyone else falls back to the conservative hourly re-probe.

## A key count that means what the pool means by it

`/kame-keys status` is where the user goes to answer one question: *do I have working keys?* It was answering with the number of rows in the pool.

The pool does not count that way. Before it looks at status, cooldown, priority or anything else, it does this:

```python
if entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
    continue
```

A row with no runtime key is skipped outright. It is not a key having a bad day — it can never serve a request. Three ways a pool ends up holding one:

- an **env source that resolves to nothing** — the row is seeded from `GOOGLE_API_KEY` and the `.env` line is commented out;
- a **borrowed credential**, which persists as a metadata-only reference and is hydrated on load, leaving stale duplicates unhydrated;
- a **lease that expired** — a `nous` entry keys on its invoke JWT and its `runtime_api_key` becomes `""` the moment that JWT stops being usable.

None of these change `last_status`, because nothing ever happened to them. So the row reads `ok`, and a pool of three keys with no working credential in it reported as three keys, all fine.

Two things were wrong, and they were the same thing: **KAME read `access_token`, the stored field, while the host runs on `runtime_api_key`, a computed property.** That mismatch reports a dead row as healthy *and* a live `nous` credential as blank. Both directions are now tested, and the count is split when they disagree:

```
gemini — 2 of 3 key(s) usable
  [ok] kame #1  AIzaSy…AAAA  (0 reqs)
  [ok] kame #2  AIzaSy…BBBB  (0 reqs)
  [no key] GOOGLE_API_KEY  (empty — the pool skips it)
```

An OAuth entry legitimately carries no API key and is left alone — the host's skip rule is scoped to `AUTH_TYPE_API_KEY`, and widening it here would have replaced one wrong answer with another.

### What was considered and not done

`/kame-keys status` does not show *how long* a benched key has left, even though KAME computes exactly that. It stays that way: `/kame-quota` renders the ledger with deadlines and per-model scope, and duplicating a live number in two commands is how the two start disagreeing. `status` answers "what is in the pool", `quota` answers "until when" — the defect here was `status` giving a wrong answer to its own question, not answering the other one.

An earlier suspicion, checked and dropped: that an env var shadows the pool entirely, since `_resolve_api_key_provider_secret` tries `api_key_env_vars` before the pool. It does — but both paths that matter reach the pool first (`agent_runtime_helpers` calls `pool.select()`, `auxiliary_client` calls `_select_pool_entry` and only falls through when the provider has no pool at all). Rotation is not affected. Recorded because the wrong version of this note would have shipped a warning about a problem that does not exist.

## A credential that holds several keys

Hermes seeds a pool from a provider env var like this:

```python
token = _get_env_prefer_dotenv(env_var)          # agent/credential_pool.py
...
_upsert_entry(entries, provider, source, _env_payload(token=token, ...))
```

One env var, one pool entry, whatever the string contains. There is no split
anywhere on that path. So `GOOGLE_API_KEY=k1,k2,k3` is a **single credential
whose key is the 119-character string `k1,k2,k3`** — measured against the
installed host before this version existed:

```
entries created: 1
  label= GOOGLE_API_KEY | key len= 119 | commas in key= 2
available: 1
```

Every request sends that string whole, the provider rejects it, and the pool
has one credential and nowhere to rotate to. A rotation plugin with one key is
not doing anything.

That list is not a mistake by whoever typed it. It is the format Agent Zero's
key pool accepts, the format this plugin's own `/kame-keys add` accepts, and
the obvious way to write down a set of keys. So the fix is here, not in an
instruction telling anyone to retype anything.

KAME derives the parts on load, using the same separator rules as
`/kame-keys add` — comma, semicolon, pipe, newline, tab, whitespace, and a
byte-order mark that survived a paste:

```
gemini — 7 key(s)
  [list] GOOGLE_API_KEY  (holds 7 keys — the rows below)
  [ok] GOOGLE_API_KEY (1/7)  AIzaFa…AAAA  (0 reqs)
  [ok] GOOGLE_API_KEY (2/7)  AIzaFa…BBBB  (0 reqs)
  ...
```

Three things make that safe to do:

**Nothing is written.** The parts exist for the length of a load and are
recomputed on the next one, which is what keeps them correct when the env var
is edited — there is no second copy of the key list to go stale. `_persist` is
wrapped to hide them for the duration of a write; `write_credential_pool`
merges back any row on disk that is missing from what it is given, so leaving
them out cannot delete anything. Splitting is switched on **by** that guard: a
Hermes whose `_persist` this plugin cannot wrap does not get the feature.

And the host refuses too, independently: a derived source (`env:GOOGLE_API_KEY#kame-key-2`,
or `manual#kame-key-2`) is not in `_PERSISTABLE_PROVIDER_SOURCES`, so
`is_borrowed_credential_source` calls it borrowed and
`sanitize_borrowed_credential_payload` strips its secret at the disk boundary.
That holds with the plugin uninstalled and the parts still in a live pool —
asserted in sandbox section 22.

**Identity is the key, not the position.** A part's id is a truncated
SHA-256 of the key. Deleting the first key of a list would otherwise renumber
every key after it, and the ledger — which remembers what is spent by id —
would read all of them as credentials it has never tried, so a pool that is
out of quota would look fresh. The hash is one-way, so the id is safe in a log
and on disk.

**The list is reported as a list.** Redacted, `k1,k2,k3` prints as
`AIzaSy…cccc` — the start of the first key and the end of the last — which is
indistinguishable from an ordinary credential. It is named `[list]`, and it is
counted as none of the keys inside it, because those are counted where they
appear.

### What was considered and not done

Persisting the parts would have been simpler at runtime and was rejected: it
creates a second copy of a key list whose only correct source is the `.env`
line behind it, one that does not update when that line is edited, and that
would be split again on the next load into parts of parts.

An explicit exclusion for `nous` was written and then removed. Its runtime
credential is an invoke JWT read from `agent_key`, and the property returns
either that JWT — which has no separator in it — or the empty string when it
has expired. Neither splits, and no test could be made to fail by deleting the
exclusion, so it was deleted.

A value that splits into tokens too short to be keys is left alone. The
`MIN_KEY_LENGTH` bound belongs to `/kame-keys add` and applies here unchanged:
inventing credentials out of things that do not look like credentials is a
worse failure than leaving one odd row alone.

## How it hooks in

Three hooks and one binding.

**`transform_api_error_classification`** — fired once per **failed** API call, before Hermes' own classifier pipeline. KAME returns a `reason` plus an `error_context.reset_at`, which the credential pool normalises into `last_error_reset_at` — the value `_exhausted_until()` returns *instead of* the default TTL.

**`pre_api_request`** — an observer. Hermes discards whatever this hook returns, by design; KAME registers it for one side effect, recording which model the request in flight is for. That is the fact the credential pool is never told, and everything below depends on it.

**`post_api_request`** — the other observer, fired once per **successful** call. It is how a prediction gets checked: a key that answers before the deadline KAME set proves that deadline was too long. The payload carries no credential identity, so the key is read from the pool's own selection instead — see [The journal](#the-journal). Nothing is written unless the success closes a question KAME had actually asked.

**The binding** (`pool_binding.py`, `aux_binding.py`) — wraps six functions so the pool can act per model. See [Closing the per-model gap](#closing-the-per-model-gap).

The hooks are documented extension points, not patches. Consequences:

- **Cold path.** Fires only on failure. Zero cost on a successful call.
- **Fail-safe.** Callbacks run inside Hermes' isolation wrapper; malformed returns are dropped. A fault here degrades to the built-in classifier.
- **Declines by default.** A 429 with no quota signal, a bare 401 or 403, anything unrecognised — all return `None` and leave the host in charge. Declining is the common case and the safe one.
- **Kill switch.** `KAME_ROTATION_DISABLED=1` makes it a no-op without uninstalling.

### Where it deliberately says nothing

Running at step 0 means that when this hook answers, **the entire built-in pipeline is skipped** — including checks that sit above status routing. Three places where Hermes' own answer is already the better one, and speaking would replace it with a worse one:

- **Provider congestion** (5xx, or a 429 whose body says "Overloaded"). Hermes routes these with no credential rotation on purpose — rotating "exhausts the pool while the endpoint is still busy, and does nothing for a single-key user" (#14038). Since `reset_at` is only ever applied by `mark_exhausted_and_rotate()`, any cooldown here would need exactly the rotation that must not happen.
- **An aggregator relaying somebody else's failure** — OpenRouter's `"Provider returned error"` envelope with the real error in `metadata.raw`. Hermes classifies it as `upstream_rate_limit`: the user's key is healthy, so fall back to another model rather than burning it. This one is the sharpest edge in the plugin: the nested upstream text is full of statements about a credential that is not ours, and an upstream "API key not valid" would otherwise mark the user's healthy aggregator key permanently dead.
- **A 403 with nothing recognisable in it.** Status alone is not evidence. Hermes checks content-policy blocks *ahead of* its own status routing, so claiming every 403 would hijack a per-prompt safety refusal and bench a healthy key for an hour over a prompt the model simply declined.

The mirror image of that, and the reason `should_fallback` is set explicitly on every verdict: Hermes builds its result with `ClassifiedError(**plugin_result)`, where the flag defaults to `False`. It sets it on every `rate_limit`, `billing` and `auth` it produces itself — so a hint left out is a hint switched **off**, not one inherited.

## Layout

```
kame-hermes/
├── hermes-kame-api-rotation/   ← the installable plugin, self-contained
│   ├── plugin.yaml
│   ├── __init__.py             ← Hermes adapter: four hooks, wires the rest
│   ├── runtime.py              ← what is in flight: model, verdict, chosen key
│   ├── store.py                ← the ledger's and journal's home in ctx.state
│   ├── dispatch_binding.py     ← the carousel: a key per call, rotation on failure
│   ├── resolver_binding.py     ← the first key of a turn, taken from the pool
│   ├── field_binding.py        ← the Settings field, widened to hold several keys
│   ├── pool_binding.py         ← per-model reads and writes on the pool
│   ├── aux_binding.py          ← the auxiliary lane, which fires no hooks
│   ├── commands.py             ← /kame-keys, the only module that writes keys
│   ├── status.py               ← /kame-quota, which only ever reads
│   ├── menu.py                 ← /kame, the settings panel Hermes never draws
          ← host knobs KAME claims, only where they are unset
│   └── core/                   ← framework-agnostic decision rules
│       ├── carousel.py         ← which key goes out, and what a refusal costs it
│       ├── quota.py            ← evidence cascade, quota windows, reset timing
│       ├── classify.py         ← verdict: reason + reset_at, or decline
│       ├── ledger.py           ← (credential, model) → until when
│       ├── reconcile.py        ← ledger + pool state → release / hold
│       ├── journal.py          ← what was predicted vs. what actually happened
│       ├── probe.py            ← when to test a prediction instead of trusting it
│       ├── dispersion.py       ← of the keys that work, which one goes out next
│       ├── report.py           ← rendering, and nothing else
│       ├── tally.py            ← how many failures KAME was asked about, and read
│       ├── multikey.py         ← one field holding several keys, read as several
│       ├── escalate.py         ← how much longer, once a deadline is measured short
│       ├── answer.py           ← the answer that carried nothing, and proves nothing
│       └── keys.py             ← bulk key parsing, dedupe, redaction
├── tools/
│   ├── sandbox_binding.py      ← the bindings against the REAL Hermes, sandboxed
│   ├── host_corpus.py          ← Hermes' own error corpus, with and without KAME
│   ├── host_pool_suite.py      ← Hermes' own pool suites, with and without the binding
│   ├── host_assumptions.py     ← the host facts KAME's *non*-decisions rest on
│   ├── live_429.py             ← 429s off a real socket, all the way to a bench
│   ├── live_multikey.py        ← one provider field, several keys, through the real loader
│   ├── deploy.py               ← copies into the install and checks the manifest
│   └── verify_installed.py     ← the *installed* plugin, inside a real Hermes process
└── tests/                      ← 1416 tests, no network, no real keys
```

`core/` imports nothing from Hermes or Agent Zero on purpose. The decision rules are the asset; the host binding is disposable. The same core can back an Agent Zero build or a standalone proxy later.

`commands.py` reaches the host through five small bridge functions (`_load_pool`, `_make_entry`, `_pooled_providers`, `_known_providers`, `_unsuppress`), each a lazy import. That is why the whole command surface is testable against fakes without Hermes present, and why a Hermes refactor surfaces as one failing bridge rather than a plugin that will not load.

## Verify

```bash
python -m pytest tests/ -q
```

```bash
hermes plugins doctor ./hermes-kame-api-rotation --ci
```

```bash
python tools/sandbox_binding.py
```

```bash
python tools/host_corpus.py
```

```bash
python tools/host_pool_suite.py
```

```bash
python tools/host_assumptions.py
```

```bash
"$HERMES_HOME/hermes-agent/venv/Scripts/python.exe" tools/live_429.py

# the shape the user actually has: several keys in one provider field
"$HERMES_HOME/hermes-agent/venv/Scripts/python.exe" tools/live_multikey.py
```

```bash
"$HERMES_HOME/hermes-agent/venv/Scripts/python.exe" tools/verify_installed.py
```

Current state: 1416/1416 tests pass; doctor reports `manifest: hermes-kame-api-rotation 1.2.0 (standalone)` and `OK: runtime discovery, manifest parsing, import, and registration passed`; the sandbox passes all 22 sections against the installed Hermes; Hermes' own error corpus gives the same 89/89 with KAME behind the hook as without it; Hermes' own credential-pool suites answer identically apart from two named assertions about the host's `fill_first` default, which is the default v0.2.6 replaces on purpose — both fail with the plugin as shipped and pass again with `KAME_SPREAD_DISABLED=1`, and the harness fails if either half of that stops being true; `live_429.py` drives four refusals off a real socket through the installed plugin, in eleven phases, reads back the benches they produce, the count of how many of those refusals KAME could size, and that an answer carrying nothing leaves a standing bench standing while a real one releases it; `live_multikey.py` puts three keys in one `GOOGLE_API_KEY`, loads them with Hermes' own `load_pool`, rotates through them on real refusals, and spreads five keys with no refusal at all, then reads that spread back out of `/kame-quota` and watches it vanish when the ordering is taken away, in ten phases; `verify_installed.py` finds the live plugin enabled, three hooks registered, all eight wrap points in place, both commands resolved, a real state directory on disk, and the host answering for both config keys while refusing a name it should refuse; and `host_assumptions.py` confirms all thirteen host facts behind the pieces KAME deliberately did not port — the nine behind what it chose not to port, the three the unbounded wait of 1.0.1 rests on, and the one that keeps Agent Zero's tool-argument heal on the Agent Zero side, each one sabotaged first to prove the check is not vacuous, four of them re-sabotaged on every run.

Pass the plugin path to `doctor` explicitly. Its default target is `.`, and pointed at a large tree — this workspace, or the Hermes install with its venv — the scan does not finish in any useful time. It is not a plugin failure and it produces no output while it runs, which makes it easy to misread as a hang caused by the last change.

`plugins doctor` validates against the real runtime contracts **without installing** — which is why it is the gate before anything is copied into the live install. Note that it counts tools and hooks only, so it reports `3 hook(s)` and says nothing about the commands; their registration is pinned by `test_registers_both_slash_commands` instead. That test exists because `register()` wraps command registration in a `try/except` (so a command failure can never cost the rotation hook), and that guard would otherwise hide a command that had silently stopped registering.

`sandbox_binding.py` is the one that matters for the binding: the unit tests state the rules against a stand-in pool, and a stand-in that drifts from the host is worse than no test. It redirects `HERMES_HOME` to a temporary directory before importing Hermes, so no real pool file, credential or plugin state is read or written.

`host_corpus.py` is the only one of these whose payloads this project did not write. Every fixture here came from the same hand that wrote the classifier, which makes them a statement of intent — and that has already been wrong: until v0.1.3 three versions were green against a sentence Google does not send. Hermes ships its own corpus, 89 cases over roughly fourteen providers, written by people who never saw this plugin. The harness runs it twice against the real `get_plugin_error_classification` dispatch — once with the hook absent, once with KAME's callback behind it — because KAME is consulted *before* the whole built-in pipeline and the first valid answer wins, so a hook that claims too much silently replaces fourteen providers' worth of tuned classification with its own opinion. Any test that changes outcome is a payload KAME took from the host. It found two in v0.1.9, and the host was right in both.

`host_pool_suite.py` asks the same question about the half of the plugin that reaches furthest into the host. The binding replaces five `CredentialPool` methods — every path selection and persistence take — and the sandbox only proves those wrappers do what KAME wants against a pool KAME built. It cannot prove they left the host's own behaviour alone. So the harness runs fourteen of Hermes' own credential-pool suites twice: clean, and with a real `PoolBinding` installed on the real class. Deferred refresh, lease reselection, OAuth write-through, quarantine locking, provider boundaries, sole-credential cooldown and rotation bounds all answer identically. Two tests fail in both runs for an unrelated missing dependency in this environment, and the harness names them out loud rather than subtracting them in silence, because "changed nothing" must never be read as "everything passed".

Two more answer differently since v0.2.6, and they are named the same way. Both build a two-key pool, select once, and require the *same* key back on the next select — the host's `fill_first` default, stated as setup rather than as the property under test, and exactly the default this version replaces. A list of tolerated failures would be a hole, so each one carries a double requirement instead: it must **fail** with the plugin as shipped and **pass** with `KAME_SPREAD_DISABLED=1`. Failing both ways means something else broke; passing both ways means the feature quietly died. Either one fails the harness.

That comparison would print the same reassuring line if the harness were quietly inert, so it ends by breaking the binding on purpose — one credential hidden from selection, the exact damage a careless wrapper does — and refuses to report success unless the host notices. It notices in 58 tests. The real binding costs zero of them.

`host_assumptions.py` is the odd one out: it checks nothing KAME does. Several pieces of the Agent Zero engine were deliberately not ported, each with a citation to the line in Hermes that makes the port pointless or wrong — and a citation is a claim about somebody else's code, which moves. So the claims are asserted against the installed Hermes source instead: that an empty answer is retried on the same key, that the pool is asked once a turn rather than once a call, that the live credential changes only on the error path, that a content refusal returns before the success hook ever fires, that the hook still carries the two counts v0.3.1 reads, that only one of the three error reports is classified, that the four API-side hooks the host dispatches are still the four KAME weighed, that the manifest still declares the three it registers, and that none of the seven capabilities Hermes gates behind user consent covers anything KAME does — so it installs with no grant to give, and a future capability naming credentials or the pool fails the proof instead of degrading the plugin in silence. Every check was sabotaged before it shipped, and the harness sabotages three of them on every run. The seventh caught a bug in itself first: `"_api_"` is not a substring of `api_request_error`, so the filter was dropping the one hook the check existed to notice. A failure here is not a bug in KAME — it means a door opened in the host and a decision needs making again.

`live_429.py` is the one that watches the whole thing happen. Everything above stops short of the wire — the sandbox builds the exception by hand, the corpus feeds payloads to the classifier, `verify_installed` proves the wrappers are attached without ever firing one — so the sentence this plugin exists for, *a 429 arrives and a cooldown comes out*, had never been observed from end to end. A local server answers with a verbatim Google free-tier 429, the real OpenAI SDK raises a real `RateLimitError` off a real socket, Hermes' own `classify_api_error` runs with the installed plugin behind its hook, and the host's own recovery benches and rotates a real `CredentialPool`. Two refusals rather than one, because the claim worth checking is a distinction: the same sentence and the same 21-second retry hint arrive for a per-minute throttle and for a daily cap, and the per-minute one is held for 21 seconds while the daily one is held to the next midnight US/Pacific. Two more phases follow the same shape. The auxiliary lane fires no hooks at all, so a real relay call asks from inside the call whether the key the main model just spent is offered to the smaller model — and the same pool, one moment later with the announcement unwound, withholds it again. And a sole credential benched by KAME's own number is offered back for a test five minutes on, because a bench of ours must never be the thing that locks a model out; only the clock is moved there, the bench and the pool are live. Then it switches KAME off through its own kill switch and fires the same payload again, to show the deadline disappearing — a proof that cannot fail is not measuring what it names. No provider is contacted, no quota is spent, the keys are obvious fakes, and the only endpoint is this process's own socket; what it does not claim is the provider itself.

`live_multikey.py` watches the shape the user actually has. Somebody who owns several Google keys types them where Hermes asks for a key, separated by commas — the format Agent Zero's key pool takes, the format `/kame-keys add` takes, and the only way Hermes offers to say *here are my keys* at all. The unit tests prove the split rules and the sandbox proves them against a pool this project built; neither goes through `load_pool`, the function that reads the real environment variable through the real provider registry and builds the pool a real run uses. So this sets `GOOGLE_API_KEY` to three keys, calls Hermes' own loader with the installed plugin behind it, and finds three usable credentials where the host alone finds one — then drives two real 429s off the socket and watches the pool walk from the first key to the second to the third, checks that the row on disk is still the single one that was typed, that editing the list leaves the surviving keys as the same keys, and finally takes the binding out and confirms the host is back to one credential whose key is the whole comma-joined string. A last phase answers the only question the design record could not: there is no cap on how many keys one field may hold — the sole per-key rule is a length floor — so fifteen go in, the pool is built in about 24 ms, and rotation walks all fifteen one refusal at a time.

`verify_installed.py` answers the one question none of the above can: doctor proves `register()` runs against a stand-in context, but not that inside a **real** Hermes process, with the real plugin manager and the real config, the wrappers actually landed on the real `CredentialPool`. It drives Hermes' own discovery over the live plugin directory and then asks the class. No provider is contacted and no credential is used. It must run under the Hermes venv interpreter — the `python` on PATH here is a Store build whose `AppData\Local` is virtualised, so it sees an empty directory where the install is.

## Closing the per-model gap

Hermes keys its credential pool by **provider**. Several providers meter quota per key **per model** — Google says so in the error body it returns, whose `quotaId` reads `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`. A key exhausted on `gemini-3.6-flash` is therefore benched for auxiliary work on `gemini-3.5-flash-lite` too, even though that model's allowance is untouched.

**This is not a nice-to-have. Without it, sizing cooldowns accurately makes things worse.** Before KAME, a daily cap got the flat one-hour default, so the auxiliary model lost the key for an hour. With KAME correctly reading "resets at midnight", the same key is benched for the real duration — and because the bench is provider-wide, the model that spent nothing loses it for the whole day. Precision without scope is a regression, so the two ship together.

### What it takes

The ledger — one row per `(credential, model)` with the deadline that key earned there — lives in `ctx.state`, Hermes' own plugin storage. Reading and writing it needs six wrap points, because the pool exposes no seam for a model:

| Wrapped | Why |
|---|---|
| `CredentialPool._mark_exhausted` | The single place a bench is written. Files the same deadline the host stored, against the model in flight. |
| `CredentialPool._available_entries` | The single place "which keys can I use" is answered. Adds back a key benched on another model; withholds one spent on this one; and offers one back anyway when that leaves the model with nothing — see [The escape hatch](#the-escape-hatch). |
| `CredentialPool._select_unlocked` | **Optional, statistics only.** The single place a credential is actually chosen. Mirrors that choice so a later success has a name to attach to. If a Hermes release renames it, the plugin loses a statistic and nothing else. |
| `auxiliary_client._relay_sync_completion` / `_relay_sync_stream` / `_relay_async_completion` | The auxiliary lane fires **no hooks at all**, so its calls would otherwise look unannounced. These three are every auxiliary request's way out to a provider. |

They are wrapped on the class or module, not on an instance, because the pool object is not shared: the conversation holds a long-lived `agent._credential_pool` while the auxiliary path calls `load_pool()` and gets a fresh object each time.

### The rules that keep it safe

- **Nothing is written to the pool, ever.** The original method runs first, in full; the wrappers only add to or subtract from the answer it produced. Every host invariant — cooldown clearing, DEAD pruning, OAuth refresh, persistence — is untouched.
- **A bench KAME did not write is never released.** Ownership is proved by the deadline: KAME records exactly the number the host stored, so a bench whose deadline matches nothing in the ledger belongs to another writer and is left alone.
- **DEAD is never released.** A revoked key is broken for every model.
- **Unknown means "do as the host would".** No announcement, an announcement from a different provider, an unreadable ledger, an exception anywhere in the wrapper — all fall through to the host's own answer.
- **It refuses to install unless it recognises what it is patching.** `inspect_module` checks every method name, parameter name and entry field at every start. A Hermes release that moves any of them degrades the plugin to cooldown sizing alone and says so once in the log. This is the part that makes wrapping internals defensible: the failure mode is *less* plugin, never corrupted credential state.

`tools/sandbox_binding.py` runs all of it against the **real** installed Hermes in a throwaway `HERMES_HOME`, including the genuine auxiliary relay body.

### Still open

The hook payload carries `provider` and `model` but not which credential failed, so Agent Zero's per-key adaptive escalation had no hook-side identity to attach to. The pool binding sees the resolved entry directly on the failure side, and v0.0.6 recovers it on the success side too. v0.0.9 supplied the *reset*: a proven-working key is released for good. v0.1.0 supplies the escalation — [see below](#when-the-deadline-was-measured-short) — built, as planned, on [the journal](#the-journal).

What is genuinely still open is smaller and harder: **no automated proof covers a live quota running out.** The 429 in the harness is a captured payload replayed off a local socket, which is enough to prove the whole chain end to end and is not the same thing as a counter reaching its limit at the provider. Daily use has since produced live evidence of its own — 1.1.2 and 1.1.3 both exist because of something a real provider did — but that is a record of incidents, not a test that runs. [The sentence two providers share](#the-sentence-two-providers-share) is why the distinction is worth keeping: four versions were green against a Google error message Google does not send.

## The journal

Everything above is a **prediction**: the provider said something, KAME read a deadline out of it, the key went to the bench until then. A prediction that was too short and a prediction that was too long both look, from outside, exactly like a call that failed and later worked. Nothing in Hermes or in the plugin could tell them apart.

`core/journal.py` is the notebook that can. It records two things and refuses to record anything else:

- **A block** — every real refusal: when, which provider, which model, which key, which quota window, who sized the cooldown (KAME or the host's flat default), and the deadline that was set.
- **A recovery** — the paired answer: the moment that same key answered on that same model again.

From those two, the only two mistakes worth knowing fall out:

| Mistake | How it shows up | Threshold |
|---|---|---|
| **Predicted too short** | a second block on the same key and model lands within 3 minutes of the deadline the first one set — the key was let back in before the quota had actually rolled | twice, before it counts |
| **Predicted too long** | a success arrives *before* the deadline — the key was benched longer than it needed to be | twice, before it counts |

Two occurrences, not one, because a single coincidence is not evidence — a genuine second burst can land right after a real reset.

Four properties, in the order that matters:

- **It steers nothing.** Selection, benching and release do not consult it. The journal is read by `/kame-quota` and by nothing else. It exists so that when a later version does tune the guesses, the change is made against recorded fact rather than against a guess about a guess. (The escape hatch in v0.0.7 acts on the *ledger*, not on the journal.)
- **A success writes nothing.** `record_success` returns "do not persist" unless there is an open block for that exact key and model. The overwhelming case — an ordinary successful call — touches memory and stops. Pinned by a test that runs 50 successes and asserts zero writes.
- **It never holds a key.** A journal entry names a credential by the pool's own opaque id. No token, no label, no fragment of one — which is why the report can be printed into a chat transcript at all.
- **It forgets.** 14 days, 300 blocks, 300 recoveries, pruned on every load. Removing a key from the pool removes its history with it.

The credential identity comes from `_select_unlocked`, not from the hook: `post_api_request` carries the model and the provider but never says which key served the call. That mirror is best-effort by construction, so an entry is dropped rather than guessed when the selection is not known — a missing statistic, never a wrong one.

## The escape hatch

Accuracy has a failure mode, and v0.0.6 made it worse rather than better: the more precisely KAME sizes a cooldown, the more completely it disables the host's own last-resort behaviour.

Hermes has `EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS = 60`, with this reasoning in the source: when the offending key is the only non-DEAD entry, a one-hour bench "means an hour of hard failures with nothing to fall back to", so a transient throttle cools down for a minute instead. The same comment ends with *"Provider-supplied reset_at still overrides"* — and a provider-supplied `reset_at` is exactly what this plugin exists to supply. Read a daily cap correctly on a single-key pool and the user has no agent until midnight Pacific.

The cost is asymmetric in a way that decides the design:

- trusting a **correct** long deadline saves a handful of doomed calls;
- trusting an **incorrect** long deadline costs every turn until it lapses.

And the incorrect case is undiscoverable by construction. The key is never tried, so no success is ever observed, so the journal's "predicted too long" detector can never fire on the case where it matters most. Probing is not just a safety net — it is what makes the measurement possible at all.

So: **when the model in flight has no usable credential left, one bench gets tested instead of obeyed.**

| Gate | Rule |
|---|---|
| Last resort only | Never while any other key is usable for that model. A probe is not a shortcut past a healthy key. |
| Ours only | The host's stored deadline must still match the ledger's fingerprint. Somebody else's bench is not KAME's guess to second-guess. |
| Worth asking | The sentence must be longer than 10 minutes, and more than 5 minutes must remain. Waiting out a 21-second throttle is cheaper than a failed call. |
| Answerable by retrying | Never for billing, credit, auth, invalid, revoked, permission or suspension. Out of credits is not a clock — Hermes keeps the full bench for the same reason. |
| Spaced | 5m, then 10m, 20m, 30m, and 30m thereafter. A correct long deadline costs a bounded handful of calls, not one per turn. |
| Consistent within a turn | Once issued, the key stays offered for 60 seconds. `_available_entries` is asked several times per request and the answers must agree, or which key gets used would depend on call order. |

The attempt is written down **before** the key is handed over — a probe that is not counted is one that repeats on the very next query. A refusal re-benches the key without resetting the count, so the schedule keeps widening; only a genuinely lapsed deadline starts a new episode at 5 minutes again.

The pool is still never written to. A probe adds one entry to the list `_available_entries` returns, past the same usability gates the host applies itself, and that is all.

`/kame-quota` shows it as `tested 3×` beside the bench, which answers the question a bench alone raises: whether the agent is sitting out a deadline or checking it.

## How far a bench reaches

Everything above sizes the bench in *time*. v0.0.8 adds the other dimension: **how much of the key a refusal covers.**

Per-model release is right where the quota is per-model — Google's free tier meters `PerProjectPerModel` and says so in the body. It is wrong where the quota is metered on the account or on the key itself: OpenRouter's free tier is a daily ceiling across every free model, so handing the key to a second model buys a second refusal, a slower turn, and a journal row about a limit that was never that model's.

Applying either provider's shape to the other is the same class of mistake as the host benching provider-wide. Only the direction of the error changes.

So KAME reads the scope the same way it reads the timing — from what the provider actually said:

| What the response says | Scope | What KAME does with the bench |
|---|---|---|
| `...PerProjectPerModel`, or a violation whose `quotaDimensions` names the model | per model | released for every other model, as before |
| `free-models-per-day`, `per user`, `per organization`, `per-api-key`, `insufficient_quota` | the whole key | held for every model until it lapses |
| nothing about scope | unknown | **treated as per model** — exactly what every version before v0.0.8 did |

The asymmetry is the safety argument. Silence changes nothing, so a scope this code fails to detect costs nothing that was not already being paid. Only an explicit account-wide statement changes behaviour, and it changes it towards holding the key — the expensive direction, taken only on evidence. Guessing there would re-create, in a new dimension, the exact regression the per-model dimension was added to fix.

Three consequences worth stating:

- **The escape hatch reaches this case too.** A key held everywhere is still tested on the same widening schedule, and the attempt is counted against the bench that is actually blocking — which, for an account-wide limit, is one filed under a different model. Counting it under the model in flight would leave the real bench untested forever.
- **Evidence does not evaporate.** A provider that named the scope once and stayed quiet on the retry has not changed its mind, so a later silence never overwrites what it said.
- **Rows written before v0.0.8 read as `unknown`**, not as account-wide. An upgrade cannot start holding keys back on evidence that was never recorded.

`/kame-quota` marks these benches `· all models`.

## When the key answers anyway

The escape hatch above asks a question. Until v0.0.9 the plugin threw the answer away.

A probe that *failed* was handled correctly — the bench was re-registered and the schedule widened. A probe that *succeeded* changed nothing at all: the ledger still said the key was spent, so the very next selection withheld it again and the next probe was five minutes later. The user got one call every widening interval instead of their agent back, for as long as the wrong deadline lasted. On a one-key pool with a daily cap read too long, that is the difference between a five-minute hiccup and a day of near-total lockout.

Every deadline in the ledger was *reasoned out of an error message*. None of them was measured. So when one is contradicted by an actual successful call, the contradiction wins:

| What is observed | What it disproves | What survives |
|---|---|---|
| a call on `(key, model)` comes back clean | that pair's bench, whole | nothing — the key is back in rotation until it fails again |
| a call on some *other* model of the same key comes back clean | the *reach* of an account-wide bench | the deadline itself, narrowed to the model that actually hit the limit |
| anything else | nothing | everything |

Three details make it safe rather than merely hopeful:

- **Only a probe settles a bench.** The success hook carries a provider and a model and no key. There is a mirror of the pool's last selection, but it is best-effort by construction — two agents on the same provider can overwrite it for each other — so it feeds statistics and never a release. A probe is different in kind: the escape hatch is reached only when the model has *nothing else usable*, and what it hands back is a single entry it named itself. There is no second candidate for the next success to have come from.
- **The bench is marked, not deleted.** The deadline stored in it is the fingerprint that proves the host's matching cooldown is KAME's to unwind. Delete the row and the key is stranded behind a bench nobody claims — a worse lockout than the one being fixed. `/kame-quota` shows these as `tested and it worked` instead of a countdown, because "free in 8h" about a key that is in rotation right now would be the most misleading line in the report.
- **Only a success shortens a bench.** A later, *shorter* refusal never does. A key held to midnight for a spent daily quota does not become free in sixty seconds because a probe came back with a per-minute complaint; the daily counter is still spent, and the smaller truth does not undo the larger one.

### `/kame-quota`

```
/kame-quota            what is benched now, and what KAME has learned
/kame-quota reset      clear the history, leave the benches alone
```

It is a separate module from `/kame-keys` on purpose: `commands.py` is the only file in the plugin that can write a credential, and a report has no business being in it. `status.py` reads two stores and renders text. It cannot throw inside a chat turn — an unreadable store renders as an empty slate, and any exception is reported as its type.

The report is explicit that it only watches:

```
KAME — per-model quota

Benched right now
  nothing is benched per-model right now

How the requests are spread
  gemini · gemini-3.6-flash
    GOOGLE_API_KEY[2]    4 requests   126 since Hermes started
    GOOGLE_API_KEY[1]    3 requests   131 since Hermes started
    GOOGLE_API_KEY[3]    idle         129 since Hermes started

What KAME was asked (since Hermes started)
  gemini               429  ×7 · 7 sized
  anthropic            401  ×2 · none sized, left to the host

What KAME has seen (last 14 days)
  nothing recorded yet — no real refusal has passed through KAME

The tally is observation. One reading acts, and only in the safe direction: a
deadline measured short twice in a row on the same key and model makes the next
bench longer, never shorter. Benches are real, and a key marked tested is being
tried again on a widening schedule.
```

The middle section is the only one in the report about the plugin working rather than about something having gone wrong, and it is why it exists: with it, a pool that is rotating and a pool that is hammering one key stop looking identical from the outside. The numbers are read from the counters selection itself ordered on — same window, same pruning, taken under the same lock — so the screen cannot become a second version of what the plugin did. Counted per key, so two pool rows seeded from one key are one line, which is what the provider meters. Two numbers per key, because the window the ordering decides on is sixty seconds long and that is too short a thing to ask somebody to catch their own pool inside of: the second number is what the key has taken since Hermes started, and a key reading `idle · 0 since Hermes started` beside one with hundreds is the picture the section exists to show. Only the first number decides anything, and there is a test that fails if the second one ever gets a vote. Neither survives a restart.

The section above it is the one that answers a question the rest of the report cannot: *was KAME even consulted, and could it answer?* Declining is the common path and the safe one, so most lines will read `none sized, left to the host` and that is the plugin working — the host has a competent classifier and overriding it with a guess is worse than silence. The line that gets pointed at is a rate limit with nothing sized, marked `← a wait KAME could not read`, because reading the wait out of a 429 is the entire job on that status and a column of those is what an inert plugin looks like from outside. It is counts only — provider, status number, two integers — and the reason it is safe to print is what it does not store: the payloads behind it may carry an unredacted provider dump.

That footer says three things on purpose, and none of them can be dropped. The tally is a wider count than the thing that acts — it groups by provider, model and window over fourteen days, while a bench is only widened by a consecutive run on one specific key. The benches and the probes are live behaviour. And the one reading that does act moves in exactly one direction. A report that flattened all of it into "nothing here changes anything" would be easier to read and false.

## When the deadline was measured short

Everything else in this plugin is *read* off the provider. This one number is not, and it is the only one.

A key comes back at its deadline, is handed to the very next call, and is refused again inside three minutes. Read once, that is noise: a burst, a neighbour on the same key, a provider rounding down. Read **twice in a row on the same key, the same model and the same kind of limit**, there is no other reading left. The deadline was short, and nothing in any response body is going to say so.

So KAME holds it longer next time: 2× on the second strike, 4× on the third, capped at 8× and never past a day.

### A moment is not a length

That doubling is right for a deadline that is a *stopwatch* — "come back in 21 seconds", "re-probe in an hour". It is wrong for one that is an **anchor**: midnight US/Pacific, the instant a Google daily counter is believed to roll.

And "wrong" here does not mean clumsy, it means inert. A key refused just after midnight is benched until the *next* midnight, so the deadline is already a day out, and the 24-hour ceiling swallows the entire multiplier. Until v0.1.2 the deadline that most needed correcting was the only one escalation could not touch — and its failure repeats **every single day**: five minutes of clock error costs a full day of that key, then does it again tomorrow.

An anchor that proves early is therefore *moved*, not multiplied: +30 minutes, then +1 hour, then +2, and never more. The day itself came from the provider, so only the offset is KAME's invention and only the offset is capped.

`quota.py` marks the deadline `source: anchor` rather than leaving it to be guessed from the window — a daily cap from a provider whose clock KAME does not know is an hourly re-probe wearing the same window name, and that one is a length, and scales.

### Two deadlines on one bench

The temptation is to write the longer number into the bench and be done. That breaks the plugin, quietly:

| Field | Whose number | What it is for |
|---|---|---|
| `reset_at` | the **host's** — exactly what Hermes stored | the fingerprint that proves this cooldown is KAME's to unwind |
| `extended_to` | **KAME's** — how long it is actually holding | the deadline that decides every release |

Overwrite `reset_at` and the fingerprint stops matching the host's stored cooldown. `_fingerprint_matches` returns `None`, the bench belongs to nobody, and the key is locked out of *every other model* for as long as the host holds it — the exact regression the per-model dimension exists to prevent. So they are two fields, and the rule is one line: every question about **withholding** reads `until = max(reset_at, extended_to)`; every question about **ownership** reads `reset_at`.

That separation also closed a hole that had been sitting in the never-shrink guard since v0.0.5, where a carried-forward longer deadline used to overwrite the fingerprint.

### What keeps it from becoming a key-hider

- **Only consecutive evidence counts.** One refusal landing at an ordinary time sets the streak back to zero. There is no decay constant and no window to tune, because "consecutive" is already a statement about recency.
- **The bench has to have been served.** If the key answered a call anywhere in the stretch it was supposed to be waiting out — a probe came back clean and released it early, or it was handed back on time and worked for a minute before hitting the limit again — then the refusal that follows is a fresh limit, not a measurement, and the run breaks. v0.1.0 read the sequence off the refusals alone and quietly assumed the gap was spent waiting; on a two-key pool it often is not.
- **The charge is narrow.** Per key, per model, per quota window. A free key proving short says nothing about a paid one; a per-minute throttle says nothing about a daily cap.
- **Never for a depletion.** An account-wide window, an exhausted balance, a revoked key — none of those are a mis-sized throttle, and stretching them would only hide the key. It reuses the same never-probe list.
- **Bounded twice**, at 8× and at 24 hours.
- **It is still a prediction.** A widened bench is tested by [the escape hatch](#the-escape-hatch) like any other, and [one clean call](#when-the-key-answers-anyway) retires it for good. That ordering is deliberate: the refutation shipped in v0.0.7 and v0.0.9, the escalation only after. The reverse order is how a plugin that sizes cooldowns turns into a plugin that hides keys.

`/kame-quota` marks these benches `· held longer, this deadline proved short before`.

## The sentence two providers share

Everything above sizes a throttle. This is about telling a throttle from a depletion in the first place — the step before all of it, and the one that was wrong from v0.0.3 through v0.1.2.

Google's current free-tier 429 says, word for word:

> "You exceeded your current quota, please check your plan and billing details."

with a link to the *rate-limit* docs appended. It says it for a **twenty-one second** per-minute throttle. It is also, word for word, OpenAI's out-of-credits message.

Read as billing, every consequence points the same way:

| | Read as billing | What the payload actually said |
|---|---|---|
| Bench | 24 hours | 21 seconds |
| Reach | every model on the key | this model only |
| Probe | disarmed — `billing` is in the never-probe list | available |
| Escalation | disarmed — `account` is in the never-stretch list | applies |

Four failures from one sentence, and the fourth is the one that hurts most: the escape hatch exists precisely for a deadline read too long, and the misreading switched it off. With a single key the agent would be down for a day with nothing in the system able to discover it — the key is never tried, so no success is ever observed. It also meant **v0.1.0, v0.1.1 and v0.1.2 were inert against real Google traffic**, including the anchor correction written specifically for Google's daily cap: no real payload could reach the code path.

The right answers were in the body the whole time and were thrown away. `detect_quota_window` reads `per_minute` off the quotaId, `detect_quota_scope` reads `per_model`, and `RetryInfo` carries `21s`. Classification returned at step 2 without asking any of them.

### The discriminator is not a provider name

Keying this on "is it Google" would be the [allowlist regression](#no-provider-allowlist) again, and it would be wrong for whichever provider adopts the sentence next. What separates the two meanings is in the payload:

- **A named wait.** Being told to come back in twenty-one seconds is not being told the balance is empty. A depletion has nothing to wait for.
- **A named counter.** `GenerateRequestsPerMinutePerProjectPerModel` names a *rate*. A balance has no window.

Either one settles it. Neither `account` nor `unknown` counts — the first *is* the depletion reading, and the second is an absence of evidence, which can never be what overturns a match.

The unambiguous markers are untouched and still decide on their own: `insufficient_quota`, `credit balance is too low`, `out of credits`, `payment required`, `billing … disabled`. A gateway that staples a stock `Retry-After` onto a genuine depletion still reads as billing, because the decisive marker outranks the question.

### Why it survived four versions

The fixtures were kinder than the provider. Every Google body in the suite stopped at *"You exceeded your current quota."* — a real wording, but not the one Google sends today — and one of the two classification tests passed no `error_message` at all. 631 tests were green against a sentence Google does not send.

That is the lesson worth keeping over the fix: a fixture written from memory tests the memory. `TestTheRealGooglePayloadEndToEnd` in `tests/test_binding.py` now runs the verbatim payload through the real classifier and into the pool, so the two halves cannot drift apart again without something going red.

### And the delay in the shape Google actually emits it

`google.rpc.RetryInfo.retryDelay` is a Duration *message*. The REST endpoint renders it `"21s"`; canonical proto JSON — what gRPC-JSON transcoding and the GenAI SDKs produce — renders it `{"seconds": 21}`. The body walker skipped every dict value, so that entire shape read as no delay at all and fell back to the window's default. It is read now; a dict carrying neither `seconds` nor `nanos` is still somebody's retry *policy* and still yields nothing.

## One name, two answers

The section above is about a sentence read wrong. This one is about a *key* read wrong, found by asking the same question of every other provider: is the fixture in the suite the payload the provider actually sends?

For OpenRouter it was not, and the difference is where the rate-limit headers live. OpenRouter puts them **in the body**:

```json
{"error": {"code": 429,
           "message": "Rate limit exceeded: free-models-per-day",
           "metadata": {"headers": {"X-RateLimit-Reset": "1786862019000"}}}}
```

That is documented, it is what litellm hands to the host, and it means the identical name `X-RateLimit-Reset` reaches this module through the headers on most providers and through the body on this one. The two paths had different patterns. `extract_from_headers` matched it by shape — a name mentioning both a limit and a reset. The body walker demanded a suffix after `reset` (`resetAt`, `reset_time`), so the same name read as an ordinary key and its value was discarded.

OpenRouter states the exact moment its free-tier counter rolls over, and KAME threw it away and fell back to the conservative hourly re-probe: nine wasted refusals per key on a cap that resets nine hours out, every day, on the one free tier a rotating pool exists to stretch.

The fix is not the missing spelling. The defect was two readings of one name drifting apart, so the body now shares the header pattern rather than restating it. It picks up `quotaResetDelay` in a body as a side effect — a key the host's own text scan already knew about and this one did not.

**And the bucket is account-wide in every window it names.** `free-models-per-day` was already read as account-wide; `free-models-per-min` is the same shared ceiling with a different window, and it was read as per-model — so the key was handed back for models it could not serve. The marker matches the bucket's name now, not one window's spelling of it. Per-model evidence is still checked first and still wins outright.

## The number the provider stated, and the one this module invented

A daily 429 very often carries both kinds of number at once:

```
headers: x-ratelimit-reset-requests: 58s          ← a different counter
message: ...on requests per day (RPD): Limit 200, Used 200.
         Please try again in 6h12m.               ← the daily wait
```

The cascade takes the header, because a structured field means what it says and a sentence has to be parsed out of prose. That ordering is right. Then the long-window rule sets the 58 seconds aside as the misleading kind — also right, and the reason [the daily case](../README.md#what-you-get) exists at all.

What replaced it was the flat hourly re-probe. That number is not the provider's; it is this module's own conservative guess for a clock it does not know. And the provider's six-hour figure was sitting one rung down the same cascade, unread.

So a long window whose strongest reading is short now asks the readings the cascade passed over, and takes the longest of those that clear the window's own default. Everything else is unchanged: a hint below the default is still discarded whichever source it came from, Google's calendar anchor is still decided before any of this, and a window shorter than a day still simply obeys its strongest reading.

Strength order is how one reading gets picked. It is not a reason to prefer an invented number to one the provider stated.

A related half-truth went with it: when the flat default *is* used, the decision now says its source was `window`, not the header whose number was just thrown away.

### What was considered and not done

The same evidence suggests a bolder rule: treat an **absolute moment** as authoritative on a long window even when it is short, since a stated moment is not a misleading duration. It is wrong, and the counter-example is common — a daily refusal usually carries per-minute reset headers, and plenty of providers render those as epochs rather than durations. Obeying one would hand the key back sixty seconds into a day-long cap and hammer it until midnight. The current rule refuses exactly that, and one hour of a key held too long in the last hour before a rollover is a far cheaper mistake.

## What Hermes already does well

Worth recording, because it narrows the scope honestly. The upstream credential pool is stronger than the open GitHub issues suggest — it already has `_extract_retry_delay_seconds` for several text formats, `failure_reason` to separate billing from transient, `STATUS_DEAD` for permanently revoked credentials, a short cooldown when a pool is down to its last key, and four selection strategies.

The gaps KAME fills are specific: retry timing in **headers, exception attributes, and structured bodies** (none of which the text scan reaches), compound durations, and the **quota-window distinction** that makes a daily cap behave differently from a per-minute throttle. That is the entire claim.
