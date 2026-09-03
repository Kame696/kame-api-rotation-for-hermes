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
 *  half is refused with a readable reason rather than half-rendered.
 *
 *  6 (1.6.0.1): the document carries a `processes` section per Hermes sharing
 *  this home. A machine running the Desktop and the gateway has two, they both
 *  load this plugin, and until this schema they overwrote each other's whole
 *  document several times a second — which defeated the byte comparison below
 *  and rebuilt the Settings form under the cursor once a second. */
const SCHEMA = 6

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

/** The failure whose raw payload is open in the inspector, or null.
 *
 *  Module-level rather than component state on purpose: the overlay is drawn
 *  once at the page root, so it is not clipped by the scroll container the
 *  event list lives in, and it survives the list re-rendering under it when a
 *  new snapshot lands mid-read. */
const $inspect = atom(null)

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

/**
 * How many keys the whole install has had refused as credentials.
 *
 * Mirrors `carousel.REFUSALS_BEFORE_RETIRING`, so the banner can say how many
 * more refusals a key has before it leaves rotation. Checked by a test rather
 * than trusted, because a number repeated in two languages drifts.
 */
const REFUSALS_BEFORE_RETIRING = 3

function invalidCount(snap) {
  return (snap?.pools ?? []).reduce((total, pool) => total + (pool.invalid ?? 0), 0)
}

/** Of those, the ones KAME has stopped offering altogether. */
function retiredCount(snap) {
  return (snap?.pools ?? []).reduce((total, pool) => total + (pool.retired ?? 0), 0)
}

/**
 * "1 key has" / "2 keys have".
 *
 * The rest of this file writes `key(s)`, which is fine in a tally line nobody
 * reads twice. These two banners are the most-read sentences on the panel and
 * are about somebody's own broken credential, so they get written properly.
 */
function count(n, one, many, verbOne = '', verbMany = '') {
  return `${n} ${n === 1 ? one : many}${verbOne ? ' ' + (n === 1 ? verbOne : verbMany) : ''}`
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

    // Settling here too: this branch can hold for as long as the
    // condition does, and a Save in flight would otherwise stay "Saving..."
    // for ever with every control disabled. settle() only ever clears a
    // request already past CONTROL_TIMEOUT_MS, so it cannot cut one short.
    settle($snapshot.get())

    return
  }

  cachedDir ??= await dataDir()

  if (!cachedDir) {
    $problem.set('This Hermes shell reports no plugin root, so the snapshot cannot be located.')

    // Settling here too: this branch can hold for as long as the
    // condition does, and a Save in flight would otherwise stay "Saving..."
    // for ever with every control disabled. settle() only ever clears a
    // request already past CONTROL_TIMEOUT_MS, so it cannot cut one short.
    settle($snapshot.get())

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

    // Settling here too: this branch can hold for as long as the
    // condition does, and a Save in flight would otherwise stay "Saving..."
    // for ever with every control disabled. settle() only ever clears a
    // request already past CONTROL_TIMEOUT_MS, so it cannot cut one short.
    settle($snapshot.get())

    return
  }

  // Deliberately *not* an early return on `text === lastText` any more. Since
  // schema 6 the file holds a section per Hermes sharing this home, and on a
  // machine running the Desktop and the gateway the bytes change every time
  // either of them writes — so comparing the file would re-render this panel
  // once a second over news that belongs to a different process. The
  // comparison moved below, onto the one section this panel actually shows.
  let document

  try {
    document = JSON.parse(text)
  } catch {
    // A torn read should be impossible (the writer is atomic), so this is a
    // real corruption — but it is also self-healing, so it stays quiet and
    // waits for the next tick rather than clearing a good reading.
    $problem.set('The snapshot could not be parsed. Waiting for the next write.')

    // Settling here too: this branch can hold for as long as the
    // condition does, and a Save in flight would otherwise stay "Saving..."
    // for ever with every control disabled. settle() only ever clears a
    // request already past CONTROL_TIMEOUT_MS, so it cannot cut one short.
    settle($snapshot.get())

    return
  }

  if (document?.schema !== SCHEMA) {
    $snapshot.set(null)
    $problem.set(
      `The installed KAME writes snapshot schema ${document?.schema ?? '?'}; this panel reads ${SCHEMA}. ` +
        'The two halves ship together, so restarting Hermes usually settles it.'
    )

    // Settling here too: this branch can hold for as long as the
    // condition does, and a Save in flight would otherwise stay "Saving..."
    // for ever with every control disabled. settle() only ever clears a
    // request already past CONTROL_TIMEOUT_MS, so it cannot cut one short.
    settle($snapshot.get())

    return
  }

  const snap = ownSection(document)

  if (!snap) {
    $snapshot.set(null)
    $problem.set('The snapshot names no Hermes process. Waiting for the next write.')
    settle($snapshot.get())

    return
  }

  // Everything below this panel renders comes from one process's section, so
  // that is what is compared. Byte-identical to what is already on screen
  // means nothing is published and nothing re-renders — but a pending request
  // is still given its chance to time out, because a backend that has stopped
  // writing is exactly the case where the section never changes and "Saving…"
  // would otherwise stay for ever.
  const mineText = JSON.stringify(snap)

  if (mineText === lastText && $snapshot.get()) {
    settle($snapshot.get())

    return
  }

  lastText = mineText
  $snapshot.set({ ...snap, neighbours: neighboursOf(document, snap) })
  $problem.set('')
  settle(snap)
}

/** A section still being written by a process that is alive.
 *
 *  The writer prunes sections older than this whenever it saves, but only a
 *  *live* process saves — so after every Hermes on a home has exited, its
 *  section stays on disk until one of them comes back. Nothing here should
 *  ever choose one of those. Mirrors `state._PROCESS_STALE_S`. */
const SECTION_STALE_S = 120

/** How much of the document this panel is looking at, and why that one.
 *
 *  Sticky, so the same process keeps the screen between ticks. */
let chosenPid = null

