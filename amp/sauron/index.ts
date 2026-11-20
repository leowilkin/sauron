// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Amp plugin: Sauron's adapter for Amp.
//
// Install as a system plugin, so it runs only on this computer and logs to its
// ~/.ai-tracker, like the Claude Code plugin:
//
//   ln -s ~/sauron/amp/sauron ~/.config/amp/plugins/sauron
//
// Each Amp event is sent as one JSON payload to hooks/run.sh amp, which runs
// tracker/amp.py. That counts characters and lines and appends a counts-only
// event to the log. Events go one at a time, in order, and never block Amp.
// Prompt text, edits and replies are passed to Python only to be counted.

import type { PluginAPI, ThreadMessage } from '@ampcode/plugin'
import { spawn } from 'node:child_process'
import { realpathSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const EDIT_TOOLS = new Set(['edit_file', 'create_file', 'apply_patch'])

function lastAssistantText(messages: ThreadMessage[]): string {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i]
		if (m?.role !== 'assistant') continue
		const text = m.content.flatMap((b) => (b.type === 'text' ? [b.text] : [])).join('\n')
		if (text.trim()) return text
	}
	return ''
}

export default function (amp: PluginAPI) {
	if (process.env.AI_TRACKER_DISABLE === '1') return
	// realpath: the plugin is usually a symlink into the sauron checkout
	const runSh = join(dirname(realpathSync(fileURLToPath(import.meta.url))), '..', '..', 'hooks', 'run.sh')
	const root = amp.system.workspaceRoot
	const cwd = root ? amp.helpers.filePathFromURI(root) : process.cwd()
	const turn = new Map<string, string>() // thread → message ID of the prompt it's working on
	let queue: Promise<void> = Promise.resolve()

	const run = (payload: Record<string, unknown>) =>
		new Promise<void>((resolve) => {
			try {
				const p = spawn('bash', [runSh, 'amp'], { stdio: ['pipe', 'ignore', 'ignore'] })
				p.on('error', () => resolve())
				p.on('close', () => resolve())
				p.stdin.on('error', () => {})
				p.stdin.end(JSON.stringify({ cwd, ...payload }))
			} catch {
				resolve()
			}
		})
	const send = (payload: Record<string, unknown>) => {
		queue = queue.then(() => run(payload))
	}

	amp.on('session.start', (event) => {
		send({ event: 'session_start', session: event.thread.id })
	})

	amp.on('agent.start', async (event, ctx) => {
		turn.set(event.thread.id, event.id)
		// A child thread's prompts come from the agent that started it, not from you.
		let origin: string | undefined
		try {
			if (await ctx.thread.parentThreadID()) origin = 'agent'
		} catch {}
		send({ event: 'prompt', session: event.thread.id, prompt_id: event.id, prompt: event.message, origin })
		return {}
	})

	amp.on('tool.result', (event) => {
		if (event.status !== 'done' || !EDIT_TOOLS.has(event.tool)) return
		const files = amp.helpers.filesModifiedByToolCall(event) ?? []
		if (!files.length) return
		send({
			event: 'edit',
			session: event.thread.id,
			prompt_id: turn.get(event.thread.id),
			tool: event.tool,
			input: event.input,
			paths: files.map((f) => amp.helpers.filePathFromURI(f)),
		})
	})

	amp.on('agent.end', (event) => {
		send({
			event: 'done',
			session: event.thread.id,
			prompt_id: event.id,
			status: event.status,
			text: lastAssistantText(event.messages),
		})
	})

	// Let queued events reach the log before the plugin process exits.
	amp.onDispose(() => Promise.race([queue, new Promise<void>((r) => setTimeout(r, 2500))]))
}
