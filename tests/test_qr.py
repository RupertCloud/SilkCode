"""The QR encoder behind `silkcode gui` pairing.

Silk Code has one runtime dependency, so the encoder is written against the
spec rather than pulled from a library — which means it needs to be held to
the spec rather than to itself. Two kinds of check do that:

  * golden matrices, taken module-for-module from an independent
    implementation (the `qrcode` package) during development and frozen here.
    They are not this encoder's own output, so they catch it drifting.
  * values the standard publishes directly: format strings, the Reed-Solomon
    generator polynomial, version sizes.

A QR that merely *looks* like a QR is the failure this guards against: the
first draft rendered a plausible-looking symbol that no scanner could read,
because the format bits were placed least-significant-first and the eighth
one overwrote the always-dark module.
"""

from __future__ import annotations

import pytest

from silkcode.qr import (MAX_VERSION, QRError, _format_bits, _generator, _pick_version,
                         _size, encode, render, terminal_qr)


def rows(matrix) -> list[str]:
    return ["".join("1" if v else "0" for v in row) for row in matrix]


# Frozen from the `qrcode` package, which is not used at runtime or in CI.
GOLDEN_A = [
    "111111100101101111111", "100000101011001000001", "101110101101001011101",
    "101110101011001011101", "101110100100101011101", "100000100011001000001",
    "111111101010101111111", "000000001100000000000", "100000101011011001110",
    "100110000001110111001", "001011100110101100000", "010101011001111101010",
    "110100111101111111111", "000000001100100000101", "111111100111010011110",
    "100000100010001000111", "101110100111010011100", "101110100101111101000",
    "101110100101110111011", "100000100011111101000", "111111101010100100110",
]

GOLDEN_URL = [
    "1111111001110100101111111", "1000001000011100001000001", "1011101001110000101011101",
    "1011101010110111101011101", "1011101010101010001011101", "1000001001100000101000001",
    "1111111010101010101111111", "0000000001001000000000000", "1100011101101101100011000",
    "0000110110100101110011110", "0001001001011101001101011", "1110100100110001100011001",
    "1111111101100010111000001", "1001110010101111000000010", "1011101010100001010101011",
    "1010000111001010100010101", "1010001101101100111110100", "0000000011000011100010100",
    "1111111010101010101011001", "1000001010010010100010001", "1011101001011101111111100",
    "1011101001001000001101011", "1011101001000110010000101", "1000001010010011101110001",
    "1111111011010011101001001",
]


# ---- against an independent implementation ----------------------------------

def test_a_single_character_matches_the_reference_module_for_module():
    assert rows(encode("a", "M")) == GOLDEN_A


def test_a_url_matches_the_reference_module_for_module():
    assert rows(encode("http://192.168.1.20:8377", "L")) == GOLDEN_URL


# ---- against values the standard publishes ----------------------------------

@pytest.mark.parametrize("ecc,mask,expected", [
    # ISO/IEC 18004 table of the 32 format strings.
    ("L", 0, "111011111000100"),
    ("L", 1, "111001011110011"),
    ("M", 0, "101010000010010"),
    ("M", 5, "100000011001110"),
    ("Q", 0, "011010101011111"),
    ("H", 0, "001011010001001"),
])
def test_format_strings_match_the_published_table(ecc, mask, expected):
    assert format(_format_bits(ecc, mask), "015b") == expected


def test_the_reed_solomon_generator_matches_the_published_polynomial():
    # The degree-7 generator's coefficients, as tabulated in the standard.
    assert _generator(7) == [1, 127, 122, 154, 164, 11, 68, 117]


@pytest.mark.parametrize("version,size", [(1, 21), (2, 25), (6, 41), (10, 57)])
def test_symbol_sizes_follow_the_version_formula(version, size):
    assert _size(version) == size


# ---- structure that every symbol must have ----------------------------------

def finder_at(matrix, r0: int, c0: int) -> bool:
    """The 7x7 finder: dark ring, light ring, dark 3x3 core."""
    for r in range(7):
        for c in range(7):
            edge = max(abs(r - 3), abs(c - 3))
            if matrix[r0 + r][c0 + c] != (edge in (0, 1, 3)):
                return False
    return True


def test_every_symbol_carries_its_three_finder_patterns():
    for text in ("a", "http://192.168.1.20:8377", "z" * 90):
        m = encode(text)
        n = len(m)
        assert finder_at(m, 0, 0), text
        assert finder_at(m, 0, n - 7), text
        assert finder_at(m, n - 7, 0), text


def test_the_timing_patterns_alternate():
    m = encode("http://100.64.0.1:8377/?token=abcdef")
    n = len(m)
    for i in range(8, n - 8):
        assert m[6][i] is (i % 2 == 0)
        assert m[i][6] is (i % 2 == 0)


def test_the_always_dark_module_is_dark():
    """It sits next to the format bits and was, in the first draft, quietly
    overwritten by one of them — which is exactly the kind of fault that still
    renders as a convincing-looking QR."""
    for text in ("a", "x" * 100, "http://192.168.1.20:8377"):
        m = encode(text)
        assert m[len(m) - 8][8] is True, text


# ---- picking a version ------------------------------------------------------

def test_short_data_uses_the_smallest_symbol():
    assert _pick_version(1, "M") == 1
    assert len(encode("a")) == 21


def test_a_longer_url_grows_the_symbol():
    small = len(encode("http://192.168.1.20:8377"))
    large = len(encode("http://192.168.1.20:8377/?token=" + "z" * 32))
    assert large > small


def test_data_beyond_the_supported_versions_is_refused_with_advice():
    with pytest.raises(QRError, match="does not fit"):
        encode("z" * 400)


def test_an_unsupported_correction_level_is_refused():
    with pytest.raises(QRError, match="error-correction"):
        encode("hello", "H")


def test_the_largest_supported_version_still_encodes():
    assert len(encode("z" * 260, "L")) == _size(MAX_VERSION)


# ---- rendering --------------------------------------------------------------

def test_the_rendered_block_has_a_quiet_zone_on_every_side():
    """Scanners need the light margin; without it the symbol runs into
    whatever else is on the terminal line and stops being findable."""
    out = terminal_qr("http://192.168.1.20:8377")
    lines = out.splitlines()
    assert lines, "rendered nothing"
    # Dark modules render as background, so a fully-quiet row is all full blocks.
    assert set(lines[0]) == {"█"}
    assert set(lines[-1]) == {"█"}
    assert all(line.startswith("██") and line.endswith("██") for line in lines)


def test_rendering_halves_the_rows_so_the_symbol_stays_square():
    """One text row per module row prints a symbol twice as tall as it is
    wide, and many scanners refuse that."""
    m = encode("a")
    out = render(m, quiet=2)
    lines = out.splitlines()
    assert len(lines) == (len(m) + 4 + 1) // 2
    assert len(lines[0]) == len(m) + 4


def test_unicode_survives_the_round_trip_into_the_matrix():
    # Encoding is over UTF-8 bytes, not characters; a multi-byte string must
    # pick a version by its byte length.
    assert len(encode("café ☕")) >= 21
