/**
 * KAME API Rotation — the Desktop half.
 *
 * The Python plugin knows everything worth knowing about the pool and could,
 * until 1.1.0, say none of it. A plugin slash command returns a string
 * (`hermes_cli/plugins.py`), and Desktop paints that string with
 * `pretty={false}` — so `/kame` is text, and text is the wrong shape for
 * "is my pool healthy right now". This file is the right shape: a status-bar
 * chip that is on screen at all times, and a real page at `/kame` with a row
 * in the sidebar, both built out of the app's own components so they look
 * like Hermes rather than like a plugin's idea of Hermes.
 *
 * HOW THE DATA GETS HERE. The Python half writes a small JSON document to
 * `<hermes home>/plugin-data/hermes-kame-api-rotation/state.json` (see
 * `state.py`) — fingerprints and counts, never key material. This file reads
 * it through the desktop bridge (`window.hermesDesktop.readFileText`) on a
 * one-second tick. No backend route, nothing to enable, nothing to restart:
 * the alternative, `plugin_api.py` behind `dashboard/plugin.json` and an
 * `plugins.enabled` entry, is the door for a plugin built around a dashboard,
 * not for one the user already has running.
 *
 * HOW A SWITCH GETS BACK (1.1.1). The same way, mirrored. This page writes
 * `control.json` next to the snapshot (`hermesDesktop.writeTextFile`) and the
 * Python half applies it on its own heartbeat, then reports the outcome in the
 * next snapshot — see `control.py`. The page never edits configuration itself:
 * it asks, and the process that owns the settings decides. That is why every
 * value here is validated twice, once for a readable message before the write
 * and once for real on the other side.
 *
 * WHY THE COUNTDOWN IS COMPUTED HERE. The snapshot carries `soonest_s` at
 * `updated_at`, not a wall-clock deadline. The Python side publishes when
 * something happens and on a slow heartbeat, so between publishes the number
 * on disk is stale by exactly the snapshot's age — which is why every ETA on
 * screen is `soonest_s - (now - updated_at)`, clamped at zero. A countdown
 * that freezes for twenty seconds and then jumps is the same freeze the whole
 * feature exists to remove.
 *
 * WHERE IT LIVES ON DISK. `<hermes home>/desktop-plugins/hermes-kame-api-rotation/plugin.js`
 * — the standalone runtime door, which loads default-on. The unified door
 * (`plugins/<name>/desktop/plugin.js`) is deliberately NOT used: it ships
 * `defaultEnabled: false` to match the Python half's installed-but-inert
 * posture, so the chip would be invisible until someone found the toggle,
 * which is the exact problem 1.1.0 was fixing.
 *
 * This file is plain ESM. It may import `@hermes/plugin-sdk` and `react` and
 * nothing else — the loader rewrites those two specifiers and rejects the
 * rest.
 */

