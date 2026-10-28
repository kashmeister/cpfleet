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

It serves a compact `/agents.json` on your network and advertises it over Bonjour as `_cpagents._tcp` (with macOS's own `dns-sd`). No hooks or settings changes are involved. The MagTags find it by name, fetch it every 3 seconds and light their LEDs at once. To spare the e-ink panel, they redraw at most every 30 seconds for new data (`cpfleet settings <b> set AGENTS_REDRAW 60` to change that per board); button presses redraw straight away. The header shows where the data comes from (`WiFi` or `USB`) next to the watcher's clock.

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
| `CPFLEET_USB_DATA` | off | `1`: turn on the USB data channel at boot (`boot.py`; needs a reset) |

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

**Privacy.** The watcher shares session titles and subagent descriptions with anything that can reach the port, and the Bonjour advertisement tells the whole local network that it's there (`--no-advertise` stops that; boards then need `AGENTS_URL`). Run it only on a trusted network, or pass `--bind` with a specific address. A token keeps casual visitors out:

```sh
AGENTS_TOKEN=<something long> python3 extras/agents_watch.py    # in the environment, so it isn't in `ps`
cpfleet settings all set AGENTS_TOKEN                            # type the same value at the prompt
```

It travels in plain HTTP, so anyone who can watch the network traffic can read it, and the snapshots with it. Over USB nothing leaves the cable; `--usb --no-http` opens no port at all.

To go back to an app after trying an Adafruit Learn project (`cpfleet bundle`), push it again. Files the
project added stay on the board until you delete them with `cpfleet rm`.
