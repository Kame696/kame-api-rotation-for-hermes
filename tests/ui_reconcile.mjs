/**
 * The panel, rendered without a browser, to prove two things about its shape.
 *
 * 1.2.3 exists because a saved number reverted to its old value on screen. The
 * cause was not in the save path — that was correct and is covered by the
 * Python suite — but in how this file hands children to React. `h` drops a
 * falsy child before React sees the list, so a list containing a conditional
 * paragraph physically grows and shrinks; React reconciles a keyless list by
 * *position*; and a card that moves from index 4 to index 5 lands where the
 * previous render had a different element, which React resolves by unmounting
 * the old subtree and mounting a new one. Every `useState` inside it dies —
 * including the one holding the number that had just been typed.
 *
 * So the invariant worth testing is not "does saving work". It is: **no
 * variadic child list in this file may be keyless**, and **the settings cards
 * must keep their identity across the render where "Saving…" appears**. Both
 * are checked here against the real file, with the SDK and React stubbed down
 * to what the file actually uses.
 *
 * Run directly (`node tests/ui_reconcile.mjs`) or through
 * `tests/test_v1_2_3.py`, which skips when there is no node on the machine.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SOURCE = path.join(HERE, '..', 'hermes-kame-api-rotation', 'desktop-ui', 'plugin.js')

// -- the fixture the panel is rendered against -------------------------------

const SNAPSHOT = {
  activity: { attempt: 1, healthy: 2, keys: 3, kind: 'calling', model: 'gemini-3.7-flash' },
  control: {},
  counters: { calls: 12, recovered: 1, rotations: 2, surfaced: 0 },
  // Two rows on purpose: one carrying a redacted payload and a sizing source
  // (1.4.0's expandable row), one without, so the keyed-list check sees the
  // conditional children actually appear and disappear.
  events: [
    { at: 1787580000, code: 429, identity: 'gemini:gemini-3.7-flash', kind: 'rotation', seq: 1,
      reason: 'rate_limit', seconds: 21, sized_by: 'retryinfo',
      detail: 'Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota exceeded' },
    { at: 1787580005, code: 503, identity: 'gemini:gemini-3.7-flash', kind: 'rotation', seq: 2 }
  ],
  first_run: false,
  gemini_tool_call_fix: { applied: true, reason: '', repaired: 0 },
  installed: true,
  build: { complete: true, fingerprint: 'abc123def456', missing: [], guidance: 'host:2' },
  pid: 4242,
  pools: [
    {
      failures: 1,
      healthy: 2,
      idle_for_s: 3,
      identity: 'gemini:gemini-3.7-flash',
      invalid: 0,
      invalid_keys: [],
      keys: 3,
      kinds: ['per_minute'],
      soonest_s: null,
      successes: 11
    }
  ],
  reason: '',
  schema: 5,
  setting_groups: [
    { id: 'extra', note: 'Off until you turn it on.', title: 'Optional' },
    { id: 'off', note: 'Escape hatches.', title: 'Turn parts of KAME off' }
  ],
  settings: [
    {
      consequential: false,
      default: 0,
      env: 'KAME_STREAM_SILENCE_TIMEOUT',
      group: 'extra',
      help: 'Wait for the first token this long.',
      key: 'stream_silence_timeout_seconds',
      kind: 'number',
      max: 3600,
      min: 0,
      off_or_at_least: 5,
      source: 'default',
      step: null,
      title: 'Wait for the first token',
      units: 'seconds',
      value: 0
    },
    {
      consequential: true,
      default: false,
      env: 'KAME_ROTATION_DISABLED',
      group: 'off',
      help: 'Every call keeps the key Hermes resolved.',
      key: 'disabled',
      kind: 'flag',
      max: null,
      min: null,
      off_or_at_least: null,
      source: 'default',
      step: null,
      title: 'Turn KAME off',
      units: '',
      value: false
    }
  ],
  settings_pending_restart: [],
  totals: { healthy: 2, keys: 3, soonest_s: null },
  updated_at: Date.now() / 1000,
  version: '1.2.3'
}

// -- the stubs ---------------------------------------------------------------

/** `atom` as the SDK provides it, reduced to what this file uses. */
function atom(initial) {
  let value = initial

  return {
    get: () => value,
    set: next => {
      value = next
    }
  }
}

