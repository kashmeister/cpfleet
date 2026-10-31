# Example apps

Each folder is pushed to boards as is: `cpfleet push apps/<name> --to <boards>`.

| App | Boards | What it does |
|---|---|---|
| [quotes](quotes) | Adafruit MagTag | A random quote from `quotes.txt` every 15 minutes; button A shows another. The LEDs glow dimly (`CPFLEET_GLOW`). Built-in modules only (no `lib/`). Edit `quotes.txt` (`text \| author` per line, plain ASCII) and push again. |

| [agents](agents) | Adafruit MagTag | Your Claude Code, Codex and opencode sessions and their subagents as a grid of cards, one column under each LED and above each button; a card's LED flashes red while it waits for you, and its button opens it. Needs `extras/agents_watch.py` running on the Mac where they run. |
| [weather](weather) | Adafruit MagTag | Now, the next hours and three days from Open-Meteo (no key), with icons. Set `WEATHER_LAT` and `WEATHER_LON` (strings), reset; `WEATHER_UNITS metric` for °C. A: fetch now. The icons are Adafruit's MagTag weather sheets (Meteocons), not in this repo: `python3 extras/weather_icons.py` fetches them into `apps/weather/bmps` before you push. `weather.py` (what to show) and `screen.py` (drawing it, in four text sizes, with icons) are written to be shared by an app that cycles through screens. |

### Coding agents display (Claude Code, Codex, opencode)

`extras/agents_watch.py`, on the computer running the agents, **only reads** what each tool already keeps:
- **Claude Code**, in `~/.claude/projects`: a session's transcript as it's appended, its `custom-title.json` and its `subagents/*.meta.json`. For sessions run in the Claude desktop app, also the app's own record of each (`~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json`), for the title you see in its sidebar and for whether you've archived it (archived sessions are left out). Otherwise the title is the latest `/rename` in the transcript.
- **Codex**: the rollouts in `~/.codex/sessions/YYYY/MM/DD/`, titles from `~/.codex/session_index.jsonl`, and archived threads and sub-thread links from `~/.codex/state_5.sqlite` (opened read-only). `~/.codex/archived_sessions` is ignored.
- **opencode**: `~/.local/share/opencode/opencode.db` (opened read-only): sessions that aren't archived, their latest message and its tool calls, and child sessions (subagents).

A tool that isn't installed, or has nothing in the last `--hours` (3), is left out; a database that is locked or unreadable is skipped for that scan, and the others carry on. `--sources claude,codex` reads only those. The databases are opened read-only (`mode=ro`); nothing of any tool's settings is touched.

