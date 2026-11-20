// SPDX-License-Identifier: AGPL-3.0-or-later
// Run: claude plugin test ~/ai-tracker
import { expect, test } from 'claude-code/testing'
import { DraftTracker, MAX_STEPS, shapeOf, type Edit } from './draft'

// Helpers that describe edits the way prompt.edit reports them.
let len = 0
const type = (n = 1, at = len): Edit => {
  const e = { textLength: len, start: at, end: at, inserted: n, hasKey: true, hasNewline: false }
  len += n
  return e
}
const backspace = (n = 1): Edit => {
  const e = { textLength: len, start: len - n, end: len, inserted: 0, hasKey: true, hasNewline: false }
  len -= n
  return e
}
const paste = (n: number): Edit => {
  const e = { textLength: len, start: len, end: len, inserted: n, hasKey: false, hasNewline: true }
  len += n
  return e
}
const clearAll = (): Edit => {
  const e = { textLength: len, start: 0, end: len, inserted: 0, hasKey: true, hasNewline: false }
  len = 0
  return e
}

test('typing time sums the gaps between keys and leaves out long pauses', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 1000
  for (let i = 0; i < 10; i++) d.edit(type(), (t += 200)) // 10 keys, 200ms apart
  d.edit(type(), (t += 45_000)) // a 45s pause
  d.edit(type(), (t += 300))
  const s = d.submit((t += 100))
  expect(s.keys_typed).toBe(12)
  expect(s.typing_ms).toBe(9 * 200 + 300)
  expect(s.pauses).toBe(1)
  expect(s.compose_ms).toBe(t - 1200)
  expect(s.final_length).toBe(12)
  expect(s.kind).toBe('sent')
})

test('short deletion bursts are fixes, long ones are rewrites', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 0
  for (let i = 0; i < 30; i++) d.edit(type(), (t += 100))
  d.edit(backspace(), (t += 100)) // burst of 2: a fix
  d.edit(backspace(), (t += 100))
  d.edit(type(), (t += 100))
  for (let i = 0; i < 12; i++) d.edit(backspace(), (t += 50)) // burst of 12: a rewrite
  d.edit(type(), (t += 100))
  const s = d.submit((t += 100))
  expect(s.fixes).toBe(1)
  expect(s.rewrites).toBe(1)
  expect(s.chars_deleted).toBe(14)
})

test('typing before the end of the draft is a revision; a big paste is not typing', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 0
  for (let i = 0; i < 5; i++) d.edit(type(), (t += 100))
  d.edit(type(1, 2), (t += 100)) // inserted in the middle
  d.edit(paste(400), (t += 100))
  const folded = { textLength: len, start: len, end: len, inserted: 3, hasKey: false, hasNewline: false }
  len += 3
  d.edit(folded, (t += 100)) // fast keys folded into one edit
  const s = d.submit((t += 100))
  expect(s.revisions).toBe(1)
  expect(s.pastes).toBe(1)
  expect(s.keys_typed).toBe(9)
})

test('clearing a typed draft abandons it; a stray key does not count', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 0
  for (let i = 0; i < 20; i++) d.edit(type(), (t += 100))
  const gone = d.edit(clearAll(), (t += 100))
  expect(gone?.kind).toBe('abandoned')
  expect(gone?.keys_typed).toBe(20)
  expect(d.active).toBe(false)

  d.edit(type(), (t += 100))
  expect(d.edit(backspace(), (t += 100))).toBe(null) // 1 key then cleared: not a draft
  expect(d.submit((t += 100)).keys_typed).toBe(0) // nothing typed: zeros
})

test('a draft left in the box at session end is abandoned', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 0
  for (let i = 0; i < 8; i++) d.edit(type(), (t += 100))
  const left = d.abandon((t += 100))
  expect(left?.kind).toBe('abandoned')
  expect(left?.keys_typed).toBe(8)
  expect(d.abandon(t)).toBe(null)
})

test('peek shows the live draft, counting a deletion burst still in progress', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 0
  expect(d.peek(t)).toBe(null)
  for (let i = 0; i < 20; i++) d.edit(type(), (t += 100))
  for (let i = 0; i < 3; i++) d.edit(backspace(), (t += 100))
  const p = d.peek(t)
  expect(p?.keys_typed).toBe(20)
  expect(p?.fixes).toBe(1)
  expect(p?.rewrites).toBe(0)
  expect(p?.typing_ms).toBe(22 * 100)
  for (let i = 0; i < 9; i++) d.edit(backspace(), (t += 100)) // same burst grows to 12
  expect(d.peek(t)?.fixes).toBe(0)
  expect(d.peek(t)?.rewrites).toBe(1)
})

test('shapeOf keeps layout and drops the characters', async () => {
  expect(shapeOf('fix the bug\nnow')).toBe('3a_3a_3an3a')
  expect(shapeOf('')).toBe('')
  expect(shapeOf('a  b')).toBe('a2_a')
  expect(shapeOf('héllo wörld')).toBe('5a_5a')
})

test('a sent draft carries its replay; an abandoned one does not', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 1000
  d.edit({ ...type(3), shape: '3a' }, (t += 0))
  d.edit({ ...type(1), shape: '_' }, (t += 150))
  d.edit(backspace(), (t += 200))
  const s = d.submit((t += 50))
  expect(s.replay).toEqual({ initial: '', steps: [[0, 0, 0, '3a'], [150, 3, 3, '_'], [350, 3, 4, '']] })

  d.edit({ ...type(5), shape: '5a' }, (t += 100))
  const gone = d.abandon((t += 100))
  expect(gone?.replay).toBe(undefined)
})

test('a huge draft stops recording its replay but keeps counting', async () => {
  len = 0
  const d = new DraftTracker()
  let t = 0
  for (let i = 0; i < MAX_STEPS + 10; i++) d.edit({ ...type(), shape: 'a' }, (t += 10))
  const s = d.submit(t)
  expect(s.replay?.truncated).toBe(true)
  expect(s.replay?.steps.length).toBe(MAX_STEPS)
  expect(s.keys_typed).toBe(MAX_STEPS + 10)
})
