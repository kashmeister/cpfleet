# Claude Code agents on a MagTag: the sessions and running subagents that extras/agents_watch.py reports,
# with the LEDs pulsing amber while a session waits for you.
#   A: switch between agents and a quote    B: acknowledge (LEDs off until something new waits)
#   C: another quote (in quote view)
# The screen redraws at most every AGENTS_REDRAW seconds (default 30) for new data; buttons redraw at once.
# Set the watcher's address once:  cpfleet settings <boards> set AGENTS_URL http://<mac-ip>:8766/agents.json
# Built-in modules only (no lib/).
import json
import math
import os
import random
import time

import board
import digitalio
import displayio
import keypad
import neopixel_write
import socketpool
import terminalio
import wifi

URL = os.getenv("AGENTS_URL")
POLL = 3            # seconds between fetches
FRESH = 3600        # waiting sessions newer than this light the LEDs
OFFLINE = 30        # seconds without a reply before saying the watcher is offline
QUOTE_EVERY = 15 * 60
# Minimum seconds between e-ink redraws for new data, to spare the panel; the LEDs still react at once.
# Button presses redraw straight away. Override per board: cpfleet settings <b> set AGENTS_REDRAW 60
REDRAW = int(os.getenv("AGENTS_REDRAW") or 30)

display = board.DISPLAY
FONT = terminalio.FONT
CW, CH = FONT.get_bounding_box()[:2]
BLANK = FONT.get_glyph(ord(" ")).tile_index
wifi.radio.power_management = wifi.PowerManagement.NONE  # answer quickly instead of sleeping between beacons
pool = socketpool.SocketPool(wifi.radio)


def pad(s, n):  # CircuitPython's str has no ljust/rjust
    return s[:n] + " " * max(0, n - len(s))


def rjust(s, n):
    return " " * max(0, n - len(s)) + s


# ---- watcher -------------------------------------------------------------------------------------------

def fetch(url):
    host_port, _, path = url.split("://", 1)[1].partition("/")
    host, _, port = host_port.partition(":")
    port = int(port or 80)
    sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(pool.getaddrinfo(host, port)[0][4])
        sock.send(("GET /%s HTTP/1.0\r\nHost: %s\r\n\r\n" % (path, host)).encode())
        data, chunk = bytearray(), bytearray(1024)
        while True:
            n = sock.recv_into(chunk)
            if not n:
                break
            data += chunk[:n]
    finally:
        sock.close()
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n")[0]:
        raise OSError("watcher replied " + head.split(b"\r\n")[0].decode())
    return json.loads(body)