/** `jsx`/`jsxs` recording the tree instead of rendering it. The third argument
 *  is the key, which is where React's runtime takes it and where `h` puts it. */
const jsx = (type, props, key) => ({ key: key === undefined ? null : key, props, type })
const jsxs = jsx

/** Hooks reduced to a single synchronous pass. Nothing here asserts on state,
 *  only on the shape of the tree, so an initial value and a no-op setter is the
 *  whole of what the render needs. */
const useState = initial => [typeof initial === 'function' ? initial() : initial, () => {}]
const useEffect = () => {}

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

/** The file under test, with its three import specifiers pointed at the stubs.
 *  Rewriting the text rather than installing a loader keeps this runnable with
 *  a bare `node` and no package.json anywhere near it. */
async function loadPlugin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kame-ui-'))
  const source = fs
    .readFileSync(SOURCE, 'utf8')
    .replace("from '@hermes/plugin-sdk'", "from './sdk.mjs'")
    .replace("from 'react/jsx-runtime'", "from './jsx.mjs'")
    .replace("from 'react'", "from './react.mjs'")

  fs.writeFileSync(path.join(dir, 'plugin.mjs'), source)
  fs.writeFileSync(
    path.join(dir, 'sdk.mjs'),
    `const S = globalThis.__KAME_SDK__\n` +
      Object.keys(SDK)
        .map(name => `export const ${name} = S.${name}`)
        .join('\n')
  )
  fs.writeFileSync(path.join(dir, 'react.mjs'), 'const S = globalThis.__KAME_REACT__\nexport const useState = S.useState\nexport const useEffect = S.useEffect\n')
  fs.writeFileSync(path.join(dir, 'jsx.mjs'), 'const S = globalThis.__KAME_JSX__\nexport const jsx = S.jsx\nexport const jsxs = S.jsxs\n')

  globalThis.__KAME_SDK__ = SDK
  globalThis.__KAME_REACT__ = { useEffect, useState }
  globalThis.__KAME_JSX__ = { jsx, jsxs }

  return import(pathToFileURL(path.join(dir, 'plugin.mjs')).href)
}

// -- the host the file expects ----------------------------------------------

let written = null
const timers = new Set()

globalThis.document = { visibilityState: 'visible' }
globalThis.window = {
  clearInterval: id => timers.delete(id),
  hermesDesktop: {
    desktopPluginsRoot: async () => 'C:/fake/hermes/desktop-plugins',
    readFileText: async () => ({ text: JSON.stringify(SNAPSHOT) }),
    writeTextFile: async (target, body) => {
      written = { body, target }
    }
  },
  setInterval: () => {
    const id = Symbol('timer')
    timers.add(id)

    return id
  }
}

// -- walking the tree --------------------------------------------------------

/** Render every function component in place, so what comes back is host nodes
 *  and the keys around them. */
function render(node) {
  if (node === null || node === undefined || typeof node !== 'object') {
    return node
  }

  if (Array.isArray(node)) {
    return node.map(render)
  }

  let current = node

  // A component that returns a component (KamePage -> SettingsPage -> ...) is
  // rendered all the way down, so one walk sees the whole page.
  while (typeof current?.type === 'function') {
    const produced = current.type(current.props ?? {})

    if (produced === null || produced === undefined) {
      return { children: [], key: current.key, type: 'null' }
    }

    current = { ...produced, key: current.key ?? produced.key }
  }

  const children = current?.props?.children

  return {
    children: Array.isArray(children) ? children.map(render) : children === undefined ? [] : [render(children)],
    key: current?.key ?? null,
    // Kept so a check can find a control and press it — the tab strip is how
    // this test reaches the Settings tree without the module exporting anything
    // it would not otherwise export.
    props: current?.props ?? {},
    type: String(current?.type ?? '?')
  }
}

/** Press the tab with this label, the way a person would. */
function openTab(pageContribution, label) {
  const button = walk(render(pageContribution.render())).find(
    node => typeof node.props?.onClick === 'function' && JSON.stringify(node.children ?? []).includes(label)
  )

  assert.ok(button, `there is a ${label} tab to open`)
  button.props.onClick()
}

