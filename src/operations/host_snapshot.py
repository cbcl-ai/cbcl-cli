"""One bounded local observation; sampling never changes configured capacity."""

from pathlib import Path
import os
import time


def snapshot(
    storage: Path, *, proc: Path = Path("/proc"), cgroup: Path = Path("/sys/fs/cgroup")
) -> dict:
    def read(path):
        try:
            with path.open() as stream:
                value = stream.read(65537)
            return value if len(value) <= 65536 else None
        except OSError:
            return None

    filesystem = os.statvfs(storage)
    try:
        load = list(os.getloadavg())
    except OSError:
        load = None
    return {
        "observed_at": time.time(),
        "logical_cpu_count": os.cpu_count(),
        "load_average": load,
        "storage": {
            "path": str(storage),
            "available_bytes": filesystem.f_bavail * filesystem.f_frsize,
            "available_inodes": filesystem.f_favail,
        },
        "host": {
            name: read(proc / name)
            for name in (
                "stat",
                "meminfo",
                "pressure/cpu",
                "pressure/memory",
                "pressure/io",
            )
        },
        "cgroup": {
            name: read(cgroup / name)
            for name in (
                "cpu.max",
                "cpu.stat",
                "memory.current",
                "memory.max",
                "memory.events",
            )
        },
        "capacity_changed": False,
        "limitation": "Single observation; compare timestamped samples during representative concurrent work.",
    }
