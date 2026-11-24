/**
 * 1.9.0.6 - the round "i" at the end of the tab bar opens "How to install or configure". Run directly
 * (`node tests/ui_1906_guide.mjs`) or through `tests/test_1_9_0_6_guide.py`, which skips without node.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'

import { loadPlugin, SOURCE } from './ui_harness.mjs'

const { panelHelpers: P } = await loadPlugin()

// every element's text, depth first
const texts = node => {
  if (node == null || node === false) return []
  if (typeof node === 'string' || typeof node === 'number') return [String(node)]
  if (Array.isArray(node)) return node.flatMap(texts)
  if (typeof node.type === 'function') return texts(node.type(node.props ?? {}))
  return texts(node.props?.children).concat(node.props?.title && typeof node.props.title === 'string' ? [node.props.title] : [])
}

const page = texts(P.GuidePage()).join('\n')
for (const want of [
  'How to install or configure',
  '1 · Install',
  'hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation',
  '2 · Add your keys',
  'separated by commas',
  '/kame-keys add gemini KEY1,KEY2,KEY3',
  '3 · Add more models (optional)',
  'hermes fallback add',
  'it waits until one is back and answers',
  '4 · Turn on this page and the status chip (optional)',
  '5 · Choose how it behaves (optional)',
  '/kame doctor'
]) {
  assert.ok(page.includes(want), `the guide says: ${want}`)
}
assert.ok(!/try again/i.test(page), 'the guide never tells anyone to try again')
assert.equal(P.GUIDE_STEPS.length, 5)

// the round i: an accessible button with the guide's name, at the end of the tab bar
const button = P.InfoTab({ active: false })
assert.equal(button.type, 'button')
assert.equal(button.props['aria-label'], 'How to install or configure')
assert.equal(button.props.children, 'i')
assert.ok(button.props.className.includes('rounded-full') && button.props.className.includes('ml-auto'))

const source = fs.readFileSync(SOURCE, 'utf8')
assert.ok(source.includes("h(InfoTab, { active: tab === 'guide', key: 'guide' })"), 'the i is in the tab bar')
assert.ok(/tab === 'guide'\s+\? h\(GuidePage, \{ key: 'body' \}\)/.test(source), 'the i opens the guide')
console.log('GUIDE-OK')
