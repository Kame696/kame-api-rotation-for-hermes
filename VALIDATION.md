# 1.8.1.9 validation and limitations

Build fingerprint `eaa130adf7eb` (both packages at 1.8.1.9). Everything below
ran offline: local fake providers, throwaway `HERMES_HOME`s, no real key and no
real request. The Hermes used is 0.21.4 (`59004a6`), the same source the author
runs; the Hermes test suites are that install's own.

## Public regression

The public checkout, run the way CI runs it (a clean Python 3.11 with only pytest installed, no Hermes, no third-party package): **2,722 passed, 368 skipped, 4 expected failures, 0 failed** (434 s). The skips are the 361 retired 1.8.1.8 tests listed below, plus tests that need Hermes or a POSIX platform. The four expected failures are the documented classifier tradeoffs and the unverified Codex OAuth refresh-error mapping carried from 1.8.1.8. Run `python -m pytest tests -q` after installing pytest.

The development workspace ran **2,772 tests, all passing**, with Hermes 0.21.4
on the path, and 2,746 without it. The 1.8.1.8 suite runs against 1.8.1.9
unchanged: `tools/legacy_dispatch.py` answers the old dispatch import with the
new transport. 361 1.8.1.8 tests are retired, each listed in
`tests/legacy_1818_retired_ids.txt` with its reason and the test that now holds
the same promise in `tests/legacy_1818_retired.py`. Their subjects are gone: the
wrapped host functions, the pool binding, the in-chat spinner, removed settings,
or scans of a source file that moved (re-run on `transport.py` in
`tests/test_source_invariants_1819.py`). New: `test_transport_1819.py` (wire-level
stream behaviour), `test_facade_1819.py` (real HTTP through Hermes' own client
classes), `test_source_invariants_1819.py`.

Hermes runs on Python 3.11. Every module of both packages compiles under
Hermes' own 3.11 interpreter, and the three 1.8.1.9 test files (54 tests) pass
on it against the real Hermes source.

Running the 1.8.1.8 suite against the new code found five real defects, all
fixed before release: a continuation's held words lost on a drop; the stop rule
not applied to drops that arrive as errors; a tool-only answer treated as empty
and asked again; an empty answer journalled as proof a key was back; custom
endpoints reading the wrong pool (`custom` instead of `custom:<name>`).

## Catalog validator (`hermes plugins validate`)

| package | ok | warnings | no core override | desktop surface |
|---|---|---|---|---|
| hermes-kame-api-rotation | yes | none | pass | pass |
| hermes-kame-provider | yes | none | pass | n/a |

## Real Hermes turns against fake providers

A driver builds the agent exactly like `hermes -z` and runs six scenarios with
no plugin, 1.8.1.8 and 1.8.1.9, on two wires: Google's native Gemini endpoint
and an OpenAI-compatible endpoint. Fifteen keys unless the scenario says
otherwise.

- S0 all healthy · S1 per-minute 429 on 5 of 15 keys · S2 per-day 429 on one
  model only · S3 503 overload · S4 every key limited for 8 s · S5 stream cut
  mid-answer.

Summary:

| scenario | no plugin | 1.8.1.8 | 1.8.1.9 |
|---|---|---|---|
| S0 | 3/3 | 3/3 | 3/3 |
| S1 | 2/4 (Gemini), 3/4 (OpenAI) | 4/4 | 4/4, same requests per key |
| S2 | 3/6 | 6/6 | 6/6, same requests per key |
| S3 | 3/3 (slower) | 3/3 | 3/3, same requests |
| S4 | 3/3 | 3/3 | 3/3, same 33 requests (*) |
| S5 | 0/3 | 3/3 | 3/3 |

Steady turns on the OpenAI wire: 0.2 s with 1.8.1.9, 0.6 s with 1.8.1.8. S5 on
the OpenAI wire: 1.8.1.8 repeated the words before the cut three times in the
final answer; 1.8.1.9 shows them once.

(*) S4's first-turn time is bimodal on the scenario's hard 8 s edge (1.8.1.8:
9.7 / 15.0 / 9.8 s; 1.8.1.9: 13.5 / 15.8 / 14.3 s over three runs each); the
decision sequence is the same. On the Gemini wire both builds made the same 33
requests; on the OpenAI wire 1.8.1.8 landed in the early mode with 29 because
each of its requests carried ~0.4 s of extra overhead.

### Native Gemini wire

