# The weather screen: the current conditions on the left, the next hours and days on the right, from
# Open-Meteo (no account or key). Plain Python, so the host's unit tests can import it too.
#
# A screen module has parts that an app cycling through several screens can share:
#   url(...) / parse(reply) -> a small model    what to fetch, and what to keep of the reply
#   blocks(model, ...)      -> [(x, y, size, text)]   the text; screen.py draws it
#   icons(model)            -> [(x, y, sheet, index)]   pictures from icon sheets (screen.py)
#   rules()                 -> [(x, y, width, height)]   lines
# size is (num, den) times the built-in font: S, M (one and a half times), L (twice), XL (four times), and
# the temperature's own sizes.
#
#   [icon]  4 7 °      | 11pm  43  [i]
#   [48px]  4 7        |  2am  40  [i]
#   feels 42  RH 85%   |  5am  38  [i]
#                      |----------------
#   Partly cloudy      | Sat 57/38 [i]
#   57/42 rain 20%     | Sun 66/46 [i]
#   wind 5mph  at 8:00pm  Mon 59/54 [i]
# The icons are Adafruit's MagTag weather sheets (Meteocons): extras/weather_icons.py fetches them into
# bmps/. Without them the left still names the conditions; the right shows only temperatures.
S, M, L, XL = (1, 1), (3, 2), (2, 1), (4, 1)
LARGE, SMALL = "large", "small"  # icon sheets: 48 and 20 pixel icons, 3 x 3 each
# Icon index in the sheets (sun, sun and cloud, cloud, clouds, rain, showers, storm, snow, fog) by WMO code
ICONS = {0: 0, 1: 1, 2: 1, 3: 3, 45: 8, 48: 8, 51: 5, 53: 5, 55: 5, 56: 7, 57: 7, 61: 4, 63: 4, 65: 4,
         66: 7, 67: 7, 71: 7, 73: 7, 75: 7, 77: 7, 80: 5, 81: 5, 82: 5, 85: 7, 86: 7, 95: 6, 96: 6, 99: 6}
# The small icons' drawings don't sit at the same height in their 20 pixel squares (the clouds are low, the
# storm high): moving these down (+) or up (-) centres each, so the rows look evenly spaced.
SMALL_SHIFT = {3: -2, 6: 1, 8: 1}
DEG = "\x02"  # the font has no degree sign: screen.py draws one where this character is
BASE = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes: (words for the current conditions, at most 14 characters; one word, at most 7)
CODES = {
    0: ("Clear", "clear"), 1: ("Mostly clear", "clear"), 2: ("Partly cloudy", "p.cloud"), 3: ("Cloudy", "cloudy"),
    45: ("Fog", "fog"), 48: ("Icy fog", "fog"),
    51: ("Light drizzle", "drizzle"), 53: ("Drizzle", "drizzle"), 55: ("Heavy drizzle", "drizzle"),
    56: ("Icy drizzle", "ice"), 57: ("Icy drizzle", "ice"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "ice"), 67: ("Freezing rain", "ice"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Showers", "showers"), 81: ("Showers", "showers"), 82: ("Heavy showers", "showers"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Storm, hail", "storm"), 99: ("Storm, hail", "storm"),
}
DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def url(lat, lon, metric=False):
    units = "" if metric else "&temperature_unit=fahrenheit&wind_speed_unit=mph"
    return (BASE + "?latitude=%.2f&longitude=%.2f" % (lat, lon) +
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
            "&hourly=temperature_2m,weather_code"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            "&timezone=auto&forecast_days=4&forecast_hours=12" + units)


def condition(code, short=False):
    words = CODES.get(code)
    if words is None:
        return "?"
    return words[1] if short else words[0]


