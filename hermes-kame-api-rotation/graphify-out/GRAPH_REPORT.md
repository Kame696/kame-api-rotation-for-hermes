# Graph Report - .  (2026-08-28)

## Corpus Check
- 39 files · ~112,179 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1050 nodes · 2055 edges · 45 communities
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 47 edges (avg confidence: 0.57)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_quota|quota]]
- [[_COMMUNITY_commands|commands]]
- [[_COMMUNITY_journal|journal]]
- [[_COMMUNITY_state|state]]
- [[_COMMUNITY_settings|settings]]
- [[_COMMUNITY_field binding|field binding]]
- [[_COMMUNITY_carousel|carousel]]
- [[_COMMUNITY_plugin|plugin]]
- [[_COMMUNITY_menu|menu]]
- [[_COMMUNITY_ledger|ledger]]
- [[_COMMUNITY_probe|probe]]
- [[_COMMUNITY_init|init]]
- [[_COMMUNITY_stitch|stitch]]
- [[_COMMUNITY_store|store]]
- [[_COMMUNITY_dispersion|dispersion]]
- [[_COMMUNITY_report|report]]
- [[_COMMUNITY_resolver binding|resolver binding]]
- [[_COMMUNITY_dispatch binding|dispatch binding]]
- [[_COMMUNITY_gemini slots|gemini slots]]
- [[_COMMUNITY_status|status]]
- [[_COMMUNITY_storm|storm]]
- [[_COMMUNITY_aux binding|aux binding]]
- [[_COMMUNITY_dispatch binding|dispatch binding]]
- [[_COMMUNITY_dispatch binding|dispatch binding]]
- [[_COMMUNITY_dispatch binding|dispatch binding]]
- [[_COMMUNITY_pool binding|pool binding]]
- [[_COMMUNITY_reconcile|reconcile]]
- [[_COMMUNITY_dispatch binding|dispatch binding]]
- [[_COMMUNITY_runtime|runtime]]
- [[_COMMUNITY_dispatch binding|dispatch binding]]
- [[_COMMUNITY_pool binding|pool binding]]
- [[_COMMUNITY_pool binding|pool binding]]
- [[_COMMUNITY_runtime|runtime]]
- [[_COMMUNITY_tally|tally]]
- [[_COMMUNITY_init|init]]
- [[_COMMUNITY_ledger|ledger]]
- [[_COMMUNITY_tally|tally]]
- [[_COMMUNITY_pool binding|pool binding]]
- [[_COMMUNITY_pool binding|pool binding]]
- [[_COMMUNITY_pool binding|pool binding]]
- [[_COMMUNITY_runtime|runtime]]
- [[_COMMUNITY_carousel|carousel]]
- [[_COMMUNITY_answer|answer]]
- [[_COMMUNITY_runtime|runtime]]
- [[_COMMUNITY_dispatch binding|dispatch binding]]

## God Nodes (most connected - your core abstractions)
1. `Ledger` - 35 edges
2. `PoolBinding` - 32 edges
3. `Bench` - 28 edges
4. `Journal` - 25 edges
5. `normalize_model()` - 23 edges
6. `h()` - 21 edges
7. `format_duration()` - 18 edges
8. `Carousel` - 17 edges
9. `KamePage()` - 16 edges
10. `MenuCommand` - 16 edges

## Surprising Connections (you probably didn't know these)
- `_on_api_error_classification()` --calls--> `classify()`  [EXTRACTED]
  __init__.py → core/classify.py
- `_install_pool_binding()` --calls--> `PoolBinding`  [EXTRACTED]
  __init__.py → pool_binding.py
- `_install_pool_binding()` --calls--> `JournalStore`  [EXTRACTED]
  __init__.py → store.py
- `_install_pool_binding()` --calls--> `LedgerStore`  [EXTRACTED]
  __init__.py → store.py
- `register()` --calls--> `install()`  [EXTRACTED]
  __init__.py → aux_binding.py

## Import Cycles
- 1-file cycle: `__init__.py -> __init__.py`
- 1-file cycle: `core/__init__.py -> core/__init__.py`

## Communities (45 total, 0 thin omitted)

