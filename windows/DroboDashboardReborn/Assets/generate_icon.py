"""Generate the Drobo Dashboard REBORN application icon.

Produces two multi-resolution .ico files next to this script:

    AppIcon.ico       -- the normal app / taskbar / tray icon
    AppIconAlert.ico  -- the same mark with a small red badge added in the
                         corner, swapped in by the tray icon while a critical
                         alert is active (see TrayIconManager.cs)

Design -- a text lockup, deliberately NOT a copy of Drobo's own logo (this
project is unaffiliated; see the repo README). The letterforms below are a
plain geometric 5x7 block font written for this file, not traced from any
existing typeface:

    - "drobo", lowercase, in front, in near-white.
    - "REBORN" behind it, larger and in red, so it reads as a stamp the
      wordmark is sitting on top of.
    - A thin dark halo around the front word, so "drobo" stays readable
      exactly where it crosses the red rather than dissolving into it.
    - A dark rounded plate behind everything, so the mark holds up against a
      light title bar or wallpaper instead of disappearing.

Sized smallest-first, because 16x16 is the taskbar and tray size and that is
the one that decides whether an icon works. Six letters across sixteen pixels
is under three pixels per letter -- unavoidably grey mush -- so below 48px the
lockup is dropped entirely in favour of a single bold lowercase "d" on a red
plate. That preserves the two things which actually identify an icon at a
glance: its colour and its silhouette.

Pure Python standard library only (zlib for PNG compression/CRC32, struct for
binary packing) -- no Pillow, no third-party packages, so this reproduces
identically on any machine with plain Python 3 installed. Re-run this script
any time the design changes:

    python windows/DroboDashboardReborn/Assets/generate_icon.py"""
from __future__ import annotations

import os
import struct
import zlib

SIZES = [16, 32, 48, 64, 128, 256]
SUPERSAMPLE = 4  # subsamples per axis per output pixel, for anti-aliased edges

# ---- palette -----------------------------------------------------------
ENCLOSURE = (46, 52, 61, 255)        # charcoal body
ENCLOSURE_TOP = (64, 71, 82, 255)    # lighter top bezel strip
BAY_SLOT = (21, 24, 29, 255)         # recessed bay slot, darker than the body
BAY_LED = (70, 192, 122, 255)        # small "healthy" activity dot (large sizes only)
REBORN_RED = (211, 58, 46, 255)      # the REBORN stamp
WORDMARK = (238, 242, 247, 255)      # the "drobo" lettering in front
PLATE = (26, 30, 37, 255)            # dark plate the lockup sits on
ALERT_RED = (226, 43, 36, 255)       # tray badge when a critical alert is active
ALERT_RING = (255, 255, 255, 230)    # thin light ring around the badge for contrast
TRANSPARENT = (0, 0, 0, 0)

# ---- a tiny, original 5x7 block font, only the letters REBORN needs ----
# ('1' = ink, '0' = empty; my own plain geometric letterforms, not traced
# from any existing typeface.)
FONT_5X7 = {
    # Uppercase -- the REBORN stamp behind.
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "N": ["10001", "11001", "10101", "10101", "10011", "10001", "10001"],
    # Lowercase -- the "drobo" wordmark in front. Drawn with a full 7-row box
    # so ascenders (d, b) and x-height letters (r, o) share one baseline.
    "d": ["00001", "00001", "01111", "10001", "10001", "10001", "01111"],
    "r": ["00000", "00000", "10110", "11001", "10000", "10000", "10000"],
    "o": ["00000", "00000", "01110", "10001", "10001", "10001", "01110"],
    "b": ["10000", "10000", "11110", "10001", "10001", "10001", "11110"],
}

BEHIND_WORD = "REBORN"   # large, red, behind
FRONT_WORD = "drobo"     # lowercase, light, in front


def word_predicate(word: str, x0: float, y0: float, cell: float):
    """
    A predicate covering `word` rendered from FONT_5X7 at `cell` px per dot,
    with its top-left at (x0, y0). One gap column between letters.
    """
    span = 5 * cell

    def hit(x: float, y: float) -> bool:
        rel_y = y - y0
        if rel_y < 0 or rel_y >= 7 * cell:
            return False
        row = int(rel_y / cell)
        if not (0 <= row < 7):
            return False
        cursor = 0.0
        rel_x = x - x0
        for ch in word:
            if cursor <= rel_x < cursor + span:
                col = int((rel_x - cursor) / cell)
                if 0 <= col < 5:
                    return FONT_5X7[ch][row][col] == "1"
                return False
            cursor += span + cell
        return False

    return hit


