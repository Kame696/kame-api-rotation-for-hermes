/** Render the real panel to a static HTML page, so it can be looked at.
 *
 * `tests/ui_reconcile.mjs` renders the same file and asks structural questions
 * of the tree — is every list keyed, does the Settings tab produce an input.
 * Those are the questions that catch remounts. They cannot answer *is this
 * readable*, and a redesign judged only by them is a redesign done blind.
 *
 * This runs the same load-with-stubs trick and then walks the tree into HTML,
 * turning the SDK's marker components into plain elements that look like what
 * Hermes draws. What comes out is not pixel-identical to the Desktop — the
 * host owns the theme and the component library — but it is the real
 * component tree with the real copy and the real class names, which is what a
 * layout decision needs.
 *
 *     node tools/panel_preview.mjs            # every tab, one page
 *     node tools/panel_preview.mjs --tab settings
 *     node tools/panel_preview.mjs --out somewhere.html
 *
 * The fixture below is deliberately the unpleasant case rather than the happy
 * one: two Hermes processes sharing a home, a provider with rejected keys, a
 * key that is not in the pool at all, and a pool mid-cooldown. A panel that
 * only looks good empty is a panel nobody has designed.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SOURCE = path.join(HERE, '..', 'hermes-kame-api-rotation', 'desktop-ui', 'plugin.js')

const args = process.argv.slice(2)
const argOf = name => {
  const at = args.indexOf(name)

  return at === -1 ? null : args[at + 1]
}

const OUT = argOf('--out') ?? path.join(os.tmpdir(), 'kame-panel-preview.html')
const ONLY = argOf('--tab')

// -- the fixture -------------------------------------------------------------

const NOW = Math.floor(Date.now() / 1000)

const SECTION = {
  activity: { attempt: 2, healthy: 12, keys: 17, kind: 'calling', model: 'gemini-3.6-flash' },
  build: { complete: true, fingerprint: 'bfe8506235e6', guidance: 'host:2', missing: [] },
  control: {},
  counters: {
    blamed_another_key: 0,
    calls: 214,
    mid_stream_cuts: 1,
    recovered: 7,
    resumes: 4,
    rotations: 9,
    stitched: 2,
    stream_drops: 3,
    surfaced: 0,
    tool_call_cuts: 1,
    tool_call_retries: 2,
    waited_s: 96,
    waits: 3
  },
  credentials: {
    guarding_container: true,
    providers: [
      { benched: 0, keys: 14, origin: 'env', provider: 'gemini', rows: 1, split: true },
      { benched: 2, keys: 3, origin: 'env', provider: 'nvidia', rows: 1, split: true }
    ],
    readable: true,
    reason: '',
    splitting: true
  },
  // One whole incident, in the order it happened, so the tab is looked at with
  // both halves of a story in it rather than with a list of failures. 1.6.0.1
  // is the release that made the `switch`, `recovery` and `wait` rows real.
  events: [
    { at: NOW - 4, identity: 'nvidia:moonshotai/kimi-k3', key: 'key:9fefb2', kind: 'recovery',
      reason: 'answered on attempt 3', seconds: 6.2, seq: 22 },
    { at: NOW - 8, identity: 'nvidia:moonshotai/kimi-k3', key: 'key:9fefb2', kind: 'switch',
      reason: 'attempt 3 — this key took over', seq: 21 },
    { at: NOW - 12, code: 403, detail: 'HTTP 403: rate limit exceeded for this key',
      identity: 'nvidia:moonshotai/kimi-k3', key: 'key:00082b', kind: 'rotation', reason: 'per_minute',
      seconds: 40, seq: 20, sized_by: 'catalog' },
    { at: NOW - 20, identity: 'nvidia:moonshotai/kimi-k3', key: 'key:00082b', kind: 'switch',
      reason: 'attempt 2 — this key took over', seq: 19 },
    { at: NOW - 48, code: 401, detail: 'HTTP 401: invalid api key',
      identity: 'nvidia:moonshotai/kimi-k3', key: 'key:b65dd9', kind: 'quarantine', reason: 'auth',
      seconds: 300, seq: 18, sized_by: 'policy' },
    { at: NOW - 180, identity: 'gemini:gemini-3.6-flash', kind: 'wait',
      reason: 'every key resting — 0 of 14 usable', seconds: 21, seq: 17 },
    { at: NOW - 300, identity: 'gemini:gemini-3.6-flash', key: 'key:9fefb2', kind: 'recovery',
      reason: 'answered on attempt 2', seconds: 23.4, seq: 16 },
    { at: NOW - 900, code: 429, identity: 'gemini:gemini-3.6-flash', key: 'key:10db63', kind: 'rotation',
      reason: 'rate_limit', seconds: 21, seq: 15, sized_by: 'retryinfo' }
  ],
  first_run: false,
  gemini_tool_call_fix: { applied: true, reason: '', repaired: 2 },
  installed: true,
  pid: 9164,
  pools: [
    { failures: 11, healthy: 14, identity: 'gemini:gemini-3.6-flash', idle_for_s: 4, invalid: 0,
      invalid_keys: [], keys: 14, kinds: ['server', 'per_minute'], outside_pool: [], resting: 0,
      soonest_s: null, successes: 132 },
    // One key the provider named dead (out of rotation) and one that has just
    // been refused and is still being tried — the two states 1.6.0.1 tells
    // apart, so the card is looked at with both sentences on it.
    { failures: 5, healthy: 3, identity: 'nvidia:moonshotai/kimi-k3', idle_for_s: 40, invalid: 2,
      invalid_keys: ['key:02ba29', 'key:b65dd9'], keys: 3, kinds: ['revoked', 'per_minute'],
      outside_pool: ['key:00082b'], resting: 0, retired: 1, retired_keys: ['key:b65dd9'],
      soonest_s: 38, successes: 0 }
  ],
  profile: 'default',
  reason: '',
  role: 'desktop',
  schema: 6,
  setting_groups: [
    { id: 'extra', note: 'Off until you turn it on.', title: 'Optional' },
    { id: 'tuning', note: 'Already right for the providers this was built against.', title: 'Tuning' },
    { id: 'off', note: 'Escape hatches, not preferences.', title: 'Turn parts of KAME off' }
  ],
  settings: [
    { consequential: false, default: false, env: 'KAME_NO_MODEL_FALLBACK', group: 'extra',
      help: 'Hermes answers a spent credential by rotating keys and, failing that, by switching to a different model. Turn this on and KAME tells it not to.',
      key: 'never_fall_back_to_another_model', kind: 'flag', max: null, min: null,
      off_or_at_least: null, source: 'default', step: null, title: 'Stay on the model you asked for',
      units: '', value: false },
    { consequential: false, default: 0, env: 'KAME_STREAM_SILENCE_TIMEOUT', group: 'extra',
      help: 'KAME waits this long for the first character of an answer, and that long again for every character after it.',
      key: 'stream_silence_timeout_seconds', kind: 'number', max: 3600, min: 0, off_or_at_least: 5,
      source: 'environment', step: null, title: 'Wait for the first token', units: 'seconds', value: 45 },
    { consequential: false, default: 3600, env: 'KAME_DAILY_QUOTA_COOLDOWN', group: 'tuning',
      help: 'How long a key rests after a daily or account-level refusal.',
      key: 'daily_quota_cooldown_seconds', kind: 'number', max: 86400, min: 60, off_or_at_least: null,
      source: 'config', step: null, title: 'Daily quota cooldown', units: 'seconds', value: 1800 },
    { consequential: false, default: 10, env: 'KAME_STREAM_RESUME_LIMIT', group: 'tuning',
      help: 'A ceiling, not the rule. How many times one request may continue an answer the provider cut off.',
      key: 'stream_resume_limit', kind: 'number', max: 10, min: 0, off_or_at_least: null,
      source: 'default', step: null, title: 'Continue a cut answer at most', units: 'times', value: 10 },
    { consequential: true, default: false, env: 'KAME_ROTATION_DISABLED', group: 'off',
      help: 'Every call keeps the key Hermes resolved. Nothing rotates.',
      key: 'disabled', kind: 'flag', max: null, min: null, off_or_at_least: null,
      source: 'default', step: null, title: 'Turn KAME off', units: '', value: false },
    { consequential: true, default: false, env: 'KAME_SPREAD_DISABLED', group: 'off',
      help: 'Stop spreading requests across healthy keys.',
      key: 'spread_disabled', kind: 'flag', max: null, min: null, off_or_at_least: null,
      source: 'default', step: null, title: 'Turn spreading off', units: '', value: false }
  ],
  settings_pending_restart: [],
  totals: { healthy: 17, keys: 17, ready: 15, rejected: 2, retired: 1, resting: 0, soonest_s: 38 },
  updated_at: Date.now() / 1000,
  version: '1.6.0.1'
}

const DOCUMENT = {
  processes: {
    9164: SECTION,
    18044: {
      build: { complete: true, fingerprint: '485bc4a5cf49', guidance: 'host:2', missing: [] },
      installed: true,
      pid: 18044,
      profile: '',
      reason: '',
      role: 'gateway',
      totals: { healthy: 17, keys: 17, ready: 15, rejected: 2, resting: 0, soonest_s: null },
      updated_at: Date.now() / 1000 - 6,
      version: '1.5.0'
    }
  },
  schema: 6,
  updated_at: Date.now() / 1000
}

// -- the stubs the file expects ----------------------------------------------

function atom(initial) {
  let value = initial
  const listeners = new Set()

  return {
    get: () => value,
    set: next => {
      value = next
      for (const listener of listeners) {
        listener(value)
      }
    },
    subscribe: listener => {
      listeners.add(listener)

      return () => listeners.delete(listener)
    }
  }
}

const jsx = (type, props, key) => ({ key: key === undefined ? null : key, props, type })
const jsxs = jsx
const useState = initial => [typeof initial === 'function' ? initial() : initial, () => {}]
const useEffect = () => {}

/** The host's own components, as the plain elements they draw.
 *
 *  Approximations on purpose: Hermes owns the component library and its exact
 *  rendering, and copying it here would be a second implementation to keep in
 *  step. What matters for a layout decision is the size and weight of the
 *  thing in the flow, which these have. */
