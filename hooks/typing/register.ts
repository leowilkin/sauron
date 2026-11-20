// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Claude Code module: typing stats for prompts you write in the terminal.
//
// Keeps the current draft's counters in memory (keystrokes never touch the
// disk). When a typed prompt is sent, or a draft is given up, it writes one
// small JSON file of counts to <data dir>/typing/<session id>/. The Python
// UserPromptSubmit / SessionEnd hooks fold those files into prompt_sent /
// session_end events in the hash-chained log and delete them.
//
// Never stores text. Never blocks or changes an edit or a prompt.

import type { Hook, Register } from 'claude-code'
import { DraftTracker, shapeOf, type DraftStats } from './draft'

type Engine = Parameters<Hook<'prompt.submit'>>[0]

const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/

const draft = new DraftTracker()
let folder: string | null | undefined // undefined: not looked up yet; null: tracking off

async function where($: Engine): Promise<string | null> {
  if (folder !== undefined) return folder
  if ((await $.env.get('AI_TRACKER_DISABLE')) === '1') return (folder = null)
  // Same folder Python's eventlog.data_dir() picks: AI_TRACKER_DIR, else ~/.ai-tracker,
  // where ~ is USERPROFILE on Windows and HOME elsewhere.
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  let base = await $.env.get('AI_TRACKER_DIR')
  if (base && home && (base === '~' || base.startsWith('~/') || base.startsWith('~\\'))) base = home + base.slice(1)
  if (!base) base = home ? `${home}/.ai-tracker` : undefined
  const sid = await $.session.id()
  if (!base || !SESSION_ID.test(sid)) return (folder = null)
  return (folder = `${base}/typing/${sid}`)
}

async function save($: Engine, s: DraftStats): Promise<void> {
  try {
    const dir = await where($)
    if (dir) await $.fs.write(`${dir}/${s.at}-${s.kind}.json`, JSON.stringify({ v: 1, ...s }))
  } catch {
    // never get in the way of the person typing
  }
}

function clock(ms: number): string {
  const s = Math.round(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : word.endsWith('x') ? 'es' : 's'}`
}

/** "84 keys · 3 fixes · 1 rewrite", leaving out what's zero. */
function counts(s: { keys_typed: number; fixes: number; rewrites: number; revisions: number }): string {
  const bits = [plural(s.keys_typed, 'key')]
  if (s.fixes) bits.push(plural(s.fixes, 'fix'))
  if (s.rewrites) bits.push(plural(s.rewrites, 'rewrite'))
  if (s.revisions) bits.push(plural(s.revisions, 'revision'))
  return bits.join(' · ')
}

let showStatus: boolean | undefined

async function status($: Engine, text: string | undefined): Promise<void> {
  try {
    // Off unless asked for: a live count of your fixes changes how you type.
    if (showStatus === undefined) showStatus = (await $.env.get('AI_TRACKER_STATUS')) === 'on'
    if (showStatus) $.ui.status(text)
  } catch {
    // display is optional
  }
}

export const register: Register = on => {
  on('prompt.edit', ($, e, next) => {
    try {
      const gaveUp = draft.edit(
        {
          textLength: e.text.length,
          start: e.start,
          end: e.end,
          inserted: e.inputText.length,
          hasKey: e.key !== undefined,
          hasNewline: e.inputText.includes('\n'),
          shape: shapeOf(e.inputText),
          initialShape: draft.active ? undefined : shapeOf(e.text),
        },
        Date.now(),
      )
      if (gaveUp) {
        void save($, gaveUp)
        void status($, `draft cleared · ${counts(gaveUp)} not sent`)
      } else {
        const live = draft.peek(Date.now())
        if (live) void status($, `typing ${clock(live.typing_ms)} · ${counts(live)}`)
      }
    } catch {
      // ignore: counting is best-effort
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    try {
      if (e.origin.kind === 'composer' && !e.isMeta) {
        const sent = draft.submit(Date.now())
        // Slash commands aren't prompts to the AI: reset the draft, keep nothing.
        if (!e.command) {
          await save($, sent)
          if (sent.keys_typed > 0) {
            void status($, `last prompt: ${clock(sent.typing_ms)} typing over ${clock(sent.compose_ms)}` +
              ` · ${counts(sent)}${sent.pauses ? ` · ${plural(sent.pauses, 'pause')}` : ''}`)
          }
        }
      }
    } catch {
      // ignore
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    try {
      const left = draft.abandon(Date.now())
      if (left) await save($, left)
    } catch {
      // ignore
    }
    folder = undefined // /clear starts a new session id
    return next(e)
  })
}