def word_width(word: str, cell: float) -> float:
    """Width of `word` at `cell` px per dot, including inter-letter gaps."""
    return len(word) * 5 * cell + (len(word) - 1) * cell


def in_rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    if r <= 0:
        return True
    if x < x0 + r and y < y0 + r:
        return (x - (x0 + r)) ** 2 + (y - (y0 + r)) ** 2 <= r * r
    if x > x1 - r and y < y0 + r:
        return (x - (x1 - r)) ** 2 + (y - (y0 + r)) ** 2 <= r * r
    if x < x0 + r and y > y1 - r:
        return (x - (x0 + r)) ** 2 + (y - (y1 - r)) ** 2 <= r * r
    if x > x1 - r and y > y1 - r:
        return (x - (x1 - r)) ** 2 + (y - (y1 - r)) ** 2 <= r * r
    return True


def in_circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def build_shapes(size: float, with_badge: bool):
    """
    Front-to-back list of (predicate, rgba). `sample` returns the FIRST match,
    so earlier entries are drawn in front.

    The mark is a text lockup: the word "drobo" in lowercase, sitting in front
    of a larger "REBORN" in red. Lowercase is deliberate -- it is what the
    owner asked for, and at icon sizes a lowercase word with ascenders (d, b)
    has a more distinctive silhouette than an all-caps block.

    Below 48px none of that survives. Six letters across sixteen pixels is
    under three pixels per letter, which renders as grey mush -- so small sizes
    get a single bold "d" on a red plate instead. That keeps the two things
    that actually identify an icon in a taskbar: its colour and its shape.
    """
    shapes = []
    s = size
    lockup = s >= 48

    if not lockup:
        # --- small: one letter, high contrast --------------------------------
        pad = 0.08 * s
        plate = (lambda x, y, x0=pad, y0=pad, x1=s - pad, y1=s - pad, r=0.22 * s:
                 in_rounded_rect(x, y, x0, y0, x1, y1, r))

        # A "d" filling most of the plate. cell is chosen so the glyph's 5x7
        # box fits with a margin; the letter is what carries recognition here.
        cell = (s * 0.52) / 7.0
        gw = word_width("d", cell)
        gx = (s - gw) / 2.0
        gy = (s - 7 * cell) / 2.0
        shapes.append((word_predicate("d", gx, gy, cell), WORDMARK))
        shapes.append((plate, REBORN_RED))
        return _with_badge(shapes, s, with_badge)

    # --- large: the full lockup ---------------------------------------------
    # REBORN sits BEHIND, set wide across the icon. "drobo" sits in front of
    # it, overlapping -- not stacked underneath, which is what a first pass at
    # this produced and which just read as two separate lines of text.
    #
    # Both are positioned from their vertical CENTRE rather than their top
    # edge, because that is what makes the overlap controllable: give them
    # centres a little apart and they interleave, give them the same centre
    # and "drobo" sits squarely across REBORN's waist.
    def from_centre(cy: float, cell: float) -> float:
        return cy - 3.5 * cell

    # REBORN: nearly the full width, so it is unmistakably the larger of the two.
    behind_cell = (s * 0.90) / word_width(BEHIND_WORD, 1.0)
    behind_w = word_width(BEHIND_WORD, behind_cell)
    behind_x = (s - behind_w) / 2.0
    behind_y = from_centre(s * 0.50, behind_cell)

    # drobo: narrower and set on a centre slightly below REBORN's, so it
    # crosses the lower half of the red letters. Sitting dead-centre hides
    # REBORN's midsection and makes it hard to read; a little low leaves the
    # tops of the red letters clear.
    front_cell = (s * 0.66) / word_width(FRONT_WORD, 1.0)
    front_w = word_width(FRONT_WORD, front_cell)
    front_x = (s - front_w) / 2.0
    front_y = from_centre(s * 0.60, front_cell)

    # A soft dark plate behind everything, so the mark reads on any wallpaper
    # or title bar rather than dissolving into it.
    pad = 0.05 * s
    plate = (lambda x, y, x0=pad, y0=pad, x1=s - pad, y1=s - pad, r=0.20 * s:
             in_rounded_rect(x, y, x0, y0, x1, y1, r))

    # A thin dark outline around the front word, so "drobo" stays legible
    # exactly where it crosses the red. Cheap halo: the same glyph, grown.
    halo = front_cell * 0.55
    shapes.append((
        lambda x, y, p=word_predicate(FRONT_WORD, front_x, front_y, front_cell), h=halo:
            p(x, y) or p(x - h, y) or p(x + h, y) or p(x, y - h) or p(x, y + h),
        None,  # placeholder, replaced below
    ))
    shapes[-1] = (shapes[-1][0], ENCLOSURE)

    # ...but the letter itself must win over its own halo, so insert it first.
    shapes.insert(0, (word_predicate(FRONT_WORD, front_x, front_y, front_cell), WORDMARK))

    shapes.append((word_predicate(BEHIND_WORD, behind_x, behind_y, behind_cell), REBORN_RED))
    shapes.append((plate, PLATE))
    return _with_badge(shapes, s, with_badge)


