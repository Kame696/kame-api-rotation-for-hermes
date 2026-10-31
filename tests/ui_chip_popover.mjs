/**
 * 1.8.3.1 (PLAN_1.8.3.1 D3) - the chip's popover says the words of the shared table.
 *
 * `replyLineText` runs every "replies" row of `fixtures/live_words_1831.json` (byte-identical in the
 * Agent Zero port); `replyFromRow` maps what the backend publishes onto that shape; `chipPopoverModel` is
 * called with snapshots (the content of the popover is plain data, so it is checked as data).
 * Run directly (`node tests/ui_chip_popover.mjs`) or through `tests/test_1_8_3_1_chip_popover.py`.
 */

import assert from 'node:assert/strict'

import { loadPlugin, readFixture } from './ui_harness.mjs'

const { panelHelpers } = await loadPlugin()
const { chipPopoverModel, ordinal, replyFromRow, replyLineText } = panelHelpers
assert.equal(typeof chipPopoverModel, 'function', 'the popover model is exported for the checks')

// -- the sentences of the shared table ----------------------------------------------------------------
for (const row of readFixture().replies) assert.equal(replyLineText(row.reply), row.line, JSON.stringify(row.reply))

assert.equal(replyLineText(null), '')
assert.equal(replyLineText({ kind: 'unknown' }), '')
assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111].map(ordinal), [
  '1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd', '101st', '111th'
])

// -- what the backend publishes becomes one of those sentences --------------------------------------------
const NOW = 1_800_000_000
const plain = { at: NOW - 5, model: 'm', total_s: 2.3 }

assert.equal(replyLineText(replyFromRow(plain)), '✓ m answered in 2.3s')
assert.equal(replyLineText(replyFromRow({ ...plain, attempts: 2 })), '↻ m answered on the 2nd key in 2.3s')
assert.equal(replyLineText(replyFromRow({ ...plain, backup: true })), '⚡ a backup key of m answered first (2.3s)')
assert.equal(replyLineText(replyFromRow({ ...plain, for: 'o', why: 'busy' })), '↪ m answered for o (busy) in 2.3s')
assert.equal(replyLineText(replyFromRow({ ...plain, for: 'o' })), '↪ m answered for o in 2.3s')
assert.equal(replyLineText(replyFromRow({ ...plain, waited_s: 40 })), '⏳ waited 40s for a key, then m answered in 2.3s')
assert.equal(replyLineText(replyFromRow({ ...plain, cut: true })), "↻ m's answer was cut off and finished on another key")
assert.equal(
  replyLineText(replyFromRow({ at: NOW, failed: true, model: 'm', reason: 'it timed out' })),
  '✕ no answer: it timed out'
)
// the most telling thing wins: a stand-in that also retried is still a stand-in
assert.match(replyLineText(replyFromRow({ ...plain, for: 'o', attempts: 3 })), /^↪ m answered for o/)

// -- the popover's content --------------------------------------------------------------------------------
const keys = (...codes) => codes.map((code, index) => [`k${index}`, code, null])
const snapshot = extra => ({
  activity: null,
  fallback: {
    entries: [
      { identity: 'gemini:main-model' },
      { identity: 'gemini:second' },
      { identity: 'gemini:third' }
    ],
    state: 'on'
  },
  installed: true,
  pools: [
    { identity: 'gemini:main-model', key_states: keys('r', 'r', 'r'), keys: 3 },
    { identity: 'gemini:second', key_states: keys('r', 'r', 'r'), keys: 3 },
    { identity: 'gemini:third', key_states: keys('r'), keys: 1 }
  ],
  replies: [],
  speed: { mode: 'adaptive' },
  totals: { healthy: 7, keys: 7, ready: 7 },
  updated_at: NOW,
  version: '1.8.3.1',
  ...extra
})
const view = (snap, at = NOW) => chipPopoverModel(snap, at * 1000)

let v = view(snapshot())
assert.equal(v.title, 'API Rotation')
assert.equal(v.version, 'KAME 1.8.3.1')
assert.equal(v.button, 'Open API Rotation')
assert.equal(v.sentence, 'Ready - next reply: main-model')
assert.equal(v.keysLine, '7 of 7 keys ready · Speed: Balanced')
assert.deepEqual(v.models.map(m => m.name), ['main-model', 'second', 'third'])
assert.deepEqual(v.models[0].tags, ['main'])
assert.equal(v.models[0].status, '3 of 3 ready')
assert.equal(v.models[1].status, '3 of 3 ready')
assert.equal(v.models[2].status, '1 of 1 ready')
assert.equal(v.retired, '')
assert.equal(v.replies.length, 0)
assert.equal(v.emptyReplies, 'No reply yet in this session.')
assert.equal(v.tone, 'good')

