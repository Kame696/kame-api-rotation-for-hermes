# 1.8.2.1 validation

1.8.2.1 changes one default and one paragraph of the README. Everything else
is the 1.8.2.0 measured below.

**The default.** When the user has not set `max_total_wait_seconds`, a cron
session (`HERMES_CRON_SESSION`) waits at most 300 s and then gets the
provider's original refusal. A chat keeps the unbounded wait. A value the user
sets wins everywhere, `0` included.

**The paragraph.** The catalog README now states plainly that the optional
bridge patches Hermes core and is not supported.

| Check | Result |
|---|---|
| Plugin suite on Hermes `56f7986` | 2,856 passed. 5 new tests cover a chat with nothing set (no bound), a cron run with nothing set (300 s), an explicit 0 in cron (no bound), an explicit number in cron, and the original refusal raised after 300 s in cron |
| Continuity gate | 23/23 when run with the machine idle. Under a parallel load (Hermes open), one end-to-end scenario timed out, as noted for 1.8.1.9 |
| `hermes plugins validate` | both catalog packages pass, `no core override` and `desktop surface` included |

---

## 1.8.2.0 validation and limitations

Three packages at 1.8.2.0. `hermes-kame-api-rotation` and `hermes-kame-provider`
form the catalog entry. `hermes-kame-bridge` is optional and not part of it.

Unless a section says otherwise, everything below ran offline, against a copy
of Hermes `56f7986` (main of 2026-10-05, the version the author runs): local
fake providers, throwaway `HERMES_HOME`s, no real key. The 1.8.1.9 sections
further down are kept as they were.

## Every wire: 1.8.1.8 vs 1.8.2.0, on today's Hermes

Real Hermes turns, 15 keys. Scenarios:

- **S0** healthy
- **S1** per-minute 429 on keys 1-5
- **S2** per-day 429 on model A only
- **S3** 503 overload
- **S4** every key 429 for 8 s
- **S5** stream cut after 2 deltas on key 1

Each cell reads: turns answered · requests sent · slowest steady turn (s).
The bridge column was measured where the bridge changes something: the
Anthropic wire, and the spinner check on two scenarios each for Gemini and the
OpenAI-compatible (`custom`) wire.

| wire | scen | 1.8.1.8 | 1.8.2.0 | 1.8.2.0 + bridge |
|---|---|---|---|---|
| gemini | S0 | 3/3 · 3 · 0.30 | 3/3 · 3 · 0.24 | — |
| gemini | S1 | 4/4 · 9 · 0.39 | 4/4 · 9 · 0.23 | 4/4 · 9 · 0.21 |
| gemini | S2 | 6/6 · 16 · 0.33 | 6/6 · 16 · 0.52 | — |
| gemini | S3 | 3/3 · 4 · 1.36 | 3/3 · 4 · 0.24 | — |
| gemini | S4 | 3/3 · 19 · 0.44 | 3/3 · 33 · 0.25 | 3/3 · 33 · 0.25 |
| gemini | S5 | 3/3 · 5 · 0.22 | 3/3 · 5 · 0.24 | — |
| custom | S0 | 3/3 · 3 · 0.84 | 3/3 · 3 · 0.22 | — |
| custom | S1 | 4/4 · 9 · 0.88 | 4/4 · 9 · 0.38 | 4/4 · 9 · 0.23 |
| custom | S2 | 6/6 · 16 · 0.57 | 6/6 · 16 · 0.20 | — |
| custom | S3 | 3/3 · 4 · 0.52 | 3/3 · 4 · 0.18 | — |
| custom | S4 | 3/3 · 33 · 0.70 | 3/3 · 33 · 0.23 | 3/3 · 33 · 0.21 |
| custom | S5 | 3/3 · 6 · 0.83 | 3/3 · 4 · 0.21 | — |
| anthropic | S0 | 3/3 · 3 · 2.03 | 3/3 · 3 · 0.16 | 3/3 · 3 · 0.22 |
| anthropic | S1 | 4/4 · 9 · 1.99 | **3/4** · 9 · 22.13 | 4/4 · 9 · 0.31 |
| anthropic | S2 | 6/6 · 16 · 1.51 | **3/6** · 18 · 0.62 | 6/6 · 16 · 0.88 |
| anthropic | S3 | 3/3 · 4 · 1.59 | 3/3 · 5 · 0.16 | 3/3 · 4 · 0.23 |
| anthropic | S4 | 3/3 · 11 · 1.75 | 3/3 · 6 · 1.57 | 3/3 · 32 · 0.20 |
| anthropic | S5 | 3/3 · 6 · 1.66 | **0/1** · 36 · — | 3/3 · 4 · 0.23 |
| responses | S0 | 3/3 · 3 · 0.83 | 3/3 · 3 · 0.20 | — |
| responses | S1 | 4/4 · 8 · 1.09 | 4/4 · 9 · 0.20 | — |
| responses | S2 | 6/6 · 15 · 0.88 | 6/6 · 16 · 0.23 | — |
| responses | S3 | 3/3 · 3 · 0.94 | 3/3 · 4 · 0.22 | — |
| responses | S4 | 3/3 · 33 · 0.78 | 3/3 · 33 · 0.23 | — |
| responses | S5 | 3/3 · 3 · 0.76 | 3/3 · 4 · 0.21 | — |