import {
  atom,
  Button,
  cn,
  ConfirmDialog,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Switch,
  Tip,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'hermes-kame-api-rotation'
const ROUTE = '/kame'

/** The name in the sidebar, the page heading and the chip's tooltip. One
 *  constant so the three can never drift apart. */
const PRODUCT = 'KAME API Rotation'

/** Schema of `state.json` this UI understands. A document from a newer Python
 *  half is refused with a readable reason rather than half-rendered. */
const SCHEMA = 4

/** Schema of the `control.json` this UI writes. Read by `control.py`, which
 *  refuses a number it does not know rather than guessing. */
const CONTROL_SCHEMA = 1

/** How often the snapshot is re-read, and how often the countdowns move.
 *  One second because that is the resolution of a countdown a person reads;
 *  the read is a few hundred bytes off a local disk. */
const TICK_MS = 1000

/** Older than this and the reading is presented as stale rather than as
 *  truth. The Python half republishes at least every 20s, so a minute of
 *  silence means the process is gone, wedged, or was never there. */
const STALE_AFTER_S = 75

/** How long a request may go unanswered before the page says so. The backend
 *  looks for one every second, so five is already six chances missed. */
const CONTROL_TIMEOUT_MS = 5000

/** How long a control that has been written keeps its value on screen while
 *  the snapshot catches up. Longer than `CONTROL_TIMEOUT_MS`, on purpose: the
 *  backend acknowledges a request in one document and may publish the new
 *  settings in the next, and a field that let go on the acknowledgement would
 *  show the old number for exactly one frame — which is the flicker this
 *  exists to end. If the value never arrives, the control gives up and shows
 *  the truth, because a page that keeps displaying a setting nothing holds is
 *  worse than one that flickers. */
const SETTLE_GRACE_MS = 8000

/** How many pools the chip shows in full before the rest collapse into `+N`.
 *  Two, because the status bar is shared with every other contribution in the
 *  app and a third pool is exactly where a chip starts pushing its neighbours
 *  off the end of the row. */
const CHIP_POOLS = 2

/** A pool untouched for longer than this is not what the user is doing now,
 *  so it stays off the chip and waits on the page. Ten minutes is long enough
 *  to cover a pause for thought and short enough to drop yesterday's model. */
const CHIP_IDLE_AFTER_S = 600

// -- state ------------------------------------------------------------------

/** The parsed document, or null when there is nothing to show. */
const $snapshot = atom(null)

/** Why there is nothing to show. Empty while the snapshot is readable. */
const $problem = atom('')

/** A one-second clock, so countdowns move between snapshot reads. */
const $now = atom(Date.now())

/** The request waiting for the backend to pick it up, or null. */
const $pending = atom(null)

/** What became of the last request: `{ ok, detail }`, or null. */
const $notice = atom(null)

/** Which page of the panel is showing. */
const $tab = atom('overview')

// -- helpers ----------------------------------------------------------------

/**
 * `jsx`/`jsxs` with a children-shaped call signature.
 *
 * Hand-written runtime JSX is easy to get subtly wrong — `jsx` for one child,
 * `jsxs` for several, and `key` as the third argument rather than a prop.
 * Wrapping it once means the rest of this file reads like markup.
 */
function h(type, props, ...rest) {
  const children = rest.flat().filter(child => child !== null && child !== undefined && child !== false)
  const { key, ...attrs } = props ?? {}

  if (children.length === 0) {
    return jsx(type, attrs, key)
  }

  if (children.length === 1) {
    return jsx(type, { ...attrs, children: children[0] }, key)
  }

  return jsxs(type, { ...attrs, children }, key)
}

/** `1h 02m`, `4m 12s`, `38s`. Matches the Python side's `format_duration`, so
 *  the chip and `/kame` never disagree about the same number. */
function duration(seconds) {
  const total = Math.max(0, Math.round(seconds ?? 0))

  if (total < 60) {
    return `${total}s`
  }

  const minutes = Math.floor(total / 60)

  if (minutes < 60) {
    return `${minutes}m ${String(total % 60).padStart(2, '0')}s`
  }

  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`
}

/** Seconds since the snapshot was written, from the app's own clock. */
function ageSeconds(snap, now) {
  if (!snap?.updated_at) {
    return Number.POSITIVE_INFINITY
  }

  return Math.max(0, now / 1000 - snap.updated_at)
}

/**
 * The live countdown to the first key coming back, or null when a key is
 * usable now.
 *
 * `value` is what the Python half measured at `updated_at`; the age is
 * subtracted here so the number on screen is the number right now.
 */
function countdown(value, snap, now) {
  if (value === null || value === undefined) {
    return null
  }

  return Math.max(0, value - ageSeconds(snap, now))
}

/** How many keys the whole install has had refused as credentials. */
function invalidCount(snap) {
  return (snap?.pools ?? []).reduce((total, pool) => total + (pool.invalid ?? 0), 0)
}

function toneFor(snap, now) {
  if (!snap || ageSeconds(snap, now) > STALE_AFTER_S) {
    return 'muted'
  }

  if (!snap.installed) {
    return 'muted'
  }

  const totals = snap.totals ?? {}

  if (!totals.keys) {
    return 'muted'
  }

  if (!totals.healthy) {
    return 'bad'
  }

  // An invalid key is not a resting key: no amount of waiting repairs it, so
  // it colours the chip even when the pool is otherwise serving.
  return totals.healthy < totals.keys || invalidCount(snap) ? 'warn' : 'good'
}

/**
 * The pools worth putting on a status bar, most relevant first.
 *
 * The pool being called right now leads, because that is the one the user is
 * waiting on. Everything else follows by how recently it was used, and a pool
 * nothing has touched in ten minutes is left off entirely — a chip showing
 * yesterday's model beside today's is a chip nobody can read at a glance.
 */
function chipPools(snap) {
  const pools = (snap?.pools ?? []).filter(pool => pool.keys > 0 && pool.idle_for_s !== null && pool.idle_for_s !== undefined)
  const current = snap?.activity?.identity ?? ''

  return pools
    .filter(pool => pool.identity === current || pool.idle_for_s <= CHIP_IDLE_AFTER_S)
    .sort((left, right) => {
      if (left.identity === current) {
        return -1
      }

      if (right.identity === current) {
        return 1
      }

      return (left.idle_for_s ?? 0) - (right.idle_for_s ?? 0)
    })
}

/** `default` / `environment` / `config`, said the way a person would. */
function sourceLabel(source) {
  if (source === 'environment') {
    return 'set in the environment'
  }

  if (source === 'config') {
    return 'set in config.yaml'
  }

  return 'default'
}

/**
 * The same rules `settings.parse` applies, so a bad number is refused before
 * it is written rather than three seconds later in a status line.
 *
 * Deliberately a copy and not a guess: every bound here arrives in the
 * snapshot from the Python side (`settings.describe`), so this validates
 * against what that release actually allows rather than against what this file
 * was written believing.
 */
function validateNumber(setting, raw) {
  const text = String(raw ?? '').trim()

  if (!text) {
    return `Enter a number of ${setting.units || 'seconds'}.`
  }

  const value = Number(text)

  if (!Number.isFinite(value)) {
    return `${setting.title} takes a number of ${setting.units || 'seconds'}; "${text}" is not one.`
  }

  if (setting.min !== null && setting.max !== null && (value < setting.min || value > setting.max)) {
    return `Accepts ${setting.min} to ${setting.max}.`
  }

  if (setting.step === 1 && !Number.isInteger(value)) {
    return 'This one counts whole things, so it takes a whole number.'
  }

  if (setting.off_or_at_least && value > 0 && value < setting.off_or_at_least) {
    return `Either 0 (off) or at least ${setting.off_or_at_least} — anything shorter fires while a healthy provider is still connecting.`
  }

  return ''
}

// -- the reader -------------------------------------------------------------

/**
 * Where the Python half writes, and where this page writes back.
 *
 * Derived from the bridge's own plugin roots rather than from `HERMES_HOME`,
 * because the renderer is Electron-local and the backend's idea of home can
 * be a different machine entirely. `desktop-plugins` and `plugins` are both
 * direct children of the Hermes home, so either one names the parent.
 */
async function dataDir() {
  const desktop = window.hermesDesktop

  if (!desktop) {
    return null
  }

  const root = (await desktop.desktopPluginsRoot?.()) || (await desktop.agentPluginsRoot?.())

  if (!root) {
    return null
  }

  const home = String(root).replace(/[\\/]+(?:desktop-plugins|plugins)[\\/]*$/, '')

  return `${home}/plugin-data/${PLUGIN_ID}`
}

let cachedDir = null

/** The exact bytes behind the reading currently on screen.
 *
 *  The backend rewrites `state.json` when something happens and otherwise once
 *  every twenty seconds; this file reads it once a second, which is the
 *  resolution a countdown needs. Those two rates are not the same rate, and
 *  until 1.2.3 the difference was paid by the whole panel: every read parsed
 *  the document and handed `$snapshot` a brand-new object, so every subscriber
 *  re-rendered once a second over bytes that had not changed. On the Settings
 *  tab that is a form rebuilding itself under the cursor. */
let lastText = ''

async function readSnapshot() {
  const desktop = window.hermesDesktop

  if (!desktop?.readFileText) {
    $snapshot.set(null)
    $problem.set('This Hermes shell has no file bridge, so the pool cannot be read.')

    return
  }

  cachedDir ??= await dataDir()

  if (!cachedDir) {
    $problem.set('This Hermes shell reports no plugin root, so the snapshot cannot be located.')

    return
  }

  let text

  try {
    ;({ text } = await desktop.readFileText(`${cachedDir}/state.json`))
  } catch {
    // Absent is the ordinary case on a fresh install, and on any Hermes
    // running a KAME older than 1.1.0 — say which, rather than "error".
    $snapshot.set(null)
    $problem.set('No snapshot yet. KAME writes one as soon as the backend loads it.')

    return
  }

  if (text === lastText && $snapshot.get()) {
    // Byte-identical to what is already on screen. Nothing is published, so
    // nothing re-renders — but the pending request is still given its chance to
    // time out, because a backend that has stopped writing is exactly the case
    // where the file never changes and "Saving…" would otherwise stay for ever.
    settle($snapshot.get())

    return
  }

  let snap

  try {
    snap = JSON.parse(text)
  } catch {
    // A torn read should be impossible (the writer is atomic), so this is a
    // real corruption — but it is also self-healing, so it stays quiet and
    // waits for the next tick rather than clearing a good reading.
    $problem.set('The snapshot could not be parsed. Waiting for the next write.')

    return
  }

  if (snap?.schema !== SCHEMA) {
    $snapshot.set(null)
    $problem.set(
      `The installed KAME writes snapshot schema ${snap?.schema ?? '?'}; this panel reads ${SCHEMA}. ` +
        'The two halves ship together, so restarting Hermes usually settles it.'
    )

    return
  }

  lastText = text
  $snapshot.set(snap)
  $problem.set('')
  settle(snap)
}

/** Close the loop on a request once the backend has reported what it did. */
function settle(snap) {
  const pending = $pending.get()

  if (!pending) {
    return
  }

  const result = snap?.control ?? {}

  if (result.id && result.id === pending.id) {
    $pending.set(null)
    $notice.set({ detail: String(result.detail ?? ''), ok: Boolean(result.ok) })

    return
  }

  if (Date.now() - pending.at > CONTROL_TIMEOUT_MS) {
    $pending.set(null)
    $notice.set({
      detail: 'The backend has not picked that up. It reads requests once a second, so Hermes may be busy or stopped.',
      ok: false
    })
  }
}

/**
 * Ask the Python half to do one thing.
 *
 * Everything the page can change goes through here, and the list of actions is
 * closed on the other side. Nothing about keys is in it: this page cannot read
 * a credential, write one, or name one — the closest it comes is a
 * fingerprint, which is a hash and not a prefix.
 */
async function request(action, key = '', value = null) {
  const desktop = window.hermesDesktop

  if (!desktop?.writeTextFile) {
    $notice.set({ detail: 'This Hermes shell cannot write files, so settings are read-only here. Use /kame set instead.', ok: false })

    return
  }

  cachedDir ??= await dataDir()

  if (!cachedDir) {
    $notice.set({ detail: 'This Hermes shell reports no plugin root, so the request cannot be delivered.', ok: false })

    return
  }

  const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`

  $notice.set(null)
  $pending.set({ action, at: Date.now(), id, key })

  try {
    await desktop.writeTextFile(
      `${cachedDir}/control.json`,
      JSON.stringify({ action, id, key, schema: CONTROL_SCHEMA, value })
    )
  } catch (error) {
    $pending.set(null)
    // The bridge refuses to create directories, and the only directory this
    // writes into is the one the snapshot already lives in — so this really
    // does mean the backend never started.
    $notice.set({
      detail: `The request could not be written: ${error instanceof Error ? error.message : 'unknown error'}`,
      ok: false
    })
  }
}

/** The one live timer, so a second `register()` cannot add a second reader. */
let activeTimer = null

/** Start the tick. Returns a disposer, so a reload of this plugin leaves no
 *  timer behind. */
function startReading() {
  if (activeTimer !== null) {
    // Registering twice without a dispose in between is the shape a hot reload
    // has. One reader is the correct number: two would read, parse and publish
    // twice a second, and the panel would flicker at a rate nothing on the
    // backend is producing.
    return () => {}
  }

  let stopped = false

  const tick = () => {
    if (stopped) {
      return
    }

    $now.set(Date.now())

    // A hidden window has nothing to render, and the reading is worthless the
    // moment it comes back anyway — but the clock keeps running so the first
    // frame after a focus is already correct.
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
      return
    }

    void readSnapshot()
  }

  tick()

  const timer = window.setInterval(tick, TICK_MS)

  activeTimer = timer

  return () => {
    stopped = true
    window.clearInterval(timer)

    if (activeTimer === timer) {
      activeTimer = null
    }
  }
}

