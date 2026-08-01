"""The node's route home: last-known-good server cache, bounded mDNS discovery,
the reconnect watchdog, and telling the truth on wake.

All of it exists because of one incident. A node sat orphaned for 47 hours —
answering its wake word with a cheerful "I'm listening" chime the whole time —
while making zero contact with a server that was up, reachable, and advertising
normally. Nothing on either side said a word. Each test here is a piece of that
failure, made cheap to reproduce.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from kenzy.node import client as client_mod
from kenzy.node.client import NodeClient


def _node(tmp_path, monkeypatch, **cfg):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    base = {"node_id": "n1", "room_id": "Office", "discovery": {"enabled": True}}
    base.update(cfg)
    return NodeClient(base)


# ---------------------------------------------------------------------------
# The URL cache — mDNS must not be the only way back
# ---------------------------------------------------------------------------


async def test_cached_url_is_preferred_over_discovery(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)
    browsed = False

    async def _browse():
        nonlocal browsed
        browsed = True
        return "ws://discovered:8765"

    monkeypatch.setattr(node, "_discover_once", _browse)
    node._connect_url = "wss://10.0.0.5:8765"
    node._mark_registered()

    assert await node._resolve_server_url() == "wss://10.0.0.5:8765"
    assert not browsed, "asked the network for an address it already knew"


async def test_cache_survives_a_restart(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)
    node._connect_url = "wss://10.0.0.5:8765"
    node._mark_registered()
    # A fresh process on the same host already knows where to go, so rebooting
    # into a broken multicast path is no longer a dead end.
    assert _node(tmp_path, monkeypatch)._cached_server_url == "wss://10.0.0.5:8765"


async def test_only_registration_populates_the_cache(tmp_path, monkeypatch):
    """Opening a socket proves nothing — a refused join gets one too. Only a
    server that answered our hello is worth remembering."""
    node = _node(tmp_path, monkeypatch)
    node._connect_url = "wss://impostor:8765"
    assert node._cached_server_url is None


async def test_stale_cache_falls_back_to_discovery(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)
    node._connect_url = "wss://10.0.0.5:8765"
    node._mark_registered()
    node._cache_stale = True

    async def _browse():
        return "ws://moved:8765"

    monkeypatch.setattr(node, "_discover_once", _browse)
    # A server that genuinely moved must still be findable.
    assert await node._resolve_server_url() == "ws://moved:8765"


async def test_unanswered_browse_retries_the_known_address(tmp_path, monkeypatch):
    """Nothing answered, but we know where the server was: a stale address beats
    no address at all, and the cache re-arms so the next failure browses again."""
    node = _node(tmp_path, monkeypatch)
    node._connect_url = "wss://10.0.0.5:8765"
    node._mark_registered()
    node._cache_stale = True

    async def _nothing():
        return None

    monkeypatch.setattr(node, "_discover_once", _nothing)
    assert await node._resolve_server_url() == "wss://10.0.0.5:8765"
    assert node._cache_stale is False


async def test_no_cache_and_no_answer_raises(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)

    async def _nothing():
        return None

    monkeypatch.setattr(node, "_discover_once", _nothing)
    with pytest.raises(OSError):
        await node._resolve_server_url()


async def test_explicit_server_url_still_wins(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch, server_url="ws://configured:8765")
    node._cached_server_url = "ws://remembered:8765"
    assert await node._resolve_server_url() == "ws://configured:8765"


# ---------------------------------------------------------------------------
# Bounded discovery — a hung browse must not park the reconnect loop
# ---------------------------------------------------------------------------


async def test_hung_browse_times_out_and_releases_its_worker(tmp_path, monkeypatch):
    """The 47-hour failure: zeroconf never returns, the await never completes, and
    the reconnect loop stops existing — silently, because nothing logs a retry
    that never happens."""
    node = _node(tmp_path, monkeypatch)
    monkeypatch.setattr(client_mod, "_DISCOVERY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(client_mod, "_DISCOVERY_GRACE_S", 0.05)
    released = threading.Event()

    def _hang(timeout, cancel_event):
        cancel_event.wait(5.0)  # ignores its own deadline, like a wedged teardown
        released.set()
        return None

    monkeypatch.setattr("kenzy.discovery.discover_server", _hang)

    assert await asyncio.wait_for(node._discover_once(), timeout=2.0) is None
    assert released.wait(2.0), "wedged worker was left to leak instead of unwinding"


async def test_browse_does_not_use_the_shared_executor(tmp_path, monkeypatch):
    """_audio_loop submits two run_in_executor calls per frame to the default
    pool. If discovery shared it, a few wedged browses would starve the audio
    path and take the wake word down with the connection."""
    node = _node(tmp_path, monkeypatch)
    monkeypatch.setattr(client_mod, "_DISCOVERY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(client_mod, "_DISCOVERY_GRACE_S", 0.05)
    names: list[str] = []

    def _record(timeout, cancel_event):
        names.append(threading.current_thread().name)
        return "ws://x:8765"

    monkeypatch.setattr("kenzy.discovery.discover_server", _record)
    await node._discover_once()
    assert names and names[0].startswith("mdns-discover")


async def test_discovery_error_is_reported_not_swallowed(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)

    def _boom(timeout, cancel_event):
        raise OSError("no multicast route")

    monkeypatch.setattr("kenzy.discovery.discover_server", _boom)
    with pytest.raises(OSError):
        await node._discover_once()


async def test_shutdown_releases_an_in_flight_browse(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)
    monkeypatch.setattr(client_mod, "_DISCOVERY_TIMEOUT_S", 5.0)
    started = threading.Event()

    def _wait(timeout, cancel_event):
        started.set()
        cancel_event.wait(5.0)
        return None

    monkeypatch.setattr("kenzy.discovery.discover_server", _wait)
    task = asyncio.create_task(node._discover_once())
    await asyncio.to_thread(started.wait, 2.0)
    for ev in list(node._discovery_cancels):
        ev.set()  # what _request_stop does on SIGINT/SIGTERM
    assert await asyncio.wait_for(task, timeout=2.0) is None


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------


async def _run_watchdog(node, seconds=0.2):
    """Let the watchdog tick for a moment. It returns of its own accord once it
    decides to re-exec; otherwise we stop waiting."""
    try:
        await asyncio.wait_for(node._watchdog_loop(), timeout=seconds)
    except TimeoutError:
        pass


async def test_watchdog_reexecs_a_wedged_loop(tmp_path, monkeypatch):
    """A reconnect loop that stopped iterating cannot recover itself. Only a new
    process can, which is why the node needs its own smoke alarm."""
    node = _node(tmp_path, monkeypatch, watchdog={"wedge_minutes": 0.0001})
    reasons: list[str] = []
    monkeypatch.setattr(node, "_reexec", lambda why: reasons.append(why))
    monkeypatch.setattr(client_mod, "_WATCHDOG_TICK_S", 0.001)

    node._loop_alive_at = 0.0  # never turned
    node._registered = False
    await _run_watchdog(node)
    assert reasons and "not run" in reasons[0]


async def test_watchdog_is_quiet_while_registered(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch, watchdog={"wedge_minutes": 0.0001})
    fired: list[str] = []
    monkeypatch.setattr(node, "_reexec", lambda why: fired.append(why))
    monkeypatch.setattr(client_mod, "_WATCHDOG_TICK_S", 0.001)

    node._registered = True
    await _run_watchdog(node)
    assert not fired


async def test_turning_loop_is_not_reexeced_for_a_server_outage(tmp_path, monkeypatch):
    """A loop that is retrying and simply not being answered is an ordinary
    outage. Re-execing every room in the house over it would be worse than the
    outage itself."""
    node = _node(
        tmp_path, monkeypatch, watchdog={"wedge_minutes": 60, "reexec_minutes": 60}
    )
    fired: list[str] = []
    monkeypatch.setattr(node, "_reexec", lambda why: fired.append(why))
    monkeypatch.setattr(client_mod, "_WATCHDOG_TICK_S", 0.001)

    node._loop_alive_at = time.monotonic()  # still turning
    node._registered = False
    await _run_watchdog(node)
    assert not fired


async def test_a_long_healthy_run_is_not_counted_as_downtime(tmp_path, monkeypatch):
    """The master-bedroom node re-execed 8 SECONDS after a blip, reporting "no
    server connection for 412m" — because downtime was measured from the last
    *join*, so a 7-hour healthy run read as 7 hours overdue. Left alone, any node
    connected longer than reexec_minutes re-execs on the first tick of any drop,
    which is the fleet-wide flap the threshold exists to prevent.
    """
    node = _node(
        tmp_path, monkeypatch, watchdog={"wedge_minutes": 60, "reexec_minutes": 30}
    )
    fired: list[str] = []
    monkeypatch.setattr(node, "_reexec", lambda why: fired.append(why))
    monkeypatch.setattr(client_mod, "_WATCHDOG_TICK_S", 0.001)

    now = time.monotonic()
    node._loop_alive_at = now  # still turning
    node._registered_at = now - 7 * 3600  # joined seven hours ago…
    node._registered = True
    node._mark_disconnected()  # …and dropped just now
    await _run_watchdog(node)
    assert not fired


async def test_watchdog_still_reexecs_a_genuinely_long_outage(tmp_path, monkeypatch):
    """The other half: measuring from the right instant must not defang the
    threshold. Past reexec_minutes of real downtime, a fresh process is still the
    move."""
    node = _node(
        tmp_path, monkeypatch, watchdog={"wedge_minutes": 60, "reexec_minutes": 30}
    )
    fired: list[str] = []
    monkeypatch.setattr(node, "_reexec", lambda why: fired.append(why))
    monkeypatch.setattr(client_mod, "_WATCHDOG_TICK_S", 0.001)

    now = time.monotonic()
    node._loop_alive_at = now
    node._registered = False
    node._disconnected_at = now - 31 * 60
    await _run_watchdog(node)
    assert fired and "no server connection" in fired[0]


async def test_registering_ends_the_outage(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)
    node._disconnected_at = time.monotonic() - 600
    node._mark_registered()
    assert node._disconnected_at == 0.0


async def test_a_refused_join_does_not_restart_the_outage_clock(tmp_path, monkeypatch):
    """Downtime is counted from when we LOST the server, not from the last failed
    attempt to reach it — otherwise a node retrying every 60 s never accumulates
    any downtime and the re-exec threshold can never be reached."""
    node = _node(tmp_path, monkeypatch)
    dropped_at = time.monotonic() - 600
    node._disconnected_at = dropped_at
    node._registered = False  # this attempt never joined

    node._mark_disconnected()
    assert node._disconnected_at == dropped_at


async def test_watchdog_can_be_disabled(tmp_path, monkeypatch):
    assert _node(tmp_path, monkeypatch, watchdog={"enabled": False})._watchdog_enabled is False


async def test_watchdog_thresholds_tune_live(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch)
    node._apply_pulled_config({"watchdog": {"warn_minutes": 2, "reexec_minutes": 0}})
    assert node._watchdog_warn_s == 120.0
    assert node._watchdog_reexec_s == 0.0


# ---------------------------------------------------------------------------
# Honest cues and honest logs
# ---------------------------------------------------------------------------


class _Player:
    def __init__(self) -> None:
        self.chimed = False
        self.pcm: list = []

    def play(self) -> None:
        self.chimed = True

    def play_pcm(self, audio, **kw) -> None:
        self.pcm.append(audio)

    def abort(self) -> None:
        pass


async def test_wake_word_does_not_chime_when_orphaned(tmp_path, monkeypatch):
    """The ready chime means "I'm listening". Playing it with no server is a lie
    — and it is exactly what made a two-day outage look like a working node from
    inside the room."""
    node = _node(tmp_path, monkeypatch)
    node._player = _Player()
    node._ws = None

    await node._begin_streaming("s1")

    assert node._player.chimed is False
    assert node._state == client_mod._STATE_IDLE
    assert node._session_id is None


async def test_configured_offline_cue_plays_instead(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch, sound_offline="disconnect.wav")
    node._player = _Player()
    node._ws = None
    node._offline_audio = object()

    await node._begin_streaming("s1")

    assert node._player.chimed is False
    assert node._player.pcm == [node._offline_audio]


async def test_connected_node_still_chimes(tmp_path, monkeypatch):
    class _WS:
        async def send(self, m):
            pass

    node = _node(tmp_path, monkeypatch)
    node._player = _Player()
    node._ws = _WS()

    await node._begin_streaming("s1")

    assert node._player.chimed is True
    assert node._state == client_mod._STATE_STREAMING


def test_close_reason_is_readable():
    """A join refused for a stale clock and an ordinary network drop are
    indistinguishable unless the server's own words survive the exception."""

    class _Frame:
        code = 1008
        reason = "invalid join token (stale timestamp)"

    class _Closed(Exception):
        rcvd = _Frame()
        sent = None

    described = client_mod._describe_close(_Closed())
    assert "1008" in described
    assert "stale timestamp" in described
    assert described.startswith("server")


def test_ago_reads_naturally():
    now = time.monotonic()
    assert client_mod._ago(0.0) == "never"
    assert client_mod._ago(now).endswith("s ago")
    assert client_mod._ago(now - 600).endswith("m ago")
    assert client_mod._ago(now - 7200).endswith("h ago")
