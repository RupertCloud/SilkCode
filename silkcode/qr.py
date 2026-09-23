"""A QR encoder, so pairing a phone is pointing a camera rather than typing.

The GUI's URL carries a 32-character access token when the daemon is reachable
beyond loopback. That is the right security posture and a miserable thing to
copy onto a phone by hand, which is the whole reason this module exists.

Silk Code has exactly one runtime dependency (httpx, see pyproject.toml), so
this is written against the spec rather than pulling in a QR library: byte
mode, versions 1-10, error-correction levels L and M. A URL with a token runs
about 55-90 characters, comfortably inside that range.

The pieces, in the order the spec applies them:

  encode()    text -> data codewords (mode, length, payload, padding)
  _ecc()      data codewords -> Reed-Solomon check codewords over GF(256)
  _interleave() data + ecc blocks -> the final codeword stream
  _place()    codewords -> module matrix, with the fixed patterns reserved
  _mask()     pick the mask that scores best under the spec's penalty rules
"""

from __future__ import annotations

# (ec codewords per block, group1 blocks, group1 data, group2 blocks, group2 data)
# Versions 1-10 at levels L and M, from the standard's block tables.
_BLOCKS: dict[tuple[int, str], tuple[int, int, int, int, int]] = {
    (1, "L"): (7, 1, 19, 0, 0),    (1, "M"): (10, 1, 16, 0, 0),
    (2, "L"): (10, 1, 34, 0, 0),   (2, "M"): (16, 1, 28, 0, 0),
    (3, "L"): (15, 1, 55, 0, 0),   (3, "M"): (26, 1, 44, 0, 0),
    (4, "L"): (20, 1, 80, 0, 0),   (4, "M"): (18, 2, 32, 0, 0),
    (5, "L"): (26, 1, 108, 0, 0),  (5, "M"): (24, 2, 43, 0, 0),
    (6, "L"): (18, 2, 68, 0, 0),   (6, "M"): (16, 4, 27, 0, 0),
    (7, "L"): (20, 2, 78, 0, 0),   (7, "M"): (18, 4, 31, 0, 0),
    (8, "L"): (24, 2, 97, 0, 0),   (8, "M"): (22, 2, 38, 2, 39),
    (9, "L"): (30, 2, 116, 0, 0),  (9, "M"): (22, 3, 36, 2, 37),
    (10, "L"): (18, 2, 68, 2, 69), (10, "M"): (26, 4, 43, 1, 44),
}

# Row/column centres of the alignment patterns, by version.
_ALIGN: dict[int, list[int]] = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

_ECC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}
MAX_VERSION = 10


class QRError(ValueError):
    """The data does not fit any supported version."""


# ---- GF(256) ----------------------------------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256