// -- the status bar chip ----------------------------------------------------

/** One pool on the chip: `provider:model 14/15`, truncated, never wrapped. */
function ChipPool({ pool, snap, now }) {
  const eta = countdown(pool.soonest_s, snap, now)

  return h(
    'span',
    { className: 'flex min-w-0 items-center gap-1', key: pool.identity },
    pool.invalid
      ? h('span', {
          className: 'size-1.5 shrink-0 rounded-full bg-destructive',
          title: `${pool.invalid} key(s) refused as credentials`
        })
      : null,
    h('span', { className: 'max-w-[9rem] truncate' }, pool.identity),
    h('span', { className: 'shrink-0 tabular-nums' }, `${pool.healthy}/${pool.keys}`),
    eta === null ? null : h('span', { className: 'shrink-0 text-(--ui-text-quaternary)' }, duration(eta))
  )
}

/**
 * Always on screen: which pools are in use, how healthy each one is, and the
 * countdown when one is resting.
 *
 * The ask this answers: knowing at a glance that nothing is frozen — pool
 * health visible whenever the app is not obviously working, and the ETA on
 * screen whenever one exists. So it renders between turns too, not only while
 * a request is in flight.
 *
 * 1.1.1 names the pool rather than the model, because `gemini-3.7-flash` and
 * `openai:gemini-3.7-flash` are two different pools of two different keys and
 * the shorter label made them look like one. Everything else here exists to
 * keep that longer label from pushing the status bar around: at most two pools
 * in full, the rest as `+N`, each name truncated with the whole picture in the
 * tooltip.
 */
function KameChip() {
  const snap = useValue($snapshot)
  const problem = useValue($problem)
  const now = useValue($now)

  const totals = snap?.totals ?? {}
  const activity = snap?.activity ?? null
  const age = ageSeconds(snap, now)
  const stale = age > STALE_AFTER_S
  const tone = toneFor(snap, now)
  const pools = snap?.installed ? chipPools(snap) : []
  const shown = pools.slice(0, CHIP_POOLS)
  const hidden = pools.length - shown.length

  const lines = []

  if (problem) {
    lines.push(problem)
  } else if (!snap?.installed) {
    lines.push(`${PRODUCT} is loaded but not rotating: ${snap?.reason ?? 'unknown'}`)
  } else if (!totals.keys) {
    lines.push(`${PRODUCT} is running. No pooled key has been used yet.`)
  } else {
    lines.push(`${totals.healthy} of ${totals.keys} keys ready, across every pool`)

    for (const pool of snap.pools ?? []) {
      const eta = countdown(pool.soonest_s, snap, now)
      const parts = [`${pool.identity}: ${pool.healthy}/${pool.keys} ready`]

      if (pool.invalid) {
        parts.push(`${pool.invalid} refused as credentials`)
      }

      if (eta !== null) {
        parts.push(`next key in ${duration(eta)}`)
      }

      lines.push(parts.join(' · '))
    }

    if (activity?.kind === 'calling') {
      lines.push(`Calling ${activity.model} on attempt ${activity.attempt}`)
    } else if (activity?.kind === 'waiting') {
      lines.push('Every key is resting — waiting rather than failing the turn')
    } else if (activity?.kind === 'stitching') {
      lines.push(`The answer was cut off — continuing it on another key (${activity.resume}/${activity.budget})`)
    }

    lines.push(`${snap.counters?.rotations ?? 0} rotations, ${snap.counters?.recovered ?? 0} recovered`)
  }

  if (stale && snap) {
    lines.push(`Last reading ${duration(age)} old — the backend may be down.`)
  }

  lines.push('Click for the full panel')

  return h(
    Tip,
    { label: lines.join('\n') },
    h(
      'button',
      {
        className: cn(
          'inline-flex h-full min-w-0 max-w-[22rem] items-center gap-1.5 overflow-hidden rounded-none px-1.5',
          'text-[0.6875rem] tabular-nums whitespace-nowrap transition-colors',
          'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
          stale && 'opacity-60'
        ),
        onClick: () => host.navigate(ROUTE),
        type: 'button'
      },
      h(StatusDot, { tone }),
      h('span', { className: 'shrink-0' }, 'KAME'),
      shown.length
        ? shown.map(pool => h(ChipPool, { key: pool.identity, now, pool, snap }))
        : h(
            'span',
            { className: 'shrink-0 text-(--ui-text-quaternary)' },
            snap?.installed ? (totals.keys ? `${totals.healthy}/${totals.keys}` : 'ready') : '—'
          ),
      hidden > 0 ? h('span', { className: 'shrink-0 text-(--ui-text-quaternary)' }, `+${hidden}`) : null
    )
  )
}