const HOST = {
  Button: props =>
    jsx('button', {
      className:
        'inline-flex h-8 items-center rounded-md border border-(--ui-stroke-secondary) ' +
        'bg-(--ui-bg-quinary) px-3 text-xs text-(--ui-text-primary) disabled:opacity-40',
      ...props
    }),
  ConfirmDialog: () => null,
  Input: props =>
    jsx('input', {
      className:
        'h-8 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-editor) px-2 ' +
        'text-xs text-(--ui-text-primary)',
      ...props
    }),
  StatusDot: props =>
    jsx('span', {
      className: 'inline-block h-2 w-2 rounded-full',
      style: {
        background:
          props?.tone === 'bad'
            ? 'var(--ui-red)'
            : props?.tone === 'warn'
              ? 'var(--ui-amber)'
              : 'var(--ui-green)'
      }
    }),
  Switch: props =>
    jsx('span', {
      className:
        'inline-flex h-5 w-9 items-center rounded-full border border-(--ui-stroke-secondary) px-0.5',
      style: { background: props?.checked ? 'var(--ui-accent)' : 'var(--ui-bg-quinary)' },
      children: jsx('span', {
        className: 'h-4 w-4 rounded-full bg-white',
        style: { marginLeft: props?.checked ? '1rem' : '0' }
      })
    }),
  // Renders its child rather than nothing. The host's Tip is a hover wrapper,
  // and a stub that dropped the child made every button inside one invisible
  // in the preview — which is how the 1.6.0.1 Settings toolbar came to be
  // looked at, twice, without noticing it had no buttons on it.
  Tip: props => jsx('span', { children: props?.children ?? null, className: 'inline-flex' })
}

