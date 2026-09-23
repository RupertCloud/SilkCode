"""Reading Tailscale's state, so the advice depends on the situation.

`silkcode inference host` tells you exactly what to run when a model server
is bound to loopback — per tool, with the macOS and Windows variants. The
pairing banner used one sentence, "put both machines on a Tailscale tailnet",
for three states with three different one-line answers. These tests pin that
each state now gets its own.

Nothing here starts anything. `tailscale up` joins a network and changes how
a machine is reachable; naming the command is Silk Code's job, running it is
not.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from silkcode import tailnet


def fake_cli(tmp_path, payload, exit_code=0):
    """A stand-in `tailscale` that prints `payload`."""
    script = tmp_path / "tailscale"
    body = payload if isinstance(payload, str) else json.dumps(payload)
    script.write_text("#!/bin/sh\ncat <<'JSON'\n" + body + "\nJSON\nexit %d\n" % exit_code)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def with_tailscale(tmp_path, monkeypatch):
    def install(payload, exit_code=0):
        fake_cli(tmp_path, payload, exit_code)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
        return tailnet.status()
    return install


# ---- each state is a different situation with a different answer -----------

def test_a_running_tailnet_reports_its_address_and_name(with_tailscale):
    state = with_tailscale({
        "BackendState": "Running",
        "Self": {"DNSName": "laptop.tail1a2b3.ts.net.",
                 "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"]},
    })
    assert state.state == "running" and state.running
    assert state.address == "100.101.102.103"
    assert state.name == "laptop.tail1a2b3.ts.net", "the trailing dot was kept"
    assert state.host() == "laptop.tail1a2b3.ts.net", \
        "the MagicDNS name is what a person can read off a screen"


@pytest.mark.parametrize("backend, expected, says", [
    ("NeedsLogin", "needs-login", "not signed in"),
    ("Stopped", "stopped", "stopped"),
    ("NoState", "stopped", "stopped"),
])
def test_an_installed_but_idle_tailscale_is_told_apart(with_tailscale, backend,
                                                       expected, says):
    """Installed-but-logged-out and installed-but-stopped are different
    problems. Both used to get "go and set up a tailnet"."""
    state = with_tailscale({"BackendState": backend, "Self": {"TailscaleIPs": []}}, 1)
    assert state.state == expected
    assert says in state.next_step()
    assert "tailscale up" in state.next_step()


def test_no_tailscale_at_all_says_install_it(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))          # nothing on it
    monkeypatch.setattr(tailnet, "MAC_APP_CLI", str(tmp_path / "nope"))
    state = tailnet.status()
    assert state.state == "absent"
    assert "not installed" in state.next_step()
    assert "tailscale up" not in state.next_step(), \
        "told to run a command that is not there"


def test_running_without_an_address_is_not_called_running(with_tailscale):
    """The backend can report Running before the node has an address. Handing
    out a URL with no host in it is worse than saying it is not ready."""
    state = with_tailscale({"BackendState": "Running", "Self": {"TailscaleIPs": []}})
    assert not state.running
    assert state.state == "stopped"


def test_a_non_tailnet_address_is_not_mistaken_for_one(with_tailscale):
    """Only 100.64.0.0/10 is the mesh range. A LAN address in that field is
    not something another network can reach."""
    state = with_tailscale({
        "BackendState": "Running",
        "Self": {"TailscaleIPs": ["192.168.1.20"]},
    })
    assert state.address is None
    assert not state.running


# ---- and none of it may fail loudly ----------------------------------------

@pytest.mark.parametrize("payload", ["not json at all", "", "[]", "{"])
def test_unreadable_output_is_not_trusted(with_tailscale, payload):
    state = with_tailscale(payload, 1)
    assert state.state in ("unknown", "stopped")
    assert state.address is None
    assert state.next_step()          # still says something usable


def test_a_hanging_tailscale_does_not_hang_the_daemon(monkeypatch):
    """This runs while a daemon is starting up. A wedged binary must not hold
    the GUI hostage."""
    import subprocess

    def hang(*_a, **_k):
        raise subprocess.TimeoutExpired("tailscale", 5)

    monkeypatch.setattr(tailnet, "cli_path", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(subprocess, "run", hang)
    assert tailnet.status().state == "unknown"


def test_the_advice_is_silent_when_there_is_already_a_tailnet_address(monkeypatch):
    """Someone whose 100.x address is already listed needs none of this."""
    monkeypatch.setattr(tailnet, "cli_path", lambda: None)
    assert tailnet.advice(has_tailnet_address=True) == ""


def test_the_advice_explains_why_the_lan_address_is_not_enough(monkeypatch):
    monkeypatch.setattr(tailnet, "cli_path", lambda: None)
    text = tailnet.advice(has_tailnet_address=False)
    assert "192.168" in text and "router" in text, \
        "does not say what actually stops working, only what to install"
    assert "tailscale.com" in text


# ---- what the phone is actually handed --------------------------------------

def test_the_pairing_url_uses_the_magicdns_name(monkeypatch):
    """`http://laptop.tail1a2b3.ts.net:8377/?token=…` survives the node being
    re-addressed and can be read off a screen. Four octets cannot."""
    import silkcode.gui.server as server

    monkeypatch.setattr(server, "reachable_addresses",
                        lambda: [("100.101.102.103", "Tailscale"),
                                 ("192.168.1.20", "LAN")], raising=False)
    monkeypatch.setattr("silkcode.inference.reachable_addresses",
                        lambda: [("100.101.102.103", "Tailscale"),
                                 ("192.168.1.20", "LAN")])
    monkeypatch.setattr(tailnet, "status",
                        lambda: tailnet.Tailnet(state="running",
                                                address="100.101.102.103",
                                                name="laptop.tail1a2b3.ts.net"))

    info = server.GuiState.pairing_info(object.__new__(server.GuiState),
                                        8377, "TOKEN", bound_host="0.0.0.0")
    by_label = {a["label"]: a for a in info["addresses"]}
    assert by_label["Tailscale"]["url"] == \
        "http://laptop.tail1a2b3.ts.net:8377/?token=TOKEN"
    assert by_label["Tailscale"]["address"] == "100.101.102.103", \
        "the raw address is still reported, for anyone who needs it"
    assert by_label["LAN"]["url"] == "http://192.168.1.20:8377/?token=TOKEN", \
        "a LAN address has no MagicDNS name and must keep its own"


def test_a_pairing_url_falls_back_to_the_address(monkeypatch):
    """No MagicDNS, or Tailscale unreadable: the 100.x address still works and
    is what gets handed out. Losing the QR because a hostname was missing
    would be a worse outcome than an ugly URL."""
    import silkcode.gui.server as server

    monkeypatch.setattr("silkcode.inference.reachable_addresses",
                        lambda: [("100.101.102.103", "Tailscale")])
    monkeypatch.setattr(tailnet, "status",
                        lambda: tailnet.Tailnet(state="running",
                                                address="100.101.102.103", name=None))
    info = server.GuiState.pairing_info(object.__new__(server.GuiState),
                                        8377, "TOKEN", bound_host="0.0.0.0")
    assert info["addresses"][0]["url"] == "http://100.101.102.103:8377/?token=TOKEN"
