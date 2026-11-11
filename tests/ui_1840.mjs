/**
 * 1.8.4.0 - the page tells the truth (U1, U2, U3, U5, U6) and shows the two new readouts (E4, E6).
 *
 * Everything here is a pure helper of `desktop/plugin.js` called with snapshots, plus one render of
 * the shared setting row for the "Why?" toggle. The Agent Zero port checks the same words in its own
 * tests; the strings below are the contract between the two. Run directly (`node tests/ui_1840.mjs`)
 * or through `tests/test_1_8_4_0_panel.py`.
 */

import assert from 'node:assert/strict'

import { loadPlugin } from './ui_harness.mjs'

const { panelHelpers: P } = await loadPlugin()
const NOW = 1_800_000_000
const NOW_MS = NOW * 1000

const keys = (...codes) => codes.map((code, index) => [`key:k${index}`, code, null, ''])
const snap = extra => ({
  activity: null,
  fallback: { entries: [{ identity: 'gemini:a' }, { identity: 'gemini:b' }], state: 'on' },
  installed: true,
  pools: [
    { identity: 'gemini:a', key_states: keys('r', 'r', 'r'), keys: 3 },
    { identity: 'gemini:b', key_states: keys('r', 'r', 'r'), keys: 3 }
  ],
  replies: [],
  speed: { mode: 'adaptive' },
  totals: { healthy: 6, keys: 6, physical_keys: 3, physical_ready: 3, ready: 6 },
  updated_at: NOW,
  version: '1.8.4.0',
  ...extra
})

// -- U1: one count --------------------------------------------------------------------------------
// 14 physical keys over 4 models used to read "0 of 56"
const four = ['a', 'b', 'c', 'd'].map(m => ({ identity: `gemini:${m}`, key_states: keys(...Array(14).fill('s')), keys: 14 }))
let one = P.oneCount(snap({ pools: four, totals: { healthy: 0, keys: 56, physical_keys: 14, physical_ready: 0, ready: 0 } }), NOW_MS)
assert.deepEqual([one.ready, one.total, one.model], [0, 14, null], 'nothing ready: physical keys over all models')

// a header "14 of 14" above a strip of 8 now reads 8 of 14
const eight = keys(...Array(8).fill('r'), ...Array(6).fill('s'))
one = P.oneCount(snap({ pools: [{ identity: 'gemini:a', key_states: eight, keys: 14 }], totals: { healthy: 14, keys: 14, physical_keys: 14, physical_ready: 14, ready: 14 } }), NOW_MS)
assert.deepEqual([one.ready, one.total, one.model], [8, 14, 'gemini:a'])
const popover = P.chipPopoverModel(snap({ pools: [{ identity: 'gemini:a', key_states: eight, keys: 14 }], totals: { healthy: 14, keys: 14, physical_keys: 14, physical_ready: 14, ready: 14 } }), NOW_MS)
assert.ok(popover.keysLine.startsWith('8 of 14 keys ready'), popover.keysLine)

// nothing asked yet (no pool rows): the physical totals the backend sends
one = P.oneCount(snap({ pools: [], totals: { healthy: 0, keys: 0, physical_keys: 0, physical_ready: 0, ready: 0 } }), NOW_MS)
assert.deepEqual([one.ready, one.total], [0, 0])

// an older backend without the physical fields still gives a count
one = P.oneCount(snap({ pools: [], totals: { healthy: 5, keys: 7, ready: 4 } }), NOW_MS)
assert.deepEqual([one.ready, one.total], [4, 7])

// -- U2: the empty line of "What KAME did" tells the truth ------------------------------------------
assert.equal(P.movesEmptyLine(snap({ replies: [], lifetime: { replies: 0 } })), 'No replies yet.')
assert.equal(
  P.movesEmptyLine(snap({ replies: [{ at: NOW, model: 'a' }], totals: { physical_keys: 14, physical_ready: 0 } })),
  'Waiting for your keys. Nothing went wrong on your side.'
)
assert.equal(
  P.movesEmptyLine(snap({ replies: [{ at: NOW, model: 'a' }] })),
  'Nothing to report: every reply went straight out.'
)
// a lifetime count alone (the session just started) is still "replies happened"
assert.equal(P.movesEmptyLine(snap({ lifetime: { replies: 40 } })), 'Nothing to report: every reply went straight out.')

