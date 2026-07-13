"""Tests for mDNS service discovery (kenzy.discovery).

The round-trip needs a routable network stack; where multicast isn't available
(some CI sandboxes) the relevant test skips rather than fails.
"""

from __future__ import annotations

import pytest

from kenzy import discovery


def test_primary_ip_returns_ipv4():
    ip = discovery._primary_ip()
    assert isinstance(ip, str)
    assert ip.count(".") == 3


def test_advertiser_init_resolves_unroutable_bind():
    # A 0.0.0.0 bind must be replaced with a concrete, routable address.
    adv = discovery.ServerAdvertiser(port=1234, host="0.0.0.0")
    assert adv._ip not in ("0.0.0.0", "::")


def test_advertise_discover_roundtrip():
    adv = discovery.ServerAdvertiser(port=18765, instance="kenzy-pytest")
    if not adv.start():
        pytest.skip("zeroconf unavailable")
    try:
        url = discovery.discover_server(timeout=5.0)
    finally:
        adv.stop()
    if url is None:
        pytest.skip("mDNS not routable in this environment")
    # A real kenzy-server elsewhere on the LAN could answer first, so only the
    # shape is asserted; the round-trip itself proves browse + resolve work.
    # (Since 3.9.0 a TLS-enabled server legitimately advertises wss://.)
    assert url.startswith(("ws://", "wss://"))