/** The section this panel is a part of.
 *
 *  This is harder than it looks, and 1.6.0.2 is the release that found out
 *  how. The panel reads a file, not its own backend — it has no way to ask
 *  "which process am I attached to?" — and the file holds a section per
 *  Hermes sharing the home.
 *
 *  **The bug this replaces.** The old rule was "the one whose role is
 *  `desktop`, else the freshest". `role` is read off `sys.argv`
 *  (`state.role()`), and the Desktop on this machine starts its backends in a
 *  way that matches neither `serve` nor `--profile`, so all of them report the
 *  generic `hermes` and the first half never matched. That left the second
 *  half deciding — *the freshest* — with **three live sections** in the file:
 *
 *      pid=13496  role='hermes'  events=150
 *      pid=16048  role='hermes'  events=0
 *      pid=6780   role='hermes'  events=3
 *
 *  Each writes on its own heartbeat, so "the freshest" named a different
 *  process every second or so, and the Events tab showed 150 rows, then none,
 *  then three, then none. Reported as the events freezing and then not coming
 *  back — which is exactly what it looks like from the outside, and no
 *  restart or new session could clear it, because nothing was stuck.
 *
 *  **The rule now**, in order:
 *
 *  1. Only sections still being written. A dead process's section lingers on
 *     disk until some Hermes starts again, and the old rule would happily
 *     have picked one.
 *  2. Keep the section already on screen while it stays alive. Stickiness is
 *     the whole fix for the flicker: whatever is chosen, it must not change
 *     because somebody else saved.
 *  3. Choosing fresh: prefer the process that most recently *routed a call*,
 *     not the one that most recently wrote. `last_call_at` moves only when
 *     there is real traffic, so it identifies the Hermes actually serving
 *     this chat and does not change on a heartbeat. A `desktop` outranks a
 *     `hermes`, and both outrank a `gateway` — the gateway serves the phone
 *     and its pools are not what this screen is about.
 *  4. `updated_at` only as the last tie-break, for a home where nothing has
 *     been asked of any process yet. */
function ownSection(document, now = Date.now()) {
  const live = Object.values(document?.processes ?? {}).filter(
    section =>
      section &&
      typeof section === 'object' &&
      now / 1000 - (section.updated_at ?? 0) <= SECTION_STALE_S
  )

  if (!live.length) {
    chosenPid = null

    return null
  }

  const sticky = live.find(section => String(section.pid) === String(chosenPid))

  // Stickiness holds against heartbeats, and yields to traffic. If another
  // live process has routed a call more recently than the one on screen, the
  // conversation has moved — a different model, a different profile, a
  // backend that was restarted under you — and following it is the whole
  // point. `last_call_at` changes only when a call is actually made, so this
  // cannot fire on a timer the way "the freshest section" did.
  if (sticky) {
    const mineCall = sticky.counters?.last_call_at ?? 0
    const busier = live.some(
      section =>
        section.pid !== sticky.pid && (section.counters?.last_call_at ?? 0) > mineCall
    )

    if (!busier) {
      return sticky
    }
  }

  const rank = section => (section.role === 'desktop' ? 0 : section.role === 'gateway' ? 2 : 1)
  const best = live.reduce((a, b) => {
    if (rank(a) !== rank(b)) return rank(a) < rank(b) ? a : b
    const callA = a.counters?.last_call_at ?? 0
    const callB = b.counters?.last_call_at ?? 0
    if (callA !== callB) return callA > callB ? a : b

    return (a.updated_at ?? 0) >= (b.updated_at ?? 0) ? a : b
  })

  chosenPid = best.pid

  return best
}

/** The other Hermes processes using these same keys, newest first.
 *
 *  Not a diagnostic curiosity: they share the credential pool. A key the
 *  gateway is resting is a key this Hermes cannot use either, and a gateway
 *  running last month's build is why a fix that is definitely installed is
 *  definitely not working on half the traffic. */
