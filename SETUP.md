# Set up ai-tracker (for an AI coding agent)

The person asked you to set up **ai-tracker** on this computer. Follow these steps in order. Tell them in a sentence what each step does as you go. At every **ASK** step, stop and wait for their answer; never assume yes.

**What ai-tracker is (tell them this first, in your own words):** a Claude Code plugin that passively logs AI coding sessions to `~/.ai-tracker/` on this computer: when prompts were sent, how long the AI worked, which files it changed and by how many lines, and typing stats such as typing time and fixes. It never stores prompt text, code or AI output. A local dashboard turns the log into time spent, split into their own time, the AI's time, and an estimate of the effort they put in (an estimate, not a measurement).

To estimate effort, ai-tracker needs to know what each prompt was doing: asking a question, directing, delegating, and so on. After every turn it asks Jev, an AI model reached through Hack Club AI, and keeps only the answer (a category and a few scores). That means **the text of each new prompt, their previous prompt, and the end of the AI's previous reply are sent to ai.hackclub.com and on to TypeSafe, who run Jev**. Things that look like API keys, passwords, emails, phone numbers, street addresses, card numbers and home folder names are removed first. That's best-effort, so anything truly secret shouldn't be in prompts anyway. Apart from this, ai-tracker shares nothing Hackatime doesn't already get.

**ASK before step 1:** "ai-tracker sends your prompts to Hack Club AI's Jev model to work out effort, as described above. It keeps only the label, never the text. Are you OK with that?" If they say no, stop here: ai-tracker can't estimate effort without it. Don't install anything.

Throughout, `~` means the home folder (`%USERPROFILE%` on Windows), and `PY` means whichever Python command works in step 1.

## 1. Check what's installed

Run these and note the results:

- `claude --version`: needs to be 2.1 or newer. If older, tell them to update Claude Code first, and stop.
- Python 3.8 or newer: try `python3 --version`, then `python --version`, then `py -3 --version` (Windows). Use the first that prints Python 3.8+ as `PY` from now on. On Windows, ignore a `python` that opens the Microsoft Store.
- `git --version`

**ASK** before installing anything that's missing:
- **macOS:** Python with `xcode-select --install` (or `brew install python` if Homebrew is present); git comes with the same command.
- **Windows:** `winget install Python.Python.3.12` and `winget install Git.Git`. Then they must open a new terminal and restart Claude Code.
- **Linux:** their package manager, e.g. `sudo apt install python3 git`.

## 2. Get the code

```sh
git clone https://github.com/OWNER/ai-tracker ~/ai-tracker
```

If `~/ai-tracker` already exists and is this repo, run `git -C ~/ai-tracker pull` instead. If it exists and is something else, **ASK** before touching it.

Check: `PY ~/ai-tracker/tracker/aitrack.py --help` prints usage.

## 3. Install the Claude Code plugin

```sh
claude plugin marketplace add ~/ai-tracker
claude plugin install ai-tracker@ai-tracker
claude plugin list
```

Check: `ai-tracker@ai-tracker` shows as **enabled**. The plugin adds its own hooks, so never edit their `settings.json` for this.

Then tell them: **"Run `/reload-plugins` in Claude Code (or restart it) so the tracker starts logging."** You can't do this yourself.

## 4. Check it works

```sh
cd ~/ai-tracker && PY -m unittest discover -s tests
```

All tests should pass. If any fail, show them the failure and stop.

After they've reloaded and sent one prompt in a **terminal** Claude Code session:
- `~/.ai-tracker/events.jsonl` exists and its last lines include `"event":"prompt_sent"`.
- That line has `typing_ms`. If it doesn't, the typing module didn't load: tell them typing stats only work in the terminal version of Claude Code, not the VS Code extension or desktop app.
- `PY ~/ai-tracker/tracker/aitrack.py verify` says `OK`.

If nothing is logged, look at `~/.ai-tracker/errors.log` and tell them what it says.

## 5. Import their past sessions (local only)

Claude Code keeps about 30 days of transcripts. This turns them into the same counts-only log, so the dashboard has history from day one. Nothing is sent anywhere, but it reads every Claude Code transcript on this computer, including other projects. **ASK first:** "Do you want me to import your past Claude Code sessions (counts only, stays on this computer)?" Skip this step if they say no.

```sh
PY ~/ai-tracker/tracker/aitrack.py import
```

## 6. Connect Jev (required)

They need a free Hack Club AI key from https://ai.hackclub.com/dashboard.

1. **Don't ask them to paste the key into this chat** (chat text is stored in transcripts). Ask them to run this in their own terminal, where it's read without echoing and saved readable only by them:
   ```sh
   PY ~/ai-tracker/tracker/jev_judge.py --save-key
   ```
   In Claude Code they can type `! PY ~/ai-tracker/tracker/jev_judge.py --save-key`. Wait until they say it's done.
2. That's all for new prompts. From now on, every turn's prompt is labelled in the background, and anything missed while offline is caught up at the next session start. Nothing to run by hand.
3. Only if they imported history in step 5, **ASK:** "Do you also want your past sessions from the import labelled? That sends those older prompts to Jev too." If yes:
   ```sh
   PY ~/ai-tracker/tracker/jev_judge.py ~/.claude/projects --out ~/.ai-tracker/jev/imported.jsonl
   ```

## 7. Show them the dashboard

```sh
PY ~/ai-tracker/tracker/aitrack.py serve
```

It opens `http://127.0.0.1:8765/`, which only this computer can reach. Point out the **Data** dropdown (**Live log** and **Imported history**), the **Prompts** list, and the **Estimated effort** card (it fills in as Jev labels prompts, a few seconds after each turn). Tell them it's an estimate, not a measurement. Prompt text isn't shown unless they add `--prompt-text`. Ctrl+C stops it.

## 8. Finish

Tell them in a few lines:
- what was installed, and where the data lives (`~/.ai-tracker/`)
- that Jev labels each new prompt in the background, and only the labels are kept
- how to open the dashboard again (the command in step 7)
- how to pause logging (`AI_TRACKER_DISABLE=1`) or uninstall (below)

## Rules

- Never send, upload, publish or share their data or the dashboard anywhere unless they ask. Step 6 is the only thing that sends data, and only after they say yes.
- Never print, commit or log their Hack Club AI key.
- Don't turn on the typing status line (`AI_TRACKER_STATUS=on`) unless they ask; watching a live fix count changes how people type.
- If a step fails and the fix isn't obvious, stop and tell them what happened, rather than working around it.

## Uninstall

```sh
claude plugin uninstall ai-tracker@ai-tracker
claude plugin marketplace remove ai-tracker
```

Their data stays in `~/.ai-tracker/` until they delete it. **ASK** before deleting it.
