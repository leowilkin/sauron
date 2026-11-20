# Sauron

(The plugin and data folder are still called `ai-tracker`.)

Passive, privacy-friendly logging of AI coding sessions. This is the first building block of an AI time tracker for Hack Club.

A Claude Code plugin logs every prompt → response cycle and every AI file edit to a local, append-only, hash-chained JSONL file. It stores counts and timestamps only. **No prompt text, file contents or model output is ever written.**

```
2026-11-18  greg
  time           36m   (you 24m, AI alone 12m)
  prompts           5   (880 chars typed)
  AI cycles         5   median AI response   4m 00s
  your replies         median 7m 00s, reading needs ~2m 10s   (1 faster than reading, 1 break)
  AI lines       +200 / -13 across 2 files
    src/motion.c   +140   -3
    src/physics.c  +60    -10

2026-11-18  site
  time           19m   (you 7m, AI alone 12m)
  ...

2026-11-18  total 55m across 2 projects   (you 31m, AI alone 24m)
```

## Install (the easy way)

Paste this into Claude Code (or any coding agent):

> Set up ai-tracker for me by following https://github.com/2Mon/sauron/blob/main/SETUP.md. Ask me before installing anything or sending any of my data anywhere.

The agent checks for Python, installs the plugin, verifies it's logging, imports your recent history, offers the optional Jev struggle estimate (asking first, since it sends prompt text), and shows you the dashboard. [SETUP.md](SETUP.md) is written for the agent, but it's readable by people too.

## Install (by hand)

Requires Claude Code and Python 3.8+ on `PATH`. No packages are needed.

```sh
git clone https://github.com/2Mon/sauron ~/sauron
mkdir -p ~/.local/bin && ln -sf ~/sauron/bin/sauron ~/.local/bin/sauron   # the `sauron` command
claude plugin marketplace add ~/sauron
claude plugin install ai-tracker@ai-tracker
```

Restart Claude Code, or run `/reload-plugins`. Logging starts immediately and needs no further setup. Because it's a plugin, your existing `settings.json` hooks are left untouched.

To try it without installing, run `claude --plugin-dir ~/sauron`.

**Uninstall:** `claude plugin uninstall ai-tracker@ai-tracker`. The log stays in `~/.ai-tracker/` until you delete it.

**Pause:** set `AI_TRACKER_DISABLE=1`. **Move the log:** set `AI_TRACKER_DIR=/some/dir`.

## Read your stats

```sh
sauron summarize              # everything, per project per day
sauron summarize --days 7 --project greg
sauron summarize --replies    # + every reply gap vs its read time
sauron summarize --idle 10    # stricter break cutoff
sauron summarize --json       # for scripts / a future server
sauron verify                 # check the record links, print the head
```

On Windows, use `python` or `py -3` instead of `python3`.

### Dashboard

```sh
sauron serve                  # your live log + imported history
sauron serve ~/ai-data        # + every .jsonl in a folder (friends' imports)
```

This opens `http://127.0.0.1:8765/`, a single local page with no external libraries, so it works offline. It shows:

- headline numbers
- time per day, stacked into you and AI alone
- time by project
- each reply's time against its read time, on a log-scale scatter with the fast zone shaded
- a table of every day and project

Hovering over anything gives a tooltip. Use the **Data** dropdown to switch logs; `?source=N` in the URL selects one directly. The page reloads its data every 15 seconds, so the live log updates while you work.

The server listens on localhost only, and the page picks logs by their number in the list, never by a file path.

### Presence (opt-in)

To measure how much of each gap between prompts you were actually at the computer, turn on the presence sampler:

```sh
echo '{"presence": true}' > ~/.ai-tracker/config.json
```

It starts with your next Claude Code prompt, runs one copy at a time, and exits after 30 minutes without Claude Code activity. Every 5 seconds it records **keyboard/mouse idle time** and **the kind of app in front**: terminal, editor, browser, AI app, other, or locked. **It never stores app names, window titles or anything on screen.** The data stays in `~/.ai-tracker/presence/` (a line per change, plus a heartbeat each minute), at about 25 ms per sample.

```sh
sauron presence     # what presence saw in each reply gap vs. what the model credited
```

To turn it off, set `"presence": false` (it stops within a few seconds) or delete the config file.