| Scenario | Plugin | Loaded | Turns ok | Outcomes | Wall s/turn | Host retries used/turn | Requests | Requests by model:key:status | Hermes pool after |
|---|---|---|---|---|---|---|---|---|---|
| S0 | none | - | 3/3 | ok ok ok | 0.3, 0.1, 0.1 | 0, 0, 0 | 3 | flash: 1:200x3 | (clean) |
| S0 | 1818 | True | 3/3 | ok ok ok | 0.4, 0.2, 0.2 | 0, 0, 0 | 3 | flash: 1:200x1 2:200x1 3:200x1 | (clean) |
| S0 | 1819 | True | 3/3 | ok ok ok | 0.4, 0.2, 0.2 | 0, 0, 0 | 3 | flash: 1:200x1 2:200x1 3:200x1 | (clean) |
| S1 | none | - | 2/4 | FAIL(429) FAIL(429) ok ok | 7.7, 8.2, 3.1, 0.2 | 3, 3, 1, 0 | 14 | flash: 1:429x2 2:429x2 3:429x3 4:429x2 5:429x3 6:200x2 | k1:exhausted<br>k2:exhausted<br>k3:exhausted<br>k4:exhausted<br>k5:exhausted |
| S1 | 1818 | True | 4/4 | ok ok ok ok | 0.9, 0.2, 0.2, 0.2 | 0, 0, 0, 0 | 9 | flash: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:200x1 7:200x1 8:200x1 9:200x1 | (clean) |
| S1 | 1819 | True | 4/4 | ok ok ok ok | 0.6, 0.2, 0.3, 0.2 | 0, 0, 0, 0 | 9 | flash: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:200x1 7:200x1 8:200x1 9:200x1 | (clean) |
| S2 | none | - | 3/6 | FAIL(429) FAIL(429) FAIL(429) ok ok ok | 8.8, 8.6, 9.0, 0.2, 0.1, 0.1 | 3, 3, 3, 0, 0, 0 | 18 | flash: 1:429x2 2:429x2 3:429x3 4:429x2 5:429x3 6:429x2 7:429x1; pro: 7:200x3 | k1:exhausted<br>k2:exhausted<br>k3:exhausted<br>k4:exhausted<br>k5:exhausted<br>k6:exhausted |
| S2 | 1818 | True | 6/6 | ok ok ok ok ok ok | 1.5, 0.2, 0.2, 0.2, 0.2, 0.2 | 0, 0, 0, 0, 0, 0 | 16 | flash: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:429x1 7:429x1 8:429x1 9:429x1 10:429x1 11:200x1 12:200x1 13:200x1; pro: 1:200x1 2:200x1 3:200x1 | (clean) |
| S2 | 1819 | True | 6/6 | ok ok ok ok ok ok | 1.1, 0.2, 0.2, 0.3, 0.2, 0.2 | 0, 0, 0, 0, 0, 0 | 16 | flash: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:429x1 7:429x1 8:429x1 9:429x1 10:429x1 11:200x1 12:200x1 13:200x1; pro: 1:200x1 2:200x1 3:200x1 | (clean) |
| S3 | none | - | 3/3 | ok ok ok | 3.2, 0.2, 0.2 | 1, 0, 0 | 5 | flash: 1:200x3 1:503x2 | (clean) |
| S3 | 1818 | True | 3/3 | ok ok ok | 0.3, 0.2, 0.2 | 0, 0, 0 | 4 | flash: 1:503x1 2:200x1 3:200x1 4:200x1 | (clean) |
| S3 | 1819 | True | 3/3 | ok ok ok | 0.6, 0.3, 0.2 | 0, 0, 0 | 4 | flash: 1:503x1 2:200x1 3:200x1 4:200x1 | (clean) |
| S4 | none | - | 3/3 | ok ok ok | 8.3, 0.1, 0.1 | 2, 0, 0 | 6 | flash: 1:429x2 2:200x3 2:429x1 | k1:exhausted |
| S4 | 1818 | True | 3/3 | ok ok ok | 14.0, 0.2, 0.2 | 0, 0, 0 | 33 | flash: 1:200x1 1:429x2 2:200x1 2:429x2 3:200x1 3:429x2 4:429x2 5:429x2 6:429x2 7:429x2 8:429x2 9:429x2 10:429x2 11:429x2 12:429x2 13:429x2 14:429x2 15:429x2 | (clean) |
| S4 | 1819 | True | 3/3 | ok ok ok | 13.8, 0.2, 0.2 | 0, 0, 0 | 33 | flash: 1:200x1 1:429x2 2:200x1 2:429x2 3:200x1 3:429x2 4:429x2 5:429x2 6:429x2 7:429x2 8:429x2 9:429x2 10:429x2 11:429x2 12:429x2 13:429x2 14:429x2 15:429x2 | (clean) |
| S5 | none | - | 0/3 | FAIL(Gemini streaming request failed: connection reset (WinError 10054)) FAIL(Gemini streaming request failed: connection reset (WinError 10054)) FAIL(Gemini streaming request failed: connection reset (WinError 10054)) | 7.4, 7.6, 8.2 | 3, 3, 3 | 9 | flash: 1:200x9 | (clean) |
| S5 | 1818 | True | 3/3 | ok ok ok | 0.5, 0.2, 0.2 | 0, 0, 0 | 5 | flash: 1:200x1 2:400x1 3:200x1 4:200x1 5:200x1 | (clean) |
| S5 | 1819 | True | 3/3 | ok ok ok | 0.5, 0.2, 0.2 | 0, 0, 0 | 5 | flash: 1:200x1 2:400x1 3:200x1 4:200x1 5:200x1 | (clean) |