const SDK = {
  PALETTE_AREA: 'palette',
  ROUTES_AREA: 'routes',
  SIDEBAR_NAV_AREA: 'sidebar',
  STATUSBAR_AREAS: { right: 'statusbar.right' },
  atom,
  Button: HOST.Button,
  cn: (...parts) => parts.filter(Boolean).join(' '),
  ConfirmDialog: HOST.ConfirmDialog,
  host: { navigate: () => {} },
  Input: HOST.Input,
  StatusDot: HOST.StatusDot,
  Switch: HOST.Switch,
  Tip: HOST.Tip,
  useValue: source => source.get()
}

let poll = null

globalThis.document = { visibilityState: 'visible' }
globalThis.window = {
  clearInterval: () => {},
  hermesDesktop: {
    desktopPluginsRoot: async () => 'C:/fake/hermes/desktop-plugins',
    readFileText: async () => ({ text: JSON.stringify(DOCUMENT) }),
    writeTextFile: async () => {}
  },
  setInterval: fn => {
    poll = fn

    return Symbol('timer')
  }
}

async function loadPlugin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kame-preview-'))
  const source = fs
    .readFileSync(SOURCE, 'utf8')
    .replace("from '@hermes/plugin-sdk'", "from './sdk.mjs'")
    .replace("from 'react/jsx-runtime'", "from './jsx.mjs'")
    .replace("from 'react'", "from './react.mjs'")

  fs.writeFileSync(path.join(dir, 'plugin.mjs'), source)
  fs.writeFileSync(
    path.join(dir, 'sdk.mjs'),
    'const S = globalThis.__KAME_SDK__\n' +
      Object.keys(SDK)
        .map(name => `export const ${name} = S.${name}`)
        .join('\n')
  )
  fs.writeFileSync(
    path.join(dir, 'react.mjs'),
    'const S = globalThis.__KAME_REACT__\nexport const useState = S.useState\nexport const useEffect = S.useEffect\n'
  )
  fs.writeFileSync(
    path.join(dir, 'jsx.mjs'),
    'const S = globalThis.__KAME_JSX__\nexport const jsx = S.jsx\nexport const jsxs = S.jsxs\n'
  )

  globalThis.__KAME_SDK__ = SDK
  globalThis.__KAME_REACT__ = { useEffect, useState }
  globalThis.__KAME_JSX__ = { jsx, jsxs }

  return import(pathToFileURL(path.join(dir, 'plugin.mjs')).href)
}