Supported on macOS (`ioreg`, `lsappinfo`) and Windows (Win32 calls). On Linux it needs X11 with `xprintidle` and `xdotool`/`xprop`; Wayland desktops don't expose the front app. Only macOS has been tested so far.

### Publish a snapshot

```sh
sauron export-site ~/ai-data --out ./site
```

This writes `site/index.html` and `site/data.json`, the same dashboard working from a frozen copy of the data, for hosting anywhere static, such as a claude.ai Artifact. File paths, session ids and prompt ids are removed. Project names, timings, counts and layout-only replays stay, so check with anyone whose data is included before sharing the link widely.

### Research: label prompts with Jev

`tracker/jev_judge.py` asks Jev (TypeSafe's classification model, via Hack Club AI) what each prompt in a set of transcripts is doing: directing, delegating, reviewing the AI's work, handing errors back, asking to understand, and so on. It also scores how much of the person's own thinking the prompt shows. Jev sees **text only**, never the tracker's timing or size signals, so its labels can be compared with those signals without circularity.

```sh
JEV_API_KEY=... sauron jev ./their-transcripts --out ~/ai-data/jev/NAME.jsonl
```

**This sends prompt text to ai.hackclub.com, and from there to TypeSafe.** Use it only with the transcript owner's agreement. The output file holds labels and ids only. Place it in `jev/` next to the matching imported log (same file name), and `aitrack serve` adds a **prompt patterns** section for that log. This is a research tool for working out what productive struggle looks like. It's not part of the passive tracker.

### Backfill history from Claude Code transcripts

Claude Code keeps every past session in `~/.claude/projects/`. You can turn those into events and get stats for the time before you installed the tracker:

```sh
sauron import                     # ~/.claude/projects → ~/.ai-tracker/imported.jsonl
sauron --file ~/.ai-tracker/imported.jsonl summarize
sauron import ./someones-export --out their.jsonl --utc-offset -5
```

- Imported events go to **a separate file, never the live log**, with `"source": "claude-code-import"`. Transcripts are editable, so imported hours aren't evidence of anything.
- They contain only counts and timestamps, like the hooks. To share data for calibration, send the `summarize --json` output, not your transcripts.
- Read times are more accurate than live ones, because the import counts all text the AI wrote in a turn, not just the final message.
- On this session, the import matched the live hooks exactly for prompts (characters and timestamps) and edits (files and line counts).
- The transcript format is internal to Claude Code and undocumented. The importer targets 2.1.3xx and skips records it doesn't recognize.
- When the original paths don't exist on this machine, the project is the `cwd` folder name, because there's no `.git` to find.

| Metric | How it's computed |
|---|---|
| time | Credited time, split into **you** and **AI alone**. See [Time model](#time-model). |
| prompts / chars typed | `prompt_sent` count and `chars`. Text inside Claude Code's `<pasted_content>` blocks is counted separately as `pasted_chars`. |
| AI cycles, median AI response | Each `prompt_sent` paired with the next `ai_done` in the same session (matched on `prompt_id` when both have one). A prompt followed by another prompt with no `ai_done` in between counts as **interrupted**. |
| your replies | Median of `ai_done` → next `prompt_sent` in the same session, shown next to the median **read time** of what the AI produced (see below). **Faster than reading** counts replies sent in under ¼ of a read time of at least 30 s, for example a 5-second reply to a 600-line diff. Gaps longer than `max(--idle, read time)` are **breaks** and are left out of the median. |
| AI lines per file | Sum of `lines_added` / `lines_removed` from `ai_edit`. |

Days use the local date recorded in each event's `ts`.

When the range covers more than one day, an **All days** block follows: total time and the share that was AI alone, prompts, AI turns (median, longest, interrupted), replies against read time, and AI lines.

`--replies` lists every reply gap next to its read time, with the ratio between them and a `FAST` or `BREAK` tag. That's the quickest way to check whether the read speeds and the idle cutoff fit how someone actually works. With `--json`, each row also gets a `replies` array.

```
Replies: time from the AI finishing to your next prompt, vs. estimated read time
  2026-10-08 01:12  onshape-dovetail-thing     2m 28s  vs      32s    4.6x
  2026-10-10 01:48  claude-proxy                   7s  vs      42s    0.2x  FAST
  2026-10-06 13:00  onshape-dovetail-thing    36h 07m  vs   1h 00m   36.1x  BREAK
```

## Typing (Claude Code terminal)

A Claude Code plugin module (`hooks/typing/`) watches each edit in the prompt box and keeps the current draft's **counters** in memory. It never keeps the text, and keystrokes never touch the disk. When you send a typed prompt, `prompt_sent` gets these fields:

| Field | Meaning |
|---|---|
| `typing_ms` | Active typing: the gaps between edits, with gaps over 30 s left out |
| `compose_ms` | Wall-clock time from the first keystroke to Enter, pauses included |
| `keys_typed` | Characters typed (pastes aren't counted; their size is `pasted_chars`) |
| `pastes` | Paste events |
| `fixes` / `rewrites` | Deletion bursts under or over 10 characters |
| `revisions` | Typing before the end of the draft: going back to change earlier text |
| `pauses` | Gaps over 30 s between edits |
| `chars_deleted` | Characters deleted |
| `drafts_abandoned`, `abandoned_typing_ms` | Drafts you cleared without sending since the last prompt (also on `session_end`) |
| `replay_hash` | SHA-256 of this prompt's line in `replays/<session>.jsonl` |

How it gets into the log: on Enter, the module writes one small file of counts to `~/.ai-tracker/typing/<session>/`. The Python `UserPromptSubmit` hook folds it into `prompt_sent` in the hash-chained log and deletes the file.

Set `AI_TRACKER_STATUS=on` for a live **status line** while you type (`typing 0:23 · 84 keys · 3 fixes · 1 rewrite`), and after you send, the prompt's totals. It's off by default: watching your own fix count tends to change how you type, which skews the data. It's handy for checking that the module works.

**Replays.** Each sent draft's edits are also kept as a layout-only timeline: every character reduced to `a` (visible), `_` (space) or `n` (newline), run-length encoded, with each edit's timing. They're stored in `~/.ai-tracker/replays/<session>.jsonl`, **not** in the event log. The prompt's `prompt_sent` event carries `replay_hash`, the SHA-256 of its replay line, so the dashboard can show whether a replay matches a logged prompt. The Python side **refuses any replay containing a character other than digits, `a`, `_` and `n`**, so text can't reach disk even if the module has a bug. A replay keeps word lengths and line structure, which is much less than text but not nothing. It never leaves your machine. The dashboard's **Prompt replays** card plays it back as blocks, with deletions in red and long pauses shortened.

**Typing time is always credited as "you" time**, even inside what would otherwise be a break, because keystrokes are direct evidence of work.

Coverage: only Claude Code's terminal prompt box raises these edit events. In the VS Code extension, the desktop app, other AI tools and imported transcripts, the fields are **absent**, which means unknown, not zero. Reports count "typed prompts" separately for that reason. Slash commands and messages from scripts or other sessions are skipped.

## Time model

`tracker/timemodel.py` turns events into time in two steps.

**1. Each session becomes a list of segments.**

```
session_start ─you─▶ prompt ─ai─▶ ai_done ─you─▶ prompt ─ai─▶ ai_done ─you─▶ session_end
```

| Segment | Span | Rule |
|---|---|---|
| **ai** | `prompt_sent` → its `ai_done` | The AI is working. Counted in full, because a long agent run is real wall-clock time, but reported separately so a reviewer can weigh it. |
| **you** | `session_start` → first prompt | Counted if at most `--idle` (default 15 min). |
| **you** | `ai_done` → next prompt, last `ai_done` → `session_end` | Only what there's evidence for: **reading** the reply (read time, capped at `--idle`), a **60 s thinking** allowance, and **writing** the next prompt (recorded typing time, or typed characters ÷ 3.5/s when keystrokes weren't recorded). Never more than the gap itself. The rest of the gap may have been spent anywhere, so it doesn't count. If Claude Code logged an `away_summary` in the gap, nothing counts from 3 minutes before it until you came back to write. |
| **ai (interrupted)** | a prompt with no `ai_done` before the next prompt or `session_end` (Esc, crash) | Counted up to `--idle`, since the real end time is unknown. |

**Read time** estimates how long it takes to read what the AI produced in one turn, at skim speed rather than careful-review speed:

```
read time = response prose chars ÷ 20/s              (~240 words/min)
          + (code lines in the response + diff lines added/removed by its edits) × 2 s
          capped at 60 min
```

Diff lines are matched to their turn by `prompt_id`, so an edit logged just after `ai_done` still counts. Read time does two jobs:

- **It stretches the break cutoff.** A 25-minute gap after a 900-line diff (a 30-minute read) counts as reading, not a break.
- **It gives breaks some credit.** After a 2-hour gap, you're credited the read time, capped at `--idle` (you plausibly glanced at it, then left), instead of 2 hours or nothing. The cap matters. Without it, a real session got an hour of "you" time from one prompt, a 72-minute agent run that wrote 4,000 lines, and a 36-hour absence.

The constants are at the top of `tracker/timemodel.py`. They're assumptions to tune against real data.

**2. Every moment is credited to exactly one project.** Segments from all sessions go on one timeline. A day's total therefore never exceeds wall-clock time, even with five sessions open at once. Where segments overlap:

1. **you** beats **ai**, because you can only think about one thing while agents can run in parallel.
2. Otherwise, the segment that started most recently wins, because that's where your attention went last.

Time that crosses midnight is split between the two local days.

**What "AI alone" means:** the AI was working and you weren't engaged in any other session. You may have been watching the AI, or you may have been away. The log can't tell the difference, which is why it's shown separately and not folded into "you". **"you" + "AI alone" is an upper bound on productive struggle, not a measurement of it.** It counts engaged time, not whether that time was hard or useful. Prompt length, typed versus pasted text, and AI lines per prompt are in the same report to help a reviewer judge that.

## Event schema (v1)

One JSON object per line in `~/.ai-tracker/events.jsonl`:

```json
{"v":1,"ts":"2026-11-18T23:04:10-05:00","source":"claude-code","session":"abc123","event":"prompt_sent","project":"greg","prompt_id":"…","chars":212,"pasted_chars":0,"cwd":"/home/max/greg","prev_hash":"…"}
{"v":1,"ts":"2026-11-18T23:04:55-05:00","source":"claude-code","session":"abc123","event":"ai_edit","project":"greg","prompt_id":"…","tool":"Edit","file":"src/motion.c","lines_added":140,"lines_removed":3,"cwd":"/home/max/greg","prev_hash":"…"}
{"v":1,"ts":"2026-11-18T23:06:02-05:00","source":"claude-code","session":"abc123","event":"ai_done","project":"greg","prompt_id":"…","response_text_chars":109,"response_code_lines":5,"cwd":"/home/max/greg","prev_hash":"…"}
```

| Field | |
|---|---|
| `v` | Schema version. |
| `ts` | ISO 8601, local time with offset, second precision. |
| `source` | Which adapter wrote it (`claude-code`). Future adapters: `cursor`, `codex`, … |
| `session` | The tool's session id. |
| `event` | `session_start` (`reason`), `session_end` (`reason`, optional abandoned-draft fields), `prompt_sent` (`chars`, `pasted_chars`, optional [typing fields](#typing-claude-code-terminal)), `ai_edit` (`tool`, `file`, `lines_added`, `lines_removed`), `ai_done` (`response_text_chars`, `response_code_lines`), `subagent_done` (`agent_type`). |
| `project` | Name of the git repo root containing `cwd`. A linked worktree is named after its main repo. Outside a repo, the folder name is used. Found by walking up for `.git`, without starting `git`. |
| `file` | Relative to the project root, with forward slashes. |
| `prompt_id`, `agent_id` | Opaque ids from Claude Code, when present. |
| `prev_hash` | SHA-256 of the previous line's bytes. The first line uses 64 zeros. |

**Writing an adapter for another tool:** build a dict with `event`, `session` and the event's fields, then call `tracker/eventlog.py: append_event(ev, cwd=..., source="your-tool")`. It fills in the common fields and handles locking and the hash chain.

## How it works

```
Claude Code hook ──stdin JSON──▶ hooks/run.sh ──▶ tracker/claude_code.py ──▶ tracker/eventlog.py ──▶ events.jsonl
                                 (find Python)     (map payload → event)     (lock, hash, append)

events.jsonl ──▶ tracker/timemodel.py (segments → credited time) ──▶ tracker/aitrack.py (summarize / verify)
```

- **Hooks:** `SessionStart`, `UserPromptSubmit`, `PostToolUse` (matcher `Edit|Write|MultiEdit|NotebookEdit`), `Stop`, `SubagentStop`, `SessionEnd`.
- **Line counts** come from `tool_response.structuredPatch`, the real diff Claude Code applied, so `replace_all` and whole-file rewrites are counted exactly. A newly created file counts all its lines. When there's no patch, a `difflib` fallback is used.
- **Never breaks Claude Code:** every hook exits 0 and prints nothing to stdout. Errors go to `~/.ai-tracker/errors.log`. If no Python is found, that is logged and the hook does nothing.
- **Never blocks:** `SessionStart`, `UserPromptSubmit`, `PostToolUse` and `SubagentStop` run with `async: true`, so Claude Code doesn't wait for them. `Stop` and `SessionEnd` are synchronous on purpose, because Claude Code cancels async hooks at teardown in `-p` mode, which lost the final `ai_done` in testing. They add roughly 0.1–0.2 s after a turn ends, while you're reading the reply.
- **Concurrency:** appends take an exclusive lock on `events.jsonl.lock` (`fcntl` on macOS/Linux, `msvcrt` on Windows), so parallel sessions never interleave lines or fork the chain. Tested with 8 processes × 25 writes.
- **Crash safety:** if the file ends in a partial line, the next write starts a new line. `verify` then reports the partial line, and the rest of the log stays readable.
- **Finding Python:** `run.sh` uses `python3`, then `python`, then `py -3`. It skips the Windows Store stub without starting it, converts Git Bash paths for native Windows Python, and caches the resolved interpreter in `~/.ai-tracker/.python`. Python runs with `-I -S -X utf8`: isolated mode, no `site` import (about 30 ms faster), and UTF-8 regardless of the Windows code page.

## Server

`server/` is the Sauron server: Hack Club Auth accounts, public-by-default profiles with live stats, per-project totals linked to each project's git remote, and an ingest API. It runs at https://sauron.k.hackclub.dev (Orchard, YSWS org).

Connect a computer to it:

1. Sign in at the server, open **Settings → Add a device** and copy the key.
2. Run `sauron connect https://sauron.k.hackclub.dev` and paste the key at the hidden prompt.

After that, the hooks send new activity in the background after each turn. Only allowlisted fields are sent, and the server drops anything outside its own allowlist too:
- counts and timings
- project names and project-relative file paths
- Jev labels
- each project's git remote

Each line's hash and the hash it links to are sent as well. The server keeps every device's latest hash as it arrives, so a log rewritten later no longer matches what the server already holds.

Running it yourself: `pip install -r server/requirements.txt`, then `uvicorn server.app:app` from the repo root. Settings:
- `PUBLIC_ORIGIN` and `HCA_CLIENT_ID` / `HCA_CLIENT_SECRET`, from an app at auth.hackclub.com/developer/apps with redirect `<origin>/auth/callback`
- `DATABASE_URL` for Postgres. Without it the server uses a local SQLite file.

The session secret is generated and kept in the database unless `SESSION_SECRET` is set.

## Privacy

Stored: timestamps, session and prompt ids, project name, `cwd`, relative file paths, character counts and line counts. From the typing module, the event log gets only counters and timings. The separate local replay file holds the layout (letter, space, newline) and timing of each edit, never the characters themselves. For the AI's reply, that's only its length: prose characters and code-block lines.

Never stored: prompt text, pasted text, file contents, diffs, model output and tool output. These are read from the hook's stdin only to be counted.

**The rule: by default, ai-tracker shares nothing Hackatime doesn't already get.** Every Hackatime heartbeat carries:
- the project name and the file path
- branch and language
- line additions and deletions
- editor, OS and machine name
- AI fields: session, model, prompt *length*, AI line changes and token counts

ai-tracker's default export carries the same kind of data:
- project names and project-relative file paths
- line counts
- prompt and reply lengths
- typing counts
- timings

Session ids are replaced with s1, s2…. If `~/.wakatime.cfg` sets `hide_project_names` or `hide_file_names`, the export follows it, and `--hide-project-names` / `--hide-file-names` do the same per export.

The one exception is Jev. Effort can't be estimated without knowing what each prompt was doing, so Sauron sends prompt text through its server to Hack Club AI's Jev model and keeps only the label it returns (category and scores). The server uses one shared key, never stores the text, and caps how many prompts each person can have labelled per day. Setup asks for agreement to this before installing. Apart from that, anything Hackatime never receives needs you to ask for it. That means prompt text, AI reply text, and keystroke-level typing replays (Hackatime sees at most one heartbeat every couple of minutes). The hooks never log prompt text at all. These commands go beyond the default, and each is opt-in:

| Command | What it reads or sends | Default |
| --- | --- | --- |
| `aitrack serve` | Reads prompt text from `~/.claude/projects` to show it next to each prompt | Off. Pass `--prompt-text` (optionally with a folder) |
| `aitrack export-site` | Writes a snapshot you may then publish | The Hackatime-level data above. Prompt text and typing replays only with `--prompt-text` / `--include-replays`. Hide names with `--hide-project-names` / `--hide-file-names` or your `~/.wakatime.cfg`. It prints what's in and what's out |
| Jev labelling | **Sends each new prompt's text, your previous prompt, and the end of the AI's previous reply to the Sauron server, which asks Hack Club AI's Jev** using one shared key. Only the returned label is kept, on your computer and on the server | **Required for effort.** Runs in the background after each turn once you've run `sauron connect`, for prompts sent from then on. Capped per person per day. Running Jev with your own key (`sauron jev --save-key`) also works, and then prompts go straight to Hack Club AI |
| `presence.py` | Samples idle seconds and the *category* of the front app (terminal, editor, browser…), never names or titles | Off. `{"presence": true}` in `~/.ai-tracker/config.json` |

Nothing else leaves your machine.

## Known limitations

- **Files written through Bash** (`cat > f`, `sed -i`, code generators) aren't counted as AI edits. Only the Edit, Write and NotebookEdit tools are. A later version could diff the working tree at `Stop`.
- **`verify` is a consistency check, not proof.** It confirms the links between records are intact, which catches accidental damage and naive edits to the middle of the log. The hash chain alone can't see the last record being edited or removed, so each `verify` remembers the head it saw (`~/.ai-tracker/verify-heads.json`) and the next one checks the log still extends it. That catches end edits between two verifies, but the saved head sits next to the log, so someone can rewrite both. `verify` prints the head (`head N:HASH`); a reviewer who keeps their own copy can check it with `verify --expect-head N:HASH`. Real tamper evidence needs that head anchored somewhere the user can't rewrite, such as a server or a signed checkpoint.
- **Estimated effort is an unvalidated estimate.** Reading, thinking and typing time between prompts is modelled, and the effort weight comes from an AI judge (Jev). Neither has been calibrated against observed behaviour yet. Parallel sessions never double-count. Each moment is credited once: your time beats the agent's, then the interval that started most recently wins. The CLI and the dashboard use this same rule, so your time plus agent time never exceeds the real time that passed. They don't estimate your time the same way, though. The CLI allows a fixed 60 s of thinking per prompt, while the dashboard scales thinking with Jev's complexity rating. So the dashboard's "your time" and "estimated effort" can be higher or lower than the CLI's for the same log.
- **Read time sees only the AI's final message.** `Stop` provides the last assistant message, so text the AI wrote between tool calls in a long agentic turn isn't counted, and read time runs low for those turns. Diff lines are fully counted.
- **Interrupted turns** (Esc) have no `ai_done`, so they appear as "interrupted" rather than as cycles.
- **In `claude -p` (headless) mode,** an async `PostToolUse` that is still running when the process exits can be cancelled. Interactive sessions aren't affected.
- **Startup latency is mostly the Python interpreter.** Bare Python plus `import json` is most of the cost. On a heavily loaded M-series Mac, the full hook took 110–160 ms, and it's async, so Claude doesn't wait. A compiled Go binary using the same schema would start in about 5 ms, which would suit 10-year-old Windows laptops.
- **Tested on macOS** with Python 3.9 and Claude Code 2.1. Windows and Linux are handled in code (`msvcrt` locking, `cygpath`, Store-stub skipping) but haven't been run yet.

## Tests

```sh
python3 -m unittest discover -s tests -v     # Python: hooks, time model, importer, reports
claude plugin test ~/sauron               # the typing module's keystroke logic
```

The tests cover the dashboard's data sources, the All days and `--replies` output, the transcript importer, read time (response size parsing, stretched break limits, break credit, fast-reply flags, matching late edits by `prompt_id`), the time model (parallel sessions never double-count, breaks, interrupted and long AI turns, midnight splitting), plus line counts (using payload shapes captured from real Claude Code hook input), the end-to-end hook through `run.sh`, privacy (secrets in prompts, code and output never reach the log), project and worktree detection, garbage stdin, an unwritable log dir, hash chain tamper detection, a truncated last line, concurrent writers, and the summarize math.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
