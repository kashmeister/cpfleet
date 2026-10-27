# Random quotes on the MagTag's e-ink screen.
# Shows a new quote every INTERVAL seconds; button A shows another one now.
# Quotes come from /quotes.txt ("text | author" per line). Update either file over Wi-Fi with deploy.sh.
# Uses only built-in modules (terminalio's font and text grid), so lib/ can stay empty.
import random
import time

import board
import displayio
import keypad
import terminalio

INTERVAL = 15 * 60  # seconds between quotes
display = board.DISPLAY
FONT = terminalio.FONT
CW, CH = FONT.get_bounding_box()[:2]  # one character cell, in pixels
BLANK = FONT.get_glyph(ord(" ")).tile_index


def load_quotes():
    quotes = []
    with open("/quotes.txt") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                text, _, author = line.partition("|")
                quotes.append((text.strip(), author.strip()))
    return quotes or [("Add quotes to quotes.txt.", "")]


def wrap(text, width):
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = line + " " + word if line else word
    if line:
        lines.append(line)
    return lines


def center(s, width):  # CircuitPython's str has no reliable center()/rjust()
    return " " * max(0, (width - len(s)) // 2) + s


def rjust(s, width):
    return " " * max(0, width - len(s)) + s


def layout(text, author):
    """Pick the largest text size the quote fits at: centered lines, author right-aligned below."""
    for scale in (3, 2, 1):
        cols, rows = display.width // (CW * scale), display.height // (CH * scale)
        width = cols - 1  # a full-width line makes the terminal wrap by itself, doubling the line break
        lines = [center(l, width) for l in wrap(text, width)]
        if author:
            lines.append(rjust(("- " + author)[:width], width))
        if len(lines) <= rows:
            return scale, cols, rows, lines
    return scale, cols, rows, lines[:rows]


def show(quote):
    scale, cols, rows, lines = layout(*quote)
    palette = displayio.Palette(2)
    palette[0], palette[1] = 0xFFFFFF, 0x000000
    grid = displayio.TileGrid(FONT.bitmap, pixel_shader=palette, width=cols, height=rows,
                              tile_width=CW, tile_height=CH, default_tile=BLANK)
    term = terminalio.Terminal(grid, FONT)
    term.write("\r\n" * ((rows - len(lines)) // 2) + "\r\n".join(lines))
    group = displayio.Group(scale=scale,
                            x=(display.width - cols * CW * scale) // 2,
                            y=(display.height - rows * CH * scale) // 2)
    group.append(grid)
    display.root_group = group
    time.sleep(display.time_to_refresh + 0.05)  # e-ink allows one refresh every ~5 s
    display.refresh()


quotes = load_quotes()
last = None


def pick():
    global last
    i = random.randrange(len(quotes))
    while len(quotes) > 1 and i == last:
        i = random.randrange(len(quotes))
    last = i
    return quotes[i]


keys = keypad.Keys((board.BUTTON_A,), value_when_pressed=False, pull=True)
show(pick())
next_at = time.monotonic() + INTERVAL
while True:
    event = keys.events.get()
    if (event and event.pressed) or time.monotonic() >= next_at:
        show(pick())
        next_at = time.monotonic() + INTERVAL
    time.sleep(0.05)