How to read it:

- **Native Gemini, OpenAI chat completions and Responses.** Every turn
  answered, as in 1.8.1.8, and steady turns are faster.
- **Gemini S4, 19 vs 33 requests.** This difference is timing variance:
  1.8.1.8's own reference run of S4 sent 33.
- **The Anthropic Messages wire.** Without the bridge, Hermes `56f7986` has no
  `create_messages_client`, so it builds its own client. The 1.8.2.0 column
  then matches Hermes with no plugin, cell for cell. KAME still classifies
  every refusal there, but cannot pick the key per call.
- **The Anthropic wire with `create_messages_client` present.** Through the
  bridge, or through Hermes with #133461 applied (S1 4/4, S2 6/6, S5 3/3), the
  wire rotates again and is faster than 1.8.1.8.

## The status line

KAME's line (`⏳ waiting on … — KAME 15/15 keys healthy`) needs
`notify_turn_status`. Measured on the spinner (`thinking`) rail:

- **Hermes `56f7986` alone:** no line on the rail. The Desktop composer line
  shows it instead.
- **With the bridge:** the line is on the rail on every wire measured.
  Example: Gemini S4, 12 KAME lines in the first turn.
- **Hermes with #133474 applied, no bridge:** the same lines as with the
  bridge.

## Real keys (owner's NVIDIA pool, 2 keys, `z-ai/glm-5.3`)

The run used 1.8.2.0 with the bridge, on the owner's own config, in a
throwaway home:

- 3 of 3 turns answered.
- One key answered empty and the next key finished the turn.
- The spinner showed `⏳ waiting on z-ai/glm-5.3 — KAME 2/2 keys healthy` and
  `↻ … trying the next key`.
- Turns took 72-236 s. That is the model's own latency on NVIDIA: one call
  took 567 s in the owner's own session the same morning.
- The key sync read the comma-joined `GOOGLE_API_KEY` (14 keys) and
  `NVIDIA_API_KEY` (2 keys) through Hermes' new scoped secret store. In
  1.8.1.9 that read failed with `UnscopedSecretError`.

## Checks

| Check | Result |
|---|---|
| Plugin suite on Hermes `56f7986` | 2,851 passed, 0 failed (23 new 1.8.2.0 tests included) |
| Plugin suite on Hermes with #133461 applied, and with #133474 applied | 2,851 passed, 0 failed on each |
| `hermes plugins validate` | `hermes-kame-api-rotation` and `hermes-kame-provider` pass, `desktop surface` and `no core override` included. `hermes-kame-bridge` fails `no core override` by design, which is why it is not in the catalog entry |
| UI harness (`tests/ui_reconcile.mjs`) | all checks pass. It now walks the Settings and Events tabs it opens; until 1.8.2.0 it rendered them after switching back to Overview |
| host_corpus · host_prose | pass (5 documented intentional differences) |
| host_assumptions | 2 checks fail on Hermes `56f7986` with 1.8.1.9 and 1.8.2.0 alike: the host now starts a subprocess inside two code paths the offline contract blocks. These are host-side facts and do not depend on the KAME version |

---

## 1.8.1.9 validation and limitations (kept as published)

