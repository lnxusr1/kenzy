"""Lightweight system metrics for the node's dashboard card — stdlib only.

Reads Linux procfs/sysfs directly (no psutil): /proc/stat for CPU (delta between
samples), /proc/meminfo for RAM, shutil.disk_usage for disk, and
/sys/class/thermal for temperature. Every reader is None-safe on non-Linux or
odd hardware — a missing metric is reported as absent, never an error.
"""

from __future__ import annotations

import glob
import shutil
from typing import Any

_PROC_STAT = "/proc/stat"
_PROC_MEMINFO = "/proc/meminfo"
_THERMAL_GLOB = "/sys/class/thermal/thermal_zone*/temp"


def read_cpu_sample(path: str = _PROC_STAT) -> tuple[int, int] | None:
    """Return (busy, total) jiffies from the aggregate cpu line, or None."""
    try:
        with open(path) as f:
            line = f.readline()
        parts = line.split()
        if parts[0] != "cpu":
            return None
        vals = [int(v) for v in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
        return total - idle, total
    except Exception:
        return None


def cpu_percent(prev: tuple[int, int] | None, cur: tuple[int, int] | None) -> float | None:
    """CPU utilisation between two samples (None until two samples exist)."""
    if prev is None or cur is None:
        return None
    dbusy, dtotal = cur[0] - prev[0], cur[1] - prev[1]
    if dtotal <= 0:
        return None
    return round(100.0 * dbusy / dtotal, 1)


def mem_percent(path: str = _PROC_MEMINFO) -> float | None:
    """RAM in use, as a percentage (MemTotal vs MemAvailable)."""
    try:
        total = avail = None
        with open(path) as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                if total is not None and avail is not None:
                    break
        if not total or avail is None:
            return None
        return round(100.0 * (1 - avail / total), 1)
    except Exception:
        return None


def disk_percent(path: str = "/") -> float | None:
    """Used space on the filesystem holding ``path``, as a percentage."""
    try:
        usage = shutil.disk_usage(path)
        if usage.total <= 0:
            return None
        return round(100.0 * usage.used / usage.total, 1)
    except Exception:
        return None


def temp_c(pattern: str = _THERMAL_GLOB) -> float | None:
    """Hottest thermal zone in °C (the interesting one), or None."""
    best: float | None = None
    try:
        for p in glob.glob(pattern):
            try:
                with open(p) as f:
                    val = int(f.read().strip()) / 1000.0
                if -20.0 < val < 150.0 and (best is None or val > best):
                    best = val
            except Exception:
                continue
    except Exception:
        return None
    return round(best, 1) if best is not None else None


class MetricsSampler:
    """Stateful sampler: keeps the previous CPU sample so each call yields a
    utilisation over the interval since the last call."""

    def __init__(self) -> None:
        self._prev_cpu = read_cpu_sample()

    def sample(self) -> dict[str, Any]:
        cur = read_cpu_sample()
        cpu = cpu_percent(self._prev_cpu, cur)
        self._prev_cpu = cur
        return {
            "cpu": cpu,
            "ram": mem_percent(),
            "disk": disk_percent(),
            "temp": temp_c(),
        }