### Community 0 - "quota"
Cohesion: 0.05
Nodes (70): _body_text(), classify(), looks_like_upstream_wrapper(), _matches(), _names_a_wait(), Any, Turning one API failure into a verdict — or into silence.  There is no list of s, Collect the machine-readable type/code strings from a failure.      Only strings (+62 more)

### Community 1 - "commands"
Cohesion: 0.05
Nodes (66): _apply_plan(), _auth_store_path(), _backup_auth_store(), _cmd_add(), _cmd_import(), _cmd_reset(), _cmd_status(), _entry_summaries() (+58 more)

### Community 2 - "journal"
Cohesion: 0.08
Nodes (26): Block, _clean(), Journal, _landed_right_after(), Any, What actually happened, so the next guess can be better than this one.  The le, Coerce to one of the three, defaulting to the claim that says least.      A ro, One credential, refused on one model, at one moment.      ``reset_at`` is the (+18 more)

### Community 3 - "state"
Cohesion: 0.07
Nodes (46): _digest(), install(), Path, Put the Desktop half where Desktop will actually load it.  The plugin ships two, Take the Desktop half back out of ``desktop-plugins/``. Never raises.      The P, Copy the UI file into the standalone door if it is not already there.      Retur, _set(), source() (+38 more)

### Community 4 - "settings"
Cohesion: 0.06
Nodes (46): _as_flag(), _as_number(), bounds(), canonical(), _config_now(), describe(), describe_all(), effective() (+38 more)

### Community 5 - "field binding"
Cohesion: 0.07
Nodes (37): _apply(), control_path(), forget(), _forget_one(), last_result(), _legacy_names(), poll(), Any (+29 more)

### Community 6 - "carousel"
Cohesion: 0.08
Nodes (31): Carousel, classify(), extract_delay(), _fresh(), _header_delay(), _headers_of(), is_auth_failure(), is_terminal() (+23 more)

### Community 7 - "plugin"
Cohesion: 0.14
Nodes (41): ageSeconds(), Card(), ChipPool(), chipPools(), countdown(), dataDir(), duration(), EVENT_LABELS (+33 more)

### Community 8 - "menu"
Cohesion: 0.08
Nodes (23): Events, Any, The last fifty things that went wrong, in the order they happened.  Every fact i, Newest first, because that is the end anybody reads., How many events have ever been recorded, including dropped ones., A fixed-size, thread-safe record of what the carousel decided., MenuCommand, Any (+15 more)

### Community 9 - "ledger"
Cohesion: 0.09
Nodes (18): Ledger, normalize_model(), Any, Whether this bench should still keep the key out of rotation.          A refut, Rebuild one bench, or return None if the row is unusable.          Persisted s, A bounded set of live benches, keyed by credential and model.      One bench p, When this credential is next usable *for this model*, if benched.          A r, Every model this credential is currently spent on. (+10 more)

### Community 10 - "probe"
Cohesion: 0.10
Nodes (21): Bench, One credential, spent on one model, until one moment.      ``reset_at`` is abs, When KAME stops withholding this key. The deadline that decides., KAME is holding this key past what the host was told., This claim was tested and the key worked anyway., choose(), eligible(), interval_for() (+13 more)

### Community 11 - "init"
Cohesion: 0.12
Nodes (27): register_command(), _count_empty(), _install_dispatch_binding(), _install_field_binding(), _install_pool_binding(), _install_resolver_binding(), _is_disabled(), _on_post_api_request() (+19 more)

### Community 12 - "stitch"
Cohesion: 0.11
Nodes (22): _comparable(), continuation(), _looks_like_a_restart(), prefill_message(), Joining a cut answer to its continuation without printing anything twice.  When, Whether ``fresh`` is the answer beginning again rather than continuing.      Onl, Trims one continuation as it streams, so the seam never reaches the user.      F, Take one delta; return the part of it that should be displayed. (+14 more)

### Community 13 - "store"
Cohesion: 0.12
Nodes (10): _Document, JournalStore, LedgerStore, Any, The current document, re-reading storage when the copy is stale.          Alwa, Persist the document, pruning first. Returns whether the write landed., Forget everything — the operator's reset button., The live per-model benches — read on every credential selection. (+2 more)