// -- small pieces of the page -----------------------------------------------

function Card(props, ...children) {
  return h(
    'section',
    {
      className: cn(
        'rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-tertiary)/40 p-4',
        props?.className
      ),
      key: props?.key
    },
    // Keyed, like every other variadic child list in this file. `h` drops a
    // falsy child, so a card that gains or loses its note shifts everything
    // after it by one — and React, reconciling a keyless list by position,
    // reads that shift as "different element here" and remounts the subtree.
    // For a card full of text that is invisible. For a card full of text
    // *inputs* it is the bug 1.2.3 exists to fix.
    props?.title &&
      h(
        'h2',
        { className: 'text-xs font-medium tracking-wide text-(--ui-text-secondary) uppercase', key: 'title' },
        props.title
      ),
    props?.note && h('p', { className: 'mt-1 text-xs text-(--ui-text-quaternary)', key: 'note' }, props.note),
    h('div', { className: 'mt-3', key: 'body' }, ...children)
  )
}

function Field(label, value, hint) {
  return h(
    'div',
    { className: 'flex items-baseline justify-between gap-3 py-1', key: label },
    h('span', { className: 'text-xs text-(--ui-text-tertiary)', key: 'label' }, label),
    h(
      'span',
      { className: 'text-right text-xs tabular-nums text-(--ui-text-primary)', key: 'value' },
      value,
      hint && h('span', { className: 'ml-2 text-(--ui-text-quaternary)', key: 'hint' }, hint)
    )
  )
}

function Note(text, tone = 'plain', key = undefined) {
  return h(
    'p',
    {
      className: cn(
        'rounded-md border p-3 text-sm',
        tone === 'bad'
          ? 'border-destructive/40 bg-destructive/5 text-(--ui-text-secondary)'
          : 'border-(--ui-stroke-tertiary) text-(--ui-text-secondary)'
      ),
      key
    },
    text
  )
}

/** One pool, with the bar that makes "12 of 15" readable without arithmetic.
 *
 *  Subscribes to the clock itself rather than being handed it. Every component
 *  that reads `$now` re-renders once a second, which is right for a countdown
 *  and wrong for everything else on the page — so the subscription lives on the
 *  countdown and not on the page that happens to contain one. */
function PoolRow({ pool, snap }) {
  const now = useValue($now)
  const ratio = pool.keys ? pool.healthy / pool.keys : 0
  const eta = countdown(pool.soonest_s, snap, now)

  return h(
    'div',
    { className: 'py-2', key: pool.identity },
    h(
      'div',
      { className: 'flex items-baseline justify-between gap-3', key: 'head' },
      h('span', { className: 'font-mono text-xs break-all text-(--ui-text-primary)', key: 'identity' }, pool.identity),
      h(
        'span',
        { className: 'shrink-0 text-xs tabular-nums text-(--ui-text-tertiary)', key: 'ready' },
        `${pool.healthy}/${pool.keys} ready`,
        // Comes and goes with the cooldown, so it needs a key of its own.
        eta !== null &&
          h('span', { className: 'ml-2 text-(--ui-text-quaternary)', key: 'eta' }, `next in ${duration(eta)}`)
      )
    ),
    h(
      'div',
      { className: 'mt-1.5 h-1 w-full overflow-hidden rounded-full bg-(--ui-bg-quinary)', key: 'bar' },
      h('div', {
        className: cn(
          'h-full rounded-full transition-[width] duration-500',
          ratio === 1 ? 'bg-primary' : ratio > 0 ? 'bg-amber-500' : 'bg-destructive'
        ),
        style: { width: `${Math.round(ratio * 100)}%` }
      })
    ),
    Boolean(pool.kinds?.length || pool.successes || pool.failures) &&
      h(
        'div',
        { className: 'mt-1 flex flex-wrap gap-x-3 text-[0.6875rem] text-(--ui-text-quaternary)', key: 'tally' },
        h('span', { key: 'counts' }, `${pool.successes} answered · ${pool.failures} refused`),
        pool.kinds?.length ? h('span', { key: 'kinds' }, pool.kinds.join(', ')) : null
      ),
    pool.invalid
      ? h(
          'p',
          { className: 'mt-1 text-[0.6875rem] text-destructive', key: 'invalid' },
          `${pool.invalid} key(s) refused as credentials — replace ${pool.invalid === 1 ? 'it' : 'them'}: ` +
            `${(pool.invalid_keys ?? []).join(', ')}`
        )
      : null
  )
}

