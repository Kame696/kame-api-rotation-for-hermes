/** 1.9.0.6 (C11) - the chip and the composer line read the open chat's own activity. */
import assert from 'node:assert/strict'

import { loadPlugin } from './ui_harness.mjs'

const { panelHelpers } = await loadPlugin()
const { activityOf, chipText } = panelHelpers

const NOW_MS = 1_800_000_000_000
const calling = model => ({ kind: 'calling', phase: 'standin', model, other: 'other-model' })
const snap = {
  installed: true,
  updated_at: NOW_MS / 1000,
  activity: calling('latest'),
  activities: { A: calling('model-a'), B: calling('model-b') }
}

assert.equal(activityOf(snap, 'A').model, 'model-a')
assert.equal(activityOf(snap, 'B').model, 'model-b')
assert.equal(activityOf(snap, 'C'), null, 'a chat with no entry shows nothing, never another chat')
assert.equal(activityOf({ activity: calling('old') }, 'A').model, 'old', 'an older plugin without activities: the latest')
assert.equal(activityOf(snap, null).model, 'latest', 'a host that cannot say which chat is open: the latest')

const fresh = () => ({ text: '', until: 0 })

assert.match(chipText(snap, NOW_MS, fresh(), 'A'), /model-a/)
assert.match(chipText(snap, NOW_MS, fresh(), 'B'), /model-b/)
assert.equal(chipText(snap, NOW_MS, fresh(), 'C'), '')
console.log('ok')