### Community 14 - "dispersion"
Cohesion: 0.10
Nodes (15): bucket_for(), Dispersion, mark_id(), Which key to use *next*, when several are healthy.  Everything else in this pl, Request marks per credential, per bucket, over a sliding window.      Every me, Count one request against a credential.          Called when a key is *handed, ``(requests in the window, last used)`` for one credential.          ``last us, The given ids, rested first and least-loaded within that.          ``just_rele (+7 more)

### Community 15 - "report"
Cohesion: 0.12
Nodes (20): What the journal knows about one provider, model and quota window., The evidence says this window is being under-predicted.          Two independe, Keys here come back sooner than KAME holds them., KAME sized these benches and the pool kept none of its numbers.          Not a, WindowStat, humanize(), _name(), Turn the ledger and the journal into something a person can read.  Two audienc (+12 more)

### Community 16 - "resolver binding"
Cohesion: 0.13
Nodes (16): choose(), Incompatible, inspect_module(), _key_of(), Any, The first key of a turn, taken from the pool instead of from the raw variable., The key an entry would actually send, by whichever field carries it., Which key this turn should carry, and whether the pool chose it.      Pure, so t (+8 more)

### Community 17 - "dispatch binding"
Cohesion: 0.13
Nodes (14): _interrupted(), _looks_local(), _publish(), Any, Whether this agent points at a model running on the same machine.      Only as, Hermes' own stream read timeout, lowered for one attempt and put back.      Th, Sleep until the soonest key is usable. ``False`` means stop waiting., Live status through ``thinking.delta`` — safe for ordinals.      The Hermes sp (+6 more)

### Community 18 - "gemini slots"
Cohesion: 0.16
Nodes (21): apply(), _complete_json_object(), Any, Two parallel Gemini tool calls, merged into one broken argument string.  The sym, True for text that is, on its own, a whole JSON object.      Anything else — a f, Every tool-call delta in one chunk, or an empty list.      Written to survive a, Per-stream bookkeeping, keyed by the identity of the host's own dict.      ``too, Move a second complete object off the index the first one owns.      Returns how (+13 more)

### Community 19 - "status"
Cohesion: 0.14
Nodes (10): Any, QuotaCommand, Holds the two stores the report reads.      Constructed with the live binding, What selection has handed out in the last minute, if it is running.          M, Labels for the spread section, which counts by key, not by row.          Two s, The classification counts, which do not need the binding at all.          This, The count of calls that answered with nothing, same rules as above.          R, What the per-call rotation has actually been doing, or why it is not. (+2 more)

### Community 20 - "storm"
Cohesion: 0.12
Nodes (12): Collapsing a storm of identical failures into something a person can read.  Ev, Decides whether a failure line is news or repetition.      One of these lives, Whether anything is currently being held back.          A storm that has not y, Take one failure and answer what should be written about it., Close the current storm, if one was actually collapsing anything.          Cal, What the caller should write, if anything.      Both fields can be set at once, One run of failures that share a shape, and what has been said about it., Storm (+4 more)

### Community 21 - "aux binding"
Cohesion: 0.18
Nodes (12): AuxBinding, _check(), Incompatible, inspect_module(), install(), Any, Give auxiliary calls the same per-model memory as the main conversation.  Hermes, Work out which provider pool and model this request belongs to.          The pro (+4 more)

### Community 22 - "dispatch binding"
Cohesion: 0.11
Nodes (17): _clear_host_stale_streak(), _partial_text(), _prefill_refused(), Whether this provider needs the continuation to end in a user turn.      Answe, The same request, asking the model to continue the answer it lost.      Always, Tell Hermes this is a different key, the way a provider swap would.      ``age, One API call, as many keys as it takes., Whether the host's answer carried no content at all.      A squeezed free-tier (+9 more)

### Community 23 - "dispatch binding"
Cohesion: 0.13
Nodes (13): _contribution(), _Delivery, _install_delivery(), _install_shims(), _Progress, ``(delivery, what_to_restore)`` for one attempt.      A host with no ``_fire_s, What this attempt added to the answer, reconciled against what was shown., Flipped the instant anything reaches the user, and tracks activity timestamp. (+5 more)

