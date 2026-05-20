"""Container resource stats from /sys/fs/cgroup and /proc.

Replaces the `docker stats` / `docker top` subprocess path the UI used to take.
Reads only the container's own cgroup + /proc view, so the same code works on:

  - Docker compose locally (cgroup v1 today, but v2 if the host switches over)
  - EKS dev05 (cgroup v2; verified 2026-05-19)
  - any other CRI-runtime cluster, kind, podman, etc.

No docker socket, no kubectl, no metrics-server dependency.

The exposed counters are deliberately cumulative + timestamped. CPU% requires
two samples; the caller (test_ui.py) computes the delta between consecutive
fetches. This keeps the endpoints stateless and lets the UI control the cadence.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

_CGROUP_V2_MARKER = Path("/sys/fs/cgroup/cgroup.controllers")
_CPU_V2 = Path("/sys/fs/cgroup/cpu.stat")
_MEM_CURRENT_V2 = Path("/sys/fs/cgroup/memory.current")
_MEM_MAX_V2 = Path("/sys/fs/cgroup/memory.max")
_PIDS_CURRENT_V2 = Path("/sys/fs/cgroup/pids.current")

_CPUACCT_V1 = Path("/sys/fs/cgroup/cpuacct/cpuacct.usage")
_MEM_USAGE_V1 = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
_MEM_LIMIT_V1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_PIDS_CURRENT_V1 = Path("/sys/fs/cgroup/pids/pids.current")


def _is_cgroup_v2() -> bool:
    return _CGROUP_V2_MARKER.exists()


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return default


def read_container_stats() -> dict[str, Any]:
    """Snapshot of the container's CPU / memory / PID counters.

    Returns a dict with cumulative counters + a wall-clock timestamp:

        cpu_usage_usec   cumulative CPU time used since the container started
                         (microseconds). Caller subtracts two samples and
                         divides by the elapsed wall time to get CPU%.
        mem_bytes        current memory in use (RSS + pagecache, same shape
                         as `docker stats`'s "Mem Usage")
        mem_max_bytes    container memory limit; 0 means unlimited
        pids_current     processes currently inside the container
        sampled_at       Unix time of the snapshot (float seconds)
    """
    if _is_cgroup_v2():
        cpu_usage_usec = 0
        if _CPU_V2.exists():
            for line in _CPU_V2.read_text().splitlines():
                if line.startswith("usage_usec"):
                    parts = line.split()
                    if len(parts) >= 2:  # noqa: PLR2004 — cgroup cpu.stat is "usage_usec N" (key, value)
                        cpu_usage_usec = int(parts[1])
                    break
        mem_bytes = _read_int(_MEM_CURRENT_V2)
        mem_max_raw = ""
        if _MEM_MAX_V2.exists():
            mem_max_raw = _MEM_MAX_V2.read_text().strip()
        mem_max_bytes = 0 if mem_max_raw in ("", "max") else int(mem_max_raw)
        pids_current = _read_int(_PIDS_CURRENT_V2)
    else:
        # cgroup v1: cpuacct is in nanoseconds; convert to microseconds.
        cpu_usage_usec = _read_int(_CPUACCT_V1) // 1000
        mem_bytes = _read_int(_MEM_USAGE_V1)
        mem_max_bytes = _read_int(_MEM_LIMIT_V1)
        # cgroup v1 represents "no limit" as a huge sentinel value
        # (typically 0x7FFF_FFFF_FFFF_F000 ≈ 2^63). Normalise to 0.
        if mem_max_bytes >= 2**62:
            mem_max_bytes = 0
        pids_current = _read_int(_PIDS_CURRENT_V1)

    return {
        "cpu_usage_usec": cpu_usage_usec,
        "mem_bytes": mem_bytes,
        "mem_max_bytes": mem_max_bytes,
        "pids_current": pids_current,
        "sampled_at": time.time(),
        "cgroup_version": 2 if _is_cgroup_v2() else 1,
    }


def _system_boot_uptime_sec() -> float:
    """Seconds since system boot (for converting /proc/[pid]/stat starttime)."""
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (FileNotFoundError, IndexError, ValueError):
        return 0.0


def list_workers(prefix: str = "office-convert-worker") -> list[dict[str, Any]]:
    """Enumerate worker processes by walking /proc/[pid]/cmdline.

    Filters to processes whose argv[0] basename starts with `prefix`. Returns
    one dict per worker with cumulative CPU + RSS + elapsed time, in the same
    spirit as `docker top` output but namespace-correct (sees only processes
    inside this container's PID namespace, which is what we want).

    Returns:
        List of {pid, cmdline, cpu_usage_usec, rss_bytes, etime_sec, sampled_at}
    """
    workers: list[dict[str, Any]] = []
    proc = Path("/proc")
    clock_ticks = os.sysconf("SC_CLK_TCK")  # usually 100
    page_size = os.sysconf("SC_PAGE_SIZE")  # usually 4096
    boot_uptime_sec = _system_boot_uptime_sec()
    sampled_at = time.time()

    try:
        entries = list(proc.iterdir())
    except (FileNotFoundError, PermissionError):
        return workers

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline_raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if not cmdline_raw:
            continue
        cmdline = cmdline_raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        first = cmdline.split()[0] if cmdline else ""
        basename = first.rsplit("/", 1)[-1]
        if not basename.startswith(prefix):
            continue

        try:
            stat_text = (entry / "stat").read_text()
        except (FileNotFoundError, PermissionError):
            continue
        # /proc/[pid]/stat fields: pid (comm) state ppid ... where `comm` may
        # contain spaces or parens. The rightmost ')' reliably terminates comm.
        rparen = stat_text.rfind(")")
        if rparen < 0:
            continue
        after = stat_text[rparen + 2 :].split()
        # Field offsets after `(comm)` (0-indexed in `after`):
        #   index 11 = utime (clock ticks user mode)
        #   index 12 = stime (clock ticks kernel mode)
        #   index 19 = starttime (clock ticks since system boot)
        try:
            utime = int(after[11])
            stime = int(after[12])
            starttime = int(after[19])
        except (IndexError, ValueError):
            continue
        cpu_usage_usec = ((utime + stime) * 1_000_000) // clock_ticks

        rss_bytes = 0
        try:
            statm_parts = (entry / "statm").read_text().split()
            rss_bytes = int(statm_parts[1]) * page_size
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            pass

        starttime_sec = starttime / clock_ticks
        etime_sec = max(0.0, boot_uptime_sec - starttime_sec)

        workers.append(
            {
                "pid": int(entry.name),
                "cmdline": cmdline,
                "cpu_usage_usec": cpu_usage_usec,
                "rss_bytes": rss_bytes,
                "etime_sec": etime_sec,
                "sampled_at": sampled_at,
            }
        )

    workers.sort(key=lambda w: w["pid"])
    return workers
