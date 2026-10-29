/**
 * 1.8.3.0 (decisions/0033 D2 b) - the model selector goes back to the main model after a reply that a
 * fallback model answered.
 *
 * The desktop plugin hears the gateway's `message.complete`, waits for KAME's snapshot to say the reply
 * was answered "for" the main model, and asks the gateway to put that model back on THIS session only
 * (`config.set`, `--session`, never `--global`). Run directly (`node tests/ui_pill_restore.mjs`) or
 * through `tests/test_1_8_3_0_pill.py`, which skips when there is no node on the machine.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SOURCE = path.join(HERE, '..', 'hermes-kame-api-rotation', 'desktop', 'plugin.js')

const atom = initial => {
  let value = initial

  return { get: () => value, set: next => { value = next } }
}

const marker = name => {
  const component = () => ({ key: null, props: {}, type: name })
  Object.defineProperty(component, 'name', { value: name })

  return component
}

const SDK = {
  PALETTE_AREA: 'palette',
  ROUTES_AREA: 'routes',
  SIDEBAR_NAV_AREA: 'sidebar',
  STATUSBAR_AREAS: { right: 'statusbar.right' },
  atom,
  Button: marker('Button'),
  cn: (...parts) => parts.filter(Boolean).join(' '),
  ConfirmDialog: marker('ConfirmDialog'),
  host: { navigate: () => {} },
  Input: marker('Input'),
  StatusDot: marker('StatusDot'),
  Switch: marker('Switch'),
  Tip: marker('Tip'),
  useValue: source => source.get()
}

async function loadPlugin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kame-pill-'))
  const source = fs
    .readFileSync(SOURCE, 'utf8')
    .replaceAll("from '@hermes/plugin-sdk'", "from './sdk.mjs'")
    .replace("from 'react/jsx-runtime'", "from './jsx.mjs'")
    .replace("from 'react'", "from './react.mjs'")

  fs.writeFileSync(path.join(dir, 'plugin.mjs'), source)
  fs.writeFileSync(
    path.join(dir, 'sdk.mjs'),
    'const S = globalThis.__KAME_SDK__\n' + Object.keys(SDK).map(name => `export const ${name} = S.${name}`).join('\n')
  )
  fs.writeFileSync(path.join(dir, 'react.mjs'), 'export const useState = i => [i, () => {}]\nexport const useEffect = () => {}\n')
  fs.writeFileSync(path.join(dir, 'jsx.mjs'), 'export const jsx = (type, props, key) => ({ key: key ?? null, props, type })\nexport const jsxs = jsx\n')
  globalThis.__KAME_SDK__ = SDK
  globalThis.document = { visibilityState: 'visible' }

  return import(pathToFileURL(path.join(dir, 'plugin.mjs')).href)
}

const { panelHelpers } = await loadPlugin()
const { pillRestorePlan, watchPillRestore } = panelHelpers
assert.equal(typeof pillRestorePlan, 'function', 'the plan is exported for the checks')
assert.equal(typeof watchPillRestore, 'function')

const NOW = 1_800_000_000
const REPLY = { at: NOW - 3, first_s: 1.2, for: 'gemini-3.8-flash', main: { model: 'gemini-3.8-flash', provider: 'gemini' }, model: 'llama-x', switched: true, total_s: 4 }
const snap = (...replies) => ({ replies })
const DONE = { session_id: 's1', type: 'message.complete' }

// -- the plan ------------------------------------------------------------------------------------------
assert.deepEqual(pillRestorePlan(snap(REPLY), DONE, 0, NOW), {
  at: REPLY.at,
  session_id: 's1',
  value: 'gemini-3.8-flash --provider gemini --session'
})
assert.ok(!pillRestorePlan(snap(REPLY), DONE, 0, NOW).value.includes('--global'), 'never the profile default')
assert.equal(pillRestorePlan(snap({ ...REPLY, for: '' }), DONE, 0, NOW), null, 'the main model answered: nothing to put back')
assert.equal(pillRestorePlan(snap({ ...REPLY, main: undefined }), DONE, 0, NOW), null, 'no main model named')
assert.equal(pillRestorePlan(snap({ ...REPLY, switched: false }), DONE, 0, NOW), null, 'a sibling answered inside KAME: Hermes never moved')
assert.equal(pillRestorePlan(snap({ ...REPLY, switched: undefined }), DONE, 0, NOW), null, 'an older row: not believed')
assert.equal(pillRestorePlan(snap(), DONE, 0, NOW), null)
assert.equal(pillRestorePlan(snap(REPLY), { ...DONE, type: 'message.delta' }, 0, NOW), null, 'only at the end of a reply')
assert.equal(pillRestorePlan(snap(REPLY), { type: 'message.complete' }, 0, NOW), null, 'no session to address')
assert.equal(pillRestorePlan(snap({ ...REPLY, at: NOW - 600 }), DONE, 0, NOW), null, 'an old reply is not this one')
assert.equal(pillRestorePlan(snap(REPLY), DONE, REPLY.at, NOW), null, 'a reply is put back once')
for (const bad of [{ model: 'x y', provider: 'gemini' }, { model: '--global', provider: 'gemini' }, { model: 'm', provider: 'a b' }, { model: 'm', provider: '' }]) {
  assert.equal(pillRestorePlan(snap({ ...REPLY, main: bad }), DONE, 0, NOW), null, `refuses ${JSON.stringify(bad)}`)
}

// -- the watcher ---------------------------------------------------------------------------------------
function rig({ snapshot, active = 's1', request } = {}) {
  const timers = []
  const calls = []
  let handler = null
  const host = {
    onEvent: (type, fn) => {
      assert.equal(type, 'message.complete')
      handler = fn

      return () => { handler = null }
    },
    request: async (method, params) => {
      calls.push([method, params])

      return request ? request(method, params) : { ok: true }
    }
  }
  const off = watchPillRestore({
    activeSessionId: () => active,
    host,
    now: () => NOW,
    setTimer: (fn, ms) => timers.push({ fn, ms }),
    snapshot
  })

  return { calls, emit: event => handler?.(event), fire: async () => { for (const t of timers.splice(0)) await t.fn() }, off, timers }
}

{
  const r = rig({ snapshot: () => snap(REPLY) })
  r.emit(DONE)
  assert.equal(r.calls.length, 0, 'nothing is asked before KAME has had time to publish the reply')
  assert.ok(r.timers.length >= 1 && r.timers[0].ms >= 1000, 'it waits for the snapshot')
  await r.fire()
  assert.deepEqual(r.calls, [['config.set', { key: 'model', session_id: 's1', value: 'gemini-3.8-flash --provider gemini --session' }]])
  r.emit(DONE)
  await r.fire()
  assert.equal(r.calls.length, 1, 'the same reply is never put back twice')
  r.off()
  assert.equal(r.emit(DONE), undefined)
}

{
  // the snapshot is late: the first look finds the previous reply, a later one finds this one
  let current = snap({ ...REPLY, at: NOW - 400, for: '' })
  const r = rig({ snapshot: () => current })
  r.emit(DONE)
  await r.fire()
  assert.equal(r.calls.length, 0)
  current = snap(REPLY)
  await r.fire()
  assert.equal(r.calls.length, 1, 'a later look found it')
}

{
  const r = rig({ snapshot: () => snap(REPLY), active: 'someone-else' })
  r.emit(DONE)
  await r.fire()
  assert.equal(r.calls.length, 0, 'a session that is not the one on screen is left alone')
}

{
  const r = rig({ snapshot: () => snap(REPLY), request: async () => { throw new Error('gateway unavailable') } })
  r.emit(DONE)
  await r.fire()
  assert.equal(r.calls.length, 1, 'a refused request is not an error the page shows or retries')
}

assert.equal(typeof watchPillRestore({ host: {}, snapshot: () => null }), 'function', 'a Desktop without the doors gets a no-op')
console.log('pill restore: ok')