// -- tree to HTML ------------------------------------------------------------

const VOID = new Set(['br', 'hr', 'img', 'input'])

const escape = text =>
  String(text).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c])

const styleOf = style =>
  Object.entries(style ?? {})
    .map(([name, value]) => `${name.replace(/[A-Z]/g, m => '-' + m.toLowerCase())}:${value}`)
    .join(';')

function html(node) {
  if (node === null || node === undefined || node === false || node === true) {
    return ''
  }

  if (typeof node !== 'object') {
    return escape(node)
  }

  if (Array.isArray(node)) {
    return node.map(html).join('')
  }

  if (typeof node.type === 'function') {
    return html(node.type(node.props ?? {}))
  }

  const { children, className, style, ...rest } = node.props ?? {}
  const attrs = [
    className ? ` class="${escape(className)}"` : '',
    style ? ` style="${escape(styleOf(style))}"` : '',
    rest.value !== undefined ? ` value="${escape(rest.value)}"` : '',
    rest.disabled ? ' disabled' : '',
    rest.title ? ` title="${escape(rest.title)}"` : ''
  ].join('')

  if (typeof node.type !== 'string') {
    return html(children)
  }

  if (VOID.has(node.type)) {
    return `<${node.type}${attrs}>`
  }

  return `<${node.type}${attrs}>${html(children)}</${node.type}>`
}