/** What KAME is doing this second — the whole point of the page being live. */
function RightNow({ snap }) {
  const now = useValue($now)
  const activity = snap.activity
  const totals = snap.totals ?? {}

  if (!activity) {
    const eta = countdown(totals.soonest_s, snap, now)

    return h(
      'p',
      { className: 'text-sm text-(--ui-text-secondary)' },
      eta === null
        ? 'Idle. A key is ready, so the next call goes straight out.'
        : `Idle, and every key is resting. The first one comes back in ${duration(eta)}.`
    )
  }

  if (activity.kind === 'calling') {
    return h(
      'p',
      { className: 'text-sm text-(--ui-text-secondary)' },
      activity.attempt > 1
        ? `Rotated. Attempt ${activity.attempt} on ${activity.model}, with ${activity.healthy} of ${activity.keys} keys still ready.`
        : `Calling ${activity.model}, with ${activity.healthy} of ${activity.keys} keys ready.`
    )
  }

  if (activity.kind === 'waiting') {
    const eta = countdown(activity.eta_s, snap, now)

    return h(
      'p',
      { className: 'text-sm text-(--ui-text-secondary)' },
      `Every key is resting. Waiting ${eta === null ? 'for one to come back' : duration(eta)} rather than failing the turn — press Esc to stop.`
    )
  }

  if (activity.kind === 'stitching') {
    return h(
      'p',
      { className: 'text-sm text-(--ui-text-secondary)' },
      `The provider cut the answer after ${activity.characters} characters. Continuing it on another key ` +
        `(${activity.resume} of ${activity.budget}) — you should see one unbroken reply.`
    )
  }

  if (activity.kind === 'recovered') {
    return h(
      'p',
      { className: 'text-sm text-(--ui-text-secondary)' },
      `Answered after ${duration(activity.waited_s)} of waiting, on ${activity.model}.`
    )
  }

  return h('p', { className: 'text-sm text-(--ui-text-secondary)' }, 'Working.')
}

// -- settings ---------------------------------------------------------------

/** The source of a value, as a quiet badge beside it. */
function Source({ setting }) {
  return h(
    Tip,
    {
      label:
        setting.source === 'environment'
          ? `${setting.env} is set in the environment, which outranks everything else.`
          : setting.source === 'config'
            ? 'Set in config.yaml under this plugin. The environment would outrank it.'
            : 'Nothing anywhere mentions this one, so the built-in default is in force.'
    },
    h(
      'span',
      {
        className: cn(
          'rounded px-1.5 py-0.5 text-[0.625rem] whitespace-nowrap',
          setting.source === 'default'
            ? 'text-(--ui-text-quaternary)'
            : 'bg-(--ui-bg-quinary) text-(--ui-text-tertiary)'
        )
      },
      sourceLabel(setting.source)
    )
  )
}

function SettingShell({ setting, control, error }) {
  return h(
    'div',
    { className: 'border-b border-(--ui-stroke-tertiary)/60 py-3 last:border-b-0', key: setting.key },
    h(
      'div',
      { className: 'flex items-start justify-between gap-4' },
      h(
        'div',
        { className: 'min-w-0' },
        h('p', { className: 'text-sm text-(--ui-text-primary)' }, setting.title),
        h('p', { className: 'mt-0.5 text-xs leading-relaxed text-(--ui-text-tertiary)' }, setting.help),
        h(
          'p',
          { className: 'mt-1 flex flex-wrap items-center gap-2 font-mono text-[0.625rem] text-(--ui-text-quaternary)' },
          h('span', null, setting.key),
          setting.env ? h('span', null, setting.env) : null,
          h(Source, { setting })
        )
      ),
      h('div', { className: 'flex shrink-0 items-center gap-2' }, control)
    ),
    error ? h('p', { className: 'mt-2 text-xs text-destructive' }, error) : null
  )
}

/** A switch. Turning off the plugin itself asks first. */
function FlagSetting({ setting, busy }) {
  const [confirming, setConfirming] = useState(false)
  const [sent, setSent] = useState(null)

  // Same rule as `NumberSetting`: a switch shows what was asked of it until
  // the snapshot says the same thing, so one click is one movement instead of
  // a flick back to the old position and a flick forward again a tick later.
  useEffect(() => {
    if (sent !== null && (Boolean(setting.value) === sent.value || Date.now() - sent.at > SETTLE_GRACE_MS)) {
      setSent(null)
    }
  }, [sent, setting.value])

  const apply = next => {
    setSent({ at: Date.now(), value: next })
    void request('set', setting.key, next ? 'true' : 'false')
  }

  return h(
    'div',
    { key: setting.key },
    h(SettingShell, {
      control: [
        setting.source === 'default'
          ? null
          : h(
              Button,
              {
                disabled: busy,
                key: 'reset',
                onClick: () => {
                  setSent({ at: Date.now(), value: Boolean(setting.default) })
                  void request('reset', setting.key)
                },
                size: 'sm',
                variant: 'ghost'
              },
              'Reset'
            ),
        h(Switch, {
          checked: sent === null ? Boolean(setting.value) : sent.value,
          disabled: busy,
          key: 'switch',
          onCheckedChange: next => {
            // Only turning one ON is consequential: that is the direction that
            // stops KAME doing the job it was installed for. Turning it back
            // off restores the plugin and needs no ceremony.
            if (next && setting.consequential) {
              setConfirming(true)

              return
            }

            apply(next)
          }
        })
      ],
      setting
    }),
    h(ConfirmDialog, {
      confirmLabel: 'Turn it on',
      description:
        `${setting.help} This is the switch people reach for when they suspect KAME of breaking something — ` +
        'it is reversible, and nothing is uninstalled.',
      destructive: true,
      onClose: () => setConfirming(false),
      onConfirm: () => apply(true),
      open: confirming,
      title: `${setting.title}?`
    })
  )
}

/** A number, checked here for a readable message and again on the other side. */
function NumberSetting({ setting, busy }) {
  const [draft, setDraft] = useState(String(setting.value ?? ''))
  const [dirty, setDirty] = useState(false)
  const [sent, setSent] = useState(null)
  const [error, setError] = useState('')

  // The snapshot is the truth. While the field is untouched it follows what
  // the backend says — including a change made from `/kame set` in the chat,
  // which would otherwise leave this page showing a value nothing holds.
  //
  // `sent` is the exception, and it is the whole of the 1.2.2 fix. Saving used
  // to clear `dirty` straight away, which handed the field back to a snapshot
  // that had not been written yet: the number flicked back to the old value
  // for a tick or two and then forward to the new one, so one save read on
  // screen as the setting changing twice by itself. While a write is in
  // flight the field holds what was written, and lets go the moment the
  // backend reports that value — or when the write has plainly not landed.
  useEffect(() => {
    const arrived = String(setting.value ?? '')

    if (sent !== null) {
      if (arrived === sent.value || Date.now() - sent.at > SETTLE_GRACE_MS) {
        setSent(null)
        setDraft(arrived)
      }

      return
    }

    if (!dirty) {
      setDraft(arrived)
    }
  }, [dirty, sent, setting.value])

  const save = () => {
    const problem = validateNumber(setting, draft)

    if (problem) {
      setError(problem)

      return
    }

    const value = String(draft).trim()

    setError('')
    setDirty(false)
    setSent({ at: Date.now(), value })
    setDraft(value)
    void request('set', setting.key, value)
  }

  const range =
    setting.min === null || setting.max === null
      ? ''
      : `${setting.min}–${setting.max} ${setting.units}${setting.off_or_at_least ? `, 0 or ${setting.off_or_at_least}+` : ''}`

  return h(SettingShell, {
    control: [
      range ? h('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', key: 'range' }, range) : null,
      h(Input, {
        className: 'h-8 w-24 text-right tabular-nums',
        disabled: busy,
        inputMode: 'decimal',
        key: 'input',
        onChange: event => {
          setDirty(true)
          setError('')
          setDraft(event.target.value)
        },
        onKeyDown: event => {
          if (event.key === 'Enter') {
            event.preventDefault()
            save()
          }
        },
        value: draft
      }),
      h(
        Button,
        { disabled: busy || !dirty, key: 'save', onClick: save, size: 'sm', variant: 'default' },
        'Save'
      ),
      setting.source === 'default'
        ? null
        : h(
            Button,
            {
              disabled: busy,
              key: 'reset',
              onClick: () => {
                const value = String(setting.default ?? '')

                setDirty(false)
                setError('')
                setSent({ at: Date.now(), value })
                setDraft(value)
                void request('reset', setting.key)
              },
              size: 'sm',
              variant: 'ghost'
            },
            'Reset'
          )
    ],
    error,
    setting
  })
}

