#!/usr/bin/env python3
"""Fetch the icon sheets for apps/weather from Adafruit's MagTag weather guide.

The icons are Meteocons, as Adafruit ship them in the guide's Project Bundle
(https://learn.adafruit.com/magtag-weather). They aren't in this repo, which doesn't redistribute them; this
copies weather_icons_20px.bmp from the bundle into apps/weather/bmps (git-ignored), and makes
weather_icons_48px.bmp there from its 70 pixel sheet (the board can't scale a BMP as it draws it), so
`cpfleet push apps/weather` takes them to the board.

  python3 extras/weather_icons.py [--dest apps/weather/bmps]
"""
import argparse
import io
import re
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

GUIDE = "https://learn.adafruit.com/magtag-weather/project-code-2"
LARGE, SMALL = "weather_icons_70px.bmp", "weather_icons_20px.bmp"
AGENT = {"User-Agent": "cpfleet (https://github.com/kashmeister/cpfleet)"}  # Learn refuses Python's own


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=60) as r:
        return r.read()


def read_bmp(data):
    """(width, height, palette [(r, g, b)], pixels [[index]] top row first) of an uncompressed 4-bit BMP."""
    off, = struct.unpack_from("<I", data, 10)
    head, w, h, _, bpp, comp = struct.unpack_from("<IiiHHI", data, 14)
    if bpp != 4 or comp:
        raise ValueError("expected an uncompressed 4-bit BMP")
    ncol = struct.unpack_from("<I", data, 46)[0] or 16
    pal = [tuple(data[14 + head + 4 * i + 2 - k] for k in range(3)) for i in range(ncol)]
    stride = (w * 4 + 31) // 32 * 4
    rows = []
    for y in range(abs(h)):
        start = off + (abs(h) - 1 - y if h > 0 else y) * stride  # positive height: stored bottom-up
        rows.append([(data[start + x // 2] >> (0 if x % 2 else 4)) & 15 for x in range(w)])
    return w, abs(h), pal, rows


def write_bmp(w, h, pal, rows):
    stride = (w * 4 + 31) // 32 * 4
    body = bytearray()
    for row in reversed(rows):
        line = bytearray(stride)
        for x, v in enumerate(row):
            line[x // 2] |= v << (0 if x % 2 else 4)
        body += line
    palette = b"".join(bytes((b, g, r, 0)) for r, g, b in pal)
    off = 14 + 40 + len(palette)
    return (b"BM" + struct.pack("<IHHI", off + len(body), 0, 0, off) +
            struct.pack("<IiiHHIIiiII", 40, w, h, 1, 4, 0, len(body), 2835, 2835, len(pal), len(pal)) +
            palette + body)


def shrink_sheet(data, old, new, n=3):
    """An n x n sheet of old-pixel icons as one of new-pixel icons: each new pixel the average grey of what
    it covers (sampled 4 x 4), as the nearest of the sheet's own greys."""
    w, h, pal, rows = read_bmp(data)
    grey = [sum(c) / 3 for c in pal]
    out = []
    for y in range(new * n):
        ty, py = divmod(y, new)
        line = []
        for x in range(new * n):
            tx, px = divmod(x, new)
            total = 0.0
            for sy in range(4):
                for sx in range(4):
                    ox = tx * old + int((px + (sx + 0.5) / 4) * old / new)
                    oy = ty * old + int((py + (sy + 0.5) / 4) * old / new)
                    total += grey[rows[oy][ox]]
            v = total / 16
            line.append(min(range(len(pal)), key=lambda i: abs(grey[i] - v)))
        out.append(line)
    return write_bmp(new * n, new * n, pal, out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default=str(Path(__file__).resolve().parent.parent / "apps" / "weather" / "bmps"))
    a = ap.parse_args()
    link = re.search(rb'https://learn\.adafruit\.com/elements/\d+/download\?type=zip', get(GUIDE))
    if not link:
        sys.exit("no Project Bundle link on %s" % GUIDE)
    with zipfile.ZipFile(io.BytesIO(get(link.group().decode()))) as zf:
        sheets = {}
        for f in (LARGE, SMALL):
            # Each CircuitPython version's folder has the same sheets; take the newest folder's
            found = sorted((n for n in zf.namelist() if "/openmeteo/" in n and n.endswith("/" + f)),
                           key=lambda n: [int(x) for x in re.findall(r"CircuitPython (\d+)", n)] or [0])
            if not found:
                sys.exit("%s isn't in the bundle any more" % f)
            sheets[f] = zf.read(found[-1])
    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in ((SMALL, sheets[SMALL]),
                       ("weather_icons_48px.bmp", shrink_sheet(sheets[LARGE], 70, 48))):
        (dest / name).write_bytes(data)
        print("%s  %d bytes" % (dest / name, len(data)))

if __name__ == "__main__":
    main()