// a refused key: the model's row, the sentence and the retired-key paragraph all say so
v = view(snapshot({ pools: [
  { identity: 'gemini:main-model', key_states: keys('r', 'r', 'r'), keys: 3 },
  { identity: 'gemini:second', key_states: keys('r'), keys: 1 },
  { identity: 'gemini:third', key_states: keys('x'), keys: 1, retired: 1 }
] }))
assert.equal(v.models[2].status, '1 key refused - replace it')
assert.equal(v.models[2].tone, 'bad')
assert.equal(v.sentence, '1 key to replace — the provider refused it as credentials.')
assert.ok(v.retired.startsWith('1 key is out of rotation'), v.retired)

// speed labels
assert.match(view(snapshot({ speed: { mode: 'max' } })).keysLine, /Speed: Faster$/)
assert.match(view(snapshot({ speed: { mode: 'off' } })).keysLine, /Speed: Saver$/)

// the live state is the sentence, in the live line's words
v = view(snapshot({ activity: { kind: 'calling', model: 'main-model', healthy: 3, keys: 3, attempt: 1 } }))
assert.equal(v.sentence, '⏳ Asking main-model · 3 of 3 keys ready')
assert.deepEqual(v.models[0].tags, ['main'])
v = view(snapshot({ activity: { identity: 'gemini:second', kind: 'calling', model: 'second', healthy: 1, keys: 3, attempt: 1 } }))
assert.deepEqual(v.models[1].tags, ['in use'])

// a busy main model: the word, and the sentence is the worst thing wrong
const busy = snapshot({
  fallback: {
    entries: [
      { busy_until: NOW + 90, identity: 'gemini:main-model', probe_at: NOW + 30 },
      { identity: 'gemini:second' },
      { identity: 'gemini:third' }
    ],
    state: 'on'
  }
})

v = view(busy)
assert.equal(v.models[0].status, 'busy · asked again in 30s')
assert.equal(v.models[0].tone, 'warn')
v = view({
  ...busy,
  fallback: { entries: [{ busy_kind: 'slow', busy_until: NOW + 600, identity: 'gemini:main-model' }, { identity: 'gemini:second' }], state: 'on' }
})
assert.equal(v.models[0].status, 'slow today · skipped for 10m 00s')

// resting, and out for today
v = view(snapshot({ pools: [{ identity: 'gemini:main-model', key_states: [['k0', 's', NOW + 130], ['k1', 's', NOW + 300]], keys: 2 }] }))
assert.equal(v.models[0].status, 'resting · back in 2m 10s')
v = view(snapshot({ pools: [{ identity: 'gemini:main-model', key_states: keys('d', 'd'), keys: 2 }] }))
assert.equal(v.models[0].status, 'out for today')
assert.equal(v.models[1].status, 'not used yet')

// a model never used: quiet
assert.equal(v.models[2].tone, 'quiet')

// the last replies: at most five, newest first as the backend gives them, one sentence each
const rows = Array.from({ length: 8 }, (_, index) => ({ at: NOW - index * 60, model: 'm', total_s: 1 + index }))

v = view(snapshot({ replies: rows }))
assert.equal(v.replies.length, 5)
assert.equal(v.replies[0].line, '✓ m answered in 1.0s')
assert.match(v.replies[0].time, /^\d\d:\d\d$/)

// more than six models: the first six, then "+N more"
const many = Array.from({ length: 9 }, (_, index) => ({ identity: `gemini:model-${index}` }))

v = view(snapshot({ fallback: { entries: many, state: 'on' }, pools: [] }))
assert.equal(v.models.length, 6)
assert.equal(v.more, 3)

// an old reading is said to be old, never presented as live
v = view(snapshot(), NOW + 600)
assert.equal(v.stale, true)
assert.equal(v.tone, 'muted')

// not installed, and nothing read yet
v = view({ installed: false, reason: 'no hermes', updated_at: NOW, version: '1.8.3.1' })
assert.equal(v.sentence, 'KAME API Rotation is loaded but not rotating: no hermes')
assert.deepEqual(v.models, [])
v = chipPopoverModel(null, NOW * 1000)
assert.equal(v.sentence, 'KAME API Rotation has not been read yet.')
assert.equal(v.version, 'KAME')

console.log('chip popover: ok')
