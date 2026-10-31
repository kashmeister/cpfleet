# Coding agents on a MagTag: the Claude Code, Codex and opencode sessions that extras/agents_watch.py
# reports, as a grid of cards, each under one of the four LEDs and above one of the four buttons. A card
# that waits for you has a black bar along its top, and its LED fades slowly between red and orange. The LEDs
# otherwise glow a dim red, easy on the eyes at night (CPFLEET_GLOW, 0-255; 0 is off). All dark: no watcher.
#   The 4 sessions that matter most, with their subagents under the title.
#   A button: that column's session on a page of its own, frozen as it was when pressed: what it's doing,
#             its subagents and how its latest reply ends (from a watcher run with --text). That counts as
#             having seen it (its bar and LED go). Any button, or AGENTS_PAGE seconds (20), goes back.
# Order: waiting for you, then busy and working, then the rest, most recent first; a session keeps its
# place while it stays on screen. With no sessions at all, a quote (any button: another).
# The screen redraws at most every AGENTS_REDRAW seconds (default 30) for new data, except at once for a
# new session waiting for you, or a button.
# Where the data comes from, first match wins:
#   USB: lines on usb_cdc.data from agents_watch.py --usb (needs CPFLEET_USB_DATA = 1 and a reset; boot.py)
#   Wi-Fi: AGENTS_URL if set, else the watcher found by name over mDNS (_cpagents._tcp); a failing
#          AGENTS_URL is looked up again by name. AGENTS_TOKEN, if set, is sent to a watcher that wants one.
# Built-in modules only (no lib/); usblink.py is this app's USB line protocol, views.py its screens.
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
import vectorio
import wifi

import usblink
import views
from views import rjust

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
OFFLINE = 30        # seconds without a reply before saying the watcher is offline
QUOTE_EVERY = 15 * 60
PAGE_FOR = int(os.getenv("AGENTS_PAGE") or 20)  # seconds a session's page stays up without a button
# Minimum seconds between e-ink redraws for new data, to spare the panel; the LEDs still react at once.
# Override per board: cpfleet settings <b> set AGENTS_REDRAW 60
REDRAW = int(os.getenv("AGENTS_REDRAW") or 30)
GLOW = int(os.getenv("CPFLEET_GLOW") or 25)  # the LEDs' constant brightness, 0-255 (1 is the dimmest)
WARM = (1.0, 0.0, 0.0)                      # the constant glow: dim red, (25, 0, 0) at the default brightness; red can run brighter, the eye is less sensitive to it
PULSE_EVERY = 3.6                           # seconds: one slow, relaxed fade red -> orange -> red
PULSE_TO = (30, 5, 0)                      # the colour a waiting column fades towards: a deep amber like a BMW instrument cluster (little green: these LEDs turn yellow fast)
# The LEDs take whole steps (0-255), and so few between the glow and PULSE_TO that a plain fade visibly
# steps. While a column pulses, the loop runs about every 5 ms instead of 50 and alternates each channel
# between the two whole values around it (ordered dithering): the eye blends them into a level between.
DITHER = (0.125, 0.625, 0.375, 0.875)
PULSE_NAP, IDLE_NAP = 0.005, 0.05          # seconds the loop sleeps while a column pulses, and otherwise
# Which LED is over which column, left to right (neopixel_write's order): AGENTS_LEDS = "3210" etc.
LED_ORDER = [int(c) for c in (os.getenv("AGENTS_LEDS") or "3210")]

display = board.DISPLAY
FONT = terminalio.FONT
CW, CH = FONT.get_bounding_box()[:2]
wifi.radio.power_management = wifi.PowerManagement.NONE  # answer quickly instead of sleeping between beacons
pool = socketpool.SocketPool(wifi.radio)


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


# ---- screen --------------------------------------------------------------------------------------------

