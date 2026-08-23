"""Tailscale, when it is there.

Silk Code does not install, start or manage Tailscale. It reads whether one
is running, because that is the difference between a GUI you can reach from
a train and one that stops working when you leave the building.

The reason to look rather than to guess: `silkcode inference host` already
tells you *exactly* what to run when a model server is bound to loopback —
per tool, including the macOS and Windows variants. The pairing banner used
one sentence, "put both machines on a Tailscale tailnet", for three states
that have three different one-line answers: not installed, installed but
logged out, and installed but stopped. Advice that does not depend on the
situation is advice you have to go and research.

What this deliberately does not do is start anything. `tailscale up` can open
a browser, join a network and change how a machine is reachable; that is the
user's decision to make, and Silk Code's job is to name the command, not to
run it.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from dataclasses import dataclass

# Tailscale hands out addresses from the RFC 6598 carrier-grade NAT range,
# which is why a 100.x address here means a mesh rather than a LAN.
CGNAT = ipaddress.ip_network("100.64.0.0/10")

# The CLI is on PATH for the package installs; the macOS app hides it inside
# the bundle, which is exactly where someone running the GUI on a MacBook
# will have it.
MAC_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def cli_path() -> str | None:
    """Where the tailscale command is, or None."""
    found = shutil.which("tailscale")
    if found:
        return found
    from pathlib import Path
    return MAC_APP_CLI if Path(MAC_APP_CLI).exists() else None


@dataclass
class Tailnet:
    """What Tailscale is doing on this machine right now."""

    state: str                    # absent | stopped | needs-login | running | unknown
    address: str | None = None    # the 100.x address other devices dial
    name: str | None = None       # MagicDNS name, stabler than the address

    @property
    def running(self) -> bool:
        return self.state == "running" and bool(self.address)

    def host(self) -> str | None:
        """What to put in a URL. The MagicDNS name where there is one: it
        survives a re-address, and it is far easier to read off a screen than
        four octets."""
        return self.name or self.address

    def next_step(self) -> str:
        """The one thing to do about it, in words, for this exact state."""
        if self.state == "absent":
            return ("Tailscale is not installed here. Install it on this machine "
                    "and on the phone (https://tailscale.com/download), sign both "
                    "into the same account, then run this again.")
        if self.state == "needs-login":
            return ("Tailscale is installed but not signed in. Run:  tailscale up\n"
                    "Sign in with the same account on the phone "
                    "(https://tailscale.com/download).")
        if self.state == "stopped":
            return ("Tailscale is installed and signed in, but stopped. Run:  "
                    "tailscale up\nPhone app: https://tailscale.com/download")
        if self.state == "running":
            return ("On the same tailnet, this address works from anywhere — "
                    "cellular, another Wi-Fi. Make sure the phone is signed into "
                    "the same account.")
        return ("Could not read Tailscale's state here. If it is running, its "
                "100.x address is listed above. Phone app: "
                "https://tailscale.com/download")


def _status_json(timeout: float = 5.0) -> dict | None:
    binary = cli_path()
    if binary is None:
        return None
    try:
        proc = subprocess.run([binary, "status", "--json"],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    # A stopped or logged-out tailscale still prints usable JSON and exits
    # non-zero, so the output is worth parsing either way.
    for stream in (proc.stdout, proc.stderr):
        text = (stream or "").strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except ValueError:
                continue
    return None


def status() -> Tailnet:
    """Read Tailscale's state. Never raises: this is a hint, not a dependency."""
    data = _status_json()
    if data is None:
        return Tailnet(state="absent" if cli_path() is None else "unknown")

    backend = str(data.get("BackendState") or "").lower()
    state = {"running": "running", "stopped": "stopped",
             "needslogin": "needs-login", "nostate": "stopped",
             "starting": "stopped"}.get(backend.replace("-", ""), "unknown")

    self_node = data.get("Self") or {}
    address = None
    for candidate in self_node.get("TailscaleIPs") or []:
        try:
            if ipaddress.ip_address(candidate) in CGNAT:
                address = candidate
                break
        except ValueError:
            continue

    name = (self_node.get("DNSName") or "").strip().rstrip(".") or None
    if state == "running" and address is None:
        # Backend says running but this node has no usable v4 address yet.
        state = "stopped"
    return Tailnet(state=state, address=address, name=name)


def advice(has_tailnet_address: bool) -> str:
    """What to tell someone whose daemon is only reachable on the LAN.

    `has_tailnet_address` is what the address scan already found, so a machine
    that plainly has a 100.x address needs none of this.
    """
    if has_tailnet_address:
        return ""
    state = status()
    return ("On the same Wi-Fi only — a 192.168.x.y address stops resolving the "
            "moment the phone leaves this router.\n" + state.next_step())
