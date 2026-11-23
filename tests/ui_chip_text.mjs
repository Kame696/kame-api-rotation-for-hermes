/**
 * 1.9.0.5 (PLAN C5) - the status bar chip says what API Rotation is doing, in the composer line's words.
 *
 * `chipText(snap, now, memory)` is the page's own pure helper; every "live" row of
 * `fixtures/live_words_1831.json` goes through it. Run directly (`node tests/ui_chip_text.mjs`) or through
 * `tests/test_1_9_0_5_chip_text.py`, which skips without node.
 */

import assert from 'node:assert/strict'

import { loadPlugin, readFixture } from './ui_harness.mjs'

const { panelHelpers } = await loadPlugin()
const { chipText, liveLineText } = panelHelpers
assert.equal(typeof chipText, 'function', 'chipText is exported for the checks')

const NOW_MS = 1_800_000_000_000
const snapOf = (activity, at = NOW_MS) => ({ installed: true, updated_at: at / 1000, activity })
const fresh = () => ({ text: '', until: 0 })

// -- every acting state of the shared table says its sentence; plain calling says nothing --------------
for (const row of readFixture().live) {
  const plainFirstAttempt = row.kind === 'calling'
  // 1.9.0.6 (C21): an ended reply is shown for ten seconds after `ended_at`
  const activity = row.kind === 'ended' ? { ...row.activity, ended_at: NOW_MS / 1000 - 2 } : row.activity
  const got = chipText(snapOf(activity), NOW_MS, fresh())

  assert.equal(got, plainFirstAttempt ? '' : row.line, `${row.kind}: ${JSON.stringify(row.activity)}`)
  if (!plainFirstAttempt) {
    assert.equal(got, liveLineText(activity, activity.eta_s ?? null, activity.elapsed_s ?? null))
  }
}

// an ended reply's line is gone after ten seconds
assert.equal(chipText(snapOf({ kind: 'ended', reason: 'stopped', ended_at: NOW_MS / 1000 - 11 }), NOW_MS, fresh()), '')

// -- idle, not installed, stale: the pool counts stay ----------------------------------------------------
assert.equal(chipText(snapOf(null), NOW_MS, fresh()), '')
assert.equal(chipText(null, NOW_MS, fresh()), '')
assert.equal(chipText({ installed: false, activity: { kind: 'waiting', model: 'm' } }, NOW_MS, fresh()), '')
assert.equal(chipText(snapOf({ kind: 'waiting', model: 'm', eta_s: 30 }, NOW_MS - 200_000), NOW_MS, fresh()), '')
assert.equal(chipText(snapOf({ kind: 'idle' }), NOW_MS, fresh()), '')

// -- the waiting countdown is the page's own: it runs down from the snapshot's clock -----------------------
const waiting = { kind: 'waiting', model: 'm', ready: 0, keys: 3, eta_s: 130 }
assert.equal(chipText(snapOf(waiting), NOW_MS, fresh()), '⏳ Every key of m is resting · next key in 2m 10s')
assert.equal(chipText(snapOf(waiting), NOW_MS + 30_000, fresh()), '⏳ Every key of m is resting · next key in 1m 40s')

// -- the Hermes field names (healthy / keys) read the same ---------------------------------------------------
assert.equal(chipText(snapOf({ kind: 'calling', model: 'm', healthy: 3, keys: 3, attempt: 1 }), NOW_MS, fresh()), '')
assert.equal(
  chipText(snapOf({ kind: 'calling', model: 'm', healthy: 3, keys: 3, attempt: 2 }), NOW_MS, fresh()),
  '↻ m: trying another key · 3 of 3 keys ready'
)
assert.equal(
  chipText(snapOf({ kind: 'calling', phase: 'racing', model: 'm', healthy: 3, keys: 3 }), NOW_MS, fresh()),
  '⚡ m is slow to start - a backup key is asking too'
)

// -- a recovery stays six seconds, then the counts come back ----------------------------------------------------
const memory = fresh()
const recovered = { kind: 'recovered', model: 'm', waited_s: 45 }
assert.equal(chipText(snapOf(recovered), NOW_MS, memory), '↻ m is back after 45s')
assert.equal(chipText(snapOf(null), NOW_MS + 5_000, memory), '↻ m is back after 45s', 'held after the activity is gone')
assert.equal(chipText(snapOf(null), NOW_MS + 6_500, memory), '', 'six seconds over')
// the activity itself lingering past the hold does not keep the sentence on
const lingering = fresh()
assert.equal(chipText(snapOf(recovered), NOW_MS, lingering), '↻ m is back after 45s')
assert.equal(chipText(snapOf(recovered, NOW_MS + 7_000), NOW_MS + 7_000, lingering), '')
// a new acting sentence replaces the held one at once
const replaced = fresh()
chipText(snapOf(recovered), NOW_MS, replaced)
assert.equal(chipText(snapOf(waiting, NOW_MS + 1_000), NOW_MS + 1_000, replaced), '⏳ Every key of m is resting · next key in 2m 10s')
assert.equal(chipText(snapOf(null), NOW_MS + 2_000, replaced), '', 'the old recovery does not come back')

console.log('chip text: ok')