Both packages at 1.8.1.9 (rework of 2026-10-05). Unless a section says
otherwise, everything below ran offline: local fake providers, throwaway
`HERMES_HOME`s, no real key. The Hermes used is 0.21.4 (`59004a6`), the same
source the author runs, with and without the two upstream seams this release
uses when present: `ProviderProfile.create_messages_client`
(NousResearch/hermes-agent#133461) and `agent.status_output.notify_turn_status`
(NousResearch/hermes-agent#133474).

## Every wire: 1.8.1.6 vs 1.8.1.8 vs 1.8.1.9

Real Hermes turns, 15 keys, local fake providers. S0 healthy · S1 per-minute
429 on keys 1-5 · S2 per-day 429 on model A only · S3 503 overload · S4 every
key 429 for 8 s · S5 stream cut after 2 deltas on key 1. Cell: turns answered ·
requests sent · slowest steady turn (s).

| wire | scen | 1.8.1.6 | 1.8.1.8 | 1.8.1.9 |
|---|---|---|---|---|
| gemini | S0 | 3/3 · 3 · 0.27 | 3/3 · 3 · 0.20 | 3/3 · 3 · 0.19 |
| gemini | S1 | 4/4 · 9 · 0.29 | 4/4 · 9 · 0.20 | 4/4 · 9 · 0.19 |
| gemini | S2 | 6/6 · 16 · 0.32 | 6/6 · 16 · 0.23 | 6/6 · 16 · 0.26 |
| gemini | S3 | 3/3 · 4 · 0.26 | 3/3 · 4 · 0.22 | 3/3 · 4 · 0.18 |
| gemini | S4 | 3/3 · 28 · 0.24 | 3/3 · 33 · 0.21 | 3/3 · 33 · 0.20 |
| gemini | S5 | 3/3 · 5 · 0.35 | 3/3 · 5 · 0.19 | 3/3 · 5 · 0.18 |
| custom | S0 | 3/3 · 3 · 1.19 | 3/3 · 3 · 0.97 | 3/3 · 3 · 0.31 |
| custom | S1 | 4/4 · 9 · 1.64 | 4/4 · 9 · 0.67 | 4/4 · 9 · 0.21 |
| custom | S2 | 6/6 · 16 · 0.79 | 6/6 · 16 · 0.68 | 6/6 · 16 · 0.24 |
| custom | S3 | 3/3 · 4 · 0.70 | 3/3 · 4 · 0.61 | 3/3 · 4 · 0.20 |
| custom | S4 | 3/3 · 23 · 2.54 | 3/3 · 29 · 0.60 | 3/3 · 33 · 0.28 |
| custom | S5 | 3/3 · 6 · 1.11 | 3/3 · 6 · 0.68 | 3/3 · 4 · 0.24 |
| anthropic | S0 | 3/3 · 3 · 1.75 | 3/3 · 3 · 2.32 | 3/3 · 3 · 0.23 |
| anthropic | S1 | 4/4 · 9 · 1.90 | 4/4 · 9 · 1.55 | 4/4 · 9 · 0.24 |
| anthropic | S2 | 6/6 · 16 · 1.91 | 6/6 · 16 · 2.88 | 6/6 · 16 · 0.66 |
| anthropic | S3 | 3/3 · 4 · 2.90 | 3/3 · 4 · 1.32 | 3/3 · 4 · 0.21 |
| anthropic | S4 | 3/3 · 11 · 1.70 | 3/3 · 7 · 7.55 | 3/3 · 33 · 0.23 |
| anthropic | S5 | 3/3 · 6 · 2.50 | 3/3 · 6 · 4.45 | 3/3 · 4 · 0.18 |
| responses | S0 | 3/3 · 3 · 0.83 | 3/3 · 3 · 1.19 | 3/3 · 3 · 0.21 |
| responses | S1 | 4/4 · 8 · 0.97 | 4/4 · 8 · 1.31 | 4/4 · 9 · 0.20 |
| responses | S2 | 6/6 · 15 · 1.19 | 6/6 · 15 · 3.24 | 6/6 · 16 · 0.24 |
| responses | S3 | 3/3 · 3 · 0.94 | 3/3 · 3 · 1.27 | 3/3 · 4 · 0.20 |
| responses | S4 | 3/3 · 33 · 0.89 | 3/3 · 31 · 1.74 | 3/3 · 33 · 0.22 |
| responses | S5 | 3/3 · 3 · 0.90 | 3/3 · 3 · 1.22 | 3/3 · 4 · 0.18 |
| gemini-comma | S0 | 3/3 · 3 · 0.33 | 3/3 · 3 · 0.21 | 3/3 · 3 · 0.21 |
| gemini-comma | S1 | 4/4 · 9 · 0.25 | 4/4 · 9 · 0.28 | 4/4 · 9 · 0.23 |
| gemini-comma | S2 | 6/6 · 16 · 0.26 | 6/6 · 16 · 0.24 | 6/6 · 16 · 0.22 |
| gemini-comma | S3 | 3/3 · 4 · 0.23 | 3/3 · 4 · 0.20 | 3/3 · 4 · 0.18 |
| gemini-comma | S4 | 3/3 · 33 · 0.24 | 3/3 · 33 · 0.20 | 3/3 · 33 · 0.21 |
| gemini-comma | S5 | 3/3 · 5 · 0.26 | 3/3 · 5 · 0.23 | 3/3 · 5 · 0.19 |

On every wire every turn answered on all three versions, and 1.8.1.9 is the
fastest on steady turns. On Anthropic Messages a cut answer is now continued on
another key (S5: 4 requests instead of 6). S4 sends more refused probes on the
Messages and Responses wires because the carousel now runs there (1.8.1.8 left
those wires to Hermes' own retry loop); they are 429s inside the 8 s window.

The status line — `⏳ waiting on <model> — KAME n/m keys healthy`, the
countdown while every key rests, the "KAME: … resting" notices — was captured
from the agent's thinking/status callbacks on every wire, with 1.8.1.8's wording
and throttle.

## Real keys, real providers

The author's own keys (Gemini x14 in one comma-joined `GOOGLE_API_KEY`, NVIDIA
x2), copied into a throwaway home and deleted after each run, 3 turns each,
1.8.1.8 and 1.8.1.9 on the same Hermes. Every turn answered on both. Gemini
returned real 503s ("model overloaded", some after ~80 s) and KAME took the
next key. NVIDIA `moonshotai/kimi-k3` at a 32-token budget answered empty most
of the time (direct probe: 5 of 6, `finish_reason=length` after reasoning);
both versions apply the same rule (empty answer, next key) and show the same
long turns. On start, the comma-joined variables became 14 and 2 pool rows,
aliases (`google`, `nim`, …) untouched.

## Public regression

The public checkout, run the way CI runs it (a clean Python 3.11 with only pytest installed, no Hermes, no third-party package): **2,764 passed, 330 skipped, 4 expected failures** (398 s), and one timing-sensitive test, `test_continuity_gate.py::TestFullGateEndToEnd::test_run_gate_returns_the_same_scenarios_in_process`, whose negative control needs three processes to collide inside a short window; under the full run's load they did not, and the same test passes run alone (101 s). The skips are the 322 still-retired 1.8.1.8 tests listed below, plus tests that need Hermes or a POSIX platform. The four expected failures are the documented classifier tradeoffs and the unverified Codex OAuth refresh-error mapping carried from 1.8.1.8. Run `python -m pytest tests -q` after installing pytest.

The development workspace ran **2,821 tests, 0 failed**, with Hermes 0.21.4 on
the path, both with and without the two upstream seams. The 1.8.1.8 suite runs
against 1.8.1.9 unchanged: `tools/legacy_dispatch.py` answers the old dispatch
import with the new transport. 322 1.8.1.8 tests stay retired, each listed in
`tests/legacy_1818_retired_ids.txt` with its reason and the test that now holds
the same promise in `tests/legacy_1818_retired.py`; 39 that the first 1.8.1.9
build retired (spinner line, wait notices, settings) run again. The retired
ones test what no longer exists: the wrapped host functions, the pool binding,
the removed Gemini repair, or scans of a source file that moved (re-run on
`transport.py` in `tests/test_source_invariants_1819.py`). New:
`test_transport_1819.py` (wire-level stream behaviour and the status line),
`test_facade_1819.py` (real HTTP through Hermes' own client classes, every
wire), `test_envsync.py` (multi-key variables against Hermes' real pool),
`test_source_invariants_1819.py`.

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

Run on the first 1.8.1.9 build (chat-completions clients); the Messages and
Responses clients added since are covered by `test_facade_1819.py` and the
every-wire matrix above.

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

## What depends on the Hermes version

- **Anthropic Messages** goes through KAME's carousel on a Hermes that asks
  `create_messages_client` (#133461). An older Hermes builds its own Messages
  client; KAME still classifies and sizes every refusal there.
- **Status line on the spinner** (CLI, TUI, Desktop, messaging gateway) needs
  `notify_turn_status` (#133474). Without it the Desktop panel draws the same
  line above the composer (`composer.top`), and the chip, `/kame` and
  `/kame-quota` carry the same state everywhere.
- **Not carried over, because Hermes now does it itself** (since v2026.9.21,
  the oldest supported): 1.8.1.8's repair of merged parallel Gemini tool calls
  (Hermes keeps them apart; 1.8.1.8's own self-check stands down), and its
  probe of the Settings key field (the Desktop saves a pasted key without
  probing it).

## Disclosures

- No core function, method, module attribute or private table is replaced,
  wrapped or rebound. `hermes-kame-provider` registers copies of Hermes'
  bundled API-key profiles, unchanged except for `create_client` and
  `create_messages_client`, through `register_provider` — the documented per-home
  override.
- `/kame-keys add|import|split` write keys to Hermes' `auth.json` through the
  pool's public API, after saving a plaintext backup `auth.json.kame-<stamp>.bak`
  beside it (last 5 kept). `split` also records a suppressed source with
  Hermes' own `suppress_credential_source`.
- On every start, a provider key variable that holds several keys
  (`GOOGLE_API_KEY=k1,k2,…`) is kept as one `auth.json` pool row per key, the
  same way `split` does it (`source: manual:kame-env:<VAR>`, backup first, the
  comma source suppressed), and follows the variable as it changes; only rows
  KAME made are ever removed. `resolver_disabled` turns this off. `.env` is
  never rewritten.
- Local, key-redacted logs (`refusals.jsonl`, `calls.jsonl`) and a status file
  per profile; each log can be switched off. No telemetry, no network call of
  KAME's own, no third-party package.
- These are finite offline checks against one Hermes release, not provider
  certification or a guarantee of uptime or speed on real traffic.