/** One card per shelf, in the order the Python half listed them.
 *
 *  The titles and the sentence under each one arrive in the snapshot rather
 *  than living here: which settings are optional extras, which are tuning and
 *  which are escape hatches is a fact about the plugin, and a second copy of
 *  that judgement written in JavaScript is a copy that drifts. A setting whose
 *  group nobody claims still gets rendered, in a card of its own at the end —
 *  wrong shelf beats missing.
 */
function settingCards({ busy, groups, settings }) {
  if (!settings.length) {
    return [
      Card(
        { key: '_empty', title: 'Settings' },
        h('p', { className: 'text-sm text-(--ui-text-tertiary)' }, 'This KAME reports no settings.')
      )
    ]
  }

  const control = setting =>
    setting.kind === 'flag'
      ? h(FlagSetting, { busy, key: setting.key, setting })
      : h(NumberSetting, { busy, key: setting.key, setting })

  const claimed = new Set()
  const cards = []
  for (const group of groups) {
    const rows = settings.filter(setting => setting.group === group.id)
    if (!rows.length) continue
    rows.forEach(setting => claimed.add(setting.key))
    cards.push(Card({ key: group.id, note: group.note, title: group.title }, rows.map(control)))
  }

  const rest = settings.filter(setting => !claimed.has(setting.key))
  if (rest.length) {
    cards.push(
      Card(
        { key: '_rest', note: 'Not sorted into any of the groups above.', title: 'Other' },
        rest.map(control)
      )
    )
  }
  return cards
}

function SettingsPage({ snap }) {
  const pending = useValue($pending)
  const notice = useValue($notice)
  const [confirming, setConfirming] = useState('')

  const settings = snap.settings ?? []
  const busy = Boolean(pending)
  const writable = Boolean(window.hermesDesktop?.writeTextFile)
  const stale = snap.settings_pending_restart ?? []

  // Every child below carries a key, and that is the whole of the 1.2.3 fix
  // for a saved number reverting to its old value.
  //
  // `h` drops a falsy child before handing the list to React, so this list
  // physically grows when "Saving…" appears and shrinks when it goes. React
  // reconciles a keyless child list by *position*: the settings cards used to
  // sit at index 4 and, the instant Save was pressed, at index 5 — where the
  // previous render had a paragraph. Different type at that position means
  // unmount and mount, so every card was rebuilt from scratch, and every
  // `NumberSetting` inside lost the `useState` holding what had just been
  // typed and the record that a write was in flight. The freshly mounted field
  // then initialised from the snapshot, which still carried the old value
  // because the backend had not published yet — so one Save read on screen as
  // the number jumping back to 0 on its own.
  //
  // With keys, position stops meaning anything and the cards keep their
  // identity through the appearing and disappearing paragraphs above them.
  return h(
    'div',
    { className: 'flex flex-col gap-4' },

    !writable &&
      Note(
        'This Hermes shell cannot write files, so nothing here can be changed from the panel. ' +
          '/kame set <key> <value> in the chat does the same job.',
        'plain',
        'read-only'
      ),

    // The one thing this page could not say before 1.2.2. KAME reads
    // config.yaml once, when Hermes starts, so an edit made since then is
    // sitting in the file doing nothing — and the page showed the old value
    // with no hint that it was old.
    stale.length > 0 &&
      Note(
        `config.yaml has been edited since Hermes started, so ${stale.join(', ')} ` +
          `${stale.length === 1 ? 'still holds the value it was read with' : 'still hold the values they were read with'}. ` +
          'Restart Hermes to apply the file, or set it here — a change made on this page takes effect on the next call.',
        'plain',
        'pending-restart'
      ),

    notice &&
      h(
        'p',
        {
          className: cn(
            'rounded-md border p-3 text-sm',
            notice.ok
              ? 'border-(--ui-stroke-tertiary) text-(--ui-text-secondary)'
              : 'border-destructive/40 bg-destructive/5 text-(--ui-text-secondary)'
          ),
          key: 'notice'
        },
        notice.detail || (notice.ok ? 'Done.' : 'That did not work.')
      ),

    pending && h('p', { className: 'text-xs text-(--ui-text-quaternary)', key: 'saving' }, 'Saving…'),

    h(
      'p',
      { className: 'text-xs text-(--ui-text-quaternary)', key: 'preamble' },
      'KAME works with none of these touched. A change takes effect on the next call, in every conversation this ' +
        "Hermes is serving, and is written to Hermes' own .env so it survives a restart. Only KAME_ lines are " +
        'touched; the rest of that file is left exactly as it is.'
    ),

    ...settingCards({ busy, groups: snap.setting_groups ?? [], settings }),

    Card(
      {
        key: 'maintenance',
        note: 'Neither of these can reach a key. KAME never reads, writes or deletes a credential.',
        title: 'Maintenance'
      },
      h(
        'div',
        { className: 'flex flex-wrap gap-2' },
        h(
          Tip,
          { label: 'Removes every KAME_ line from the .env and forgets the environment value, so every setting falls back to its built-in default.' },
          h(Button, { disabled: busy || !writable, onClick: () => setConfirming('reset_all'), size: 'sm', variant: 'outline' }, 'Reset to defaults')
        ),
        h(
          Tip,
          { label: 'Only clears rotation and quarantine; it does not delete any key.' },
          h(Button, { disabled: busy || !writable, onClick: () => setConfirming('clear_pool'), size: 'sm', variant: 'outline' }, 'Clear pool')
        ),
        h(
          Tip,
          { label: 'Empties the event list on the Events page. Nothing else changes.' },
          h(Button, { disabled: busy || !writable, onClick: () => void request('clear_events'), size: 'sm', variant: 'ghost' }, 'Clear events')
        )
      )
    ),

    h(ConfirmDialog, {
      confirmLabel: 'Reset everything',
      description:
        'Every KAME setting goes back to its built-in default, and the KAME_ lines are removed from the .env. ' +
        'Nothing else in that file is touched, and no key is affected.',
      destructive: true,
      key: 'confirm-reset',
      onClose: () => setConfirming(''),
      onConfirm: () => void request('reset_all'),
      open: confirming === 'reset_all',
      title: 'Reset every setting?'
    }),

    h(ConfirmDialog, {
      confirmLabel: 'Clear the pool',
      key: 'confirm-clear',
      description:
        'Every key starts again as if it had never been tried: cooldowns, quarantines and the request history are ' +
        'forgotten. This does not delete any key, and a key that is genuinely spent will simply be refused again.',
      onClose: () => setConfirming(''),
      onConfirm: () => void request('clear_pool'),
      open: confirming === 'clear_pool',
      title: 'Clear the pool?'
    })
  )
}

