/**
 * 1.9.0.3 - the words of the page: C1 (a failure row can always be opened), C3 (the Speed line), C4 ("API
 * Rotation" on screen), C5 ("slow to start" / "overloaded"). Run directly (`node tests/ui_1903.mjs`) or through
 * `tests/test_1_9_0_3_panel_words.py`, which skips without node.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'

import { loadPlugin, SOURCE } from './ui_harness.mjs'

const { panelHelpers } = await loadPlugin()
const P = panelHelpers
const NOW_S = 1_800_000_000
const NOW_MS = NOW_S * 1000

// -- C1: every bad or warn row opens, a good row does not -----------------------------------------------
assert.equal(P.canInspectEvent({ kind: 'surfaced' }), true, 'a surfaced row with no text still opens')
assert.equal(P.canInspectEvent({ kind: 'surfaced', detail: '404 page not found' }), true)
assert.equal(P.canInspectEvent({ kind: 'handover' }), true, 'warn rows open')
assert.equal(P.canInspectEvent({ kind: 'quarantine' }), true)
assert.equal(P.canInspectEvent({ kind: 'answered', detail: 'x' }), false, 'a good row stays closed')
assert.equal(P.canInspectEvent({ kind: 'sent' }), false, 'a plain row opens only with a text')
assert.equal(P.canInspectEvent({ kind: 'sent', detail: 'x' }), true)

assert.equal(P.inspectorText({ detail: '404 page not found', error_type: 'NotFoundError' }), '404 page not found')
assert.equal(
  P.inspectorText({ kind: 'surfaced', code: 404, error_type: 'NotFoundError' }),
  'The provider sent no text with this error.\nError type: NotFoundError\nHTTP status: 404'
)
assert.equal(P.inspectorText({ kind: 'surfaced' }), 'The provider sent no text with this error.')

// the reason words
assert.equal(
  P.reasonText({ kind: 'surfaced', reason: 'request', code: 404 }),
  'the provider refused the request itself, not a key - often a model name or address it does not know'
)
assert.equal(
  P.reasonText({ kind: 'surfaced', reason: 'other', code: 500 }),
  'an error API Rotation does not recognise - click to read it'
)

// -- C3: the Speed line ---------------------------------------------------------------------------------
const owners = {
  cap_pct: 10,
  hour: { replies: 8, backup: 0, race: 1, burst: 26, rescue: 0, rescue_won: 0, extra_pct: 337.5, limited: 0, limited_pct: 0.0 },
  since_start: { replies: 80, extra_pct: 1125.0, limited: 0, limited_pct: 0.0 }
}
let cost = P.speedCostParts(owners)
assert.equal(cost.main, 'Backup requests this hour: +0% (8 replies). Speed allows about +10%.')
assert.equal(cost.over, false, 'the owner\'s numbers are not orange')
assert.ok(cost.notLimited.includes('26 asked together while keys were running dry'), cost.notLimited)
assert.ok(cost.notLimited.includes('1 asked of another model of your list'), cost.notLimited)
assert.ok(!cost.notLimited.includes('re-asked'), 'zero parts are left out')
assert.equal(cost.rescue, '')
assert.equal(cost.since, 'Since Hermes started: +0% backup requests.')

cost = P.speedCostParts({
  cap_pct: 10,
  hour: { replies: 10, backup: 3, race: 0, burst: 0, rescue: 0, extra_pct: 30.0, limited: 3, limited_pct: 30.0 },
  since_start: { replies: 10, limited_pct: 30.0 }
})
assert.equal(cost.over, true, 'a backup rate over the cap (30 > 15) is orange')
assert.equal(cost.notLimited, '', 'the sentence is left out when all are zero')
assert.equal(cost.rescueWon, '')

cost = P.speedCostParts({ cap_pct: 10, hour: { replies: 10, limited_pct: 12.0, rescue: 2, rescue_won: 1, race: 3 }, since_start: {} })
assert.equal(cost.over, false, '12 is not over 15')
assert.ok(cost.notLimited.includes('3 asked of other models of your list'), cost.notLimited)
assert.ok(!cost.notLimited.includes('re-asked'), 'rescues are not "not limited by Speed"')
assert.equal(cost.rescue, '2 re-asked replies that stopped mid-thought.')
assert.equal(cost.rescueWon, '1 of the re-asks answered first.')
cost = P.speedCostParts({ cap_pct: 10, hour: { replies: 10, rescue: 1, limited_pct: 0 }, since_start: {} })
assert.equal(cost.notLimited, '')
assert.equal(cost.rescue, '1 re-asked a reply that stopped mid-thought.')
assert.equal(cost.rescueWon, '', 'no win, no sentence')

cost = P.speedCostParts({ cap_pct: 10, hour: { replies: 0, limited_pct: null }, since_start: { replies: 0, limited_pct: null } })
assert.equal(cost.main, 'No replies this hour yet. Speed allows about +10% backup requests.')
assert.equal(cost.since, '')
assert.equal(P.speedCostParts(null), null)

// -- C4: "API Rotation" on screen -----------------------------------------------------------------------
const view = (snap) => P.chipPopoverModel(snap, NOW_MS)
assert.equal(view(null).version, 'API Rotation')
assert.equal(view({ installed: false, reason: 'no hermes', updated_at: NOW_S, version: '1.9.0.3' }).version, 'API Rotation 1.9.0.3')
for (const text of [view(null).sentence, view({ installed: false, reason: 'r', updated_at: NOW_S }).sentence]) {
  assert.ok(!text.includes('KAME'), text)
}

// no visible string of the page names the product KAME: the only places are the title constant, the author,
// the KAME_ environment names, and the "Made by" credit
const allowed = [
  /^const PRODUCT = /, /^const AUTHOR = /, /KAME_/, /\$\{AUTHOR\}/, /Made by KAME/, /KAME on GitHub/
]
const offenders = fs
  .readFileSync(SOURCE, 'utf8')
  .split('\n')
  .map((line, index) => [index + 1, line])
  .filter(([, line]) => line.includes('KAME'))
  .filter(([, line]) => !/^\s*(\/\/|\*|\/\*)/.test(line))
  .filter(([, line]) => !allowed.some(pattern => pattern.test(line)))
assert.deepEqual(offenders, [], 'on-screen text says API Rotation, not KAME')

// -- C5: "slow to start" and "overloaded" -------------------------------------------------------------------
const pool = { identity: 'gemini:m', key_states: [['k0', 'r', null]], keys: 1 }
const slow = { entry: { busy_kind: 'slow', busy_until: NOW_S + 600 }, pool }
const busy = { entry: { busy_kind: 'busy', busy_until: NOW_S + 90, probe_at: NOW_S + 30 }, pool }
assert.equal(P.modelState(slow, {}, NOW_MS).word, 'slow to start (no first word in time) — skipped for 10m 00s')
assert.equal(P.modelState(busy, {}, NOW_MS).word, 'overloaded (the provider said so) — asked again in 30s')
assert.equal(
  P.modelState({ entry: { busy_kind: 'busy', busy_until: NOW_S + 90 }, pool }, {}, NOW_MS).word,
  'overloaded (the provider said so) — asked again soon'
)
assert.equal(
  P.modelStateTitle(slow, NOW_MS),
  'This model did not say its first word within the wait limit, so the next replies start on another model of your list ' +
    'for a while. It is asked again after 10m 00s.'
)
assert.equal(
  P.modelStateTitle(busy, NOW_MS),
  "The provider answered 'overloaded' or timed out on several keys. Each key rests 1 s; the model is skipped for a while."
)
assert.equal(P.modelStateTitle({ entry: {}, pool }, NOW_MS), undefined)
for (const word of ['slow today', 'busy —']) {
  assert.ok(!P.modelState(slow, {}, NOW_MS).word.includes(word) && !P.modelState(busy, {}, NOW_MS).word.includes(word))
}

console.log('1.9.0.3 page: ok')