def _init_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:          # reduce by the QR generator polynomial 0x11D
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """The Reed-Solomon generator polynomial (x-2^0)(x-2^1)...(x-2^(n-1))."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= _gf_mul(coef, 1)
            nxt[j + 1] ^= _gf_mul(coef, _EXP[i])
        poly = nxt
    return poly


def _ecc(data: list[int], count: int) -> list[int]:
    """`count` Reed-Solomon check codewords for one block."""
    gen = _generator(count)
    rem = list(data) + [0] * count
    for i in range(len(data)):
        factor = rem[i]
        if factor:
            for j, g in enumerate(gen):
                rem[i + j] ^= _gf_mul(g, factor)
    return rem[len(data):]


# ---- data encoding ----------------------------------------------------------

def _capacity(version: int, ecc: str) -> int:
    ecw, g1, d1, g2, d2 = _BLOCKS[(version, ecc)]
    return g1 * d1 + g2 * d2


def _pick_version(length: int, ecc: str) -> int:
    """Smallest version whose data capacity holds `length` bytes in byte mode."""
    for version in range(1, MAX_VERSION + 1):
        # 4 bits mode + 8 or 16 bits length + payload, rounded up to codewords
        count_bits = 8 if version < 10 else 16
        needed = (4 + count_bits + length * 8 + 7) // 8
        if needed <= _capacity(version, ecc):
            return version
    raise QRError(
        f"{length} bytes does not fit a version-{MAX_VERSION} QR at level {ecc}; "
        "shorten the URL (a shorter host name, or a smaller token)")


def _encode_data(text: str, version: int, ecc: str) -> list[int]:
    payload = text.encode("utf-8")
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                      # byte mode
    push(len(payload), count_bits)
    for byte in payload:
        push(byte, 8)

    capacity_bits = _capacity(version, ecc) * 8
    if len(bits) > capacity_bits:
        raise QRError("data does not fit the chosen version")
    push(0, min(4, capacity_bits - len(bits)))     # terminator
    while len(bits) % 8:                           # pad to a codeword boundary
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]
    # Alternating pad bytes, per the spec, until the block is full.
    for pad in _pad_cycle():
        if len(codewords) >= _capacity(version, ecc):
            break
        codewords.append(pad)
    return codewords


def _pad_cycle():
    while True:
        yield 0xEC
        yield 0x11


def _interleave(codewords: list[int], version: int, ecc: str) -> list[int]:
    """Split into blocks, compute ECC per block, then interleave both sets."""
    ecw, g1, d1, g2, d2 = _BLOCKS[(version, ecc)]
    blocks: list[list[int]] = []
    pos = 0
    for _ in range(g1):
        blocks.append(codewords[pos:pos + d1])
        pos += d1
    for _ in range(g2):
        blocks.append(codewords[pos:pos + d2])
        pos += d2
    checks = [_ecc(block, ecw) for block in blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ecw):
        for check in checks:
            out.append(check[i])
    return out


# ---- matrix -----------------------------------------------------------------

def _size(version: int) -> int:
    return version * 4 + 17


def _reserved(version: int) -> list[list[bool]]:
    """Modules the data stream must skip: finders, timing, alignment, format."""
    n = _size(version)
    res = [[False] * n for _ in range(n)]

    def block(r0: int, c0: int, h: int, w: int) -> None:
        for r in range(r0, r0 + h):
            for c in range(c0, c0 + w):
                if 0 <= r < n and 0 <= c < n:
                    res[r][c] = True

    for r0, c0 in ((0, 0), (0, n - 8), (n - 8, 0)):
        block(r0, c0, 9 if r0 == 0 else 8, 9 if c0 == 0 else 8)
    for i in range(n):
        res[6][i] = True
        res[i][6] = True
    centres = _ALIGN[version]
    for r in centres:
        for c in centres:
            if (r < 9 and c < 9) or (r < 9 and c > n - 10) or (r > n - 10 and c < 9):
                continue
            block(r - 2, c - 2, 5, 5)
    if version >= 7:
        block(n - 11, 0, 3, 6)
        block(0, n - 11, 6, 3)
    return res


def _draw_function_patterns(m: list[list[bool]], version: int) -> None:
    n = _size(version)

    def finder(r0: int, c0: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if not (0 <= rr < n and 0 <= cc < n):
                    continue
                edge = max(abs(r - 3), abs(c - 3))
                m[rr][cc] = edge in (0, 1, 3)   # 3 = outer ring, 0-1 = centre

    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)
    for i in range(8, n - 8):
        m[6][i] = m[i][6] = i % 2 == 0
    m[n - 8][8] = True                          # the always-dark module

    centres = _ALIGN[version]
    for r in centres:
        for c in centres:
            if (r < 9 and c < 9) or (r < 9 and c > n - 10) or (r > n - 10 and c < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1

    if version >= 7:
        bits = _version_bits(version)
        for i in range(18):
            bit = (bits >> i) & 1
            m[i // 3][n - 11 + i % 3] = bool(bit)
            m[n - 11 + i % 3][i // 3] = bool(bit)


def _version_bits(version: int) -> int:
    """18-bit version information: 6 data bits + BCH(18,6) remainder."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ (0x1F25 if (rem >> 11) & 1 else 0)
    return (version << 12) | rem


