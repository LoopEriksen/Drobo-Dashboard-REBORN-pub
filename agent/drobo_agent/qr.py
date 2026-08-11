"""
A small QR encoder, so pairing is a camera scan instead of typing a token.

Standard library only -- no pillow, no qrcode package. Byte mode, error
correction level M (~15% recoverable, which is the right trade-off for
scanning off a screen), versions 1-10. That covers roughly 270 characters,
far more than a pairing URL needs.

Implemented from the specification, ISO/IEC 18004 -- nobody's source code was
read to write this. The Reed-Solomon error correction below runs over
GF(256) with primitive polynomial 0x11D; that polynomial and the GF
arithmetic are the standard's own mathematics, not anyone's implementation.

    from drobo_agent import qr
    svg = qr.svg("http://10.0.0.50:7420/?token=...")

DroboPix paired exactly this way -- a QR code shown in Drobo Dashboard that you
scanned with the phone. It was the right call then and it still is: nobody
should be typing a 32-character token on a phone keyboard.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# GF(256) arithmetic, primitive polynomial 0x11D
# --------------------------------------------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(nsym: int) -> list[int]:
    """
    Product of (x - alpha^i) for i in 0..nsym-1, highest degree first so that
    g[0] == 1. _ec_codewords below depends on that ordering.
    """
    g = [1]
    for i in range(nsym):
        out = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            out[j] ^= c                       # c * x
            out[j + 1] ^= _mul(c, _EXP[i])    # c * alpha^i
        g = out
    return g


def _ec_codewords(data: list[int], nsym: int) -> list[int]:
    gen = _generator(nsym)
    res = list(data) + [0] * nsym
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(1, len(gen)):
                res[i + j] ^= _mul(gen[j], coef)
    return res[len(data):]


# --------------------------------------------------------------------------
# Version tables, error-correction level M
#   version -> (ec per block, group1 blocks, group1 data cw,
#                             group2 blocks, group2 data cw)
# --------------------------------------------------------------------------
_SPEC_M = {
    1:  (10, 1, 16, 0, 0),
    2:  (16, 1, 28, 0, 0),
    3:  (26, 1, 44, 0, 0),
    4:  (18, 2, 32, 0, 0),
    5:  (24, 2, 43, 0, 0),
    6:  (16, 4, 27, 0, 0),
    7:  (18, 4, 31, 0, 0),
    8:  (22, 2, 38, 2, 39),
    9:  (22, 3, 36, 2, 37),
    10: (26, 4, 43, 1, 44),
}

_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

_ECL_BITS = 0b00          # level M
_FORMAT_XOR = 0b101010000010010


def _capacity(version: int) -> int:
    ec, b1, d1, b2, d2 = _SPEC_M[version]
    return b1 * d1 + b2 * d2


def _pick_version(nbytes: int) -> int:
    for v in range(1, 11):
        # 4 bits mode + count field + payload, rounded up to whole codewords
        count_bits = 8 if v < 10 else 16
        need = (4 + count_bits + nbytes * 8 + 7) // 8
        if need <= _capacity(v):
            return v
    raise ValueError("payload too long for this encoder (max ~270 bytes)")


# --------------------------------------------------------------------------
# Data encoding
# --------------------------------------------------------------------------
def _encode_data(payload: bytes, version: int) -> list[int]:
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                      # byte mode
    put(len(payload), count_bits)
    for byte in payload:
        put(byte, 8)

    total = _capacity(version) * 8
    put(0, min(4, total - len(bits)))   # terminator
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    i = 0
    while len(codewords) < _capacity(version):
        codewords.append(pad[i % 2])
        i += 1
    return codewords


def _interleave(codewords: list[int], version: int) -> list[int]:
    ec, b1, d1, b2, d2 = _SPEC_M[version]
    blocks, ecblocks, pos = [], [], 0
    for _ in range(b1):
        blocks.append(codewords[pos:pos + d1]); pos += d1
    for _ in range(b2):
        blocks.append(codewords[pos:pos + d2]); pos += d2
    for blk in blocks:
        ecblocks.append(_ec_codewords(blk, ec))

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for blk in blocks:
            if i < len(blk):
                out.append(blk[i])
    for i in range(ec):
        for blk in ecblocks:
            out.append(blk[i])
    return out


# --------------------------------------------------------------------------
# Matrix construction
# --------------------------------------------------------------------------
def _new_matrix(version: int):
    size = version * 4 + 17
    mod = [[None] * size for _ in range(size)]

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                inside = (0 <= r < 7 and 0 <= c < 7)
                on = inside and (r in (0, 6) or c in (0, 6) or
                                 (2 <= r <= 4 and 2 <= c <= 4))
                mod[rr][cc] = 1 if on else 0

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(8, size - 8):          # timing patterns
        bit = 1 if i % 2 == 0 else 0
        mod[6][i] = bit
        mod[i][6] = bit

    centres = _ALIGN[version]
    for r in centres:
        for c in centres:
            if (r < 8 and c < 8) or (r < 8 and c > size - 9) or (r > size - 9 and c < 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    mod[r + dr][c + dc] = 1 if (max(abs(dr), abs(dc)) != 1) else 0

    mod[size - 8][8] = 1                  # dark module

    for i in range(9):                    # reserve format areas
        if mod[8][i] is None: mod[8][i] = 0
        if mod[i][8] is None: mod[i][8] = 0
    for i in range(8):
        if mod[8][size - 1 - i] is None: mod[8][size - 1 - i] = 0
        if mod[size - 1 - i][8] is None: mod[size - 1 - i][8] = 0

    if version >= 7:
        # Reserve the two 6x3 version-information blocks, or data placement
        # would run straight through them.
        for i in range(18):
            r, c = i // 3, i % 3
            mod[size - 11 + c][r] = 0
            mod[r][size - 11 + c] = 0

    return mod, size


def _reserved(version: int, size: int):
    """Which cells are function patterns (and so not data)."""
    mod, _ = _new_matrix(version)
    return [[mod[r][c] is not None for c in range(size)] for r in range(size)]


def _place(mod, reserved, data: list[int], size: int):
    bits = []
    for cw in data:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1                       # skip the vertical timing column
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not reserved[row][c]:
                    mod[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2
    return mod


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _format_bits(mask: int) -> int:
    """15-bit format info: 5 data bits, BCH(15,5) remainder, then the spec XOR."""
    value = (_ECL_BITS << 3) | mask
    rem = value << 10
    gen = 0b10100110111
    for i in range(14, 9, -1):
        if rem & (1 << i):
            rem ^= gen << (i - 10)
    return ((value << 10) | rem) ^ _FORMAT_XOR


def _apply_format(mod, size: int, mask: int):
    bits = _format_bits(mask)
    for i in range(15):
        bit = (bits >> i) & 1
        if i < 6:
            mod[8][i] = bit
        elif i == 6:
            mod[8][7] = bit
        elif i == 7:
            mod[8][8] = bit
        elif i == 8:
            mod[7][8] = bit
        else:
            mod[14 - i][8] = bit
        if i < 8:
            mod[size - 1 - i][8] = bit
        else:
            mod[8][size - 15 + i] = bit
    mod[size - 8][8] = 1


def _version_bits(version: int) -> int:
    rem = version << 12
    gen = 0b1111100100101
    for i in range(17, 11, -1):
        if rem & (1 << i):
            rem ^= gen << (i - 12)
    return (version << 12) | rem


def _apply_version(mod, size: int, version: int):
    if version < 7:
        return
    bits = _version_bits(version)
    for i in range(18):
        bit = (bits >> i) & 1
        r, c = i // 3, i % 3
        mod[size - 11 + c][r] = bit
        mod[r][size - 11 + c] = bit


def _penalty(mod, size: int) -> int:
    score = 0
    # rule 1: runs of five or more
    for line in list(mod) + [list(col) for col in zip(*mod)]:
        run, prev = 1, line[0]
        for cell in line[1:]:
            if cell == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, cell
        if run >= 5:
            score += 3 + (run - 5)
    # rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            if mod[r][c] == mod[r][c + 1] == mod[r + 1][c] == mod[r + 1][c + 1]:
                score += 3
    # rule 3: finder-like patterns
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = list(reversed(pat1))
    for line in list(mod) + [list(col) for col in zip(*mod)]:
        for i in range(size - 10):
            window = line[i:i + 11]
            if window == pat1 or window == pat2:
                score += 40
    # rule 4: dark/light balance
    dark = sum(sum(row) for row in mod)
    pct = dark * 100 // (size * size)
    score += 10 * (abs(pct - 50) // 5)
    return score


def matrix(text: str) -> list[list[int]]:
    """Encode text and return the finished module grid as 0/1 rows."""
    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    data = _interleave(_encode_data(payload, version), version)

    best, best_score = None, None
    for mask in range(8):
        mod, size = _new_matrix(version)
        reserved = _reserved(version, size)
        _place(mod, reserved, data, size)
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and _MASKS[mask](r, c):
                    mod[r][c] ^= 1
        _apply_format(mod, size, mask)
        _apply_version(mod, size, version)
        grid = [[cell or 0 for cell in row] for row in mod]
        score = _penalty(grid, size)
        if best_score is None or score < best_score:
            best, best_score = grid, score
    return best


def svg(text: str, scale: int = 6, quiet: int = 4) -> str:
    """Render as a self-contained SVG. currentColor so it follows the theme."""
    grid = matrix(text)
    size = len(grid)
    total = (size + quiet * 2) * scale
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="{total}" '
        f'viewBox="0 0 {total} {total}" role="img" aria-label="Pairing QR code">',
        f'<rect width="{total}" height="{total}" fill="#fff"/>',
        '<g fill="#000">',
    ]
    for r, row in enumerate(grid):
        c = 0
        while c < size:
            if row[c]:
                start = c
                while c < size and row[c]:
                    c += 1
                parts.append(
                    f'<rect x="{(start + quiet) * scale}" y="{(r + quiet) * scale}" '
                    f'width="{(c - start) * scale}" height="{scale}"/>')
            else:
                c += 1
    parts.append("</g></svg>")
    return "".join(parts)
