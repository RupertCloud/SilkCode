"""The connection monitor behind the GUI's Connections panel.

The daemon's token is worth a shell on the machine it runs on. These cover
the record of who used it and who was turned away — and, as much, what that
record deliberately refuses to keep.
"""

from __future__ import annotations

import threading

import pytest

from silkcode.connections import (ACTIVE_WINDOW, MAX_CLIENTS, MAX_DENIED,
                                  ConnectionMonitor)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def monitor(clock):
    return ConnectionMonitor(now=clock)


# ---- what it records --------------------------------------------------------

def test_a_client_is_counted_and_timestamped(monitor, clock):
    monitor.record("10.0.0.5", "/", "phone")
    monitor.record("10.0.0.5", "/api/state", "phone")
    clock.advance(5)                       # idle is measured since the last request
    client = monitor.snapshot()["clients"][0]
    assert client["address"] == "10.0.0.5"
    assert client["requests"] == 2
    assert client["denied"] == 0
    assert client["last_path"] == "/api/state"
    assert client["idle"] == 5


def test_the_most_recently_seen_client_is_listed_first(monitor, clock):
    monitor.record("10.0.0.1", "/")
    clock.advance(10)
    monitor.record("10.0.0.2", "/")
    assert [c["address"] for c in monitor.snapshot()["clients"]] == ["10.0.0.2", "10.0.0.1"]


def test_a_client_goes_quiet_rather_than_disappearing(monitor, clock):
    """An operator wants "this address was here an hour ago" to survive; it
    just should not be counted as connected now."""
    monitor.record("10.0.0.5", "/")
    clock.advance(ACTIVE_WINDOW + 1)
    snap = monitor.snapshot()
    assert snap["active"] == 0
    assert snap["clients"][0]["active"] is False
    assert snap["clients"][0]["requests"] == 1


def test_live_streams_are_counted_up_and_down(monitor):
    monitor.stream_opened()
    monitor.stream_opened()
    assert monitor.snapshot()["streams"] == 2
    monitor.stream_closed()
    assert monitor.snapshot()["streams"] == 1


def test_closing_more_streams_than_opened_cannot_go_negative(monitor):
    """A stream can end through several paths at once (client gone, server
    shutting down); a negative count would render as nonsense."""
    monitor.stream_closed()
    monitor.stream_closed()
    assert monitor.snapshot()["streams"] == 0


# ---- refusals ---------------------------------------------------------------

def test_a_refusal_is_recorded_with_its_reason(monitor):
    denial = monitor.record("10.0.0.9", "/api/state", "curl/8",
                            allowed=False, reason="no token presented")
    assert denial["address"] == "10.0.0.9"
    assert denial["reason"] == "no token presented"
    assert denial["count"] == 1
    snap = monitor.snapshot()
    assert snap["denied_total"] == 1
    assert snap["denied_recent"][0]["reason"] == "no token presented"


def test_an_allowed_request_returns_nothing_to_warn_about(monitor):
    assert monitor.record("10.0.0.5", "/") is None


def test_repeated_refusals_from_one_source_are_counted(monitor):
    for _ in range(4):
        denial = monitor.record("10.0.0.9", "/", allowed=False, reason="bad token")
    assert denial["count"] == 4
    assert monitor.snapshot()["clients"][0]["denied"] == 4


def test_refusals_are_newest_first(monitor):
    monitor.record("10.0.0.1", "/first", allowed=False, reason="a")
    monitor.record("10.0.0.2", "/second", allowed=False, reason="b")
    assert [d["path"] for d in monitor.snapshot()["denied_recent"]] == ["/second", "/first"]


# ---- what it refuses to keep ------------------------------------------------

def test_no_presented_token_is_ever_stored():
    """A wrong token is usually a real credential — the right one for another
    daemon, or one with a typo. The record must not become a place credentials
    accumulate, so record() is not even given the token to store."""
    import inspect
    signature = inspect.signature(ConnectionMonitor.record)
    assert "token" not in signature.parameters


def test_a_hostile_user_agent_is_flattened_to_one_line(monitor):
    """The agent string is chosen by the caller. Newlines in it would let a
    scanner forge extra lines in anything that prints the record."""
    monitor.record("10.0.0.9", "/", "evil\nrefused 1.2.3.4 (once): fake", allowed=False)
    stored = monitor.snapshot()["denied_recent"][0]["agent"]
    assert "\n" not in stored
    assert stored.startswith("evil refused")


def test_a_giant_user_agent_is_truncated(monitor):
    monitor.record("10.0.0.9", "/", "A" * 5000)
    assert len(monitor.snapshot()["clients"][0]["agent"]) <= 120


# ---- bounded under load -----------------------------------------------------

def test_the_refusal_ring_is_bounded(monitor):
    for i in range(MAX_DENIED * 2):
        monitor.record("10.0.0.9", f"/{i}", allowed=False, reason="bad token")
    snap = monitor.snapshot()
    assert len(snap["denied_recent"]) == MAX_DENIED
    # the running total still reflects everything that happened
    assert snap["denied_total"] == MAX_DENIED * 2


def test_many_distinct_sources_cannot_grow_memory_without_bound(monitor, clock):
    """A scan from a whole subnet is one source per address; the table has to
    stop somewhere, and it drops the least recently seen."""
    for i in range(MAX_CLIENTS + 50):
        clock.advance(1)
        monitor.record(f"10.0.{i // 256}.{i % 256}", "/", allowed=False, reason="x")
    clients = monitor.snapshot()["clients"]
    assert len(clients) <= MAX_CLIENTS
    # the survivors are the recent ones
    assert clients[0]["address"] == f"10.0.{(MAX_CLIENTS + 49) // 256}.{(MAX_CLIENTS + 49) % 256}"


def test_concurrent_writers_do_not_lose_records():
    """Every request handler thread writes here."""
    monitor = ConnectionMonitor()

    def hammer(address):
        for _ in range(200):
            monitor.record(address, "/api/state")

    threads = [threading.Thread(target=hammer, args=(f"10.0.0.{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = monitor.snapshot()
    assert len(snap["clients"]) == 8
    assert sum(c["requests"] for c in snap["clients"]) == 1600


def test_a_snapshot_cannot_be_used_to_mutate_the_monitor(monitor):
    monitor.record("10.0.0.5", "/")
    snap = monitor.snapshot()
    snap["clients"][0]["requests"] = 9999
    assert monitor.snapshot()["clients"][0]["requests"] == 1