/** Every node in the tree, flat. */
function walk(node, out = []) {
  if (!node || typeof node !== 'object') {
    return out
  }

  out.push(node)

  for (const child of node.children ?? []) {
    walk(child, out)
  }

  return out
}

// -- the checks --------------------------------------------------------------

const failures = []

function check(name, run) {
  try {
    run()
    console.log(`ok   ${name}`)
  } catch (error) {
    failures.push(`${name}: ${error.message}`)
    console.log(`FAIL ${name}\n     ${error.message}`)
  }
}

const plugin = await loadPlugin()

const contributions = []
plugin.default.register({
  onDispose: () => {},
  registerMany: entries => contributions.push(...entries)
})

const page = contributions.find(entry => entry.id === 'page')
const chip = contributions.find(entry => entry.id === 'chip')

assert.ok(page, 'the plugin registers a page')
assert.ok(chip, 'the plugin registers a status-bar chip')

// The reader ticks once on start; give the awaits in `readSnapshot` a turn.
await new Promise(resolve => setTimeout(resolve, 20))

check('the snapshot reached the page', () => {
  const tree = render(page.render())
  const text = JSON.stringify(tree)

  assert.ok(text.includes('KAME API Rotation'), 'the page renders its own name')
  assert.ok(!text.includes('Reading the pool'), 'the page is past its empty state')
})

check('every variadic child list is keyed, on every tab', () => {
  const seen = new Set()
  const offenders = []
  const surfaces = [chip.render(), page.render()]

  // The three tabs are three different trees, and the one that regressed in
  // 1.2.2 was the one holding the inputs — so all three are walked. Each is
  // checked for a landmark first, because a walk over a tab that never opened
  // is a check that passes by seeing nothing.
  const landmark = { Events: 'Rotated', Overview: 'Pool health', Settings: 'KAME works with none of these' }

  for (const label of ['Settings', 'Events', 'Overview']) {
    openTab(page, label)

    const tree = page.render()

    assert.ok(
      JSON.stringify(render(tree)).includes(landmark[label]),
      `the ${label} tab did not open — this check would then prove nothing about it`
    )
    surfaces.push(tree)
  }

  for (const surface of surfaces) {
    for (const node of walk(render(surface))) {
      const children = (node.children ?? []).filter(child => child && typeof child === 'object')

      if (children.length < 2) {
        continue
      }

      for (const child of children) {
        if (child.key === null || child.key === undefined) {
          const where = `<${node.type}> holds a keyless <${child.type}>`

          if (!seen.has(where)) {
            seen.add(where)
            offenders.push(where)
          }
        }
      }
    }
  }

  assert.deepEqual(
    offenders,
    [],
    'a keyless child in a list React reconciles by position — one sibling appearing or ' +
      'disappearing remounts everything after it, and any input in there loses what was typed:\n       ' +
      offenders.join('\n       ')
  )
})

check('the settings tab renders an editable number field', () => {
  openTab(page, 'Settings')

  const nodes = walk(render(page.render()))
  const inputs = nodes.filter(node => node.type === 'Input')

  assert.ok(inputs.length > 0, 'the number setting rendered an input')
  assert.ok(
    nodes.some(node => node.key === 'preamble'),
    'the settings body kept its stable slots'
  )
})

check('the fixes that made this version are still in place', () => {
  // The structural guarantee, stated once more against the raw text: the two
  // lists that gained and lost siblings are the ones that regressed, so a
  // future edit that adds an unkeyed conditional child to either is caught
  // here even if the fixture stops reaching that branch.
  const source = fs.readFileSync(SOURCE, 'utf8')

  for (const needle of ["key: 'saving'", "key: 'notice'", "key: 'preamble'", "key: 'body'", "key: 'stale'"]) {
    assert.ok(source.includes(needle), `${needle} is missing — the settings page lost a stable slot`)
  }

  assert.ok(source.includes('let lastText'), 'the snapshot dedupe is gone: the panel will re-render once a second')
  assert.ok(source.includes('let activeTimer'), 'the single-reader guard is gone')
  assert.ok(
    !/function KamePage\(\)[\s\S]{0,400}useValue\(\$now\)/.test(source),
    'KamePage subscribes to the clock again — every tab re-renders once a second'
  )
})

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed`)
  process.exit(1)
}

console.log('\nall checks passed')