### Community 24 - "dispatch binding"
Cohesion: 0.15
Nodes (16): _apply_key(), _attribute(), candidates(), _entries_of(), _key_of(), model_label(), passes_desktop_status_gate(), Every API call picks its own key, and a failed call rotates instead of failing. (+8 more)

### Community 25 - "pool binding"
Cohesion: 0.16
Nodes (8): PoolBinding, Installs, owns, and can fully remove the wrappers., Wrap the two methods. Returns whether the plugin gained per-model memory., Wrap ``_persist`` so a derived key can never be written, and only         then, Split at construction, so ``entries()`` is right before any request., Put the original methods back. Safe to call when not installed., Record that a call came back clean, and act on it if it settles a bet., Mark what this success disproved, if it answered a probe.          Returns qui

### Community 26 - "reconcile"
Cohesion: 0.19
Nodes (12): Action, EntryView, _fingerprint_matches(), plan(), Any, Turn per-model memory into a list of edits for a provider-scoped pool.  The host, Return the model whose bench explains the host's deadline, if ours.      Matchin, Decide what the pool should look like for ``model`` right now.      Returns an e (+4 more)

### Community 27 - "dispatch binding"
Cohesion: 0.19
Nodes (8): format_duration(), ``6m 11s`` — for a human reading a status line, not for a machine., ``01:23 (around 14:32:07)`` — the wait, and when it ends by the wall clock., Tells the user a long wait is a wait, not a freeze.      Agent Zero's carousel, Emit a notice if this wait has now been going on long enough., Close the loop, but only if the user was told it was open., recovery_clock(), _Vigil

### Community 28 - "runtime"
Cohesion: 0.15
Nodes (5): forget_call(), note_empty_answer(), Which call is in flight — the fact the credential pool is never told.  ``Crede, Count a call that returned with no content and no tool call., Clear the announcement — used by tests and by explicit teardown.

### Community 29 - "dispatch binding"
Cohesion: 0.18
Nodes (7): DispatchBinding, install(), Installs, owns, and can fully remove the per-call carousel., Wrap both dispatch functions. Never raises; a refusal is an outcome., How many times this call may continue a cut answer. Zero disables it., Whether the pool has proved the request is at fault, not the keys.          Ag, Convenience entry point used by ``register()``; never raises.      Imports the

### Community 30 - "pool binding"
Cohesion: 0.23
Nodes (9): _check_signature(), Incompatible, inspect_module(), Teach the credential pool the one thing it has no field for: the model.  Herme, The installed Hermes does not present the surfaces these wrappers need., Raise ``Incompatible`` unless every surface the wrappers use is present., Whether the load spreading half of the plugin is switched off.      Separate f, Wrap selection if it is recognisable, and shrug if it is not.          Deliber (+1 more)

### Community 31 - "pool binding"
Cohesion: 0.31
Nodes (5): Any, Re-answer "which keys are usable" for the model actually in flight., Offer one benched key back, to test a deadline KAME chose itself.          Rea, The KAME-written bench that is keeping this key from this model.          Usua, Mirror the host's own gates before handing a benched key back.          The ho

### Community 32 - "runtime"
Cohesion: 0.18
Nodes (11): describe(), Judgement, model_for(), note_judgement(), providers_match(), The in-flight model, but only if it belongs to this pool's provider.      Retu, One short line for logs — never includes credential material., What KAME concluded about one failure, waiting to be filed. (+3 more)

### Community 33 - "tally"
Cohesion: 0.20
Nodes (7): Every row, busiest first, as plain values.          A copy: the caller renders, One provider and status: how many arrived, how many KAME sized., A status whose whole point is a wait, and no wait was ever read., Seen, NamedTuple, classifications(), empty_answers()

### Community 34 - "init"
Cohesion: 0.24
Nodes (10): _count(), _headers_from(), _on_api_error_classification(), Any, Translate a core verdict into the dict shape the hook expects.      ``reset_at, Dig the response headers out of whatever exception shape arrived.      SDKs di, Count one classification, and never fail because of it.      A counter is wort, Classify one failure, or decline so the host pipeline runs.      Accepts and i (+2 more)