// -- U3: a dot only means something when there is something ---------------------------------------
const tone = (extra) => P.toneFor(snap(extra), NOW_MS)
assert.equal(tone({ pools: [], totals: { healthy: 0, keys: 0, physical_keys: 0, physical_ready: 0, ready: 0 } }), 'muted')
assert.equal(tone({}), 'good')
assert.equal(tone({ totals: { healthy: 0, keys: 6, physical_keys: 3, physical_ready: 0, ready: 0 } }), 'bad')

// -- U6: the lifetime line and the star -------------------------------------------------------------
assert.equal(P.lifetimeLine({ replies: 0, saved: 0, since: NOW }), null)
const day = new Date(NOW * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
let line = P.lifetimeLine({ replies: 1240, saved: 96, since: NOW }, 'en-GB')
assert.equal(line.text, `Since ${day}: 1,240 replies, 96 saved from a provider error.`)
assert.equal(line.star, true, '96 saved is past ten')
line = P.lifetimeLine({ replies: 40, saved: 9, since: NOW }, 'en-GB')
assert.equal(line.star, false)
line = P.lifetimeLine({ replies: 40, saved: 10, since: NOW }, 'en-GB')
assert.equal(line.star, true)
assert.equal(line.starText, 'A star helps others find it.')
assert.equal(P.lifetimeLine({ replies: 1, saved: 0, since: NOW }, 'en-GB').text, `Since ${day}: 1 reply, 0 saved from a provider error.`)

// -- E6: the ring detail line -----------------------------------------------------------------------
assert.equal(
  P.sentLine({ daily_limit: 20, daily_scope: 'project', sent_24h: 143 }),
  "Sent in the last 24 hours: 143. The provider's own limit: 20 a day per project."
)
assert.equal(P.sentLine({ daily_limit: null, daily_scope: null, sent_24h: 7 }), 'Sent in the last 24 hours: 7.')
assert.equal(P.sentLine({ daily_limit: 50, daily_scope: 'key', sent_24h: 1 }).endsWith('50 a day per key.'), true)
assert.equal(P.sentLine({}), '', 'an older backend sends no number: no line')
assert.ok(!/%|left|remaining/i.test(P.sentLine({ daily_limit: 20, daily_scope: 'project', sent_24h: 143 })), 'no percentage and no "left" number')

// -- U5: a quick control shows its title, the buttons and one line ------------------------------------
const textOf = node => {
  if (node === null || node === undefined || node === false) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join(' ')
  if (typeof node.type === 'function' && !node.type.name?.match(/^(Button|Tip|StatusDot|Input|Switch|ConfirmDialog)$/)) {
    return textOf(node.type(node.props ?? {}))
  }
  return textOf(node.props?.children)
}
const setting = {
  advanced: false,
  default: 'adaptive',
  detail: 'long',
  env: 'KAME_HEDGE_MODE',
  help: 'HELP SENTENCE THAT EXPLAINS WHAT IT DOES',
  impact: 'high',
  key: 'hedge_mode',
  line: 'How hard KAME works to answer sooner.',
  source: 'default',
  title: 'Speed',
  value: 'adaptive',
  when: 'WHEN TO CHANGE IT'
}
const control = [{ type: 'span', props: { children: 'BUTTONS' }, key: 'c' }]
const render = (extra, shown = false) => {
  globalThis.__KAME_FORCE_STATE__ = shown ? i => (i === false ? true : i) : undefined
  const tree = P.SettingShell({ control, note: 'THE ANSWER IN FORCE', setting, ...extra })
  globalThis.__KAME_FORCE_STATE__ = undefined

  return textOf(tree)
}

let said = render({ quick: true })
assert.ok(said.includes('Speed') && said.includes('BUTTONS') && said.includes('How hard KAME works to answer sooner.'), said)
assert.ok(said.includes('Why?'), 'the toggle is there')
assert.ok(!said.includes('HELP SENTENCE') && !said.includes('WHEN TO CHANGE') && !said.includes('THE ANSWER IN FORCE'), 'closed by default')
said = render({ quick: true }, true)
assert.ok(said.includes('HELP SENTENCE') && said.includes('WHEN TO CHANGE') && said.includes('THE ANSWER IN FORCE'), 'no word is deleted, it opens')
said = render({})
assert.ok(said.includes('HELP SENTENCE') && said.includes('WHEN TO CHANGE') && said.includes('THE ANSWER IN FORCE'), 'the Settings tab keeps all of it')
assert.ok(!said.includes('Why?') && !said.includes('How hard KAME works'), 'and shows no toggle')
said = render({ quick: true, setting: { ...setting, line: '' } })
assert.ok(said.includes('HELP SENTENCE'), 'a snapshot without the line falls back to the full row')

console.log('1.8.4.0 page: ok')
