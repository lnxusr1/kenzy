"""Node system metrics (cpu/ram/disk/temp) → dashboard fleet card."""

from __future__ import annotations

import json

from kenzy import protocol
from kenzy.node import sysinfo


def test_protocol_metrics_message():
    msg = json.loads(protocol.metrics(cpu=12.5, ram=40.0, disk=61.2, temp=47.8))
    assert msg == {
        "type": protocol.MSG_METRICS,
        "cpu": 12.5,
        "ram": 40.0,
        "disk": 61.2,
        "temp": 47.8,
    }
    assert json.loads(protocol.metrics())["cpu"] is None  # None-safe


def test_cpu_percent_from_proc_stat(tmp_path):
    stat = tmp_path / "stat"
    # busy = user+nice+system(+irq…), idle = idle+iowait
    stat.write_text("cpu  100 0 100 700 100 0 0 0 0 0\n")
    s1 = sysinfo.read_cpu_sample(str(stat))
    assert s1 == (200, 1000)
    stat.write_text("cpu  200 0 200 1000 200 0 0 0 0 0\n")
    s2 = sysinfo.read_cpu_sample(str(stat))
    assert s2 == (400, 1600)
    assert sysinfo.cpu_percent(s1, s2) == round(100 * 200 / 600, 1)
    assert sysinfo.cpu_percent(None, s2) is None  # first tick: no delta yet
    assert sysinfo.cpu_percent(s2, s2) is None  # zero interval


def test_mem_percent_from_meminfo(tmp_path):
    mi = tmp_path / "meminfo"
    mi.write_text("MemTotal:  8000000 kB\nMemFree:  1000000 kB\nMemAvailable:  6000000 kB\n")
    assert sysinfo.mem_percent(str(mi)) == 25.0
    assert sysinfo.mem_percent(str(tmp_path / "missing")) is None


def test_temp_c_picks_hottest_zone_and_ignores_garbage(tmp_path):
    (tmp_path / "thermal_zone0").mkdir()
    (tmp_path / "thermal_zone1").mkdir()
    (tmp_path / "thermal_zone2").mkdir()
    (tmp_path / "thermal_zone0" / "temp").write_text("47500\n")
    (tmp_path / "thermal_zone1" / "temp").write_text("62000\n")
    (tmp_path / "thermal_zone2" / "temp").write_text("garbage\n")
    assert sysinfo.temp_c(str(tmp_path / "thermal_zone*" / "temp")) == 62.0
    assert sysinfo.temp_c(str(tmp_path / "nope*" / "temp")) is None


def test_disk_percent_real_filesystem():
    pct = sysinfo.disk_percent("/")
    assert pct is None or 0.0 <= pct <= 100.0


def test_sampler_shape():
    m = sysinfo.MetricsSampler().sample()
    assert set(m) == {"cpu", "ram", "disk", "temp"}


def test_session_metrics_surfaced_in_dashboard_state(tmp_path, monkeypatch):
    from kenzy.server.dashboard import Dashboard, DashboardConfig
    from kenzy.server.server import AudioServer, NodeSession

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = AudioServer({})
    d = Dashboard(s, {}, DashboardConfig(enabled=True))

    class _WS:
        remote_address = ("10.0.0.5", 1234)

    sess = NodeSession(ws=_WS(), node_id="n1", room_id="office")
    sess.metrics = {"cpu": 7.5, "ram": 33.1, "disk": 58.0, "temp": 51.2}
    s._nodes["n1"] = sess
    (state,) = d._nodes_state()
    assert state["metrics"] == {"cpu": 7.5, "ram": 33.1, "disk": 58.0, "temp": 51.2}

    # Metrics listeners are separate from state listeners (MQTT stays quiet).
    fired: list[bool] = []
    s.add_metrics_listener(lambda: fired.append(True))
    s._notify_metrics()
    assert fired == [True]
