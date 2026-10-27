# cpfleet

Manage several CircuitPython boards over Wi-Fi from the command line. cpfleet uses CircuitPython's
**web workflow**, so you never need the `CIRCUITPY` USB drive:

- check every board's state;
- upload code and libraries;
- run Python and get just its output;
- read console output and errors;
- restart boards;
- install Adafruit Learn guide projects;
- provision new boards over USB, even on a Mac that won't mount them.

Built for a set of Adafruit MagTag e-ink displays, it works with any Wi-Fi CircuitPython board
(ESP32-S2, ESP32-S3, ESP32-C3/C6, Pico W). It's a single Python package with no dependencies.

```sh
brew install kashmeister/tap/cpfleet      # or: pipx install git+https://github.com/kashmeister/cpfleet
cpfleet password                          # the boards' CIRCUITPY_WEB_API_PASSWORD, stored locally (mode 600)
cpfleet add 192.168.1.42                  # one board's address; cpfleet finds the others it sees
cpfleet status
```

```
MagTag-1a2b    192.168.1.42    10.3.1  running code.py              931 KB free
MagTag-3c4d    192.168.1.43    10.3.1  ended (crashed or returned; --why) 934 KB free
MagTag-5e6f    192.168.1.44    10.3.1  running code.py              931 KB free
```

## Why

CircuitPython's usual workflow is a USB drive named `CIRCUITPY`. That's awkward with several boards, or
boards on a wall or shelf, and some Macs never mount the drive at all: macOS detects it but creates no
disk. The **web workflow** (CircuitPython 8 and later) runs a small HTTP server on the board, so files
and the serial console are available over Wi-Fi. There are already good tools for parts of this, but
none covers a group of boards end to end:

| Tool | What it does | What's missing for a fleet |
|---|---|---|
| [code.circuitpython.org](https://code.circuitpython.org) | Browser editor over web workflow | One board at a time, by hand |
| [circup](https://github.com/adafruit/circup) | Installs libraries, including over web workflow | Libraries only (cpfleet wraps it: `cpfleet lib`) |
| [circremote](https://github.com/romkey/circremote) | Runs code over serial or web workflow | Doesn't upload files |
| [discotool](https://github.com/Neradoc/discotool) | Finds USB boards and their drives | Needs mounted drives |

cpfleet adds:
- **Fleet-wide commands:** `all`, a name, a name suffix or a list, run in parallel, with one line of output per board.
- **A `status` that doesn't interrupt running code.**
- **Change-aware uploads.**
- **USB provisioning that writes the board's file system directly,** so it works where the drive won't mount.

The output is deliberately compact, with `--json` available, so it's also cheap for AI coding agents to
drive (see [AGENTS.md](AGENTS.md)).

## Requirements

- **The machine running cpfleet:** macOS or Linux, Python 3.9 or later (the Python that ships with macOS is fine).
- **Boards:** CircuitPython 8 or later with Wi-Fi and the web workflow enabled. Version 10 is tested.
- **Provisioning only:** [esptool](https://github.com/espressif/esptool) and [mtools](https://www.gnu.org/software/mtools/) (`brew install esptool mtools`).
- **`cpfleet lib` only:** [circup](https://github.com/adafruit/circup).

## Enable the web workflow on a board

If you've used the [web workflow guide](https://learn.adafruit.com/getting-started-with-web-workflow-using-the-code-editor),
your boards are ready. Otherwise, put this in each board's `settings.toml`. With several boards, use one
web password for all of them:

```toml
CIRCUITPY_WIFI_SSID = "your network"
CIRCUITPY_WIFI_PASSWORD = "your Wi-Fi password"
CIRCUITPY_WEB_API_PASSWORD = "a password for the web workflow"
CIRCUITPY_WEB_INSTANCE_NAME = "Kitchen-Display"
```

The board can't write files over Wi-Fi while a computer has its USB drive. cpfleet's `boot.py`
(installed by `cpfleet provision`, or copy [cpfleet/board_files/boot.py](cpfleet/board_files/boot.py))
hides the USB drive unless a button is held at reset. The serial console stays on USB. A board that is
only powered from USB, not connected to a computer, doesn't need this.

`cpfleet provision` does all of this for you over USB (see below).

## Usage

The target is `all` (the default), a name, the end of a name (`1a2b`), an IP, or a comma list.

| Command | What it does |
|---|---|
| `cpfleet add <ip>…` | Add boards, plus every board they see over mDNS (from `/cp/devices.json`) |
| `cpfleet scan` | Refresh the list: new boards, changed DHCP addresses |
| `cpfleet ls` | List known boards |
| `cpfleet status [target] [--why]` | Version, state (running / error / REPL / ended) and free space, read from the board's status bar without interrupting it. `--why` re-runs an ended program to show its error. |
| `cpfleet push <files or folders> [--to target] [--dest /]` | Upload over Wi-Fi. Unchanged files are skipped, folders are created, `code.py` goes last, and free space is checked first. `settings.toml` is never replaced unless you pass `--settings`. `-n` shows what would change. |
| `cpfleet run <target> "code"` or `-f file.py` | Run Python and print only its output (raw REPL over the websocket, or `--usb PORT`). `code.py` restarts afterwards unless you pass `--stay`. |
| `cpfleet log [target] [--restart] [--seconds 3]` | Console output with escape codes removed. `--restart` catches startup errors. |
| `cpfleet reload` / `reset` / `bootloader [--uf2]` | Restart `code.py`, hard reset (needed after `settings.toml` changes), or reboot into the ROM or UF2 bootloader |
| `cpfleet cat <target> <path>` / `rm <target> <paths>` | Read or delete files |
| `cpfleet settings <target> keys` / `set KEY [VALUE]` / `unset KEY` | Change `settings.toml` without printing values. Leave out VALUE to type it hidden. |
| `cpfleet lib install <names> [--to target]` | Install libraries with circup over Wi-Fi |
| `cpfleet bundle <zip or URL> [--to target]` | Install an Adafruit Learn guide's Project Bundle (below) |
| `cpfleet provision` / `firmware` | Set up or update boards over USB (below) |
| `--json` (before the command) | Machine-readable output |

Examples:

```sh
cpfleet push apps/quotes                       # an example app in this repo, to every board
cpfleet run all "import wifi; print(wifi.radio.ipv4_address)"
cpfleet status 3c4d --why                      # MagTag-3c4d    ...  crashed: NameError: ... (File "code.py", line 3)
cpfleet settings all set CIRCUITPY_WEB_INSTANCE_NAME Hall-Display && cpfleet reset
```

## Adafruit Learn guide projects

Most [Adafruit Learn](https://learn.adafruit.com) projects have a **Download Project Bundle** button on
their code page. It gives a zip with the project's `code.py`, its libraries and assets, one folder per
CircuitPython version. cpfleet installs it on boards over Wi-Fi:

```sh
cpfleet bundle ~/Downloads/MagTag_Showerthoughts.zip --to 1a2b
cpfleet bundle "https://learn.adafruit.com/.../download?type=zip" --to all -n    # -n: show what would upload
```

- It picks the folder for each board's CircuitPython version.
- It pushes everything except `settings.toml`. If the project needs settings, such as an Adafruit IO key, it lists their names so you can set them with `cpfleet settings <board> set KEY`.
- Bundles can be bigger than a small board's file system (the MagTag has 960 KB). cpfleet checks free space before uploading anything.

## Provisioning boards over USB

```sh
cpfleet provision --dry-run           # what it would do to each board on USB
cpfleet provision                     # asks for the Wi-Fi name and password once, for all boards
cpfleet scan                          # after pressing Reset on each board
```

For every board of the profile on USB (default `magtag_2.9_grayscale`):

- **Already on CircuitPython 10:** it keeps the firmware, reboots the board into the ROM bootloader, and rewrites `boot.py` and `settings.toml`. Other keys in `settings.toml` are kept.
- **Anything else** (factory firmware, older CircuitPython, a board already in download mode): it backs up the whole flash to `~/.config/cpfleet/backups/`, installs [TinyUF2](https://github.com/adafruit/tinyuf2) and the latest CircuitPython, sets the boot selector, and creates the file system.
- **The files are written into the board's FAT partition with esptool and mtools, not through the USB drive.** That's why it works on Macs that never mount `CIRCUITPY`.
- **Wi-Fi password:** typed hidden and stored only on the boards.
- **Web workflow password:** shared by all boards, and also kept in `~/.config/cpfleet/password`.

Each board is named `MagTag-<last 4 hex digits of its MAC>`; change that with `--name`.
`cpfleet firmware` updates CircuitPython and keeps the files. `--only 1a2b` limits either command to
particular boards.

Board specifics (partition offsets, TinyUF2 build, the USB-drive button) live in a profile:
[cpfleet/profiles/](cpfleet/profiles/). [CONTRIBUTING.md](CONTRIBUTING.md) explains how to add one.

## How it works

- **HTTP:** `/cp/version.json`, `/cp/devices.json` (the other boards on the network) and `/cp/diskinfo.json` are public. `/fs/…` (GET, PUT, DELETE, with JSON directory listings) uses HTTP Basic auth with an empty user name and the web workflow password.
- **Console:** the board's serial console is a websocket at `/cp/serial/`. On connect, the board sends its status bar (IP, what's running, version), which is how `status` reads state without interrupting anything. `run` uses the raw REPL (Ctrl-A … Ctrl-D), so code arrives exactly as written. Only one client can hold the console at a time.
- **Uploads:** they set the file's modification time (`X-Timestamp`), and FAT stores times in 2-second steps. A later push skips any file whose size matches and whose time is within 2 seconds.
- **Provisioning:** ESP32-S2 MagTags ship with a partition table that both TinyUF2 and CircuitPython use (`ota_0` for CircuitPython, `uf2` for TinyUF2, `ffat` for `CIRCUITPY`). CircuitPython's full `.bin` leaves the boot selector (`otadata`) blank, which makes the board start TinyUF2. cpfleet writes an entry pointing at `ota_0`.

## Example apps

[apps/](apps/) has programs that cpfleet pushes to boards. [apps/quotes](apps/quotes) shows a random
quote on a MagTag's e-ink screen; button A shows another.

## Security

- The web workflow is plain HTTP on your local network. Treat its password as a LAN secret, and don't expose boards to the internet.
- cpfleet passes the password in request headers, never on the command line. The exception is `cpfleet lib`, because circup only takes it as an argument.
- Secrets in `settings.toml` are never printed: `settings keys` shows names only.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): tests run with `python3 -m unittest discover tests` and need no hardware.

## License

MIT, see [LICENSE](LICENSE).
