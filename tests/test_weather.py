"""The weather screen (apps/weather/weather.py): parsing Open-Meteo's reply and laying it out."""
import importlib.util
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("weather", HERE.parent / "apps" / "weather" / "weather.py")
weather = importlib.util.module_from_spec(spec)
spec.loader.exec_module(weather)

# The shape of Open-Meteo's reply to weather.url(), with made-up values
REPLY = {
    "current": {"time": "2026-01-02T20:00", "interval": 900, "temperature_2m": 47.2,
                "apparent_temperature": 42.6, "relative_humidity_2m": 85, "weather_code": 2,
                "wind_speed_10m": 4.6},
    "hourly": {"time": ["2026-01-02T%02d:00" % h for h in range(20, 24)] +
                       ["2026-01-03T%02d:00" % h for h in range(0, 8)],
               "temperature_2m": [47.2, 45.5, 44.0, 43.1, 42.5, 41.9, 41.0, 40.2, 39.9, 39.0, 38.1, 37.7],
               "weather_code": [2, 2, 3, 3, 61, 61, 63, 63, 3, 2, 1, 0]},
    "daily": {"time": ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
              "weather_code": [2, 82, 75, 99], "temperature_2m_max": [52.4, 100.2, 18.0, 61.0],
              "temperature_2m_min": [38.1, 71.0, -12.4, 44.0],
              "precipitation_probability_max": [20, 85, 90, 40]},
}
WIDTH, HEIGHT = 296, 128


class Weather(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(weather.weekday("2026-01-02"), "Fri")
        self.assertEqual(weather.weekday("2024-02-29"), "Thu")
        self.assertEqual(weather.weekday("2026-03-01"), "Sun")
        self.assertEqual(weather.clock("2026-01-02T20:05"), "8:05pm")
        self.assertEqual(weather.clock("2026-01-02T00:30"), "12:30am")
        self.assertEqual(weather.hour("2026-01-02T12:00"), "12pm")
        self.assertEqual(weather.condition(2), "Partly cloudy")
        self.assertEqual(weather.condition(2, short=True), "p.cloud")
        self.assertEqual(weather.condition(1234), "?")
        for words in weather.CODES.values():
            self.assertLessEqual(len(words[0]), 14, words)
            self.assertLessEqual(len(words[1]), 7, words)

    def test_parse(self):
        w = weather.parse(REPLY)
        self.assertEqual((w["temp"], w["feels"], w["rh"], w["wind"], w["hi"], w["lo"], w["pop"]),
                         (47, 43, 85, 5, 52, 38, 20))
        self.assertEqual(w["hours"], [("11pm", 43, 3), ("2am", 41, 63), ("5am", 39, 2)])
        self.assertEqual(w["days"], [("Sat", 100, 71, 82), ("Sun", 18, -12, 75), ("Mon", 61, 44, 99)])

    def test_layout_fits_and_reads(self):
        w = weather.parse(REPLY)
        bl = weather.blocks(w)
        texts = [b[3] for b in bl]
        self.assertEqual(texts, ["47", weather.DEG, "feels 43  RH 85%", "Partly cloudy", "52/38 rain 20%",
                                 "wind 5mph  at 8:00pm", "11pm  43", " 2am  41", " 5am  39",
                                 "Sat 100/71", "Sun 18/-12", "Mon 61/44"])
        self.assertIn("wind 5mph  at 8:00pm (old)", [b[3] for b in weather.blocks(w, stale=True)])
        self.assertEqual(bl[0][2], (6, 1))
        self.assertEqual(weather.blocks(dict(w, temp=100))[0][2], (5, 1))  # three digits: a size smaller
        self.assertIn("feels 43", [b[3] for b in weather.blocks(dict(w, rh=None))])
        boxes = []
        for x, y, (num, den), text in bl + weather.blocks(dict(w, temp=-10), stale=True):
            cw, ch = 6 * num // den, (9 if num >= 5 else 12) * num // den  # digits end at the cell's row 8
            boxes.append((x, y, x + len(text) * cw, y + ch, text))
        sizes = {weather.LARGE: 48, weather.SMALL: 20}
        for x, y, sheet, index in weather.icons(w):
            self.assertLess(index, 9)
            boxes.append((x, y, x + sizes[sheet], y + sizes[sheet], "icon %d" % index))
        for x0, y0, x1, y1, what in boxes:
            self.assertLessEqual(y1, HEIGHT, what)
            self.assertLessEqual(x1, weather.RULE_X if x0 < weather.RULE_X else WIDTH, what)
        for a in boxes[:len(bl)] + boxes[-len(weather.icons(w)):]:  # nothing on this screen overlaps
            for b in boxes[:len(bl)] + boxes[-len(weather.icons(w)):]:
                if a is not b:
                    self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3], (a, b))
        for x, y, w_, h in weather.rules():
            self.assertLessEqual(x + w_, WIDTH)
            self.assertLessEqual(y + h, HEIGHT)

    def test_icons(self):
        w = weather.parse(REPLY)
        self.assertEqual([i[3] for i in weather.icons(w)], [1, 3, 4, 1, 5, 7, 6])  # now, 3 hours, 3 days
        self.assertEqual(weather.icons(dict(w, code=1234, hours=[], days=[])), [])  # an unknown code: none
        for code in weather.CODES:
            self.assertIn(code, weather.ICONS)
        rows = [i[1] for i in weather.icons(dict(w, hours=[("x", 0, 3)] * 3, days=[("y", 0, 0, 95)] * 3))[1:]]
        self.assertEqual(rows, [0, 20, 41, 67, 88, 108])  # moved by SMALL_SHIFT, kept on the screen

    def test_no_string_methods_circuitpython_lacks(self):
        for name in ("weather.py", "screen.py", "code.py"):
            src = (HERE.parent / "apps" / "weather" / name).read_text()
            self.assertIsNone(re.search(r"\.(ljust|rjust|center)\(", src), name)


if __name__ == "__main__":
    unittest.main()