### Community 35 - "ledger"
Cohesion: 0.22
Nodes (5): Per-model quota memory — the fact the host's pool has no field for.  Hermes be, What one successful call disproved. Empty is the ordinary answer.      Success, Refutation, The ``/kame-quota`` slash command — what is benched, and what was learned.  Se, Durable home for KAME's two documents, on top of ``ctx.state``.  ``ctx.state``

### Community 36 - "tally"
Cohesion: 0.29
Nodes (4): How many failures reached KAME, and how many of them it could size.  Declining, Counts of classification outcomes, safe to call from any thread.      The clas, Count one failure the hook was asked about.          ``sized`` is whether KAME, Tally

### Community 37 - "pool binding"
Cohesion: 0.29
Nodes (4): _label(), A stable, non-secret name for one credential, for logs only., How many times in a row this exact deadline has been measured short., Write the refusal down, with whatever KAME was thinking at the time.

### Community 38 - "pool binding"
Cohesion: 0.29
Nodes (5): _mark_id(), Keep a readable name for a counted credential, for the report only.          B, The mark ids of keys whose bench lapsed moments ago.          Read off the ent, What the load counter calls this credential.      Reads the key the host would, Order the usable keys least-loaded first, or leave them alone.          The ho

### Community 39 - "pool binding"
Cohesion: 0.25
Nodes (4): Make a credential that holds several keys present as several keys.          Id, The keys inside one entry, or nothing if it is not that kind of entry., One part, as a credential in its own right.          Everything the parent kno, Whether this entry has been replaced by its own parts.          The parent of

### Community 40 - "runtime"
Cohesion: 0.25
Nodes (8): _norm(), note_call(), note_selection(), The last credential this provider's pool handed out, or ``""``., Record the provider and model of the request about to be sent., Announce a call for exactly as long as it is being made.      ``note_call`` is, scoped_call(), selected_for()

### Community 41 - "carousel"
Cohesion: 0.29
Nodes (5): BaseException, fingerprint(), A stable, non-reversible label for one key, safe to log.      Never the key. N, Record one event. Never raises - a readout must not end a turn., ``(verdict, kind, status)`` — and the key's rest recorded either way.

### Community 42 - "answer"
Cohesion: 0.40
Nodes (5): carried_nothing(), _count(), Did the call that came back actually carry an answer?  Everywhere else in this, ``value`` as a non-negative count, or ``None`` when it says nothing.      ``No, True only when the host reported an answer with no content and no calls.

### Community 43 - "runtime"
Cohesion: 0.40
Nodes (5): note_probe_issued(), Probe, A benched credential handed back on purpose, to test its deadline., Claim the outstanding probe for this provider, once., take_probe()

### Community 44 - "dispatch binding"
Cohesion: 0.67
Nodes (3): Incompatible, The installed Hermes does not present the surface this module needs., Exception

## Knowledge Gaps
- **6 isolated node(s):** `$snapshot`, `$problem`, `$now`, `$pending`, `$notice` (+1 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Ledger` connect `ledger` to `commands`, `ledger`, `probe`, `store`, `report`, `status`, `reconcile`?**
  _High betweenness centrality (0.093) - this node is a cross-community bridge._
- **Why does `PoolBinding` connect `pool binding` to `pool binding`, `pool binding`, `pool binding`, `init`, `pool binding`, `pool binding`?**
  _High betweenness centrality (0.058) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `Ledger` (e.g. with `Action` and `EntryView`) actually correct?**
  _`Ledger` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Hermes KAME API Rotation — a key chosen for every call, and a call that rotates`, `Translate a core verdict into the dict shape the hook expects.      ``reset_at`, `Dig the response headers out of whatever exception shape arrived.      SDKs di` to the rest of the system?**
  _398 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `quota` be split into smaller, more focused modules?**
  _Cohesion score 0.052289815447710185 - nodes in this community are weakly interconnected._
- **Should `commands` be split into smaller, more focused modules?**
  _Cohesion score 0.05289193302891933 - nodes in this community are weakly interconnected._
- **Should `journal` be split into smaller, more focused modules?**
  _Cohesion score 0.08418367346938775 - nodes in this community are weakly interconnected._