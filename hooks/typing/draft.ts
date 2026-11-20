// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Turns prompt-box edits into typing stats. Pure logic, no engine calls, so it
// can be tested directly. Nothing here keeps text: each edit is reduced to how
// many characters went in and out, where, and when.

/**
 * Layout of some text with the characters themselves dropped: `a` for any
 * visible character, `_` for a space or tab, `n` for a newline, run-length
 * encoded (`5a_3an2a`). Enough to replay how a prompt took shape, not what it said.
 */
export function shapeOf(text: string): string {
  let out = ''
  let prev = ''
  let run = 0
  const flush = (): void => {
    if (run) out += (run > 1 ? String(run) : '') + prev
  }
  for (const ch of text) {
    const c = ch === '\n' ? 'n' : ch === ' ' || ch === '\t' || ch === '\r' ? '_' : 'a'
    if (c === prev) {
      run++
    } else {
      flush()
      prev = c
      run = 1
    }
  }
  flush()
  return out
}

/** One replay step: [ms since the draft's first edit, start, end, shape of what went in]. */
export type Step = [number, number, number, string]

export type Replay = {
  /** Shape of what was already in the box when the draft started (history recall, say). */
  initial: string
  steps: Step[]
  /** Set when the draft was too long to record whole; the stats are still complete. */
  truncated?: true
}

/** One edit, as `prompt.edit` describes it (lengths only are used). */
export type Edit = {
  /** Length of the draft before the edit. */
  textLength: number
  /** Where the replaced span starts and ends in the draft before the edit. */
  start: number
  end: number
  /** How many characters went in. */
  inserted: number
  /** Whether a single key made the edit (absent for pastes and folded bursts). */
  hasKey: boolean
  /** Whether what went in contains a newline. */
  hasNewline: boolean
  /** shapeOf(what went in), for the replay. */
  shape?: string
  /** shapeOf(the draft before the edit); only needed when the draft starts. */
  initialShape?: string
}

export type DraftStats = {
  kind: 'sent' | 'abandoned'
  /** ms since epoch when the draft was sent or given up */
  at: number
  /** Active typing: gaps between edits, gaps over PAUSE_MS left out. */
  typing_ms: number
  /** Wall clock from the first edit to send / give-up, pauses included. */
  compose_ms: number
  /** Characters typed (not pasted). */
  keys_typed: number
  /** Paste events (their size is already in the prompt's pasted_chars). */
  pastes: number
  /** Deletion bursts under REWRITE_CHARS: typo fixes. */
  fixes: number
  /** Deletion bursts of REWRITE_CHARS or more, or a replaced selection that big. */
  rewrites: number
  /** Typed insertions before the end of the draft: going back to change earlier text. */
  revisions: number
  /** Gaps between edits longer than PAUSE_MS. */
  pauses: number
  chars_deleted: number
  /** Final draft length (sent only). */
  final_length: number
  /** Layout-only replay of the draft (sent only). */
  replay?: Replay
}

export const PAUSE_MS = 30_000
export const REWRITE_CHARS = 10
/** A key-less insertion this short with no newline is fast typing folded into one edit, not a paste. */
export const BURST_MAX = 8
/** A cleared draft shorter than this wasn't really a draft (a stray key). */
export const MIN_ABANDONED_KEYS = 3
/** Replay limits: past these the replay stops (marked truncated); counting goes on. */
export const MAX_STEPS = 5000
export const MAX_SHAPE = 20000

type Live = Omit<DraftStats, 'kind' | 'at' | 'compose_ms' | 'final_length'> & {
  startedAt: number
  lastAt: number
  burstDeleted: number
  length: number
  replay: Replay
  shapeBudget: number
}

export class DraftTracker {
  private d: Live | null = null

  get active(): boolean {
    return this.d !== null
  }