def weekday(date):
    """"2026-10-30" -> "Fri" (Sakamoto's method; the board has no calendar functions to spare)"""
    y, m, d = int(date[0:4]), int(date[5:7]), int(date[8:10])
    if m < 3:
        y -= 1
    return DAYS[(y + y // 4 - y // 100 + y // 400 + (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)[m - 1] + d) % 7]


def clock(stamp):
    """"2026-10-30T20:00" -> "8:00pm" """
    h, mi = int(stamp[11:13]), stamp[14:16]
    return "%d:%s%s" % (h % 12 or 12, mi, "am" if h < 12 else "pm")


def hour(stamp):
    """"2026-10-30T20:00" -> "8pm" """
    h = int(stamp[11:13])
    return "%d%s" % (h % 12 or 12, "am" if h < 12 else "pm")


def parse(d, metric=False):
    """The model blocks() draws, from Open-Meteo's reply."""
    cur, hourly, daily = d["current"], d["hourly"], d["daily"]
    hours = [(hour(hourly["time"][i]), round(hourly["temperature_2m"][i]), hourly["weather_code"][i])
             for i in (3, 6, 9) if i < len(hourly["time"])]
    days = [(weekday(daily["time"][i]), round(daily["temperature_2m_max"][i]),
             round(daily["temperature_2m_min"][i]), daily["weather_code"][i])
            for i in range(1, min(4, len(daily["time"])))]
    pop = daily.get("precipitation_probability_max") or [None]
    return {"time": cur["time"], "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]), "rh": cur.get("relative_humidity_2m"),
            "code": cur["weather_code"],
            "wind": round(cur["wind_speed_10m"]), "wind_unit": "km/h" if metric else "mph",
            "hi": round(daily["temperature_2m_max"][0]), "lo": round(daily["temperature_2m_min"][0]),
            "pop": pop[0], "hours": hours, "days": days}


def message(lines):
    """A screen of plain lines, for when there's no weather to show."""
    return [(4, 4 + 16 * i, S, line) for i, line in enumerate(lines)]



RULE_X, RULE_Y, RIGHT_X, ICON_X = 172, 64, 178, 272  # the dividers, the right-hand part, its icons
ROW = 21  # pixels between the right-hand rows: 20 pixel icons, a pixel apart
HOURS_Y, DAYS_Y = 1, 66  # the first hour's and first day's icon


def rules():
    """The lines between the parts, as rectangles [(x, y, width, height)]."""
    return [(RULE_X, 4, 1, 120), (RIGHT_X, RULE_Y, 296 - RIGHT_X, 1)]


def temperature(t, x=52):
    """The current temperature as large as fits beside the icon: two characters six times the font's
    size, three (100, -12) five times, with a smaller degree sign at the digits' top right."""
    digits = "%d" % t
    scale = 6 if len(digits) <= 2 else 5
    top = -6  # the font's digits start two rows into their cell: this puts them level with the icon
    return [(x, top, (scale, 1), digits),
            (x + len(digits) * 6 * scale, top + 2 * scale - 6, (3, 1), DEG)]  # the sign's own top row is 2


def blocks(w, stale=False):
    """The text for model w on the 296 x 128 MagTag: [(x, y, size, text)]."""
    out = temperature(w["temp"])
    feels = "feels %d" % w["feels"]
    if w.get("rh") is not None:
        feels += "  RH %d%%" % w["rh"]
    out.append((2, 56, M, feels))  # under the icon and the digits (whose cells' last rows are empty)
    out.append((2, 80, M, condition(w["code"])[:14]))
    today = "%d/%d" % (w["hi"], w["lo"])
    if w.get("pop") is not None and w["pop"] >= 10:
        today += " rain %d%%" % w["pop"]
    out.append((2, 98, M, today[:14]))
    out.append((2, 116, S, ("wind %d%s  at %s%s" % (w["wind"], w["wind_unit"], clock(w["time"]),
                                                    " (old)" if stale else ""))[:28]))
    for i, (label, temp, code) in enumerate(w["hours"]):
        out.append((RIGHT_X, HOURS_Y + 1 + ROW * i, M, "%4s %3d" % (label, temp)))
    for i, (name, hi, lo, code) in enumerate(w["days"]):
        out.append((RIGHT_X, DAYS_Y + 1 + ROW * i, M, ("%s %d/%d" % (name, hi, lo))[:10]))
    return out


def small_icon(y, code):
    index = ICONS[code]
    return (ICON_X, min(108, max(0, y + SMALL_SHIFT.get(index, 0))), SMALL, index)


def icons(w):
    """The pictures for model w: [(x, y, sheet, index)], the large one for now and a small one per row."""
    out = [(0, 4, LARGE, ICONS[w["code"]])] if w["code"] in ICONS else []
    out += [small_icon(HOURS_Y + ROW * i, h[2]) for i, h in enumerate(w["hours"]) if h[2] in ICONS]
    out += [small_icon(DAYS_Y + ROW * i, d[3]) for i, d in enumerate(w["days"]) if d[3] in ICONS]
    return out
