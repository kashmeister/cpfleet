# Draws a screen of text blocks on the e-ink display: [(x, y, size, text)] in pixels, with optional rules
# (filled rectangles) and icons from sprite sheets (BMP files read from flash, so they cost no RAM). Shared by the screen modules (weather.py; the agents views and quotes can move to it),
# so an app can cycle through several. Built-in modules only.
#
# size is (num, den): (1, 1) terminalio's 6x12 font, (3, 2) half as large again (9x18), (2, 1) and (4, 1)
# twice and four times. Fractional sizes are a copy of the font with each pixel taken from the nearest one
# of the original; whole ones scale the original. The font lacks a few characters, which are drawn here:
#   "\x01"  a check mark      "\x02"  a degree sign
import displayio
import terminalio

FONT = terminalio.FONT
CW, CH = FONT.get_bounding_box()[:2]
# Extra characters, in the font's 6x12 cell as its capitals are drawn (rows 2-8, the last column blank)
EXTRA = {
    "\x01": (2, ("    #",
                 "    #",
                 "   ##",
                 "   # ",
                 "# ## ",
                 "###  ",
                 " #   ")),
    "\x02": (2, (" ##  ",
                 "#  # ",
                 "#  # ",
                 " ##  ")),
}


class Font:
    """The printable ASCII characters and EXTRA, num/den times the font's size, as one tile sheet."""

    def __init__(self, num=1, den=1):
        self.cw, self.ch = CW * num // den, CH * num // den
        xs = [x * den // num for x in range(self.cw)]  # the original column and row of each new one
        ys = [y * den // num for y in range(self.ch)]
        chars = [chr(c) for c in range(32, 127)] + sorted(EXTRA)
        src, per_row = FONT.bitmap, FONT.bitmap.width // CW
        self.bitmap = displayio.Bitmap(self.cw * len(chars), self.ch, 2)
        self.tiles = {}
        for i, ch in enumerate(chars):
            bx = i * self.cw
            if ch in EXTRA:
                top, shape = EXTRA[ch]
                for y in range(self.ch):
                    row = ys[y] - top
                    if 0 <= row < len(shape):
                        for x in range(self.cw):
                            if xs[x] < len(shape[row]) and shape[row][xs[x]] == "#":
                                self.bitmap[bx + x, y] = 1
            else:
                glyph = FONT.get_glyph(ord(ch))
                if glyph is None:
                    continue
                ox, oy = glyph.tile_index % per_row * CW, glyph.tile_index // per_row * CH
                for y in range(self.ch):
                    sy = oy + ys[y]
                    for x in range(self.cw):
                        if src[ox + xs[x], sy]:
                            self.bitmap[bx + x, y] = 1
            self.tiles[ch] = i
        self.blank = self.tiles[" "]


_fonts = {}


def font(num, den):
    """The tile sheet for a size, made once (a fractional one takes about a second)."""
    key = (num, den) if den > 1 else (1, 1)
    if key not in _fonts:
        _fonts[key] = Font(*key)
    return _fonts[key], (1 if den > 1 else num)


_sheets = {}


def sheet(name, path, size):
    """Make an icon sheet available as `name`: a BMP of size x size pixel icons, numbered left to right and
    top to bottom. False if the file isn't there."""
    try:
        bmp = displayio.OnDiskBitmap(path)
    except OSError:
        return False
    _sheets[name] = (bmp, size)
    return True


def group(blocks, rules=(), width=296, height=128, icons=()):
    """A displayio Group showing blocks [(x, y, (num, den), text)], rules [(x, y, width, height)] and icons
    [(x, y, sheet name, index)], black on white. Icons from a sheet that isn't loaded are left out."""
    palette = displayio.Palette(2)
    palette[0], palette[1] = 0xFFFFFF, 0x000000
    root = displayio.Group()
    # A white page under everything: displayio fills whatever no layer covers with black. (A one-pixel
    # bitmap in a scaled-up Group doesn't cover the screen; a full-size one does, for 5 KB.)
    root.append(displayio.TileGrid(displayio.Bitmap(width, height, 2), pixel_shader=palette))
    for x, y, size, text in blocks:
        if not text:
            continue
        f, scale = font(*size)
        grid = displayio.TileGrid(f.bitmap, pixel_shader=palette, width=len(text), height=1,
                                  tile_width=f.cw, tile_height=f.ch, default_tile=f.blank)
        for c, ch in enumerate(text):
            t = f.tiles.get(ch)
            if t is not None:
                grid[c, 0] = t
        g = displayio.Group(scale=scale, x=x, y=y)
        g.append(grid)
        root.append(g)
    for x, y, w, h in rules:
        bmp = displayio.Bitmap(w, h, 2)
        bmp.fill(1)
        root.append(displayio.TileGrid(bmp, pixel_shader=palette, x=x, y=y))
    for x, y, name, index in icons:
        if name in _sheets:
            bmp, size = _sheets[name]
            icon = displayio.TileGrid(bmp, pixel_shader=bmp.pixel_shader, tile_width=size, tile_height=size, x=x, y=y)
            icon[0] = index
            root.append(icon)
    return root


def show(display, blocks, rules=(), icons=()):
    """Draw on the display at once. The caller waits for display.time_to_refresh (e-ink: one refresh
    every few seconds) or catches the RuntimeError."""
    display.root_group = group(blocks, rules, display.width, display.height, icons)
    display.refresh()
