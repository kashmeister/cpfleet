# The weather on a MagTag: now, the next hours and the next three days, from Open-Meteo (no key needed).
# Fetches every WEATHER_EVERY minutes (default 15); button A fetches now. The screen redraws only when what
# it shows changes. If a fetch fails, the last weather stays up, marked "(old)", and it tries again in a
# minute.
# settings.toml (cpfleet settings <b> set ...):
#   WEATHER_LAT, WEATHER_LON   where, as strings ("42.36", "-71.06"); two decimals are plenty
#   WEATHER_UNITS              "metric" for °C and km/h; otherwise °F and mph
#   WEATHER_EVERY              minutes between fetches
#   CPFLEET_GLOW               the 4 LEDs glow a dim red, easy on the eyes at night, 0-255
#                              (default 25; 0 is off), as in apps/agents and apps/quotes
# Icons: bmps/weather_icons_48px.bmp and _20px.bmp, from Adafruit's MagTag weather sheets (Meteocons), which
# extras/weather_icons.py fetches next to this file; without them the screen does without.
# Built-in modules only (no lib/); weather.py is the screen, screen.py draws it.
import json
import os
import ssl
import time

import board
import digitalio
import keypad
import neopixel_write
import socketpool
import wifi

import screen
import weather

EVERY = 60 * int(os.getenv("WEATHER_EVERY") or 15)
RETRY = 60
METRIC = (os.getenv("WEATHER_UNITS") or "").lower() == "metric"
GLOW = int(os.getenv("CPFLEET_GLOW") or 25)  # 1 is the dimmest
WARM = (1.0, 0.0, 0.0)  # dim red glow, easy on the eyes at night: (25, 0, 0) at the default brightness; red can run brighter, the eye is less sensitive to it
display = board.DISPLAY
pool = socketpool.SocketPool(wifi.radio)
context = ssl.create_default_context()


def place():
    try:
        return float(os.getenv("WEATHER_LAT")), float(os.getenv("WEATHER_LON"))
    except (TypeError, ValueError):
        return None


def get_json(url):
    """GET an https URL (HTTP/1.0, so the reply isn't chunked) and parse its JSON body."""
    host, _, path = url.split("://", 1)[1].partition("/")
    sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
    sock.settimeout(15)
    sock = context.wrap_socket(sock, server_hostname=host)
    try:
        sock.connect((host, 443))
        sock.send(("GET /%s HTTP/1.0\r\nHost: %s\r\nUser-Agent: cpfleet-weather\r\n\r\n" % (path, host)).encode())
        data, chunk = bytearray(), bytearray(1024)
        while True:
            n = sock.recv_into(chunk)
            if not n:
                break
            data += chunk[:n]
    finally:
        sock.close()
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0]
    if b" 200 " not in status:
        raise OSError("weather service replied " + status.decode())
    return json.loads(body)


led_power = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
led_power.switch_to_output(value=GLOW == 0)  # low = LEDs powered on the MagTag
led_pin = digitalio.DigitalInOut(board.NEOPIXEL)
led_pin.direction = digitalio.Direction.OUTPUT
r, g, b = (int(GLOW * f + 0.5) for f in WARM)
neopixel_write.neopixel_write(led_pin, bytes([g, r, b] * 4))  # the LEDs take green, red, blue
keys = keypad.Keys((board.BUTTON_A,), value_when_pressed=False, pull=True)
if not (screen.sheet(weather.LARGE, "/bmps/weather_icons_48px.bmp", 48) and
        screen.sheet(weather.SMALL, "/bmps/weather_icons_20px.bmp", 20)):
    print("no icons in /bmps (extras/weather_icons.py fetches them)")
model, stale, shown = None, False, None
next_fetch = 0
where = place()

while True:
    now = time.monotonic()
    event = keys.events.get()
    if event and event.pressed:
        next_fetch = now
    if where and now >= next_fetch:
        try:
            model, stale = weather.parse(get_json(weather.url(where[0], where[1], METRIC)), METRIC), False
            next_fetch = now + EVERY
            print("weather for", model["time"])
        except Exception as e:  # keep showing the last weather
            print("fetch failed:", e)
            stale, next_fetch = True, now + RETRY
    if not where:
        page = (weather.message(["Weather: set where, then reset:", "",
                                 "cpfleet settings <b> set WEATHER_LAT 42.36",
                                 "cpfleet settings <b> set WEATHER_LON -71.06"]), [], [])
    elif model is None:
        page = (weather.message(["Weather: fetching...", "", "(no reply yet; trying every minute)"
                                 if stale else ""]), [], [])
    else:
        page = (weather.blocks(model, stale), weather.rules(), weather.icons(model))
    if page != shown and display.time_to_refresh == 0 and not display.busy:
        try:
            screen.show(display, *page)
            shown = page
        except RuntimeError:  # refreshed too soon; try again next time round
            pass
    time.sleep(0.1)