# Pictures drawn in a 12 x 12 cell: the states' icons, and a check mark for finished subagents in the
# font's 6 x 12 cell (its capitals' rows 2-8; terminalio's font has no check mark).
ICON_SHAPES = {
    "question": ("   ######   ", "  ##    ##  ", "  ##    ##  ", "        ##  ", "       ##   ", "     ###    ",
                 "     ##     ", "     ##     ", "            ", "     ##     ", "     ##     ", "            "),
    "your-turn": ("            ", "  ###  ###  ", "  ###  ###  ", "  ###  ###  ", "  ###  ###  ", "  ###  ###  ",
                  "  ###  ###  ", "  ###  ###  ", "  ###  ###  ", "  ###  ###  ", "  ###  ###  ", "            "),
    "working": ("            ", "  ##        ", "  ####      ", "  ######    ", "  ########  ", "  ######### ",
                "  ######### ", "  ########  ", "  ######    ", "  ####      ", "  ##        ", "            "),
    "background": ("     ##     ", "  #  ##  #  ", " ### ## ### ", "  ########  ", "  ##    ##  ", "####    ####",
                   "####    ####", "  ##    ##  ", "  ########  ", " ### ## ### ", "  #  ##  #  ", "     ##     "),
    "busy": (" ########## ", "  #      #  ", "   #    #   ", "    #  #    ", "     ##     ", "     ##     ",
             "    #  #    ", "   # ## #   ", "  # #### #  ", "  ########  ", " ########## ", "            "),
    "idle": ("            ", " ########## ", " ########## ", "        ##  ", "       ##   ", "      ##    ",
             "     ##     ", "    ##      ", "   ##       ", " ########## ", " ########## ", "            "),
}
CHECK_SHAPE = ("", "", "    #", "    #", "   ##", "   # ", "# ## ", "###  ", " #   ")


def picture(shape, w, h, xs=None, ys=None):
    """A two-colour Bitmap of shape (rows of "#"), w x h, each pixel from the shape's (xs[x], ys[y])."""
    bmp = displayio.Bitmap(w, h, 2)
    for y in range(h):
        row = shape[ys[y] if ys else y] if (ys[y] if ys else y) < len(shape) else ""
        for x in range(w):
            sx = xs[x] if xs else x
            if sx < len(row) and row[sx] == "#":
                bmp[x, y] = 1
    return bmp


ICONS = dict((k, picture(v, 12, 12)) for k, v in ICON_SHAPES.items())


