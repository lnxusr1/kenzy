"""LAN service discovery for Kenzy via mDNS / DNS-SD (zeroconf).

The server advertises a ``_kenzy._tcp.local.`` service; nodes browse for it and
resolve a WebSocket URL, so a node needs no hardcoded ``server_url``. Discovery
is optional in both directions: an explicit ``server_url`` short-circuits
browsing, and if ``zeroconf`` is unavailable both sides degrade to a logged
no-op rather than failing.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

log = logging.getLogger(__name__)

#: DNS-SD service type advertised by the server and browsed for by nodes.
SERVICE_TYPE = "_kenzy._tcp.local."
DEFAULT_INSTANCE = "kenzy-server"


def _primary_ip() -> str:
    """Best-effort LAN IP of this host — the address peers should connect to.

    Uses a connected UDP socket so the OS picks the egress interface; no packet
    is actually sent. Falls back to loopback if there's no route.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class ServerAdvertiser:
    """Advertises the Kenzy server over mDNS. A no-op if zeroconf is missing."""

    def __init__(
        self,
        port: int,
        *,
        host: str | None = None,
        instance: str = DEFAULT_INSTANCE,
        properties: dict[str, str] | None = None,
    ) -> None:
        self._port = port
        # A bind address of 0.0.0.0/:: is not routable; advertise the real LAN IP.
        self._ip = host if host and host not in ("0.0.0.0", "::") else _primary_ip()
        self._instance = instance
        self._properties = properties or {}
        self._zc: Any = None
        self._info: Any = None

    def start(self) -> bool:
        """Register the service. Returns True on success, False if unavailable."""
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.warning("zeroconf not installed — server will not advertise over mDNS")
            return False
        try:
            self._info = ServiceInfo(
                SERVICE_TYPE,
                f"{self._instance}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(self._ip)],
                port=self._port,
                properties={k: str(v) for k, v in self._properties.items()},
                server=f"{socket.gethostname()}.local.",
            )
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            log.info("Advertising %s at %s:%d via mDNS", SERVICE_TYPE, self._ip, self._port)
            return True
        except Exception as exc:  # pragma: no cover - environment/network dependent
            log.warning("mDNS advertise failed: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        try:
            if self._zc is not None and self._info is not None:
                self._zc.unregister_service(self._info)
        except Exception:  # pragma: no cover
            pass
        finally:
            if self._zc is not None:
                try:
                    self._zc.close()
                except Exception:  # pragma: no cover
                    pass
            self._zc = None
            self._info = None


def discover_server(timeout: float = 5.0) -> str | None:
    """Browse for a Kenzy server; return a ``ws://host:port`` URL or ``None``.

    Blocking — call it from a thread (e.g. ``asyncio.to_thread``) inside async
    code. Returns the first responder; IPv4 is preferred.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        log.warning("zeroconf not installed — cannot discover the server over mDNS")
        return None

    found: list[str] = []
    done = threading.Event()

    class _Listener(ServiceListener):
        def _resolve(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=max(int(timeout * 1000), 1000))
            if info is None:
                return
            addrs = info.parsed_addresses()
            # Prefer IPv4 (no colon) for the widest compatibility.
            ipv4 = [a for a in addrs if ":" not in a]
            candidates = ipv4 or addrs
            if candidates:
                found.append(f"ws://{candidates[0]}:{info.port}")
                done.set()

        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            self._resolve(zc, type_, name)

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            self._resolve(zc, type_, name)

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE_TYPE, _Listener())
        done.wait(timeout)
    finally:
        zc.close()

    return found[0] if found else None
