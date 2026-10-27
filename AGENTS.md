# Driving cpfleet from an AI agent

Use cpfleet for every board interaction, not ad-hoc serial or curl scripts: each call is one short
line, and so is its output. Add `--json` before the command when you need to parse the result.

```sh
cpfleet status                      # one line per board: version, state, free space; doesn't interrupt code
cpfleet status <b> --why            # if a board's code.py ended, re-run it and report the error and line
cpfleet run <b> "print(1 + 1)"      # prints only the output; errors go to stderr, last line only; exit 1 on error
cpfleet run <b> -f snippet.py       # longer code; code.py restarts afterwards (--stay to keep the REPL)
cpfleet log <b> --restart --lines 10
cpfleet push <dir-or-files> --to <b|all>   # skips unchanged files; never replaces settings.toml without --settings
cpfleet cat <b> /code.py
cpfleet reload <b|all>              # after editing, if auto-reload didn't happen
cpfleet settings <b> keys           # names only; never print or echo setting values
```

`<b>` is a board name, the end of one (`1a2b`), an IP, `all`, or a comma list.

Rules:

- **Don't read or print secrets.** Don't `cat settings.toml`; use `cpfleet settings <b> keys`. To change a secret, run `cpfleet settings <b> set KEY` and let the user type the value.
- **`provision` and `firmware` write to flash over USB, and they prompt for passwords.** Run them only when the user asks, in a terminal where the user can type. Use `--dry-run` first.
- **`bootloader` and `reset` take a board off Wi-Fi for a while.** After `bootloader`, it stays off until someone presses Reset or the board is flashed.
- **Only one client can hold a board's console.** If you get "another client already has the board's serial console open", close the browser editor or other tool first.
