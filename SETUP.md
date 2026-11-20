# Set up Sauron (for an AI coding agent)

The person asked you to set up **Sauron** (the ai-tracker plugin) on this computer. Follow these steps in order. Tell them in a sentence what each step does as you go. At every **ASK** step, stop and wait for their answer; never assume yes.

**What Sauron is (tell them this first, in your own words):** a Claude Code plugin that passively logs AI coding sessions: when prompts were sent, how long the AI worked, which files it changed and by how many lines, and typing stats such as typing time and fixes. The log stays at `~/.ai-tracker/` on this computer and is synced to their account at **https://sauron.k.hackclub.dev**, which turns it into time spent: their own time, the AI's time, and an estimate of the effort they put in (an estimate, not a measurement). The sync carries the same kind of data Hackatime already gets (project, file paths inside the project, line counts, prompt length, timings) and never prompt text, code or AI output. Admins of the Sauron server can see everyone's synced data.

To estimate effort, Sauron needs to know what each prompt was doing: asking a question, directing, delegating, and so on. After every turn, **the text of the new prompt, their previous prompt, and the end of the AI's previous reply are sent to the Sauron server, which asks Jev (an AI model run by TypeSafe) and keeps only the answer**: a category and a few scores. The text isn't stored. Things that look like API keys, passwords, emails, phone numbers, street addresses, card numbers and home folder names are removed first. That's best-effort, so anything truly secret shouldn't be in prompts anyway. They don't need a Jev or TypeSafe key of their own.

**ASK before step 1:** "Sauron syncs your coding activity to sauron.k.hackclub.dev and sends each prompt there so Jev can work out effort. Only the label is kept, never the text. Are you OK with that?" If they say no, stop here and don't install anything.

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
git clone https://github.com/2Mon/sauron ~/sauron
```

If `~/sauron` already exists and is this repo, run `git -C ~/sauron pull` instead. If it exists and is something else, **ASK** before touching it.

Then add the `sauron` command:
- macOS/Linux: `mkdir -p ~/.local/bin && ln -sf ~/sauron/bin/sauron ~/.local/bin/sauron`. If `~/.local/bin` isn't on their PATH, tell them, and use `~/sauron/bin/sauron` in place of `sauron` below.
- Windows: use `%USERPROFILE%\sauron\bin\sauron.cmd` in place of `sauron` below, or add that folder to PATH if they agree.

Check: `sauron --help` prints usage.

## 3. Install the Claude Code plugin

```sh
claude plugin marketplace add 2Mon/sauron
claude plugin install ai-tracker@ai-tracker
claude plugin list
```

If `claude plugin marketplace list` already shows an `ai-tracker` marketplace that points at a local folder (an install from before 0.2.0, which never updates), run `claude plugin marketplace remove ai-tracker` first, then the commands above.

Check: `ai-tracker@ai-tracker` shows as **enabled**. The plugin adds its own hooks, so never edit their `settings.json` for this.

Then tell them: **"Run `/reload-plugins` in Claude Code (or restart it) so the tracker starts logging. To get updates automatically, run `/plugin`, open Marketplaces, choose ai-tracker and enable auto-update."** You can't do either yourself.

## 4. Check it works

```sh
cd ~/sauron && PY -m unittest discover -s tests
```

All tests should pass. If any fail, show them the failure and stop.

After they've reloaded and sent one prompt in a **terminal** Claude Code session:
- `~/.ai-tracker/events.jsonl` exists and its last lines include `"event":"prompt_sent"`.
- That line has `typing_ms`. If it doesn't, the typing module didn't load: tell them typing stats only work in the terminal version of Claude Code, not the VS Code extension or desktop app.
- `sauron verify` says `OK`.

If nothing is logged, look at `~/.ai-tracker/errors.log` and tell them what it says.

## 5. Connect to Sauron

1. Ask them to open **https://sauron.k.hackclub.dev**, sign in (with Hack Club), go to **Devices**, name this computer and click **Add Device**. The page shows a key once.
2. **Don't ask them to paste the key into this chat** (chat text is stored in transcripts). Ask them to run this in their own terminal, where the key is read without echoing and saved readable only by them:
   ```sh
   sauron connect https://sauron.k.hackclub.dev
   ```
   In Claude Code they can type `! sauron connect https://sauron.k.hackclub.dev`. Wait until they say it printed "Connected".
3. That's all. After each turn their activity syncs in the background, and new prompts are labelled by Jev through the server a few seconds later. Connecting also backfills their recent history from Claude Code's transcripts (about the last 30 days, counts only); it shows on their profile tagged as imported and never on the leaderboard. Prompts from before connecting aren't sent for labelling unless they choose to: `sauron label-history` asks first, then sends those prompts' text the same way.

## 6. Finish

Tell them in a few lines:
- what was installed, and that the local log lives in `~/.ai-tracker/`
- that their stats are at **https://sauron.k.hackclub.dev** (their profile updates every 15 seconds while they code)
- that only labels are kept from Jev, never prompt text
- how to pause logging (`AI_TRACKER_DISABLE=1`) or uninstall (below)

## Rules

- Never send, upload, publish or share their data anywhere other than the Sauron server they agreed to.
- Never print, commit or log their device key.
- Don't turn on the typing status line (`AI_TRACKER_STATUS=on`) unless they ask; watching a live fix count changes how people type.
- If a step fails and the fix isn't obvious, stop and tell them what happened, rather than working around it.

## Uninstall

```sh
claude plugin uninstall ai-tracker@ai-tracker
claude plugin marketplace remove ai-tracker
```

Their local log stays in `~/.ai-tracker/` until they delete it. **ASK** before deleting it. To stop syncing without uninstalling, they can revoke the device under **Devices** on the site.