// -- events -----------------------------------------------------------------

/** How each kind of event is said, and how loudly. */
const EVENT_LABELS = {
  invalid_key: ['Invalid key', 'bad'],
  quarantine: ['Quarantined', 'bad'],
  recovery: ['Recovered', 'good'],
  rotation: ['Rotated', 'plain'],
  stitch: ['Continued', 'good'],
  storm: ['Outage', 'warn'],
  stream_drop: ['Answer cut', 'warn'],
  surfaced: ['Surfaced', 'bad'],
  wait: ['Waited', 'warn']
}

function EventRow({ event }) {
  const [label, tone] = EVENT_LABELS[event.kind] ?? [event.kind, 'plain']
  const when = new Date((event.at ?? 0) * 1000)

  return h(
    'div',
    { className: 'flex items-baseline gap-3 border-b border-(--ui-stroke-tertiary)/60 py-2 last:border-b-0', key: event.seq },
    h(
      'span',
      { className: 'w-16 shrink-0 font-mono text-[0.6875rem] tabular-nums text-(--ui-text-quaternary)', key: 'when' },
      Number.isFinite(when.getTime()) ? when.toLocaleTimeString() : '—'
    ),
    h(
      'span',
      {
        className: cn(
          'w-24 shrink-0 text-xs',
          tone === 'bad' ? 'text-destructive' : tone === 'warn' ? 'text-amber-500' : 'text-(--ui-text-secondary)'
        ),
        key: 'label'
      },
      label
    ),
    h(
      'span',
      { className: 'min-w-0 flex-1 text-xs text-(--ui-text-tertiary)', key: 'detail' },
      // Four of these five come and go with what the provider said, so the row
      // rebuilds its own tail on every event that carries a different set.
      h('span', { className: 'font-mono break-all text-(--ui-text-quaternary)', key: 'identity' }, event.identity || ''),
      event.key ? h('span', { className: 'ml-2 font-mono text-(--ui-text-quaternary)', key: 'fingerprint' }, event.key) : null,
      event.reason ? h('span', { className: 'ml-2', key: 'reason' }, event.reason) : null,
      event.code ? h('span', { className: 'ml-2 text-(--ui-text-quaternary)', key: 'code' }, `HTTP ${event.code}`) : null,
      event.seconds
        ? h('span', { className: 'ml-2 text-(--ui-text-quaternary)', key: 'rested' }, `rested ${duration(event.seconds)}`)
        : null
    )
  )
}

function EventsPage({ snap }) {
  const events = snap.events ?? []

  return h(
    'div',
    { className: 'flex flex-col gap-4' },
    Card(
      {
        note:
          'The last fifty decisions this Hermes process made about your keys, newest first. Keys appear as ' +
          'fingerprints — a hash, never a prefix of the key itself — and no provider error text is kept, ' +
          'because a provider can quote your prompt back inside one.',
        title: 'Events'
      },
      events.length
        ? events.map(event => h(EventRow, { event, key: event.seq }))
        : h(
            'p',
            { className: 'text-sm text-(--ui-text-tertiary)' },
            'Nothing recorded yet. Rotations, quarantines, cut answers and continuations appear here as they happen.'
          )
    )
  )
}

// -- the page ---------------------------------------------------------------

function FirstRun() {
  return Card(
    { title: 'Getting started' },
    h(
      'div',
      { className: 'flex flex-col gap-3 text-sm text-(--ui-text-secondary)' },
      h(
        'p',
        null,
        'KAME is running, and no pooled key has been used yet. Nothing is wrong — this page fills in the first time ' +
          'a call goes out.'
      ),
      h(
        'ol',
        { className: 'ml-4 flex list-decimal flex-col gap-2 text-xs text-(--ui-text-tertiary)' },
        h(
          'li',
          null,
          'Open Settings and paste several keys into one provider key field, separated by commas. Hermes stores ' +
            'them as one credential; KAME reads them as the several keys they are.'
        ),
        h('li', null, '/kame-keys does the same thing in bulk, from the chat.'),
        h('li', null, 'Send a message. The status bar chip starts showing this pool from the first call.')
      ),
      h(
        'p',
        { className: 'text-xs text-(--ui-text-quaternary)' },
        'With one key there is nothing to rotate to, and what remains — backoff sized from what the provider ' +
          'actually said, and a failure that waits instead of ending the turn — still works.'
      )
    )
  )
}

function Tab({ id, label, active }) {
  return h(
    'button',
    {
      className: cn(
        'rounded-md px-3 py-1 text-xs transition-colors',
        active
          ? 'bg-(--ui-bg-quinary) text-(--ui-text-primary)'
          : 'text-(--ui-text-tertiary) hover:text-(--ui-text-primary)'
      ),
      onClick: () => $tab.set(id),
      type: 'button'
    },
    label
  )
}

/** The live reading beside the page title.
 *
 *  Its own component so that it, and not the page, is what the one-second clock
 *  re-renders. Before 1.2.3 `KamePage` read `$now` itself, which meant the
 *  Settings tab — which has no countdown anywhere on it — rebuilt its entire
 *  form once a second. */
function HeaderStatus({ snap }) {
  const now = useValue($now)

  const totals = snap.totals ?? {}
  const age = ageSeconds(snap, now)
  const stale = age > STALE_AFTER_S

  return h(
    'div',
    { className: 'flex items-center gap-2 text-xs text-(--ui-text-tertiary)' },
    h(StatusDot, { key: 'dot', tone: toneFor(snap, now) }),
    h('span', { key: 'ready' }, snap.installed ? `${totals.healthy} of ${totals.keys} keys ready` : 'not rotating'),
    h(
      'span',
      { className: cn('text-(--ui-text-quaternary)', stale && 'text-(--ui-red)'), key: 'age' },
      stale ? `reading ${duration(age)} old` : 'live'
    )
  )
}