### OpenAI-compatible wire

| Scenario | Plugin | Loaded | Turns ok | Outcomes | Wall s/turn | Host retries used/turn | Requests | Requests by model:key:status | Hermes pool after |
|---|---|---|---|---|---|---|---|---|---|
| S0 | none | - | 3/3 | ok ok ok | 6.6, 0.2, 0.2 | 0, 0, 0 | 3 | fake-model-a: 1:200x3 | (clean) |
| S0 | 1818 | True | 3/3 | ok ok ok | 1.9, 1.0, 0.7 | 0, 0, 0 | 3 | fake-model-a: 1:200x1 2:200x1 3:200x1 | (clean) |
| S0 | 1819 | True | 3/3 | ok ok ok | 2.1, 0.2, 0.2 | 0, 0, 0 | 3 | fake-model-a: 1:200x1 2:200x1 3:200x1 | (clean) |
| S1 | none | - | 3/4 | FAIL(429) ok ok ok | 41.6, 20.7, 0.2, 0.2 | 3, 1, 0, 0 | 9 | fake-model-a: 1:200x3 1:429x4 2:429x2 | k2:exhausted reset=-1s |
| S1 | 1818 | True | 4/4 | ok ok ok ok | 2.6, 0.7, 0.7, 0.6 | 0, 0, 0, 0 | 9 | fake-model-a: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:200x1 7:200x1 8:200x1 9:200x1 | (clean) |
| S1 | 1819 | True | 4/4 | ok ok ok ok | 2.0, 0.2, 0.2, 0.2 | 0, 0, 0, 0 | 9 | fake-model-a: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:200x1 7:200x1 8:200x1 9:200x1 | (clean) |
| S2 | none | - | 3/6 | FAIL(429) FAIL(429) FAIL(429) ok ok ok | 10.3, 6.9, 7.5, 0.2, 0.1, 0.2 | 3, 3, 3, 0, 0, 0 | 18 | fake-model-a: 1:429x2 2:429x2 3:429x3 4:429x2 5:429x3 6:429x2 7:429x1; fake-model-b: 7:200x3 | k1:exhausted<br>k2:exhausted<br>k3:exhausted<br>k4:exhausted<br>k5:exhausted<br>k6:exhausted |
| S2 | 1818 | True | 6/6 | ok ok ok ok ok ok | 2.5, 0.6, 0.6, 0.7, 0.6, 0.2 | 0, 0, 0, 0, 0, 0 | 16 | fake-model-a: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:429x1 7:429x1 8:429x1 9:429x1 10:429x1 11:200x1 12:200x1 13:200x1; fake-model-b: 1:200x1 2:200x1 3:200x1 | (clean) |
| S2 | 1819 | True | 6/6 | ok ok ok ok ok ok | 2.2, 0.2, 0.2, 0.2, 0.2, 0.2 | 0, 0, 0, 0, 0, 0 | 16 | fake-model-a: 1:429x1 2:429x1 3:429x1 4:429x1 5:429x1 6:429x1 7:429x1 8:429x1 9:429x1 10:429x1 11:200x1 12:200x1 13:200x1; fake-model-b: 1:200x1 2:200x1 3:200x1 | (clean) |
| S3 | none | - | 3/3 | ok ok ok | 4.1, 0.1, 0.1 | 1, 0, 0 | 5 | fake-model-a: 1:200x3 1:503x2 | (clean) |
| S3 | 1818 | True | 3/3 | ok ok ok | 1.6, 0.6, 0.6 | 0, 0, 0 | 4 | fake-model-a: 1:503x1 2:200x1 3:200x1 4:200x1 | (clean) |
| S3 | 1819 | True | 3/3 | ok ok ok | 1.6, 0.2, 0.2 | 0, 0, 0 | 4 | fake-model-a: 1:503x1 2:200x1 3:200x1 4:200x1 | (clean) |
| S4 | none | - | 3/3 | ok ok ok | 12.0, 0.6, 0.2 | 2, 0, 0 | 6 | fake-model-a: 1:200x2 1:429x2 2:200x1 2:429x1 | (clean) |
| S4 | 1818 | True | 3/3 | ok ok ok | 9.7, 0.6, 0.6 | 0, 0, 0 | 29 | fake-model-a: 1:429x2 2:429x2 3:429x2 4:429x2 5:429x2 6:429x2 7:429x2 8:429x2 9:429x2 10:429x2 11:429x2 12:200x1 12:429x1 13:200x1 13:429x1 14:200x1 14:429x1 15:429x1 | (clean) |
| S4 | 1819 | True | 3/3 | ok ok ok | 13.5, 0.2, 0.2 | 0, 0, 0 | 33 | fake-model-a: 1:200x1 1:429x2 2:200x1 2:429x2 3:200x1 3:429x2 4:429x2 5:429x2 6:429x2 7:429x2 8:429x2 9:429x2 10:429x2 11:429x2 12:429x2 13:429x2 14:429x2 15:429x2 | (clean) |
| S5 | none | - | 0/1 | FAIL(turn exceeded harness bound 180.0s) | 180.0 | 0 | 36 | fake-model-a: 1:200x36 | (clean) |
| S5 | 1818 | True | 3/3 | ok ok ok | 4.9, 0.6, 0.7 | 0, 0, 0 | 6 | fake-model-a: 1:200x3 2:200x1 3:200x1 4:200x1 | (clean) |
| S5 | 1819 | True | 3/3 | ok ok ok | 1.7, 0.2, 0.2 | 0, 0, 0 | 4 | fake-model-a: 1:200x1 2:200x1 3:200x1 4:200x1 | (clean) |

