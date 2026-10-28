# Claude Code agents on a MagTag: the sessions and running subagents that extras/agents_watch.py reports,
# with the LEDs pulsing amber while a session waits for you.
#   A: switch between agents and a quote    B: acknowledge (LEDs off until something new waits)
#   C: another quote (in quote view)
# The screen redraws at most every AGENTS_REDRAW seconds (default 30) for new data; buttons redraw at once.
# Where the data comes from, first match wins:
#   USB: lines on usb_cdc.data from agents_watch.py --usb (needs CPFLEET_USB_DATA = 1 and a reset; boot.py)
#   Wi-Fi: AGENTS_URL if set, else the watcher found by name over mDNS (_cpagents._tcp); a failing
#          AGENTS_URL is looked up again by name. AGENTS_TOKEN, if set, is sent to a watcher that wants one.
# Built-in modules only (no lib/); usblink.py is this app's USB line protocol.
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

import usblink

try:
    import usb_cdc
    usb = usb_cdc.data  # None unless boot.py enabled the second USB serial channel
except ImportError:
    usb = None

URL = os.getenv("AGENTS_URL")    # an override; without it the watcher is found by name
TOKEN = os.getenv("AGENTS_TOKEN")
NAME = os.getenv("CIRCUITPY_WEB_INSTANCE_NAME") or "MagTag"
SERVICE = "_cpagents"            # the watcher's Bonjour service type, _cpagents._tcp
POLL = 3            # seconds between fetches
USB_STALE = 10      # seconds without a USB line before falling back to Wi-Fi
LOOKUP_EVERY = 30   # seconds between mDNS lookups while there's no working address
LOOKUP_AFTER = 3    # failed fetches in a row before looking the watcher up again
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
        auth = "Authorization: Bearer %s\r\n" % TOKEN if TOKEN else ""
        sock.send(("GET /%s HTTP/1.0\r\nHost: %s\r\n%s\r\n" % (path, host, auth)).encode())
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


mdns_server = None  # one for the whole run: deinit could stop the web workflow's own mDNS


def discover():
    """The watcher's URL from its Bonjour advertisement, or None."""
    global mdns_server
    try:
        import mdns
        if mdns_server is None:
            mdns_server = mdns.Server(wifi.radio)
        for r in mdns_server.find(service_type=SERVICE, protocol="_tcp", timeout=2):
            if r.ipv4_address and r.port:
                return "http://%s:%d/agents.json" % (r.ipv4_address, r.port)
    except Exception as e:
        print("lookup failed:", e)
    return None


def read_usb():
    """The newest snapshot among the lines waiting on the USB data channel, or None. Answers probes."""
    n = usb.in_waiting
    if not n:
        return None
    snap = None
    for line in reader.feed(usb.read(n)):
        kind, value = usblink.parse(line)
        if kind == "probe":
            usb.write(usblink.hello(NAME))
        elif kind == "snapshot":
            snap = value
    return snap


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


def agent_lines(d, cols, rows, offline_since, source):
    sessions = d.get("sessions", [])
    gen = d.get("generated", 0)
    head = "AGENTS"
    if d.get("waiting"):
        head += "  %d waiting" % d["waiting"]
    if offline_since:
        head = "WATCHER OFFLINE since " + d.get("clock", "?")
    tail = (source + " " if source else "") + d.get("clock", "")
    lines = [pad(head, cols - 12) + rjust(tail, 11), ""]
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
url, fails, last_lookup = URL, 0, -LOOKUP_EVERY
reader, last_usb, source = usblink.LineReader(), None, ""
was_known = False
if usb:
    usb.timeout, usb.write_timeout = 0, 0.2

while True:
    now = time.monotonic()
    if usb:
        try:
            snap = read_usb()
        except Exception as e:
            snap = None
            print("usb read failed:", e)
        if snap is not None:
            if last_usb is None or now - last_usb >= USB_STALE:
                print("data from USB")
            data, last_ok, last_usb, source = snap, now, now, "USB"
    usb_fresh = last_usb is not None and now - last_usb < USB_STALE
    if not usb_fresh and now - last_fetch >= POLL and wifi.radio.ipv4_address:
        last_fetch = now
        if (url is None or fails >= LOOKUP_AFTER) and now - last_lookup >= LOOKUP_EVERY:
            last_lookup = now
            found = discover()
            if found and found != url:
                print("watcher found at", found)
                url, fails = found, 0
        if url:
            try:
                data = fetch(url)
                if source != "WiFi":
                    print("data from Wi-Fi:", url)
                last_ok, fails, source = now, 0, "WiFi"
            except Exception as e:  # keep showing the last data
                fails += 1
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

    known = bool(url) or last_usb is not None
    if known and not was_known:  # replace the "looking for the watcher" screen at once
        force, was_known = True, True
    offline = known and now - last_ok > OFFLINE
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

    if not known:
        lines, scale = ["Looking for the agents watcher...", "",
                        "Start extras/agents_watch.py on the Mac", "(Wi-Fi, found by name), or with --usb",
                        "and plug this board into it. Or set",
                        "AGENTS_URL http://<mac-ip>:8766/agents.json"], 1
    elif view == "agents":
        cols, rows = display.width // CW, display.height // CH
        lines, scale = agent_lines(data, cols, rows, offline, source), 1
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
