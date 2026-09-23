"""Driving the GUI from a phone: which address to hand out, and pairing.

The daemon holds API keys and runs an agent that edits files and executes
commands, so reaching it from another device is a security decision as much
as a convenience one. These cover the convenience half — the auth half lives
in test_gui.py — plus the phone layout, which is what makes the page usable
once you have opened it.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from silkcode.inference import reachable_addresses

APP = Path(__file__).resolve().parents[1] / "silkcode" / "gui" / "app.html"


# ---- which address is worth printing ----------------------------------------

@pytest.fixture
def addresses(monkeypatch):
    def use(values):
        monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                            lambda: list(values))
    return use


def test_a_tailscale_address_is_labelled_and_listed_first(addresses):
    """100.64.0.0/10 is the CGNAT range Tailscale allocates from. It is the
    address that still works from a cafe, so it goes first — a LAN address
    only resolves while both machines are on the same router."""
    addresses(["192.168.1.20", "100.101.102.103"])
    assert reachable_addresses() == [("100.101.102.103", "Tailscale"),
                                     ("192.168.1.20", "LAN")]


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.101.102.103", "100.127.255.254"])
def test_the_whole_cgnat_range_reads_as_tailscale(addresses, ip):
    addresses([ip])
    assert reachable_addresses() == [(ip, "Tailscale")]


@pytest.mark.parametrize("ip", ["100.63.255.255", "100.128.0.1", "10.0.0.5",
                                "192.168.1.20", "172.16.3.4"])
def test_addresses_outside_that_range_are_lan(addresses, ip):
    """100.63.x and 100.128.x bracket the range; classifying them as a mesh
    address would promise reachability the machine does not have."""
    addresses([ip])
    assert reachable_addresses() == [(ip, "LAN")]


def test_a_machine_with_no_address_reports_none(addresses):
    addresses([])
    assert reachable_addresses() == []


def test_unparseable_addresses_are_dropped_rather_than_crashing(addresses):
    addresses(["not-an-ip", "192.168.1.20"])
    assert reachable_addresses() == [("192.168.1.20", "LAN")]


# ---- the pairing banner -----------------------------------------------------

def banner(port=8377, token="tok123", ips=("192.168.1.20",), monkeypatch=None):
    from silkcode.gui import server
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses", lambda: list(ips))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server._print_pairing(port, token)
    return out.getvalue()


def test_the_banner_prints_a_url_carrying_the_token(monkeypatch):
    text = banner(monkeypatch=monkeypatch)
    assert "http://192.168.1.20:8377/?token=tok123" in text


def test_the_banner_prints_a_scannable_qr(monkeypatch):
    """The token is 32 characters; retyping it on a phone is the friction this
    removes, so the QR has to actually be there."""
    text = banner(monkeypatch=monkeypatch)
    assert "Point a phone camera" in text
    block = [ln for ln in text.splitlines() if set(ln) <= set("█▀▄ ") and len(ln) > 20]
    assert len(block) > 8, "no QR block in the banner"
    assert len(set(len(ln) for ln in block)) == 1, "QR rows are ragged"


def test_a_lan_only_machine_is_told_how_to_reach_it_from_anywhere(monkeypatch):
    text = banner(ips=("192.168.1.20",), monkeypatch=monkeypatch)
    assert "same network only" in text
    assert "tailscale.com" in text.lower()


def test_a_tailscale_machine_is_not_nagged_about_tailscale(monkeypatch):
    text = banner(ips=("100.101.102.103", "192.168.1.20"), monkeypatch=monkeypatch)
    assert "works from anywhere" in text
    assert "tailscale.com" not in text.lower()
    # the QR should encode the address that keeps working, not the LAN one
    assert text.index("100.101.102.103") < text.index("192.168.1.20")


def test_a_machine_with_no_address_says_so_instead_of_printing_a_qr(monkeypatch):
    text = banner(ips=(), monkeypatch=monkeypatch)
    assert "cannot be reached" in text
    assert "Point a phone camera" not in text


def test_the_banner_works_without_a_token(monkeypatch):
    """A daemon bound to a named host the user trusts may run tokenless; the
    URL must still be right rather than carrying a literal 'None'."""
    from silkcode.gui import server
    monkeypatch.setattr("silkcode.inference.local_ipv4_addresses",
                        lambda: ["192.168.1.20"])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        server._print_pairing(8377, None)
    text = out.getvalue()
    assert "http://192.168.1.20:8377/" in text
    assert "None" not in text


# ---- the page on a phone ----------------------------------------------------

def test_the_page_reflows_for_phones():
    """The layout is a fixed three-column grid (230px + 1fr + 270px). Without
    a media query a phone got a page wider than the screen, with the composer
    off the right-hand edge."""
    text = APP.read_text(encoding="utf-8")
    assert "@media (max-width: 768px), (max-height: 480px)" in text
    assert "grid-template-columns: 1fr" in text


def test_the_phone_layout_overrides_come_after_the_rules_they_override():
    """Same specificity as the base #id rules, so it only wins by position.
    Placed earlier in the sheet it silently lost, and the panes stayed in
    their desktop columns."""
    text = APP.read_text(encoding="utf-8")
    media = text.index("@media (max-width: 768px)")
    assert media > text.index("#composer { grid-column: 2")
    assert media > text.index("#bottom { grid-column: 2")
    assert media < text.index("</style>")


def test_the_phone_pane_switcher_covers_every_pane():
    """One pane fits a phone at a time, so each desktop pane needs a way back."""
    text = APP.read_text(encoding="utf-8")
    offered = set(re.findall(r'data-pane="([a-z]+)"', text))
    assert {"chat", "files", "activity", "bottom"} <= offered
    for pane in ("chat", "files", "activity", "bottom"):
        assert f'main[data-pane="{pane}"] #{pane}' in text, pane


def test_the_switcher_is_hidden_on_desktop():
    text = APP.read_text(encoding="utf-8")
    assert "#mobile-tabs { display: none; }" in text


def test_the_composer_clears_the_ios_home_indicator():
    assert "safe-area-inset-bottom" in APP.read_text(encoding="utf-8")


def test_the_input_is_large_enough_that_ios_does_not_zoom():
    """Safari zooms the page when a focused input is under 16px, which leaves
    the layout scrolled sideways with no way back."""
    text = APP.read_text(encoding="utf-8")
    block = text[text.index("@media (max-width: 768px)"):]
    assert "font-size: 16px" in block


def test_a_phone_in_landscape_also_gets_the_phone_layout():
    """A phone on its side is 844px wide and ~390px tall. Width alone reads
    that as a desktop, and the three-column grid overflows sideways there —
    so the short viewport is the second way in."""
    text = APP.read_text(encoding="utf-8")
    assert "(max-height: 480px)" in text


# ---- pairing a second device, from the page ---------------------------------

def test_the_page_offers_a_pair_button():
    """Startup prints the QR once, and only when the daemon was started off
    loopback. Everything after that — a scrolled terminal, a second phone —
    needs a way in that is not restarting the daemon."""
    text = APP.read_text(encoding="utf-8")
    assert 'id="pair-btn"' in text
    assert 'id="pair-modal"' in text
    assert '/api/pairing' in text


def test_the_pair_modal_draws_the_qr_without_a_library():
    """The daemon sends a matrix; the page renders cells. Nothing is fetched,
    which matters because the GUI is served offline."""
    text = APP.read_text(encoding="utf-8")
    # the grid element is built in script, not markup, so look for both halves
    assert 'grid.id = "pair-qr"' in text
    assert "#pair-qr" in text          # its styles
    assert "gridTemplateColumns" in text


def test_the_pair_modal_warns_that_the_link_is_a_credential():
    text = APP.read_text(encoding="utf-8")
    assert "carries the access token" in text
