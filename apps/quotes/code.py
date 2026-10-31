# Random quotes on the MagTag's e-ink screen.
# Shows a new quote every INTERVAL seconds; button A shows another one now.
# Quotes come from /quotes.txt ("text | author" per line). Update either file over Wi-Fi with deploy.sh.
# The LEDs glow a dim red, easy on the eyes at night (CPFLEET_GLOW in settings.toml, 0-255; 0 is off).
# Uses only built-in modules (terminalio's font and text grid), so lib/ can stay empty.
import os
import random
import time

import board
import digitalio
import displayio
import keypad
import neopixel_write
import terminalio

INTERVAL = 15 * 60  # seconds between quotes
GLOW = int(os.getenv("CPFLEET_GLOW") or 25)  # 1 is the dimmest
WARM = (1.0, 0.0, 0.0)  # dim red glow, easy on the eyes at night: (25, 0, 0) at the default brightness; red can run brighter, the eye is less sensitive to it
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
    # Place the characters ourselves, vertically centred. terminalio.Terminal writes from a cursor at the
    # bottom row and scrolls up, which doesn't reliably put text where you mean it.
    top = max(0, (rows - len(lines)) // 2)
    for r, line in enumerate(lines[:rows - top]):
        for c, ch in enumerate(line[:cols]):
            if ch != " ":
                glyph = FONT.get_glyph(ord(ch))
                if glyph is not None:
                    grid[c, top + r] = glyph.tile_index
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


led_power = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
led_power.switch_to_output(value=GLOW == 0)  # low = LEDs powered on the MagTag
led_pin = digitalio.DigitalInOut(board.NEOPIXEL)
led_pin.direction = digitalio.Direction.OUTPUT
r, g, b = (int(GLOW * f + 0.5) for f in WARM)
neopixel_write.neopixel_write(led_pin, bytes([g, r, b] * 4))  # the LEDs take green, red, blue
keys = keypad.Keys((board.BUTTON_A,), value_when_pressed=False, pull=True)
show(pick())
next_at = time.monotonic() + INTERVAL
while True:
    event = keys.events.get()
    if (event and event.pressed) or time.monotonic() >= next_at:
        show(pick())
        next_at = time.monotonic() + INTERVAL
    time.sleep(0.05)