/** The "nothing has been written for a while" warning, on the same reasoning:
 *  it is the only thing outside the header that needs the clock, so it keeps a
 *  stable slot in the page and decides for itself whether to show anything. */
function StaleNote({ snap }) {
  const now = useValue($now)
  const age = ageSeconds(snap, now)

  if (age <= STALE_AFTER_S) {
    return null
  }

  return Note(
    `Nothing has been written for ${duration(age)}. The numbers below are the last reading, not the current one — ` +
      'the backend is probably restarting.'
  )
}

function KamePage() {
  const snap = useValue($snapshot)
  const problem = useValue($problem)
  const tab = useValue($tab)

  const header = right =>
    h(
      'header',
      { className: 'flex flex-wrap items-baseline justify-between gap-3', key: 'header' },
      h(
        'div',
        { className: 'flex items-baseline gap-2', key: 'title' },
        h('h1', { className: 'text-lg font-medium text-(--ui-text-primary)', key: 'name' }, PRODUCT),
        snap?.version &&
          h('span', { className: 'text-xs text-(--ui-text-quaternary)', key: 'version' }, `v${snap.version}`)
      ),
      right
    )

  if (!snap) {
    return h(
      'div',
      { className: 'flex h-full w-full flex-col gap-4 overflow-y-auto p-6' },
      header(null),
      h('p', { className: 'text-sm text-(--ui-text-tertiary)', key: 'problem' }, problem || 'Reading the pool…'),
      h(
        'p',
        { className: 'text-xs text-(--ui-text-quaternary)', key: 'hint' },
        'This page reads a file the backend half of KAME writes. If Hermes has just started, give it a moment.'
      )
    )
  }

  const counters = snap.counters ?? {}
  const repair = snap.gemini_tool_call_fix ?? {}
  const invalid = invalidCount(snap)

  return h(
    'div',
    { className: 'flex h-full w-full flex-col gap-4 overflow-y-auto p-6' },

    header(h(HeaderStatus, { key: 'status', snap })),

    h(
      'nav',
      { className: 'flex gap-1', key: 'tabs' },
      h(Tab, { active: tab === 'overview', id: 'overview', key: 'overview', label: 'Overview' }),
      h(Tab, { active: tab === 'settings', id: 'settings', key: 'settings', label: 'Settings' }),
      h(Tab, {
        active: tab === 'events',
        id: 'events',
        key: 'events',
        label: `Events${snap.events?.length ? ` (${snap.events.length})` : ''}`
      })
    ),

    !snap.installed &&
      Note(`KAME is loaded but every call keeps the key Hermes resolved: ${snap.reason}`, 'plain', 'not-rotating'),

    h(StaleNote, { key: 'stale', snap }),

    invalid > 0 &&
      Note(
        `${invalid} key(s) have been refused as credentials. Waiting will not repair one: replace ${invalid === 1 ? 'it' : 'them'} in ` +
          'Settings, or remove them from the pool. Until then every turn spends an attempt discovering the same thing.',
        'bad',
        'invalid'
      ),

    tab === 'settings'
      ? h(SettingsPage, { key: 'body', snap })
      : tab === 'events'
        ? h(EventsPage, { key: 'body', snap })
        : h(
            'div',
            { className: 'flex flex-col gap-4', key: 'body' },

            snap.first_run ? h(FirstRun, { key: 'first-run' }) : null,

            Card({ key: 'right-now', title: 'Right now' }, h(RightNow, { snap })),

            Card(
              {
                key: 'pool-health',
                note: 'One pool per provider and model. A key spent on one model keeps its allowance on another.',
                title: 'Pool health'
              },
              (snap.pools ?? []).length
                ? (snap.pools ?? []).map(pool => h(PoolRow, { key: pool.identity, pool, snap }))
                : h(
                    'p',
                    { className: 'text-sm text-(--ui-text-tertiary)', key: 'empty' },
                    'Nothing recorded yet — no call has been made through a pooled key this session.'
                  )
            ),

            Card(
              {
                key: 'process',
                note: 'Across every conversation it is serving, including subagents and the auxiliary lane.',
                title: 'This Hermes process'
              },
              Field('Calls', counters.calls ?? 0),
              Field('Rotations', counters.rotations ?? 0),
              Field('Recovered', counters.recovered ?? 0, 'answered after a rotation'),
              Field('Surfaced', counters.surfaced ?? 0, 'errors KAME let through'),
              counters.waits ? Field('Waited', duration(counters.waited_s), `across ${counters.waits} pause(s)`) : null,
              counters.stream_drops
                ? Field('Answers cut', counters.stream_drops, 'the provider stopped mid-sentence')
                : null,
              counters.stitched ? Field('Continued', counters.stitched, 'finished on another key, as one reply') : null,
              counters.mid_stream_cuts
                ? Field('Handed back', counters.mid_stream_cuts, 'could not be continued')
                : null,
              counters.tool_call_cuts
                ? Field('Cut in a tool call', counters.tool_call_cuts, 'a half-written call cannot be continued')
                : null
            ),

            Card(
              {
                key: 'gemini',
                note: 'Two parallel calls to one tool arrive under the same slot and their arguments are concatenated, which Hermes reports as "Response truncated due to output length limit".',
                title: 'Gemini parallel tool calls'
              },
              Field('Repair', repair.applied ? 'in place' : 'not applied', repair.applied ? null : repair.reason),
              repair.applied ? Field('Calls separated', repair.repaired ?? 0) : null
            )
          ),

    h(
      'footer',
      { className: 'pb-2 text-[0.6875rem] text-(--ui-text-quaternary)', key: 'footer' },
      h('p', { key: 'pid' }, `Backend process ${snap.pid} · reading refreshed every second`),
      h(
        'p',
        { className: 'mt-1', key: 'commands' },
        '/kame for the same picture in the chat, /kame-quota per key and model, /kame-keys to add keys in bulk'
      )
    )
  )
}

// -- registration -----------------------------------------------------------

export default {
  id: PLUGIN_ID,
  name: PRODUCT,
  description: 'Live key-pool health, rotation and recovery countdowns — a status-bar chip and a full panel.',
  register(ctx) {
    ctx.onDispose(startReading())

    ctx.registerMany([
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 90,
        render: () => h(KameChip, null)
      },
      {
        id: 'page',
        area: ROUTES_AREA,
        title: PRODUCT,
        data: { path: ROUTE },
        render: () => h(KamePage, null)
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 60,
        data: { codicon: 'sync', label: PRODUCT, path: ROUTE }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'kame.open',
          label: 'KAME: Open rotation panel',
          keywords: ['kame', 'rotation', 'keys', 'quota', 'pool', 'api'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])
  }
}
