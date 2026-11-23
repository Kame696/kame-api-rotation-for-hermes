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
  // 1.9.0.6 (C21): the row's running time is `elapsed_s`; the page counts it from `started_at` (checked below)
  assert.equal(
    liveLineText(row.activity, row.activity.eta_s ?? null, row.activity.elapsed_s ?? null),
    row.line,
    `${row.kind}: ${JSON.stringify(row.activity)}`
  )
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
// no time known = no suffix (an older snapshot)
assert.equal(liveLineText({ kind: 'calling', model: 'm', ready: 3, keys: 3 }, null), '⏳ Asking m · 3 of 3 keys ready')
// the page counts the running time itself from started_at (epoch seconds) and its own clock
const { activityOf, elapsedOf } = panelHelpers
assert.equal(elapsedOf({ started_at: 1000 }, 1023000), 23)
assert.equal(elapsedOf({ elapsed_s: 125 }, 0), 125)
assert.equal(elapsedOf({}, 0), null)
// an ended reply shows its line for 10 s, then the line clears; an answered one is gone at once
const ended = { kind: 'ended', reason: 'every key was refused', ended_at: 5000 }
assert.deepEqual(activityOf({ activities: { s1: ended } }, 's1', 5009000), ended)
assert.equal(activityOf({ activities: { s1: ended } }, 's1', 5011000), null)
assert.equal(activityOf({ activities: {} }, 's1', 5001000), null)
assert.equal(liveLineText(null, null), '')

// durations: the same table the Python side checks
const DURATIONS = [
  [0, '0s'], [0.4, '0s'], [0.5, '1s'], [45, '45s'], [59.4, '59s'], [59.6, '1m 00s'], [60, '1m 00s'],
  [125, '2m 05s'], [130, '2m 10s'], [3599, '59m 59s'], [3600, '1h 00m'], [3725, '1h 02m'], [null, '0s']
]

for (const [seconds, text] of DURATIONS) assert.equal(duration(seconds), text, `duration(${seconds})`)

console.log('live words: ok')