class Font:
    """terminalio's font, or a copy of its printable characters num/den times the size (each pixel taken
    from the nearest one of the original), as a tile sheet; with a check mark of the same size."""

    def __init__(self, num=1, den=1):
        self.cw, self.ch = CW * num // den, CH * num // den
        xs = [x * den // num for x in range(self.cw)]  # the original column and row of each new one
        ys = [y * den // num for y in range(self.ch)]
        self.check = picture(CHECK_SHAPE, self.cw, self.ch, xs, ys)
        if num == den:
            self.bitmap, self.tiles = FONT.bitmap, None
            self.blank = FONT.get_glyph(ord(" ")).tile_index
            return
        src, per_row = FONT.bitmap, FONT.bitmap.width // CW
        self.bitmap = displayio.Bitmap(self.cw * 95, self.ch, 2)
        self.tiles = {}
        for i in range(95):  # " " to "~"
            glyph = FONT.get_glyph(32 + i)
            if glyph is None:
                continue
            ox, oy = glyph.tile_index % per_row * CW, glyph.tile_index // per_row * CH
            for y in range(self.ch):
                sy = oy + ys[y]
                for x in range(self.cw):
                    if src[ox + xs[x], sy]:
                        self.bitmap[i * self.cw + x, y] = 1
            self.tiles[32 + i] = i
        self.blank = self.tiles[32]

    def tile(self, ch):
        if self.tiles is not None:
            return self.tiles.get(ord(ch))
        glyph = FONT.get_glyph(ord(ch))
        return glyph.tile_index if glyph is not None else None


SMALL = Font()       # 6 x 12
LARGE = Font(3, 2)   # 9 x 18

PAPER = displayio.Palette(2)
PAPER[0], PAPER[1] = 0xFFFFFF, 0x000000


def text(group, lines, font, x, y, cols, rows, palette, scale=1):
    """lines into group at (x, y), each character placed from the top-left (terminalio.Terminal writes from
    the bottom row and scrolls up, so a short list would end up at the bottom), check marks drawn."""
    g = displayio.Group(scale=scale, x=x, y=y)
    grid = displayio.TileGrid(font.bitmap, pixel_shader=palette, width=cols, height=rows,
                              tile_width=font.cw, tile_height=font.ch, default_tile=font.blank)
    g.append(grid)
    for r, line in enumerate(lines[:rows]):
        for c, ch in enumerate(line[:cols]):
            if ch == views.CHECK:
                g.append(displayio.TileGrid(font.check, pixel_shader=palette, x=c * font.cw, y=r * font.ch))
            elif ch != " ":
                t = font.tile(ch)
                if t is not None:
                    grid[c, r] = t
    group.append(g)


def icon(group, state, x, y, palette, scale=1):
    if state in ICONS:
        g = displayio.Group(scale=scale, x=x, y=y)
        g.append(displayio.TileGrid(ICONS[state], pixel_shader=palette))
        group.append(g)


def box(group, x, y, w, h, colour=1):
    """A filled rectangle: black (1) by default, or white (0)."""
    group.append(vectorio.Rectangle(pixel_shader=PAPER, color_index=colour, width=w, height=h, x=x, y=y))


def draw_card(group, c, x, y, w, h):
    """One card (views.card) in the w x h box at (x, y); its right edge is the grid line, left out here.
    Black on white always; one waiting for you gets a bar along its top; subagents sit under a rule."""
    state, el, title, subs, footer, marked = c
    if marked:
        box(group, x, y, w - 1, 5)
    icon(group, state, x + 3, y + 8, PAPER, 2)
    text(group, [el], SMALL, x + 31, y + 8, 3, 1, PAPER, 2)
    text(group, list(title), LARGE, x + 1, y + 36, 8, len(title), PAPER)
    if subs:
        box(group, x + 1, y + 73, w - 4, 1)
        text(group, list(subs), SMALL, x + 1, y + 76, 12, len(subs), PAPER)
    text(group, [footer], SMALL, x + 1, y + h - 13, 12, 1, PAPER)


def show(group):
    display.root_group = group
    display.refresh()


def draw_grid(cards):
    group = displayio.Group()
    box(group, 0, 0, display.width, display.height, 0)
    w = display.width // 4
    for i, c in enumerate(cards):
        if c is not None:
            draw_card(group, c, i * w, 0, w, display.height)
    for col in range(1, 4):
        box(group, col * w - 1, 0, 1, display.height)
    show(group)


PAGE_HEAD = 2 * LARGE.ch + 3  # where the page's small text starts, under its two large lines and a rule


def page_size():
    """(head columns, body columns, body rows) of a session's page"""
    return (display.width // LARGE.cw, display.width // SMALL.cw,
            (display.height - PAGE_HEAD) // SMALL.ch)


def draw_page(state, head, body):
    hc, bc, br = page_size()
    group = displayio.Group()
    box(group, 0, 0, display.width, display.height, 0)
    text(group, head, LARGE, 0, 0, hc, 2, PAPER)
    icon(group, state, 2, 3, PAPER)
    box(group, 0, 2 * LARGE.ch + 1, display.width, 1)
    text(group, body, SMALL, 0, PAGE_HEAD, bc, br, PAPER)
    show(group)


def draw_text(lines, scale):
    group = displayio.Group()
    box(group, 0, 0, display.width, display.height, 0)
    text(group, lines, SMALL, 0, 0, display.width // (CW * scale), display.height // (CH * scale), PAPER,
         scale)
    show(group)


# ---- LEDs ----------------------------------------------------------------------------------------------

led_power = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
led_power.switch_to_output(value=False)  # low = LEDs powered on the MagTag
led_pin = digitalio.DigitalInOut(board.NEOPIXEL)
led_pin.direction = digitalio.Direction.OUTPUT
led_last = None


def leds(colours):
    """colours: (r, g, b) for each column, left to right."""
    global led_last
    if colours != led_last:
        buf = bytearray(12)
        for col, (r, g, b) in enumerate(colours):
            i = LED_ORDER[col] * 3
            buf[i], buf[i + 1], buf[i + 2] = g, r, b
        neopixel_write.neopixel_write(led_pin, buf)
        led_last = colours


# ---- main loop -----------------------------------------------------------------------------------------

keys = keypad.Keys((board.BUTTON_A, board.BUTTON_B, board.BUTTON_C, board.BUTTON_D),
                   value_when_pressed=False, pull=True)
quotes = load_quotes()
quote = random.choice(quotes)
quote_at = time.monotonic()
data, acked, slots = {}, set(), []
page, page_at, page_spec = None, 0, None  # the session on a page of its own, frozen
data_n, built, spec, shown = 0, None, None, None  # data_n counts snapshots, so screens are rebuilt only for new ones
last_draw, force = -REDRAW, True
frame = 0  # counts loop passes, for the dithering
last_fetch, last_ok = -POLL, time.monotonic()
url, fails, last_lookup = URL, 0, -LOOKUP_EVERY
reader, last_usb, source = usblink.LineReader(), None, ""
was_known, waiting_before = False, set()
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
            data_n += 1
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
                data_n += 1
                if source != "WiFi":
                    print("data from Wi-Fi:", url)
                last_ok, fails, source = now, 0, "WiFi"
            except Exception as e:  # keep showing the last data
                fails += 1
                print("fetch failed:", e)

    gen = data.get("generated", 0)
    by_key = dict((views.key(s), s) for s in data.get("sessions", []))
    slots = views.assign(slots, [views.key(s) for s in views.ranked(data, acked)], 4)

    event = keys.events.get()
    if event and event.pressed:  # opens the session above the button; on a page, any button goes back
        force = True
        if not by_key:
            quote, quote_at = random.choice(quotes), now
        else:
            page, page_at = (slots[event.key_number] if page is None else None), now
            if page in by_key:
                s = by_key[page]
                acked.add((s.get("id"), s.get("since")))  # seen: its bar and LED go
                head, body = views.page(s, gen, *page_size())
                page_spec = ("page", s.get("state"), tuple(head), tuple(body))  # frozen until it closes
    if page is not None and now - page_at > PAGE_FOR:
        page, force = None, True
    if not by_key and now - quote_at > QUOTE_EVERY:
        quote, quote_at = random.choice(quotes), now

    known = bool(url) or last_usb is not None
    if known and not was_known:  # replace the "looking for the watcher" screen at once
        force, was_known = True, True
    offline = known and now - last_ok > OFFLINE
    waiting = set(k for k in slots if k and views.needs_you(by_key[k], gen, acked))
    if waiting - waiting_before:  # something new waits for you: show it now
        force = True
    waiting_before = waiting
    glow = tuple(int(GLOW * f + 0.5) for f in WARM)
    if offline:
        leds([(0, 0, 0)] * 4)  # dark: no data from the watcher (the normal glow is red, so not red)
    elif waiting:
        # The columns that need you fade slowly between the red glow and orange and back, so the one that
        # waits always looks different from the steady red ones, whenever you glance at it.
        t = 0.5 - 0.5 * math.cos(2 * math.pi * now / PULSE_EVERY)  # 0 (glow) .. 1 (PULSE_TO)
        frame += 1
        d = DITHER[frame % len(DITHER)]
        pulse = tuple(int(a + (b - a) * t + d) for a, b in zip(glow, PULSE_TO))
        leds([pulse if slots[c] in waiting else glow for c in range(4)])
    else:
        leds([glow] * 4)

    nap = PULSE_NAP if waiting and not offline else IDLE_NAP
    due = force or now - last_draw >= REDRAW
    if not due:
        time.sleep(nap)
        continue
    key = (data_n, page, offline, known, quote, tuple(slots), len(acked))
    if key != built:
        built = key
        if not known:
            spec = ("text", ("Looking for the agents watcher...", "",
                             "Start extras/agents_watch.py on the Mac", "(Wi-Fi, found by name), or with --usb",
                             "and plug this board into it. Or set",
                             "AGENTS_URL http://<mac-ip>:8766/agents.json"), 1)
        elif page is not None:
            spec = page_spec
        elif by_key:
            spec = ("grid", tuple(views.card(by_key[k], gen, acked) if k else None for k in slots))
        else:
            cols, rows = display.width // (CW * 2), display.height // (CH * 2)
            spec = ("text", tuple(quote_lines(quote, cols, rows)), 2)
    if spec != shown and display.time_to_refresh == 0 and not display.busy:
        try:
            if spec[0] == "grid":
                draw_grid(spec[1])
            elif spec[0] == "page":
                draw_page(spec[1], list(spec[2]), list(spec[3]))
            else:
                draw_text(list(spec[1]), spec[2])
            shown, last_draw, force = spec, now, False
        except RuntimeError:  # refreshed too soon; try again next time round
            pass
    elif force and spec == shown:
        force = False
    time.sleep(nap)