function neighboursOf(document, mine, now = Date.now()) {
  return Object.values(document?.processes ?? {})
    .filter(
      section =>
        section &&
        typeof section === 'object' &&
        section.pid !== mine.pid &&
        // A process that has stopped leaves its section on disk until some
        // Hermes writes again. Listing it as a live neighbour was the same
        // mistake `ownSection` was making, and here it would claim your keys
        // are being shared with something that exited an hour ago.
        now / 1000 - (section.updated_at ?? 0) <= SECTION_STALE_S
    )
    .sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0))
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
    lines.push(
      totals.rejected
        ? `${totals.ready ?? totals.healthy} of ${totals.keys} keys ready, across every pool ` +
            `(${totals.rejected} the provider rejected — replace them, waiting will not help)`
        : `${totals.ready ?? totals.healthy} of ${totals.keys} keys ready, across every pool`
    )

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
  // A credential the provider rejected is not benched after its hour is up, so
  // it counts as healthy again — while the line at the bottom of this very row
  // says it has to be replaced. The bar and the count take the rejected ones
  // out; the row still names them, which is the part that is actionable.
  const ready = Math.max(0, pool.healthy - (pool.invalid ?? 0))
  const ratio = pool.keys ? ready / pool.keys : 0
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
        `${ready}/${pool.keys} ready`,
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
    // Two sentences, because they ask the reader for different things. A key
    // that is merely benched will be tried again on its own; a key that has
    // left rotation will not, until it is replaced. Saying "refused, retry in
    // 20s" about the second one is what had the owner watching the same error
    // come round for an hour.
    pool.retired
      ? h(
          'p',
          { className: 'mt-1 text-[0.6875rem] text-destructive', key: 'retired' },
          `${pool.retired} key(s) are out of rotation — the provider refused ${
            pool.retired === 1 ? 'it' : 'them'
          } as ${pool.retired === 1 ? 'a credential' : 'credentials'}. ` +
            `Replace ${pool.retired === 1 ? 'it' : 'them'} in Settings and ${
              pool.retired === 1 ? 'it comes' : 'they come'
            } back on its own: ${(pool.retired_keys ?? []).join(', ')}`
        )
      : null,
    pool.invalid > (pool.retired ?? 0)
      ? h(
          'p',
          { className: 'mt-1 text-[0.6875rem] text-amber-500', key: 'invalid' },
          `${pool.invalid - (pool.retired ?? 0)} more just refused — still being tried, ` +
            'because one refusal can be a token that was a second from refreshing'
        )
      : null,
    // Not a fault, and deliberately not styled as one. The carousel uses the
    // key the agent already carries when the credential pool does not know it,
    // because dropping it would narrow what Hermes would have sent. Saying so
    // is the point: a key nobody remembers configuring is a key nobody can
    // fix, and on this machine it was the only NVIDIA key that authenticated
    // while the two in the pool were being refused.
    pool.outside_pool?.length
      ? h(
          'p',
          { className: 'mt-1 text-[0.6875rem] text-(--ui-text-quaternary)', key: 'outside' },
          `${pool.outside_pool.length} key(s) here are not in the credential pool — ` +
            `Hermes resolved ${pool.outside_pool.length === 1 ? 'it' : 'them'} from this model's own ` +
            `settings: ${pool.outside_pool.join(', ')}`
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

/**
 * One setting, as a row.
 *
 * 1.6.0.1 halved its height. Thirteen settings each carrying a title, a
 * paragraph, two monospace names and a chip made a page that had to be
 * scrolled past to reach the buttons at the bottom, and the density was doing
 * nothing for anybody: the paragraph matters the first time and never again,
 * and the names matter only to somebody typing `/kame set`.
 *
 * So the paragraph is clamped to two lines with the whole of it on hover, the
 * names sit on one muted line beside the source chip, and a row whose value is
 * not the default is marked in the margin — which is the one thing a person
 * scanning this page is actually looking for.
 */
function SettingShell({ setting, control, error }) {
  const changed = setting.source !== 'default'

  return h(
    'div',
    {
      className: cn(
        'border-l-2 py-2 pl-3 last:border-b-0',
        changed ? 'border-(--ui-stroke-secondary)' : 'border-transparent'
      ),
      key: setting.key
    },
    h(
      'div',
      { className: 'flex items-start justify-between gap-4' },
      h(
        'div',
        { className: 'min-w-0' },
        h(
          'p',
          { className: 'flex items-center gap-2 text-sm text-(--ui-text-primary)' },
          setting.title,
          changed
            ? h(
                'span',
                {
                  className: 'rounded bg-(--ui-bg-quinary) px-1.5 py-0.5 text-[0.625rem] text-(--ui-text-tertiary)',
                  key: 'changed'
                },
                'changed'
              )
            : null
        ),
        h(
          'p',
          {
            className: 'mt-0.5 line-clamp-2 text-xs leading-relaxed text-(--ui-text-tertiary)',
            title: setting.help
          },
          setting.help
        ),
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
    const changed = rows.filter(setting => setting.source !== 'default').length
    cards.push(
      Card(
        {
          key: group.id,
          note: group.note,
          title: changed ? `${group.title} · ${changed} changed` : group.title
        },
        rows.map(control)
      )
    )
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
  const changedCount = settings.filter(setting => setting.source !== 'default').length
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

    // The toolbar sits at the top from 1.6.0.1. It used to be a card at the
    // bottom of thirteen settings, which put "Reset to defaults" and — worse —
    // the only way to re-read the environment behind a full page of scrolling.
    Card(
      {
        key: 'toolbar',
        note:
          'KAME works with none of these touched. A change takes effect on the next call, in every ' +
          "conversation this Hermes is serving, and is written to Hermes' own .env so it survives a " +
          'restart — only KAME_ lines, the rest of that file is left exactly as it is. Nothing on this ' +
          'page can reach a key: KAME never reads, writes or deletes a credential.',
        title: changedCount
          ? `Settings · ${changedCount} of ${settings.length} changed from default`
          : `Settings · all ${settings.length} at their defaults`
      },
      h(
        'div',
        { className: 'flex flex-wrap gap-2' },
        h(
          Tip,
          {
            label:
              "Reads Hermes' .env again and rebuilds this page on the spot. Use it after editing that " +
              'file by hand, or after adding a key somewhere else, instead of restarting.'
          },
          h(
            Button,
            { disabled: busy || !writable, onClick: () => void request('refresh'), size: 'sm', variant: 'outline' },
            'Refresh'
          )
        ),
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

    ...settingCards({ busy, groups: snap.setting_groups ?? [], settings }),

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
  denied_model: ['Not this model', 'warn'],
  invalid_key: ['Invalid key', 'bad'],
  quarantine: ['Quarantined', 'bad'],
  recovery: ['Answered', 'good'],
  rotation: ['Rested', 'plain'],
  stitch: ['Continued', 'good'],
  storm: ['Outage', 'warn'],
  stream_drop: ['Answer cut', 'warn'],
  surfaced: ['Handed over', 'bad'],
  switch: ['Took over', 'good'],
  wait: ['Waiting', 'warn']
}

/**
 * What each kind means, in a sentence, for somebody who did not write this.
 *
 * The tab used to be a list of nine words with no glossary anywhere on the
 * screen, which is a readout for the person who built it. These render as the
 * row's tooltip and as the legend under the filters.
 */
const EVENT_MEANING = {
  denied_model: 'The provider refused this key for THIS model only — a plan that does not include it, or an API not switched on. The key is fine everywhere else, and replacing it would change nothing.',
  invalid_key: 'The provider said this is not a valid credential. Replace it — waiting will not repair one.',
  quarantine: 'This key was rested for a minute or more before it will be offered again.',
  recovery: 'A key answered after KAME had already rotated at least once. The rotation worked.',
  rotation: 'This key was rested briefly and the next call went somewhere else.',
  stitch: 'An answer that was cut short was continued on another key and joined back up.',
  storm: 'A lot of keys refused at once, which is usually the provider rather than your keys.',
  stream_drop: 'The provider stopped in the middle of an answer.',
  surfaced: 'KAME ran out of things to try, so the error was handed to you rather than hidden.',
  switch: 'KAME moved the call to this key.',
  wait: 'Every key was resting, so KAME waited instead of spending a request it knew would be refused.'
}

/**
 * The classifier's word for a refusal, in the reader's language.
 *
 * `reason` is written by `core.classify`, whose vocabulary is precise and
 * internal — `per_minute`, `insufficient_quota`, `auth_permanent`. Printing it
 * raw made the busiest column on the page the one nobody outside this codebase
 * can read. Anything not in here falls through unchanged, so a reason a future
 * release invents is still shown rather than swallowed.
 */
const REASON_WORDS = {
  auth: 'the provider refused this credential',
  auth_permanent: 'the provider says this is not a key',
  billing: 'the account is out of credit',
  daily: "today's quota is spent",
  denied: 'this key may not use this model',
  host_breaker: 'Hermes stopped the call itself',
  insufficient_quota: 'the account is out of credit',
  other: 'an error nothing recognised',
  per_minute: 'a per-minute rate limit',
  rate_limit: 'a rate limit',
  refused: 'the provider refused the call',
  server: 'the provider had a server error',
  timeout: 'the provider did not answer in time'
}

/**
 * The kinds that are KAME working, rather than a provider failing.
 *
 * Mirrors `core.events.GOOD_KINDS`, and the mirror is checked by a test. The
 * split is the whole point of the 1.6.0.1 tab: until this release the buffer
 * only ever recorded failures, so a rotation engine doing its job produced a
 * screen that read like a fault report.
 */
const GOOD_KINDS = new Set(['switch', 'recovery', 'stitch', 'wait'])

/** The three views. `id` is what the filter chip stores. */
const EVENT_VIEWS = [
  ['all', 'Everything', () => true],
  ['did', 'What KAME did', event => GOOD_KINDS.has(event.kind)],
  ['wrong', 'What went wrong', event => !GOOD_KINDS.has(event.kind)]
]

/**
 * `nvidia:moonshotai/kimi-k3` -> `moonshotai/kimi-k3`.
 *
 * The provider is already the first half of every fingerprint's story and it
 * repeats on every row of one incident; the model is what distinguishes two
 * pools on the same provider. The whole identity stays on the hover.
 */
function modelOnly(identity) {
  const text = String(identity ?? '')
  const at = text.indexOf(':')

  return at === -1 ? text : text.slice(at + 1)
}

/** "4m ago" — the form that is actually useful on a screen that updates itself. */
function ago(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 45) return 'just now'
  if (seconds < 90) return 'a minute ago'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`

  return `${Math.round(seconds / 86400)}d ago`
}

// Where a cooldown came from, in one word, for the column that finally makes
// the difference visible. Nine days of telemetry said 67% of every bench in
// this pool was `dropped` — a guess — and nobody could see it, because the
// panel showed the number and never where it came from. A wrong cooldown and a
// well-sourced one look identical until this column exists.
const SIZED_BY_LABELS = {
  catalog: ['field', 'good'],
  header: ['header', 'good'],
  retryinfo: ['retryDelay', 'good'],
  anchor: ['reset time', 'good'],
  exception: ['sdk', 'good'],
  type: ['field', 'good'],
  verdict: ['evidence', 'good'],
  pattern: ['text', 'weak'],
  table: ['table', 'weak'],
  dropped: ['guess', 'weak']
}

function EventRow({ event, now }) {
  const [label, tone] = EVENT_LABELS[event.kind] ?? [event.kind, 'plain']
  // 1.5.0: the payload opens in an inspector, and only for a failure.
  //
  // The history matters, because two of the three shapes were wrong. 1.2.9
  // used `window.alert()` — it stops the renderer, cannot be copied out of
  // comfortably, and shows one payload at a time; it also arrived beside a
  // mangled template literal, so the tab threw on the first event with a
  // status code and took the panel with it. 1.4.0 replaced it with an inline
  // expander, which fixed all of that and introduced a smaller problem: every
  // row with a `detail` was clickable, including the ones that succeeded,
  // where the thing revealed is not an error and there is nothing to check.
  //
  // So: an overlay rather than an alert (the renderer keeps running, the text
  // stays selectable, Escape closes it), and only on a row that represents a
  // failure. A recovery has nothing to inspect and no longer pretends to.
  const when = new Date((event.at ?? 0) * 1000)
  const canInspect = Boolean(event.detail) && tone !== 'good'
  const sized = SIZED_BY_LABELS[event.sized_by] ?? null
  // Relative, because this list redraws itself once a second and "4m ago" is
  // the question a person is actually asking of it. The wall clock stays, on
  // the hover, for the one case relative time is no use: matching a row
  // against a log line.
  const elapsed = (now - (event.at ?? 0) * 1000) / 1000
  const clock = Number.isFinite(when.getTime()) ? when.toLocaleTimeString() : ''
  const meaning = EVENT_MEANING[event.kind] ?? ''

  return h(
    'div',
    {
      className: cn(
        'group flex gap-3 border-l-2 py-1.5 pl-3 transition-colors',
        tone === 'bad'
          ? 'border-destructive/60'
          : tone === 'warn'
            ? 'border-amber-500/50'
            : tone === 'good'
              ? 'border-emerald-500/40'
              : 'border-(--ui-stroke-tertiary)',
        'hover:bg-(--ui-bg-quinary)/40'
      ),
      key: event.seq
    },
    h(
      'div',
      {
        className: cn('flex min-w-0 flex-1 items-baseline gap-3', canInspect && 'cursor-pointer'),
        onClick: canInspect ? () => $inspect.set(event) : undefined,
        title: canInspect ? `${meaning}

Click to see what the provider actually said.` : meaning,
        key: 'summary'
      },
      h(
        'span',
        {
          className: 'w-20 shrink-0 text-[0.6875rem] tabular-nums text-(--ui-text-quaternary)',
          key: 'when',
          title: clock
        },
        ago(elapsed) || clock || '—'
      ),
      h(
        'span',
        {
          className: cn(
            'w-24 shrink-0 text-xs',
            tone === 'bad'
              ? 'text-destructive'
              : tone === 'warn'
                ? 'text-amber-500'
                : tone === 'good'
                  ? 'text-emerald-500'
                  : 'text-(--ui-text-secondary)'
          ),
          key: 'label'
        },
        label
      ),
      // The reason leads, because it is the sentence. Until 1.6.0.1 the row
      // opened with `provider:model` and a fingerprint — two strings that are
      // the same on every row of an incident — and the one part that differed
      // was fourth. Everything that identifies rather than explains is now a
      // muted tail, right of the sentence and truncated before it.
      h(
        'span',
        { className: 'min-w-0 flex-1 truncate text-xs text-(--ui-text-secondary)', key: 'reason' },
        REASON_WORDS[event.reason] ?? event.reason ?? EVENT_LABELS[event.kind]?.[0] ?? ''
      ),
      h(
        'span',
        { className: 'flex shrink-0 items-baseline gap-2 text-xs text-(--ui-text-quaternary)', key: 'detail' },
        h(
          'span',
          { className: 'max-w-[14rem] truncate font-mono', key: 'identity', title: event.identity || '' },
          modelOnly(event.identity)
        ),
        event.key ? h('span', { className: 'font-mono', key: 'fingerprint' }, event.key) : null,
        event.code ? h('span', { key: 'code' }, `HTTP ${event.code}`) : null,
        event.seconds
          ? h(
              'span',
              { key: 'rested' },
              event.kind === 'recovery'
                ? `after ${duration(event.seconds)}`
                : event.kind === 'wait'
                  ? `for ${duration(event.seconds)}`
                  : `rested ${duration(event.seconds)}`
            )
          : null,
        sized
          ? h(
              'span',
              {
                className: cn(
                  'rounded px-1 text-[0.625rem] uppercase tracking-wide',
                  sized[1] === 'good' ? 'text-(--ui-text-quaternary)' : 'text-amber-500'
                ),
                title:
                  sized[1] === 'good'
                    ? 'The provider said how long. This wait is its number, not ours.'
                    : 'Nothing in the response said how long. This wait is a fallback.',
                key: 'sized'
              },
              sized[0]
            )
          : null,
        canInspect
          ? h(
              'span',
              {
                className:
                  'rounded px-1 text-[0.625rem] uppercase tracking-wide text-(--ui-text-quaternary) ' +
                  'underline decoration-dotted underline-offset-2',
                key: 'inspect'
              },
              'see the error'
            )
          : null
      )
    ),
  )
}

/**
 * The raw provider payload for one failure, over the page.
 *
 * Already scrubbed by `core.redact` **before it was written to disk**, which is
 * the part that matters: redacting on the way to a screen would leave the
 * secret in `state.json`, in a screenshot of this panel, and in any support
 * bundle built from either. Nothing is redacted here, because nothing here
 * ever held a secret.
 *
 * Deliberately not `ConfirmDialog`: that renders a sentence and asks a
 * question, and this is neither — it is a block of machine output a person
 * needs to read, select and copy.
 */
function PayloadInspector() {
  const event = useValue($inspect)

  useEffect(() => {
    if (!event) {
      return undefined
    }
    const onKey = codes => {
      if (codes.key === 'Escape') {
        $inspect.set(null)
      }
    }
    window.addEventListener('keydown', onKey)

    return () => window.removeEventListener('keydown', onKey)
  }, [event])

  if (!event) {
    return null
  }

  const when = new Date((event.at ?? 0) * 1000)
  const [label] = EVENT_LABELS[event.kind] ?? [event.kind]

  return h(
    'div',
    {
      className: 'fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6',
      // The backdrop closes; the card below stops the click so a drag that
      // ends outside a selection does not dismiss what is being read.
      onClick: () => $inspect.set(null)
    },
    h(
      'div',
      {
        // `--ui-bg-elevated` and not `--ui-bg-primary`. The fill ladder
        // (`--ui-bg-primary`, `-secondary`, `-tertiary` ...) is built for
        // tinting a surface that already exists: every rung ends in
        // `color-mix(in srgb, var(--ui-base) 10%, transparent)`, so it is a
        // 90%-transparent wash, not a background. Painted onto a floating
        // card it let the whole page through and the payload was read over
        // the events list behind it.
        //
        // `--ui-bg-elevated` is the one token in the family with no
        // `transparent` in it — `color-mix(in srgb, var(--theme-elevated-seed)
        // var(--theme-mix-elevated), var(--theme-neutral-card))` — and it is
        // what Hermes' own popovers use for exactly this job. Following the
        // host's recipe also means the card keeps tracking the user's theme
        // instead of hard-coding a colour that breaks on the next one.
        className:
          'flex max-h-[80vh] w-full max-w-3xl flex-col gap-3 rounded-lg border border-(--ui-stroke-secondary) ' +
          'bg-(--ui-bg-elevated) p-4 shadow-xl',
        onClick: codes => codes.stopPropagation()
      },
      h(
        'div',
        { className: 'flex items-baseline justify-between gap-3', key: 'head' },
        h(
          'div',
          { className: 'flex min-w-0 items-baseline gap-2', key: 'what' },
          h('span', { className: 'text-sm font-medium text-(--ui-text-primary)', key: 'label' }, label),
          h(
            'span',
            { className: 'truncate font-mono text-xs text-(--ui-text-quaternary)', key: 'identity' },
            event.identity || ''
          ),
          Number.isFinite(when.getTime())
            ? h(
                'span',
                { className: 'font-mono text-[0.6875rem] text-(--ui-text-quaternary)', key: 'when' },
                when.toLocaleTimeString()
              )
            : null
        ),
        h(Button, { onClick: () => $inspect.set(null), size: 'sm', variant: 'ghost', key: 'close' }, 'Close')
      ),
      h(
        'p',
        { className: 'text-xs text-(--ui-text-tertiary)', key: 'why' },
        'What the provider actually sent, with anything key-shaped removed before it was stored.'
      ),
      h(
        'pre',
        {
          // `--ui-bg-editor` for the same reason as the card above, and
          // because it is the token Hermes uses for a code surface. The
          // payload is the one thing in this dialog the user opened it to
          // read, so it gets the opaque one rather than a 93%-transparent
          // wash over it. `--ui-text-secondary` for the same reason:
          // `-tertiary` is the shade for labels nobody has to read closely.
          className:
            'min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-all rounded bg-(--ui-bg-editor) p-3 ' +
            'font-mono text-[0.6875rem] leading-relaxed text-(--ui-text-secondary) select-text',
          key: 'payload'
        },
        event.detail
      )
    )
  )
}

/** One number and its word, for the strip above the list. */
/**
 * One line saying the plugin is alive, under a list that is allowed to be
 * empty.
 *
 * Events records failures and rotations. A day where nothing failed writes
 * nothing, which is the same screen a plugin that stopped running draws — and
 * the owner read one as the other, cleared the list, restarted twice, and
 * reported it as frozen. It was quiet. Nothing on the screen said so.
 *
 * `last_call_at` is the fact that separates them: a wall clock stamped every
 * time KAME had a call to route. Paired with `updated_at` it answers both
 * halves of the question — *is the panel reading a live process* and *is that
 * process being asked to do anything* — which are different failures and were
 * being reported as one.
 */
function Heartbeat({ snap, now }) {
  const counters = snap.counters ?? {}
  const calls = Number(counters.calls ?? 0)
  const lastCall = Number(counters.last_call_at ?? 0)
  const wrote = Number(snap.updated_at ?? 0)

  // Age of the document itself. A Hermes that died mid-turn leaves a snapshot
  // that still looks live, so this is the only way to catch a stale reader.
  const readAge = wrote ? (now - wrote * 1000) / 1000 : null
  const stale = readAge !== null && readAge > 30

  let sentence
  if (!snap.installed) {
    sentence = 'KAME is not installed in this Hermes process, so nothing will be recorded here.'
  } else if (stale) {
    sentence =
      `This reading is ${ago(readAge)} — the Hermes process that writes it may have stopped. ` +
      'Everything below is what it last knew, not what is happening now.'
  } else if (!calls) {
    sentence = 'KAME is running and has not been asked to route a call yet in this process.'
  } else if (lastCall) {
    sentence =
      `KAME is running. ${calls} call${calls === 1 ? '' : 's'} routed, the last one ` +
      `${ago((now - lastCall * 1000) / 1000)}. An empty list above means nothing failed.`
  } else {
    sentence = `KAME is running. ${calls} call${calls === 1 ? '' : 's'} routed. An empty list above means nothing failed.`
  }

  return h(
    'p',
    {
      className: `mt-3 border-t border-(--ui-border-secondary) pt-3 text-xs ${
        stale ? 'text-(--ui-text-warning)' : 'text-(--ui-text-tertiary)'
      }`
    },
    sentence
  )
}

function Tally({ count, label, tone, active, onClick, title }) {
  return h(
    'button',
    {
      className: cn(
        'flex min-w-0 flex-col items-start rounded-md border px-3 py-2 text-left transition-colors',
        active
          ? 'border-(--ui-stroke-secondary) bg-(--ui-bg-quinary)'
          : 'border-transparent hover:bg-(--ui-bg-quinary)/50'
      ),
      onClick,
      title,
      type: 'button'
    },
    h(
      'span',
      {
        className: cn(
          'text-lg leading-none font-medium tabular-nums',
          tone === 'bad'
            ? 'text-destructive'
            : tone === 'good'
              ? 'text-emerald-500'
              : 'text-(--ui-text-primary)'
        ),
        key: 'n'
      },
      count
    ),
    h('span', { className: 'mt-1 text-[0.6875rem] text-(--ui-text-tertiary)', key: 'l' }, label)
  )
}

/**
 * What happened, newest first — and, since 1.6.0.1, what KAME *did* about it.
 *
 * The tab shipped as a list of failures. That is half a story: a rotation
 * engine working perfectly produced a screen of red, because the rotation
 * itself was never recorded. `switch`, `recovery` and `wait` were in the
 * event vocabulary from 1.1.1 and not one of them was ever written.
 *
 * So the page is now built around the split rather than around the buffer:
 * three tallies that are also the filter, a legend, and rows whose left rail
 * says at a glance which half of the story they belong to.
 */
function EventsPage({ snap }) {
  const now = useValue($now)
  const [view, setView] = useState('all')
  const events = snap.events ?? []

  const good = events.filter(event => GOOD_KINDS.has(event.kind))
  const bad = events.length - good.length
  const keep = EVENT_VIEWS.find(([id]) => id === view)?.[2] ?? (() => true)
  const shown = events.filter(keep)

  // The window the buffer actually covers, so the tallies are not read as
  // "since Hermes started" — which they are not, and which would make a quiet
  // afternoon look like a broken one.
  const oldest = events.length ? events[events.length - 1].at : null
  const covers = oldest ? ago((now - oldest * 1000) / 1000) : ''

  return h(
    'div',
    { className: 'flex flex-col gap-4' },
    Card(
      {
        note:
          'Every decision this Hermes process made about your keys, newest first — the failures and the ' +
          'rotations that answered them. Keys appear as fingerprints: a hash, never a prefix of the key ' +
          'itself. Provider error text is scrubbed before it is written down, because a provider can quote ' +
          'your own prompt back inside an error.',
        title: 'Events'
      },
      h(
        'div',
        { className: 'flex flex-wrap items-stretch gap-1', key: 'tallies' },
        h(Tally, {
          active: view === 'all',
          count: events.length,
          key: 'all',
          label: covers ? `in the last ${covers.replace(' ago', '')}` : 'recorded',
          onClick: () => setView('all'),
          title: 'Everything in the buffer, newest first.',
          tone: 'plain'
        }),
        h(Tally, {
          active: view === 'did',
          count: good.length,
          key: 'did',
          label: 'KAME rotating',
          onClick: () => setView('did'),
          title: 'Rotations, recoveries, continued answers and waits — the plugin doing its job.',
          tone: 'good'
        }),
        h(Tally, {
          active: view === 'wrong',
          count: bad,
          key: 'wrong',
          label: 'providers refusing',
          onClick: () => setView('wrong'),
          title: 'What the providers said. A refusal here is not a fault in KAME; it is what KAME is for.',
          tone: bad ? 'bad' : 'plain'
        })
      ),
      shown.length
        ? h(
            'div',
            { className: 'mt-3 flex flex-col', key: 'rows' },
            shown.map(event => h(EventRow, { event, key: event.seq, now }))
          )
        : h(
            'p',
            { className: 'mt-3 text-sm text-(--ui-text-tertiary)', key: 'empty' },
            events.length
              ? 'Nothing of that kind yet.'
              : 'Nothing recorded yet. Every rotation, refusal, cut answer and continuation appears here as it happens.'
          ),
      // Proof of life, and the reason it exists: this list records failures
      // and rotations, so a healthy stretch draws exactly what a broken plugin
      // draws — nothing. An empty screen was reported as a frozen one, and
      // nothing on the screen could contradict that. The counts alone cannot
      // either; "53 calls" reads the same a second later and an hour later.
      // What settles it is when the last call actually went out.
      h(Heartbeat, { key: 'heartbeat', now, snap })
    ),
    Card(
      { key: 'legend', note: 'Nine words, and what each one means.', title: 'Reading this list' },
      h(
        'div',
        { className: 'grid gap-x-6 gap-y-2 sm:grid-cols-2' },
        Object.entries(EVENT_LABELS).map(([kind, [label, tone]]) =>
          h(
            'div',
            { className: 'flex gap-2 text-xs', key: kind },
            h(
              'span',
              {
                className: cn(
                  'w-24 shrink-0',
                  tone === 'bad'
                    ? 'text-destructive'
                    : tone === 'warn'
                      ? 'text-amber-500'
                      : tone === 'good'
                        ? 'text-emerald-500'
                        : 'text-(--ui-text-secondary)'
                ),
                key: 'l'
              },
              label
            ),
            h('span', { className: 'text-(--ui-text-tertiary)', key: 'm' }, EVENT_MEANING[kind] ?? '')
          )
        )
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

// 1.6.0.0. The card that answers "is it even seeing my keys?".
//
// Every other card on this page describes what KAME *did*. None of them said
// what it is looking at, and that turned out to be the question behind every
// report: a profile where the key separation "doesn't seem to work", a
// provider that intermittently says the API key is wrong. Both are one line
// each — rows stored, keys after splitting, where they came from — and
// neither was anywhere on screen.
//
// Counts and origins only. The snapshot carries no key and no fragment of
// one, so there is nothing here that could leak by being rendered.
/** The other Hermes processes sharing this home, and this plugin.
 *
 *  A home is usually served by more than one: the Desktop you are looking at,
 *  and the gateway the phone app talks to. They load this plugin separately
 *  and they use the same credential pool — so a key the gateway is resting is
 *  a key this Hermes cannot use either, and a gateway still running an older
 *  build is why a fix that is definitely installed is definitely not working
 *  on half the traffic.
 *
 *  Renders nothing when this is the only one, which is the common case and
 *  should cost the page nothing. */
function Neighbours({ snap }) {
  // Subscribed before the early return so the hook order is stable — a
  // neighbour appearing must not change how many hooks this component ran.
  const now = useValue($now)
  const others = snap.neighbours ?? []

  if (!others.length) {
    return null
  }

  // `build` is an object — `{ version, fingerprint }` — so the fingerprint has
  // to be reached into on both sides. Comparing the objects rendered the
  // neighbour's build as "[object Object]" and made every neighbour look like
  // it was on a different build, which is the opposite of a useful warning.
  const mine = snap.build?.fingerprint
  const buildOf = other => other.build?.fingerprint ?? ''
  const behind = others.filter(other => buildOf(other) && mine && buildOf(other) !== mine)

  return Card(
    {
      key: 'neighbours',
      note:
        'They load this plugin separately and share your keys. A key one of them is resting is ' +
        'a key the others cannot use either.',
      title: `Also using these keys (${others.length})`
    },
    h(
      'div',
      { className: 'flex flex-col gap-2', key: 'rows' },
      ...others.map(other =>
        h(
          'div',
          {
            className:
              'flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 ' +
              'border-b border-(--ui-stroke-tertiary) pb-2 last:border-0 last:pb-0',
            key: String(other.pid)
          },
          h(
            'span',
            { className: 'text-xs text-(--ui-text-primary)', key: 'who' },
            other.role === 'gateway'
              ? 'the gateway — what the phone app talks to'
              : other.profile
                ? `another Desktop — profile ${other.profile}`
                : 'another Hermes'
          ),
          h(
            'span',
            { className: 'text-xs tabular-nums text-(--ui-text-tertiary)', key: 'keys' },
            `${other.totals?.ready ?? other.totals?.healthy ?? '?'} of ${
              other.totals?.keys ?? '?'
            } ready`
          ),
          // 1.6.0.2. Whether the plugin is doing anything over there.
          //
          // "5 of 5 ready" is a statement about keys, and it reads exactly the
          // same on a profile whose KAME never registered — the keys are fine
          // either way. A profile that would not answer was reported here, and
          // this row had no way to show that its plugin was inert. Calls, and
          // when the last one was, is the smallest fact that does.
          h(
            'span',
            {
              className: cn(
                'text-xs tabular-nums',
                other.installed === false ? 'text-amber-500' : 'text-(--ui-text-quaternary)'
              ),
              key: 'work'
            },
            other.installed === false
              ? 'KAME not installed there'
              : (() => {
                  const calls = Number(other.counters?.calls ?? 0)
                  const last = Number(other.counters?.last_call_at ?? 0)
                  if (!calls) return 'no calls yet'
                  const when = last ? `, last ${ago((now - last * 1000) / 1000)}` : ''
                  return `${calls} call${calls === 1 ? '' : 's'}${when}`
                })()
          ),
          h(
            'span',
            {
              className: cn(
                'font-mono text-[0.6875rem]',
                buildOf(other) && mine && buildOf(other) !== mine
                  ? 'text-amber-500'
                  : 'text-(--ui-text-quaternary)'
              ),
              key: 'build'
            },
            `v${other.build?.version ?? other.version ?? '?'} ${buildOf(other)}`
          )
        )
      )
    ),
    behind.length
      ? h(
          'p',
          { className: 'mt-2 text-xs text-amber-500', key: 'behind' },
          `${behind.length === 1 ? 'One of them is' : `${behind.length} of them are`} running a ` +
            'different build of KAME than this Hermes. They load the plugin at start-up, so the ' +
            'one that is behind will stay behind until it is restarted — and it is handling its ' +
            'own share of your traffic with the older code.'
        )
      : null
  )
}

function WhatItSees({ snap }) {
  const seen = snap.credentials ?? {}
  const rows = seen.providers ?? []
  return Card(
    {
      key: 'what-it-sees',
      note:
        'Rows are what Hermes stored; keys are what they turned out to be. More keys than rows means a field ' +
        'holding several keys was read as several keys.',
      title: 'What KAME can see'
    },
    !seen.readable
      ? h(
          'p',
          { className: 'text-sm text-(--ui-text-tertiary)', key: 'why' },
          seen.reason || 'The credential pool has not been read yet.'
        )
      : rows.length === 0
        ? h(
            'p',
            { className: 'text-sm text-(--ui-text-tertiary)', key: 'empty' },
            'No pool has been used yet this session. This fills in on the first call.'
          )
        : h(
            'div',
            { className: 'flex flex-col gap-2', key: 'rows' },
            rows.map(row =>
              h(
                'div',
                {
                  // A grid rather than a flex row with `justify-between`.
                  // The "resting" cell only exists on a provider that has one,
                  // so with flex the two rows below it had their columns in
                  // different places and the card read as a list of unrelated
                  // facts. Four fixed tracks; an absent cell leaves a gap
                  // instead of moving its neighbours.
                  className:
                    'grid grid-cols-[8rem_1fr_6rem_5rem] items-baseline gap-x-3 border-b ' +
                    'border-(--ui-stroke-tertiary) pb-2 last:border-0 last:pb-0',
                  key: row.provider
                },
                h(
                  'span',
                  { className: 'truncate font-mono text-xs text-(--ui-text-primary)', key: 'p' },
                  row.provider
                ),
                h(
                  'span',
                  { className: 'text-xs text-(--ui-text-secondary)', key: 'n' },
                  row.rows === row.keys
                    ? `${row.keys} key${row.keys === 1 ? '' : 's'}`
                    : `${row.rows} row${row.rows === 1 ? '' : 's'} → ${row.keys} keys`
                ),
                h(
                  'span',
                  { className: 'text-xs text-(--ui-text-tertiary)', key: 'b' },
                  row.benched ? `${row.benched} resting` : ''
                ),
                h(
                  'span',
                  {
                    className:
                      'truncate text-right font-mono text-[0.6875rem] text-(--ui-text-quaternary)',
                    key: 'o',
                    title: `from ${row.origin || '?'}`
                  },
                  `from ${row.origin || '?'}`
                )
              )
            )
          ),
    seen.readable && rows.length
      ? h(
          'p',
          { className: 'text-xs text-(--ui-text-quaternary)', key: 'note' },
          seen.splitting
            ? 'Splitting is on: a field holding a comma-separated list is read as the several keys it is.'
            : 'Splitting is off, so a field holding several keys is sent to the provider whole and refused.'
        )
      : null
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
      onClick: () => {
        // Changing tab closes the inspector: the row it belongs to is about to
        // leave the screen, and an overlay outliving its subject is a panel
        // the user has to dismiss before they can use what they clicked.
        $inspect.set(null)
        $tab.set(id)
      },
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
    h(
      'span',
      { key: 'ready' },
      // `ready`, not `healthy`. A credential the provider rejected stops being
      // benched after an hour and went back to counting as ready — on the same
      // screen as a banner saying waiting will not repair one. The two numbers
      // contradicted each other and the large one was wrong.
      snap.installed
        ? `${totals.ready ?? totals.healthy} of ${totals.keys} keys ready`
        : 'not rotating'
    ),
    totals.rejected
      ? h(
          'span',
          { className: 'text-(--ui-red)', key: 'rejected' },
          `${totals.rejected} to replace`
        )
      : null,
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
          h('span', { className: 'text-xs text-(--ui-text-quaternary)', key: 'version' }, `v${snap.version}`),
        // The build fingerprint, beside the version and deliberately not
        // instead of it. A version string is written by whoever last edited
        // the manifest and survives a copy that dropped half the package —
        // that is exactly how an install claiming 1.3.3 ran for nine days with
        // no engine. This number is computed from the bytes on disk, so it
        // cannot describe a file that is not there, and it is the one a deploy
        // compares against what it just built.
        snap?.build?.fingerprint &&
          h(
            'span',
            {
              className: 'font-mono text-[0.625rem] text-(--ui-text-quaternary)',
              title: 'Fingerprint of the source actually installed. Compare it with the build you deployed.',
              key: 'build'
            },
            snap.build.fingerprint
          )
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
  const retired = retiredCount(snap)
  const build = snap.build ?? {}

  return h(
    'div',
    { className: 'flex h-full w-full flex-col gap-4 overflow-y-auto p-6' },

    header(h(HeaderStatus, { key: 'status', snap })),

    // 1.4.0. The loudest thing on the page, and the only one that is allowed
    // to be, because it is the failure that cost the most: an install whose
    // `core/` package was not on disk registered cleanly, published
    // `installed: true, reason: "active"`, rotated nothing for nine days, and
    // showed a version number the whole time. Every counter on this page was
    // zero and none of them said why.
    //
    // A plugin cannot fix its own missing files. What it can do is refuse to
    // look healthy.
    build.complete === false
      ? h(
          'div',
          {
            className:
              'rounded border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive',
            key: 'incomplete'
          },
          h(
            'p',
            { className: 'font-medium', key: 'headline' },
            'This install is incomplete — KAME is not rotating.'
          ),
          h(
            'p',
            { className: 'mt-1 text-xs opacity-90', key: 'missing' },
            `Missing: ${(build.missing ?? []).join(', ') || 'unknown'}`
          ),
          h(
            'p',
            { className: 'mt-1 text-xs opacity-90', key: 'fix' },
            'Re-deploy the whole plugin directory. A copy that does not recurse drops core/ ' +
              'and leaves a plugin that registers, reports itself active, and does nothing.'
          )
        )
      : null,

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

    // At the page root rather than inside the events list: an overlay drawn
    // inside a scrolling container is clipped by it, and this one has to sit
    // over the whole panel. It renders nothing until a row is opened.
    h(PayloadInspector, { key: 'inspector' }),

    // 1.6.0.1 split this in two, because the old wording ("until then every
    // turn spends an attempt discovering the same thing") stopped being true:
    // a key that has left rotation is not spending anything. What it is doing
    // is waiting for a person, and that is what the banner should say.
    retired > 0 &&
      Note(
        `${count(retired, 'key', 'keys', 'has', 'have')} left rotation — the provider refused ` +
          `${retired === 1 ? 'it' : 'them'} as ${retired === 1 ? 'a credential' : 'credentials'}, ` +
          `so KAME no longer offers ${retired === 1 ? 'it' : 'them'} and nothing is being spent on ` +
          `${retired === 1 ? 'it' : 'them'}. Nothing was deleted: paste the replacement over ` +
          `${retired === 1 ? 'it' : 'them'} in Settings and ${retired === 1 ? 'it comes' : 'they come'} ` +
          'back by itself. Your other keys are carrying every turn meanwhile.',
        'bad',
        'retired'
      ),

    invalid > retired &&
      Note(
        `${count(invalid - retired, 'key was', 'keys were')} just refused and ${
          invalid - retired === 1 ? 'is' : 'are'
        } still being tried. ` +
          'One refusal is not proof — an expired token a second from refreshing sends the same thing — ' +
          `so it takes ${REFUSALS_BEFORE_RETIRING} in a row, with nothing working in between.`,
        'plain',
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

            h(Neighbours, { key: 'neighbours', snap }),

            h(WhatItSees, { key: 'what-it-sees', snap }),

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
              counters.tool_call_retries
                ? Field(
                    'Tool call re-asked',
                    counters.tool_call_retries,
                    'the call was dropped before anything was shown, so another key was asked for it'
                  )
                : null,
              counters.tool_call_cuts
                ? Field('Cut in a tool call', counters.tool_call_cuts, 'a half-written call cannot be continued')
                : null,
              counters.blamed_another_key
                ? Field(
                    'Blamed another key',
                    counters.blamed_another_key,
                    'a cooldown was written against a key that was not the one the request carried — report this'
                  )
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
