# Contributing

## Build and test

There's nothing to build. Run from a checkout with `python3 -m cpfleet`, or install it with `pipx install -e .`.

```sh
python3 -m unittest discover tests     # no hardware needed; also run it with the oldest supported Python (3.9)
```

The tests use a fake web workflow server and a fake websocket board. If you change how cpfleet talks to
boards, also try it against a real board: `status`, `push`, `run`, `log --restart`, `reload`.

## How the code is laid out

| File | Role |
|---|---|
| `cpfleet/cli.py` | Commands, argument parsing, one-line-per-board output, running boards in parallel |
| `cpfleet/web.py` | Web workflow HTTP API |
| `cpfleet/repl.py` | Websocket client for `/cp/serial/`, USB serial, raw REPL, status bar parsing |
| `cpfleet/sync.py` | Upload plan and change detection |
| `cpfleet/inventory.py` | `~/.config/cpfleet/boards.json`, the password file, discovery, target names |
| `cpfleet/settings.py` | Editing `settings.toml` without exposing values |
| `cpfleet/provision.py` | USB provisioning and firmware updates (esptool + mtools) |
| `cpfleet/bundle.py` | Adafruit Learn Project Bundles |
| `cpfleet/profiles/*.json` | Board profiles for provisioning |
| `cpfleet/board_files/boot.py` | The `boot.py` provisioning installs |

It must stay standard-library only and run on Python 3.9 (the version that ships with macOS).

## Adding a board profile

Only provisioning needs a profile. Every other command works with any web workflow board.

1. **Find the board's IDs:**
   - CircuitPython board id: `import board; print(board.board_id)`, or the downloads page on circuitpython.org.
   - Its [TinyUF2](https://github.com/adafruit/tinyuf2/releases) build name, if it has one.
2. **Read its partition table** from a board running CircuitPython. In ROM download mode:
   `esptool --chip <chip> --port <port> --before no-reset --after no-reset read-flash 0x8000 0xC00 pt.bin`.
   Decode `pt.bin` with ESP-IDF's `gen_esp32part.py`, or by hand: 32-byte entries starting `AA 50`.
   Note the `otadata` and `ffat` offsets and sizes.
3. **Copy `profiles/magtag_2.9_grayscale.json`** and fill in `board_id`, `chip`, `tinyuf2_board`, `flash_size`, the offsets, and `usb_drive_pin`: a button that `boot.py` reads, or `""` if there isn't one.
4. **Test on a real board:** run `cpfleet provision --profile <name> --dry-run`, then a full run with `--reinstall --only <mac suffix>`. Confirm the board joins Wi-Fi and that `cpfleet status` sees it.

Boards without TinyUF2, or with a different partition layout, may need changes to `provision.main()`.
Please open an issue first.

## Pull requests

- Keep each pull request to one change, with tests where they make sense.
- Match the style around it: short functions, compact output, no new dependencies.
- Never commit board inventories, passwords, IP addresses or flash images (`.gitignore` covers the usual paths).