## Hermes' own suites with KAME live (`tools/host_suite_1819.py`)

25 Hermes suites (credential pool, provider-client seam, auxiliary client,
streaming, Gemini adapter, error classifier, provider profiles), run clean and
again with KAME's client handed out for every bundled chat-completions profile
in every test home. 11 KAME clients were actually built during the run. **No
unexpected divergence.** Three expected ones are tests that replace the client
class with a mock and assert on the mock (a subclass never calls it); what they
check is pinned on KAME's client in `tests/test_facade_1819.py`. One real gap
was found this way and fixed: auxiliary NVIDIA calls lacked Hermes' billing
header.

## Host contract gates

| gate | result |
|---|---|
| `host_corpus.py` (Hermes' 200-case classifier corpus) | pass — five documented intentional verdicts, unchanged |
| `host_prose.py` | pass |
| `host_assumptions.py` | pass; 14 probes of 1.8.1.8's wrapped surface retired with reasons; 4 new provider-route facts |
| `host_gemini_contracts.py` | 4/4 contracts, 4/4 negative controls detected |
| `clock_gate.py` | pass, 6/6 |
| `continuity_gate.py` | pass, 7/7 |

`host_pool_suite.py`, `sandbox_binding.py`, `host_gemini_error_transport.py`
and the `live_*.py` probes measure 1.8.1.8's bindings, which 1.8.1.9 no longer
has; they stay in `tools/` as history and are not part of this release's
evidence. `host_suite_1819.py` replaces `host_pool_suite.py`.

## Known differences from 1.8.1.8

- **In-chat status line.** Drawn above the Desktop composer (`composer.top`),
  not in Hermes' spinner. CLI, TUI and gateway clients have no plugin channel
  for it; the chip, `/kame` and `/kame-quota` carry the same state.
- **Where KAME steps aside.** Anthropic-Messages and Responses-API endpoints,
  `api.openai.com`, `opencode-*`, `actual`, and any provider configured with a
  non-chat `api_mode` keep Hermes' own client: Hermes uses a profile's client
  without wrapping it for another wire. There KAME classifies and sizes every
  refusal through the hook and Hermes' pool rotates; the in-call carousel
  (least-loaded key first, waiting, continuation) does not run on those wires.
- **Comma-joined keys.** KAME's client splits `GOOGLE_API_KEY=k1,k2,…` itself.
  Hermes' own pool still sees one entry until `/kame-keys split` is run once;
  that matters only on the wires above.

## Disclosures

- No core function, method, module attribute or private table is replaced,
  wrapped or rebound. `hermes-kame-provider` registers copies of Hermes'
  bundled API-key chat-completions profiles, unchanged except for
  `create_client`, through `register_provider` — the documented per-home
  override.
- `/kame-keys add|import|split` write keys to Hermes' `auth.json` through the
  pool's public API, after saving a plaintext backup `auth.json.kame-<stamp>.bak`
  beside it (last 5 kept). `split` also records a suppressed source with
  Hermes' own `suppress_credential_source`. `.env` is never rewritten by
  `split`.
- Local, key-redacted logs (`refusals.jsonl`, `calls.jsonl`) and a status file
  per profile; each log can be switched off. No telemetry, no network call of
  KAME's own, no third-party package.
- These are finite offline checks against one Hermes release, not provider
  certification or a guarantee of uptime or speed on real traffic.
