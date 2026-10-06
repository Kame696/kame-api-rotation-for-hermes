# 1.8.1.8 validation and limitations

## Reproducible public regression

The dedicated public checkout passed **3,133 tests**, with six Windows/platform
or host-isolation skips, four documented expected failures and zero unexpected
failures (204.78 seconds). Run `python -m pytest tests -q` after
installing pytest. The optional JavaScript execution check also uses Node.

The full development workspace passed **3,157** tests. Its additional 24
assertions exercise private research/evidence-navigation tools, not additional
runtime functionality; those ledgers and owner traces are not published.
Historical public tests remain, with focused profile, recovery, cache, serial-only,
Desktop and clock regressions added. CI retains Linux/macOS/Windows and Python
3.11/3.12/3.13; a local run is not a claim that every CI matrix job already passed.

The six skips are five POSIX-permission checks on Windows and one isolated host
tripwire. Four expected failures retain three classifier tradeoffs and an
unverified Codex OAuth refresh-error dispatch mapping; they are not new passes.

## Host contracts

Publication-tip Hermes `46904a3b467f62616f5b3ee247adce30b1b277a0` passed unchanged admission **15/15**
with positive/negative controls, runtime **12 + 12 mutation controls**, native
Gemini **4 + 4 mutation controls**, and real three-home PluginManager lifecycle,
unload/reload/heartbeat cleanup **11/11**. Scratch homes and fixture transport
were used; the owner's installed Hermes core and configuration were untouched.

The earlier pinned Hermes `476268f16732e09b93bc7202ee38f773b913707e` additionally
passed 15 actual SDK/native TCP cases, active pool integration in 99 fixture homes,
spreading/off controls and credential-hiding mutations. Its 200-case classifier
corpus retains five documented intentional differences. Pool hold-ceiling changes
are intentional, not hidden host-test failures. These are finite compatibility
checks, not exhaustive provider certification.

## Preservation and performance

All 24 non-stitch core ASTs match public 1.8.1.6 except version metadata. Recovery
code intentionally changes; there is no claim of whole-dispatch AST equality.
No provider/model/effort downgrade, discarded history/signatures or speculative
request racing is introduced. Runtime bytes match the previously validated owner
trial; publication changes its README only. Earlier releases are preserved.

Balanced disk-inclusive snapshot/router benchmarks saved about **28 ms per
tested call (63–65%)**, with same-source and artificial-slowdown controls.
This does **not** mean 65% faster whole Hermes answers, lower provider billing,
or measured CPU/RAM savings. Representative paired quality/p95 proof remains open.
Limited real Gemini/NVIDIA successes, an earlier wrong NVIDIA arithmetic answer,
daily-spent Gemini credentials and 429/503/deadline failures remain in the private
evidence; they are not converted into universal speed/quality claims.

## Disclosures and catalog review

Guarded core bindings remain: main request helpers and temporary delivery funnel,
CredentialPool methods, auxiliary calls, credential Settings route, resolver,
native Gemini error evidence and witness-gated tool translation. They delegate,
are profile-scoped and conditionally restored on unload; they are not public
credential/retry APIs. The unchanged scanner's syntactic pass is not evidence
that these bindings disappeared or that rule 9 grants a new exception.

Prior pin-specific ruling:
https://github.com/NousResearch/hermes-agent/pull/117966#issuecomment-5762488795
Landing: https://github.com/NousResearch/hermes-agent/pull/118402
Current rule 9: https://github.com/NousResearch/hermes-agent/blob/46904a3b467f62616f5b3ee247adce30b1b277a0/plugin-catalog/README.md
The catalog's current 1.8.1.0 pin is separate from this GitHub release. The update
asks for explicit renewed maintainer review; acceptance is not guaranteed.

`/kame-keys add|import` writes the configured Hermes auth store after a plaintext
backup `auth.json.kame-<timestamp>.bak`, retaining the last five. Status/timing
state contains key fingerprints, not raw keys; local refusal logs redact keys
and are switchable. A stoppable heartbeat writes local status. No telemetry,
self-updater, unrelated OAuth write, automatic approval or credential egress is
added. No secret found by the owner-key-aware publication check is shipped.

## Artifact and rollback

ZIP: `hermes-kame-api-rotation-1.8.1.8.zip` (51 members).
SHA-256: `79e55f12838183fae8292a08616efb84b76c80d5c44e0a5adebf842aba90c7fe`.
Runtime fingerprint: `2b29f123099d` (documentation-independent).
The ZIP is CRC-checked and matches the public plugin payload exactly.

Restart Hermes after updating. To roll back a direct installation, disable the
plugin, restore its previous plugin directory from your backup, and restart;
do not delete auth/config/history. The untouched 1.8.1.6 release asset remains
available at https://github.com/Kame696/kame-api-rotation-for-hermes/releases/tag/v1.8.1.6.

Machine summary: [validation/1.8.1.8.json](validation/1.8.1.8.json).
