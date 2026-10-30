# Example apps

Each folder is pushed to boards as is: `cpfleet push apps/<name> --to <boards>`.

| App | Boards | What it does |
|---|---|---|
| [quotes](quotes) | Adafruit MagTag | A random quote from `quotes.txt` every 15 minutes; button A shows another. Built-in modules only (no `lib/`). Edit `quotes.txt` (`text \| author` per line, plain ASCII) and push again. |

| [agents](agents) | Adafruit MagTag | Your Claude Code, Codex and opencode sessions and their subagents, one view per tool, with the LEDs pulsing amber while one waits for you. Needs `extras/agents_watch.py` running on the Mac where they run. A: next view (Claude, Codex, opencode, a quote); B: acknowledge; C: another quote. |

### Coding agents display (Claude Code, Codex, opencode)

`extras/agents_watch.py`, on the computer running the agents, **only reads** what each tool already keeps:
- **Claude Code**, in `~/.claude/projects`: a session's transcript as it's appended, its `custom-title.json` and its `subagents/*.meta.json`.
- **Codex**: the rollouts in `~/.codex/sessions/YYYY/MM/DD/`, titles from `~/.codex/session_index.jsonl`, and archived threads and sub-thread links from `~/.codex/state_5.sqlite` (opened read-only). `~/.codex/archived_sessions` is ignored.
- **opencode**: `~/.local/share/opencode/opencode.db` (opened read-only): sessions that aren't archived, their latest message and its tool calls, and child sessions (subagents).

A tool that isn't installed, or has nothing in the last `--hours` (3), is left out; a database that is locked or unreadable is skipped for that scan, and the others carry on. `--sources claude,codex` reads only those. The databases are opened read-only (`mode=ro`); nothing of any tool's settings is touched.