It serves a compact `/agents.json` on your network and advertises it over Bonjour as `_cpagents._tcp` (with macOS's own `dns-sd`). No hooks or settings changes are involved. The MagTags find it by name, fetch it every 3 seconds and light their LEDs at once.

**The screen** is four cards side by side, the 4 sessions that matter most from all three tools, each under one of the MagTag's four LEDs and above one of its four buttons. A card shows the state's icon and how long, the title, and the tool. With subagents, the title takes 2 lines, and up to 3 subagents follow under a rule (the footer counts the rest: `claude +2`).

```
 ? 3m     | || 9m    | > 12m    | z 2h
 Pick a   | Fix the  | Port the | Release
 name     | parser   | board a~ | notes
          |          | +Explor 2m
          |          | ✓write t |
 codex    | opencode | claude +2| claude
```

| Icon | State | How long |
|---|---|---|
| `?` | A question or choice open for you: Claude's `AskUserQuestion` or plan approval, or opencode's `question` tool (also in a subagent) | since it asked |
| pause sign | Your turn: the session finished its turn, or was stopped, and waits for your next prompt | since then |
| `>` (a play triangle) | Working, or waiting on its own running subagents during its turn | since your prompt (a watcher from before this sends no start time, and the card shows none) |
| hourglass | Busy: a tool call has had no result for 20 s or more. It's either a long command or a permission prompt; none of the three tools' logs tell those apart (opencode's `permission` table holds saved rules, not pending prompts). | since your prompt |
| gear | In the background: its turn is over, but it waits on its own background command or subagent (Claude's `run_in_background`, or any subagent still running), which will wake it when done | since your prompt |
| `Z` | Idle: nothing for a while, and not waiting. Codex runs nobody waits for (`codex exec`, the app's automations) are idle when done. | since it last did anything |

Times are coarse (`<1m`, `9m`, then 10-minute steps, then hours) so the screen doesn't redraw every minute. Under a card's title, `+` is a running subagent (Claude's subagents, Codex's sub-threads, opencode's child sessions) and how long it has run, `✓` one that finished in the last 5 minutes (drawn: the board's font has no check mark); a subagent's type is shown only when it says something (`Explore:`, `Plan:`), not `general-purpose` and the like.

**Order.** Sessions waiting for you come first (for up to an hour, until you look at them), then busy and working ones, then the rest, the most recently active first. A session keeps its column while it stays on screen, so its LED doesn't move. With no sessions at all, the board shows a quote; any button shows another.

**Waiting for you.** The card gets a black bar along its top, and the LED above it flashes red every 2.5 seconds.

**Press the button under a card** for that session's page, a frozen frame of the moment you pressed: its state and for how long, the tool and the title, then what it's doing (`> Bash: Run the tests`), its subagents, and as much of the end of its latest reply as fits. That counts as having seen it: the bar and the LED go. The board goes back to the cards by itself after 20 seconds (`cpfleet settings <b> set AGENTS_PAGE 30`), or at any button.

The reply and the tool call come only from a watcher started with `--text`, which is off by default (see Privacy): what the agent wrote, the last 280 characters of it, never your prompts. Without it, the page shows the state, title and subagents.

To spare the e-ink panel, the board redraws at most every 30 seconds for new data (`cpfleet settings <b> set AGENTS_REDRAW 60` to change that), except straight away for a session that newly waits for you, and for buttons. A watcher from before Codex and opencode support sends no `tool`, and its sessions show as Claude's.

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
| `AGENTS_PAGE` | 20 | Seconds a session's page stays up |
| `CPFLEET_GLOW` | 5 | The LEDs' constant brightness, 0-255 (0: off) |
| `AGENTS_LEDS` | `3210` | Which LED is over each column, left to right |
| `CPFLEET_USB_DATA` | off | `1`: turn on the USB data channel at boot (`boot.py`; needs a reset) |

At most 8 sessions per tool and 3 subagents per session are sent; the header counts them all.

**LEDs:**
- **A dim, soft white glow, always:** so the screen can be read in the dark (`CPFLEET_GLOW`, 5 by default: `cpfleet settings <b> set CPFLEET_GLOW 10` is brighter, `1` the dimmest, `0` off; the quotes app does the same).
- **A red flash over a column every 2.5 seconds:** its session has been waiting for less than an hour and you haven't opened it.
- **All dim red:** no reply from the watcher for 30 s.
- If the LEDs are over the wrong columns on your board, `cpfleet settings <b> set AGENTS_LEDS 0123` gives their order, left to right (the MagTag's own is `3210`).

**Privacy.** The watcher shares session titles (from all three tools) and subagent descriptions with anything that can reach the port, never prompts or messages; with `--text`, also the end of each agent's latest reply and its current tool call (a command line, a file path), so leave it off on networks you don't trust, or use USB with `--no-http` (Codex's database keeps a thread's first prompt as `title`; the watcher never reads it, and uses the thread name or the folder instead), and the Bonjour advertisement tells the whole local network that it's there (`--no-advertise` stops that; boards then need `AGENTS_URL`). Run it only on a trusted network, or pass `--bind` with a specific address. A token keeps casual visitors out:

```sh
AGENTS_TOKEN=<something long> python3 extras/agents_watch.py    # in the environment, so it isn't in `ps`
cpfleet settings all set AGENTS_TOKEN                            # type the same value at the prompt
```

It travels in plain HTTP, so anyone who can watch the network traffic can read it, and the snapshots with it. Over USB nothing leaves the cable; `--usb --no-http` opens no port at all.

To go back to an app after trying an Adafruit Learn project (`cpfleet bundle`), push it again. Files the
project added stay on the board until you delete them with `cpfleet rm`.
