"""Who is talking to the GUI daemon, from where, and who is being turned away.

The daemon drives an agent that reads and writes files, runs commands and holds
API keys. Reachable beyond loopback, its access token is worth a shell on the
machine — so the question "who is connected right now, and is anyone trying to
get in?" needs an answer that is not "read the terminal and hope".

Nothing here is a security control; the token and the origin checks in
gui/server.py are. This is the record of what those controls did, which is what
turns a silent 401 into something an operator can act on.

Clients are keyed by address, so a row is a source address rather than a
device: two machines behind one NAT collapse into one row, and the client
string shown is whichever spoke most recently. On a tailnet or a home LAN -
where this daemon is meant to live - an address is a device, and the honest
alternative (keying on address plus a client-chosen header) would let anyone
manufacture as many rows as they liked.

Two things are deliberately *not* recorded:

  * any presented token, right or wrong. A wrong one is often a real token
    with a typo, or the right token for a different daemon; storing it would
    put a live credential in a buffer that the GUI then renders.
  * anything beyond a capped, single-line User-Agent. It is attacker-chosen
    text, so it is length-limited here and escaped at the point of render.
"""

from __future__ import annotations

import threading
import time
from collections import deque

# A client is "active" while it has been seen this recently. Long enough that a
# phone whose screen went dark still counts, short enough that a laptop closed
# an hour ago does not.
ACTIVE_WINDOW = 90.0

MAX_AGENT = 120          # User-Agent is attacker-chosen; cap it before storing
MAX_DENIED = 100         # a bounded ring, so a flood cannot grow memory
MAX_CLIENTS = 200        # ditto for distinct sources


class ConnectionMonitor:
    """A thread-safe record of requests, refusals and live event streams.

    Every request handler thread writes here, and the API reads a snapshot, so
    each operation takes the lock and copies rather than handing out the
    internal dicts.
    """

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._clients: dict[str, dict] = {}
        self._denied: deque[dict] = deque(maxlen=MAX_DENIED)
        self._streams = 0
        self._denied_total = 0

    # ---- recording ---------------------------------------------------------

    def record(self, address: str, path: str, agent: str = "",
               allowed: bool = True, reason: str = "") -> dict | None:
        """Log one request. Returns the denial when this one was refused.

        The return value is what lets the caller decide whether to say
        something on the terminal, without the monitor needing to know how
        the daemon talks to its operator.
        """
        now = self._now()
        agent = _one_line(agent)[:MAX_AGENT]
        with self._lock:
            client = self._clients.get(address)
            if client is None:
                if len(self._clients) >= MAX_CLIENTS:
                    self._evict_oldest_locked()
                client = self._clients[address] = {
                    "address": address, "first_seen": now, "last_seen": now,
                    "requests": 0, "denied": 0, "agent": agent, "last_path": path,
                }
            client["last_seen"] = now
            client["last_path"] = path
            client["requests"] += 1
            if agent:
                client["agent"] = agent
            if allowed:
                return None
            client["denied"] += 1
            self._denied_total += 1
            event = {"at": now, "address": address, "path": path,
                     "reason": reason, "agent": agent}
            self._denied.append(event)
            # How many refusals this source has produced, so a caller can be
            # loud once and quiet afterwards rather than per request.
            return dict(event, count=client["denied"])

    def _evict_oldest_locked(self) -> None:
        oldest = min(self._clients, key=lambda a: self._clients[a]["last_seen"])
        del self._clients[oldest]

    def stream_opened(self) -> None:
        with self._lock:
            self._streams += 1

    def stream_closed(self) -> None:
        with self._lock:
            self._streams = max(0, self._streams - 1)

    # ---- reading -----------------------------------------------------------

    def snapshot(self) -> dict:
        """Everything the monitor knows, safe to serialise and render."""
        now = self._now()
        with self._lock:
            clients = [
                dict(c, active=(now - c["last_seen"]) <= ACTIVE_WINDOW,
                     idle=round(now - c["last_seen"], 1))
                for c in self._clients.values()
            ]
            denied = [dict(d) for d in self._denied]
            streams = self._streams
            denied_total = self._denied_total
        clients.sort(key=lambda c: c["last_seen"], reverse=True)
        denied.reverse()          # most recent first, the way it is read
        return {
            "now": now,
            "clients": clients,
            "active": sum(1 for c in clients if c["active"]),
            "streams": streams,
            "denied_recent": denied,
            "denied_total": denied_total,
        }


def _one_line(text: str) -> str:
    """Collapse whitespace so a crafted header cannot forge extra log lines."""
    return " ".join(str(text).split())