It serves a compact `/agents.json` on your network and advertises it over Bonjour as `_cpagents._tcp` (with macOS's own `dns-sd`). No hooks or settings changes are involved. The MagTags find it by name, fetch it every 3 seconds and light their LEDs at once. To spare the e-ink panel, they redraw at most every 30 seconds for new data (`cpfleet settings <b> set AGENTS_REDRAW 60` to change that per board); button presses redraw straight away. The top line names the view's tool and how many of its sessions wait, with where the data comes from (`WiFi` or `USB`) and the watcher's clock on the right; the sessions follow straight after, each with how long it has been in its state. The text is terminalio's font at one and a half times its size, 32 characters by 7 lines (`cpfleet settings <b> set AGENTS_FONT small` for the original size, 49 by 10). The LEDs pulse for a waiting session in any tool, so press A to find which view it's in:

```
CLAUDE  2 waiting    WiFi 14:05
! Fix the parser        idle 9m
  + Explore: find tests      2m
  ✓ write the summary
> Port the board app    run 12m
? Pick a name            ask 3m
. Release notes         idle 2h
```

Button A goes Claude, Codex, opencode, quote; a tool's view is skipped while it has no sessions. A watcher from before Codex and opencode support sends no `tool`, and its sessions show as Claude's.

```sh
python3 extras/agents_watch.py                    # serves http://0.0.0.0:8766/agents.json and advertises it
cpfleet push apps/agents
```

**Finding the watcher.** Without `AGENTS_URL`, a board looks the watcher up over mDNS at start and every 30 seconds until it finds one, and keeps the address it found. With several watchers on the network, it takes the first that answers. `AGENTS_URL` is an override for networks where mDNS doesn't get through (some guest and mesh networks drop it), or for a watcher on another subnet. If an `AGENTS_URL` stops answering (3 failed fetches), the board looks the watcher up by name as well.

```sh
cpfleet settings <b> set AGENTS_URL http://<mac-ip>:8766/agents.json && cpfleet reset <b>   # optional
```

#### On the go: over USB, with no network

A MagTag plugged into the Mac running Claude Code can get the same data over USB. That needs CircuitPython's second USB serial channel, which cpfleet's `boot.py` turns on only when asked:

```sh
cpfleet push cpfleet/board_files/boot.py --to <b>      # if the board's boot.py predates this; MagTag button D
cpfleet settings <b> set CPFLEET_USB_DATA 1 && cpfleet reset <b>
python3 extras/agents_watch.py --usb                   # HTTP and USB; --no-http for USB only
```

- `--usb` looks for boards every half second. Each USB device with two serial ports (`/dev/cu.usbmodem…1` is the console, `…3` the data channel) gets a probe on its data port; a board running `apps/agents` answers, then receives each new snapshot as one line of JSON, and the same line again every 5 seconds as a keep-alive. Boards can be plugged and unplugged at any time. Console ports are never written to.
- A board uses USB while lines keep arriving and goes back to Wi-Fi after 10 seconds without one.
- With the data channel on, USB keyboard/mouse (`usb_hid`) and MIDI (`usb_midi`) are off: the ESP32-S2 hasn't enough USB endpoints for everything. Holding button D at Reset skips the data channel for that boot, along with showing the USB drive. `cpfleet settings <b> unset CPFLEET_USB_DATA` and a reset turn it off for good.

| Setting | Default | Meaning |
|---|---|---|
| `AGENTS_URL` | found by name | The watcher's address, if mDNS can't find it |
| `AGENTS_TOKEN` | none | Sent as `Authorization: Bearer …`; needed when the watcher runs with `AGENTS_TOKEN` set |
| `AGENTS_REDRAW` | 30 | Minimum seconds between e-ink redraws for new data |
| `AGENTS_FONT` | larger | `small`: terminalio's font at its own size, for more rows |
| `CPFLEET_USB_DATA` | off | `1`: turn on the USB data channel at boot (`boot.py`; needs a reset) |

Each row starts with a symbol for its state and ends with how long (`<1m`, `9m`, then 10-minute steps, then hours, so the screen doesn't redraw every minute):
- **`run 12m`:** working (or busy) on the current turn for 12 minutes, counted from your prompt. A watcher from before this sends no start time, and the row shows none.
- **`ask 3m`:** asking you something for 3 minutes.
- **`idle 9m`:** nothing for 9 minutes since the turn ended (`!`), or since the session last did anything (`.`).

| Symbol | State | Meaning |
|---|---|---|
| `?` | question | Claude's `AskUserQuestion` or plan approval, or opencode's `question` tool (also in a subagent), is open |
| `!` | your turn | The session finished its turn (or was stopped) |
| `~` | busy? | A tool call has had no result for 20 s or more. It's either a long command or a permission prompt; none of the three tools' logs tell those apart (opencode's `permission` table holds saved rules, not pending prompts). |
| `>` | working | The session is writing, or a subagent is running |
| `.` | idle | Nothing for a while, and not waiting. Codex runs nobody waits for (`codex exec`, the app's automations) are idle when done. |
| `+` | subagent | A running subagent (Claude's subagents, Codex's sub-threads, opencode's child sessions), and how long it has run. Its type is shown only when it says something (`Explore:`, `Plan:`), not `general-purpose` and the like. |
| `✓` | subagent | One that finished in the last 5 minutes (drawn: the board's font has no check mark) |

At most 8 sessions per tool and 3 subagents per session are sent; the header counts them all.

**LEDs:**
- **Pulsing amber:** a session has been waiting for less than an hour and hasn't been acknowledged (B).
- **Dim red:** no reply from the watcher for 30 s.

**Privacy.** The watcher shares session titles (from all three tools) and subagent descriptions with anything that can reach the port, never prompts or messages (Codex's database keeps a thread's first prompt as `title`; the watcher never reads it, and uses the thread name or the folder instead), and the Bonjour advertisement tells the whole local network that it's there (`--no-advertise` stops that; boards then need `AGENTS_URL`). Run it only on a trusted network, or pass `--bind` with a specific address. A token keeps casual visitors out:

```sh
AGENTS_TOKEN=<something long> python3 extras/agents_watch.py    # in the environment, so it isn't in `ps`
cpfleet settings all set AGENTS_TOKEN                            # type the same value at the prompt
```

It travels in plain HTTP, so anyone who can watch the network traffic can read it, and the snapshots with it. Over USB nothing leaves the cable; `--usb --no-http` opens no port at all.

To go back to an app after trying an Adafruit Learn project (`cpfleet bundle`), push it again. Files the
project added stay on the board until you delete them with `cpfleet rm`.