def _with_badge(shapes, s: float, with_badge: bool):
    """The critical-alert dot, sized so it signals at 16px without dominating at 256."""
    if not with_badge:
        return shapes
    frac = 0.20 if s <= 32 else 0.15 if s <= 64 else 0.115
    r = frac * s
    off = 0.80 if s <= 32 else 0.835
    cx = cy = off * s
    ring = r + (0.045 if s <= 32 else 0.03) * s
    shapes.insert(0, (lambda x, y, cx=cx, cy=cy, r=r: in_circle(x, y, cx, cy, r), ALERT_RED))
    shapes.insert(1, (lambda x, y, cx=cx, cy=cy, r=ring: in_circle(x, y, cx, cy, r), ALERT_RING))
    return shapes


def sample(shapes, x: float, y: float):
    for predicate, rgba in shapes:
        if predicate(x, y):
            return rgba
    return TRANSPARENT


def render(size: int, with_badge: bool):
    shapes = build_shapes(float(size), with_badge)
    ss = SUPERSAMPLE
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            rs = gs = bs = asum = 0
            for j in range(ss):
                y = py + (j + 0.5) / ss
                for i in range(ss):
                    x = px + (i + 0.5) / ss
                    r, g, b, a = sample(shapes, x, y)
                    rs += r * a
                    gs += g * a
                    bs += b * a
                    asum += a
            n = ss * ss
            if asum == 0:
                row.append((0, 0, 0, 0))
            else:
                row.append((rs // asum, gs // asum, bs // asum, asum // n))
        rows.append(row)
    return rows


# ---- minimal PNG encoder (stdlib zlib only) -----------------------------

def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(pixels) -> bytes:
    height = len(pixels)
    width = len(pixels[0]) if height else 0
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type: None
        for (r, g, b, a) in row:
            raw += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), 9)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


# ---- minimal ICO container (PNG-compressed frames, one per size) --------

def write_ico(path: str, size_png_pairs) -> None:
    count = len(size_png_pairs)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    data = bytearray()
    offset = 6 + 16 * count
    for size, png in size_png_pairs:
        wh = size if size < 256 else 0  # 0 means 256 per the ICO spec
        entry = struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(png), offset)
        entries += entry
        data += png
        offset += len(png)
    with open(path, "wb") as f:
        f.write(header + bytes(entries) + bytes(data))


def build(with_badge: bool):
    pairs = []
    for size in SIZES:
        pixels = render(size, with_badge)
        pairs.append((size, encode_png(pixels)))
    return pairs


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))

    normal = build(with_badge=False)
    write_ico(os.path.join(out_dir, "AppIcon.ico"), normal)
    print("Wrote AppIcon.ico (%d frames: %s)" % (len(normal), ", ".join(str(s) for s, _ in normal)))

    alert = build(with_badge=True)
    write_ico(os.path.join(out_dir, "AppIconAlert.ico"), alert)
    print("Wrote AppIconAlert.ico (%d frames: %s)" % (len(alert), ", ".join(str(s) for s, _ in alert)))


if __name__ == "__main__":
    main()
