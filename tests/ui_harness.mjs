/**
 * Loads `desktop/plugin.js` without a browser: the Desktop SDK and React are stubbed down to what the file
 * touches at import time, and the file's exported helpers (`panelHelpers`) are returned. Shared by the node
 * checks added in 1.8.3.1 (`ui_live_words.mjs`, `ui_chip_popover.mjs`); the older checks carry their own copy.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
export const SOURCE = path.join(HERE, '..', 'hermes-kame-api-rotation', 'desktop', 'plugin.js')
export const FIXTURE = path.join(HERE, 'fixtures', 'live_words_1831.json')

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

export async function loadPlugin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kame-ui-'))
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

export function readFixture() {
  return JSON.parse(fs.readFileSync(FIXTURE, 'utf8'))
}