def age(seconds):
    """Coarse, so the screen doesn't redraw every minute: now, 1-9m, 10m steps, then hours."""
    m = seconds // 60
    if m < 1:
        return "now"
    if m < 10:
        return "%dm" % m
    if m < 60:
        return "%dm" % (m // 10 * 10)
    return "%dh" % (m // 60)


SYMBOL = {"question": "?", "your-turn": "!", "busy": "~", "working": ">", "idle": "."}
LABEL = {"your-turn": "your turn", "busy": "busy?", "working": "working", "idle": "idle"}


def agent_lines(d, cols, rows, offline_since):
    sessions = d.get("sessions", [])
    gen = d.get("generated", 0)
    head = "AGENTS"
    if d.get("waiting"):
        head += "  %d waiting" % d["waiting"]
    if offline_since:
        head = "WATCHER OFFLINE since " + d.get("clock", "?")
    lines = [pad(head, cols - 6) + rjust(d.get("clock", ""), 5), ""]
    if not sessions:
        lines.append("no Claude Code sessions in the last hours")
    for s in sessions:
        label = (s.get("detail") or "question") if s["state"] == "question" else LABEL[s["state"]]
        if s["state"] in ("question", "your-turn"):
            label += " " + age(gen - s["since"])
        name_w = cols - 3 - len(label)
        lines.append(SYMBOL[s["state"]] + " " + pad(s["title"], name_w) + label)
        subs = s.get("subagents", [])
        for sub in subs[:2]:
            lines.append(pad("   - %s: %s" % (sub["type"], sub["desc"]), cols - 1))
        if len(subs) > 2:
            lines.append("   + %d more subagents" % (len(subs) - 2))
    if len(lines) > rows:
        lines = lines[:rows - 1] + ["  + %d more" % (len(lines) - rows + 1)]
    return lines


# ---- quotes (shares /quotes.txt with apps/quotes) ------------------------------------------------------

def load_quotes():
    out = []
    try:
        with open("/quotes.txt") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    text, _, author = line.partition("|")
                    out.append((text.strip(), author.strip()))
    except OSError:
        pass
    return out or [("Add quotes to quotes.txt.", "")]


def wrap(text, width):
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = line + " " + word if line else word
    return lines + ([line] if line else [])


def quote_lines(quote, cols, rows):
    text, author = quote
    lines = [" " * max(0, (cols - 1 - len(l)) // 2) + l for l in wrap(text, cols - 1)]
    if author:
        lines.append(rjust("- " + author, cols - 1))
    return [""] * max(0, (rows - len(lines)) // 2) + lines


# ---- screen and LEDs -----------------------------------------------------------------------------------

def draw(lines, scale):
    cols, rows = display.width // (CW * scale), display.height // (CH * scale)
    palette = displayio.Palette(2)
    palette[0], palette[1] = 0xFFFFFF, 0x000000
    grid = displayio.TileGrid(FONT.bitmap, pixel_shader=palette, width=cols, height=rows,
                              tile_width=CW, tile_height=CH, default_tile=BLANK)
    terminalio.Terminal(grid, FONT).write("\r\n".join(l[:cols - 1] for l in lines[:rows]))
    group = displayio.Group(scale=scale)
    group.append(grid)
    display.root_group = group
    display.refresh()


led_power = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
led_power.switch_to_output(value=False)  # low = LEDs powered on the MagTag
led_pin = digitalio.DigitalInOut(board.NEOPIXEL)
led_pin.direction = digitalio.Direction.OUTPUT
led_last = None


def leds(r, g, b):
    global led_last
    if (r, g, b) != led_last:
        neopixel_write.neopixel_write(led_pin, bytes([g, r, b] * 4))
        led_last = (r, g, b)


# ---- main loop -----------------------------------------------------------------------------------------

keys = keypad.Keys((board.BUTTON_A, board.BUTTON_B, board.BUTTON_C), value_when_pressed=False, pull=True)
quotes = load_quotes()
quote = random.choice(quotes)
quote_at = time.monotonic()
view, data, shown, acked = "agents", {}, None, set()
last_draw, force = -REDRAW, True
last_fetch, last_ok = -POLL, time.monotonic()

while True:
    now = time.monotonic()
    if URL and now - last_fetch >= POLL:
        last_fetch = now
        try:
            data = fetch(URL)
            last_ok = now
        except Exception as e:  # keep showing the last data
            print("fetch failed:", e)

    event = keys.events.get()
    if event and event.pressed:
        force = True
        if event.key_number == 0:
            view = "quote" if view == "agents" else "agents"
        elif event.key_number == 1:
            acked |= {(s["id"], s["since"]) for s in data.get("sessions", [])}
        elif event.key_number == 2 and view == "quote":
            quote, quote_at = random.choice(quotes), now
    if view == "quote" and now - quote_at > QUOTE_EVERY:
        quote, quote_at = random.choice(quotes), now

    offline = URL and now - last_ok > OFFLINE
    gen = data.get("generated", 0)
    waiting = [s for s in data.get("sessions", []) if s["state"] in ("question", "your-turn")
               and gen - s["since"] < FRESH and (s["id"], s["since"]) not in acked]
    if waiting and not offline:
        level = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(now * 3))  # pulse
        leds(int(90 * level), int(40 * level), 0)
    elif offline:
        leds(8, 0, 0)
    else:
        leds(0, 0, 0)

    if not URL:
        lines, scale = ["Set the watcher address:", "cpfleet settings <board> set", "AGENTS_URL",
                        "http://<mac-ip>:8766/agents.json", "then cpfleet reset <board>"], 1
    elif view == "agents":
        cols, rows = display.width // CW, display.height // CH
        lines, scale = agent_lines(data, cols, rows, offline), 1
    else:
        cols, rows = display.width // (CW * 2), display.height // (CH * 2)
        lines, scale = quote_lines(quote, cols, rows), 2
    due = force or now - last_draw >= REDRAW
    if due and (lines, scale) != shown and display.time_to_refresh == 0 and not display.busy:
        try:
            draw(lines, scale)
            shown, last_draw, force = (lines, scale), now, False
        except RuntimeError:  # refreshed too soon; try again next time round
            pass
    elif force and (lines, scale) == shown:
        force = False
    time.sleep(0.05)