def _format_bits(ecc: str, mask: int) -> int:
    """15-bit format information: 5 data bits, BCH(15,5), then the spec's mask."""
    data = (_ECC_BITS[ecc] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ (0x537 if (rem >> 9) & 1 else 0)
    return ((data << 10) | rem) ^ 0x5412


def _place_format(m: list[list[bool]], version: int, ecc: str, mask: int) -> None:
    n = _size(version)
    bits = _format_bits(ecc, mask)
    for i in range(15):
        # Most-significant bit first: position i carries format bit 14-i.
        bit = bool((bits >> (14 - i)) & 1)
        # copy 1, around the top-left finder
        if i < 6:
            m[8][i] = bit
        elif i == 6:
            m[8][7] = bit
        elif i == 7:
            m[8][8] = bit
        elif i == 8:
            m[7][8] = bit
        else:
            m[14 - i][8] = bit
        # copy 2, split 7/8 between the other two finders. Seven in the
        # column, not eight: the eighth would land on (n-8, 8), which is the
        # always-dark module and not part of the format string.
        if i < 7:
            m[n - 1 - i][8] = bit
        else:
            m[8][n - 15 + i] = bit


def _place(codewords: list[int], version: int) -> list[list[bool]]:
    """Zigzag the codeword bits up and down the two-column strips, right to left."""
    n = _size(version)
    m = [[False] * n for _ in range(n)]
    reserved = _reserved(version)
    _draw_function_patterns(m, version)

    bits = [(cw >> i) & 1 for cw in codewords for i in range(7, -1, -1)]
    idx = 0
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:        # the vertical timing pattern is not a data column
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for row in rows:
            for c in (col, col - 1):
                if reserved[row][c]:
                    continue
                m[row][c] = bool(bits[idx]) if idx < len(bits) else False
                idx += 1
        col -= 2
        upward = not upward
    return m


# ---- masking ----------------------------------------------------------------

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


def _penalty(m: list[list[bool]]) -> int:
    """The spec's four penalty rules. Lower is a more scannable symbol."""
    n = len(m)
    score = 0

    # Rule 1: runs of five or more same-coloured modules in a row or column.
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, prev = 1, line[0]
        for value in line[1:]:
            if value == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, value
        if run >= 5:
            score += 3 + (run - 5)

    # Rule 2: every 2x2 block of one colour.
    for r in range(n - 1):
        for c in range(n - 1):
            block = (m[r][c], m[r][c + 1], m[r + 1][c], m[r + 1][c + 1])
            if all(block) or not any(block):
                score += 3

    # Rule 3: the finder-like 1:1:3:1:1 pattern with four light modules beside it.
    target_a = [True, False, True, True, True, False, True,
                False, False, False, False]
    target_b = list(reversed(target_a))
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(n - 10):
            window = line[i:i + 11]
            if window == target_a or window == target_b:
                score += 40

    # Rule 4: deviation from an even balance of dark and light. The spec takes
    # the two multiples of 5 bracketing the dark percentage and scores by
    # whichever sits nearer 50 - not by the raw percentage.
    dark = sum(sum(1 for v in row if v) for row in m)
    percent = dark * 100 // (n * n)
    lower = percent - percent % 5
    score += 10 * min(abs(lower - 50), abs(lower + 5 - 50)) // 5
    return score


def _apply_mask(base: list[list[bool]], version: int, ecc: str,
                mask: int) -> list[list[bool]]:
    reserved = _reserved(version)
    m = [row[:] for row in base]
    rule = _MASKS[mask]
    for r in range(len(m)):
        for c in range(len(m)):
            if not reserved[r][c] and rule(r, c):
                m[r][c] = not m[r][c]
    _place_format(m, version, ecc, mask)
    return m


# ---- public API -------------------------------------------------------------

def encode(text: str, ecc: str = "M") -> list[list[bool]]:
    """The module matrix for `text`. True is a dark module.

    The version is the smallest that fits; the mask is whichever of the eight
    scores best under the spec's penalty rules.
    """
    if ecc not in ("L", "M"):
        raise QRError(f"unsupported error-correction level {ecc!r} (use L or M)")
    payload = text.encode("utf-8")
    version = _pick_version(len(payload), ecc)
    codewords = _interleave(_encode_data(text, version, ecc), version, ecc)
    base = _place(codewords, version)
    best, best_score = None, None
    for mask in range(8):
        candidate = _apply_mask(base, version, ecc, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


def render(matrix: list[list[bool]], quiet: int = 2) -> str:
    """The matrix as text, two module rows per line of output.

    Half-block characters keep the symbol close to square in a terminal, where
    a cell is about twice as tall as it is wide. A QR printed with one row per
    line comes out stretched and many scanners refuse it.
    """
    n = len(matrix)
    padded = [[False] * (n + quiet * 2) for _ in range(quiet)]
    for row in matrix:
        padded.append([False] * quiet + list(row) + [False] * quiet)
    padded += [[False] * (n + quiet * 2) for _ in range(quiet)]
    if len(padded) % 2:
        padded.append([False] * (n + quiet * 2))

    lines = []
    for i in range(0, len(padded), 2):
        top, bottom = padded[i], padded[i + 1]
        # Dark modules print as background: scanners want a light-on-dark
        # symbol, and a terminal's default background is usually dark.
        line = "".join(
            {(False, False): "█", (True, True): " ",
             (True, False): "▄", (False, True): "▀"}[(t, b)]
            for t, b in zip(top, bottom))
        lines.append(line)
    return "\n".join(lines)


def terminal_qr(text: str, ecc: str = "M") -> str:
    """`text` as a scannable block of terminal output."""
    return render(encode(text, ecc))
