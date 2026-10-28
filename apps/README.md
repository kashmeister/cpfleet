# Example apps

Each folder is pushed to boards as is: `cpfleet push apps/<name> --to <boards>`.

| App | Boards | What it does |
|---|---|---|
| [quotes](quotes) | Adafruit MagTag | A random quote from `quotes.txt` every 15 minutes; button A shows another. Built-in modules only (no `lib/`). Edit `quotes.txt` (`text \| author` per line, plain ASCII) and push again. |

| [agents](agents) | Adafruit MagTag | Your Claude Code sessions and their running subagents, with the LEDs pulsing amber while one waits for you. Needs `extras/agents_watch.py` running on the Mac where Claude Code runs. A: switch to a quote and back; B: acknowledge; C: another quote. |

### Claude Code agents display

`extras/agents_watch.py`, on the computer running Claude Code, **only reads** Claude Code's session files in `~/.claude/projects`:
- a session's transcript, as it's appended;
- its `custom-title.json`;
- its `subagents/*.meta.json`.

It serves a compact `/agents.json` on your network. No hooks or settings changes are involved. The MagTags fetch it every 3 seconds, redraw only when something changes, and light their LEDs at once.

```sh
python3 extras/agents_watch.py                    # serves http://0.0.0.0:8766/agents.json
cpfleet settings all set AGENTS_URL http://<that computer's IP>:8766/agents.json && cpfleet reset
cpfleet push apps/agents
```

| Symbol | State | Meaning |
|---|---|---|
| `?` | question | An `AskUserQuestion` or plan approval is open |
| `!` | your turn | The session finished its turn |
| `~` | busy? | A tool call has had no result for 20 s or more. It's either a long command or a permission prompt; transcripts can't tell those apart. |
| `>` | working | The session is writing its transcript, or a subagent is running |
| `.` | idle | Nothing for 30 s or more, and not waiting |

**LEDs:**
- **Pulsing amber:** a session has been waiting for less than an hour and hasn't been acknowledged (B).
- **Dim red:** no reply from the watcher for 30 s.

The watcher shares session titles and subagent descriptions with anything that can reach the port, so run it only on a trusted network, or pass `--bind` with a specific address.

To go back to an app after trying an Adafruit Learn project (`cpfleet bundle`), push it again. Files the
project added stay on the board until you delete them with `cpfleet rm`.
