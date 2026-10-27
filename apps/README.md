# Example apps

Each folder is pushed to boards as is: `cpfleet push apps/<name> --to <boards>`.

| App | Boards | What it does |
|---|---|---|
| [quotes](quotes) | Adafruit MagTag | A random quote from `quotes.txt` every 15 minutes; button A shows another. Built-in modules only (no `lib/`). Edit `quotes.txt` (`text \| author` per line, plain ASCII) and push again. |

To go back to an app after trying an Adafruit Learn project (`cpfleet bundle`), push it again. Files the
project added stay on the board until you delete them with `cpfleet rm`.