  /** Record one edit. Returns the stats of a draft the edit cleared, if it counts as abandoned. */
  edit(e: Edit, now: number): DraftStats | null {
    const removed = Math.max(0, e.end - e.start)
    const after = e.textLength - removed + e.inserted

    if (this.d === null) {
      if (after === 0) return null // cursor moves / deletes in an empty box
      this.d = {
        startedAt: now, lastAt: now, burstDeleted: 0, length: e.textLength,
        replay: { initial: e.textLength ? (e.initialShape ?? `${e.textLength}a`) : '', steps: [] },
        shapeBudget: MAX_SHAPE,
        typing_ms: 0, keys_typed: 0, pastes: 0, fixes: 0, rewrites: 0,
        revisions: 0, pauses: 0, chars_deleted: 0,
      }
    } else {
      const gap = now - this.d.lastAt
      if (gap > PAUSE_MS) this.d.pauses++
      else this.d.typing_ms += gap
      this.d.lastAt = now
    }
    const d = this.d

    if (removed > 0 || e.inserted > 0) this.record(d, now, e, removed)

    const isDeletion = removed > 0 && e.inserted === 0
    if (isDeletion) {
      d.burstDeleted += removed
    } else {
      this.closeBurst()
    }
    d.chars_deleted += removed

    if (e.inserted > 0) {
      const isPaste = !e.hasKey && (e.inserted > BURST_MAX || (e.hasNewline && e.inserted > 1))
      if (isPaste) {
        d.pastes++
      } else {
        d.keys_typed += e.inserted
        if (e.start < e.textLength) d.revisions++
      }
      if (removed > 0) {
        // typed or pasted over a selection
        if (removed >= REWRITE_CHARS) d.rewrites++
        else d.fixes++
      }
    }

    d.length = after
    if (after === 0) {
      // the whole draft was cleared without sending
      const out = this.finish('abandoned', now)
      return out.keys_typed >= MIN_ABANDONED_KEYS ? out : null
    }
    return null
  }

  /** The live draft's counts so far, for display; null when the box is empty. */
  peek(now: number): Pick<DraftStats, 'typing_ms' | 'compose_ms' | 'keys_typed' | 'fixes' | 'rewrites' | 'revisions' | 'pauses'> | null {
    const d = this.d
    if (d === null) return null
    // A deletion burst still in progress counts as what it would close as.
    const open = d.burstDeleted
    return {
      typing_ms: d.typing_ms,
      compose_ms: Math.max(0, now - d.startedAt),
      keys_typed: d.keys_typed,
      fixes: d.fixes + (open > 0 && open < REWRITE_CHARS ? 1 : 0),
      rewrites: d.rewrites + (open >= REWRITE_CHARS ? 1 : 0),
      revisions: d.revisions,
      pauses: d.pauses,
    }
  }

  /** The draft was sent. Returns its stats (zeros if nothing was typed in the box). */
  submit(now: number): DraftStats {
    if (this.d === null) {
      return {
        kind: 'sent', at: now, typing_ms: 0, compose_ms: 0, keys_typed: 0, pastes: 0,
        fixes: 0, rewrites: 0, revisions: 0, pauses: 0, chars_deleted: 0, final_length: 0,
      }
    }
    return this.finish('sent', now)
  }

  /** The session is ending: a draft still in the box counts as abandoned. */
  abandon(now: number): DraftStats | null {
    if (this.d === null) return null
    const out = this.finish('abandoned', now)
    return out.keys_typed >= MIN_ABANDONED_KEYS ? out : null
  }

  private record(d: Live, now: number, e: Edit, removed: number): void {
    const r = d.replay
    if (r.truncated) return
    const shape = e.inserted ? (e.shape ?? `${e.inserted}a`) : ''
    d.shapeBudget -= shape.length
    if (r.steps.length >= MAX_STEPS || d.shapeBudget < 0) {
      r.truncated = true
      return
    }
    r.steps.push([now - d.startedAt, e.start, e.start + removed, shape])
  }

  private closeBurst(): void {
    const d = this.d
    if (d === null || d.burstDeleted === 0) return
    if (d.burstDeleted >= REWRITE_CHARS) d.rewrites++
    else d.fixes++
    d.burstDeleted = 0
  }

  private finish(kind: DraftStats['kind'], now: number): DraftStats {
    this.closeBurst()
    const d = this.d!
    this.d = null
    return {
      kind,
      at: now,
      typing_ms: d.typing_ms,
      compose_ms: Math.max(0, now - d.startedAt),
      keys_typed: d.keys_typed,
      pastes: d.pastes,
      fixes: d.fixes,
      rewrites: d.rewrites,
      revisions: d.revisions,
      pauses: d.pauses,
      chars_deleted: d.chars_deleted,
      final_length: kind === 'sent' ? d.length : 0,
      ...(kind === 'sent' ? { replay: d.replay } : {}),
    }
  }
}
