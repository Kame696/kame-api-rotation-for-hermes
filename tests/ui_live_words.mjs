/**
 * 1.8.3.1 (PLAN_1.8.3.1 D2) - the live line says the words of the shared table.
 *
 * Every row of `fixtures/live_words_1831.json` (byte-identical in the Agent Zero port) goes through the
 * page's own `liveLineText`, and the durations through its `duration`. Run directly
 * (`node tests/ui_live_words.mjs`) or through `tests/test_1_8_3_1_live_line.py`, which skips without node.
 */

import assert from 'node:assert/strict'

import { loadPlugin, readFixture } from './ui_harness.mjs'

const { panelHelpers } = await loadPlugin()
const { duration, liveLineText } = panelHelpers
assert.equal(typeof liveLineText, 'function', 'the live line is exported for the checks')
assert.equal(typeof duration, 'function')

const table = readFixture()

for (const row of table.live) {
  // the page counts down from the snapshot's clock; here the number in the row is the time left
  assert.equal(liveLineText(row.activity, row.activity.eta_s ?? null), row.line, `${row.kind}: ${JSON.stringify(row.activity)}`)
}

// the words of the other half, with the field names Hermes publishes
assert.equal(
  liveLineText({ kind: 'calling', model: 'm', healthy: 3, keys: 4, attempt: 1 }, null),
  '↻ m: trying another key · 3 of 4 keys ready'
)
assert.equal(
  liveLineText({ kind: 'calling', phase: 'racing', model: 'm', healthy: 3, keys: 3 }, null),
  '⚡ m is slow to start - a backup key is asking too'
)
assert.equal(
  liveLineText({ kind: 'calling', phase: 'standin', model: 'm', other: 'n', healthy: 3, keys: 3 }, null),
  "↪ m can't answer now - n is answering instead"
)
assert.equal(
  liveLineText({ kind: 'stitching', model: 'm', resume: 2, budget: 5 }, null),
  '↻ The answer was cut off - continuing it on another key (2 of 5)'
)
assert.equal(
  liveLineText({ kind: 'stitching', model: 'm', resume: 2, budget: null }, null),
  '↻ The answer was cut off - continuing it on another key'
)
assert.equal(liveLineText({ kind: 'recovered', model: 'm', waited_s: 130 }, null), '↻ m is back after 2m 10s')
assert.equal(liveLineText({ kind: 'idle' }, null), '')
assert.equal(liveLineText(null, null), '')

// durations: the same table the Python side checks
const DURATIONS = [
  [0, '0s'], [0.4, '0s'], [0.5, '1s'], [45, '45s'], [59.4, '59s'], [59.6, '1m 00s'], [60, '1m 00s'],
  [125, '2m 05s'], [130, '2m 10s'], [3599, '59m 59s'], [3600, '1h 00m'], [3725, '1h 02m'], [null, '0s']
]

for (const [seconds, text] of DURATIONS) assert.equal(duration(seconds), text, `duration(${seconds})`)

console.log('live words: ok')
