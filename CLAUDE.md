# cpfleet: notes for coding agents

cpfleet is a standard-library-only Python CLI (3.9+, the version macOS ships) for managing CircuitPython
boards over the web workflow. Read [AGENTS.md](AGENTS.md) for *using* it; this file is for *changing* it.
Machine-specific details (which Mac, which boards, how to publish) live in `CLAUDE.local.md`, which is
git-ignored.

## Rules

- **No dependencies.** The standard library only, and code that runs on Python 3.9: no `match`, no `tomllib`, no `X | Y` type hints.
- **Compact output.** One line per board, with `--json` for machines. Errors are one line, never a traceback. New commands follow `cli.each()` and `cli.line()`.
- **Never print, log or commit secrets.** That covers `settings.toml` values, Wi-Fi and web-workflow passwords, and board inventories. Provisioning reads passwords with `getpass`. Temporary images that hold them are blanked and deleted.
- **Example values only in the repo** (192.168.1.x, `MagTag-1a2b`, `a1b2c3d4…`), never real IPs, MACs, UIDs or network names. Scan before every push: `git grep -nE '192\.168\.|[0-9a-f]{12}'`, and check anything you've added.
- **Run the tests:** `python3 -m unittest discover tests`. They need no hardware, and should pass on 3.9 and the newest Python. Anything touching how cpfleet talks to boards also needs a try against a real board.

## Hardware behaviour (learned the hard way, ESP32-S2 MagTag on CircuitPython 10)

1. **Switched to download mode by software** (`microcontroller.RunMode.BOOTLOADER`), the board reboots a few seconds after each esptool session ends, and after a `chip-id`. So each esptool step is its own session straight after a switch (`provision.rom_session`). A fresh install is assembled on the host and written as one image (`provision.full_image`). The ROM port is found as "the new port that appeared", never with `chip-id`. A side effect: no Reset is needed afterwards.
2. **Never use esptool's own reset** (`--after hard-reset`): the board drops off USB until Reset is pressed. Always pass `--before no-reset --after no-reset`.
3. **CircuitPython's full `.bin` blanks `otadata`,** so the board boots TinyUF2 (the factory app). Always write the boot selector entry (`provision.otadata`, CRC `4743989a` for sequence 1).
4. **The USB console sometimes stops taking input while still printing;** a reset clears it. The USB port name contains the board's UID (`version.json` `"UID"`), so `provision.wifi_twin` falls back to the board's Wi-Fi console.
5. **Some Macs never mount `CIRCUITPY`** (macOS loads `IOBlockStorageDriver` but creates no disk). Never rely on the drive: provisioning edits the FAT partition with esptool and mtools.
6. **Web workflow writes need the USB drive hidden** (`storage.disable_usb_drive()` in `boot.py`); otherwise you get 409.
7. **On connect, the websocket console sends the status bar** (`ip | state | version`), which is how `status` avoids interrupting code. After a crash the bar says `Done`, not the error (hence `status --why`).
8. **Use raw REPL** (Ctrl-A … Ctrl-D, small chunks) for code; typing into the friendly REPL loses newlines and characters. A board that is auto-reloading after a file write ignores the first interrupts (`repl.enter_raw` retries). `repl.reload` must wait for "soft reboot", or closing the connection drops the Ctrl-D.
9. **CircuitPython strings have no `rjust`** (and no reliable `center`), and `terminalio.Terminal` wraps a full-width line by itself. Device code in `apps/` has to allow for both.
10. **Downloads need a non-default User-Agent:** Adafruit Learn returns 403 to Python's.
11. **The USB data channel** (`usb_cdc.enable(console=True, data=True)` in `boot.py`, behind `CPFLEET_USB_DATA`) needs `usb_hid` and `usb_midi` off on the ESP32-S2, which is short of USB endpoints. It takes a hard reset (`cpfleet reset`); a soft reload keeps it, and keeps the ports. macOS then shows two ports per board, named alike but for the last digit: the console ends in `1`, the data channel in `3`. The data port doesn't answer the REPL, so `provision.serial_ports` lists only the first port of each pair.
12. **mDNS on the board:** `mdns.Server(wifi.radio)` works alongside the web workflow's own; keep one for the whole run rather than deinit it. `find()` right after a service has registered can come back empty, so look again later rather than giving up.

## Layout

- `cpfleet/cli.py`: commands.
- `web.py`: HTTP.
- `repl.py`: websocket and serial, raw REPL.
- `sync.py`: uploads.
- `inventory.py`: boards and password.
- `settings.py`: editing `settings.toml`.
- `provision.py`: USB provisioning and firmware.
- `bundle.py`: Adafruit Project Bundles.
- `profiles/*.json`: board profiles.
- `board_files/boot.py`: the `boot.py` that provisioning installs.
- `apps/`: device programs. `apps/agents/usblink.py` is the USB line protocol the board shares with `extras/agents_watch.py`; the tests import it.
- `extras/agents_watch.py`: the host side of `apps/agents` (HTTP, Bonjour via `dns-sd`, USB).
- `tests/`.

## Releasing

1. Bump `__version__` in `cpfleet/__init__.py` and in `pyproject.toml`.
2. Tag `vX.Y.Z` and make a GitHub release with notes.
3. Update `url` and `sha256` in the `cpfleet.rb` formula in the `kashmeister/homebrew-tap` repo.
4. `brew install` and `brew test` it on a Mac with current Command Line Tools.