// -- the page ----------------------------------------------------------------

const THEME = `
:root {
  color-scheme: dark;
  --ui-bg-primary: #17130f;
  --ui-bg-secondary: #1c1713;
  --ui-bg-tertiary: #221c17;
  --ui-bg-quinary: #2b241d;
  --ui-bg-elevated: #241d18;
  --ui-bg-editor: #14100d;
  --ui-stroke-secondary: #3a3128;
  --ui-stroke-tertiary: #2b241d;
  --ui-text-primary: #f3ece4;
  --ui-text-secondary: #cbc0b3;
  --ui-text-tertiary: #9d9184;
  --ui-text-quaternary: #6f655a;
  --ui-accent: #d97706;
  --ui-red: #ef4444;
  --ui-amber: #f59e0b;
  --ui-green: #22c55e;
}
body { background: var(--ui-bg-primary); color: var(--ui-text-primary); margin: 0; }
.preview-shell { max-width: 1120px; margin: 0 auto; padding: 24px; }
.preview-label {
  font: 600 11px/1 ui-monospace, monospace; letter-spacing: .12em; text-transform: uppercase;
  color: var(--ui-text-quaternary); margin: 32px 0 8px;
}
.destructive, .text-destructive { color: var(--ui-red); }
.bg-primary { background: var(--ui-green); }
.bg-destructive { background: var(--ui-red); }
.bg-amber-500 { background: var(--ui-amber); }
`

async function main() {
  const plugin = await loadPlugin()
  const contributions = []
  plugin.default.register({ onDispose: () => {}, registerMany: entries => contributions.push(...entries) })

  const page = contributions.find(entry => entry.id === 'page')
  const chip = contributions.find(entry => entry.id === 'chip')

  // The reader kicks off at register and finishes on a later turn; give it
  // one, then drive the poll directly so the fixture is definitely in.
  await new Promise(resolve => setTimeout(resolve, 20))

  if (poll) {
    await poll()
  }

  /** Click the tab the way a person does.
   *
   *  The open tab lives in a module-scope atom the file does not export, so it
   *  cannot be set from outside. What can be done from outside is what a user
   *  does: render the page, find the button with that label, and call its
   *  handler. `useState` is stubbed, but the tab is an atom and atoms here are
   *  real, so the click takes. */
  const openTab = id => {
    const buttons = []
    const walk = node => {
      if (!node || typeof node !== 'object') {
        return
      }

      if (Array.isArray(node)) {
        node.forEach(walk)

        return
      }

      if (typeof node.type === 'function') {
        walk(node.type(node.props ?? {}))

        return
      }

      if (node.props?.onClick) {
        buttons.push(node.props)
      }

      walk(node.props?.children)
    }

    walk(page.render())

    const found = buttons.find(
      props => String(props.children ?? '').toLowerCase().startsWith(id)
    )

    if (!found) {
      console.log(`  (no tab button for ${id}; rendering whatever is open)`)

      return
    }

    found.onClick()
  }

  const tabs = ONLY ? [ONLY] : ['overview', 'settings', 'events']
  const blocks = []

  for (const tab of tabs) {
    // The panel keeps the open tab in an atom the page owns; the stubbed
    // `useState` cannot switch it, so the tab is chosen the way the file
    // itself does — through the exported atom when there is one, otherwise by
    // rendering whatever is open.
    openTab(tab)
    blocks.push(`<div class="preview-label">${escape(tab)}</div>`, html(page.render()))
  }

  blocks.push('<div class="preview-label">status bar chip</div>', html(chip.render()))

  const out = `<!doctype html>
<meta charset="utf-8">
<title>KAME panel preview</title>
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
<style>${THEME}</style>
<div class="preview-shell">${blocks.join('\n')}</div>
`
  fs.writeFileSync(OUT, out)
  console.log(OUT)
}

await main()
